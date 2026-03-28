#!/usr/bin/env python3
"""
Managed API scaffold generator.

Given an IDL JSON, generates a starter managed_api.source.json that:
  - Enables auto_abi_surface (zero-config P/Invoke layer for ALL functions)
  - Stubs out callback classes for each callback struct in the IDL
  - Stubs out handle_api classes for each opaque handle type
  - Groups functions under their owning handle class as reference comments

Run once when adding a new library. Then fill in the TODO markers.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

TOOL_PATH = "tools/abi_framework/generator_sdk/managed_api_scaffold_generator.py"
CORE_SRC = Path(__file__).resolve().parents[2] / "abi_codegen_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from abi_codegen_core.common import load_json_object, write_if_changed


def snake_to_pascal(name: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\s]+", name) if w)


def strip_prefix(name: str, prefix: str) -> str:
    return name[len(prefix):] if prefix and name.startswith(prefix) else name


def strip_suffix(name: str, suffix: str) -> str:
    return name[:-len(suffix)] if suffix and name.endswith(suffix) else name


def infer_class_name(c_type_name: str, symbol_prefix: str) -> str:
    name = strip_prefix(c_type_name, symbol_prefix)
    for suf in ("_t", "_s"):
        name = strip_suffix(name, suf)
    return snake_to_pascal(name)


def infer_native_struct_name(c_type_name: str) -> str:
    name = c_type_name
    for suf in ("_t", "_s"):
        name = strip_suffix(name, suf)
    return snake_to_pascal(name)


def infer_symbol_prefix(idl: dict[str, Any]) -> str:
    target = str(idl.get("target") or "")
    functions = idl.get("functions") or []
    guessed = target.rstrip("_") + "_"
    if functions:
        first = str(functions[0].get("name") or "")
        if first.startswith(guessed):
            return guessed
    return ""


def is_callback_struct(name: str, callback_suffixes: list[str], symbol_prefix: str) -> bool:
    if any(name.endswith(suf) for suf in callback_suffixes):
        return True
    bare = strip_prefix(name, symbol_prefix).lower()
    return "callback" in bare or bare.rstrip("_t").endswith("_cb")


def is_function_pointer_field(field: dict[str, Any]) -> bool:
    c_type = str(field.get("c_type") or "")
    return "(*)" in c_type or bool(re.search(r"\(\s*\*", c_type))


def build_callback_entry(struct: dict[str, Any], symbol_prefix: str) -> dict[str, Any]:
    struct_name = str(struct.get("name") or "")
    class_name = infer_class_name(struct_name, symbol_prefix)
    native_struct = infer_native_struct_name(struct_name)

    fields_raw = struct.get("fields") or []
    fields_out: list[dict[str, Any]] = []
    for field in fields_raw:
        field_name = str(field.get("name") or "")
        if not field_name:
            continue
        if field_name in ("user_data", "ud", "context", "ctx", "userdata"):
            continue
        if not is_function_pointer_field(field):
            continue
        managed_name = snake_to_pascal(field_name)
        fields_out.append({
            "managed_name": managed_name,
            "managed_type": "Action</* TODO: add managed param types */>?",
            "delegate_field": f"_{managed_name[0].lower()}{managed_name[1:]}Cb",
            "delegate_type": f"{managed_name}Cb",
            "native_field": field_name,
            "assignment_lines": [
                f"(ud /*, TODO: params */) => {managed_name}?.Invoke(/* TODO: marshal params */)"
            ],
        })

    return {
        "class": class_name,
        "summary": f"Callbacks for {class_name}.",
        "native_struct": native_struct,
        "fields": fields_out,
    }


def group_functions_by_handle(
    functions: list[dict[str, Any]],
    opaque_types: dict[str, Any],
) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {k: [] for k in opaque_types}
    groups["__global__"] = []
    for func in functions:
        name = str(func.get("name") or "")
        if not name:
            continue
        params = func.get("parameters") or []
        matched: str | None = None
        for param in params:
            bare = re.sub(r"[\s*]|const", "", str(param.get("c_type") or "")).strip()
            if bare in opaque_types:
                matched = bare
                break
        (groups[matched] if matched else groups["__global__"]).append(name)
    return groups


def scaffold(idl: dict[str, Any], namespace: str, symbol_prefix: str | None) -> dict[str, Any]:
    target = str(idl.get("target") or "unknown")
    bindings = idl.get("bindings") or {}
    interop = bindings.get("interop") or {}
    opaque_types: dict[str, Any] = interop.get("opaque_types") or {}
    callback_suffixes: list[str] = interop.get("callback_struct_suffixes") or ["_callbacks_t"]

    sp = symbol_prefix if symbol_prefix is not None else infer_symbol_prefix(idl)
    structs: list[dict[str, Any]] = idl.get("structs") or []
    functions: list[dict[str, Any]] = idl.get("functions") or []

    cb_structs = [s for s in structs if is_callback_struct(str(s.get("name") or ""), callback_suffixes, sp)]
    func_groups = group_functions_by_handle(functions, opaque_types)

    callbacks = [build_callback_entry(s, sp) for s in cb_structs]

    handle_api: list[dict[str, Any]] = []
    for handle_type in sorted(opaque_types):
        class_name = infer_class_name(handle_type, sp)
        funcs = sorted(func_groups.get(handle_type) or [])
        members: list[dict[str, Any]] = [
            {"line": f"// TODO: add managed members for {class_name}"},
        ]
        if funcs:
            members.append({"line": f"// Native functions for this handle:"})
            members.extend({"line": f"//   {f}"} for f in funcs)
        handle_api.append({"class": class_name, "members": members})

    return {
        "schema_version": 2,
        "namespace": namespace,
        "output_hints": {
            "pattern": "ManagedApi.{section_pascal}",
            "suffix": ".g.cs",
        },
        "auto_abi_surface": {
            "enabled": True,
            "method_prefix": "Abi",
            "section_suffix": "_abi_surface",
            "global_section": "global",
            "global_class": "Global",
            "include_deprecated": False,
            "coverage": {
                "strict": True,
                "waived_functions": [],
            },
            "public_facade": {
                "enabled": True,
                "class_suffix": "_abi_facade",
                "method_prefix": "Raw",
                "typed_method_prefix": "Typed",
                "section_suffix": "_abi_facade",
                "allow_int_ptr": True,
                "safe_facade": {
                    "enabled": True,
                    "class_suffix": "_abi_safe",
                    "method_prefix": "",
                    "try_method_prefix": "Try",
                    "async_method_suffix": "Async",
                    "section_suffix": "_abi_safe",
                    "exception_type": "global::System.InvalidOperationException",
                },
            },
        },
        "callbacks": callbacks,
        "handle_api": handle_api,
        # Computed by managed_api_metadata_generator — leave empty here
        "required_native_functions": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold managed_api.source.json from IDL JSON.")
    parser.add_argument("--idl", required=True)
    parser.add_argument("--namespace", required=True, help="Managed C# namespace (e.g. MyLib).")
    parser.add_argument("--out", required=True)
    parser.add_argument("--symbol-prefix", default=None, help="Symbol prefix override (auto-inferred if omitted).")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output file.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    out_path = Path(args.out)
    if out_path.exists() and not args.force and not args.check and not args.dry_run:
        print(f"Scaffold: output already exists at '{out_path}'. Use --force to overwrite.")
        return 0

    idl = load_json_object(Path(args.idl))
    result = scaffold(idl, args.namespace, args.symbol_prefix)
    content = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    return write_if_changed(out_path, content, args.check, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
