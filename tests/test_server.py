"""Tests de l'attente de disponibilité d'un serveur aw-server-rust."""

import pytest

import aw_server


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """L'attente est testée via sa logique, pas via le temps réel."""
    monkeypatch.setattr(aw_server.time, "sleep", lambda _: None)


class TestWaitForServer:
    def test_polls_until_the_api_answers(self, monkeypatch):
        attempts = {"n": 0}

        def fake_urlopen(url, timeout):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise OSError("connection refused")
            return _NullResponse()

        monkeypatch.setattr(aw_server.urllib.request, "urlopen", fake_urlopen)
        aw_server.wait_for_server(5600)
        assert attempts["n"] == 3

    def test_uses_the_requested_port(self, monkeypatch):
        seen = {}

        def fake_urlopen(url, timeout):
            seen["url"] = url
            return _NullResponse()

        monkeypatch.setattr(aw_server.urllib.request, "urlopen", fake_urlopen)
        aw_server.wait_for_server(5702)
        assert seen["url"] == "http://localhost:5702/api/0/info"

    def test_gives_up_with_a_meaningful_error(self, monkeypatch):
        monkeypatch.setattr(
            aw_server.urllib.request,
            "urlopen",
            lambda url, timeout: (_ for _ in ()).throw(OSError("refused")),
        )
        with pytest.raises(aw_server.ServerUnreachableError, match="injoignable"):
            aw_server.wait_for_server(5600, timeout_s=0)


class _NullResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
