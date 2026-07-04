"""Root pytest config: makes backend/src importable without an editable install.

The locked CI workflow (.github/workflows/ci.yml) runs pytest from the repo
root and never pip-installs the backend package, and the locked pytest.ini
pins testpaths = tests. This shim is therefore the one sanctioned way tests
import `meic` — matching doc 05 §10's backend/src layout without touching any
locked file.
"""
import sys
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parent / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))
