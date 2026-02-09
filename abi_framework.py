#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

_SRC_ROOT = Path(__file__).resolve().parent / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from abi_framework_core.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
