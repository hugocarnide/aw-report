import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta

from aw_client import ActivityWatchClient
from requests.exceptions import ConnectionError

from aw_fix_gaps import FixGapsError, fix_gaps
from aw_log import log_debug
from aw_windows import (
    DEFAULT_WINDOWS_PORT,
    WindowsDataError,
    ensure_mounted,
    windows_server,
)

STOPWATCH_BUCKET_ID = "aw-stopwatch"
CATEGORY_NAME = "Perso"
IT_LABEL = "IT"
WORKDAY_HOURS = 7.5

# Valeurs de repli alignées sur les défauts du web UI (src/stores/settings.ts).
DEFAULT_START_OF_DAY = "04:00"
DEFAULT_START_OF_WEEK = "Monday"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Résumé AFK et stopwatch ActivityWatch"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--previous-week",
        "-p",
        default=False,
        action="store_true",
        help="Afficher la semaine précédente (lundi à lundi) au lieu de la semaine en cours",
    )
    group.add_argument(
        "--day",
        "-j",
        nargs="?",
        const=0,
        type=int,
        default=None,
        metavar="N",
        help="Afficher une seule journée : aujourd'hui (-j) ou N jours en arrière (-j N)",
    )
    parser.add_argument(
        "--server-port",
        "-s",
        nargs="?",
        const=0,
        type=int,
        default=5600,
        metavar="N",
        help="Port du serveur ActivityWatch",
    )
    parser.add_argument(
        "--fix-gaps",
        default=False,
        action="store_true",
        help="Combler les trous laissés par awatcher avant le rapport "
        "(arrête ActivityWatch, corrige la base, relance aw-qt)",
    )
    parser.add_argument(
        "--windows",
        default=False,
        action="store_true",
        help="Éditer aussi le rapport des données ActivityWatch de Windows "
        "(monte /mnt/windows et sert sa base sur un second port)",
    )
    parser.add_argument(
        "--windows-port",
        type=int,
        default=DEFAULT_WINDOWS_PORT,
        metavar="N",
        help=f"Port servant la base Windows (défaut {DEFAULT_WINDOWS_PORT})",
    )
    parser.add_argument(
        "--debug",
        default=False,
        action="store_true",
        help="Afficher la requête AWQL et les paramètres résolus",
    )
    return parser.parse_args()


def parse_start_of_day(start_of_day: str) -> tuple[int, int]:
    """Convertit le réglage 'HH:MM' du web UI en (heures, minutes)."""
    try:
        hours, minutes = (int(part) for part in start_of_day.split(":", 1))
    except ValueError as e:
        raise ValueError(
            f"Réglage startOfDay invalide: {start_of_day!r} (attendu 'HH:MM')"
        ) from e
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        raise ValueError(f"Réglage startOfDay hors bornes: {start_of_day!r}")
    return hours, minutes


def localize(naive: datetime) -> datetime:
    """Attache le fuseau local à une date naïve.

    On garde des datetimes naïfs jusqu'au bout des additions de jours pour reproduire
    l'arithmétique « heure murale » de moment.js : +7 jours reste à la même heure locale
    même quand un changement d'heure survient dans l'intervalle.
    """
    return naive.astimezone()


def get_week_period(
    previous_week: bool,
    start_of_day: str,
    start_of_week: str,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Retourne (début, fin) de la semaine, bornes identiques à celles du web UI.

    Le web UI fait moment(date).startOf('isoWeek').hour(H).minute(M) puis +1 semaine.
    """
    hours, minutes = parse_start_of_day(start_of_day)
    reference = (now or datetime.now()) - timedelta(days=7 if previous_week else 0)
    # startOf('isoWeek') = lundi ; startOf('week') = dimanche
    days_since_start = (
        reference.weekday()
        if start_of_week == "Monday"
        else (reference.weekday() + 1) % 7
    )
    start = (reference - timedelta(days=days_since_start)).replace(
        hour=hours, minute=minutes, second=0, microsecond=0
    )
    return localize(start), localize(start + timedelta(days=7))


def get_day_period(
    days_ago: int, start_of_day: str, now: datetime | None = None
) -> tuple[datetime, datetime]:
    """Retourne (début, fin) de la journée, bornes identiques à celles du web UI."""
    hours, minutes = parse_start_of_day(start_of_day)
    start = ((now or datetime.now()) - timedelta(days=days_ago)).replace(
        hour=hours, minute=minutes, second=0, microsecond=0
    )
    return localize(start), localize(start + timedelta(days=1))


def query_classes(classes: list[dict]) -> list[list]:
    """Catégories au format attendu par categorize().

    Les règles de type null sont écartées comme le fait le web UI (classes_for_query) :
    ce sont des catégories parentes purement structurelles.
    """
    return [
        [c["name"], c["rule"]]
        for c in classes
        if c.get("rule", {}).get("type") is not None
    ]


def dumps_for_awql(value) -> str:
    """Sérialise en littéral AWQL.

    Le double antislash ajouté par json.dumps est ramené à un simple antislash pour que
    les motifs regex comme '\\w' restent fonctionnels (même traitement que le web UI).
    """
    return json.dumps(value).replace("\\\\", "\\")


def build_active_query(
    window_bucket_id: str,
    afk_bucket_id: str,
    classes: list[dict],
    always_active_pattern: str,
    category_name: str,
) -> list[str]:
    """Réplique de fullDesktopQuery (aw-webui/src/queries.ts) avec filter_afk activé.

    Le « Time active » affiché par le web UI est sum_durations des événements de fenêtre
    intersectés avec les périodes non-AFK, et non la somme brute du bucket AFK.
    """
    categories = query_classes(classes)
    lines = [
        f'events = flood(query_bucket("{window_bucket_id}"));',
        f'not_afk = flood(query_bucket("{afk_bucket_id}"));',
        'not_afk = filter_keyvals(not_afk, "status", ["not-afk"]);',
    ]
    if always_active_pattern:
        # Ces applications comptent comme actives sans input clavier/souris (réunions, chat).
        pattern = always_active_pattern.replace('"', '\\"')
        for key in ("app", "title"):
            lines += [
                f'not_treat_as_afk = filter_keyvals_regex(events, "{key}", "{pattern}");',
                "not_afk = period_union(not_afk, not_treat_as_afk);",
            ]
    lines.append("events = filter_period_intersect(events, not_afk);")
    if categories:
        lines.append(f"events = categorize(events, {dumps_for_awql(categories)});")
        lines += [
            f'category_events = filter_keyvals(events, "$category", [{dumps_for_awql([category_name])}]);',
            "category_sec = sum_durations(category_events);",
        ]
    else:
        lines.append("category_sec = 0;")
    lines += [
        "active_sec = sum_durations(events);",
        "not_afk_sec = sum_durations(not_afk);",
    ]
    return lines


def build_raw_afk_query(afk_bucket_id: str) -> list[str]:
    """Temps AFK brut, affiché à titre indicatif (hors base de calcul)."""
    return [
        f'raw_afk_events = query_bucket("{afk_bucket_id}");',
        'raw_afk_events = filter_keyvals(raw_afk_events, "status", ["afk"]);',
        "afk_sec = sum_durations(raw_afk_events);",
    ]


def build_stopwatch_query(has_stopwatch: bool) -> list[str]:
    if not has_stopwatch:
        return ["sw_by_label = [];"]
    return [
        f'sw_events = query_bucket("{STOPWATCH_BUCKET_ID}");',
        'sw_by_label = merge_events_by_keys(sw_events, ["label"]);',
        "sw_by_label = sort_by_duration(sw_by_label);",
    ]


def stopwatch_duration(entries: list[dict], label: str) -> float:
    """Somme des durées des sessions stopwatch portant ce label (insensible à la casse)."""
    target = label.strip().lower()
    return sum(
        entry["duration"]
        for entry in entries
        if entry["data"].get("label", "").strip().lower() == target
    )


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60, 2)
    hours = round(seconds / 3600, 2)
    return f"{minutes} min ({hours} h)"


def format_workdays(seconds: float) -> str:
    """Découpe une durée en journées de travail : 14.95 h -> '1x7.5 + 7.45'.

    Le découpage part des heures déjà arrondies par format_duration pour que
    journées x 7.5 + reste redonne exactement la valeur affichée à côté.
    """
    hours = round(seconds / 3600, 2)
    sign = "-" if hours < 0 else ""
    days, remainder = divmod(abs(hours), WORKDAY_HOURS)
    remainder = round(remainder, 2)
    # Un reste qui remonte à 7.5 par arrondi doit basculer en journée complète.
    if remainder >= WORKDAY_HOURS:
        days += 1
        remainder = 0.0

    if not days:
        return f"{sign}{remainder}"
    if not remainder:
        return f"{sign}{int(days)}x{WORKDAY_HOURS}"
    return f"{sign}{int(days)}x{WORKDAY_HOURS} + {remainder}"


@dataclass(frozen=True)
class ReportTotals:
    """Agrégats d'un rapport.

    Les deux systèmes sont en dual boot : leurs périodes d'activité ne se recouvrent
    jamais, additionner Linux et Windows donne donc bien le temps réellement passé.
    """

    active_sec: float
    not_afk_sec: float
    afk_sec: float
    category_sec: float
    it_sec: float

    @property
    def general_work_sec(self) -> float:
        """Temps actif hors IT et hors Perso.

        Calculé plutôt que stocké : la somme de deux rapports reste ainsi cohérente avec
        la somme de leurs composantes.
        """
        return self.active_sec - self.it_sec - self.category_sec

    def __add__(self, other: "ReportTotals") -> "ReportTotals":
        return ReportTotals(
            active_sec=self.active_sec + other.active_sec,
            not_afk_sec=self.not_afk_sec + other.not_afk_sec,
            afk_sec=self.afk_sec + other.afk_sec,
            category_sec=self.category_sec + other.category_sec,
            it_sec=self.it_sec + other.it_sec,
        )


def print_totals(totals: ReportTotals, stopwatch_entries: list[dict] | None) -> None:
    """Affiche un bloc de résultats. `stopwatch_entries` à None pour un cumul."""
    print(f"Temps actif (Time active web UI) : {format_duration(totals.active_sec)}")
    print(f"  Non-AFK : {format_duration(totals.not_afk_sec)}")
    print(f"  AFK : {format_duration(totals.afk_sec)}")
    print(f"Catégorie {CATEGORY_NAME} : {format_duration(totals.category_sec)}")

    if stopwatch_entries is not None:
        if not stopwatch_entries:
            print("Stopwatch : aucune session enregistrée")
        for entry in stopwatch_entries:
            entry_label = entry["data"].get("label", "(sans label)")
            print(f"Stopwatch - {entry_label} : {format_duration(entry['duration'])}")

    print("--- Work performed ---")
    print(
        f"General Work : {format_duration(totals.general_work_sec)} "
        f"= {format_workdays(totals.general_work_sec)}"
    )
    print(f"IT : {format_duration(totals.it_sec)}")


def run_report(
    port: int, args: argparse.Namespace, fix_gaps_enabled: bool
) -> ReportTotals:
    """Édite le rapport pour le serveur écoutant sur `port`.

    Le port est explicite plutôt que lu dans `args` : la même fonction sert le serveur
    local et l'instance pointée sur la base Windows. Les agrégats sont retournés pour
    que l'appelant puisse cumuler plusieurs systèmes.
    """
    client = ActivityWatchClient("script-aw-summary", port=port)

    try:
        buckets = client.get_buckets()
    except ConnectionError as e:
        print(
            f"[ERROR] Impossible de contacter le serveur ActivityWatch: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        afk_bucket_id = next(b for b in buckets if b.startswith("aw-watcher-afk_"))
    except StopIteration:
        print(
            "[ERROR] Aucun bucket aw-watcher-afk trouvé, le watcher AFK est-il lancé ?",
            file=sys.stderr,
        )
        sys.exit(1)

    window_bucket_id = next(
        (b for b in buckets if b.startswith("aw-watcher-window_")), None
    )
    if window_bucket_id is None:
        print(
            "[ERROR] Aucun bucket aw-watcher-window trouvé : le temps actif du web UI ne peut pas "
            "être reproduit sans les événements de fenêtre.",
            file=sys.stderr,
        )
        sys.exit(1)

    if any(b.startswith("aw-watcher-web") for b in buckets):
        print(
            "[WARN] Bucket navigateur détecté : l'option « Count audible browser tab as active » "
            "du web UI n'est pas reproduite, un léger écart est possible.",
            file=sys.stderr,
        )

    try:
        settings = client.get_setting()
    except Exception as e:
        print(
            f"[ERROR] Impossible de récupérer les réglages du serveur: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    classes = settings.get("classes") or []
    always_active_pattern = settings.get("always_active_pattern") or ""
    start_of_day = settings.get("startOfDay") or DEFAULT_START_OF_DAY
    start_of_week = settings.get("startOfWeek") or DEFAULT_START_OF_WEEK

    try:
        if args.day is not None:
            period_start, period_end = get_day_period(args.day, start_of_day)
            if args.day == 0:
                period_label = "aujourd'hui"
            elif args.day == 1:
                period_label = "hier"
            else:
                period_label = f"il y a {args.day} jours"
        else:
            period_start, period_end = get_week_period(
                args.previous_week, start_of_day, start_of_week
            )
            period_label = (
                "semaine précédente" if args.previous_week else "semaine en cours"
            )
    except ValueError as e:
        print(f"[ERROR] Calcul de la période impossible: {e}", file=sys.stderr)
        sys.exit(1)

    # La correction arrête puis relance le serveur : elle doit encadrer exactement la
    # période qui sera interrogée juste après, d'où sa place après le calcul des bornes.
    if fix_gaps_enabled:
        try:
            fix_gaps(period_start, period_end, port)
        except (FixGapsError, ValueError) as e:
            print(f"[ERROR] Correction des gaps impossible: {e}", file=sys.stderr)
            sys.exit(1)

    has_stopwatch = STOPWATCH_BUCKET_ID in buckets
    query = (
        build_active_query(
            window_bucket_id,
            afk_bucket_id,
            classes,
            always_active_pattern,
            CATEGORY_NAME,
        )
        + build_raw_afk_query(afk_bucket_id)
        + build_stopwatch_query(has_stopwatch)
        + [
            (
                'RETURN = {"active_sec": active_sec, "not_afk_sec": not_afk_sec, '
                '"afk_sec": afk_sec, "category_sec": category_sec, '
                '"stopwatch_by_label": sw_by_label};'
            )
        ]
    )

    if args.debug:
        log_debug(
            "main", f"periode={period_start.isoformat()}/{period_end.isoformat()}"
        )
        log_debug("main", f"startOfDay={start_of_day} startOfWeek={start_of_week}")
        log_debug("main", f"always_active_pattern={always_active_pattern!r}")
        log_debug(
            "main", f"categories retenues={len(query_classes(classes))}/{len(classes)}"
        )
        log_debug("main", "requête AWQL:\n" + "\n".join(query))

    try:
        res = client.query("\n".join(query), [(period_start, period_end)])[0]
    except Exception as e:
        print(
            f"[ERROR] Échec de la requête AWQL sur {afk_bucket_id}/{window_bucket_id}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.day is not None:
        print(
            f"--- {period_label.capitalize()} : {period_start:%d/%m/%Y %H:%M} → {period_end:%d/%m/%Y %H:%M} ---"
        )
    else:
        print(
            f"--- {period_label.capitalize()} : du {period_start:%d/%m/%Y %H:%M} au {period_end:%d/%m/%Y %H:%M} ---"
        )
    # Le temps IT est pointé au stopwatch pendant que l'activité est enregistrée : il est déjà
    # inclus dans le temps actif, on le retranche donc (comme le Perso) pour isoler le travail général.
    totals = ReportTotals(
        active_sec=res["active_sec"],
        not_afk_sec=res["not_afk_sec"],
        afk_sec=res["afk_sec"],
        category_sec=res["category_sec"],
        it_sec=stopwatch_duration(res["stopwatch_by_label"], IT_LABEL),
    )
    print_totals(totals, res["stopwatch_by_label"])
    return totals


def main() -> None:
    args = parse_args()

    if not args.windows:
        run_report(args.server_port, args, fix_gaps_enabled=args.fix_gaps)
        return

    # Le rapport Linux passe en premier : --fix-gaps y fait un `killall aw-server-rust`,
    # qui emporterait l'instance servant la base Windows si elle tournait déjà.
    print(f"=== Usage Linux (port {args.server_port}) ===")
    linux_totals = run_report(args.server_port, args, fix_gaps_enabled=args.fix_gaps)

    try:
        ensure_mounted()
        with windows_server(args.windows_port) as windows_port:
            print()
            print(f"=== Usage Windows (port {windows_port}) ===")
            windows_totals = run_report(windows_port, args, fix_gaps_enabled=False)
    except WindowsDataError as e:
        print(f"[ERROR] Données Windows indisponibles: {e}", file=sys.stderr)
        sys.exit(1)

    print()
    print("=== Total Linux + Windows ===")
    print_totals(linux_totals + windows_totals, stopwatch_entries=None)


if __name__ == "__main__":
    main()
