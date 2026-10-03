"""Correction des trous laissés par awatcher entre deux événements de fenêtre.

fix-gaps.sh écrit directement dans la base SQLite d'aw-server : ActivityWatch doit être
arrêté avant l'écriture, puis relancé et de nouveau joignable avant que le rapport
n'interroge l'API HTTP. Ce module orchestre cette séquence.

Séquence complète :

    stop_activitywatch()  ->  run_fix_gaps()  ->  start_activitywatch()  ->  wait_for_server()
    aw-qt, aw-server-rust     fix-gaps.sh -s -e    aw-qt détaché            GET /api/0/info
    et aw-awatcher tués       sur sqlite.db                                 jusqu'à réponse

La fenêtre corrigée est celle que le rapport affiche, bornes comprises : corriger au-delà
serait du travail inutile, corriger en deçà laisserait des trous dans le résultat.
"""

import subprocess
import time
from datetime import datetime
from pathlib import Path

from aw_log import log_debug, log_error
from aw_server import ServerUnreachableError, wait_for_server

DEFAULT_FIX_GAPS_SCRIPT = Path(
    "/home/hugoc/Documents/git2/fix-awatcher-gaps/fix-gaps.sh"
)
DEFAULT_DB_PATH = Path.home() / ".local/share/activitywatch/aw-server-rust/sqlite.db"

# aw-qt est le superviseur : le tuer en premier évite qu'il ne relance aw-server-rust
# pendant que fix-gaps.sh écrit dans la base.
AW_PROCESSES = ("aw-qt", "aw-server-rust", "aw-awatcher")

STOP_TIMEOUT_S = 10.0
POLL_INTERVAL_S = 0.5


class FixGapsError(RuntimeError):
    """Échec d'une étape de la correction des gaps."""


def to_ns(moment: datetime) -> int:
    """Convertit une borne de période en nanosecondes epoch (paramètres -s/-e).

    Le calcul passe par les secondes entières : int(timestamp() * 1e9) perdrait de la
    précision, un float64 ne portant pas les 19 chiffres significatifs d'un horodatage
    nanoseconde. fix-gaps.sh refuse d'ailleurs toute valeur qui n'en fait pas exactement 19.
    """
    moment_ns = int(moment.timestamp()) * 1_000_000_000 + moment.microsecond * 1_000
    if len(str(moment_ns)) != 19:
        raise ValueError(
            f"Horodatage hors format attendu par fix-gaps.sh: {moment_ns} "
            f"({len(str(moment_ns))} chiffres, 19 attendus)"
        )
    return moment_ns


def _is_running(process: str) -> bool:
    """True si un processus portant exactement ce nom tourne."""
    return (
        subprocess.run(
            ["pgrep", "-x", process], capture_output=True, check=False
        ).returncode
        == 0
    )


def stop_activitywatch(timeout_s: float = STOP_TIMEOUT_S) -> None:
    """Arrête ActivityWatch et attend que la base ne soit plus ouverte par le serveur."""
    for process in AW_PROCESSES:
        if not _is_running(process):
            continue
        log_debug("stop_activitywatch", f"arrêt de {process}")
        result = subprocess.run(
            ["killall", process], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            log_error(
                "stop_activitywatch",
                f"killall {process} a échoué (code {result.returncode}): "
                f"{result.stderr.strip()}",
            )

    deadline = time.monotonic() + timeout_s
    while _is_running("aw-server-rust"):
        if time.monotonic() >= deadline:
            raise FixGapsError(
                f"aw-server-rust est toujours actif après {timeout_s:g}s, "
                "écrire dans la base risquerait de la corrompre"
            )
        time.sleep(POLL_INTERVAL_S)


def run_fix_gaps(
    start_ns: int,
    end_ns: int,
    db_path: Path = DEFAULT_DB_PATH,
    script_path: Path = DEFAULT_FIX_GAPS_SCRIPT,
) -> None:
    """Lance fix-gaps.sh sur la base, entre `start_ns` et `end_ns`."""
    if start_ns >= end_ns:
        raise FixGapsError(
            f"Période vide ou inversée: début {start_ns} >= fin {end_ns}"
        )
    if not script_path.is_file():
        raise FixGapsError(f"Script de correction introuvable: {script_path}")
    if not db_path.is_file():
        raise FixGapsError(f"Base ActivityWatch introuvable: {db_path}")

    # -b : les processus sont déjà arrêtés, on court-circuite la confirmation interactive.
    command = [
        "bash",
        str(script_path),
        "-f",
        str(db_path),
        "-s",
        str(start_ns),
        "-e",
        str(end_ns),
        "-b",
    ]
    log_debug("run_fix_gaps", f"exécution: {' '.join(command)}")
    try:
        result = subprocess.run(command, check=False)
    except OSError as e:
        raise FixGapsError(f"Impossible d'exécuter {script_path}: {e}") from e
    if result.returncode != 0:
        raise FixGapsError(
            f"{script_path.name} a échoué (code {result.returncode}), "
            "la base peut être partiellement corrigée"
        )


def start_activitywatch() -> None:
    """Relance aw-qt détaché du rapport, qui redémarre serveur et watchers."""
    if _is_running("aw-qt"):
        log_debug("start_activitywatch", "aw-qt déjà en cours d'exécution")
        return
    log_debug("start_activitywatch", "démarrage de aw-qt")
    try:
        subprocess.Popen(
            ["aw-qt"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as e:
        raise FixGapsError(f"Impossible de démarrer aw-qt: {e}") from e


def fix_gaps(
    period_start: datetime,
    period_end: datetime,
    port: int,
    db_path: Path = DEFAULT_DB_PATH,
    script_path: Path = DEFAULT_FIX_GAPS_SCRIPT,
) -> None:
    """Séquence complète : arrêt, correction, redémarrage, attente du serveur.

    `period_start` et `period_end` sont les bornes que le rapport va interroger.
    """
    start_ns = to_ns(period_start)
    end_ns = to_ns(period_end)
    log_debug(
        "fix_gaps",
        f"correction de {period_start.isoformat()} à {period_end.isoformat()}",
    )
    stop_activitywatch()
    run_fix_gaps(start_ns, end_ns, db_path=db_path, script_path=script_path)
    start_activitywatch()
    try:
        wait_for_server(port)
    except ServerUnreachableError as e:
        raise FixGapsError(
            f"ActivityWatch n'est pas reparti après la correction: {e}"
        ) from e
