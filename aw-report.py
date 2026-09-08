import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

from aw_client import ActivityWatchClient
from requests.exceptions import ConnectionError

STOPWATCH_BUCKET_ID = "aw-stopwatch"
CATEGORY_NAME = "Perso"
IT_LABEL = "IT"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Résumé AFK et stopwatch ActivityWatch")
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
    return parser.parse_args()


def get_week_period(previous_week: bool) -> tuple[datetime, datetime, datetime]:
    """Retourne (début de semaine, fin de la requête, fin de semaine affichée)."""
    now = datetime.now(timezone.utc)
    monday_this_week = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    if previous_week:
        week_start = monday_this_week - timedelta(days=7)
        query_end = monday_this_week
    else:
        week_start = monday_this_week
        query_end = now
    week_end = week_start + timedelta(days=6)
    return week_start, query_end, week_end


def get_day_period(days_ago: int) -> tuple[datetime, datetime, datetime]:
    """Retourne (début de journée, fin de la requête, fin de journée affichée)."""
    now = datetime.now(timezone.utc)
    day_start = (now - timedelta(days=days_ago)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    query_end = now if days_ago == 0 else day_start + timedelta(days=1)
    return day_start, query_end, day_start


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


def main() -> None:
    args = parse_args()
    client = ActivityWatchClient("script-aw-summary", port=args.server_port)

    try:
        buckets = client.get_buckets()
    except ConnectionError as e:
        print(f"[ERROR] Impossible de contacter le serveur ActivityWatch: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        afk_bucket_id = next(b for b in buckets if b.startswith("aw-watcher-afk_"))
    except StopIteration:
        print("[ERROR] Aucun bucket aw-watcher-afk trouvé, le watcher AFK est-il lancé ?", file=sys.stderr)
        sys.exit(1)

    window_bucket_id = next((b for b in buckets if b.startswith("aw-watcher-window_")), None)
    if window_bucket_id is None:
        print("[WARN] Aucun bucket aw-watcher-window trouvé, catégorie ignorée.", file=sys.stderr)

    try:
        classes = client.get_setting("classes") if window_bucket_id else []
    except Exception as e:
        print(f"[WARN] Impossible de récupérer les catégories: {e}", file=sys.stderr)
        classes = []
    categories = [[c["name"], c["rule"]] for c in classes]

    has_stopwatch = STOPWATCH_BUCKET_ID in buckets

    if args.day is not None:
        period_start, query_end, period_end = get_day_period(args.day)
        if args.day == 0:
            period_label = "aujourd'hui"
        elif args.day == 1:
            period_label = "hier"
        else:
            period_label = f"il y a {args.day} jours"
    else:
        period_start, query_end, period_end = get_week_period(args.previous_week)
        period_label = "semaine précédente" if args.previous_week else "semaine en cours"
    period = (period_start, query_end)

    query = [
        f'events = query_bucket("{afk_bucket_id}");',
        'afk_events = filter_keyvals(events, "status", ["afk"]);',
        'not_afk_events = filter_keyvals(events, "status", ["not-afk"]);',
        "afk_sec = sum_durations(afk_events);",
        "not_afk_sec = sum_durations(not_afk_events);",
    ]
    if has_stopwatch:
        query += [
            f'sw_events = query_bucket("{STOPWATCH_BUCKET_ID}");',
            'sw_by_label = merge_events_by_keys(sw_events, ["label"]);',
            "sw_by_label = sort_by_duration(sw_by_label);",
            "sw_total_sec = sum_durations(sw_events);",
        ]
    else:
        query += ["sw_by_label = [];", "sw_total_sec = 0;"]

    if window_bucket_id and categories:
        query += [
            f'window_events = query_bucket("{window_bucket_id}");',
            "window_events = filter_period_intersect(window_events, not_afk_events);",
            f"window_events = categorize(window_events, {json.dumps(categories)});",
            f'category_events = filter_keyvals(window_events, "$category", [{json.dumps([CATEGORY_NAME])}]);',
            "category_sec = sum_durations(category_events);",
        ]
    else:
        query += ["category_sec = 0;"]

    query.append(
        'RETURN = {"afk_sec": afk_sec, "not_afk_sec": not_afk_sec, '
        '"stopwatch_by_label": sw_by_label, "stopwatch_total_sec": sw_total_sec, '
        '"category_sec": category_sec};'
    )

    try:
        res = client.query("\n".join(query), [period])[0]
    except Exception as e:
        print(f"[ERROR] Échec de la requête AWQL: {e}", file=sys.stderr)
        sys.exit(1)

    if args.day is not None:
        print(f"--- {period_label.capitalize()} : {period_start:%d/%m/%Y} ---")
    else:
        print(f"--- {period_label.capitalize()} : du {period_start:%d/%m/%Y} au {period_end:%d/%m/%Y} ---")
    print(f"AFK : {format_duration(res['afk_sec'])}")
    print(f"Non-AFK : {format_duration(res['not_afk_sec'])}")
    print(f"AFK + Non-AFK : {format_duration(res['afk_sec'] + res['not_afk_sec'])}")
    print(f"Catégorie {CATEGORY_NAME} : {format_duration(res['category_sec'])}")

    # print(f"Stopwatch total : {format_duration(res['stopwatch_total_sec'])}")
    if not res["stopwatch_by_label"]:
        print("Stopwatch : aucune session enregistrée")
    for entry in res["stopwatch_by_label"]:
        entry_label = entry["data"].get("label", "(sans label)")
        print(f"Stopwatch - {entry_label} : {format_duration(entry['duration'])}")

    # Le temps IT est pointé au stopwatch pendant que l'AFK tourne : il est déjà inclus
    # dans AFK + Non-AFK, on le retranche donc (comme le Perso) pour isoler le travail général.
    total_sec = res["afk_sec"] + res["not_afk_sec"]
    it_sec = stopwatch_duration(res["stopwatch_by_label"], IT_LABEL)
    general_work_sec = total_sec - it_sec - res["category_sec"]

    print("--- Work performed ---")
    print(f"General Work : {format_duration(general_work_sec)}")
    print(f"IT : {format_duration(it_sec)}")


if __name__ == "__main__":
    main()
