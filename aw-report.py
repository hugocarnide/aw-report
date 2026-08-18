import argparse
import sys
from datetime import datetime, timedelta, timezone

from aw_client import ActivityWatchClient
from requests.exceptions import ConnectionError

STOPWATCH_BUCKET_ID = "aw-stopwatch"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Résumé AFK et stopwatch ActivityWatch")
    parser.add_argument(
        "--previous-week",
        "-p",
        action="store_true",
        help="Afficher la semaine précédente (lundi à lundi) au lieu de la semaine en cours",
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


def format_duration(seconds: float) -> str:
    minutes = round(seconds / 60, 2)
    hours = round(seconds / 3600, 2)
    return f"{minutes} min ({hours} h)"


def main() -> None:
    args = parse_args()
    client = ActivityWatchClient("script-aw-summary")

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

    has_stopwatch = STOPWATCH_BUCKET_ID in buckets
    week_start, query_end, week_end = get_week_period(args.previous_week)
    period = (week_start, query_end)
    period_label = "semaine précédente" if args.previous_week else "semaine en cours"

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

    query.append(
        'RETURN = {"afk_sec": afk_sec, "not_afk_sec": not_afk_sec, '
        '"stopwatch_by_label": sw_by_label, "stopwatch_total_sec": sw_total_sec};'
    )

    try:
        res = client.query("\n".join(query), [period])[0]
    except Exception as e:
        print(f"[ERROR] Échec de la requête AWQL: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"--- {period_label.capitalize()} : du {week_start:%d/%m/%Y} au {week_end:%d/%m/%Y} ---")
    print(f"AFK : {format_duration(res['afk_sec'])}")
    print(f"Non-AFK : {format_duration(res['not_afk_sec'])}")

    # print(f"Stopwatch total : {format_duration(res['stopwatch_total_sec'])}")
    if not res["stopwatch_by_label"]:
        print("Stopwatch : aucune session enregistrée")
    for entry in res["stopwatch_by_label"]:
        entry_label = entry["data"].get("label", "(sans label)")
        print(f"Stopwatch - {entry_label} : {format_duration(entry['duration'])}")


if __name__ == "__main__":
    main()
