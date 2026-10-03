"""Journalisation commune aux modules du rapport (stderr, horodatée)."""

import sys
from datetime import datetime


def _log(level: str, func: str, message: str) -> None:
    print(
        f"[{level}] {datetime.now().isoformat(timespec='seconds')} {func}: {message}",
        file=sys.stderr,
    )


def log_debug(func: str, message: str) -> None:
    _log("DEBUG", func, message)


def log_error(func: str, message: str) -> None:
    _log("ERROR", func, message)
