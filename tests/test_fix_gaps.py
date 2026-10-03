"""Tests de l'orchestration de la correction des gaps.

Le comportement vérifié est celui qui compte pour l'utilisateur : la fenêtre corrigée est
exactement celle que le rapport affiche, la base n'est jamais touchée pendant qu'aw-server
tourne, et le rapport n'est lancé qu'une fois le serveur de nouveau joignable.
"""

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import aw_fix_gaps

START_NS = 1789099200000000000
END_NS = 1789704000000000000


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Les attentes sont testées via leur logique, pas via le temps réel."""
    monkeypatch.setattr(aw_fix_gaps.time, "sleep", lambda _: None)


class TestToNs:
    def test_converts_a_bound_to_epoch_nanoseconds(self):
        moment = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
        assert aw_fix_gaps.to_ns(moment) == int(moment.timestamp()) * 1_000_000_000

    def test_has_the_19_digits_fix_gaps_requires(self):
        assert len(str(aw_fix_gaps.to_ns(datetime.now().astimezone()))) == 19

    def test_keeps_full_precision(self):
        """int(timestamp() * 1e9) tronquerait : un float64 ne porte pas 19 chiffres."""
        moment = datetime(2026, 9, 18, 4, 0, 0, 123456, tzinfo=timezone.utc)
        assert aw_fix_gaps.to_ns(moment) % 1_000_000_000 == 123_456_000


class TestStopActivitywatch:
    def test_kills_supervisor_before_server(self, monkeypatch):
        """aw-qt doit tomber en premier, sinon il relance aw-server-rust."""
        killed = []
        monkeypatch.setattr(aw_fix_gaps, "_is_running", lambda p: p != "aw-server-rust")

        def fake_run(command, **kwargs):
            killed.append(command[1])
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(aw_fix_gaps.subprocess, "run", fake_run)
        aw_fix_gaps.stop_activitywatch()
        assert killed == ["aw-qt", "aw-awatcher"]

    def test_raises_when_server_survives(self, monkeypatch):
        monkeypatch.setattr(aw_fix_gaps, "_is_running", lambda p: True)
        monkeypatch.setattr(
            aw_fix_gaps.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        )
        with pytest.raises(aw_fix_gaps.FixGapsError, match="toujours actif"):
            aw_fix_gaps.stop_activitywatch(timeout_s=0)


class TestRunFixGaps:
    @pytest.fixture
    def paths(self, tmp_path):
        script = tmp_path / "fix-gaps.sh"
        script.write_text("#!/bin/bash\nexit 0\n")
        db = tmp_path / "sqlite.db"
        db.write_bytes(b"SQLite format 3\x00")
        return script, db

    def test_bounds_both_ends_of_the_period(self, monkeypatch, paths):
        """Sans -e, fix-gaps.sh corrigerait aussi les événements hors période."""
        script, db = paths
        seen = {}

        def fake_run(command, **kwargs):
            seen["command"] = command
            return subprocess.CompletedProcess(command, 0)

        monkeypatch.setattr(aw_fix_gaps.subprocess, "run", fake_run)
        aw_fix_gaps.run_fix_gaps(START_NS, END_NS, db_path=db, script_path=script)
        assert seen["command"] == [
            "bash",
            str(script),
            "-f",
            str(db),
            "-s",
            str(START_NS),
            "-e",
            str(END_NS),
            "-b",
        ]

    def test_rejects_an_inverted_period(self, paths):
        script, db = paths
        with pytest.raises(aw_fix_gaps.FixGapsError, match="inversée"):
            aw_fix_gaps.run_fix_gaps(END_NS, START_NS, db_path=db, script_path=script)

    def test_reports_missing_database(self, paths):
        script, _ = paths
        with pytest.raises(aw_fix_gaps.FixGapsError, match="Base ActivityWatch"):
            aw_fix_gaps.run_fix_gaps(
                START_NS, END_NS, db_path=Path("/nowhere/sqlite.db"), script_path=script
            )

    def test_reports_missing_script(self, paths):
        _, db = paths
        with pytest.raises(aw_fix_gaps.FixGapsError, match="introuvable"):
            aw_fix_gaps.run_fix_gaps(
                START_NS, END_NS, db_path=db, script_path=Path("/nowhere/fix.sh")
            )

    def test_failure_is_not_silent(self, monkeypatch, paths):
        script, db = paths
        monkeypatch.setattr(
            aw_fix_gaps.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(command, 1),
        )
        with pytest.raises(aw_fix_gaps.FixGapsError, match="code 1"):
            aw_fix_gaps.run_fix_gaps(START_NS, END_NS, db_path=db, script_path=script)


class TestFixGapsSequence:
    def test_steps_run_in_order(self, monkeypatch):
        """Écrire avant l'arrêt corromprait la base ; lire avant le redémarrage échouerait."""
        steps = []
        monkeypatch.setattr(
            aw_fix_gaps, "stop_activitywatch", lambda: steps.append("stop")
        )
        monkeypatch.setattr(
            aw_fix_gaps,
            "run_fix_gaps",
            lambda start_ns, end_ns, **kwargs: steps.append("fix"),
        )
        monkeypatch.setattr(
            aw_fix_gaps, "start_activitywatch", lambda: steps.append("start")
        )
        monkeypatch.setattr(
            aw_fix_gaps, "wait_for_server", lambda port: steps.append("wait")
        )
        now = datetime.now().astimezone()
        aw_fix_gaps.fix_gaps(now - timedelta(days=7), now, 5600)
        assert steps == ["stop", "fix", "start", "wait"]


class TestPeriodMatchesTheReport:
    """La fenêtre corrigée doit être celle que le rapport interroge, pas une approximation."""

    @pytest.fixture
    def run_main(self, aw_report, monkeypatch):
        def run(*argv: str) -> dict:
            calls = {}
            monkeypatch.setattr(aw_report.sys, "argv", ["aw-report.py", *argv])
            monkeypatch.setattr(
                aw_report,
                "fix_gaps",
                lambda start, end, port: calls.update(fixed=(start, end), port=port),
            )
            client = _FakeClient()
            monkeypatch.setattr(
                aw_report, "ActivityWatchClient", lambda *a, **k: client
            )
            aw_report.main()
            calls["queried"] = client.queried_period
            return calls

        return run

    def test_default_run_corrects_the_current_week(self, run_main, aw_report, capsys):
        calls = run_main("--fix-gaps")
        expected = aw_report.get_week_period(False, "04:00", "Monday")
        assert calls["fixed"] == expected
        assert calls["fixed"] == calls["queried"]

    def test_previous_week_corrects_that_week_only(self, run_main, aw_report, capsys):
        calls = run_main("--fix-gaps", "-p")
        assert calls["fixed"] == aw_report.get_week_period(True, "04:00", "Monday")
        assert calls["fixed"] == calls["queried"]

    def test_day_option_corrects_that_day_only(self, run_main, aw_report, capsys):
        calls = run_main("--fix-gaps", "-j", "3")
        start, end = calls["fixed"]
        assert (start, end) == aw_report.get_day_period(3, "04:00")
        assert end - start == timedelta(days=1)
        assert calls["fixed"] == calls["queried"]

    def test_server_port_is_forwarded(self, run_main, capsys):
        assert run_main("--fix-gaps", "-s", "5666")["port"] == 5666

    def test_nothing_is_corrected_without_the_flag(self, run_main, capsys):
        assert "fixed" not in run_main("-p")


class _FakeClient:
    """Serveur ActivityWatch minimal : juste ce que main() consomme."""

    def __init__(self):
        self.queried_period = None

    def get_buckets(self):
        return {"aw-watcher-afk_host": {}, "aw-watcher-window_host": {}}

    def get_setting(self):
        return {"classes": [], "startOfDay": "04:00", "startOfWeek": "Monday"}

    def query(self, query, periods):
        self.queried_period = periods[0]
        return [
            {
                "active_sec": 0,
                "not_afk_sec": 0,
                "afk_sec": 0,
                "category_sec": 0,
                "stopwatch_by_label": [],
            }
        ]
