#!/usr/bin/env python3
"""
Python ctypes binding generator.

Generates a complete Python module that provides:
  - ctypes function signatures for all exported functions
  - IntEnum classes for all enums
  - ctypes.Structure subclasses for all structs
  - CFUNCTYPE definitions for all callback typedefs
  - Opaque handle wrapper classes with context manager support
  - A high-level OOP interface with handles grouped by owning type
  - A load() function to initialize the library

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


# ---------------------------------------------------------------------------
# C → Python ctypes type map
# ---------------------------------------------------------------------------

_C_TO_CTYPES: dict[str, str] = {
    "void": "None",
    "bool": "ctypes.c_bool",
    "_Bool": "ctypes.c_bool",
    "char": "ctypes.c_char",
    "signed char": "ctypes.c_int8",
    "unsigned char": "ctypes.c_uint8",
    "int8_t": "ctypes.c_int8",
    "uint8_t": "ctypes.c_uint8",
    "int16_t": "ctypes.c_int16",
    "uint16_t": "ctypes.c_uint16",
    "short": "ctypes.c_short",
    "unsigned short": "ctypes.c_ushort",
    "int": "ctypes.c_int",
    "int32_t": "ctypes.c_int32",
    "unsigned int": "ctypes.c_uint",
    "uint32_t": "ctypes.c_uint32",
    "long": "ctypes.c_long",
    "unsigned long": "ctypes.c_ulong",
    "long long": "ctypes.c_longlong",
    "unsigned long long": "ctypes.c_ulonglong",
    "int64_t": "ctypes.c_int64",
    "uint64_t": "ctypes.c_uint64",
    "size_t": "ctypes.c_size_t",
    "ssize_t": "ctypes.c_ssize_t",
    "intptr_t": "ctypes.c_ssize_t",
    "uintptr_t": "ctypes.c_size_t",
    "float": "ctypes.c_float",
    "double": "ctypes.c_double",
}


def c_type_to_ctypes(c_type: str, opaque_types: set[str], symbol_prefix: str) -> str:
    """Convert a C type string to its ctypes equivalent."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    # void* → c_void_p
    if bare_no_ptr == "void" and is_ptr:
        return "ctypes.c_void_p"

    # const char* → c_char_p
    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "ctypes.c_char_p"

    # Opaque handle pointer
    if is_ptr and bare_no_ptr in opaque_types:
        class_name = infer_class_name(bare_no_ptr, symbol_prefix) + "Handle"
        return class_name

    # Non-pointer primitive
    if bare_no_ptr in _C_TO_CTYPES:
        ct = _C_TO_CTYPES[bare_no_ptr]
        if is_ptr:
            if ct == "None":
                return "ctypes.c_void_p"
            return f"ctypes.POINTER({ct})"
        return ct if ct != "None" else "None"

    # Struct pointer
    if is_ptr:
        struct_name = snake_to_pascal(strip_suffix(strip_suffix(bare_no_ptr, "_t"), "_s"))
        return f"ctypes.POINTER({struct_name})"

    # Unknown struct/enum (passed by value - rare)
    return "ctypes.c_void_p"


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

def _strip_enum_member_prefix(member_name: str, symbol_prefix: str) -> str:
    """Strip C prefix from enum member name, return Python-style name."""
    # e.g. LRTC_DATA_CHANNEL_CONNECTING → CONNECTING (strip LRTC_DATA_CHANNEL_)
    # We strip the uppercase version of the symbol prefix first
    upper_prefix = symbol_prefix.upper()
    m = member_name
    if m.startswith(upper_prefix):
        m = m[len(upper_prefix):]
    return m


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
    # Truncate to last underscore boundary
    last_us = prefix.rfind("_")
    if last_us >= 0:
        prefix = prefix[:last_us + 1]
    return prefix


# ---------------------------------------------------------------------------
# Callback typedef parser (extract params)
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

def _indent(lines: list[str], n: int = 4) -> list[str]:
    prefix = " " * n
    return [prefix + line for line in lines]


def generate_bindings(
    idl: dict[str, Any],
    symbol_prefix_override: str | None = None,
) -> str:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    target = str(idl.get("target") or "lumenrtc")
    target_upper = target.upper().replace("-", "_")

    opaque_types = get_opaque_types(idl)
    opaque_set = set(opaque_types.keys())
    enums = get_enums(idl)
    structs = get_structs(idl)
    callback_typedefs = get_callback_typedefs(idl)
    functions: list[dict[str, Any]] = idl.get("functions") or []

    lines: list[str] = []

    # Header
    lines += [
        f"# {target}_ctypes.py — generated by abi_framework python_bindings_generator",
        "# DO NOT EDIT — regenerate with: abi_framework generate-python-bindings",
        "#",
        "# Python ctypes bindings for lib" + target,
        "",
        "from __future__ import annotations",
        "import ctypes",
        "import ctypes.util",
        "import os",
        "import sys",
        "from enum import IntEnum",
        "from pathlib import Path",
        "from typing import Any, Optional",
        "",
    ]

    # Library loader
    lines += [
        "# ---------------------------------------------------------------------------",
        "# Library loader",
        "# ---------------------------------------------------------------------------",
        "",
        "_LIB: Optional[ctypes.CDLL] = None",
        "",
        "",
        f"def load(path: Optional[str] = None) -> ctypes.CDLL:",
        f'    """Load lib{target} and bind all function signatures."""',
        "    global _LIB",
        "    if path is None:",
        f'        for name in ["{target}", "lib{target}", "lib{target}.so", "lib{target}.dylib"]:',
        "            found = ctypes.util.find_library(name)",
        "            if found:",
        "                path = found",
        "                break",
        f'    env = os.environ.get("{target_upper}_LIBRARY_PATH")',
        "    if env:",
        "        path = env",
        "    if path is None:",
        f'        raise OSError(',
        f'            "Could not find lib{target}. "',
        f'            "Set {target_upper}_LIBRARY_PATH environment variable or pass path= to load()."',
        f'        )',
        "    _LIB = ctypes.CDLL(path)",
        "    _bind_all(_LIB)",
        "    return _LIB",
        "",
        "",
        "def get_lib() -> ctypes.CDLL:",
        '    """Return the loaded library, raising if not yet loaded."""',
        "    if _LIB is None:",
        '        raise RuntimeError("Library not loaded. Call load() first.")',
        "    return _LIB",
        "",
    ]

    # Enums
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# Enums",
        "# ---------------------------------------------------------------------------",
        "",
    ]
    for enum_key, enum_def in enums.items():
        members: list[dict[str, Any]] = enum_def.get("members") or []
        class_name = infer_class_name(enum_key, sp)
        common_prefix = _common_enum_prefix(members)

        lines.append(f"class {class_name}(IntEnum):")
        if members:
            for mem in members:
                raw_name = str(mem.get("name", ""))
                value = mem.get("value", 0)
                py_name = raw_name[len(common_prefix):] if raw_name.startswith(common_prefix) else raw_name
                if not py_name or py_name[0].isdigit():
                    py_name = "V_" + py_name
                lines.append(f"    {py_name} = {value}")
        else:
            lines.append("    pass")
        lines.append("")

    # Opaque handle types
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# Opaque handle types",
        "# ---------------------------------------------------------------------------",
        "",
    ]
    for ot_name in sorted(opaque_types.keys()):
        handle_class = infer_class_name(ot_name, sp) + "Handle"
        lines.append(f"class {handle_class}(ctypes.c_void_p): pass")
    lines.append("")

    # Callback function types
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# Callback function types",
        "# ---------------------------------------------------------------------------",
        "",
    ]
    for cb in callback_typedefs:
        cb_name = str(cb.get("name") or "")
        decl = str(cb.get("declaration") or "")
        if not cb_name:
            continue

        # Extract return type from declaration
        # e.g. typedef void (LUMENRTC_CALL *lrtc_sdp_success_cb)(...)
        ret_match = re.match(r"typedef\s+(\w[\w\s\*]*?)\s*\(", decl)
        ret_c = ret_match.group(1).strip() if ret_match else "void"
        ret_ct = c_type_to_ctypes(ret_c, opaque_set, sp)

        raw_params = parse_callback_params(decl)
        param_cts: list[str] = []
        for c_type, _name in raw_params:
            param_cts.append(c_type_to_ctypes(c_type, opaque_set, sp))

        type_name = snake_to_pascal(strip_prefix(cb_name, sp))
        all_types = [ret_ct] + param_cts
        lines.append(f"{type_name} = ctypes.CFUNCTYPE({', '.join(all_types)})")
    lines.append("")

    # Struct types
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# Struct types",
        "# ---------------------------------------------------------------------------",
        "",
    ]
    for struct_name, struct_def in structs.items():
        class_name = infer_class_name(struct_name, sp)
        fields_raw: list[dict[str, Any]] = struct_def.get("fields") or []

        lines.append(f"class {class_name}(ctypes.Structure):")
        lines.append("    _fields_: list = [")
        if fields_raw:
            for field in fields_raw:
                f_name = str(field.get("name") or "")
                f_decl = str(field.get("declaration") or "")
                # Detect function pointer field (contains '(*' or '( *')
                if "(*" in f_decl or "( *" in f_decl:
                    # Function pointer field → use c_void_p
                    f_ct = "ctypes.c_void_p"
                elif f_decl and f_name:
                    # Parse type from declaration: everything except the last identifier
                    # Strip trailing field name, handle pointer prefix
                    d = f_decl.strip()
                    if d.endswith(f_name):
                        f_c_type = d[: -len(f_name)].strip().rstrip("*").strip()
                        if d[: -len(f_name)].strip().endswith("*"):
                            f_c_type = d[: -len(f_name)].strip()[:-1].strip() + "*"
                            # reconstruct: base type + pointer
                            base = d[: -len(f_name)].strip()
                            f_c_type = base
                        else:
                            f_c_type = f_c_type
                        # Actually do a simple rsplit on space
                        tokens = f_decl.rsplit(None, 1)
                        f_c_type = tokens[0].strip() if len(tokens) >= 2 else "int"
                    else:
                        tokens = f_decl.rsplit(None, 1)
                        f_c_type = tokens[0].strip() if len(tokens) >= 2 else "int"
                    f_ct = c_type_to_ctypes(f_c_type, opaque_set, sp)
                else:
                    f_ct = "ctypes.c_int"
                lines.append(f'        ("{f_name}", {f_ct}),')
        else:
            lines.append('        ("_dummy", ctypes.c_uint8),')
        lines.append("    ]")
        lines.append("")

    # Function bindings
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# Function bindings",
        "# ---------------------------------------------------------------------------",
        "",
        "def _bind_all(lib: ctypes.CDLL) -> None:",
    ]

    bind_lines: list[str] = []
    for func in functions:
        f_name = str(func.get("name") or "")
        if not f_name:
            continue
        ret_c = str(func.get("c_return_type") or "void")
        params: list[dict[str, Any]] = func.get("parameters") or []

        ret_ct = c_type_to_ctypes(ret_c, opaque_set, sp)
        if ret_ct == "None":
            bind_lines.append(f"    lib.{f_name}.restype = None")
        else:
            bind_lines.append(f"    lib.{f_name}.restype = {ret_ct}")

        if params:
            arg_types = [c_type_to_ctypes(str(p.get("c_type") or "void"), opaque_set, sp) for p in params]
            bind_lines.append(f"    lib.{f_name}.argtypes = [{', '.join(arg_types)}]")
        else:
            bind_lines.append(f"    lib.{f_name}.argtypes = []")

    if bind_lines:
        lines.extend(bind_lines)
    else:
        lines.append("    pass")
    lines.append("")

    # High-level wrappers
    lines += [
        "",
        "# ---------------------------------------------------------------------------",
        "# High-level handle wrappers",
        "# ---------------------------------------------------------------------------",
        "",
    ]

    func_groups = group_functions_by_handle(functions, opaque_types)

    for ot_name in sorted(opaque_types.keys()):
        class_name = infer_class_name(ot_name, sp)
        handle_class = class_name + "Handle"
        ot_info = opaque_types[ot_name]
        release_fn = str(ot_info.get("release") or "")
        retain_fn = str(ot_info.get("retain") or "")

        # Compute the method prefix to strip from function names
        # e.g. lrtc_audio_device_get_name → strip "lrtc_" → audio_device_get_name
        # then in class strip "audio_device_" → get_name
        bare_type = strip_prefix(ot_name, sp)  # e.g. "audio_device_t"
        bare_type = strip_suffix(strip_suffix(bare_type, "_t"), "_s")  # "audio_device"
        method_prefix = sp + bare_type + "_"  # "lrtc_audio_device_"

        handle_funcs = func_groups.get(ot_name) or []

        lines.append(f'class {class_name}:')
        lines.append(f'    """Managed wrapper for {ot_name}."""')
        lines.append("")
        lines.append(f"    def __init__(self, _handle: {handle_class}) -> None:")
        lines.append(f"        self._h = _handle")
        lines.append("")
        lines.append("    def __del__(self) -> None:")
        lines.append("        self.release()")
        lines.append("")
        lines.append(f"    def __enter__(self) -> \"{class_name}\":")
        lines.append("        return self")
        lines.append("")
        lines.append("    def __exit__(self, *_: Any) -> None:")
        lines.append("        self.release()")
        lines.append("")
        lines.append("    def __bool__(self) -> bool:")
        lines.append("        return bool(self._h)")
        lines.append("")
        lines.append("    def release(self) -> None:")
        if release_fn:
            lines.append("        if self._h:")
            lines.append(f"            get_lib().{release_fn}(self._h)")
            lines.append("            self._h = None")
        else:
            lines.append("        self._h = None")
        lines.append("")

        if retain_fn:
            lines.append("    def retain(self) -> None:")
            lines.append("        if self._h:")
            lines.append(f"            get_lib().{retain_fn}(self._h)")
            lines.append("")

        # Generate methods for all grouped functions
        for func in handle_funcs:
            f_name = str(func.get("name") or "")
            if not f_name or f_name == release_fn or f_name == retain_fn:
                continue

            params: list[dict[str, Any]] = func.get("parameters") or []
            ret_c = str(func.get("c_return_type") or "void")

            # Method name: strip the handle prefix
            method_name = strip_prefix(f_name, method_prefix)
            if not method_name:
                method_name = strip_prefix(f_name, sp)

            # Build param list (skip first param = the handle itself)
            extra_params = params[1:] if params else []
            py_params = []
            call_args = ["self._h"]
            for p in extra_params:
                p_name = str(p.get("name") or "arg")
                p_type = str(p.get("c_type") or "void")
                py_type = _ctypes_to_py_annotation(p_type, opaque_set, sp)
                py_params.append(f"{p_name}: {py_type}")
                call_args.append(p_name)

            param_str = ", ".join(["self"] + py_params)
            ret_annotation = _ctypes_to_py_annotation(ret_c, opaque_set, sp, is_return=True)

            lines.append(f"    def {method_name}({param_str}) -> {ret_annotation}:")
            call_str = f"get_lib().{f_name}({', '.join(call_args)})"
            if ret_c.strip() == "void":
                lines.append(f"        {call_str}")
            else:
                lines.append(f"        return {call_str}")
            lines.append("")

        lines.append("")

    # Module-level functions (global group)
    global_funcs = func_groups.get("__global__") or []
    if global_funcs:
        lines += [
            "# ---------------------------------------------------------------------------",
            "# Module-level functions",
            "# ---------------------------------------------------------------------------",
            "",
        ]
        for func in global_funcs:
            f_name = str(func.get("name") or "")
            if not f_name:
                continue
            params: list[dict[str, Any]] = func.get("parameters") or []
            ret_c = str(func.get("c_return_type") or "void")

            py_name = strip_prefix(f_name, sp)
            py_params = []
            call_args = []
            for p in params:
                p_name = str(p.get("name") or "arg")
                p_type = str(p.get("c_type") or "void")
                py_type = _ctypes_to_py_annotation(p_type, opaque_set, sp)
                py_params.append(f"{p_name}: {py_type}")
                call_args.append(p_name)

            param_str = ", ".join(py_params)
            ret_annotation = _ctypes_to_py_annotation(ret_c, opaque_set, sp, is_return=True)

            lines.append(f"def {py_name}({param_str}) -> {ret_annotation}:")
            call_str = f"get_lib().{f_name}({', '.join(call_args)})"
            if ret_c.strip() == "void":
                lines.append(f"    {call_str}")
            else:
                lines.append(f"    return {call_str}")
            lines.append("")

    return "\n".join(lines) + "\n"


def _ctypes_to_py_annotation(
    c_type: str,
    opaque_set: set[str],
    symbol_prefix: str,
    is_return: bool = False,
) -> str:
    """Convert C type to Python type annotation (for docstrings/stubs)."""
    t = c_type.strip()
    bare = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = bare.endswith("*")
    bare_no_ptr = bare.rstrip("*").strip()

    if bare_no_ptr == "void" and not is_ptr:
        return "None"
    if bare_no_ptr in ("char", "signed char") and is_ptr:
        return "Optional[bytes]"
    if bare_no_ptr == "void" and is_ptr:
        return "int"  # c_void_p → int in Python
    if is_ptr and bare_no_ptr in opaque_set:
        class_name = infer_class_name(bare_no_ptr, symbol_prefix) + "Handle"
        return f"Optional[{class_name}]"
    if bare_no_ptr == "bool" or bare_no_ptr == "_Bool":
        return "bool"
    if bare_no_ptr in _C_TO_CTYPES:
        ct = _C_TO_CTYPES[bare_no_ptr]
        if ct == "None":
            return "None"
        # Map ctypes to Python native types
        if ct in ("ctypes.c_bool",):
            return "bool"
        if ct in ("ctypes.c_float", "ctypes.c_double"):
            return "float"
        if ct in ("ctypes.c_char_p",):
            return "Optional[bytes]"
        return "int"  # integers
    return "Any"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Generate Python ctypes bindings from IDL JSON.")
    parser.add_argument("--idl", required=True, help="IDL JSON path.")
    parser.add_argument("--out", required=True, help="Output .py file path.")
    parser.add_argument("--symbol-prefix", default=None, help="Symbol prefix override.")
    parser.add_argument("--check", action="store_true", help="Fail if output would change.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write files.")
    args = parser.parse_args()

    idl_path = Path(args.idl)
    out_path = Path(args.out)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = generate_bindings(idl, getattr(args, "symbol_prefix", None))

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
