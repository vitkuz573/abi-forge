#!/usr/bin/env python3
"""
Generic native ABI export forwarding generator.
Works for any C library — all parameters auto-inferred from the IDL when not supplied.
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

TOOL_PATH = "tools/abi_framework/generator_sdk/native_exports_generator.py"
CORE_SRC = Path(__file__).resolve().parents[2] / "abi_codegen_core" / "src"
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

from abi_codegen_core.common import load_json_object, write_if_changed
from abi_codegen_core.native_exports import NativeExportRenderOptions, render_exports, render_impl_header


def _from_idl(idl: dict, key: str, fallback: str) -> str:
    codegen = idl.get("codegen")
    if isinstance(codegen, dict):
        v = codegen.get(key)
        if isinstance(v, str) and v:
            return v
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate native export forwarding stubs from IDL JSON.")
    parser.add_argument("--idl", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--impl-header", required=True)
    parser.add_argument("--header-include", default=None)
    parser.add_argument("--impl-header-include", default=None)
    parser.add_argument("--api-macro", default=None)
    parser.add_argument("--call-macro", default=None)
    parser.add_argument("--impl-prefix", default=None)
    parser.add_argument("--symbol-prefix", default=None)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    idl = load_json_object(Path(args.idl))
    functions = idl.get("functions")
    if not isinstance(functions, list):
        raise SystemExit("IDL missing 'functions' array")

    target = str(idl.get("target") or "unknown")
    api_macro = args.api_macro or _from_idl(idl, "native_api_macro", f"{target.upper()}_API")
    call_macro = args.call_macro or _from_idl(idl, "native_call_macro", f"{target.upper()}_CALL")

    symbol_prefix = args.symbol_prefix
    if not symbol_prefix:
        guessed = target.rstrip("_") + "_"
        first = str(functions[0].get("name") or "") if functions else ""
        symbol_prefix = guessed if first.startswith(guessed) else ""

    impl_prefix = args.impl_prefix or (symbol_prefix.rstrip("_") + "_impl_" if symbol_prefix else "impl_")
    header_include = args.header_include or f"{target}.h"
    impl_header_include = args.impl_header_include or Path(args.impl_header).name

    options = NativeExportRenderOptions(
        header_include=header_include,
        impl_header_include=impl_header_include,
        api_macro=api_macro,
        call_macro=call_macro,
        impl_prefix=impl_prefix,
        symbol_prefix=symbol_prefix,
    )
    status = 0
    status |= write_if_changed(Path(args.out), render_exports(functions, options, TOOL_PATH), args.check, args.dry_run)
    status |= write_if_changed(Path(args.impl_header), render_impl_header(functions, options), args.check, args.dry_run)
    return status

if __name__ == "__main__":
    raise SystemExit(main())
