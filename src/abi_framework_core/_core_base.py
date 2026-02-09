#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import datetime as dt
import difflib
import glob
import hashlib
import html
import json
import tempfile
import os
import re
import shutil
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOL_VERSION = "1.0.0"
IDL_SCHEMA_VERSION = 1
IDL_SCHEMA_URI_V1 = "https://lumenrtc.dev/abi_framework/idl.schema.v1.json"
ATTESTATION_PREDICATE_TYPE = "https://slsa.dev/provenance/v1"
ATTESTATION_BUILD_TYPE = "https://lumenrtc.dev/abi_framework/release-prepare@v1"
DEFAULT_WAIVER_REQUIREMENTS = {
    "require_owner": False,
    "require_reason": False,
    "require_expires_utc": False,
    "require_approved_by": False,
    "require_ticket": False,
    "max_ttl_days": None,
    "warn_expiring_within_days": 30,
}


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


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    enabled: bool
    severity: str
    message: str
    when: dict[str, Any]


@dataclass(frozen=True)
class PolicyWaiver:
    waiver_id: str
    target_patterns: tuple[re.Pattern[str], ...]
    severity: str
    message_pattern: re.Pattern[str]
    expires_utc: str | None
    created_utc: str | None
    owner: str | None
    reason: str | None
    approved_by: str | None
    ticket: str | None


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_c_decl_attributes(value: str) -> str:
    text = value

    def _strip_balanced_macro_calls(payload: str, token_pattern: str) -> str:
        out = payload
        token_re = re.compile(token_pattern)
        while True:
            match = token_re.search(out)
            if not match:
                break
            open_idx = out.find("(", match.end())
            if open_idx < 0:
                out = f"{out[:match.start()]} {out[match.end():]}"
                continue
            depth = 0
            end_idx = None
            for idx in range(open_idx, len(out)):
                ch = out[idx]
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        end_idx = idx + 1
                        break
            if end_idx is None:
                out = f"{out[:match.start()]} {out[match.end():]}"
                continue
            out = f"{out[:match.start()]} {out[end_idx:]}"
        return out

    text = _strip_balanced_macro_calls(text, r"\b__attribute__\b")
    text = _strip_balanced_macro_calls(text, r"\b__declspec\b")
    text = re.sub(r"\b(?:__cdecl|__stdcall|__fastcall|__vectorcall|__thiscall)\b", " ", text)
    return normalize_ws(text)


def sanitize_c_decl_text(value: str) -> str:
    text = strip_c_decl_attributes(value)
    text = re.sub(r"\b_Bool\b", "bool", text)
    return normalize_ws(text)


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
    base = Path(__file__).resolve().parents[2] / "schemas"
    mapping = {
        "config": base / "config.schema.json",
        "snapshot": base / "snapshot.schema.json",
        "report": base / "report.schema.json",
        "idl_v1": base / "idl.schema.v1.json",
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


def validate_policy_object(policy: dict[str, Any], label: str) -> None:
    classification = policy.get("max_allowed_classification")
    if classification is not None and classification not in {"none", "additive", "breaking"}:
        raise AbiFrameworkError(f"{label}.max_allowed_classification must be none/additive/breaking")
    for bool_key in ["fail_on_warnings", "require_layout_probe"]:
        value = policy.get(bool_key)
        if value is not None and not isinstance(value, bool):
            raise AbiFrameworkError(f"{label}.{bool_key} must be boolean when specified")
    rules_value = policy.get("rules")
    if rules_value is not None and not isinstance(rules_value, list):
        raise AbiFrameworkError(f"{label}.rules must be an array when specified")
    waivers_value = policy.get("waivers")
    if waivers_value is not None and not isinstance(waivers_value, list):
        raise AbiFrameworkError(f"{label}.waivers must be an array when specified")

    requirements = policy.get("waiver_requirements")
    if requirements is not None:
        if not isinstance(requirements, dict):
            raise AbiFrameworkError(f"{label}.waiver_requirements must be an object when specified")
        for key in [
            "require_owner",
            "require_reason",
            "require_expires_utc",
            "require_approved_by",
            "require_ticket",
        ]:
            value = requirements.get(key)
            if value is not None and not isinstance(value, bool):
                raise AbiFrameworkError(f"{label}.waiver_requirements.{key} must be boolean when specified")
        for key in ["max_ttl_days", "warn_expiring_within_days"]:
            value = requirements.get(key)
            if value is not None and (not isinstance(value, int) or value < 0):
                raise AbiFrameworkError(f"{label}.waiver_requirements.{key} must be a non-negative integer")


def validate_config_payload(payload: dict[str, Any]) -> None:
    if not isinstance(payload, dict):
        raise AbiFrameworkError("config root must be an object")

    root_policy = payload.get("policy")
    if root_policy is not None and not isinstance(root_policy, dict):
        raise AbiFrameworkError("config.policy must be an object when specified")
    if isinstance(root_policy, dict):
        validate_policy_object(root_policy, "config.policy")
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
        parser_cfg = header.get("parser")
        if parser_cfg is not None:
            if not isinstance(parser_cfg, dict):
                raise AbiFrameworkError(f"target '{target_name}'.header.parser must be an object when specified")
            backend = parser_cfg.get("backend")
            if backend is not None and backend not in {"regex", "clang_preprocess"}:
                raise AbiFrameworkError(
                    f"target '{target_name}'.header.parser.backend must be regex or clang_preprocess"
                )
            for key in ["compiler"]:
                value = parser_cfg.get(key)
                if value is not None and (not isinstance(value, str) or not value):
                    raise AbiFrameworkError(
                        f"target '{target_name}'.header.parser.{key} must be a non-empty string when specified"
                    )
            for key in ["args", "include_dirs", "compiler_candidates"]:
                value = parser_cfg.get(key)
                if value is not None:
                    if not isinstance(value, list):
                        raise AbiFrameworkError(
                            f"target '{target_name}'.header.parser.{key} must be an array when specified"
                        )
                    for idx, item in enumerate(value):
                        if not isinstance(item, str) or not item:
                            raise AbiFrameworkError(
                                f"target '{target_name}'.header.parser.{key}[{idx}] must be a non-empty string"
                            )
            fallback = parser_cfg.get("fallback_to_regex")
            if fallback is not None and not isinstance(fallback, bool):
                raise AbiFrameworkError(
                    f"target '{target_name}'.header.parser.fallback_to_regex must be boolean when specified"
                )
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
            symbol_docs = bindings.get("symbol_docs")
            if symbol_docs is not None:
                if not isinstance(symbol_docs, dict):
                    raise AbiFrameworkError(f"target '{target_name}'.bindings.symbol_docs must be an object")
                for key, value in symbol_docs.items():
                    if not isinstance(key, str) or not key:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.bindings.symbol_docs keys must be non-empty strings"
                        )
                    if not isinstance(value, str):
                        raise AbiFrameworkError(
                            f"target '{target_name}'.bindings.symbol_docs['{key}'] must be string"
                        )
            deprecated_symbols = bindings.get("deprecated_symbols")
            if deprecated_symbols is not None:
                if not isinstance(deprecated_symbols, list):
                    raise AbiFrameworkError(f"target '{target_name}'.bindings.deprecated_symbols must be an array")
                for idx, item in enumerate(deprecated_symbols):
                    if not isinstance(item, str) or not item:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.bindings.deprecated_symbols[{idx}] must be non-empty string"
                        )
            interop_metadata_path = bindings.get("interop_metadata_path")
            if interop_metadata_path is not None:
                if not isinstance(interop_metadata_path, str) or not interop_metadata_path:
                    raise AbiFrameworkError(
                        f"target '{target_name}'.bindings.interop_metadata_path must be a non-empty string"
                    )
            metadata_path = bindings.get("metadata_path")
            if metadata_path is not None:
                if not isinstance(metadata_path, str) or not metadata_path:
                    raise AbiFrameworkError(
                        f"target '{target_name}'.bindings.metadata_path must be a non-empty string"
                    )
            interop_metadata = bindings.get("interop_metadata")
            if interop_metadata is not None and not isinstance(interop_metadata, dict):
                raise AbiFrameworkError(
                    f"target '{target_name}'.bindings.interop_metadata must be an object when specified"
                )
            metadata = bindings.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise AbiFrameworkError(
                    f"target '{target_name}'.bindings.metadata must be an object when specified"
                )
            generators = bindings.get("generators")
            if generators is not None:
                if not isinstance(generators, list):
                    raise AbiFrameworkError(f"target '{target_name}'.bindings.generators must be an array")
                for idx, item in enumerate(generators):
                    if not isinstance(item, dict):
                        raise AbiFrameworkError(
                            f"target '{target_name}'.bindings.generators[{idx}] must be object"
                        )

        target_policy = target.get("policy")
        if target_policy is not None:
            if not isinstance(target_policy, dict):
                raise AbiFrameworkError(f"target '{target_name}'.policy must be an object when specified")
            validate_policy_object(target_policy, f"target '{target_name}'.policy")

        codegen = target.get("codegen")
        if codegen is not None:
            if not isinstance(codegen, dict):
                raise AbiFrameworkError(f"target '{target_name}'.codegen must be an object when specified")
            string_fields = [
                "idl_output_path",
                "native_header_output_path",
                "native_export_map_output_path",
                "native_header_guard",
                "native_api_macro",
                "native_call_macro",
            ]
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
            idl_schema_version_value = codegen.get("idl_schema_version")
            if idl_schema_version_value is not None:
                if not isinstance(idl_schema_version_value, int):
                    raise AbiFrameworkError(
                        f"target '{target_name}'.codegen.idl_schema_version must be integer when specified"
                    )
                if idl_schema_version_value != IDL_SCHEMA_VERSION:
                    raise AbiFrameworkError(
                        f"target '{target_name}'.codegen.idl_schema_version={idl_schema_version_value} is not supported; "
                        f"only {IDL_SCHEMA_VERSION} is supported"
                    )
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
            native_constants = codegen.get("native_constants")
            if native_constants is not None:
                if not isinstance(native_constants, dict):
                    raise AbiFrameworkError(
                        f"target '{target_name}'.codegen.native_constants must be an object when specified"
                    )
                for key, value in native_constants.items():
                    if not isinstance(key, str) or not key:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.codegen.native_constants keys must be non-empty strings"
                        )
                    if not isinstance(value, str) or not value:
                        raise AbiFrameworkError(
                            f"target '{target_name}'.codegen.native_constants['{key}'] must be non-empty string"
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


def validate_idl_payload(payload: dict[str, Any], label: str) -> None:
    if not isinstance(payload, dict):
        raise AbiFrameworkError(f"{label} root must be an object")
    schema_version = payload.get("idl_schema_version", IDL_SCHEMA_VERSION)
    if schema_version != IDL_SCHEMA_VERSION:
        raise AbiFrameworkError(
            f"{label} uses unsupported idl_schema_version={schema_version}; only {IDL_SCHEMA_VERSION} is supported"
        )
    validate_with_jsonschema_if_available("idl_v1", payload)


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


def parse_utc_timestamp(value: str) -> dt.datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def now_utc() -> dt.datetime:
    return dt.datetime.now(tz=dt.timezone.utc)


def utc_timestamp_now() -> str:
    return now_utc().isoformat()


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


def resolve_header_parser_config(header_cfg: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    raw = header_cfg.get("parser")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AbiFrameworkError("Target field 'header.parser' must be an object when specified.")

    backend = str(raw.get("backend", "regex")).strip().lower()
    if backend not in {"regex", "clang_preprocess"}:
        raise AbiFrameworkError(
            "Target field 'header.parser.backend' must be one of: regex, clang_preprocess."
        )

    compiler_value = raw.get("compiler")
    compiler = str(compiler_value).strip() if isinstance(compiler_value, str) and compiler_value.strip() else None
    compiler_candidates = normalize_string_list(
        raw.get("compiler_candidates"),
        "header.parser.compiler_candidates",
    )
    parser_args = normalize_string_list(raw.get("args"), "header.parser.args")
    include_dirs_raw = normalize_string_list(raw.get("include_dirs"), "header.parser.include_dirs")
    include_dirs = [str(ensure_relative_path(repo_root, item).resolve()) for item in include_dirs_raw]

    return {
        "backend": backend,
        "compiler": compiler,
        "compiler_candidates": compiler_candidates,
        "args": parser_args,
        "include_dirs": include_dirs,
        "fallback_to_regex": bool(raw.get("fallback_to_regex", True)),
    }


def _dedupe_non_empty_strings(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = raw.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _default_clang_compiler_candidates() -> list[str]:
    candidates: list[str] = []

    for env_key in ["ABI_CLANG", "LLVM_CLANG", "CC"]:
        value = os.environ.get(env_key)
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())

    if os.name == "nt":
        llvm_home = os.environ.get("LLVM_HOME")
        if isinstance(llvm_home, str) and llvm_home.strip():
            candidates.append(str(Path(llvm_home.strip()) / "bin" / "clang.exe"))
        program_files = os.environ.get("ProgramFiles")
        if isinstance(program_files, str) and program_files.strip():
            candidates.append(str(Path(program_files.strip()) / "LLVM" / "bin" / "clang.exe"))
        candidates.extend(
            [
                "clang",
                "clang.exe",
                "clang++",
                "clang++.exe",
                "clang-cl",
                "clang-cl.exe",
            ]
        )
    else:
        candidates.extend(
            [
                "clang",
                "clang-20",
                "clang-19",
                "clang-18",
                "clang-17",
                "clang-16",
                "clang-15",
                "clang-14",
                "clang++",
                "clang++-20",
                "clang++-19",
                "clang++-18",
                "clang++-17",
                "clang++-16",
            ]
        )
    return _dedupe_non_empty_strings(candidates)


def default_parser_compiler_candidates_for_config() -> list[str]:
    return [
        "clang",
        "clang-20",
        "clang-19",
        "clang-18",
        "clang-17",
        "clang-16",
        "clang++",
        "clang-cl",
        "clang.exe",
    ]


def _resolve_executable_candidate(candidate: str) -> str | None:
    expanded = os.path.expanduser(os.path.expandvars(candidate.strip()))
    if not expanded:
        return None

    # Explicit path (absolute or relative with separators).
    if any(sep in expanded for sep in ["/", "\\"]):
        candidate_path = Path(expanded)
        if candidate_path.exists():
            return str(candidate_path)
        return None

    return shutil.which(expanded)


def resolve_parser_compiler(parser_cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    explicit = parser_cfg.get("compiler")
    explicit_compiler = str(explicit).strip() if isinstance(explicit, str) and explicit.strip() else None
    raw_candidates = parser_cfg.get("compiler_candidates")
    configured_candidates = [str(item).strip() for item in raw_candidates] if isinstance(raw_candidates, list) else []

    candidate_sources: list[str] = []
    if explicit_compiler:
        candidate_sources.append(explicit_compiler)
    candidate_sources.extend(configured_candidates)
    candidate_sources.extend(_default_clang_compiler_candidates())

    candidates = _dedupe_non_empty_strings(candidate_sources)
    if not candidates:
        raise AbiFrameworkError(
            "header.parser compiler candidates are empty. Configure header.parser.compiler "
            "or header.parser.compiler_candidates."
        )

    for candidate in candidates:
        resolved = _resolve_executable_candidate(candidate)
        if resolved:
            return resolved, {
                "compiler_requested": explicit_compiler,
                "compiler_selected": candidate,
                "compiler_candidates": candidates,
            }

    raise AbiFrameworkError(
        "header.parser compiler not found; tried: "
        + ", ".join(candidates)
        + ". Configure header.parser.compiler/header.parser.compiler_candidates "
        + "or set ABI_CLANG."
    )


def preprocess_header_for_parsing(header_path: Path, parser_cfg: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    args = parser_cfg.get("args")
    include_dirs = parser_cfg.get("include_dirs")
    parser_args = [str(item) for item in args] if isinstance(args, list) else []
    include_values = [str(item) for item in include_dirs] if isinstance(include_dirs, list) else []

    compiler_resolved, compiler_meta = resolve_parser_compiler(parser_cfg)
    compiler_basename = Path(compiler_resolved).name.lower()

    if compiler_basename in {"clang-cl", "clang-cl.exe"}:
        command: list[str] = [compiler_resolved, "/EP", "/nologo", "/TC", str(header_path)]
        for include_dir in include_values:
            command.extend(["/I", include_dir])
        command.extend(parser_args)
    else:
        command = [compiler_resolved, "-E", "-P", "-x", "c", "-std=c11", str(header_path)]
        for include_dir in include_values:
            command.extend(["-I", include_dir])
        command.extend(parser_args)

    start = time.perf_counter()
    try:
        proc = subprocess.run(command, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as exc:
        message = exc.stderr.strip() or exc.stdout.strip() or "unknown parser error"
        raise AbiFrameworkError(
            "header.parser backend 'clang_preprocess' failed. "
            f"command={' '.join(shlex.quote(item) for item in command)}; error={message}"
        ) from exc
    elapsed_ms = round((time.perf_counter() - start) * 1000.0, 3)

    metadata = {
        "backend": "clang_preprocess",
        "compiler_resolved": compiler_resolved,
        "command": " ".join(shlex.quote(item) for item in command),
        "elapsed_ms": elapsed_ms,
    }
    metadata.update(compiler_meta)
    return proc.stdout, metadata


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
    decl = sanitize_c_decl_text(decl)

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


def extract_opaque_struct_typedefs(content: str, symbol_prefix: str) -> list[dict[str, str]]:
    pattern = re.compile(
        r"typedef\s+struct\s+(?P<tag>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*;"
    )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(content):
        tag = match.group("tag")
        name = match.group("name")
        if tag != name:
            continue
        if not name.startswith(symbol_prefix):
            continue
        if not name.endswith("_t"):
            continue
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "declaration": normalize_ws(match.group(0)),
            }
        )
    return out


def extract_callback_typedefs(content: str, symbol_prefix: str, call_macro: str) -> list[dict[str, str]]:
    name_pattern = rf"{re.escape(symbol_prefix)}[A-Za-z0-9_]*_cb"
    pattern = re.compile(
        rf"typedef\s+[^;]*?\(\s*{re.escape(call_macro)}\s*\*\s*(?P<name>{name_pattern})\s*\)\s*\([^;]*?\)\s*;",
        flags=re.S,
    )
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(content):
        name = match.group("name")
        if name in seen:
            continue
        seen.add(name)
        out.append(
            {
                "name": name,
                "declaration": normalize_ws(match.group(0)),
            }
        )
    return out


def extract_prefixed_define_constants(content: str, macro_prefix: str) -> dict[str, str]:
    pattern = re.compile(
        r"^\s*#\s*define\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s+(?P<value>.+?)\s*$",
        flags=re.M,
    )
    constants: dict[str, str] = {}
    for match in pattern.finditer(content):
        name = match.group("name")
        if not name.startswith(macro_prefix):
            continue
        value = normalize_ws(match.group("value"))
        if not value:
            continue
        constants[name] = value
    return {name: constants[name] for name in sorted(constants.keys())}


def parse_c_header(
    header_path: Path,
    api_macro: str,
    call_macro: str,
    symbol_prefix: str,
    version_macros: dict[str, str],
    type_policy: TypePolicy,
    parser_cfg: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], AbiVersion, dict[str, Any]]:
    try:
        raw = header_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AbiFrameworkError(f"Unable to read header '{header_path}': {exc}") from exc

    parser_cfg_value = parser_cfg if isinstance(parser_cfg, dict) else {"backend": "regex", "fallback_to_regex": True}
    backend_requested = str(parser_cfg_value.get("backend", "regex")).strip().lower()
    fallback_to_regex = bool(parser_cfg_value.get("fallback_to_regex", True))

    parser_info: dict[str, Any] = {
        "backend_requested": backend_requested,
        "backend": backend_requested,
        "fallback_used": False,
        "details": {},
    }

    content_for_versions = strip_c_comments(raw)
    major = extract_define_int(content_for_versions, version_macros["major"])
    minor = extract_define_int(content_for_versions, version_macros["minor"])
    patch = extract_define_int(content_for_versions, version_macros["patch"])

    declaration_source = raw
    if backend_requested == "clang_preprocess":
        try:
            preprocessed, metadata = preprocess_header_for_parsing(header_path=header_path, parser_cfg=parser_cfg_value)
            declaration_source = preprocessed
            parser_info["details"] = metadata
        except AbiFrameworkError as exc:
            if not fallback_to_regex:
                raise
            parser_info["backend"] = "regex"
            parser_info["fallback_used"] = True
            parser_info["details"] = {
                "fallback_reason": str(exc),
            }
            declaration_source = raw

    content = strip_c_comments(declaration_source)
    declaration_content = re.sub(r"^\s*#.*?$", "", content, flags=re.M)

    if parser_info.get("backend") == "clang_preprocess":
        pattern = re.compile(
            rf"(?P<ret>[^;\n][^;]*?)\s+(?P<name>{re.escape(symbol_prefix)}[A-Za-z0-9_]*)\s*\((?P<params>.*?)\)\s*;",
            flags=re.S,
        )
        parser_info["parse_mode"] = "prefix_symbols_from_preprocessed_header"
    else:
        pattern = re.compile(
            rf"{re.escape(api_macro)}\s+(?P<ret>.*?)\s+{re.escape(call_macro)}\s+(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<params>.*?)\)\s*;",
            flags=re.S,
        )
        parser_info["parse_mode"] = "api_call_macro_match"

    functions: dict[str, dict[str, str]] = {}
    for match in pattern.finditer(declaration_content):
        name = match.group("name")
        if symbol_prefix and not name.startswith(symbol_prefix):
            continue
        return_type = sanitize_c_decl_text(match.group("ret"))
        return_type = re.sub(r"^\s*extern\s+", "", return_type)
        params = sanitize_c_decl_text(match.group("params"))
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

    enums = parse_enum_blocks(content=declaration_content, policy=type_policy)
    structs = parse_struct_blocks(content=declaration_content, policy=type_policy)
    raw_without_comments = strip_c_comments(raw)
    opaque_entries = extract_opaque_struct_typedefs(content=raw_without_comments, symbol_prefix=symbol_prefix)
    callback_typedefs = extract_callback_typedefs(
        content=raw_without_comments,
        symbol_prefix=symbol_prefix,
        call_macro=call_macro,
    )
    constants = extract_prefixed_define_constants(
        content=raw_without_comments,
        macro_prefix=symbol_prefix.upper(),
    )

    header_payload = {
        "path": str(header_path),
        "function_count": len(functions),
        "symbols": sorted(functions.keys()),
        "functions": {name: functions[name] for name in sorted(functions.keys())},
        "enum_count": len(enums),
        "enums": enums,
        "struct_count": len(structs),
        "structs": structs,
        "opaque_types": [item["name"] for item in opaque_entries],
        "opaque_type_declarations": [item["declaration"] for item in opaque_entries],
        "callback_typedefs": callback_typedefs,
        "constants": constants,
    }
    return header_payload, AbiVersion(major=major, minor=minor, patch=patch), parser_info
