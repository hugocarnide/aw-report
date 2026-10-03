"""Tests de la lecture des données ActivityWatch de Windows depuis Linux.

Ce qui compte pour l'utilisateur : la partition est montée si besoin, l'instance servant
la base Windows est toujours arrêtée en sortie, et les deux rapports sortent dans l'ordre
qui ne fait pas tuer cette instance par --fix-gaps.
"""

import subprocess

import pytest

import aw_windows


@pytest.fixture
def windows_paths(tmp_path):
    """Binaire et base factices : aucun vrai serveur n'est lancé."""
    binary = tmp_path / "aw-server-rust"
    binary.write_text("#!/bin/sh\nsleep 30\n")
    db = tmp_path / "sqlite.db"
    db.write_bytes(b"SQLite format 3\x00")
    return binary, db


class _FakeProcess:
    def __init__(self, hangs: bool = False):
        self.terminated = False
        self.killed = False
        self._hangs = hangs

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        if self._hangs and timeout is not None:
            raise subprocess.TimeoutExpired("aw-server-rust", timeout)
        return 0

    def kill(self):
        self.killed = True
        self._hangs = False


class TestEnsureMounted:
    def test_does_nothing_when_already_mounted(self, monkeypatch):
        monkeypatch.setattr(aw_windows.os.path, "ismount", lambda p: True)
        monkeypatch.setattr(
            aw_windows.subprocess,
            "run",
            lambda *a, **k: pytest.fail("mount ne doit pas être rappelé"),
        )
        aw_windows.ensure_mounted()

    def test_mounts_when_needed(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(aw_windows.os.path, "ismount", lambda p: "mounted" in seen)

        def fake_run(command, **kwargs):
            seen["mounted"] = command
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(aw_windows.subprocess, "run", fake_run)
        aw_windows.ensure_mounted()
        assert seen["mounted"] == ["mount", str(aw_windows.MOUNT_POINT)]

    def test_hibernated_windows_gives_an_actionable_error(self, monkeypatch):
        monkeypatch.setattr(aw_windows.os.path, "ismount", lambda p: False)
        monkeypatch.setattr(
            aw_windows.subprocess,
            "run",
            lambda command, **kwargs: subprocess.CompletedProcess(
                command, 32, "", "falling back to read-only mount: volume is dirty"
            ),
        )
        with pytest.raises(aw_windows.WindowsDataError, match="veille prolongée"):
            aw_windows.ensure_mounted()


class TestWindowsServer:
    def test_serves_the_windows_database_on_the_given_port(
        self, monkeypatch, windows_paths
    ):
        binary, db = windows_paths
        seen = {}

        def fake_popen(command, **kwargs):
            seen["command"] = command
            return _FakeProcess()

        monkeypatch.setattr(aw_windows.subprocess, "Popen", fake_popen)
        monkeypatch.setattr(aw_windows, "wait_for_server", lambda port: None)

        with aw_windows.windows_server(5702, db_path=db, binary=binary) as port:
            assert port == 5702
        assert seen["command"] == [
            str(binary),
            "--port",
            "5702",
            "--dbpath",
            str(db),
            "--no-legacy-import",
        ]

    def test_instance_is_stopped_on_exit(self, monkeypatch, windows_paths):
        binary, db = windows_paths
        process = _FakeProcess()
        monkeypatch.setattr(aw_windows.subprocess, "Popen", lambda *a, **k: process)
        monkeypatch.setattr(aw_windows, "wait_for_server", lambda port: None)

        with aw_windows.windows_server(5702, db_path=db, binary=binary):
            pass
        assert process.terminated

    def test_instance_is_stopped_even_when_the_report_fails(
        self, monkeypatch, windows_paths
    ):
        """Sans cela, un rapport en erreur laisserait un serveur orphelin sur le port."""
        binary, db = windows_paths
        process = _FakeProcess()
        monkeypatch.setattr(aw_windows.subprocess, "Popen", lambda *a, **k: process)
        monkeypatch.setattr(aw_windows, "wait_for_server", lambda port: None)

        with pytest.raises(RuntimeError, match="rapport cassé"):
            with aw_windows.windows_server(5702, db_path=db, binary=binary):
                raise RuntimeError("rapport cassé")
        assert process.terminated

    def test_unresponsive_instance_is_killed(self, monkeypatch, windows_paths):
        binary, db = windows_paths
        process = _FakeProcess(hangs=True)
        monkeypatch.setattr(aw_windows.subprocess, "Popen", lambda *a, **k: process)
        monkeypatch.setattr(aw_windows, "wait_for_server", lambda port: None)

        with aw_windows.windows_server(5702, db_path=db, binary=binary):
            pass
        assert process.killed

    def test_reports_a_missing_database(self, monkeypatch, windows_paths):
        binary, _ = windows_paths
        monkeypatch.setattr(
            aw_windows.subprocess,
            "Popen",
            lambda *a, **k: pytest.fail("aucun serveur ne doit être lancé"),
        )
        with pytest.raises(aw_windows.WindowsDataError, match="introuvable"):
            with aw_windows.windows_server(
                5702, db_path=aw_windows.MOUNT_POINT / "absente.db", binary=binary
            ):
                pass

    def test_startup_failure_is_reported(self, monkeypatch, windows_paths):
        binary, db = windows_paths
        process = _FakeProcess()
        monkeypatch.setattr(aw_windows.subprocess, "Popen", lambda *a, **k: process)

        def never_ready(port):
            raise aw_windows.ServerUnreachableError("timeout")

        monkeypatch.setattr(aw_windows, "wait_for_server", never_ready)
        with pytest.raises(aw_windows.WindowsDataError, match="n'a pas démarré"):
            with aw_windows.windows_server(5702, db_path=db, binary=binary):
                pass
        assert process.terminated


class TestDualReport:
    """Le flux complet, transposé de aw-win-report-of-linux.ps1."""

    @pytest.fixture
    def run_main(self, aw_report, monkeypatch):
        def run(*argv: str) -> list:
            events = []
            monkeypatch.setattr(aw_report.sys, "argv", ["aw-report.py", *argv])

            def fake_report(port, args, fix_gaps_enabled):
                events.append(("report", port, fix_gaps_enabled))
                return _totals(aw_report, active_sec=3600.0)

            monkeypatch.setattr(aw_report, "run_report", fake_report)
            monkeypatch.setattr(
                aw_report, "ensure_mounted", lambda: events.append(("mount",))
            )

            class _Server:
                def __init__(self, port):
                    self.port = port

                def __enter__(self):
                    events.append(("server-up", self.port))
                    return self.port

                def __exit__(self, *exc):
                    events.append(("server-down", self.port))
                    return False

            monkeypatch.setattr(aw_report, "windows_server", _Server)
            aw_report.main()
            return events

        return run

    def test_reports_linux_then_windows(self, run_main, capsys):
        assert run_main("--windows") == [
            ("report", 5600, False),
            ("mount",),
            ("server-up", 5702),
            ("report", 5702, False),
            ("server-down", 5702),
        ]

    def test_linux_report_runs_before_the_windows_instance_starts(
        self, run_main, capsys
    ):
        """--fix-gaps fait un killall aw-server-rust : il emporterait l'instance Windows."""
        events = run_main("--windows", "--fix-gaps")
        assert events.index(("report", 5600, True)) < events.index(("server-up", 5702))

    def test_windows_report_never_triggers_a_fix(self, run_main, capsys):
        """La base Windows est lue telle quelle, jamais réécrite par la correction."""
        events = run_main("--windows", "--fix-gaps")
        assert ("report", 5702, False) in events

    def test_windows_port_is_configurable(self, run_main, capsys):
        events = run_main("--windows", "--windows-port", "5800")
        assert ("server-up", 5800) in events

    def test_nothing_windows_without_the_flag(self, run_main, capsys):
        assert run_main("-p") == [("report", 5600, False)]


def _totals(aw_report, **overrides):
    """Agrégats neutres, surchargés au besoin."""
    fields = {
        "active_sec": 0.0,
        "not_afk_sec": 0.0,
        "afk_sec": 0.0,
        "category_sec": 0.0,
        "it_sec": 0.0,
    }
    return aw_report.ReportTotals(**{**fields, **overrides})


class TestCombinedTotals:
    """Le cumul doit donner le temps réellement travaillé sur les deux systèmes."""

    def test_components_add_up(self, aw_report):
        linux = _totals(
            aw_report,
            active_sec=3600.0,
            not_afk_sec=3700.0,
            afk_sec=600.0,
            category_sec=300.0,
            it_sec=900.0,
        )
        windows = _totals(
            aw_report,
            active_sec=1800.0,
            not_afk_sec=1850.0,
            afk_sec=120.0,
            category_sec=60.0,
            it_sec=0.0,
        )
        combined = linux + windows
        assert combined.active_sec == 5400.0
        assert combined.not_afk_sec == 5550.0
        assert combined.afk_sec == 720.0
        assert combined.category_sec == 360.0
        assert combined.it_sec == 900.0

    def test_general_work_of_the_sum_equals_the_sum_of_general_works(self, aw_report):
        """Le cumul ne doit pas introduire d'écart avec les lignes affichées au-dessus."""
        linux = _totals(aw_report, active_sec=3600.0, category_sec=300.0, it_sec=900.0)
        windows = _totals(aw_report, active_sec=1800.0, category_sec=60.0)
        assert (linux + windows).general_work_sec == (
            linux.general_work_sec + windows.general_work_sec
        )

    def test_total_block_is_printed_after_both_reports(
        self, aw_report, monkeypatch, capsys
    ):
        monkeypatch.setattr(aw_report.sys, "argv", ["aw-report.py", "--windows"])
        monkeypatch.setattr(
            aw_report,
            "run_report",
            lambda port, args, fix_gaps_enabled: _totals(
                aw_report, active_sec=3600.0, it_sec=600.0
            ),
        )
        monkeypatch.setattr(aw_report, "ensure_mounted", lambda: None)

        class _Server:
            def __init__(self, port):
                self.port = port

            def __enter__(self):
                return self.port

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(aw_report, "windows_server", _Server)
        aw_report.main()

        out = capsys.readouterr().out
        assert out.index("=== Usage Windows") < out.index(
            "=== Total Linux + Windows ==="
        )
        total_block = out[out.index("=== Total Linux + Windows ===") :]
        # 2 x (3600 actif - 600 IT) = 6000 s = 100 min, et 2 x 600 s d'IT = 20 min.
        assert "General Work : 100.0 min" in total_block
        assert "IT : 20.0 min" in total_block

    def test_no_stopwatch_detail_in_the_total(self, aw_report, capsys):
        """Les sessions stopwatch sont listées par système, pas dans le cumul."""
        aw_report.print_totals(_totals(aw_report), stopwatch_entries=None)
        assert "Stopwatch" not in capsys.readouterr().out
