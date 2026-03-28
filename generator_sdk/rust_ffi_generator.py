#!/usr/bin/env python3
"""
Rust FFI binding generator.

Generates a complete Rust FFI file from IDL JSON including:
  - Type aliases and portable integer types
  - Enums as #[repr(C)] pub enum
  - Opaque handle types as zero-size structs + pointer type aliases
  - Callback function types as Option<unsafe extern "C" fn(...)>
  - Struct definitions as #[repr(C)] pub struct
  - An extern "C" block with all function declarations

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
# C → Rust type map
# ---------------------------------------------------------------------------

_C_TO_RUST: dict[str, str] = {
    "void": "c_void",
    "bool": "c_bool",
    "_Bool": "c_bool",
    "char": "c_char",
    "signed char": "i8",
    "unsigned char": "u8",
    "int8_t": "i8",
    "uint8_t": "u8",
    "int16_t": "i16",
    "uint16_t": "u16",
    "short": "c_short",
    "unsigned short": "c_ushort",
    "int": "c_int",
    "int32_t": "i32",
    "unsigned int": "c_uint",
    "uint32_t": "u32",
    "long": "c_long",
    "unsigned long": "c_ulong",
    "long long": "c_longlong",
    "unsigned long long": "c_ulonglong",
    "int64_t": "i64",
    "uint64_t": "u64",
    "size_t": "size_t",
    "ssize_t": "ssize_t",
    "intptr_t": "intptr_t",
    "uintptr_t": "uintptr_t",
    "float": "c_float",
    "double": "c_double",
}


def c_type_to_rust(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert a C type string to its Rust FFI equivalent."""
    t = c_type.strip()
    is_const = "const" in t
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return "()"

    # void* → *mut c_void
    if bare_no_ptr == "void" and is_ptr:
        qualifier = "*const" if is_const else "*mut"
        return f"{qualifier} c_void"

    # const char* → *const c_char
    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "*const c_char"

    # Opaque handle pointer
    if is_ptr and bare_no_ptr in opaque_types:
        ptr_type = infer_class_name(bare_no_ptr, symbol_prefix) + "Ptr"
        return ptr_type

    # Primitive
    if bare_no_ptr in _C_TO_RUST:
        rust_type = _C_TO_RUST[bare_no_ptr]
        if is_ptr:
            qualifier = "*const" if is_const else "*mut"
            return f"{qualifier} {rust_type}"
        return rust_type

    # Struct pointer
    if is_ptr:
        struct_name = snake_to_pascal(strip_suffix(strip_suffix(bare_no_ptr, "_t"), "_s"))
        qualifier = "*const" if is_const else "*mut"
        return f"{qualifier} {struct_name}"

    # Unknown
    return "*mut c_void"


def c_return_type_to_rust(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert C return type to Rust return type."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return ""  # no return type in Rust

    return c_type_to_rust(c_type, opaque_types, symbol_prefix)


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
    """Compute the longest common prefix shared by all member names."""
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
    """Parse (c_type, param_name) from callback typedef. Includes user_data."""
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
# Code generation
# ---------------------------------------------------------------------------

def generate_rust_ffi(
    idl: dict[str, Any],
    symbol_prefix_override: str | None = None,
) -> str:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    target = str(idl.get("target") or "lumenrtc")

    opaque_types = get_opaque_types(idl)
    enums = get_enums(idl)
    structs = get_structs(idl)
    callback_typedefs = get_callback_typedefs(idl)
    functions: list[dict[str, Any]] = idl.get("functions") or []

    lines: list[str] = []

    # Header comment
    lines += [
        f"// {target}_ffi.rs — generated by abi_framework rust_ffi_generator",
        "// DO NOT EDIT — regenerate with: abi_framework generate-rust-ffi",
        "",
        "#![allow(non_camel_case_types, non_snake_case, dead_code, unused_imports)]",
        "",
        "use std::ffi::{c_char, c_double, c_float, c_int, c_longlong, c_uint, c_ulonglong, c_void};",
        "use std::os::raw::{c_long, c_ulong, c_short, c_ushort};",
        "",
        "// Portable integer types",
        "pub type c_bool = u8;",
        "pub type int8_t = i8; pub type uint8_t = u8;",
        "pub type int16_t = i16; pub type uint16_t = u16;",
        "pub type int32_t = i32; pub type uint32_t = u32;",
        "pub type int64_t = i64; pub type uint64_t = u64;",
        "pub type size_t = usize; pub type ssize_t = isize;",
        "pub type intptr_t = isize; pub type uintptr_t = usize;",
        "",
    ]

    # Enums
    lines += [
        "// ---------------------------------------------------------------------------",
        "// Enums",
        "// ---------------------------------------------------------------------------",
        "",
    ]
    for enum_key, enum_def in enums.items():
        members: list[dict[str, Any]] = enum_def.get("members") or []
        class_name = infer_class_name(enum_key, sp)
        common_prefix = _common_enum_prefix(members)

        lines.append("#[repr(C)]")
        lines.append("#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]")
        lines.append(f"pub enum {class_name} {{")
        for mem in members:
            raw_name = str(mem.get("name", ""))
            value = mem.get("value", 0)
            py_name = raw_name[len(common_prefix):] if raw_name.startswith(common_prefix) else raw_name
            # Convert UPPER_SNAKE to PascalCase for Rust enum variants
            rust_name = to_pascal_from_upper(py_name) if py_name else f"V{value}"
            if not rust_name or rust_name[0].isdigit():
                rust_name = f"V{rust_name}"
            lines.append(f"    {rust_name} = {value},")
        lines.append("}")
        lines.append("")

    # Opaque handle types
    lines += [
        "// ---------------------------------------------------------------------------",
        "// Opaque handle types",
        "// ---------------------------------------------------------------------------",
        "",
    ]
    for ot_name in sorted(opaque_types.keys()):
        # Generate a PascalCase struct name without the symbol prefix
        # e.g. lrtc_audio_device_t → LrtcAudioDevice (keep prefix in struct name for clarity)
        bare = strip_suffix(strip_suffix(ot_name, "_t"), "_s")
        struct_name = snake_to_pascal(bare)
        ptr_type = infer_class_name(ot_name, sp) + "Ptr"

        lines.append("#[repr(C)]")
        lines.append(f"pub struct {struct_name} {{ _opaque: [u8; 0] }}")
        lines.append(f"pub type {ptr_type} = *mut {struct_name};")
        lines.append("")

    # Callback function types
    lines += [
        "// ---------------------------------------------------------------------------",
        "// Callback function types",
        "// ---------------------------------------------------------------------------",
        "",
    ]
    for cb in callback_typedefs:
        cb_name = str(cb.get("name") or "")
        decl = str(cb.get("declaration") or "")
        if not cb_name:
            continue

        # Extract return type from declaration
        ret_match = re.match(r"typedef\s+(\w[\w\s\*]*?)\s*\(", decl)
        ret_c = ret_match.group(1).strip() if ret_match else "void"
        ret_rust = c_return_type_to_rust(ret_c, opaque_types, sp)

        raw_params = parse_callback_params(decl)
        rust_params: list[str] = []
        for c_type, p_name in raw_params:
            rust_type = c_type_to_rust(c_type, opaque_types, sp)
            rust_params.append(f"{p_name}: {rust_type}")

        # Build Rust type alias name: strip prefix, PascalCase
        type_name = snake_to_pascal(strip_prefix(cb_name, sp))
        param_str = ", ".join(rust_params)
        if ret_rust:
            lines.append(f"pub type {type_name} = Option<unsafe extern \"C\" fn({param_str}) -> {ret_rust}>;")
        else:
            lines.append(f"pub type {type_name} = Option<unsafe extern \"C\" fn({param_str})>;")

    lines.append("")

    # Struct types
    lines += [
        "// ---------------------------------------------------------------------------",
        "// Structs",
        "// ---------------------------------------------------------------------------",
        "",
    ]
    for struct_name, struct_def in structs.items():
        rust_struct_name = snake_to_pascal(strip_suffix(strip_suffix(strip_prefix(struct_name, sp), "_t"), "_s"))
        # For Rust, keep the full name for clarity
        full_struct_name = snake_to_pascal(strip_suffix(strip_suffix(struct_name, "_t"), "_s"))
        fields_raw: list[dict[str, Any]] = struct_def.get("fields") or []

        lines.append("#[repr(C)]")
        lines.append(f"pub struct {full_struct_name} {{")
        if fields_raw:
            for field in fields_raw:
                f_name = str(field.get("name") or "")
                f_decl = str(field.get("declaration") or "")
                if f_decl and f_name:
                    tokens = f_decl.rsplit(None, 1)
                    f_c_type = tokens[0].strip() if len(tokens) >= 2 else "int"
                else:
                    f_c_type = "int"
                f_rust = c_type_to_rust(f_c_type, opaque_types, sp)
                lines.append(f"    pub {f_name}: {f_rust},")
        else:
            lines.append("    pub _dummy: u8,")
        lines.append("}")
        lines.append("")

    # FFI extern block
    lines += [
        "// ---------------------------------------------------------------------------",
        "// FFI extern block",
        "// ---------------------------------------------------------------------------",
        "",
        f"#[link(name = \"{target}\")]",
        "extern \"C\" {",
    ]

    for func in functions:
        f_name = str(func.get("name") or "")
        if not f_name:
            continue
        ret_c = str(func.get("c_return_type") or "void")
        params: list[dict[str, Any]] = func.get("parameters") or []

        ret_rust = c_return_type_to_rust(ret_c, opaque_types, sp)

        rust_params: list[str] = []
        for p in params:
            p_name = str(p.get("name") or "arg")
            p_c_type = str(p.get("c_type") or "void")
            p_rust = c_type_to_rust(p_c_type, opaque_types, sp)
            rust_params.append(f"{p_name}: {p_rust}")

        param_str = ", ".join(rust_params)
        if ret_rust:
            lines.append(f"    pub fn {f_name}({param_str}) -> {ret_rust};")
        else:
            lines.append(f"    pub fn {f_name}({param_str});")

    lines.append("}")
    lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Rust FFI bindings from IDL JSON.")
    parser.add_argument("--idl", required=True, help="IDL JSON path.")
    parser.add_argument("--out", required=True, help="Output .rs file path.")
    parser.add_argument("--symbol-prefix", default=None, help="Symbol prefix override.")
    parser.add_argument("--check", action="store_true", help="Fail if output would change.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files.")
    args = parser.parse_args()

    idl_path = Path(args.idl)
    out_path = Path(args.out)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = generate_rust_ffi(idl, getattr(args, "symbol_prefix", None))

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
