"""Lecture des données ActivityWatch de l'installation Windows depuis Linux.

Transposition de ~/bin/aw-win-report-of-linux.ps1, qui fait la manœuvre inverse : depuis
Windows, il sert la base Linux (montée en G:) sur un second port et édite les deux
rapports. Ici on monte la partition Windows et on sert sa base sur un second port local.

    mount /mnt/windows  ->  aw-server-rust --port 5702 --dbpath <base Windows>  ->  rapport
                            (instance séparée, arrêtée en sortie)

L'instance lit la base Windows telle quelle : les catégories et réglages affichés sont
donc ceux configurés sous Windows, pas ceux de la session Linux.
"""

import os
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from aw_log import log_debug, log_error
from aw_server import AW_SERVER_BINARY, ServerUnreachableError, wait_for_server

MOUNT_POINT = Path("/mnt/windows")
WINDOWS_DB_PATH = (
    MOUNT_POINT / "Users/hugoc/AppData/Local/activitywatch/aw-server-rust/sqlite.db"
)

# Port que le .ps1 réserve aux données « de l'autre OS ».
DEFAULT_WINDOWS_PORT = 5702

STOP_TIMEOUT_S = 5.0


class WindowsDataError(RuntimeError):
    """Les données ActivityWatch de Windows ne sont pas exploitables."""


def ensure_mounted(mount_point: Path = MOUNT_POINT) -> None:
    """Monte la partition Windows si besoin.

    L'entrée fstab porte l'option `users` : aucun sudo n'est nécessaire. Le montage est
    laissé en place en sortie, comme le .ps1 laisse G: monté.
    """
    if os.path.ismount(mount_point):
        log_debug("ensure_mounted", f"{mount_point} déjà monté")
        return

    log_debug("ensure_mounted", f"montage de {mount_point}")
    result = subprocess.run(
        ["mount", str(mount_point)], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise WindowsDataError(
            f"Montage de {mount_point} impossible (code {result.returncode}): "
            f"{result.stderr.strip() or 'aucun détail'}. "
            "Un Windows en veille prolongée laisse le volume NTFS sale et bloque le montage."
        )
    if not os.path.ismount(mount_point):
        raise WindowsDataError(
            f"{mount_point} n'est toujours pas un point de montage après `mount`"
        )


@contextmanager
def windows_server(
    port: int = DEFAULT_WINDOWS_PORT,
    db_path: Path = WINDOWS_DB_PATH,
    binary: Path = AW_SERVER_BINARY,
) -> Iterator[int]:
    """Sert la base Windows sur `port` le temps du bloc, puis arrête l'instance.

    L'instance partage le nom de processus du serveur local : la lancer après toute
    opération qui fait un `killall aw-server-rust` (cf. aw_fix_gaps) évite de la perdre.
    """
    if not binary.is_file():
        raise WindowsDataError(f"aw-server-rust introuvable: {binary}")
    if not db_path.is_file():
        raise WindowsDataError(
            f"Base ActivityWatch Windows introuvable: {db_path}. "
            "Le profil utilisateur Windows attendu est-il bien celui monté ?"
        )

    command = [
        str(binary),
        "--port",
        str(port),
        "--dbpath",
        str(db_path),
        # Sans cela, une base absente déclencherait un import depuis aw-server-python.
        "--no-legacy-import",
    ]
    log_debug("windows_server", f"démarrage: {' '.join(command)}")
    try:
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
    except OSError as e:
        raise WindowsDataError(f"Impossible de démarrer {binary}: {e}") from e

    try:
        try:
            wait_for_server(port)
        except ServerUnreachableError as e:
            raise WindowsDataError(
                f"Le serveur sur la base Windows n'a pas démarré: {e}"
            ) from e
        yield port
    finally:
        log_debug("windows_server", f"arrêt de l'instance du port {port}")
        process.terminate()
        try:
            process.wait(timeout=STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            log_error(
                "windows_server",
                f"instance du port {port} insensible à SIGTERM, envoi de SIGKILL",
            )
            process.kill()
            process.wait()
