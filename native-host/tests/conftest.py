"""Shared pytest fixtures for native-host tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Make `host` importable as a top-level module.
_HOST_DIR = Path(__file__).resolve().parent.parent
if str(_HOST_DIR) not in sys.path:
    sys.path.insert(0, str(_HOST_DIR))
