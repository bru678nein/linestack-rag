"""Test configuration.

`ingest.py` lives at the repository root rather than inside the package
(ADR-0010), so the root has to be importable before the ingestion tests can
reach it. This is the whole cost of that decision, and it disappears when the
module moves.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
