"""Rend les modules du projet importables depuis les tests.

aw-report.py est chargé par chemin (son nom contient un tiret) mais importe
aw_fix_gaps et aw_log : la racine du projet doit être sur sys.path.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def aw_report():
    """Charge aw-report.py (son nom contient un tiret : import par chemin).

    Une instance neuve par test pour que les monkeypatch sur le module restent isolés.
    """
    spec = importlib.util.spec_from_file_location(
        "aw_report_main", Path(__file__).resolve().parent.parent / "aw-report.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
