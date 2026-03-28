#!/usr/bin/env python3
"""
TypeScript/Node.js ffi-napi binding generator.

Generates a complete TypeScript module using ffi-napi and ref-napi including:
  - Enum declarations
  - Opaque handle type declarations and ref types
  - Callback type aliases
  - Struct notes (complex structs noted as needing manual implementation)
  - loadLibrary() function with all function signatures
  - OOP wrapper classes for each opaque handle type with dispose support

Works for any C library — all information comes from the IDL JSON.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Name helpers
# ---------------------------------------------------------------------------

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


def to_pascal_from_upper(name: str) -> str:
    """Convert UPPER_SNAKE to PascalCase."""
    return "".join(w.capitalize() for w in name.lower().split("_") if w)


# ---------------------------------------------------------------------------
# C → TypeScript/ffi-napi type map
# ---------------------------------------------------------------------------

_C_TO_FFI: dict[str, str] = {
    "void": "'void'",
    "bool": "'bool'",
    "_Bool": "'bool'",
    "char": "'int8'",
    "signed char": "'int8'",
    "int8_t": "'int8'",
    "unsigned char": "'uint8'",
    "uint8_t": "'uint8'",
    "short": "'int16'",
    "int16_t": "'int16'",
    "unsigned short": "'uint16'",
    "uint16_t": "'uint16'",
    "int": "'int32'",
    "int32_t": "'int32'",
    "unsigned int": "'uint32'",
    "uint32_t": "'uint32'",
    "long long": "'int64'",
    "int64_t": "'int64'",
    "unsigned long long": "'uint64'",
    "uint64_t": "'uint64'",
    "float": "'float'",
    "double": "'double'",
    "size_t": "'size_t'",
    "ssize_t": "'size_t'",
    "long": "'int64'",
    "unsigned long": "'uint64'",
}

# TypeScript type annotations for TS-side declarations
_C_TO_TS_TYPE: dict[str, str] = {
    "void": "void",
    "bool": "boolean",
    "_Bool": "boolean",
    "char": "number",
    "signed char": "number",
    "int8_t": "number",
    "unsigned char": "number",
    "uint8_t": "number",
    "short": "number",
    "int16_t": "number",
    "unsigned short": "number",
    "uint16_t": "number",
    "int": "number",
    "int32_t": "number",
    "unsigned int": "number",
    "uint32_t": "number",
    "long long": "number",
    "int64_t": "number",
    "unsigned long long": "number",
    "uint64_t": "number",
    "float": "number",
    "double": "number",
    "size_t": "number",
    "ssize_t": "number",
    "long": "number",
    "unsigned long": "number",
}


def c_type_to_ffi(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert C type to ffi-napi type string."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    # void* → 'pointer'
    if bare_no_ptr == "void" and is_ptr:
        return "'pointer'"

    # const char* → 'string'
    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "'string'"

    # uint8_t* (non-const) → 'pointer'
    if bare_no_ptr == "uint8_t" and is_ptr and "const" not in t:
        return "'pointer'"

    # Opaque handle pointer → XxxHandleType
    if is_ptr and bare_no_ptr in opaque_types:
        class_name = infer_class_name(bare_no_ptr, symbol_prefix)
        return f"{class_name}HandleType"

    # Non-pointer primitive
    if bare_no_ptr in _C_TO_FFI:
        if is_ptr:
            return "'pointer'"
        return _C_TO_FFI[bare_no_ptr]

    # Unknown pointer
    if is_ptr:
        return "'pointer'"

    # Unknown non-pointer (enum or struct passed by value)
    return "'int32'"


def c_type_to_ts(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert C type to TypeScript type annotation."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return "void"
    if bare_no_ptr == "void" and is_ptr:
        return "ref.Pointer<unknown>"
    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "string"
    if is_ptr and bare_no_ptr in opaque_types:
        class_name = infer_class_name(bare_no_ptr, symbol_prefix)
        return f"{class_name}Handle"
    if is_ptr:
        return "ref.Pointer<unknown>"
    if bare_no_ptr == "bool" or bare_no_ptr == "_Bool":
        return "boolean"
    if bare_no_ptr in _C_TO_TS_TYPE:
        return _C_TO_TS_TYPE[bare_no_ptr]
    return "unknown"


# ---------------------------------------------------------------------------
# IDL accessors
# ---------------------------------------------------------------------------

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


def get_opaque_types(idl: dict[str, Any]) -> dict[str, Any]:
    return idl.get("bindings", {}).get("interop", {}).get("opaque_types", {}) or {}


def get_enums(idl: dict[str, Any]) -> dict[str, Any]:
    return idl.get("header_types", {}).get("enums", {}) or {}


def get_structs(idl: dict[str, Any]) -> dict[str, Any]:
    return idl.get("header_types", {}).get("structs", {}) or {}


def get_callback_typedefs(idl: dict[str, Any]) -> list[dict[str, Any]]:
    raw = idl.get("header_types", {}).get("callback_typedefs") or []
    if isinstance(raw, list):
        return raw
    return []


# ---------------------------------------------------------------------------
# Enum member prefix stripping
# ---------------------------------------------------------------------------

def _common_enum_prefix(members: list[dict[str, Any]]) -> str:
    names = [str(mem.get("name", "")) for mem in members if mem.get("name")]
    if not names:
        return ""
    prefix = names[0]
    for name in names[1:]:
        while not name.startswith(prefix):
            prefix = prefix[:-1]
        if not prefix:
            break
    last_us = prefix.rfind("_")
    if last_us >= 0:
        prefix = prefix[:last_us + 1]
    return prefix


# ---------------------------------------------------------------------------
# Callback typedef parser
# ---------------------------------------------------------------------------

def _split_c_params(params_str: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in params_str:
        if ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
        else:
            current.append(ch)
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth = max(0, depth - 1)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def parse_callback_params(declaration: str) -> list[tuple[str, str]]:
    m = re.search(r"\)\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)\s*;?\s*$", declaration)
    if not m:
        return []
    params_str = m.group(1).strip()
    if not params_str or params_str == "void":
        return []
    raw = _split_c_params(params_str)
    result: list[tuple[str, str]] = []
    for param in raw:
        param = param.strip()
        if not param or param == "...":
            continue
        tokens = param.rsplit(None, 1)
        if len(tokens) == 2:
            c_type = tokens[0].strip()
            name = tokens[1].strip().lstrip("*")
        else:
            c_type = param
            name = "arg"
        result.append((c_type, name))
    return result


# ---------------------------------------------------------------------------
# Group functions by handle
# ---------------------------------------------------------------------------

def group_functions_by_handle(
    functions: list[dict[str, Any]],
    opaque_types: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {k: [] for k in opaque_types}
    groups["__global__"] = []
    for func in functions:
        params = func.get("parameters") or []
        matched: str | None = None
        for param in params:
            c_type = str(param.get("c_type") or "")
            bare = re.sub(r"[\s*]|const", "", c_type).strip()
            if bare in opaque_types:
                matched = bare
                break
        (groups[matched] if matched else groups["__global__"]).append(func)
    return groups


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------

def generate_typescript_bindings(
    idl: dict[str, Any],
    symbol_prefix_override: str | None = None,
    lib_name: str | None = None,
) -> str:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    target = str(idl.get("target") or "lumenrtc")
    effective_lib_name = lib_name or f"lib{target}"

    opaque_types = get_opaque_types(idl)
    enums = get_enums(idl)
    structs = get_structs(idl)
    callback_typedefs = get_callback_typedefs(idl)
    functions: list[dict[str, Any]] = idl.get("functions") or []

    lines: list[str] = []

    # Header
    lines += [
        "// <auto-generated />",
        "// Generated by tools/abi_framework/generator_sdk/typescript_bindings_generator.py",
        "// DO NOT EDIT — regenerate with: abi_framework codegen",
        "import * as ffi from 'ffi-napi';",
        "import * as ref from 'ref-napi';",
        "",
    ]

    # Enums
    lines += [
        "// \u2500\u2500 Enums \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]
    for enum_key, enum_def in enums.items():
        members: list[dict[str, Any]] = enum_def.get("members") or []
        class_name = infer_class_name(enum_key, sp)
        common_prefix = _common_enum_prefix(members)

        lines.append(f"export enum {class_name} {{")
        if members:
            for mem in members:
                raw_name = str(mem.get("name", ""))
                value = mem.get("value", 0)
                stripped = raw_name[len(common_prefix):] if raw_name.startswith(common_prefix) else raw_name
                ts_name = to_pascal_from_upper(stripped) if stripped else f"V{value}"
                if not ts_name or ts_name[0].isdigit():
                    ts_name = "V" + ts_name
                lines.append(f"  {ts_name} = {value},")
        lines.append("}")
        lines.append("")

    # Opaque handles
    lines += [
        "// \u2500\u2500 Opaque handles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]
    for ot_name in sorted(opaque_types.keys()):
        class_name = infer_class_name(ot_name, sp)
        lines.append(f"export type {class_name}Handle = ref.Pointer<unknown>;")
        lines.append(f"export const {class_name}HandleType = ref.refType(ref.types.void);")
        lines.append("")

    # Callback types
    lines += [
        "// \u2500\u2500 Callback types \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]
    for cb in callback_typedefs:
        cb_name = str(cb.get("name") or "")
        decl = str(cb.get("declaration") or "")
        if not cb_name:
            continue

        # Extract return type
        ret_match = re.match(r"typedef\s+(\w[\w\s\*]*?)\s*\(", decl)
        ret_c = ret_match.group(1).strip() if ret_match else "void"
        ret_ts = c_type_to_ts(ret_c, opaque_types, sp)

        raw_params = parse_callback_params(decl)
        ts_params: list[str] = []
        for c_type, p_name in raw_params:
            ts_type = c_type_to_ts(c_type, opaque_types, sp)
            ts_params.append(f"{p_name}: {ts_type}")

        type_name = snake_to_pascal(strip_prefix(cb_name, sp))
        param_str = ", ".join(ts_params)
        lines.append(f"export type {type_name} = ({param_str}) => {ret_ts};")
    lines.append("")

    # Structs (note)
    lines += [
        "// \u2500\u2500 Structs \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
        "// Note: complex structs with function pointer fields are represented as opaque.",
        "// Manual implementation may be needed for callback structs.",
        "",
    ]
    for struct_name in structs.keys():
        ts_name = infer_class_name(struct_name, sp)
        lines.append(f"// export interface {ts_name} {{ ... }}  // manual implementation needed")
    if structs:
        lines.append("")

    # Library declaration
    lines += [
        "// \u2500\u2500 Library declaration \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
        "export function loadLibrary(libraryPath: string) {",
        "  return ffi.Library(libraryPath, {",
    ]

    for func in functions:
        f_name = str(func.get("name") or "")
        if not f_name:
            continue
        ret_c = str(func.get("c_return_type") or "void")
        params: list[dict[str, Any]] = func.get("parameters") or []

        ret_ffi = c_type_to_ffi(ret_c, opaque_types, sp)
        param_ffis = [c_type_to_ffi(str(p.get("c_type") or "void"), opaque_types, sp) for p in params]
        param_str = ", ".join(param_ffis)
        lines.append(f"    '{f_name}': [{ret_ffi}, [{param_str}]],")

    lines += [
        "  });",
        "}",
        "",
    ]

    # OOP wrappers
    lines += [
        "// \u2500\u2500 OOP wrappers \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]

    func_groups = group_functions_by_handle(functions, opaque_types)

    for ot_name in sorted(opaque_types.keys()):
        class_name = infer_class_name(ot_name, sp)
        ot_info = opaque_types[ot_name]
        release_fn = str(ot_info.get("release") or "")
        retain_fn = str(ot_info.get("retain") or "")

        bare_type = strip_prefix(ot_name, sp)
        bare_type = strip_suffix(strip_suffix(bare_type, "_t"), "_s")
        method_prefix = sp + bare_type + "_"

        handle_funcs = func_groups.get(ot_name) or []

        # Find constructor function — may be in global group (no handle param) or handle group
        create_fn = f"{method_prefix}create"
        global_funcs_all = func_groups.get("__global__") or []
        create_func = next(
            (f for f in list(handle_funcs) + list(global_funcs_all)
             if str(f.get("name") or "") == create_fn),
            None,
        )

        lines += [
            f"export class {class_name} {{",
            f"  private readonly handle: {class_name}Handle;",
            "  private readonly lib: ReturnType<typeof loadLibrary>;",
            "",
        ]

        # Constructor
        if create_func:
            create_params = create_func.get("parameters") or []
            ts_ctor_params: list[str] = []
            ctor_call_args: list[str] = []
            for p in create_params:
                p_name = str(p.get("name") or "arg")
                p_c_type = str(p.get("c_type") or "void")
                p_ts_type = c_type_to_ts(p_c_type, opaque_types, sp)
                ts_ctor_params.append(f"{p_name}: {p_ts_type}")
                ctor_call_args.append(p_name)
            ctor_param_str = ", ".join(["lib: ReturnType<typeof loadLibrary>"] + ts_ctor_params)
            ctor_args_str = ", ".join(ctor_call_args)
            lines += [
                f"  constructor({ctor_param_str}) {{",
                "    this.lib = lib;",
                f"    this.handle = this.lib.{create_fn}({ctor_args_str});",
                "  }",
                "",
            ]
        else:
            lines += [
                "  constructor(lib: ReturnType<typeof loadLibrary>) {",
                "    this.lib = lib;",
                "    // Note: no create function found in IDL — provide handle externally",
                f"    this.handle = null as unknown as {class_name}Handle;",
                "  }",
                "",
            ]

        # dispose / [Symbol.dispose]
        if release_fn:
            lines += [
                "  dispose(): void {",
                f"    this.lib.{release_fn}(this.handle);",
                "  }",
                "",
                "  [Symbol.dispose](): void {",
                "    this.dispose();",
                "  }",
                "",
            ]
        else:
            lines += [
                "  dispose(): void {",
                "    // no release function in IDL",
                "  }",
                "",
                "  [Symbol.dispose](): void {",
                "    this.dispose();",
                "  }",
                "",
            ]

        # Methods
        for func in handle_funcs:
            f_name = str(func.get("name") or "")
            if not f_name:
                continue
            if f_name == create_fn or f_name == release_fn or f_name == retain_fn:
                continue

            params: list[dict[str, Any]] = func.get("parameters") or []
            ret_c = str(func.get("c_return_type") or "void")
            ret_ts = c_type_to_ts(ret_c, opaque_types, sp)

            # Method name: strip handle prefix
            method_name = strip_prefix(f_name, method_prefix)
            if not method_name:
                method_name = strip_prefix(f_name, sp)
            # Convert to camelCase (first word lowercase, rest pascal)
            parts = method_name.split("_")
            if parts:
                method_name_camel = parts[0] + "".join(w.capitalize() for w in parts[1:] if w)
            else:
                method_name_camel = method_name

            # Parameters (skip first = the handle itself)
            extra_params = params[1:] if params else []
            ts_params: list[str] = []
            call_args: list[str] = ["this.handle"]
            for p in extra_params:
                p_name = str(p.get("name") or "arg")
                p_c_type = str(p.get("c_type") or "void")
                p_ts_type = c_type_to_ts(p_c_type, opaque_types, sp)
                ts_params.append(f"{p_name}: {p_ts_type}")
                call_args.append(p_name)

            param_str = ", ".join(ts_params)
            call_str = f"this.lib.{f_name}({', '.join(call_args)})"

            if ret_ts == "void":
                lines.append(f"  {method_name_camel}({param_str}): {ret_ts} {{")
                lines.append(f"    {call_str};")
            else:
                lines.append(f"  {method_name_camel}({param_str}): {ret_ts} {{")
                lines.append(f"    return {call_str};")
            lines.append("  }")
            lines.append("")

        lines.append("}")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate TypeScript ffi-napi bindings from IDL JSON.")
    parser.add_argument("--idl", required=True, help="IDL JSON path.")
    parser.add_argument("--out", required=True, help="Output .ts file path.")
    parser.add_argument("--symbol-prefix", default=None, help="Symbol prefix override.")
    parser.add_argument("--lib-name", default=None, help="Library name (e.g. liblumenrtc).")
    parser.add_argument("--check", action="store_true", help="Fail if output would change.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files.")
    args = parser.parse_args()

    idl_path = Path(args.idl)
    out_path = Path(args.out)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = generate_typescript_bindings(
        idl,
        getattr(args, "symbol_prefix", None),
        getattr(args, "lib_name", None),
    )

    if args.check:
        if out_path.exists():
            existing = out_path.read_text(encoding="utf-8")
            if existing != content:
                print(f"DRIFT: {out_path} is out of date.", file=sys.stderr)
                return 1
            return 0
        else:
            print(f"MISSING: {out_path} does not exist.", file=sys.stderr)
            return 1

    if args.dry_run:
        print(content)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")
    print(f"Generated: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
