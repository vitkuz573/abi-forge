#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import glob
import hashlib
import json
import tempfile
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_VERSION = "3.0.0"


class AbiFrameworkError(Exception):
    pass


@dataclass(frozen=True)
class AbiVersion:
    major: int
    minor: int
    patch: int

    def as_tuple(self) -> tuple[int, int, int]:
        return (self.major, self.minor, self.patch)

    def as_dict(self) -> dict[str, int]:
        return {
            "major": self.major,
            "minor": self.minor,
            "patch": self.patch,
        }


@dataclass(frozen=True)
class TypePolicy:
    enable_enums: bool
    enable_structs: bool
    enum_name_pattern: str
    struct_name_pattern: str
    ignore_enums: tuple[str, ...]
    ignore_structs: tuple[str, ...]
    struct_tail_addition_is_breaking: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "enable_enums": self.enable_enums,
            "enable_structs": self.enable_structs,
            "enum_name_pattern": self.enum_name_pattern,
            "struct_name_pattern": self.struct_name_pattern,
            "ignore_enums": list(self.ignore_enums),
            "ignore_structs": list(self.ignore_structs),
            "struct_tail_addition_is_breaking": self.struct_tail_addition_is_breaking,
        }


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_c_comments(content: str) -> str:
    content = re.sub(r"/\*.*?\*/", "", content, flags=re.S)
    content = re.sub(r"//.*?$", "", content, flags=re.M)
    return content


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AbiFrameworkError(f"Unable to read JSON file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise AbiFrameworkError(f"Invalid JSON in '{path}': {exc}") from exc


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def get_schema_path(kind: str) -> Path:
    base = Path(__file__).resolve().parent / "schemas"
    mapping = {
        "config": base / "config.schema.json",
        "snapshot": base / "snapshot.schema.json",
        "report": base / "report.schema.json",
    }
    if kind not in mapping:
        raise AbiFrameworkError(f"Unknown schema kind: {kind}")
    return mapping[kind]


def validate_with_jsonschema_if_available(kind: str, payload: dict[str, Any]) -> tuple[bool, str | None]:
    schema_path = get_schema_path(kind)
    if not schema_path.exists():
        return False, f"schema file not found: {schema_path}"

    try:
        import jsonschema  # type: ignore
    except Exception:
        return False, "jsonschema package is not installed"

    schema_payload = load_json(schema_path)
    try:
        jsonschema.validate(payload, schema_payload)
    except Exception as exc:
        raise AbiFrameworkError(f"{kind} failed JSON schema validation: {exc}") from exc
    return True, None


def require_keys(obj: dict[str, Any], keys: list[str], label: str) -> None:
    missing = [key for key in keys if key not in obj]
    if missing:
        raise AbiFrameworkError(f"{label} is missing required keys: {', '.join(missing)}")


def validate_config_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise AbiFrameworkError("config root must be an object")
    root_policy = payload.get("policy")
    if root_policy is not None and not isinstance(root_policy, dict):
        raise AbiFrameworkError("config.policy must be an object when specified")
    if isinstance(root_policy, dict):
        classification = root_policy.get("max_allowed_classification")
        if classification is not None and classification not in {"none", "additive", "breaking"}:
            raise AbiFrameworkError("config.policy.max_allowed_classification must be none/additive/breaking")
        for bool_key in ["fail_on_warnings", "require_layout_probe"]:
            value = root_policy.get(bool_key)
            if value is not None and not isinstance(value, bool):
                raise AbiFrameworkError(f"config.policy.{bool_key} must be boolean when specified")
    targets = payload.get("targets")
    if not isinstance(targets, dict) or not targets:
        raise AbiFrameworkError("config must define non-empty 'targets' object")
    for target_name, target in targets.items():
        if not isinstance(target_name, str) or not target_name:
            raise AbiFrameworkError("config target names must be non-empty strings")
        if not isinstance(target, dict):
            raise AbiFrameworkError(f"target '{target_name}' must be an object")
        require_keys(target, ["header"], f"target '{target_name}'")
        header = target.get("header")
        if not isinstance(header, dict):
            raise AbiFrameworkError(f"target '{target_name}'.header must be an object")
        require_keys(
            header,
            ["path", "api_macro", "call_macro", "symbol_prefix", "version_macros"],
            f"target '{target_name}'.header",
        )
        version_macros = header.get("version_macros")
        if not isinstance(version_macros, dict):
            raise AbiFrameworkError(f"target '{target_name}'.header.version_macros must be an object")
        require_keys(version_macros, ["major", "minor", "patch"], f"target '{target_name}'.header.version_macros")
        bindings = target.get("bindings")
        if bindings is not None:
            if not isinstance(bindings, dict):
                raise AbiFrameworkError(f"target '{target_name}'.bindings must be an object when specified")
            expected_symbols = bindings.get("expected_symbols")
            if expected_symbols is not None:
                if not isinstance(expected_symbols, list):
                    raise AbiFrameworkError(f"target '{target_name}'.bindings.expected_symbols must be an array")
                for idx, symbol in enumerate(expected_symbols):
                    if not isinstance(symbol, str) or not symbol:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.bindings.expected_symbols[{idx}] must be a non-empty string"
                        )

        target_policy = target.get("policy")
        if target_policy is not None:
            if not isinstance(target_policy, dict):
                raise AbiFrameworkError(f"target '{target_name}'.policy must be an object when specified")
            classification = target_policy.get("max_allowed_classification")
            if classification is not None and classification not in {"none", "additive", "breaking"}:
                raise AbiFrameworkError(
                    f"target '{target_name}'.policy.max_allowed_classification must be none/additive/breaking"
                )
            for bool_key in ["fail_on_warnings", "require_layout_probe"]:
                value = target_policy.get(bool_key)
                if value is not None and not isinstance(value, bool):
                    raise AbiFrameworkError(f"target '{target_name}'.policy.{bool_key} must be boolean when specified")

        codegen = target.get("codegen")
        if codegen is not None:
            if not isinstance(codegen, dict):
                raise AbiFrameworkError(f"target '{target_name}'.codegen must be an object when specified")
            string_fields = ["idl_output_path"]
            for field_name in string_fields:
                value = codegen.get(field_name)
                if value is not None and (not isinstance(value, str) or not value):
                    raise AbiFrameworkError(
                        f"target '{target_name}'.codegen.{field_name} must be a non-empty string when specified"
                    )
            bool_fields = ["enabled"]
            for field_name in bool_fields:
                value = codegen.get(field_name)
                if value is not None and not isinstance(value, bool):
                    raise AbiFrameworkError(f"target '{target_name}'.codegen.{field_name} must be a boolean when specified")
            include_symbols = codegen.get("include_symbols")
            if include_symbols is not None:
                if not isinstance(include_symbols, list):
                    raise AbiFrameworkError(f"target '{target_name}'.codegen.include_symbols must be an array")
                for idx, item in enumerate(include_symbols):
                    if not isinstance(item, str) or not item:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.codegen.include_symbols[{idx}] must be a non-empty string"
                        )
            exclude_symbols = codegen.get("exclude_symbols")
            if exclude_symbols is not None:
                if not isinstance(exclude_symbols, list):
                    raise AbiFrameworkError(f"target '{target_name}'.codegen.exclude_symbols must be an array")
                for idx, item in enumerate(exclude_symbols):
                    if not isinstance(item, str) or not item:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.codegen.exclude_symbols[{idx}] must be a non-empty string"
                        )
            regex_lists = ["include_symbols_regex", "exclude_symbols_regex"]
            for list_name in regex_lists:
                regex_items = codegen.get(list_name)
                if regex_items is None:
                    continue
                if not isinstance(regex_items, list):
                    raise AbiFrameworkError(f"target '{target_name}'.codegen.{list_name} must be an array")
                for idx, item in enumerate(regex_items):
                    if not isinstance(item, str) or not item:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.codegen.{list_name}[{idx}] must be a non-empty string"
                        )

    validate_with_jsonschema_if_available("config", payload)


def validate_snapshot_payload(payload: dict[str, Any], label: str) -> None:
    if not isinstance(payload, dict):
        raise AbiFrameworkError(f"{label} root must be an object")
    require_keys(payload, ["tool", "target", "abi_version", "header", "bindings", "binary"], label)
    header = payload.get("header")
    if not isinstance(header, dict):
        raise AbiFrameworkError(f"{label} must contain object section 'header'")
    require_keys(header, ["symbols", "functions"], f"{label}.header")
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        raise AbiFrameworkError(f"{label}.bindings must be an object")
    symbols = bindings.get("symbols")
    if symbols is not None and not isinstance(symbols, list):
        raise AbiFrameworkError(f"{label}.bindings.symbols must be an array when specified")
    validate_with_jsonschema_if_available("snapshot", payload)


def validate_report_payload(payload: dict[str, Any], label: str) -> None:
    if not isinstance(payload, dict):
        raise AbiFrameworkError(f"{label} root must be an object")
    require_keys(payload, ["status", "change_classification", "required_bump", "errors", "warnings"], label)
    validate_with_jsonschema_if_available("report", payload)


def load_config(path: Path) -> dict[str, Any]:
    config = load_json(path)
    validate_config_payload(config)
    return config


def resolve_target(config: dict[str, Any], target_name: str) -> dict[str, Any]:
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config is missing required object: 'targets'.")
    target = targets.get(target_name)
    if not isinstance(target, dict):
        known = ", ".join(sorted(targets.keys()))
        raise AbiFrameworkError(f"Unknown target '{target_name}'. Known targets: {known or '<none>'}")
    return target


def ensure_relative_path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path


def to_repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root.resolve()))
    except ValueError:
        return str(path.resolve())


def iter_files_from_entries(root: Path, entries: list[str], suffix: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()

    for entry in entries:
        expanded: list[Path] = []
        entry_path = ensure_relative_path(root, entry)

        if any(ch in entry for ch in "*?[]"):
            for match in glob.glob(str(entry_path), recursive=True):
                expanded.append(Path(match))
        elif entry_path.is_dir():
            expanded.extend(entry_path.rglob(f"*{suffix}"))
        elif entry_path.is_file():
            expanded.append(entry_path)

        for candidate in expanded:
            if not candidate.is_file() or candidate.suffix.lower() != suffix:
                continue
            if "bin" in candidate.parts or "obj" in candidate.parts:
                continue
            resolved = candidate.resolve()
            if resolved not in seen:
                seen.add(resolved)
                paths.append(resolved)

    return sorted(paths)


def extract_define_int(content: str, macro_name: str) -> int:
    match = re.search(rf"^\s*#\s*define\s+{re.escape(macro_name)}\s+([0-9]+)\b", content, flags=re.M)
    if not match:
        raise AbiFrameworkError(f"Required macro '{macro_name}' was not found.")
    return int(match.group(1))


def normalize_identifier_list(value: Any, key: str) -> tuple[str, ...]:
    if value is None:
        return tuple()
    if not isinstance(value, list):
        raise AbiFrameworkError(f"Target field '{key}' must be an array when specified.")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise AbiFrameworkError(f"Target field '{key}[{idx}]' must be a non-empty string.")
        out.append(item)
    return tuple(out)


def build_type_policy(header_cfg: dict[str, Any], symbol_prefix: str) -> TypePolicy:
    raw_policy = header_cfg.get("types")
    if raw_policy is None:
        raw_policy = {}
    if not isinstance(raw_policy, dict):
        raise AbiFrameworkError("Target field 'header.types' must be an object when specified.")

    default_pattern = f"^{re.escape(symbol_prefix)}"

    enable_enums = bool(raw_policy.get("enable_enums", True))
    enable_structs = bool(raw_policy.get("enable_structs", True))
    enum_name_pattern = str(raw_policy.get("enum_name_pattern", default_pattern))
    struct_name_pattern = str(raw_policy.get("struct_name_pattern", default_pattern))

    ignore_enums = normalize_identifier_list(raw_policy.get("ignore_enums"), "header.types.ignore_enums")
    ignore_structs = normalize_identifier_list(raw_policy.get("ignore_structs"), "header.types.ignore_structs")

    struct_tail_addition_is_breaking = bool(raw_policy.get("struct_tail_addition_is_breaking", True))

    try:
        re.compile(enum_name_pattern)
    except re.error as exc:
        raise AbiFrameworkError(f"Invalid regex in header.types.enum_name_pattern: {exc}") from exc

    try:
        re.compile(struct_name_pattern)
    except re.error as exc:
        raise AbiFrameworkError(f"Invalid regex in header.types.struct_name_pattern: {exc}") from exc

    return TypePolicy(
        enable_enums=enable_enums,
        enable_structs=enable_structs,
        enum_name_pattern=enum_name_pattern,
        struct_name_pattern=struct_name_pattern,
        ignore_enums=ignore_enums,
        ignore_structs=ignore_structs,
        struct_tail_addition_is_breaking=struct_tail_addition_is_breaking,
    )


def stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sanitize_c_int_expr(expr: str) -> str:
    compact = normalize_ws(expr)
    compact = re.sub(r"\b(0[xX][0-9A-Fa-f]+)([uUlL]+)\b", r"\1", compact)
    compact = re.sub(r"\b([0-9]+)([uUlL]+)\b", r"\1", compact)
    return compact


def eval_c_int_expr(expr: str) -> int | None:
    sanitized = sanitize_c_int_expr(expr)
    try:
        tree = ast.parse(sanitized, mode="eval")
    except SyntaxError:
        return None

    def _eval(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, int):
                return int(node.value)
            raise ValueError("non-int literal")
        if isinstance(node, ast.UnaryOp):
            value = _eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.Invert):
                return ~value
            raise ValueError("unsupported unary op")
        if isinstance(node, ast.BinOp):
            left = _eval(node.left)
            right = _eval(node.right)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.FloorDiv) or isinstance(node.op, ast.Div):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.LShift):
                return left << right
            if isinstance(node.op, ast.RShift):
                return left >> right
            if isinstance(node.op, ast.BitOr):
                return left | right
            if isinstance(node.op, ast.BitAnd):
                return left & right
            if isinstance(node.op, ast.BitXor):
                return left ^ right
            raise ValueError("unsupported binary op")
        raise ValueError("unsupported expression")

    try:
        return _eval(tree)
    except Exception:
        return None


def parse_enum_blocks(content: str, policy: TypePolicy) -> dict[str, Any]:
    if not policy.enable_enums:
        return {}

    enum_pattern = re.compile(
        r"typedef\s+enum(?:\s+[A-Za-z_][A-Za-z0-9_]*)?\s*\{(?P<body>.*?)\}\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;",
        flags=re.S,
    )
    name_re = re.compile(policy.enum_name_pattern)

    enums: dict[str, Any] = {}

    for match in enum_pattern.finditer(content):
        enum_name = match.group("name")
        if enum_name in policy.ignore_enums:
            continue
        if not name_re.search(enum_name):
            continue

        body = match.group("body")
        raw_items = [normalize_ws(item) for item in body.split(",")]

        members: list[dict[str, Any]] = []
        last_value: int | None = None
        next_from_last = True

        for raw_item in raw_items:
            item = raw_item.strip()
            if not item:
                continue

            m = re.match(r"^(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\s*=\s*(?P<expr>.+))?$", item)
            if not m:
                continue

            member_name = m.group("name")
            expr = m.group("expr")

            value_expr: str | None = None
            value: int | None
            if expr is None:
                if next_from_last and last_value is not None:
                    value = last_value + 1
                elif not members:
                    value = 0
                else:
                    value = None
                value_expr = None
            else:
                value_expr = sanitize_c_int_expr(expr)
                value = eval_c_int_expr(value_expr)

            if value is not None:
                last_value = value
                next_from_last = True
            else:
                next_from_last = False

            members.append(
                {
                    "name": member_name,
                    "value": value,
                    "value_expr": value_expr,
                }
            )

        enums[enum_name] = {
            "member_count": len(members),
            "members": members,
            "fingerprint": stable_hash(members),
        }

    return {name: enums[name] for name in sorted(enums.keys())}


def split_struct_declarations(body: str) -> list[str]:
    declarations: list[str] = []
    buffer = ""

    for line in body.splitlines():
        stripped = normalize_ws(line)
        if not stripped or stripped.startswith("#"):
            continue

        buffer = f"{buffer} {stripped}".strip() if buffer else stripped

        while ";" in buffer:
            before, after = buffer.split(";", 1)
            decl = normalize_ws(before)
            if decl:
                declarations.append(decl)
            buffer = normalize_ws(after)

    return declarations


def parse_struct_field(decl: str, index: int) -> dict[str, str]:
    function_ptr = re.search(r"\(\s*\*\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)\s*\(", decl)
    if function_ptr:
        name = function_ptr.group("name")
        return {
            "name": name,
            "declaration": normalize_ws(decl),
        }

    bitfield = re.match(r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?P<bits>.+)$", decl)
    if bitfield:
        return {
            "name": bitfield.group("name"),
            "declaration": normalize_ws(decl),
        }

    array_field = re.match(
        r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<array>(?:\s*\[[^\]]+\])+)\s*$",
        decl,
    )
    if array_field:
        return {
            "name": array_field.group("name"),
            "declaration": normalize_ws(decl),
        }

    regular = re.match(r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$", decl)
    if regular:
        return {
            "name": regular.group("name"),
            "declaration": normalize_ws(decl),
        }

    return {
        "name": f"__unnamed_{index}",
        "declaration": normalize_ws(decl),
    }


def parse_struct_blocks(content: str, policy: TypePolicy) -> dict[str, Any]:
    if not policy.enable_structs:
        return {}

    struct_pattern = re.compile(
        r"typedef\s+struct(?:\s+[A-Za-z_][A-Za-z0-9_]*)?\s*\{(?P<body>.*?)\}\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;",
        flags=re.S,
    )
    name_re = re.compile(policy.struct_name_pattern)

    structs: dict[str, Any] = {}

    for match in struct_pattern.finditer(content):
        struct_name = match.group("name")
        if struct_name in policy.ignore_structs:
            continue
        if not name_re.search(struct_name):
            continue

        body = match.group("body")
        declarations = split_struct_declarations(body)
        fields = [parse_struct_field(decl, idx) for idx, decl in enumerate(declarations)]

        structs[struct_name] = {
            "field_count": len(fields),
            "fields": fields,
            "fingerprint": stable_hash(fields),
        }

    return {name: structs[name] for name in sorted(structs.keys())}


def parse_c_header(
    header_path: Path,
    api_macro: str,
    call_macro: str,
    symbol_prefix: str,
    version_macros: dict[str, str],
    type_policy: TypePolicy,
) -> tuple[dict[str, Any], AbiVersion]:
    try:
        raw = header_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AbiFrameworkError(f"Unable to read header '{header_path}': {exc}") from exc

    content = strip_c_comments(raw)
    declaration_content = re.sub(r"^\s*#.*?$", "", content, flags=re.M)
    pattern = re.compile(
        rf"{re.escape(api_macro)}\s+(?P<ret>.*?)\s+{re.escape(call_macro)}\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>.*?)\)\s*;",
        flags=re.S,
    )

    functions: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(declaration_content):
        name = match.group("name")
        if symbol_prefix and not name.startswith(symbol_prefix):
            continue
        return_type = normalize_ws(match.group("ret"))
        params = normalize_ws(match.group("params"))
        signature = f"{return_type} ({params})"
        functions[name] = {
            "return_type": return_type,
            "parameters": params,
            "signature": signature,
        }

    if not functions:
        raise AbiFrameworkError(
            f"No ABI functions were found in '{header_path}' with macros '{api_macro}'/'{call_macro}'."
        )

    major = extract_define_int(content, version_macros["major"])
    minor = extract_define_int(content, version_macros["minor"])
    patch = extract_define_int(content, version_macros["patch"])

    enums = parse_enum_blocks(content=declaration_content, policy=type_policy)
    structs = parse_struct_blocks(content=declaration_content, policy=type_policy)

    header_payload = {
        "path": str(header_path),
        "function_count": len(functions),
        "symbols": sorted(functions.keys()),
        "functions": {name: functions[name] for name in sorted(functions.keys())},
        "enum_count": len(enums),
        "enums": enums,
        "struct_count": len(structs),
        "structs": structs,
    }
    return header_payload, AbiVersion(major=major, minor=minor, patch=patch)


def normalize_c_type(value: str) -> str:
    text = normalize_ws(value)
    text = re.sub(r"\s*\*\s*", "*", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_c_parameters(parameters: str) -> list[str]:
    raw = parameters.strip()
    if not raw or raw == "void":
        return []

    parts: list[str] = []
    token: list[str] = []
    depth = 0

    for ch in raw:
        if ch == "," and depth == 0:
            piece = normalize_ws("".join(token))
            if piece:
                parts.append(piece)
            token = []
            continue

        token.append(ch)
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)

    tail = normalize_ws("".join(token))
    if tail:
        parts.append(tail)

    return parts


def parse_c_parameter_decl(declaration: str, index: int) -> dict[str, Any]:
    decl = normalize_ws(declaration)
    if decl == "...":
        return {
            "name": f"arg{index}",
            "c_type": "...",
            "raw": decl,
            "variadic": True,
        }

    function_ptr = re.search(r"\(\s*\*\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\)", decl)
    if function_ptr:
        name = function_ptr.group("name")
        c_type = normalize_c_type(decl.replace(name, "", 1))
        return {
            "name": name,
            "c_type": c_type,
            "raw": decl,
            "variadic": False,
        }

    array_decl = re.match(
        r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<array>(?:\[[^\]]*\])+)\s*$",
        decl,
    )
    if array_decl:
        left = normalize_c_type(array_decl.group("left"))
        c_type = normalize_c_type(f"{left}*")
        return {
            "name": array_decl.group("name"),
            "c_type": c_type,
            "raw": decl,
            "variadic": False,
        }

    regular = re.match(r"^(?P<left>.+?)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*$", decl)
    if regular:
        return {
            "name": regular.group("name"),
            "c_type": normalize_c_type(regular.group("left")),
            "raw": decl,
            "variadic": False,
        }

    return {
        "name": f"arg{index}",
        "c_type": normalize_c_type(decl),
        "raw": decl,
        "variadic": False,
    }


def parse_c_function_parameters(parameters: str) -> list[dict[str, Any]]:
    chunks = split_c_parameters(parameters)
    parsed = [parse_c_parameter_decl(chunk, idx) for idx, chunk in enumerate(chunks)]
    return parsed


def normalize_regex_list(value: Any, key: str) -> list[re.Pattern[str]]:
    patterns = normalize_string_list(value, key)
    out: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            out.append(re.compile(pattern))
        except re.error as exc:
            raise AbiFrameworkError(f"Invalid regex in '{key}': {pattern} ({exc})") from exc
    return out


def resolve_codegen_config(target: dict[str, Any], target_name: str, repo_root: Path) -> dict[str, Any]:
    raw = target.get("codegen")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AbiFrameworkError(f"target '{target_name}'.codegen must be an object when specified.")

    include_patterns = normalize_regex_list(raw.get("include_symbols_regex"), "codegen.include_symbols_regex")
    exclude_patterns = normalize_regex_list(raw.get("exclude_symbols_regex"), "codegen.exclude_symbols_regex")

    include_symbols = set(normalize_string_list(raw.get("include_symbols"), "codegen.include_symbols"))
    exclude_symbols = set(normalize_string_list(raw.get("exclude_symbols"), "codegen.exclude_symbols"))

    idl_output_value = raw.get("idl_output_path")
    idl_output_path = None
    if isinstance(idl_output_value, str) and idl_output_value:
        idl_output_path = ensure_relative_path(repo_root, idl_output_value).resolve()

    return {
        "enabled": bool(raw.get("enabled", True)),
        "idl_output_path": idl_output_path,
        "include_symbols": include_symbols,
        "exclude_symbols": exclude_symbols,
        "include_patterns": include_patterns,
        "exclude_patterns": exclude_patterns,
    }


def include_symbol_for_codegen(symbol: str, config: dict[str, Any]) -> bool:
    include_symbols = config.get("include_symbols", set())
    if isinstance(include_symbols, set) and include_symbols:
        if symbol not in include_symbols:
            return False

    exclude_symbols = config.get("exclude_symbols", set())
    if isinstance(exclude_symbols, set) and symbol in exclude_symbols:
        return False

    include_patterns = config.get("include_patterns", [])
    if isinstance(include_patterns, list) and include_patterns:
        if not any(pattern.search(symbol) for pattern in include_patterns if isinstance(pattern, re.Pattern)):
            return False

    exclude_patterns = config.get("exclude_patterns", [])
    if isinstance(exclude_patterns, list):
        if any(pattern.search(symbol) for pattern in exclude_patterns if isinstance(pattern, re.Pattern)):
            return False

    return True


def build_function_idl_records(
    target_name: str,
    snapshot: dict[str, Any],
    codegen_cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    header = snapshot.get("header")
    if not isinstance(header, dict):
        raise AbiFrameworkError(f"Snapshot for target '{target_name}' is missing header section.")

    functions_obj = header.get("functions")
    if not isinstance(functions_obj, dict):
        raise AbiFrameworkError(f"Snapshot for target '{target_name}' is missing header.functions.")

    out: list[dict[str, Any]] = []
    for symbol in sorted(functions_obj.keys()):
        if not include_symbol_for_codegen(symbol, codegen_cfg):
            continue

        payload = functions_obj.get(symbol)
        if not isinstance(payload, dict):
            continue
        return_type = str(payload.get("return_type") or "void")
        parameters_raw = str(payload.get("parameters") or "")
        parsed_params = parse_c_function_parameters(parameters_raw)

        param_entries: list[dict[str, Any]] = []
        for idx, param in enumerate(parsed_params):
            raw_name = str(param.get("name") or f"arg{idx}")
            c_param_type = normalize_c_type(str(param.get("c_type") or "void"))

            entry = {
                "name": raw_name,
                "c_type": c_param_type,
                "pointer_depth": c_param_type.count("*"),
                "variadic": bool(param.get("variadic")),
            }
            param_entries.append(entry)

        record = {
            "name": symbol,
            "c_return_type": normalize_c_type(return_type),
            "c_parameters_raw": parameters_raw,
            "parameters": param_entries,
            "c_signature": payload.get("signature"),
            "stable_id": stable_hash(
                {
                    "name": symbol,
                    "return_type": normalize_c_type(return_type),
                    "parameters": [
                        (str(item["name"]), str(item["c_type"])) for item in param_entries
                    ],
                }
            ),
        }
        out.append(record)

    return out


def build_idl_payload(
    target_name: str,
    snapshot: dict[str, Any],
    codegen_cfg: dict[str, Any],
) -> dict[str, Any]:
    records = build_function_idl_records(target_name=target_name, snapshot=snapshot, codegen_cfg=codegen_cfg)
    header = snapshot.get("header")
    if not isinstance(header, dict):
        raise AbiFrameworkError(f"Snapshot for target '{target_name}' is missing header.")
    content_fingerprint = stable_hash(
        {
            "target": target_name,
            "abi_version": snapshot.get("abi_version"),
            "functions": [
                {
                    "name": record.get("name"),
                    "c_return_type": record.get("c_return_type"),
                    "parameters": record.get("parameters"),
                }
                for record in records
            ],
        }
    )

    payload = {
        "tool": {
            "name": "abi_framework",
            "version": TOOL_VERSION,
        },
        "content_fingerprint": content_fingerprint,
        "target": target_name,
        "abi_version": snapshot.get("abi_version"),
        "summary": {
            "function_count": len(records),
            "enum_count": int(header.get("enum_count") or 0),
            "struct_count": int(header.get("struct_count") or 0),
        },
        "functions": records,
        "header_types": {
            "enums": header.get("enums", {}),
            "structs": header.get("structs", {}),
        },
        "codegen": {
            "enabled": bool(codegen_cfg.get("enabled", True)),
            "include_symbols": sorted(codegen_cfg.get("include_symbols", [])),
            "exclude_symbols": sorted(codegen_cfg.get("exclude_symbols", [])),
        },
    }
    return payload


def read_text_if_exists(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AbiFrameworkError(f"Unable to read file '{path}': {exc}") from exc


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def normalized_lines(value: str) -> list[str]:
    return value.replace("\r\n", "\n").splitlines()


def compute_unified_diff(old_content: str, new_content: str, old_label: str, new_label: str) -> str:
    diff_lines = difflib.unified_diff(
        normalized_lines(old_content),
        normalized_lines(new_content),
        fromfile=old_label,
        tofile=new_label,
        lineterm="",
    )
    return "\n".join(diff_lines)


def write_artifact_if_changed(
    *,
    path: Path,
    content: str,
    dry_run: bool,
    check: bool,
) -> tuple[str, str]:
    old_content = read_text_if_exists(path)
    if old_content == content:
        return "unchanged", ""
    if check:
        return "drift", compute_unified_diff(old_content, content, f"a/{path}", f"b/{path}")
    if dry_run:
        return "would_write", compute_unified_diff(old_content, content, f"a/{path}", f"b/{path}")
    write_text(path, content)
    return "updated", compute_unified_diff(old_content, content, f"a/{path}", f"b/{path}")


def is_valid_layout_name(name: str) -> bool:
    return bool(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name))


def is_offsetable_field(field: dict[str, Any]) -> bool:
    name = str(field.get("name", ""))
    declaration = str(field.get("declaration", ""))
    if not is_valid_layout_name(name):
        return False
    if name.startswith("__unnamed_"):
        return False
    if ":" in declaration:
        return False
    return True


def normalize_string_list(value: Any, key: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise AbiFrameworkError(f"Target field '{key}' must be an array when specified.")
    out: list[str] = []
    for idx, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise AbiFrameworkError(f"Target field '{key}[{idx}]' must be a non-empty string.")
        out.append(item)
    return out


def probe_struct_layouts(
    header_path: Path,
    structs: dict[str, Any],
    layout_cfg_raw: Any,
    repo_root: Path,
) -> dict[str, Any]:
    default_payload = {
        "enabled": False,
        "available": False,
        "reason": "disabled",
        "compiler": None,
        "struct_count": 0,
        "structs": {},
        "errors": [],
    }

    if layout_cfg_raw is None:
        return default_payload
    if not isinstance(layout_cfg_raw, dict):
        raise AbiFrameworkError("Target field 'header.layout' must be an object when specified.")
    if not bool(layout_cfg_raw.get("enable", False)):
        return default_payload

    compiler = str(layout_cfg_raw.get("compiler") or os.environ.get("CC") or "cc")
    cflags = normalize_string_list(layout_cfg_raw.get("cflags"), "header.layout.cflags")
    include_dirs_raw = normalize_string_list(layout_cfg_raw.get("include_dirs"), "header.layout.include_dirs")
    include_dirs = [str(ensure_relative_path(repo_root, path).resolve()) for path in include_dirs_raw]

    include_dir_set: set[str] = set(include_dirs)
    include_dir_set.add(str(header_path.parent.resolve()))
    include_dirs = sorted(include_dir_set)

    struct_names = [name for name in sorted(structs.keys()) if is_valid_layout_name(name)]
    if not struct_names:
        return {
            "enabled": True,
            "available": False,
            "reason": "no_structs",
            "compiler": compiler,
            "struct_count": 0,
            "structs": {},
            "errors": [],
        }

    with tempfile.TemporaryDirectory(prefix="abi_layout_probe_") as temp_dir:
        temp_path = Path(temp_dir)
        source_path = temp_path / "probe.c"
        binary_path = temp_path / ("probe.exe" if os.name == "nt" else "probe")

        lines: list[str] = []
        lines.append("#include <stddef.h>")
        lines.append("#include <stdio.h>")
        lines.append(f'#include "{str(header_path)}"')
        lines.append("int main(void) {")
        lines.append('  printf("{");')

        for s_idx, struct_name in enumerate(struct_names):
            prefix = "," if s_idx > 0 else ""
            lines.append(
                f'  printf("{prefix}\\"{struct_name}\\":{{\\"size\\":%zu,\\"alignment\\":%zu,\\"offsets\\":{{", '
                f"sizeof({struct_name}), _Alignof({struct_name}));"
            )
            struct_obj = structs.get(struct_name)
            fields = struct_obj.get("fields") if isinstance(struct_obj, dict) else []
            offsetable = [field for field in fields if isinstance(field, dict) and is_offsetable_field(field)]
            for f_idx, field in enumerate(offsetable):
                field_name = str(field["name"])
                field_prefix = "," if f_idx > 0 else ""
                lines.append(
                    f'  printf("{field_prefix}\\"{field_name}\\":%zu", offsetof({struct_name}, {field_name}));'
                )
            lines.append('  printf("}}");')

        lines.append('  printf("}");')
        lines.append("  return 0;")
        lines.append("}")

        source_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        compile_cmd = [compiler, "-std=c11", str(source_path), "-o", str(binary_path)]
        for include_dir in include_dirs:
            compile_cmd.extend(["-I", include_dir])
        compile_cmd.extend(cflags)

        try:
            compile_proc = subprocess.run(compile_cmd, capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            return {
                "enabled": True,
                "available": False,
                "reason": "compile_failed",
                "compiler": " ".join(compile_cmd),
                "struct_count": 0,
                "structs": {},
                "errors": [exc.stderr.strip() or exc.stdout.strip()],
            }
        except OSError as exc:
            return {
                "enabled": True,
                "available": False,
                "reason": "compiler_not_found",
                "compiler": " ".join(compile_cmd),
                "struct_count": 0,
                "structs": {},
                "errors": [str(exc)],
            }

        try:
            run_proc = subprocess.run([str(binary_path)], capture_output=True, text=True, check=True)
        except subprocess.CalledProcessError as exc:
            return {
                "enabled": True,
                "available": False,
                "reason": "probe_execution_failed",
                "compiler": " ".join(compile_cmd),
                "struct_count": 0,
                "structs": {},
                "errors": [exc.stderr.strip() or exc.stdout.strip()],
            }

        raw_output = run_proc.stdout.strip()
        try:
            layout_data = json.loads(raw_output) if raw_output else {}
        except json.JSONDecodeError as exc:
            return {
                "enabled": True,
                "available": False,
                "reason": "probe_output_parse_failed",
                "compiler": " ".join(compile_cmd),
                "struct_count": 0,
                "structs": {},
                "errors": [f"{exc}: {raw_output[:240]}"],
            }

        if not isinstance(layout_data, dict):
            return {
                "enabled": True,
                "available": False,
                "reason": "probe_output_invalid",
                "compiler": " ".join(compile_cmd),
                "struct_count": 0,
                "structs": {},
                "errors": ["Probe output root is not an object."],
            }

        normalized_layout: dict[str, Any] = {}
        for struct_name in struct_names:
            entry = layout_data.get(struct_name)
            if not isinstance(entry, dict):
                continue
            size = entry.get("size")
            alignment = entry.get("alignment")
            offsets = entry.get("offsets")
            if not isinstance(size, int) or not isinstance(alignment, int):
                continue
            if not isinstance(offsets, dict):
                offsets = {}
            normalized_offsets: dict[str, int] = {}
            for field_name, offset_value in offsets.items():
                if isinstance(field_name, str) and isinstance(offset_value, int):
                    normalized_offsets[field_name] = offset_value
            normalized_layout[struct_name] = {
                "size": size,
                "alignment": alignment,
                "offsets": normalized_offsets,
            }

        return {
            "enabled": True,
            "available": True,
            "reason": "ok",
            "compiler": " ".join(compile_cmd),
            "struct_count": len(normalized_layout),
            "structs": normalized_layout,
            "errors": [],
            "compile_stdout": compile_proc.stdout.strip(),
        }


def run_first_command(commands: list[list[str]]) -> tuple[str, list[str]]:
    errors: list[str] = []
    for command in commands:
        exe = command[0]
        if shutil.which(exe) is None:
            continue
        try:
            proc = subprocess.run(command, check=True, capture_output=True, text=True)
            return proc.stdout, command
        except subprocess.CalledProcessError as exc:
            errors.append(f"{' '.join(command)}: {exc.stderr.strip() or exc.stdout.strip()}")

    if errors:
        raise AbiFrameworkError("Failed to query binary exports. " + " | ".join(errors))
    raise AbiFrameworkError("No export listing tool found. Install 'nm', 'llvm-nm', or equivalent.")


def parse_nm_exports(output: str) -> list[str]:
    exports: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        symbol = parts[-1]
        if symbol in {"|", "<"}:
            continue
        exports.add(symbol)
    return sorted(exports)


def parse_dumpbin_exports(output: str) -> list[str]:
    exports: set[str] = set()
    export_line = re.compile(r"^\s+\d+\s+[0-9A-Fa-f]+\s+[0-9A-Fa-f]+\s+(\S+)$")
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        match = export_line.match(line)
        if match:
            exports.add(match.group(1))
    return sorted(exports)


def canonicalize_prefixed_symbol(symbol: str, symbol_prefix: str) -> str | None:
    raw = symbol
    if raw.startswith("_"):
        raw = raw[1:]
    base = raw
    if "@" in base:
        left, right = base.rsplit("@", 1)
        if right.isdigit():
            base = left
    if symbol_prefix and not base.startswith(symbol_prefix):
        return None
    return base


def extract_binary_exports(binary_path: Path, symbol_prefix: str, allow_non_prefixed_exports: bool) -> dict[str, Any]:
    if not binary_path.exists():
        return {
            "available": False,
            "path": str(binary_path),
            "tool": None,
            "symbol_count": 0,
            "symbols": [],
            "raw_export_count": 0,
            "non_prefixed_export_count": 0,
            "non_prefixed_exports": [],
            "allow_non_prefixed_exports": allow_non_prefixed_exports,
        }

    commands: list[list[str]] = []
    if sys.platform.startswith("linux"):
        commands.append(["nm", "-D", "--defined-only", str(binary_path)])
        commands.append(["llvm-nm", "-D", "--defined-only", str(binary_path)])
    elif sys.platform == "darwin":
        commands.append(["nm", "-gU", str(binary_path)])
        commands.append(["llvm-nm", "-gU", str(binary_path)])
    elif os.name == "nt":
        commands.append(["dumpbin", "/exports", str(binary_path)])
        commands.append(["llvm-nm", "--defined-only", str(binary_path)])
    else:
        commands.append(["nm", "--defined-only", str(binary_path)])
        commands.append(["llvm-nm", "--defined-only", str(binary_path)])

    raw_output, used_tool = run_first_command(commands)
    if used_tool and used_tool[0].lower() == "dumpbin":
        raw_exports = parse_dumpbin_exports(raw_output)
    else:
        raw_exports = parse_nm_exports(raw_output)

    canonical_symbols: set[str] = set()
    non_prefixed: list[str] = []
    for raw_symbol in raw_exports:
        canonical = canonicalize_prefixed_symbol(raw_symbol, symbol_prefix)
        if canonical is None:
            non_prefixed.append(raw_symbol)
            continue
        canonical_symbols.add(canonical)

    return {
        "available": True,
        "path": str(binary_path),
        "tool": " ".join(used_tool),
        "symbol_count": len(canonical_symbols),
        "symbols": sorted(canonical_symbols),
        "raw_export_count": len(raw_exports),
        "non_prefixed_export_count": len(non_prefixed),
        "non_prefixed_exports": non_prefixed,
        "allow_non_prefixed_exports": allow_non_prefixed_exports,
    }


def require_dict(value: Any, key: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AbiFrameworkError(f"Target is missing required object '{key}'.")
    return value


def require_str(value: Any, key: str) -> str:
    if not isinstance(value, str) or not value:
        raise AbiFrameworkError(f"Target field '{key}' must be a non-empty string.")
    return value


def build_snapshot(config: dict[str, Any], target_name: str, repo_root: Path, binary_override: str | None, skip_binary: bool) -> dict[str, Any]:
    target = resolve_target(config, target_name)

    header_cfg = require_dict(target.get("header"), "header")
    header_path = ensure_relative_path(repo_root, require_str(header_cfg.get("path"), "header.path")).resolve()
    api_macro = require_str(header_cfg.get("api_macro"), "header.api_macro")
    call_macro = require_str(header_cfg.get("call_macro"), "header.call_macro")
    symbol_prefix = require_str(header_cfg.get("symbol_prefix"), "header.symbol_prefix")
    type_policy = build_type_policy(header_cfg=header_cfg, symbol_prefix=symbol_prefix)

    version_macros_cfg = require_dict(header_cfg.get("version_macros"), "header.version_macros")
    version_macros = {
        "major": require_str(version_macros_cfg.get("major"), "header.version_macros.major"),
        "minor": require_str(version_macros_cfg.get("minor"), "header.version_macros.minor"),
        "patch": require_str(version_macros_cfg.get("patch"), "header.version_macros.patch"),
    }

    header_payload, abi_version = parse_c_header(
        header_path=header_path,
        api_macro=api_macro,
        call_macro=call_macro,
        symbol_prefix=symbol_prefix,
        version_macros=version_macros,
        type_policy=type_policy,
    )
    header_payload["path"] = to_repo_relative(header_path, repo_root)
    header_payload["layout_probe"] = probe_struct_layouts(
        header_path=header_path,
        structs=header_payload.get("structs", {}),
        layout_cfg_raw=header_cfg.get("layout"),
        repo_root=repo_root,
    )

    bindings_payload: dict[str, Any] = {
        "available": False,
        "source": "not_configured",
        "symbol_count": 0,
        "symbols": [],
    }
    bindings_cfg = target.get("bindings")
    if isinstance(bindings_cfg, dict):
        expected_symbols = bindings_cfg.get("expected_symbols")
        if isinstance(expected_symbols, list):
            cleaned_symbols = sorted({str(item) for item in expected_symbols if isinstance(item, str) and item})
            bindings_payload = {
                "available": True,
                "source": "config.bindings.expected_symbols",
                "symbol_count": len(cleaned_symbols),
                "symbols": cleaned_symbols,
            }

    binary_payload: dict[str, Any]
    if skip_binary:
        binary_payload = {
            "available": False,
            "path": None,
            "tool": None,
            "symbol_count": 0,
            "symbols": [],
            "raw_export_count": 0,
            "non_prefixed_export_count": 0,
            "non_prefixed_exports": [],
            "allow_non_prefixed_exports": True,
            "skipped": True,
            "reason": "explicit_skip",
        }
    else:
        binary_cfg_obj = target.get("binary")
        if binary_override:
            allow_non_prefixed = False
            binary_path = ensure_relative_path(repo_root, binary_override).resolve()
            binary_payload = extract_binary_exports(
                binary_path=binary_path,
                symbol_prefix=symbol_prefix,
                allow_non_prefixed_exports=allow_non_prefixed,
            )
            binary_payload["path"] = to_repo_relative(binary_path, repo_root)
            binary_payload["skipped"] = False
        elif isinstance(binary_cfg_obj, dict):
            configured_path = require_str(binary_cfg_obj.get("path"), "binary.path")
            allow_non_prefixed = bool(binary_cfg_obj.get("allow_non_prefixed_exports", False))
            binary_path = ensure_relative_path(repo_root, configured_path).resolve()
            binary_payload = extract_binary_exports(
                binary_path=binary_path,
                symbol_prefix=symbol_prefix,
                allow_non_prefixed_exports=allow_non_prefixed,
            )
            binary_payload["path"] = to_repo_relative(binary_path, repo_root)
            binary_payload["skipped"] = False
        else:
            binary_payload = {
                "available": False,
                "path": None,
                "tool": None,
                "symbol_count": 0,
                "symbols": [],
                "raw_export_count": 0,
                "non_prefixed_export_count": 0,
                "non_prefixed_exports": [],
                "allow_non_prefixed_exports": True,
                "skipped": True,
                "reason": "not_configured",
            }

    snapshot = {
        "tool": {
            "name": "abi_framework",
            "version": TOOL_VERSION,
        },
        "target": target_name,
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "policy": {
            "type_policy": type_policy.as_dict(),
            "strict_semver": True,
        },
        "abi_version": abi_version.as_dict(),
        "header": header_payload,
        "bindings": bindings_payload,
        "binary": binary_payload,
    }
    validate_snapshot_payload(snapshot, f"generated snapshot '{target_name}'")
    return snapshot


def load_snapshot(path: Path) -> dict[str, Any]:
    snapshot = load_json(path)
    validate_snapshot_payload(snapshot, f"snapshot '{path}'")
    return snapshot


def parse_snapshot_version(snapshot: dict[str, Any], label: str) -> AbiVersion:
    version_obj = snapshot.get("abi_version")
    if not isinstance(version_obj, dict):
        raise AbiFrameworkError(f"Snapshot '{label}' is missing abi_version.")
    try:
        major = int(version_obj["major"])
        minor = int(version_obj["minor"])
        patch = int(version_obj["patch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AbiFrameworkError(f"Snapshot '{label}' has invalid abi_version format.") from exc
    return AbiVersion(major=major, minor=minor, patch=patch)


def as_symbol_set(snapshot: dict[str, Any], section: str) -> set[str]:
    payload = snapshot.get(section)
    if not isinstance(payload, dict):
        raise AbiFrameworkError(f"Snapshot is missing section '{section}'.")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        raise AbiFrameworkError(f"Snapshot section '{section}' is missing symbols array.")
    return {str(x) for x in symbols}


def get_header_types(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    header = snapshot.get("header")
    if not isinstance(header, dict):
        return {}, {}

    enums = header.get("enums")
    structs = header.get("structs")

    out_enums = enums if isinstance(enums, dict) else {}
    out_structs = structs if isinstance(structs, dict) else {}
    return out_enums, out_structs


def compare_enum_sets(base_enums: dict[str, Any], curr_enums: dict[str, Any]) -> dict[str, Any]:
    base_names = set(base_enums.keys())
    curr_names = set(curr_enums.keys())

    removed_enums = sorted(base_names - curr_names)
    added_enums = sorted(curr_names - base_names)

    changed_enums: dict[str, Any] = {}
    breaking_changes: list[str] = []
    additive_changes: list[str] = []

    for name in sorted(base_names & curr_names):
        base_members = base_enums[name].get("members")
        curr_members = curr_enums[name].get("members")
        if not isinstance(base_members, list) or not isinstance(curr_members, list):
            changed_enums[name] = {
                "kind": "unknown",
                "reason": "enum members payload malformed",
            }
            breaking_changes.append(f"enum {name} malformed")
            continue

        base_map = {str(item.get("name")): item for item in base_members if isinstance(item, dict)}
        curr_map = {str(item.get("name")): item for item in curr_members if isinstance(item, dict)}

        removed_members = sorted(set(base_map.keys()) - set(curr_map.keys()))
        added_members = sorted(set(curr_map.keys()) - set(base_map.keys()))

        value_changed: list[str] = []
        for member_name in sorted(set(base_map.keys()) & set(curr_map.keys())):
            b = base_map[member_name]
            c = curr_map[member_name]
            if (b.get("value"), b.get("value_expr")) != (c.get("value"), c.get("value_expr")):
                value_changed.append(member_name)

        if removed_members or value_changed:
            changed_enums[name] = {
                "kind": "breaking",
                "removed_members": removed_members,
                "added_members": added_members,
                "value_changed": value_changed,
            }
            if removed_members:
                breaking_changes.append(f"enum {name} removed members: {', '.join(removed_members)}")
            if value_changed:
                breaking_changes.append(f"enum {name} changed values: {', '.join(value_changed)}")
            continue

        if added_members:
            changed_enums[name] = {
                "kind": "additive",
                "removed_members": [],
                "added_members": added_members,
                "value_changed": [],
            }
            additive_changes.append(f"enum {name} added members: {', '.join(added_members)}")

    if removed_enums:
        breaking_changes.append("removed enums: " + ", ".join(removed_enums))
    if added_enums:
        additive_changes.append("added enums: " + ", ".join(added_enums))

    return {
        "removed_enums": removed_enums,
        "added_enums": added_enums,
        "changed_enums": changed_enums,
        "breaking_changes": breaking_changes,
        "additive_changes": additive_changes,
    }


def compare_struct_sets(base_structs: dict[str, Any], curr_structs: dict[str, Any], struct_tail_addition_is_breaking: bool) -> dict[str, Any]:
    base_names = set(base_structs.keys())
    curr_names = set(curr_structs.keys())

    removed_structs = sorted(base_names - curr_names)
    added_structs = sorted(curr_names - base_names)

    changed_structs: dict[str, Any] = {}
    breaking_changes: list[str] = []
    additive_changes: list[str] = []

    for name in sorted(base_names & curr_names):
        base_fields = base_structs[name].get("fields")
        curr_fields = curr_structs[name].get("fields")
        if not isinstance(base_fields, list) or not isinstance(curr_fields, list):
            changed_structs[name] = {
                "kind": "unknown",
                "reason": "struct fields payload malformed",
            }
            breaking_changes.append(f"struct {name} malformed")
            continue

        base_decls = [normalize_ws(str(item.get("declaration"))) for item in base_fields if isinstance(item, dict)]
        curr_decls = [normalize_ws(str(item.get("declaration"))) for item in curr_fields if isinstance(item, dict)]

        if base_decls == curr_decls:
            continue

        base_names_seq = [str(item.get("name")) for item in base_fields if isinstance(item, dict)]
        curr_names_seq = [str(item.get("name")) for item in curr_fields if isinstance(item, dict)]

        removed_fields = sorted(set(base_names_seq) - set(curr_names_seq))
        added_fields = sorted(set(curr_names_seq) - set(base_names_seq))

        common = set(base_names_seq) & set(curr_names_seq)
        changed_fields: list[str] = []
        for field_name in sorted(common):
            b_idx = base_names_seq.index(field_name)
            c_idx = curr_names_seq.index(field_name)
            if base_decls[b_idx] != curr_decls[c_idx] or b_idx != c_idx:
                changed_fields.append(field_name)

        base_is_prefix = len(curr_decls) >= len(base_decls) and curr_decls[: len(base_decls)] == base_decls
        additive_tail = base_is_prefix and not struct_tail_addition_is_breaking

        if additive_tail:
            changed_structs[name] = {
                "kind": "additive",
                "removed_fields": removed_fields,
                "added_fields": added_fields,
                "changed_fields": changed_fields,
                "base_is_prefix": base_is_prefix,
            }
            additive_changes.append(f"struct {name} tail extended")
        else:
            changed_structs[name] = {
                "kind": "breaking",
                "removed_fields": removed_fields,
                "added_fields": added_fields,
                "changed_fields": changed_fields,
                "base_is_prefix": base_is_prefix,
            }
            breaking_changes.append(f"struct {name} layout changed")

    if removed_structs:
        breaking_changes.append("removed structs: " + ", ".join(removed_structs))
    if added_structs:
        additive_changes.append("added structs: " + ", ".join(added_structs))

    return {
        "removed_structs": removed_structs,
        "added_structs": added_structs,
        "changed_structs": changed_structs,
        "breaking_changes": breaking_changes,
        "additive_changes": additive_changes,
    }


def compare_layout_probes(base_header: dict[str, Any], curr_header: dict[str, Any]) -> dict[str, Any]:
    base_layout = base_header.get("layout_probe")
    curr_layout = curr_header.get("layout_probe")

    out = {
        "available_in_baseline": False,
        "available_in_current": False,
        "checked_structs": 0,
        "breaking_changes": [],
        "warnings": [],
    }

    if isinstance(base_layout, dict) and bool(base_layout.get("available")):
        out["available_in_baseline"] = True
    if isinstance(curr_layout, dict) and bool(curr_layout.get("available")):
        out["available_in_current"] = True

    if out["available_in_baseline"] and not out["available_in_current"]:
        out["warnings"].append("layout probe unavailable in current snapshot while baseline had layout data")
        return out
    if out["available_in_current"] and not out["available_in_baseline"]:
        out["warnings"].append("layout probe available in current snapshot but baseline has no layout data")
        return out
    if not out["available_in_baseline"] and not out["available_in_current"]:
        return out

    base_structs_obj = base_layout.get("structs") if isinstance(base_layout, dict) else {}
    curr_structs_obj = curr_layout.get("structs") if isinstance(curr_layout, dict) else {}
    if not isinstance(base_structs_obj, dict) or not isinstance(curr_structs_obj, dict):
        out["warnings"].append("layout probe payload malformed")
        return out

    shared_structs = sorted(set(base_structs_obj.keys()) & set(curr_structs_obj.keys()))
    out["checked_structs"] = len(shared_structs)

    for struct_name in shared_structs:
        base_entry = base_structs_obj.get(struct_name)
        curr_entry = curr_structs_obj.get(struct_name)
        if not isinstance(base_entry, dict) or not isinstance(curr_entry, dict):
            out["breaking_changes"].append(f"layout {struct_name}: malformed entry")
            continue

        base_size = base_entry.get("size")
        curr_size = curr_entry.get("size")
        base_alignment = base_entry.get("alignment")
        curr_alignment = curr_entry.get("alignment")
        if base_size != curr_size:
            out["breaking_changes"].append(
                f"layout {struct_name}: size changed ({base_size} -> {curr_size})"
            )
        if base_alignment != curr_alignment:
            out["breaking_changes"].append(
                f"layout {struct_name}: alignment changed ({base_alignment} -> {curr_alignment})"
            )

        base_offsets = base_entry.get("offsets")
        curr_offsets = curr_entry.get("offsets")
        if not isinstance(base_offsets, dict) or not isinstance(curr_offsets, dict):
            out["breaking_changes"].append(f"layout {struct_name}: offsets payload malformed")
            continue

        for field_name in sorted(set(base_offsets.keys()) & set(curr_offsets.keys())):
            base_offset = base_offsets.get(field_name)
            curr_offset = curr_offsets.get(field_name)
            if base_offset != curr_offset:
                out["breaking_changes"].append(
                    f"layout {struct_name}.{field_name}: offset changed ({base_offset} -> {curr_offset})"
                )

    return out


def classify_change(has_breaking: bool, has_additive: bool) -> tuple[str, str]:
    if has_breaking:
        return "breaking", "major"
    if has_additive:
        return "additive", "minor"
    return "none", "none"


def recommended_version(baseline: AbiVersion, required_bump: str) -> AbiVersion:
    if required_bump == "major":
        return AbiVersion(baseline.major + 1, 0, 0)
    if required_bump == "minor":
        return AbiVersion(baseline.major, baseline.minor + 1, 0)
    return AbiVersion(baseline.major, baseline.minor, baseline.patch + 1)


def validate_version_policy(
    baseline_version: AbiVersion,
    current_version: AbiVersion,
    required_bump: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if current_version.as_tuple() < baseline_version.as_tuple():
        errors.append(
            f"ABI version regressed: baseline {baseline_version.as_tuple()} -> current {current_version.as_tuple()}."
        )
        return False, errors

    if required_bump == "major":
        if current_version.major <= baseline_version.major:
            errors.append(
                "Breaking ABI changes detected but ABI major version was not increased "
                f"(baseline {baseline_version.major}, current {current_version.major})."
            )
            return False, errors
    elif required_bump == "minor":
        if current_version.major == baseline_version.major and current_version.minor <= baseline_version.minor:
            errors.append(
                "Additive ABI changes detected but ABI minor version was not increased "
                f"(baseline {baseline_version.major}.{baseline_version.minor}, "
                f"current {current_version.major}.{current_version.minor})."
            )
            return False, errors

    return True, errors


def compare_snapshots(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    base_header = baseline.get("header", {})
    curr_header = current.get("header", {})
    base_funcs = base_header.get("functions")
    curr_funcs = curr_header.get("functions")
    if not isinstance(base_funcs, dict) or not isinstance(curr_funcs, dict):
        raise AbiFrameworkError("Snapshots must include header.functions objects.")

    base_names = set(base_funcs.keys())
    curr_names = set(curr_funcs.keys())
    removed = sorted(base_names - curr_names)
    added = sorted(curr_names - base_names)
    changed = sorted(
        name
        for name in (base_names & curr_names)
        if base_funcs[name].get("signature") != curr_funcs[name].get("signature")
    )

    if removed:
        warnings.append(f"Header symbols removed since baseline: {', '.join(removed)}")
    if changed:
        warnings.append(f"Header signatures changed since baseline: {', '.join(changed)}")

    baseline_version = parse_snapshot_version(baseline, "baseline")
    current_version = parse_snapshot_version(current, "current")

    curr_header_symbols = as_symbol_set(current, "header")
    bindings_payload = current.get("bindings")
    if isinstance(bindings_payload, dict):
        binding_symbols = bindings_payload.get("symbols")
        if isinstance(binding_symbols, list) and binding_symbols:
            curr_binding_symbols = {str(x) for x in binding_symbols}
            missing_in_bindings = sorted(curr_header_symbols - curr_binding_symbols)
            extra_in_bindings = sorted(curr_binding_symbols - curr_header_symbols)
            if missing_in_bindings:
                errors.append(
                    "Header symbols missing in configured bindings: " + ", ".join(missing_in_bindings)
                )
            if extra_in_bindings:
                errors.append(
                    "Configured bindings symbols not present in header: " + ", ".join(extra_in_bindings)
                )
        else:
            warnings.append("Bindings symbol checks skipped: bindings.symbols is not configured.")
    else:
        warnings.append("Bindings symbol checks skipped: no bindings section in snapshot.")

    binary_payload = current.get("binary", {})
    binary_available = bool(binary_payload.get("available"))
    binary_skipped = bool(binary_payload.get("skipped"))
    if binary_available:
        curr_binary_symbols = as_symbol_set(current, "binary")
        missing_in_binary = sorted(curr_header_symbols - curr_binary_symbols)
        extra_prefixed_binary = sorted(curr_binary_symbols - curr_header_symbols)
        if missing_in_binary:
            errors.append(
                "Header symbols missing in native binary exports: " + ", ".join(missing_in_binary)
            )
        if extra_prefixed_binary:
            errors.append(
                "Native binary exports prefixed ABI symbols not present in header: " + ", ".join(extra_prefixed_binary)
            )

        allow_non_prefixed = bool(binary_payload.get("allow_non_prefixed_exports", False))
        non_prefixed = binary_payload.get("non_prefixed_exports")
        if isinstance(non_prefixed, list) and non_prefixed and not allow_non_prefixed:
            max_preview = 25
            preview = ", ".join(non_prefixed[:max_preview])
            if len(non_prefixed) > max_preview:
                preview += ", ..."
            errors.append(
                "Native binary exports non-ABI symbols. "
                f"Count={len(non_prefixed)}. Examples: {preview}"
            )
    elif not binary_skipped:
        warnings.append(
            "Binary export checks were not executed because the binary path does not exist yet."
        )

    base_enums, base_structs = get_header_types(baseline)
    curr_enums, curr_structs = get_header_types(current)

    struct_tail_breaking = True
    current_policy = current.get("policy")
    if isinstance(current_policy, dict):
        type_policy = current_policy.get("type_policy")
        if isinstance(type_policy, dict):
            struct_tail_breaking = bool(type_policy.get("struct_tail_addition_is_breaking", True))

    enum_diff = compare_enum_sets(base_enums=base_enums, curr_enums=curr_enums)
    struct_diff = compare_struct_sets(
        base_structs=base_structs,
        curr_structs=curr_structs,
        struct_tail_addition_is_breaking=struct_tail_breaking,
    )
    layout_diff = compare_layout_probes(base_header=base_header, curr_header=curr_header)

    function_breaking = bool(removed or changed)
    function_additive = bool(added)

    breaking_reasons: list[str] = []
    additive_reasons: list[str] = []

    if function_breaking:
        if removed:
            breaking_reasons.append("removed function symbols")
        if changed:
            breaking_reasons.append("changed function signatures")
    if function_additive:
        additive_reasons.append("added function symbols")

    breaking_reasons.extend(enum_diff["breaking_changes"])
    additive_reasons.extend(enum_diff["additive_changes"])
    breaking_reasons.extend(struct_diff["breaking_changes"])
    additive_reasons.extend(struct_diff["additive_changes"])
    if layout_diff["breaking_changes"]:
        breaking_reasons.extend(layout_diff["breaking_changes"])
    if layout_diff["warnings"]:
        warnings.extend(layout_diff["warnings"])

    change_classification, required_bump = classify_change(
        has_breaking=bool(breaking_reasons),
        has_additive=bool(additive_reasons),
    )

    version_ok, version_errors = validate_version_policy(
        baseline_version=baseline_version,
        current_version=current_version,
        required_bump=required_bump,
    )
    errors.extend(version_errors)

    recommended = recommended_version(baseline=baseline_version, required_bump=required_bump)

    status = "pass" if not errors else "fail"
    report = {
        "status": status,
        "change_classification": change_classification,
        "required_bump": required_bump,
        "baseline_abi_version": baseline_version.as_dict(),
        "current_abi_version": current_version.as_dict(),
        "recommended_next_version": recommended.as_dict(),
        "version_policy_satisfied": version_ok,
        "removed_symbols": removed,
        "added_symbols": added,
        "changed_signatures": changed,
        "enum_diff": enum_diff,
        "struct_diff": struct_diff,
        "layout_diff": layout_diff,
        "breaking_reasons": breaking_reasons,
        "additive_reasons": additive_reasons,
        "errors": errors,
        "warnings": warnings,
    }
    validate_report_payload(report, "compare report")
    return report


def print_report(report: dict[str, Any]) -> None:
    status = report.get("status", "unknown")
    print(f"ABI check status: {status}")

    removed = report.get("removed_symbols", [])
    added = report.get("added_symbols", [])
    changed = report.get("changed_signatures", [])
    print(f"Removed symbols: {len(removed)}")
    print(f"Added symbols: {len(added)}")
    print(f"Changed signatures: {len(changed)}")

    classification = report.get("change_classification")
    required_bump = report.get("required_bump")
    recommended = report.get("recommended_next_version")
    print(f"Change classification: {classification}")
    print(f"Required bump: {required_bump}")
    print(f"Recommended next version: {recommended}")

    warnings = report.get("warnings", [])
    errors = report.get("errors", [])

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# ABI Report ({report.get('status', 'unknown')})")
    lines.append("")
    lines.append(f"- Baseline ABI version: `{report.get('baseline_abi_version')}`")
    lines.append(f"- Current ABI version: `{report.get('current_abi_version')}`")
    lines.append(f"- Change classification: `{report.get('change_classification')}`")
    lines.append(f"- Required bump: `{report.get('required_bump')}`")
    lines.append(f"- Recommended next version: `{report.get('recommended_next_version')}`")
    lines.append(f"- Removed symbols: `{len(report.get('removed_symbols', []))}`")
    lines.append(f"- Added symbols: `{len(report.get('added_symbols', []))}`")
    lines.append(f"- Changed signatures: `{len(report.get('changed_signatures', []))}`")
    lines.append("")

    breaking_reasons = report.get("breaking_reasons", [])
    additive_reasons = report.get("additive_reasons", [])
    warnings = report.get("warnings", [])
    errors = report.get("errors", [])

    if breaking_reasons:
        lines.append("## Breaking Reasons")
        for reason in breaking_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if additive_reasons:
        lines.append("## Additive Reasons")
        for reason in additive_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    if errors:
        lines.append("## Errors")
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def version_dict_to_str(value: Any) -> str:
    if isinstance(value, dict):
        major = value.get("major")
        minor = value.get("minor")
        patch = value.get("patch")
        if isinstance(major, int) and isinstance(minor, int) and isinstance(patch, int):
            return f"{major}.{minor}.{patch}"
    return "n/a"


def append_markdown_list(lines: list[str], items: list[str], indent: str = "") -> None:
    for item in items:
        lines.append(f"{indent}- {item}")


def render_target_changelog_section(target_name: str, report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {target_name}")
    lines.append("")
    lines.append(f"- Status: `{report.get('status', 'unknown')}`")
    lines.append(f"- Change classification: `{report.get('change_classification', 'unknown')}`")
    lines.append(f"- Required bump: `{report.get('required_bump', 'none')}`")
    lines.append(f"- Baseline ABI version: `{version_dict_to_str(report.get('baseline_abi_version'))}`")
    lines.append(f"- Current ABI version: `{version_dict_to_str(report.get('current_abi_version'))}`")
    lines.append(f"- Recommended next version: `{version_dict_to_str(report.get('recommended_next_version'))}`")
    lines.append("")

    breaking_reasons = get_message_list(report, "breaking_reasons")
    additive_reasons = get_message_list(report, "additive_reasons")
    removed_symbols = get_message_list(report, "removed_symbols")
    added_symbols = get_message_list(report, "added_symbols")
    changed_signatures = get_message_list(report, "changed_signatures")

    enum_diff = report.get("enum_diff")
    struct_diff = report.get("struct_diff")
    enum_diff_obj = enum_diff if isinstance(enum_diff, dict) else {}
    struct_diff_obj = struct_diff if isinstance(struct_diff, dict) else {}

    lines.append("### Breaking")
    if not breaking_reasons and not removed_symbols and not changed_signatures:
        lines.append("- None.")
    else:
        if breaking_reasons:
            lines.append("- Reasons:")
            append_markdown_list(lines, breaking_reasons, indent="  ")
        if removed_symbols:
            lines.append("- Removed function symbols:")
            append_markdown_list(lines, removed_symbols, indent="  ")
        if changed_signatures:
            lines.append("- Changed function signatures:")
            append_markdown_list(lines, changed_signatures, indent="  ")

    removed_enums = get_message_list(enum_diff_obj, "removed_enums")
    if removed_enums:
        lines.append("- Removed enums:")
        append_markdown_list(lines, removed_enums, indent="  ")

    changed_enums = enum_diff_obj.get("changed_enums")
    if isinstance(changed_enums, dict):
        for enum_name in sorted(changed_enums.keys()):
            detail = changed_enums[enum_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "breaking":
                continue
            lines.append(f"- Enum `{enum_name}` changed (breaking):")
            removed_members = get_message_list(detail, "removed_members")
            changed_members = get_message_list(detail, "value_changed")
            if removed_members:
                lines.append("  - Removed members:")
                append_markdown_list(lines, removed_members, indent="    ")
            if changed_members:
                lines.append("  - Members with changed values:")
                append_markdown_list(lines, changed_members, indent="    ")

    removed_structs = get_message_list(struct_diff_obj, "removed_structs")
    if removed_structs:
        lines.append("- Removed structs:")
        append_markdown_list(lines, removed_structs, indent="  ")

    changed_structs = struct_diff_obj.get("changed_structs")
    if isinstance(changed_structs, dict):
        for struct_name in sorted(changed_structs.keys()):
            detail = changed_structs[struct_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "breaking":
                continue
            lines.append(f"- Struct `{struct_name}` layout changed (breaking).")

    lines.append("")
    lines.append("### Additive")
    if not additive_reasons and not added_symbols:
        lines.append("- None.")
    else:
        if additive_reasons:
            lines.append("- Reasons:")
            append_markdown_list(lines, additive_reasons, indent="  ")
        if added_symbols:
            lines.append("- Added function symbols:")
            append_markdown_list(lines, added_symbols, indent="  ")

    added_enums = get_message_list(enum_diff_obj, "added_enums")
    if added_enums:
        lines.append("- Added enums:")
        append_markdown_list(lines, added_enums, indent="  ")

    if isinstance(changed_enums, dict):
        for enum_name in sorted(changed_enums.keys()):
            detail = changed_enums[enum_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "additive":
                continue
            added_members = get_message_list(detail, "added_members")
            if added_members:
                lines.append(f"- Enum `{enum_name}` added members:")
                append_markdown_list(lines, added_members, indent="  ")

    added_structs = get_message_list(struct_diff_obj, "added_structs")
    if added_structs:
        lines.append("- Added structs:")
        append_markdown_list(lines, added_structs, indent="  ")

    if isinstance(changed_structs, dict):
        for struct_name in sorted(changed_structs.keys()):
            detail = changed_structs[struct_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "additive":
                continue
            lines.append(f"- Struct `{struct_name}` was extended (additive tail).")

    warnings = get_message_list(report, "warnings")
    errors = get_message_list(report, "errors")
    if warnings:
        lines.append("")
        lines.append("### Warnings")
        append_markdown_list(lines, warnings)
    if errors:
        lines.append("")
        lines.append("### Errors")
        append_markdown_list(lines, errors)

    lines.append("")
    return lines


def render_changelog_document(
    title: str,
    release_tag: str | None,
    generated_at_utc: str,
    results_by_target: dict[str, dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{generated_at_utc}`")
    if release_tag:
        lines.append(f"- Release tag: `{release_tag}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Target | Status | Classification | Required bump | Baseline | Current | Recommended |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for target_name in sorted(results_by_target.keys()):
        report = results_by_target[target_name]
        lines.append(
            f"| {target_name} | {report.get('status', 'unknown')} | "
            f"{report.get('change_classification', 'unknown')} | "
            f"{report.get('required_bump', 'none')} | "
            f"{version_dict_to_str(report.get('baseline_abi_version'))} | "
            f"{version_dict_to_str(report.get('current_abi_version'))} | "
            f"{version_dict_to_str(report.get('recommended_next_version'))} |"
        )
    lines.append("")

    for target_name in sorted(results_by_target.keys()):
        lines.extend(render_target_changelog_section(target_name, results_by_target[target_name]))

    return "\n".join(lines) + "\n"


def get_message_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def build_sarif_results_for_target(target_name: str, report: dict[str, Any], source_path: str | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    location = None
    if source_path:
        location = {
            "physicalLocation": {
                "artifactLocation": {
                    "uri": source_path,
                },
                "region": {
                    "startLine": 1,
                },
            }
        }

    for message in get_message_list(report, "errors"):
        result: dict[str, Any] = {
            "ruleId": "ABI001",
            "level": "error",
            "message": {
                "text": f"[{target_name}] {message}",
            },
        }
        if location:
            result["locations"] = [location]
        results.append(result)

    for message in get_message_list(report, "warnings"):
        result = {
            "ruleId": "ABI002",
            "level": "warning",
            "message": {
                "text": f"[{target_name}] {message}",
            },
        }
        if location:
            result["locations"] = [location]
        results.append(result)

    return results


def write_sarif_report(path: Path, results: list[dict[str, Any]]) -> None:
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "abi_framework",
                        "version": TOOL_VERSION,
                        "rules": [
                            {
                                "id": "ABI001",
                                "name": "AbiFrameworkError",
                                "shortDescription": {
                                    "text": "ABI compatibility error",
                                },
                                "defaultConfiguration": {
                                    "level": "error",
                                },
                            },
                            {
                                "id": "ABI002",
                                "name": "AbiFrameworkWarning",
                                "shortDescription": {
                                    "text": "ABI compatibility warning",
                                },
                                "defaultConfiguration": {
                                    "level": "warning",
                                },
                            },
                        ],
                    }
                },
                "results": results,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_aggregate_summary(results_by_target: dict[str, dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "target_count": len(results_by_target),
        "pass_count": 0,
        "fail_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "classification": {
            "none": 0,
            "additive": 0,
            "breaking": 0,
        },
    }

    for report in results_by_target.values():
        if report.get("status") == "pass":
            summary["pass_count"] += 1
        else:
            summary["fail_count"] += 1
        summary["error_count"] += len(get_message_list(report, "errors"))
        summary["warning_count"] += len(get_message_list(report, "warnings"))
        classification = str(report.get("change_classification", "none"))
        if classification in summary["classification"]:
            summary["classification"][classification] += 1

    return summary


CLASSIFICATION_ORDER = {
    "none": 0,
    "additive": 1,
    "breaking": 2,
}


def resolve_effective_policy(config: dict[str, Any], target_name: str) -> dict[str, Any]:
    defaults = {
        "max_allowed_classification": "breaking",
        "fail_on_warnings": False,
        "require_layout_probe": False,
    }

    root_policy = config.get("policy")
    if isinstance(root_policy, dict):
        for key in defaults.keys():
            if key in root_policy:
                defaults[key] = root_policy[key]

    target = resolve_target(config, target_name)
    target_policy = target.get("policy")
    if isinstance(target_policy, dict):
        for key in defaults.keys():
            if key in target_policy:
                defaults[key] = target_policy[key]

    max_allowed = str(defaults.get("max_allowed_classification", "breaking"))
    if max_allowed not in CLASSIFICATION_ORDER:
        raise AbiFrameworkError(
            f"Invalid policy.max_allowed_classification for target '{target_name}': {max_allowed}"
        )
    return {
        "max_allowed_classification": max_allowed,
        "fail_on_warnings": bool(defaults.get("fail_on_warnings", False)),
        "require_layout_probe": bool(defaults.get("require_layout_probe", False)),
    }


def apply_policy_to_report(report: dict[str, Any], policy: dict[str, Any], target_name: str) -> dict[str, Any]:
    out = json.loads(json.dumps(report))

    errors = get_message_list(out, "errors")
    warnings = get_message_list(out, "warnings")

    observed = str(out.get("change_classification", "none"))
    max_allowed = str(policy.get("max_allowed_classification", "breaking"))
    if observed not in CLASSIFICATION_ORDER:
        observed = "breaking"
    if max_allowed not in CLASSIFICATION_ORDER:
        max_allowed = "breaking"
    if CLASSIFICATION_ORDER[observed] > CLASSIFICATION_ORDER[max_allowed]:
        errors.append(
            f"Policy violation for target '{target_name}': classification '{observed}' exceeds allowed '{max_allowed}'."
        )

    if bool(policy.get("require_layout_probe", False)):
        layout_diff = out.get("layout_diff")
        layout_available = False
        if isinstance(layout_diff, dict):
            layout_available = bool(layout_diff.get("available_in_current"))
        if not layout_available:
            errors.append(
                f"Policy violation for target '{target_name}': layout probe is required but unavailable."
            )

    out["errors"] = errors
    out["warnings"] = warnings
    out["status"] = "pass" if not errors else "fail"
    out["policy"] = policy
    validate_report_payload(out, f"policy report '{target_name}'")
    return out


def resolve_target_names(config: dict[str, Any], target_name: str | None) -> list[str]:
    targets = get_targets_map(config)
    if target_name:
        if target_name not in targets:
            known = ", ".join(sorted(targets.keys()))
            raise AbiFrameworkError(f"Unknown target '{target_name}'. Known targets: {known}")
        return [target_name]
    return sorted(targets.keys())


def build_codegen_for_target(
    *,
    repo_root: Path,
    config: dict[str, Any],
    target_name: str,
    binary_override: str | None,
    skip_binary: bool,
    idl_output_override: str | None,
    dry_run: bool,
    check: bool,
    print_diff: bool,
) -> dict[str, Any]:
    target = resolve_target(config, target_name)
    snapshot = build_snapshot(
        config=config,
        target_name=target_name,
        repo_root=repo_root,
        binary_override=binary_override,
        skip_binary=skip_binary,
    )
    codegen_cfg = resolve_codegen_config(target=target, target_name=target_name, repo_root=repo_root)
    idl_payload = build_idl_payload(target_name=target_name, snapshot=snapshot, codegen_cfg=codegen_cfg)

    if idl_output_override:
        idl_output_path = ensure_relative_path(repo_root, idl_output_override).resolve()
    else:
        configured = codegen_cfg.get("idl_output_path")
        if isinstance(configured, Path):
            idl_output_path = configured
        else:
            idl_output_path = ensure_relative_path(repo_root, f"abi/generated/{target_name}.idl.json").resolve()

    idl_text = json.dumps(idl_payload, indent=2, sort_keys=True) + "\n"
    idl_status, idl_diff = write_artifact_if_changed(
        path=idl_output_path,
        content=idl_text,
        dry_run=dry_run,
        check=check,
    )

    generated_symbols = {
        str(item.get("name"))
        for item in idl_payload.get("functions", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    bindings_cfg = target.get("bindings")
    expected_symbols: set[str] = set()
    if isinstance(bindings_cfg, dict):
        raw = bindings_cfg.get("expected_symbols")
        if isinstance(raw, list):
            expected_symbols = {str(item) for item in raw if isinstance(item, str) and item}

    sync_comparison = {
        "mode": "expected_symbols" if expected_symbols else "not_configured",
        "missing_symbols": sorted(expected_symbols - generated_symbols),
        "extra_symbols": sorted(generated_symbols - expected_symbols) if expected_symbols else [],
    }

    if print_diff and idl_diff:
        print(idl_diff)

    has_codegen_drift = idl_status in {"drift", "would_write"}
    has_sync_drift = bool(sync_comparison["missing_symbols"]) or bool(sync_comparison["extra_symbols"])

    return {
        "target": target_name,
        "snapshot": snapshot,
        "idl_payload": idl_payload,
        "codegen_config": {
            "idl_output_path": to_repo_relative(idl_output_path, repo_root),
        },
        "artifacts": {
            "idl": {
                "path": to_repo_relative(idl_output_path, repo_root),
                "status": idl_status,
            },
        },
        "sync": sync_comparison,
        "has_codegen_drift": has_codegen_drift,
        "has_sync_drift": has_sync_drift,
    }


def print_sync_comparison(target_name: str, comparison: dict[str, Any]) -> None:
    mode = str(comparison.get("mode", "not_configured"))
    missing = get_message_list(comparison, "missing_symbols")
    extra = get_message_list(comparison, "extra_symbols")

    if mode == "not_configured":
        print(f"[{target_name}] bindings sync: not configured")
        return

    if not missing and not extra:
        print(f"[{target_name}] bindings sync: clean")
        return

    print(f"[{target_name}] bindings sync: drift")
    if missing:
        print(f"  missing expected symbols: {', '.join(missing)}")
    if extra:
        print(f"  extra generated symbols: {', '.join(extra)}")


def command_generate(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)

    if args.idl_output and len(target_names) != 1:
        raise AbiFrameworkError("--idl-output can only be used with a single target via --target.")

    aggregate: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "results": {},
    }
    exit_code = 0

    for target_name in target_names:
        result = build_codegen_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
            idl_output_override=args.idl_output,
            dry_run=args.dry_run,
            check=args.check,
            print_diff=args.print_diff,
        )
        aggregate["results"][target_name] = {
            "artifacts": result["artifacts"],
            "sync": result["sync"],
            "has_codegen_drift": result["has_codegen_drift"],
            "has_sync_drift": result["has_sync_drift"],
            "abi_version": result["snapshot"].get("abi_version"),
        }

        artifacts = result["artifacts"]
        idl_status = ((artifacts.get("idl") or {}).get("status")) or "unknown"
        print(f"[{target_name}] generate: idl={idl_status}")
        print_sync_comparison(target_name, result["sync"])

        if args.check and result["has_codegen_drift"]:
            exit_code = 1
        if bool(args.fail_on_sync) and result["has_sync_drift"]:
            exit_code = 1

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    return exit_code


def command_sync(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    aggregate: dict[str, Any] = {
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "results": {},
    }
    final_status = 0

    for target_name in target_names:
        result = build_codegen_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
            idl_output_override=None,
            dry_run=bool(args.check),
            check=bool(args.check),
            print_diff=bool(args.print_diff),
        )

        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        baseline_written = False
        if bool(args.update_baselines) and not bool(args.check):
            write_json(baseline_path, result["snapshot"])
            baseline_written = True
            print(f"[{target_name}] baseline updated: {baseline_path}")

        report: dict[str, Any] | None = None
        if not bool(args.no_verify):
            if not baseline_path.exists():
                raise AbiFrameworkError(f"Baseline does not exist for target '{target_name}': {baseline_path}")
            baseline = load_snapshot(baseline_path)
            raw_report = compare_snapshots(baseline=baseline, current=result["snapshot"])
            effective_policy = resolve_effective_policy(config=config, target_name=target_name)
            report = apply_policy_to_report(
                report=raw_report,
                policy=effective_policy,
                target_name=target_name,
            )
            print(
                f"[{target_name}] verify={report.get('status')} "
                f"classification={report.get('change_classification')} "
                f"required_bump={report.get('required_bump')}"
            )
            if report.get("status") != "pass":
                final_status = 1
            if bool(args.fail_on_warnings) and get_message_list(report, "warnings"):
                final_status = 1

        if bool(args.check) and bool(result["has_codegen_drift"]):
            final_status = 1
        if bool(args.fail_on_sync) and bool(result["has_sync_drift"]):
            final_status = 1

        aggregate["results"][target_name] = {
            "artifacts": result["artifacts"],
            "sync": result["sync"],
            "has_codegen_drift": result["has_codegen_drift"],
            "has_sync_drift": result["has_sync_drift"],
            "baseline_path": to_repo_relative(baseline_path, repo_root),
            "baseline_updated": baseline_written,
            "verify_report": report,
        }

        if output_dir:
            if report is not None:
                write_json(output_dir / f"{target_name}.sync.verify.report.json", report)
                write_markdown_report(output_dir / f"{target_name}.sync.verify.report.md", report)
            write_json(output_dir / f"{target_name}.sync.codegen.report.json", aggregate["results"][target_name])

    aggregate["summary"] = {
        "target_count": len(target_names),
        "codegen_drift_count": sum(
            1 for item in aggregate["results"].values() if isinstance(item, dict) and bool(item.get("has_codegen_drift"))
        ),
        "sync_drift_count": sum(
            1 for item in aggregate["results"].values() if isinstance(item, dict) and bool(item.get("has_sync_drift"))
        ),
    }

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    if output_dir:
        write_json(output_dir / "sync.aggregate.report.json", aggregate)

    return final_status


def command_release_prepare(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (repo_root / "artifacts" / "abi" / "release")
    output_dir.mkdir(parents=True, exist_ok=True)

    doctor_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        baseline_root=args.baseline_root,
        binary=args.binary,
        require_baselines=True,
        require_binaries=bool(args.require_binaries),
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    doctor_exit = command_doctor(doctor_args)
    if doctor_exit != 0:
        return doctor_exit

    sync_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        update_baselines=bool(args.update_baselines),
        check=bool(args.check_generated),
        print_diff=bool(args.print_diff),
        no_verify=True,
        fail_on_warnings=bool(args.fail_on_warnings),
        fail_on_sync=bool(args.fail_on_sync),
        output_dir=str(output_dir / "sync"),
        report_json=str(output_dir / "sync.aggregate.report.json"),
    )
    sync_exit = command_sync(sync_args)
    if sync_exit != 0:
        return sync_exit

    verify_output_dir = output_dir / "verify"
    verify_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        output_dir=str(verify_output_dir),
        sarif_report=str(output_dir / "verify.aggregate.report.sarif.json"),
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    verify_exit = command_verify_all(verify_args)
    if verify_exit != 0:
        return verify_exit

    changelog_output = (
        Path(args.changelog_output).resolve()
        if args.changelog_output
        else (repo_root / "abi" / "CHANGELOG.md")
    )
    changelog_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        baseline=None,
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        title=args.title,
        release_tag=args.release_tag,
        output=str(changelog_output),
        report_json=str(output_dir / "changelog.aggregate.report.json"),
        sarif_report=str(output_dir / "changelog.aggregate.report.sarif.json"),
        fail_on_failing=True,
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    changelog_exit = command_changelog(changelog_args)
    if changelog_exit != 0:
        return changelog_exit

    manifest = {
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "release_tag": args.release_tag,
        "artifacts": {
            "output_dir": to_repo_relative(output_dir, repo_root),
            "verify_dir": to_repo_relative(verify_output_dir, repo_root),
            "changelog": to_repo_relative(changelog_output, repo_root),
            "sync_report": to_repo_relative(output_dir / "sync.aggregate.report.json", repo_root),
            "verify_sarif": to_repo_relative(output_dir / "verify.aggregate.report.sarif.json", repo_root),
            "changelog_report": to_repo_relative(output_dir / "changelog.aggregate.report.json", repo_root),
            "changelog_sarif": to_repo_relative(output_dir / "changelog.aggregate.report.sarif.json", repo_root),
        },
        "options": {
            "update_baselines": bool(args.update_baselines),
            "check_generated": bool(args.check_generated),
            "skip_binary": bool(args.skip_binary),
            "fail_on_warnings": bool(args.fail_on_warnings),
        },
        "status": "pass",
    }
    write_json(output_dir / "release.prepare.report.json", manifest)
    print(f"release-prepare completed: {output_dir}")
    return 0


def command_snapshot(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    snapshot = build_snapshot(
        config=config,
        target_name=args.target,
        repo_root=repo_root,
        binary_override=args.binary,
        skip_binary=args.skip_binary,
    )

    if args.output:
        write_json(Path(args.output).resolve(), snapshot)
    else:
        print(json.dumps(snapshot, indent=2, sort_keys=True))

    print(
        f"Snapshot created for target '{args.target}' with "
        f"{snapshot['header']['function_count']} header symbols, "
        f"{snapshot['header']['enum_count']} enums, "
        f"{snapshot['header']['struct_count']} structs, and "
        f"{(snapshot.get('bindings') or {}).get('symbol_count', 0)} binding symbols.",
        file=sys.stderr,
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())

    current = build_snapshot(
        config=config,
        target_name=args.target,
        repo_root=repo_root,
        binary_override=args.binary,
        skip_binary=args.skip_binary,
    )
    baseline = load_snapshot(Path(args.baseline).resolve())

    raw_report = compare_snapshots(baseline=baseline, current=current)
    effective_policy = resolve_effective_policy(config=config, target_name=str(args.target))
    report = apply_policy_to_report(
        report=raw_report,
        policy=effective_policy,
        target_name=str(args.target),
    )

    if args.current_output:
        write_json(Path(args.current_output).resolve(), current)
    if args.report:
        write_json(Path(args.report).resolve(), report)
    if args.markdown_report:
        write_markdown_report(Path(args.markdown_report).resolve(), report)
    if args.sarif_report:
        current_header = current.get("header")
        source_path = None
        if isinstance(current_header, dict):
            path_value = current_header.get("path")
            if isinstance(path_value, str):
                source_path = path_value
        sarif_results = build_sarif_results_for_target(
            target_name=str(args.target),
            report=report,
            source_path=source_path,
        )
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    print_report(report)
    status_ok = report.get("status") == "pass"
    effective_fail_on_warnings = bool(args.fail_on_warnings) or bool(effective_policy.get("fail_on_warnings", False))
    if status_ok and effective_fail_on_warnings:
        status_ok = not bool(get_message_list(report, "warnings"))
    return 0 if status_ok else 1


def command_diff(args: argparse.Namespace) -> int:
    baseline = load_snapshot(Path(args.baseline).resolve())
    current = load_snapshot(Path(args.current).resolve())
    report = compare_snapshots(baseline=baseline, current=current)

    if args.report:
        write_json(Path(args.report).resolve(), report)
    if args.markdown_report:
        write_markdown_report(Path(args.markdown_report).resolve(), report)
    if args.sarif_report:
        sarif_results = build_sarif_results_for_target(
            target_name="diff",
            report=report,
            source_path=None,
        )
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    print_report(report)
    status_ok = report.get("status") == "pass"
    if status_ok and bool(args.fail_on_warnings):
        status_ok = not bool(get_message_list(report, "warnings"))
    return 0 if status_ok else 1


def get_targets_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config is missing required object: 'targets'.")
    out: dict[str, dict[str, Any]] = {}
    for name, payload in targets.items():
        if isinstance(name, str) and isinstance(payload, dict):
            out[name] = payload
    if not out:
        raise AbiFrameworkError("Config has no valid targets.")
    return out


def command_list_targets(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    for name in sorted(targets.keys()):
        print(name)
    return 0


def resolve_baseline_for_target(repo_root: Path, config: dict[str, Any], target_name: str, baseline_root: str | None) -> Path:
    target = resolve_target(config, target_name)
    if baseline_root:
        return ensure_relative_path(repo_root, f"{baseline_root.rstrip('/')}/{target_name}.json").resolve()

    baseline_path = target.get("baseline_path")
    if isinstance(baseline_path, str) and baseline_path:
        return ensure_relative_path(repo_root, baseline_path).resolve()

    return ensure_relative_path(repo_root, f"abi/baselines/{target_name}.json").resolve()


def resolve_binary_for_target(repo_root: Path, config: dict[str, Any], target_name: str, binary_override: str | None) -> tuple[str | None, bool]:
    if binary_override:
        return binary_override, False
    target = resolve_target(config, target_name)
    binary_cfg = target.get("binary")
    if isinstance(binary_cfg, dict):
        path_value = binary_cfg.get("path")
        if isinstance(path_value, str) and path_value:
            return str(ensure_relative_path(repo_root, path_value).resolve()), False
    return None, True


def command_verify_all(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())

    targets = get_targets_map(config)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    final_status = 0
    aggregate: dict[str, Any] = {
        "status": "pass",
        "generated_at_utc": dt.datetime.now(tz=dt.timezone.utc).isoformat(),
        "results": {},
    }
    sarif_results: list[dict[str, Any]] = []

    for target_name in sorted(targets.keys()):
        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        if not baseline_path.exists():
            raise AbiFrameworkError(f"Baseline does not exist for target '{target_name}': {baseline_path}")

        current = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
        )
        baseline = load_snapshot(baseline_path)
        raw_report = compare_snapshots(baseline=baseline, current=current)
        effective_policy = resolve_effective_policy(config=config, target_name=target_name)
        report = apply_policy_to_report(
            report=raw_report,
            policy=effective_policy,
            target_name=target_name,
        )

        aggregate["results"][target_name] = report

        print(
            f"[{target_name}] {report.get('status')} "
            f"(classification={report.get('change_classification')}, required_bump={report.get('required_bump')})"
        )
        if report.get("status") != "pass":
            final_status = 1

        if output_dir:
            write_json(output_dir / f"{target_name}.current.json", current)
            write_json(output_dir / f"{target_name}.report.json", report)
            write_markdown_report(output_dir / f"{target_name}.report.md", report)

        current_header = current.get("header")
        source_path = None
        if isinstance(current_header, dict):
            path_value = current_header.get("path")
            if isinstance(path_value, str):
                source_path = path_value
        sarif_results.extend(
            build_sarif_results_for_target(
                target_name=target_name,
                report=report,
                source_path=source_path,
            )
        )

    aggregate["summary"] = build_aggregate_summary(aggregate["results"])

    if final_status != 0:
        aggregate["status"] = "fail"
    else:
        fail_on_warnings_global = bool(args.fail_on_warnings)
        fail_on_warnings_policy = any(
            bool((report.get("policy") or {}).get("fail_on_warnings", False))
            for report in aggregate["results"].values()
            if isinstance(report, dict)
        )
        if (fail_on_warnings_global or fail_on_warnings_policy) and aggregate["summary"]["warning_count"] > 0:
            final_status = 1
            aggregate["status"] = "fail"

    if final_status != 0 and aggregate["status"] != "fail":
        final_status = 1
        aggregate["status"] = "fail"

    if output_dir:
        write_json(output_dir / "aggregate.report.json", aggregate)
        write_sarif_report(output_dir / "aggregate.report.sarif.json", sarif_results)
    if args.sarif_report:
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    return final_status


def command_regen_baselines(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    regenerated: list[str] = []
    for target_name in sorted(targets.keys()):
        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        snapshot = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
        )
        write_json(baseline_path, snapshot)
        regenerated.append(f"{target_name} -> {baseline_path}")

    for line in regenerated:
        print(f"Regenerated baseline: {line}")

    if bool(args.verify):
        verify_args = argparse.Namespace(
            repo_root=str(repo_root),
            config=str(Path(args.config).resolve()),
            baseline_root=args.baseline_root,
            binary=args.binary,
            skip_binary=args.skip_binary,
            output_dir=args.output_dir,
            sarif_report=args.sarif_report,
            fail_on_warnings=args.fail_on_warnings,
        )
        return command_verify_all(verify_args)

    return 0


def command_doctor(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    issues: list[tuple[str, str, str]] = []
    schema_ok, schema_note = validate_with_jsonschema_if_available("config", config)
    if not schema_ok and schema_note:
        issues.append(("warning", "global", f"jsonschema fallback mode: {schema_note}"))

    for target_name in sorted(targets.keys()):
        target = targets[target_name]
        header_cfg = target.get("header")
        if not isinstance(header_cfg, dict):
            issues.append(("error", target_name, "missing header config"))
            continue

        header_path_value = header_cfg.get("path")
        if not isinstance(header_path_value, str) or not header_path_value:
            issues.append(("error", target_name, "header.path is missing"))
        else:
            header_path = ensure_relative_path(repo_root, header_path_value).resolve()
            if not header_path.exists():
                issues.append(("error", target_name, f"header file not found: {header_path}"))

        bindings_cfg = target.get("bindings")
        if bindings_cfg is not None:
            if not isinstance(bindings_cfg, dict):
                issues.append(("error", target_name, "bindings must be an object when specified"))
            else:
                expected_symbols = bindings_cfg.get("expected_symbols")
                if expected_symbols is not None:
                    if not isinstance(expected_symbols, list):
                        issues.append(("error", target_name, "bindings.expected_symbols must be an array"))
                    elif not expected_symbols:
                        issues.append(("warning", target_name, "bindings.expected_symbols is empty"))

        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        if not baseline_path.exists():
            severity = "error" if bool(args.require_baselines) else "warning"
            issues.append((severity, target_name, f"baseline missing: {baseline_path}"))

        binary_value, unresolved = resolve_binary_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
        )
        if not unresolved and binary_value:
            binary_path = Path(binary_value)
            if not binary_path.exists():
                severity = "error" if bool(args.require_binaries) else "warning"
                issues.append((severity, target_name, f"binary missing: {binary_path}"))
        elif bool(args.require_binaries):
            issues.append(("error", target_name, "binary path is not configured"))

        try:
            codegen_cfg = resolve_codegen_config(target=target, target_name=target_name, repo_root=repo_root)
            idl_output = codegen_cfg.get("idl_output_path")
            if not isinstance(idl_output, Path):
                idl_output = ensure_relative_path(repo_root, f"abi/generated/{target_name}.idl.json").resolve()
            if not idl_output.parent.exists():
                issues.append(("warning", target_name, f"IDL output parent does not exist yet: {idl_output.parent}"))

        except AbiFrameworkError as exc:
            issues.append(("error", target_name, f"codegen config invalid: {exc}"))

    error_count = sum(1 for sev, _, _ in issues if sev == "error")
    warning_count = sum(1 for sev, _, _ in issues if sev == "warning")

    if not issues:
        print("abi_framework doctor: healthy")
        return 0

    print("abi_framework doctor: issues found")
    for severity, target_name, message in issues:
        print(f"  [{severity}] {target_name}: {message}")

    if error_count > 0:
        return 1
    if bool(args.fail_on_warnings) and warning_count > 0:
        return 1
    return 0


def command_changelog(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    if args.baseline and not args.target:
        raise AbiFrameworkError("--baseline can only be used together with --target.")

    if args.target:
        if args.target not in targets:
            known = ", ".join(sorted(targets.keys()))
            raise AbiFrameworkError(f"Unknown target '{args.target}'. Known targets: {known}")
        target_names = [args.target]
    else:
        target_names = sorted(targets.keys())

    results_by_target: dict[str, dict[str, Any]] = {}
    sarif_results: list[dict[str, Any]] = []

    for target_name in target_names:
        if args.baseline and args.target:
            baseline_path = Path(args.baseline).resolve()
        else:
            baseline_path = resolve_baseline_for_target(
                repo_root=repo_root,
                config=config,
                target_name=target_name,
                baseline_root=args.baseline_root,
            )
        if not baseline_path.exists():
            raise AbiFrameworkError(f"Baseline does not exist for target '{target_name}': {baseline_path}")

        current = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
        )
        baseline = load_snapshot(baseline_path)
        raw_report = compare_snapshots(baseline=baseline, current=current)
        effective_policy = resolve_effective_policy(config=config, target_name=target_name)
        report = apply_policy_to_report(
            report=raw_report,
            policy=effective_policy,
            target_name=target_name,
        )
        results_by_target[target_name] = report

        current_header = current.get("header")
        source_path = None
        if isinstance(current_header, dict):
            path_value = current_header.get("path")
            if isinstance(path_value, str):
                source_path = path_value
        sarif_results.extend(
            build_sarif_results_for_target(
                target_name=target_name,
                report=report,
                source_path=source_path,
            )
        )

    generated_at_utc = dt.datetime.now(tz=dt.timezone.utc).isoformat()
    changelog = render_changelog_document(
        title=str(args.title),
        release_tag=args.release_tag,
        generated_at_utc=generated_at_utc,
        results_by_target=results_by_target,
    )

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(changelog, encoding="utf-8")
        print(f"Wrote changelog: {output_path}")
    else:
        print(changelog, end="")

    aggregate = {
        "generated_at_utc": generated_at_utc,
        "results": results_by_target,
        "summary": build_aggregate_summary(results_by_target),
    }

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    if args.sarif_report:
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    has_failing = any(report.get("status") != "pass" for report in results_by_target.values())
    has_warnings = aggregate["summary"]["warning_count"] > 0
    if bool(args.fail_on_failing) and has_failing:
        return 1
    if bool(args.fail_on_warnings) and has_warnings:
        return 1
    return 0


def command_init_target(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve()

    if config_path.exists():
        config = load_config(config_path)
    else:
        config = {"targets": {}}

    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config root must contain object 'targets'.")

    if args.target in targets and not args.force:
        raise AbiFrameworkError(
            f"Target '{args.target}' already exists in config. Use --force to overwrite."
        )

    baseline_rel = args.baseline_path or f"abi/baselines/{args.target}.json"

    target_entry: dict[str, Any] = {
        "baseline_path": baseline_rel,
        "header": {
            "path": args.header_path,
            "api_macro": args.api_macro,
            "call_macro": args.call_macro,
            "symbol_prefix": args.symbol_prefix,
            "version_macros": {
                "major": args.version_major_macro,
                "minor": args.version_minor_macro,
                "patch": args.version_patch_macro,
            },
            "types": {
                "enable_enums": True,
                "enable_structs": True,
                "enum_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "struct_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "ignore_enums": [],
                "ignore_structs": [],
                "struct_tail_addition_is_breaking": True,
            },
        },
        "codegen": {
            "enabled": True,
            "idl_output_path": f"abi/generated/{args.target}.idl.json",
        },
    }

    if args.binding_symbol:
        target_entry["bindings"] = {
            "expected_symbols": args.binding_symbol,
        }

    if args.binary_path:
        target_entry["binary"] = {
            "path": args.binary_path,
            "allow_non_prefixed_exports": False,
        }

    targets[args.target] = target_entry
    config["targets"] = targets
    write_json(config_path, config)

    if args.create_baseline:
        snapshot = build_snapshot(
            config=config,
            target_name=args.target,
            repo_root=repo_root,
            binary_override=None,
            skip_binary=True,
        )
        baseline_path = ensure_relative_path(repo_root, baseline_rel).resolve()
        write_json(baseline_path, snapshot)
        print(f"Created baseline: {baseline_path}")

    print(f"Target '{args.target}' initialized in {config_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abi_framework",
        description="Config-driven ABI governance framework (snapshot/verify/diff/bootstrap/release).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="Generate current ABI snapshot.")
    snapshot.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    snapshot.add_argument("--config", required=True, help="Path to ABI config JSON.")
    snapshot.add_argument("--target", required=True, help="Target name from config targets map.")
    snapshot.add_argument("--binary", help="Override binary path for export checks.")
    snapshot.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    snapshot.add_argument("--output", help="Write snapshot JSON to path.")
    snapshot.set_defaults(func=command_snapshot)

    verify = sub.add_parser("verify", help="Compare current ABI state with baseline snapshot.")
    verify.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    verify.add_argument("--config", required=True, help="Path to ABI config JSON.")
    verify.add_argument("--target", required=True, help="Target name from config targets map.")
    verify.add_argument("--baseline", required=True, help="Path to baseline snapshot JSON.")
    verify.add_argument("--binary", help="Override binary path for export checks.")
    verify.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    verify.add_argument("--current-output", help="Write current snapshot JSON to path.")
    verify.add_argument("--report", help="Write verify report JSON to path.")
    verify.add_argument("--markdown-report", help="Write verify report as Markdown.")
    verify.add_argument("--sarif-report", help="Write verify report as SARIF (for CI/code scanning).")
    verify.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    verify.set_defaults(func=command_verify)

    verify_all = sub.add_parser("verify-all", help="Verify all targets from config.")
    verify_all.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    verify_all.add_argument("--config", required=True, help="Path to ABI config JSON.")
    verify_all.add_argument("--baseline-root", help="Baseline directory (default: target baseline_path or abi/baselines).")
    verify_all.add_argument("--binary", help="Override binary path for export checks (applies to each target).")
    verify_all.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction for all targets.")
    verify_all.add_argument("--output-dir", help="Directory to write per-target current/report artifacts.")
    verify_all.add_argument("--sarif-report", help="Write aggregate SARIF report.")
    verify_all.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    verify_all.set_defaults(func=command_verify_all)

    regen = sub.add_parser("regen-baselines", help="Regenerate baseline snapshots for all targets.")
    regen.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    regen.add_argument("--config", required=True, help="Path to ABI config JSON.")
    regen.add_argument("--baseline-root", help="Baseline directory override (otherwise target baseline_path is used).")
    regen.add_argument("--binary", help="Override binary path for export checks.")
    regen.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction while regenerating.")
    regen.add_argument("--verify", action="store_true", help="Run verify-all after regeneration.")
    regen.add_argument("--output-dir", help="Verification output dir (effective with --verify).")
    regen.add_argument("--sarif-report", help="Verification SARIF output (effective with --verify).")
    regen.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures during --verify.")
    regen.set_defaults(func=command_regen_baselines)

    doctor = sub.add_parser("doctor", help="Run ABI environment/config diagnostics.")
    doctor.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    doctor.add_argument("--config", required=True, help="Path to ABI config JSON.")
    doctor.add_argument("--baseline-root", help="Baseline directory override.")
    doctor.add_argument("--binary", help="Override binary path for checks.")
    doctor.add_argument("--require-baselines", action="store_true", help="Fail if any baseline is missing.")
    doctor.add_argument("--require-binaries", action="store_true", help="Fail if any binary is missing/unconfigured.")
    doctor.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    doctor.set_defaults(func=command_doctor)

    changelog = sub.add_parser("changelog", help="Generate markdown ABI changelog from baseline vs current.")
    changelog.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    changelog.add_argument("--config", required=True, help="Path to ABI config JSON.")
    changelog.add_argument("--target", help="Optional target name. If omitted, all targets are included.")
    changelog.add_argument("--baseline", help="Explicit baseline path (only valid with --target).")
    changelog.add_argument("--baseline-root", help="Baseline directory override.")
    changelog.add_argument("--binary", help="Override binary path for export checks.")
    changelog.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    changelog.add_argument("--title", default="ABI Changelog", help="Changelog title.")
    changelog.add_argument("--release-tag", help="Optional release tag (display only).")
    changelog.add_argument("--output", help="Write markdown changelog to file.")
    changelog.add_argument("--report-json", help="Write aggregate changelog data as JSON.")
    changelog.add_argument("--sarif-report", help="Write SARIF for changelog diagnostics.")
    changelog.add_argument("--fail-on-failing", action="store_true", help="Return non-zero if any target report failed.")
    changelog.add_argument("--fail-on-warnings", action="store_true", help="Return non-zero if warnings are present.")
    changelog.set_defaults(func=command_changelog)

    generate = sub.add_parser("generate", help="Generate ABI IDL artifacts.")
    generate.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    generate.add_argument("--config", required=True, help="Path to ABI config JSON.")
    generate.add_argument("--target", help="Optional target name. If omitted, all targets are processed.")
    generate.add_argument("--binary", help="Override binary path for export checks.")
    generate.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    generate.add_argument("--idl-output", help="IDL output path (single-target mode only).")
    generate.add_argument("--dry-run", action="store_true", help="Do not write files; only compute outputs.")
    generate.add_argument("--check", action="store_true", help="Fail when generated artifacts drift from files on disk.")
    generate.add_argument("--print-diff", action="store_true", help="Print unified diff for changed artifacts.")
    generate.add_argument("--report-json", help="Write aggregate generation report JSON.")
    generate.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbols.")
    generate.set_defaults(func=command_generate)

    sync = sub.add_parser("sync", help="Sync generated ABI artifacts and optionally baselines.")
    sync.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    sync.add_argument("--config", required=True, help="Path to ABI config JSON.")
    sync.add_argument("--target", help="Optional target name. If omitted, all targets are processed.")
    sync.add_argument("--baseline-root", help="Baseline directory override.")
    sync.add_argument("--binary", help="Override binary path for export checks.")
    sync.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    sync.add_argument("--update-baselines", action="store_true", help="Write current snapshots to baseline files.")
    sync.add_argument("--check", action="store_true", help="Check mode: do not write files and fail on drift.")
    sync.add_argument("--print-diff", action="store_true", help="Print unified diff for changed artifacts.")
    sync.add_argument("--no-verify", action="store_true", help="Skip baseline comparison and policy checks.")
    sync.add_argument("--fail-on-warnings", action="store_true", help="Treat ABI warnings as failures.")
    sync.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbols.")
    sync.add_argument("--output-dir", help="Directory for sync reports.")
    sync.add_argument("--report-json", help="Write aggregate sync report JSON.")
    sync.set_defaults(func=command_sync)

    release_prepare = sub.add_parser("release-prepare", help="Run end-to-end ABI release preparation pipeline.")
    release_prepare.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    release_prepare.add_argument("--config", required=True, help="Path to ABI config JSON.")
    release_prepare.add_argument("--baseline-root", help="Baseline directory override.")
    release_prepare.add_argument("--binary", help="Override binary path for export checks.")
    release_prepare.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    release_prepare.add_argument("--require-binaries", action="store_true", help="Require binaries to be present during doctor checks.")
    release_prepare.add_argument("--update-baselines", action="store_true", help="Refresh baselines before verification.")
    release_prepare.add_argument("--check-generated", action="store_true", help="Fail if generated artifacts are out of date.")
    release_prepare.add_argument("--print-diff", action="store_true", help="Print unified diff for generated artifact drift.")
    release_prepare.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbols.")
    release_prepare.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    release_prepare.add_argument("--release-tag", help="Release tag displayed in changelog/report.")
    release_prepare.add_argument("--title", default="ABI Changelog", help="Changelog title.")
    release_prepare.add_argument("--changelog-output", help="Path for markdown changelog output.")
    release_prepare.add_argument("--output-dir", help="Directory for release preparation artifacts.")
    release_prepare.set_defaults(func=command_release_prepare)

    diff = sub.add_parser("diff", help="Compare two snapshot files.")
    diff.add_argument("--baseline", required=True, help="Path to baseline snapshot JSON.")
    diff.add_argument("--current", required=True, help="Path to current snapshot JSON.")
    diff.add_argument("--report", help="Write diff report JSON to path.")
    diff.add_argument("--markdown-report", help="Write diff report as Markdown.")
    diff.add_argument("--sarif-report", help="Write diff report as SARIF.")
    diff.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    diff.set_defaults(func=command_diff)

    list_targets = sub.add_parser("list-targets", help="List target names from config.")
    list_targets.add_argument("--config", required=True, help="Path to ABI config JSON.")
    list_targets.set_defaults(func=command_list_targets)

    init_target = sub.add_parser("init-target", help="Bootstrap a new ABI target in config and create baseline.")
    init_target.add_argument("--repo-root", default=".", help="Repository root for relative path resolution.")
    init_target.add_argument("--config", required=True, help="Path to ABI config JSON.")
    init_target.add_argument("--target", required=True, help="Target name to initialize.")
    init_target.add_argument("--header-path", required=True, help="Header path relative to repo root.")
    init_target.add_argument("--api-macro", default="LUMENRTC_API", help="Export macro used in ABI declarations.")
    init_target.add_argument("--call-macro", default="LUMENRTC_CALL", help="Calling convention macro used in ABI declarations.")
    init_target.add_argument("--symbol-prefix", default="lrtc_", help="ABI symbol prefix (for function export matching).")
    init_target.add_argument("--version-major-macro", required=True, help="ABI major version macro name.")
    init_target.add_argument("--version-minor-macro", required=True, help="ABI minor version macro name.")
    init_target.add_argument("--version-patch-macro", required=True, help="ABI patch version macro name.")
    init_target.add_argument("--binding-symbol", action="append", help="Optional expected binding symbol (repeatable).")
    init_target.add_argument("--binary-path", help="Optional native binary path.")
    init_target.add_argument("--baseline-path", help="Baseline path (default abi/baselines/<target>.json).")
    init_target.add_argument("--no-create-baseline", action="store_true", help="Do not create baseline immediately.")
    init_target.add_argument("--force", action="store_true", help="Overwrite target if already exists.")
    init_target.set_defaults(func=command_init_target)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "no_create_baseline"):
        args.create_baseline = not bool(args.no_create_baseline)

    try:
        return int(args.func(args))
    except AbiFrameworkError as exc:
        print(f"abi_framework error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
