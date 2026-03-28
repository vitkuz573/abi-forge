#!/usr/bin/env python3
"""
Managed API scaffold generator.

Given an IDL JSON, generates a starter ``managed_api.source.json`` that:
  - Enables ``auto_abi_surface`` (zero-config P/Invoke for ALL functions)
  - Stubs out ``callbacks`` classes for each callback struct found in the IDL
  - Stubs out ``handle_api`` classes for each opaque handle type
  - Uses smart type inference to fill in managed parameter types

Run once (--force) to bootstrap, then use --update to merge IDL changes
into an existing file without losing existing customizations.
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


# ---------------------------------------------------------------------------
# Name utilities
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


def infer_native_struct_name(c_type_name: str) -> str:
    name = c_type_name
    for suf in ("_t", "_s"):
        name = strip_suffix(name, suf)
    return snake_to_pascal(name)


# ---------------------------------------------------------------------------
# IDL extraction helpers
# ---------------------------------------------------------------------------

def get_symbol_prefix(idl: dict[str, Any], override: str | None) -> str:
    if override is not None:
        return override
    # Try IDL codegen section (added by framework >= this version)
    codegen = idl.get("codegen") or {}
    sp = codegen.get("symbol_prefix")
    if isinstance(sp, str) and sp:
        return sp
    # Infer from target name + first function
    target = str(idl.get("target") or "")
    functions = idl.get("functions") or []
    guessed = target.rstrip("_") + "_"
    if functions:
        first = str(functions[0].get("name") or "")
        if first.startswith(guessed):
            return guessed
    return ""


def get_header_structs(idl: dict[str, Any]) -> dict[str, Any]:
    """Return the struct definitions dict from header_types (not idl.structs)."""
    return idl.get("header_types", {}).get("structs", {}) or {}


def get_callback_typedefs(idl: dict[str, Any]) -> dict[str, str]:
    """Return {typedef_name: declaration_string} from header_types.callback_typedefs."""
    raw = idl.get("header_types", {}).get("callback_typedefs") or []
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "")
                decl = str(item.get("declaration") or "")
                if name:
                    result[name] = decl
    elif isinstance(raw, dict):
        for name, decl in raw.items():
            result[str(name)] = str(decl)
    return result


def get_opaque_types(idl: dict[str, Any]) -> dict[str, Any]:
    return idl.get("bindings", {}).get("interop", {}).get("opaque_types", {}) or {}


def get_callback_suffixes(idl: dict[str, Any]) -> list[str]:
    raw = idl.get("bindings", {}).get("interop", {}).get("callback_struct_suffixes") or []
    return list(raw) if isinstance(raw, list) else ["_callbacks_t"]


def get_idl_enums(idl: dict[str, Any]) -> set[str]:
    enums = idl.get("header_types", {}).get("enums") or {}
    if isinstance(enums, dict):
        return set(enums.keys())
    return set()


# ---------------------------------------------------------------------------
# Typedef declaration parser
# ---------------------------------------------------------------------------

def _split_c_params(params_str: str) -> list[str]:
    """Split a C parameter list by comma, respecting parens."""
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


def parse_typedef_params(declaration: str) -> list[tuple[str, str]]:
    """
    Parse (c_type, param_name) pairs from a typedef function pointer declaration.
    Skips the leading user_data/ud/ctx/void* parameter.
    """
    # Extract everything inside the last pair of parens (the parameter list)
    m = re.search(r"\)\s*\(([^)]*(?:\([^)]*\)[^)]*)*)\)\s*;?\s*$", declaration)
    if not m:
        return []
    params_str = m.group(1).strip()
    if not params_str or params_str == "void":
        return []

    raw_params = _split_c_params(params_str)
    result: list[tuple[str, str]] = []
    first = True
    for param in raw_params:
        param = param.strip()
        if not param or param == "...":
            continue
        # Split into type + name: last non-* token is the name
        # Handle function pointer params specially (rare in callbacks)
        tokens = param.rsplit(None, 1)
        if len(tokens) == 2:
            c_type = tokens[0].strip().lstrip("*").strip()
            name = tokens[1].strip().lstrip("*").strip()
        else:
            c_type = param
            name = "arg"
        # Skip user_data-like first params
        if first and name.lower() in ("user_data", "ud", "ctx", "context", "userdata", "self"):
            first = False
            continue
        # Also skip explicit void* params
        if c_type.replace("const", "").strip().rstrip("*").strip() == "void" and name.lower() in (
            "user_data", "ud", "ctx", "context", "userdata",
        ):
            first = False
            continue
        first = False
        result.append((c_type, name))
    return result


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------

# C primitive → C# type
_PRIMITIVE_MAP = {
    "bool": "bool", "_Bool": "bool",
    "int": "int", "int32_t": "int",
    "unsigned int": "uint", "uint32_t": "uint",
    "long": "nint", "long long": "long", "int64_t": "long",
    "unsigned long": "nuint", "unsigned long long": "ulong", "uint64_t": "ulong",
    "short": "short", "int16_t": "short",
    "unsigned short": "ushort", "uint16_t": "ushort",
    "float": "float", "double": "double",
    "size_t": "nuint", "ssize_t": "nint",
    "intptr_t": "nint", "uintptr_t": "nuint",
    "char": "sbyte",
    "signed char": "sbyte", "int8_t": "sbyte",
    "unsigned char": "byte", "uint8_t": "byte",
}


def _bare_type(c_type: str) -> tuple[str, bool]:
    """Return (bare_type_without_const_and_pointer, is_pointer)."""
    t = c_type.strip()
    t = re.sub(r"\bconst\b", "", t).strip()
    is_ptr = t.endswith("*")
    t = t.rstrip("*").strip()
    return t, is_ptr


def infer_param(
    c_type: str,
    param_name: str,
    idl_enums: set[str],
    opaque_types: dict[str, Any],
    symbol_prefix: str,
) -> tuple[str, str]:
    """Return (managed_type_str, marshal_expression)."""
    bare, is_ptr = _bare_type(c_type)

    # const char* / char* → string
    if bare in ("char", "signed char") and is_ptr:
        return "string?", f"Utf8String.Read({param_name})"

    # Enum (non-pointer)
    if not is_ptr and bare in idl_enums:
        managed_enum = infer_class_name(bare, symbol_prefix)
        return managed_enum, f"({managed_enum}){param_name}"

    # Opaque handle pointer
    if is_ptr and bare in opaque_types:
        class_name = infer_class_name(bare, symbol_prefix)
        return f"{class_name}?", f"new {class_name}({param_name})"

    # bool via int binary flag pattern
    if bare == "int" and param_name.lower() in ("binary", "is_binary", "is_text"):
        return "bool", f"{param_name} != 0"

    # int/uint enum detection heuristic
    if bare in ("int", "unsigned int", "uint32_t", "int32_t") and not is_ptr:
        if param_name.lower() in ("state", "type", "kind", "result", "error", "code"):
            matching_enum = next(
                (k for k in idl_enums if strip_prefix(k, symbol_prefix).endswith(f"_{param_name}")),
                None,
            )
            if matching_enum:
                enum_class = infer_class_name(matching_enum, symbol_prefix)
                return f"{enum_class}  /* int */", f"({enum_class}){param_name}"

    # uint8_t* data buffer — generate TODO block (without buffer-pair detection here)
    if bare in ("uint8_t", "unsigned char") and is_ptr:
        return "ReadOnlyMemory<byte>", f"/* TODO: copy {param_name} buffer */"

    # Primitives
    if bare in _PRIMITIVE_MAP:
        return _PRIMITIVE_MAP[bare], param_name

    # Unknown
    return f"/* {c_type} */", f"{param_name}"


def _build_lambda_params(typed_params: list[tuple[str, str, str]]) -> str:
    """Build lambda parameter list: (ud, p1, p2, ...)"""
    names = ["ud"] + [name for _, name, _ in typed_params]
    return "(" + ", ".join(names) + ")"


def _build_invoke(managed_name: str, typed_params: list[tuple[str, str, str]]) -> str:
    """Build the Invoke(...) call from marshal expressions."""
    args = ", ".join(expr for _, _, expr in typed_params)
    return f"{managed_name}?.Invoke({args})"


def _has_complex_param(typed_params: list[tuple[str, str, str]]) -> bool:
    return any("TODO" in expr for _, _, expr in typed_params)


def detect_buffer_pairs(raw_params: list[tuple[str, str]]) -> dict[int, int]:
    """Return {ptr_param_index: length_param_index} for uint8_t*/int pairs."""
    pairs: dict[int, int] = {}
    for i, (c_type, name) in enumerate(raw_params):
        bare, is_ptr = _bare_type(c_type)
        if is_ptr and bare in ("uint8_t", "unsigned char"):
            for j in range(i + 1, min(i + 3, len(raw_params))):
                next_type, next_name = raw_params[j]
                nb, np = _bare_type(next_type)
                if not np and nb in ("int", "size_t", "uint32_t", "int32_t") and (
                    "length" in next_name.lower() or "len" in next_name.lower()
                    or "size" in next_name.lower() or "count" in next_name.lower()
                ):
                    pairs[i] = j
                    break
    return pairs


def build_buffer_copy_lines(
    managed_name: str,
    ptr_name: str,
    len_name: str,
    other_params: list[tuple[str, str, str]],
) -> list[str]:
    """Generate ReadOnlyMemory<byte> copy pattern."""
    other_args = ", ".join(expr for _, _, expr in other_params)
    invoke_args = "data" + (f", {other_args}" if other_args else "")
    param_names = ["ud", ptr_name, len_name] + [name for _, name, _ in other_params]
    lam = "(" + ", ".join(param_names) + ")"
    lines = [
        f"{lam} =>",
        "{",
        f"    if ({ptr_name} == IntPtr.Zero || {len_name} <= 0)",
        "    {",
        f"        {managed_name}?.Invoke(ReadOnlyMemory<byte>.Empty{', ' + other_args if other_args else ''});",
        "        return;",
        "    }",
        f"    var data = GC.AllocateUninitializedArray<byte>((int){len_name});",
        f"    Marshal.Copy({ptr_name}, data, 0, (int){len_name});",
        f"    {managed_name}?.Invoke({invoke_args});",
        "}",
    ]
    return lines


def build_assignment_lines(
    managed_name: str,
    typedef_decl: str,
    idl_enums: set[str],
    opaque_types: dict[str, Any],
    symbol_prefix: str,
) -> list[str]:
    """Generate assignment_lines for a callback field."""
    raw_params = parse_typedef_params(typedef_decl)
    if not raw_params:
        # No params beyond user_data: simple nullary invoke
        return [f"(ud) => {managed_name}?.Invoke()"]

    # Detect buffer pairs before processing
    buffer_pairs = detect_buffer_pairs(raw_params)
    # Indices that are length params (consumed by buffer pairs)
    len_indices = set(buffer_pairs.values())

    typed: list[tuple[str, str, str]] = []  # (managed_type, c_name, marshal_expr)
    for idx, (c_type, name) in enumerate(raw_params):
        if idx in len_indices:
            # Skip length param — it's absorbed into ReadOnlyMemory
            continue
        m_type, expr = infer_param(c_type, name, idl_enums, opaque_types, symbol_prefix)
        typed.append((m_type, name, expr))

    # If there are buffer pairs, generate the Marshal.Copy pattern
    if buffer_pairs:
        # Use the first buffer pair for the multi-line pattern
        ptr_idx = next(iter(buffer_pairs))
        len_idx = buffer_pairs[ptr_idx]
        _, ptr_name = raw_params[ptr_idx]
        _, len_name = raw_params[len_idx]
        # Other params: everything except ptr and len indices
        other: list[tuple[str, str, str]] = []
        for idx, (c_type, name) in enumerate(raw_params):
            if idx == ptr_idx or idx == len_idx:
                continue
            m_type, expr = infer_param(c_type, name, idl_enums, opaque_types, symbol_prefix)
            other.append((m_type, name, expr))
        return build_buffer_copy_lines(managed_name, ptr_name, len_name, other)

    lam_params = _build_lambda_params(typed)
    if _has_complex_param(typed):
        # Multi-line with comment hints
        lines = [f"{lam_params} =>", "{"]
        for m_type, name, expr in typed:
            if "TODO" in expr:
                lines.append(f"    // TODO: marshal {name} ({m_type})")
        invoke_args = ", ".join(
            expr if "TODO" not in expr else f"/* {name} */" for _, name, expr in typed
        )
        lines.append(f"    {managed_name}?.Invoke({invoke_args});")
        lines.append("}")
        return lines
    else:
        invoke = _build_invoke(managed_name, typed)
        return [f"{lam_params} => {invoke}"]


def build_managed_type_signature(
    typed_params: list[tuple[str, str, str]],
    raw_params: list[tuple[str, str]] | None = None,
) -> str:
    """Build Action<...> type from typed params.

    When raw_params is provided, buffer pairs are detected and the length param
    is removed from the managed Action<> signature (its size is encoded in
    ReadOnlyMemory<byte>).
    """
    if not typed_params:
        return "Action?"

    if raw_params is not None:
        buffer_pairs = detect_buffer_pairs(raw_params)
        len_indices = set(buffer_pairs.values())
        # Rebuild typed without len params, replacing ptr param type with ReadOnlyMemory<byte>
        filtered: list[tuple[str, str, str]] = []
        for idx, (c_type, name) in enumerate(raw_params):
            if idx in len_indices:
                continue
            if idx in buffer_pairs:
                filtered.append(("ReadOnlyMemory<byte>", name, ""))
            else:
                matching = next((t for t in typed_params if t[1] == name), None)
                if matching:
                    filtered.append(matching)
        typed_params = filtered

    if not typed_params:
        return "Action?"
    type_args = ", ".join(m_type.split("  /*")[0].strip() for m_type, _, _ in typed_params)
    return f"Action<{type_args}>?"


# ---------------------------------------------------------------------------
# Callback and handle builders
# ---------------------------------------------------------------------------

def is_callback_struct(name: str, callback_suffixes: list[str], symbol_prefix: str) -> bool:
    if any(name.endswith(suf) for suf in callback_suffixes):
        return True
    bare = strip_prefix(name, symbol_prefix).lower()
    return "callback" in bare or (bare.rstrip("t_").rstrip("s_").endswith("_cb"))


def build_callback_entry(
    struct_name: str,
    struct_def: dict[str, Any],
    symbol_prefix: str,
    callback_typedefs: dict[str, str],
    idl_enums: set[str],
    opaque_types: dict[str, Any],
) -> dict[str, Any]:
    class_name = infer_class_name(struct_name, symbol_prefix)
    native_struct = infer_native_struct_name(struct_name)

    fields_raw = struct_def.get("fields") or []
    fields_out: list[dict[str, Any]] = []

    for field in fields_raw:
        if not isinstance(field, dict):
            continue
        field_name = str(field.get("name") or "")
        if not field_name or field_name.lower() in ("user_data", "ud", "context", "ctx"):
            continue

        # Try to find the typedef for this field's type from declaration
        decl = str(field.get("declaration") or "")
        # declaration looks like: "lrtc_data_channel_state_cb on_state_change"
        # Extract the typedef name (first token before field_name)
        typedef_name = ""
        if decl:
            parts = decl.split()
            if len(parts) >= 2 and parts[-1] == field_name:
                typedef_name = parts[0]

        typedef_decl = callback_typedefs.get(typedef_name, "")
        managed_name = snake_to_pascal(field_name)

        # Build typed params for managed_type inference
        raw_params = parse_typedef_params(typedef_decl) if typedef_decl else []
        typed: list[tuple[str, str, str]] = []
        for c_type, pname in raw_params:
            m_type, expr = infer_param(c_type, pname, idl_enums, opaque_types, symbol_prefix)
            typed.append((m_type, pname, expr))

        managed_type = build_managed_type_signature(typed, raw_params) if typedef_decl else "Action</* TODO */>?"

        assignment_lines = (
            build_assignment_lines(managed_name, typedef_decl, idl_enums, opaque_types, symbol_prefix)
            if typedef_decl else
            [f"(ud /*, TODO: params */) => {managed_name}?.Invoke(/* TODO: marshal params */)"]
        )

        delegate_field = f"_{managed_name[0].lower()}{managed_name[1:]}Cb" if managed_name else f"_{field_name}Cb"
        delegate_type = snake_to_pascal(typedef_name) if typedef_name else f"{managed_name}Cb"

        fields_out.append({
            "managed_name": managed_name,
            "managed_type": managed_type,
            "delegate_field": delegate_field,
            "delegate_type": delegate_type,
            "native_field": field_name,
            "assignment_lines": assignment_lines,
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
            c_type = str(param.get("c_type") or "")
            bare = re.sub(r"[\s*]|const", "", c_type).strip()
            if bare in opaque_types:
                matched = bare
                break
        (groups[matched] if matched else groups["__global__"]).append(name)
    return groups


# ---------------------------------------------------------------------------
# Main scaffold / update logic
# ---------------------------------------------------------------------------

def scaffold(
    idl: dict[str, Any],
    namespace: str,
    symbol_prefix_override: str | None,
) -> dict[str, Any]:
    sp = get_symbol_prefix(idl, symbol_prefix_override)
    opaque_types = get_opaque_types(idl)
    callback_suffixes = get_callback_suffixes(idl)
    header_structs = get_header_structs(idl)
    callback_typedefs = get_callback_typedefs(idl)
    idl_enums = get_idl_enums(idl)
    functions: list[dict[str, Any]] = idl.get("functions") or []

    # Callback structs
    cb_struct_names = [
        name for name, defn in header_structs.items()
        if is_callback_struct(name, callback_suffixes, sp)
    ]
    callbacks = [
        build_callback_entry(name, header_structs[name], sp, callback_typedefs, idl_enums, opaque_types)
        for name in cb_struct_names
    ]

    # Handle API
    func_groups = group_functions_by_handle(functions, opaque_types)
    handle_api: list[dict[str, Any]] = []
    for handle_type in sorted(opaque_types):
        class_name = infer_class_name(handle_type, sp)
        funcs = sorted(func_groups.get(handle_type) or [])
        members: list[dict[str, Any]] = [
            {"line": f"// TODO: add managed members for {class_name}"},
        ]
        if funcs:
            members.append({"line": "// Native functions for this handle:"})
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
        "required_native_functions": [],
    }


def _update_existing(
    existing: dict[str, Any],
    generated: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    """Merge newly-discovered handles and callbacks into existing source without losing customizations."""
    result = json.loads(json.dumps(existing))  # deep copy
    stats = {"added_callbacks": 0, "added_handles": 0, "kept_callbacks": 0, "kept_handles": 0}

    # Callbacks: merge by class name
    existing_cb_names = {cb["class"] for cb in (result.get("callbacks") or []) if isinstance(cb, dict)}
    new_callbacks = [cb for cb in (generated.get("callbacks") or []) if cb.get("class") not in existing_cb_names]
    if "callbacks" not in result or not isinstance(result.get("callbacks"), list):
        result["callbacks"] = []
    result["callbacks"].extend(new_callbacks)
    stats["added_callbacks"] = len(new_callbacks)
    stats["kept_callbacks"] = len(existing_cb_names)

    # Handle API: merge by class name
    existing_h_names = {h["class"] for h in (result.get("handle_api") or []) if isinstance(h, dict)}
    new_handles = [h for h in (generated.get("handle_api") or []) if h.get("class") not in existing_h_names]
    if "handle_api" not in result or not isinstance(result.get("handle_api"), list):
        result["handle_api"] = []
    result["handle_api"].extend(new_handles)
    stats["added_handles"] = len(new_handles)
    stats["kept_handles"] = len(existing_h_names)

    return result, stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Scaffold managed_api.source.json from IDL JSON.")
    parser.add_argument("--idl", required=True)
    parser.add_argument("--namespace", required=True, help="C# namespace (e.g. MyLib).")
    parser.add_argument("--out", required=True)
    parser.add_argument("--symbol-prefix", default=None)
    parser.add_argument("--force", action="store_true", help="Overwrite existing output.")
    parser.add_argument("--update", action="store_true", help="Merge new handles/callbacks into existing file.")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    idl = load_json_object(Path(args.idl))
    out_path = Path(args.out)
    generated = scaffold(idl, args.namespace, args.symbol_prefix)

    if args.update and out_path.exists():
        existing = load_json_object(out_path)
        merged, stats = _update_existing(existing, generated)
        content = json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        if not args.check and not args.dry_run:
            print(f"scaffold --update: added {stats['added_callbacks']} callbacks, "
                  f"{stats['added_handles']} handles; "
                  f"kept {stats['kept_callbacks']} callbacks, {stats['kept_handles']} handles")
    elif out_path.exists() and not args.force and not args.check and not args.dry_run:
        print(f"scaffold: '{out_path}' exists. Use --force to overwrite or --update to merge.")
        return 0
    else:
        content = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"

    return write_if_changed(out_path, content, args.check, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
