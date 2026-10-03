"""Pilotage des instances aw-server-rust (local ou pointée sur une autre base)."""

import time
import urllib.error
import urllib.request
from pathlib import Path

from aw_log import log_debug

AW_SERVER_BINARY = Path("/opt/activitywatch/aw-server-rust/aw-server-rust")

READY_TIMEOUT_S = 60.0
POLL_INTERVAL_S = 0.5


class ServerUnreachableError(RuntimeError):
    """Le serveur n'a pas répondu dans le délai imparti."""


def wait_for_server(port: int, timeout_s: float = READY_TIMEOUT_S) -> None:
    """Attend que l'API réponde : interroger un serveur pas encore prêt échouerait."""
    url = f"http://localhost:{port}/api/0/info"
    log_debug("wait_for_server", f"attente de {url} (max {timeout_s:g}s)")
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                log_debug("wait_for_server", f"serveur disponible sur le port {port}")
                return
        except (urllib.error.URLError, OSError) as e:
            last_error = e
            time.sleep(POLL_INTERVAL_S)
    raise ServerUnreachableError(
        f"Serveur injoignable sur {url} après {timeout_s:g}s (dernière erreur: {last_error})"
    )
