#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: external_generator_stub.py <idl.json> <output.txt>", file=sys.stderr)
        return 2
    idl_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    if not idl_path.exists():
        print(f"IDL not found: {idl_path}", file=sys.stderr)
        return 2

    payload = json.loads(idl_path.read_text(encoding="utf-8"))
    target = payload.get("target") or "unknown"
    functions = payload.get("functions")
    function_count = len(functions) if isinstance(functions, list) else 0

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        f"target={target}\nfunction_count={function_count}\nsource={idl_path}\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
