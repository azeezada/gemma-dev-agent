from __future__ import annotations

import sys
from pathlib import Path


def add_project_root() -> Path:
    root = Path(__file__).resolve().parents[3]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    return root
