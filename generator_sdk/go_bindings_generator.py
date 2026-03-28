#!/usr/bin/env python3
"""
Go cgo binding generator.

Generates a complete Go package using cgo including:
  - Enum types and const blocks
  - Opaque handle wrapper structs with runtime.SetFinalizer
  - Constructor (NewXxx), Close, and method functions
  - All function bindings via cgo

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
    return "".join(w.capitalize() for w in name.lower().split("_") if w)


def snake_to_camel(name: str) -> str:
    parts = name.split("_")
    if not parts:
        return name
    return parts[0] + "".join(w.capitalize() for w in parts[1:] if w)


# ---------------------------------------------------------------------------
# C → Go/cgo type map
# ---------------------------------------------------------------------------

# (cgo_type, go_type)
_C_TO_GO: dict[str, tuple[str, str]] = {
    "void": ("", ""),
    "bool": ("C.bool", "bool"),
    "_Bool": ("C.bool", "bool"),
    "char": ("C.schar", "int8"),
    "signed char": ("C.schar", "int8"),
    "int8_t": ("C.schar", "int8"),
    "unsigned char": ("C.uchar", "uint8"),
    "uint8_t": ("C.uchar", "uint8"),
    "short": ("C.short", "int16"),
    "int16_t": ("C.short", "int16"),
    "unsigned short": ("C.ushort", "uint16"),
    "uint16_t": ("C.ushort", "uint16"),
    "int": ("C.int", "int32"),
    "int32_t": ("C.int", "int32"),
    "unsigned int": ("C.uint", "uint32"),
    "uint32_t": ("C.uint", "uint32"),
    "long long": ("C.longlong", "int64"),
    "int64_t": ("C.longlong", "int64"),
    "unsigned long long": ("C.ulonglong", "uint64"),
    "uint64_t": ("C.ulonglong", "uint64"),
    "float": ("C.float", "float32"),
    "double": ("C.double", "float64"),
    "size_t": ("C.size_t", "uintptr"),
    "ssize_t": ("C.size_t", "uintptr"),
    "long": ("C.long", "int64"),
    "unsigned long": ("C.ulong", "uint64"),
}


def c_type_to_cgo(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert C type to cgo parameter type."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return ""

    if bare_no_ptr == "void" and is_ptr:
        return "unsafe.Pointer"

    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "*C.char"

    if is_ptr and bare_no_ptr in opaque_types:
        return f"*C.{bare_no_ptr}"

    if bare_no_ptr in _C_TO_GO:
        cgo_t, _ = _C_TO_GO[bare_no_ptr]
        if is_ptr:
            if not cgo_t:
                return "unsafe.Pointer"
            return f"*{cgo_t}"
        return cgo_t if cgo_t else ""

    if is_ptr:
        return "unsafe.Pointer"

    return "C.int"


def c_type_to_go(c_type: str, opaque_types: dict[str, Any], symbol_prefix: str) -> str:
    """Convert C type to Go parameter type."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return ""

    if bare_no_ptr == "void" and is_ptr:
        return "unsafe.Pointer"

    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "string"

    if is_ptr and bare_no_ptr in opaque_types:
        class_name = infer_class_name(bare_no_ptr, symbol_prefix)
        return f"*{class_name}"

    if bare_no_ptr in _C_TO_GO:
        _, go_t = _C_TO_GO[bare_no_ptr]
        if is_ptr:
            if not go_t:
                return "unsafe.Pointer"
            return f"*{go_t}"
        return go_t if go_t else ""

    if is_ptr:
        return "unsafe.Pointer"

    return "int32"


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

def generate_go_bindings(
    idl: dict[str, Any],
    symbol_prefix_override: str | None = None,
    package_name: str | None = None,
    lib_name: str | None = None,
) -> str:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    target = str(idl.get("target") or "lumenrtc")
    effective_package = package_name or target.replace("-", "_")
    effective_lib = lib_name or target

    opaque_types = get_opaque_types(idl)
    enums = get_enums(idl)
    functions: list[dict[str, Any]] = idl.get("functions") or []

    # Check if unsafe is needed
    needs_unsafe = False
    for func in functions:
        ret_c = str(func.get("c_return_type") or "void")
        if "void*" in ret_c or "*" in ret_c:
            needs_unsafe = True
            break
        params = func.get("parameters") or []
        for p in params:
            ct = str(p.get("c_type") or "")
            if "void*" in ct or ("*" in ct and "char" not in ct):
                needs_unsafe = True
                break
        if needs_unsafe:
            break

    # Always import unsafe when we have opaque types
    if opaque_types:
        needs_unsafe = True

    lines: list[str] = []

    # Header
    lines += [
        "// Code generated by tools/abi_framework/generator_sdk/go_bindings_generator.py; DO NOT EDIT.",
        "",
        f"package {effective_package}",
        "",
        "/*",
        f"#cgo LDFLAGS: -l{effective_lib}",
        f"#include \"{target}.h\"",
        "*/",
        "import \"C\"",
        "import (",
        "    \"runtime\"",
    ]
    if needs_unsafe:
        lines.append("    \"unsafe\"")
    lines += [
        ")",
        "",
    ]

    if needs_unsafe:
        lines.append("var _ = unsafe.Sizeof(0)")
        lines.append("")

    # Enums
    lines += [
        "// \u2500\u2500 Enums \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]
    for enum_key, enum_def in enums.items():
        members: list[dict[str, Any]] = enum_def.get("members") or []
        type_name = infer_class_name(enum_key, sp)
        common_prefix = _common_enum_prefix(members)

        lines.append(f"type {type_name} int32")
        lines.append("")
        lines.append("const (")
        for i, mem in enumerate(members):
            raw_name = str(mem.get("name", ""))
            value = mem.get("value", 0)
            stripped = raw_name[len(common_prefix):] if raw_name.startswith(common_prefix) else raw_name
            go_name = to_pascal_from_upper(stripped) if stripped else f"V{value}"
            if not go_name or go_name[0].isdigit():
                go_name = "V" + go_name
            const_name = f"{type_name}{go_name}"
            lines.append(f"    {const_name} {type_name} = {value}")
        lines.append(")")
        lines.append("")

    # Opaque handles
    lines += [
        "// \u2500\u2500 Opaque handles \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
        "",
    ]

    func_groups = group_functions_by_handle(functions, opaque_types)
    # Track constructor functions that are "claimed" by OOP wrappers
    claimed_as_constructors: set[str] = set()

    for ot_name in sorted(opaque_types.keys()):
        class_name = infer_class_name(ot_name, sp)
        ot_info = opaque_types[ot_name]
        release_fn = str(ot_info.get("release") or "")

        bare_type = strip_prefix(ot_name, sp)
        bare_type = strip_suffix(strip_suffix(bare_type, "_t"), "_s")
        method_prefix = sp + bare_type + "_"

        handle_funcs = func_groups.get(ot_name) or []

        # Find constructor — may be in global group (no handle param) or handle group
        create_fn = f"{method_prefix}create"
        global_funcs_all = func_groups.get("__global__") or []
        create_func = next(
            (f for f in list(handle_funcs) + list(global_funcs_all)
             if str(f.get("name") or "") == create_fn),
            None,
        )
        if create_func:
            claimed_as_constructors.add(create_fn)

        lines += [
            f"// {class_name} wraps {ot_name}*.",
            f"type {class_name} struct {{",
            f"    ptr *C.{ot_name}",
            "}",
            "",
        ]

        # Constructor
        if create_func:
            create_params = create_func.get("parameters") or []
            go_ctor_params: list[str] = []
            cgo_call_args: list[str] = []
            for p in create_params:
                p_name = str(p.get("name") or "arg")
                p_c_type = str(p.get("c_type") or "void")
                p_go_type = c_type_to_go(p_c_type, opaque_types, sp)
                p_cgo_type = c_type_to_cgo(p_c_type, opaque_types, sp)
                if p_go_type:
                    go_ctor_params.append(f"{p_name} {p_go_type}")
                    if p_go_type == "string":
                        cgo_call_args.append(f"C.CString({p_name})")
                    elif p_cgo_type and p_cgo_type != p_go_type:
                        cgo_call_args.append(f"({p_cgo_type})({p_name})")
                    else:
                        cgo_call_args.append(p_name)

            ctor_param_str = ", ".join(go_ctor_params)
            ctor_args_str = ", ".join(cgo_call_args)

            lines += [
                f"// New{class_name} creates a new {class_name}.",
                f"func New{class_name}({ctor_param_str}) *{class_name} {{",
                f"    h := &{class_name}{{ptr: C.{create_fn}({ctor_args_str})}}",
                f"    runtime.SetFinalizer(h, (*{class_name}).Close)",
                "    return h",
                "}",
                "",
            ]
        else:
            lines += [
                f"// New{class_name} creates a new {class_name}.",
                f"// Note: no create function found in IDL.",
                f"func New{class_name}() *{class_name} {{",
                f"    return &{class_name}{{}}",
                "}",
                "",
            ]

        # Close method
        if release_fn:
            lines += [
                f"// Close releases the native resource.",
                f"func (h *{class_name}) Close() {{",
                "    if h.ptr != nil {",
                f"        C.{release_fn}(h.ptr)",
                "        h.ptr = nil",
                "    }",
                "}",
                "",
            ]
        else:
            lines += [
                f"// Close is a no-op (no release function in IDL).",
                f"func (h *{class_name}) Close() {{",
                "}",
                "",
            ]

        # Methods
        for func in handle_funcs:
            f_name = str(func.get("name") or "")
            if not f_name:
                continue
            if f_name == create_fn or f_name == release_fn:
                continue

            params: list[dict[str, Any]] = func.get("parameters") or []
            ret_c = str(func.get("c_return_type") or "void")
            ret_go = c_type_to_go(ret_c, opaque_types, sp)
            ret_cgo = c_type_to_cgo(ret_c, opaque_types, sp)

            method_raw = strip_prefix(f_name, method_prefix)
            if not method_raw:
                method_raw = strip_prefix(f_name, sp)
            method_name = snake_to_pascal(method_raw)

            # Parameters (skip first = the handle itself)
            extra_params = params[1:] if params else []
            go_params: list[str] = []
            cgo_call_args: list[str] = ["h.ptr"]

            for p in extra_params:
                p_name = str(p.get("name") or "arg")
                p_c_type = str(p.get("c_type") or "void")
                p_go_type = c_type_to_go(p_c_type, opaque_types, sp)
                p_cgo_type = c_type_to_cgo(p_c_type, opaque_types, sp)
                if p_go_type:
                    go_params.append(f"{p_name} {p_go_type}")
                    if p_go_type == "string":
                        cgo_call_args.append(f"C.CString({p_name})")
                    elif p_cgo_type and p_cgo_type != p_go_type:
                        cgo_call_args.append(f"({p_cgo_type})({p_name})")
                    else:
                        cgo_call_args.append(p_name)

            param_str = ", ".join(go_params)
            call_str = f"C.{f_name}({', '.join(cgo_call_args)})"

            if not ret_go:
                lines += [
                    f"// {method_name} calls {f_name}.",
                    f"func (h *{class_name}) {method_name}({param_str}) {{",
                    f"    {call_str}",
                    "}",
                    "",
                ]
            elif ret_go == "string":
                lines += [
                    f"// {method_name} calls {f_name}.",
                    f"func (h *{class_name}) {method_name}({param_str}) {ret_go} {{",
                    f"    return C.GoString({call_str})",
                    "}",
                    "",
                ]
            elif ret_go in ("bool",):
                lines += [
                    f"// {method_name} calls {f_name}.",
                    f"func (h *{class_name}) {method_name}({param_str}) {ret_go} {{",
                    f"    return {call_str} != 0",
                    "}",
                    "",
                ]
            else:
                # Check if ret needs type conversion
                needs_cast = ret_cgo and ret_cgo != ret_go and ret_go not in ("unsafe.Pointer",)
                if needs_cast:
                    lines += [
                        f"// {method_name} calls {f_name}.",
                        f"func (h *{class_name}) {method_name}({param_str}) {ret_go} {{",
                        f"    return {ret_go}({call_str})",
                        "}",
                        "",
                    ]
                else:
                    lines += [
                        f"// {method_name} calls {f_name}.",
                        f"func (h *{class_name}) {method_name}({param_str}) {ret_go} {{",
                        f"    return {call_str}",
                        "}",
                        "",
                    ]

    # Global functions
    global_funcs = func_groups.get("__global__") or []
    if global_funcs:
        lines += [
            "// \u2500\u2500 Global functions \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500",
            "",
        ]
        for func in global_funcs:
            f_name = str(func.get("name") or "")
            if not f_name:
                continue
            # Skip functions claimed as OOP constructors
            if f_name in claimed_as_constructors:
                continue
            params: list[dict[str, Any]] = func.get("parameters") or []
            ret_c = str(func.get("c_return_type") or "void")
            ret_go = c_type_to_go(ret_c, opaque_types, sp)
            ret_cgo = c_type_to_cgo(ret_c, opaque_types, sp)

            func_name = snake_to_pascal(strip_prefix(f_name, sp))

            go_params: list[str] = []
            cgo_call_args: list[str] = []
            for p in params:
                p_name = str(p.get("name") or "arg")
                p_c_type = str(p.get("c_type") or "void")
                p_go_type = c_type_to_go(p_c_type, opaque_types, sp)
                p_cgo_type = c_type_to_cgo(p_c_type, opaque_types, sp)
                if p_go_type:
                    go_params.append(f"{p_name} {p_go_type}")
                    if p_go_type == "string":
                        cgo_call_args.append(f"C.CString({p_name})")
                    elif p_cgo_type and p_cgo_type != p_go_type:
                        cgo_call_args.append(f"({p_cgo_type})({p_name})")
                    else:
                        cgo_call_args.append(p_name)

            param_str = ", ".join(go_params)
            call_str = f"C.{f_name}({', '.join(cgo_call_args)})"

            if not ret_go:
                lines += [
                    f"// {func_name} calls {f_name}.",
                    f"func {func_name}({param_str}) {{",
                    f"    {call_str}",
                    "}",
                    "",
                ]
            elif ret_go == "string":
                lines += [
                    f"// {func_name} calls {f_name}.",
                    f"func {func_name}({param_str}) {ret_go} {{",
                    f"    return C.GoString({call_str})",
                    "}",
                    "",
                ]
            else:
                needs_cast = ret_cgo and ret_cgo != ret_go and ret_go not in ("unsafe.Pointer",)
                if needs_cast:
                    lines += [
                        f"// {func_name} calls {f_name}.",
                        f"func {func_name}({param_str}) {ret_go} {{",
                        f"    return {ret_go}({call_str})",
                        "}",
                        "",
                    ]
                else:
                    lines += [
                        f"// {func_name} calls {f_name}.",
                        f"func {func_name}({param_str}) {ret_go} {{",
                        f"    return {call_str}",
                        "}",
                        "",
                    ]

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Go cgo bindings from IDL JSON.")
    parser.add_argument("--idl", required=True, help="IDL JSON path.")
    parser.add_argument("--out", required=True, help="Output .go file path.")
    parser.add_argument("--symbol-prefix", default=None, help="Symbol prefix override.")
    parser.add_argument("--package-name", default=None, help="Go package name.")
    parser.add_argument("--lib-name", default=None, help="Library name for cgo LDFLAGS.")
    parser.add_argument("--check", action="store_true", help="Fail if output would change.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files.")
    args = parser.parse_args()

    idl_path = Path(args.idl)
    out_path = Path(args.out)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = generate_go_bindings(
        idl,
        getattr(args, "symbol_prefix", None),
        getattr(args, "package_name", None),
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
