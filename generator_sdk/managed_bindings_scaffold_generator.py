#!/usr/bin/env python3
"""
Managed bindings scaffold generator.

Generates the ``managed.json`` (SafeHandle definitions) from the IDL's
``bindings.interop.opaque_types`` section. This file is consumed by the
Roslyn source generator to produce typed SafeHandle wrapper classes.

Run once when adding a new library target, then refine the generated file.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TOOL_PATH = "tools/abi_framework/generator_sdk/managed_bindings_scaffold_generator.py"
CORE_SRC = Path(__file__).resolve().parents[2] / "abi_codegen_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from abi_codegen_core.common import load_json_object, write_if_changed


def snake_to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\s]+", name) if w)


def strip_prefix(name: str, prefix: str) -> str:
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def infer_class_name(c_type_name: str, symbol_prefix: str) -> str:
    name = strip_prefix(c_type_name, symbol_prefix)
    for suf in ("_t", "_s"):
        if name.endswith(suf):
            name = name[:-len(suf)]
            break
    return snake_to_pascal(name)


def get_symbol_prefix(idl: dict[str, Any], override: str | None) -> str:
    if override is not None:
        return override
    codegen = idl.get("codegen") or {}
    sp = codegen.get("symbol_prefix")
    if isinstance(sp, str) and sp:
        return sp
    target = str(idl.get("target") or "")
    functions = idl.get("functions") or []
    guessed = target.rstrip("_") + "_"
    if functions:
        first = str(functions[0].get("name") or "")
        if first.startswith(guessed):
            return guessed
    return ""


def scaffold_managed_bindings(
    idl: dict[str, Any],
    namespace: str,
    symbol_prefix_override: str | None,
) -> dict[str, Any]:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    opaque_types: dict[str, Any] = (
        idl.get("bindings", {}).get("interop", {}).get("opaque_types", {}) or {}
    )

    handles: list[dict[str, Any]] = []
    for c_type_name in sorted(opaque_types):
        cfg = opaque_types[c_type_name]
        if not isinstance(cfg, dict):
            cfg = {}
        cs_type = infer_class_name(c_type_name, sp)
        entry: dict[str, Any] = {
            "access": "public",
            "c_handle_type": f"{c_type_name}*",
            "cs_type": cs_type,
            "namespace": namespace,
            "release": cfg.get("release") or f"// TODO: {c_type_name}_release",
        }
        retain = cfg.get("retain")
        if retain:
            entry["retain"] = retain
        handles.append(entry)

    return {"handles": handles}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold managed.json (SafeHandle definitions) from IDL opaque_types."
    )
    parser.add_argument("--idl", required=True)
    parser.add_argument("--namespace", required=True, help="C# namespace for the handles.")
    parser.add_argument("--out", required=True)
    parser.add_argument("--symbol-prefix", default=None)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force and not args.check and not args.dry_run:
        print(f"scaffold-managed-bindings: '{out_path}' exists. Use --force to overwrite.")
        return 0

    idl = load_json_object(Path(args.idl))
    result = scaffold_managed_bindings(idl, args.namespace, args.symbol_prefix)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    return write_if_changed(out_path, content, args.check, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
