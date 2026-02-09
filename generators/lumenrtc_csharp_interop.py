#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import json
import re
from pathlib import Path
from typing import Any

PRIMITIVE_TYPE_MAP = {
    "void": "void",
    "bool": "bool",
    "char": "byte",
    "signed char": "sbyte",
    "unsigned char": "byte",
    "short": "short",
    "unsigned short": "ushort",
    "int": "int",
    "unsigned int": "uint",
    "long": "nint",
    "unsigned long": "nuint",
    "long long": "long",
    "unsigned long long": "ulong",
    "int8_t": "sbyte",
    "uint8_t": "byte",
    "int16_t": "short",
    "uint16_t": "ushort",
    "int32_t": "int",
    "uint32_t": "uint",
    "int64_t": "long",
    "uint64_t": "ulong",
    "size_t": "nuint",
    "ssize_t": "nint",
    "float": "float",
    "double": "double",
}


def load_idl(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"IDL '{path}' must be a JSON object")
    return data


def normalize_c_type(value: str) -> str:
    text = " ".join(value.replace("\t", " ").split())
    text = re.sub(r"\s*\*\s*", "*", text)
    return text.strip()


def strip_qualifiers(value: str) -> str:
    text = normalize_c_type(value)
    text = re.sub(r"\b(const|volatile|restrict)\b", " ", text)
    text = re.sub(r"\b(struct|enum)\s+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def to_managed_type_name(c_identifier: str, strip_typedef_suffix: bool = True) -> str:
    value = c_identifier
    if strip_typedef_suffix and value.endswith("_t"):
        value = value[:-2]
    parts = [p for p in value.split("_") if p]
    if not parts:
        return "IntPtr"
    return "".join(p[:1].upper() + p[1:] for p in parts)


def map_managed_base_type(c_type: str, enum_names: set[str], struct_names: set[str]) -> str:
    stripped = strip_qualifiers(c_type)
    if stripped in PRIMITIVE_TYPE_MAP:
        return PRIMITIVE_TYPE_MAP[stripped]
    if stripped in enum_names or stripped in struct_names:
        return to_managed_type_name(stripped, strip_typedef_suffix=True)
    if stripped.endswith("_cb"):
        return to_managed_type_name(stripped, strip_typedef_suffix=False)
    if stripped.endswith("_t"):
        return to_managed_type_name(stripped, strip_typedef_suffix=True)
    return "IntPtr"


def map_field_type(c_type: str, enum_names: set[str], struct_names: set[str]) -> str:
    stripped = strip_qualifiers(c_type)
    if "*" in stripped:
        return "IntPtr"
    return map_managed_base_type(stripped, enum_names, struct_names)


def parse_callback_typedefs(callback_typedefs: list[Any]) -> dict[str, dict[str, Any]]:
    typedefs: dict[str, dict[str, Any]] = {}
    pattern = re.compile(
        r"^typedef\s+(?P<ret>.+?)\s*\(\s*LUMENRTC_CALL\s*\*\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\((?P<params>.*)\)\s*;?\s*$"
    )
    for item in callback_typedefs:
        decl = item.get("declaration") if isinstance(item, dict) else item
        if not isinstance(decl, str):
            continue
        match = pattern.match(decl.strip())
        if not match:
            continue
        name = match.group("name")
        typedefs[name] = {
            "return_type": normalize_c_type(match.group("ret")),
            "parameters": parse_param_list(match.group("params")),
        }
    return typedefs


def parse_param_list(text: str) -> list[dict[str, str]]:
    raw = text.strip()
    if not raw or raw == "void":
        return []
    parts: list[str] = []
    token: list[str] = []
    depth = 0
    for ch in raw:
        if ch == "," and depth == 0:
            piece = "".join(token).strip()
            if piece:
                parts.append(piece)
            token = []
            continue
        token.append(ch)
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
    tail = "".join(token).strip()
    if tail:
        parts.append(tail)

    params: list[dict[str, str]] = []
    for idx, part in enumerate(parts):
        part = normalize_c_type(part)
        part = re.sub(r"\*([A-Za-z_])", r"* \1", part)
        if part == "...":
            params.append({"name": f"arg{idx}", "c_type": "..."})
            continue
        fn_ptr = re.search(r"\(\s*\*\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)", part)
        if fn_ptr:
            name = fn_ptr.group("name")
            c_type = normalize_c_type(part.replace(name, "", 1))
            params.append({"name": name, "c_type": c_type})
            continue
        array_decl = re.match(r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<array>(?:\[[^\]]*\])+)$", part)
        if array_decl:
            left = normalize_c_type(array_decl.group("left"))
            params.append({"name": array_decl.group("name"), "c_type": f"{left}*"})
            continue
        regular = re.match(r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)$", part)
        if regular:
            params.append({"name": regular.group("name"), "c_type": normalize_c_type(regular.group("left"))})
            continue
        params.append({"name": f"arg{idx}", "c_type": part})
    return params


def signature_key(ret_type: str, params: list[dict[str, str]]) -> tuple:
    return (
        normalize_c_type(ret_type),
        tuple(normalize_c_type(p.get("c_type", "")) for p in params),
    )


def parse_function_pointer_field(decl: str) -> dict[str, Any] | None:
    match = re.search(r"^(?P<ret>.+?)\(\s*\*\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\((?P<params>.*)\)$", decl.strip())
    if not match:
        return None
    return {
        "name": match.group("name"),
        "return_type": normalize_c_type(match.group("ret")),
        "parameters": parse_param_list(match.group("params")),
    }


def render_enum(name: str, payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    managed = to_managed_type_name(name, strip_typedef_suffix=True)
    members = payload.get("members", [])
    raw_names = [str(m.get("name") or "") for m in members if isinstance(m, dict)]
    common_prefix = ""
    if raw_names:
        prefix = raw_names[0]
        for item in raw_names[1:]:
            i = 0
            while i < len(prefix) and i < len(item) and prefix[i] == item[i]:
                i += 1
            prefix = prefix[:i]
            if not prefix:
                break
        if "_" in prefix:
            common_prefix = prefix[: prefix.rfind("_") + 1]
        else:
            common_prefix = prefix
    lines.append(f"internal enum {managed}")
    lines.append("{")
    for member in members:
        member_name = str(member.get("name") or "")
        if not member_name:
            continue
        trimmed = member_name
        if common_prefix and trimmed.startswith(common_prefix):
            trimmed = trimmed[len(common_prefix):]
        trimmed = trimmed.strip("_")
        managed_member = to_managed_type_name(trimmed.lower(), strip_typedef_suffix=False)
        value = member.get("value")
        if value is None:
            lines.append(f"    {managed_member},")
        else:
            lines.append(f"    {managed_member} = {value},")
    lines.append("}")
    lines.append("")
    return lines


def render_constant(name: str, value: str) -> list[str]:
    trimmed = name
    if trimmed.startswith("LRTC_"):
        trimmed = trimmed[5:]
    managed = to_managed_type_name(trimmed.lower(), strip_typedef_suffix=False)
    lines = [f"    public const int {managed} = {value};"]
    return lines


def render_struct(
    name: str,
    payload: dict[str, Any],
    enum_names: set[str],
    struct_names: set[str],
    callback_field_types: dict[str, str],
    struct_field_overrides: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    managed = to_managed_type_name(name, strip_typedef_suffix=True)
    pack = 8 if name == "lrtc_rtc_config_t" else None
    if pack is None:
        lines.append("[StructLayout(LayoutKind.Sequential)]")
    else:
        lines.append("[StructLayout(LayoutKind.Sequential, Pack = 8)]")
    lines.append(f"internal struct {managed}")
    lines.append("{")
    for field in payload.get("fields", []):
        decl = str(field.get("declaration") or "").strip()
        field_name = str(field.get("name") or "").strip()
        if not decl or not field_name:
            continue
        if field_name in callback_field_types:
            delegate_type = callback_field_types[field_name]
            lines.append(f"    public {delegate_type}? {field_name};")
            lines.append("")
            continue
        array_match = re.match(r"^(?P<type>.+?)\s+" + re.escape(field_name) + r"\s*\[(?P<len>\d+)\]$", decl)
        if array_match:
            c_type = normalize_c_type(array_match.group("type"))
            length = array_match.group("len")
            managed_type = map_managed_base_type(c_type, enum_names, struct_names)
            lines.append(f"    [MarshalAs(UnmanagedType.ByValArray, SizeConst = {length})]")
            lines.append(f"    public {managed_type}[] {field_name};")
            lines.append("")
            continue
        c_type = normalize_c_type(decl[: -len(field_name)].strip())
        override_key = f"{name}.{field_name}"
        override_type = struct_field_overrides.get(override_key)
        field_type = override_type or map_field_type(c_type, enum_names, struct_names)
        is_callback = strip_qualifiers(c_type).endswith("_cb")
        if is_callback:
            field_type += "?"
        if strip_qualifiers(c_type) == "bool":
            lines.append("    [MarshalAs(UnmanagedType.I1)]")
        lines.append(f"    public {field_type} {field_name};")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    lines.append("}")
    lines.append("")
    return lines


def render_delegate(managed_name: str, info: dict[str, Any], enum_names: set[str], struct_names: set[str]) -> list[str]:
    lines: list[str] = []
    ret = map_managed_base_type(info.get("return_type", "void"), enum_names, struct_names)
    params: list[str] = []
    for idx, param in enumerate(info.get("parameters", [])):
        c_type = param.get("c_type", "void")
        if c_type == "...":
            param_type = "IntPtr"
        else:
            base = strip_qualifiers(c_type)
            if "*" in base:
                param_type = "IntPtr"
            else:
                param_type = map_managed_base_type(base, enum_names, struct_names)
        name = param.get("name") or f"arg{idx}"
        params.append(f"{param_type} {name}")
    lines.append("[UnmanagedFunctionPointer(CallingConvention.Cdecl)]")
    lines.append(f"internal delegate {ret} {managed_name}({', '.join(params)});")
    lines.append("")
    return lines


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idl", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    idl = load_idl(Path(args.idl))
    header_types = idl.get("header_types") or {}
    enums = header_types.get("enums") or {}
    structs = header_types.get("structs") or {}
    callback_typedefs = header_types.get("callback_typedefs") or []
    constants = header_types.get("constants") or {}

    enum_names = set(enums.keys())
    struct_names = set(structs.keys())

    interop = (idl.get("bindings") or {}).get("interop") or {}
    callback_overrides = interop.get("callback_field_overrides") or {}
    struct_field_overrides = interop.get("struct_field_overrides") or {}

    typedefs = parse_callback_typedefs(callback_typedefs)
    signature_to_typedef: dict[tuple, str] = {}
    typedef_managed_names: dict[str, str] = {}
    for name, info in typedefs.items():
        managed_name = to_managed_type_name(name, strip_typedef_suffix=False)
        typedef_managed_names[name] = managed_name
        signature_to_typedef[signature_key(info["return_type"], info["parameters"])] = managed_name

    inline_callbacks: dict[str, dict[str, Any]] = {}
    callback_structs: dict[str, dict[str, Any]] = {}
    non_callback_structs: dict[str, dict[str, Any]] = {}

    for name, payload in structs.items():
        if name.endswith("_callbacks_t"):
            callback_structs[name] = payload
            for field in payload.get("fields", []):
                decl = str(field.get("declaration") or "").strip()
                fp = parse_function_pointer_field(decl)
                if not fp:
                    continue
                inline_callbacks[fp["name"]] = fp
        else:
            non_callback_structs[name] = payload

    delegates: dict[str, dict[str, Any]] = {}
    for name, info in typedefs.items():
        delegates[typedef_managed_names[name]] = info

    callback_field_types: dict[str, str] = {}

    for field_name, info in inline_callbacks.items():
        override_name = callback_overrides.get(field_name)
        signature = signature_key(info["return_type"], info["parameters"])
        typedef_name = signature_to_typedef.get(signature)
        delegate_name = override_name or typedef_name
        if not delegate_name:
            generated = to_managed_type_name(field_name, strip_typedef_suffix=False) + "Cb"
            delegate_name = generated if generated.startswith("Lrtc") else "Lrtc" + generated
        callback_field_types[field_name] = delegate_name
        if delegate_name not in delegates:
            delegates[delegate_name] = info

    lines: list[str] = []
    lines.append("// <auto-generated />")
    lines.append("// Generated by tools/abi_framework/generators/lumenrtc_csharp_interop.py")
    lines.append("using System;")
    lines.append("using System.Runtime.InteropServices;")
    lines.append("")
    lines.append("namespace LumenRTC.Interop;")
    lines.append("")

    # Enums
    for name in sorted(enums.keys()):
        lines.extend(render_enum(name, enums[name]))

    # Constants
    if constants:
        lines.append("internal static class LrtcConstants")
        lines.append("{")
        for const_name in sorted(constants.keys()):
            lines.extend(render_constant(const_name, str(constants[const_name])))
        lines.append("}")
        lines.append("")

    # Structs (non-callback)
    for name in sorted(non_callback_structs.keys()):
        lines.extend(render_struct(name, non_callback_structs[name], enum_names, struct_names, {}, struct_field_overrides))

    # Delegates
    for name in sorted(delegates.keys()):
        lines.extend(render_delegate(name, delegates[name], enum_names, struct_names))

    # Callback structs
    for name in sorted(callback_structs.keys()):
        lines.extend(render_struct(name, callback_structs[name], enum_names, struct_names, callback_field_types, struct_field_overrides))

    content = "\n".join(lines)
    if not content.endswith("\n"):
        content += "\n"

    return write_if_changed(Path(args.out), content, args.check, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
