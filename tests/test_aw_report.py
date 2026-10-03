"""Tests du calcul de période et de la requête AWQL.

Les bornes et la requête doivent rester identiques à celles du web UI ActivityWatch :
c'est la seule garantie que le script affiche le même « Time active » que le mode navigation.
"""

import importlib.util
import os
import time
from datetime import datetime
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parent.parent / "aw-report.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("aw_report", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aw_report = _load_module()


@pytest.fixture
def toronto_tz():
    """Fuseau avec heure d'été, celui de la machine de référence."""
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "America/Toronto"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


class TestParseStartOfDay:
    def test_parses_ui_format(self):
        assert aw_report.parse_start_of_day("04:00") == (4, 0)
        assert aw_report.parse_start_of_day("06:30") == (6, 30)

    @pytest.mark.parametrize("value", ["", "4", "abc:00", "04:xx"])
    def test_rejects_malformed(self, value):
        with pytest.raises(ValueError, match="startOfDay"):
            aw_report.parse_start_of_day(value)

    @pytest.mark.parametrize("value", ["24:00", "04:60", "-1:00"])
    def test_rejects_out_of_range(self, value):
        with pytest.raises(ValueError, match="startOfDay"):
            aw_report.parse_start_of_day(value)


class TestWeekPeriod:
    def test_starts_monday_at_start_of_day_in_local_time(self, toronto_tz):
        # Vendredi 18/09/2026 15:30 locale -> semaine du lundi 14/09
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_week_period(False, "04:00", "Monday", now=now)

        assert (start.year, start.month, start.day) == (2026, 9, 14)
        assert (start.hour, start.minute) == (4, 0)
        assert start.utcoffset().total_seconds() == -4 * 3600
        assert (end.year, end.month, end.day, end.hour) == (2026, 9, 21, 4)

    def test_monday_belongs_to_its_own_week(self, toronto_tz):
        now = datetime(2026, 9, 14, 5, 0)
        start, _ = aw_report.get_week_period(False, "04:00", "Monday", now=now)
        assert (start.month, start.day) == (9, 14)

    def test_previous_week_is_a_full_monday_to_monday_range(self, toronto_tz):
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_week_period(True, "04:00", "Monday", now=now)

        assert (start.month, start.day, start.hour) == (9, 7, 4)
        assert (end.month, end.day, end.hour) == (9, 14, 4)

    def test_sunday_start_of_week_setting(self, toronto_tz):
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_week_period(False, "04:00", "Sunday", now=now)

        assert (start.month, start.day) == (9, 13)
        assert (end.month, end.day) == (9, 20)

    def test_start_of_day_offset_is_honoured(self, toronto_tz):
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_week_period(False, "06:30", "Monday", now=now)

        assert (start.hour, start.minute) == (6, 30)
        assert (end.hour, end.minute) == (6, 30)

    def test_week_spanning_dst_end_keeps_wall_clock_time(self, toronto_tz):
        # L'heure d'été 2026 se termine le dimanche 01/11 : la semaine du 26/10 la traverse.
        now = datetime(2026, 10, 28, 12, 0)
        start, end = aw_report.get_week_period(False, "04:00", "Monday", now=now)

        # moment.js ajoute une semaine en heure murale : 04:00 des deux côtés du changement,
        # donc la période dure 169 h et non 168 h.
        assert (start.month, start.day, start.hour) == (10, 26, 4)
        assert (end.month, end.day, end.hour) == (11, 2, 4)
        assert start.utcoffset().total_seconds() == -4 * 3600
        assert end.utcoffset().total_seconds() == -5 * 3600
        assert (end - start).total_seconds() == 169 * 3600


class TestDayPeriod:
    def test_today_runs_from_start_of_day_to_next(self, toronto_tz):
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_day_period(0, "04:00", now=now)

        assert (start.month, start.day, start.hour) == (9, 18, 4)
        assert (end.month, end.day, end.hour) == (9, 19, 4)

    def test_n_days_ago(self, toronto_tz):
        now = datetime(2026, 9, 18, 15, 30)
        start, end = aw_report.get_day_period(3, "04:00", now=now)

        assert (start.month, start.day, start.hour) == (9, 15, 4)
        assert (end.month, end.day, end.hour) == (9, 16, 4)

    def test_before_start_of_day_still_uses_current_calendar_date(self, toronto_tz):
        # À 02:00, le web UI vise toujours la date du jour à 04:00 (période majoritairement future).
        now = datetime(2026, 9, 18, 2, 0)
        start, _ = aw_report.get_day_period(0, "04:00", now=now)
        assert (start.day, start.hour) == (18, 4)


class TestQueryClasses:
    def test_keeps_only_typed_rules(self):
        classes = [
            {"name": ["Work"], "rule": {"type": "regex", "regex": "vim"}},
            {"name": ["Comms"], "rule": {"type": None}},
        ]
        result = aw_report.query_classes(classes)
        assert len(result) == 1
        assert result[0][0] == ["Work"]

    def test_tolerates_missing_rule(self):
        assert aw_report.query_classes([{"name": ["X"]}]) == []


class TestDumpsForAwql:
    def test_regex_backslash_is_not_double_escaped(self):
        # json.dumps produirait '\\w', ce que categorize() interpréterait mal.
        assert aw_report.dumps_for_awql([{"regex": r"\w+"}]) == '[{"regex": "\\w+"}]'


class TestBuildActiveQuery:
    def _query(self, **kwargs):
        params = {
            "window_bucket_id": "aw-watcher-window_host",
            "afk_bucket_id": "aw-watcher-afk_host",
            "classes": [{"name": ["Perso"], "rule": {"type": "regex", "regex": "vlc"}}],
            "always_active_pattern": "Slack|Teams",
            "category_name": "Perso",
        }
        params.update(kwargs)
        return "\n".join(aw_report.build_active_query(**params))

    def test_floods_both_buckets_like_the_ui(self):
        q = self._query()
        assert 'events = flood(query_bucket("aw-watcher-window_host"));' in q
        assert 'not_afk = flood(query_bucket("aw-watcher-afk_host"));' in q

    def test_active_time_is_window_events_intersected_with_not_afk(self):
        q = self._query()
        assert "events = filter_period_intersect(events, not_afk);" in q
        assert "active_sec = sum_durations(events);" in q

    def test_always_active_pattern_widens_not_afk_on_app_and_title(self):
        q = self._query()
        assert 'filter_keyvals_regex(events, "app", "Slack|Teams");' in q
        assert 'filter_keyvals_regex(events, "title", "Slack|Teams");' in q
        assert q.count("not_afk = period_union(not_afk, not_treat_as_afk);") == 2

    def test_empty_always_active_pattern_is_skipped(self):
        q = self._query(always_active_pattern="")
        assert "filter_keyvals_regex" not in q
        assert "period_union" not in q

    def test_pattern_double_quotes_are_escaped(self):
        q = self._query(always_active_pattern='say "hi"')
        assert '"say \\"hi\\""' in q

    def test_category_is_filtered_as_a_nested_list(self):
        # $category vaut ["Perso"] : comparer à la chaîne "Perso" ne matcherait rien.
        q = self._query()
        assert 'filter_keyvals(events, "$category", [["Perso"]]);' in q

    def test_without_categories_category_sec_is_zero(self):
        q = self._query(classes=[])
        assert "category_sec = 0;" in q
        assert "categorize(" not in q


class TestFormatWorkdays:
    @pytest.mark.parametrize(
        ("hours", "expected"),
        [
            (0, "0.0"),
            (3.66, "3.66"),
            (7.49, "7.49"),
            (7.5, "1x7.5"),
            (7.51, "1x7.5 + 0.01"),
            (14.95, "1x7.5 + 7.45"),
            (15.0, "2x7.5"),
            (22.5, "3x7.5"),
            (37.5, "5x7.5"),
        ],
    )
    def test_splits_into_workdays(self, hours, expected):
        assert aw_report.format_workdays(hours * 3600) == expected

    def test_negative_duration_keeps_the_sign(self):
        # General Work peut passer sous zéro si IT + Perso dépassent le temps actif.
        assert aw_report.format_workdays(-1.2 * 3600) == "-1.2"
        assert aw_report.format_workdays(-9.0 * 3600) == "-1x7.5 + 1.5"

    def test_split_matches_the_duration_displayed_next_to_it(self):
        # format_duration affiche 14.95 h : 1x7.5 + 7.45 doit retomber sur 14.95.
        seconds = 14.95 * 3600
        assert aw_report.format_duration(seconds).endswith("(14.95 h)")
        assert 1 * 7.5 + 7.45 == pytest.approx(14.95)
        assert aw_report.format_workdays(seconds) == "1x7.5 + 7.45"


class TestStopwatchDuration:
    def test_sums_matching_label_case_insensitively(self):
        entries = [
            {"duration": 60, "data": {"label": "IT"}},
            {"duration": 30, "data": {"label": " it "}},
            {"duration": 99, "data": {"label": "Autre"}},
        ]
        assert aw_report.stopwatch_duration(entries, "IT") == 90

    def test_returns_zero_without_match(self):
        assert aw_report.stopwatch_duration([{"duration": 5, "data": {}}], "IT") == 0
