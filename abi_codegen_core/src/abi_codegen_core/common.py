from __future__ import annotations

import difflib
import json
from pathlib import Path
from typing import Any


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"JSON root in '{path}' must be an object")
    return payload


def write_if_changed(path: Path, content: str, check: bool, dry_run: bool) -> int:
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if existing == content:
        return 0
    if check:
        diff = difflib.unified_diff(
            existing.splitlines(),
            content.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
        print("\n".join(diff))
        return 1
    if not dry_run:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return 0
