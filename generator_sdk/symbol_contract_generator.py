#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import re
from pathlib import Path
from typing import Any


TOOL_PATH = "tools/abi_framework/generator_sdk/symbol_contract_generator.py"
SUPPORTED_MODE = {"strict", "required_only"}


class SymbolContractError(Exception):
    pass


def load_json_any(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SymbolContractError(f"Unable to read JSON file '{path}': {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SymbolContractError(f"Invalid JSON in '{path}': {exc}") from exc


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


def normalize_symbols(values: list[Any], label: str) -> list[str]:
    symbols: list[str] = []
    for index, value in enumerate(values):
        if not isinstance(value, str) or not value.strip():
            raise SymbolContractError(f"{label}[{index}] must be a non-empty string")
        symbols.append(value.strip())
    return symbols


def dedupe_sorted(values: list[str]) -> list[str]:
    return sorted({item for item in values if item})


def parse_mode(raw: Any, label: str) -> str:
    if raw is None:
        return "strict"
    if not isinstance(raw, str) or raw not in SUPPORTED_MODE:
        raise SymbolContractError(f"{label} must be one of: strict, required_only")
    return raw


def apply_path_tokens(raw: str, repo_root: Path, spec_dir: Path) -> str:
    return (
        raw.replace("{repo_root}", str(repo_root))
        .replace("{spec_dir}", str(spec_dir))
    )


def resolve_path(raw: str, repo_root: Path, spec_dir: Path) -> Path:
    expanded = apply_path_tokens(raw, repo_root, spec_dir)
    path = Path(expanded)
    if not path.is_absolute():
        path = (spec_dir / path).resolve()
    return path


def resolve_json_pointer(payload: Any, pointer: str, label: str) -> Any:
    if pointer in {"", "/"}:
        return payload
    if not pointer.startswith("/"):
        raise SymbolContractError(f"{label}.pointer must start with '/'")

    current = payload
    parts = pointer.split("/")[1:]
    for raw_part in parts:
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                raise SymbolContractError(f"{label}.pointer segment '{part}' was not found")
            current = current[part]
            continue
        if isinstance(current, list):
            if not part.isdigit():
                raise SymbolContractError(f"{label}.pointer segment '{part}' must be list index")
            index = int(part)
            if index < 0 or index >= len(current):
                raise SymbolContractError(f"{label}.pointer index '{index}' is out of range")
            current = current[index]
            continue
        raise SymbolContractError(f"{label}.pointer traversed into non-container value")
    return current


def collect_source_symbols(
    source: dict[str, Any],
    *,
    source_index: int,
    repo_root: Path,
    spec_dir: Path,
) -> list[str]:
    kind = source.get("kind")
    if not isinstance(kind, str) or not kind:
        raise SymbolContractError(f"sources[{source_index}].kind must be a non-empty string")
    label = f"sources[{source_index}]"

    if kind == "symbols":
        raw = source.get("symbols")
        if not isinstance(raw, list):
            raise SymbolContractError(f"{label}.symbols must be an array")
        return normalize_symbols(raw, f"{label}.symbols")

    if kind == "json_array":
        raw_path = source.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise SymbolContractError(f"{label}.path must be a non-empty string")
        json_path = resolve_path(raw_path, repo_root, spec_dir)
        payload = load_json_any(json_path)
        pointer = source.get("pointer", "")
        if pointer is not None and not isinstance(pointer, str):
            raise SymbolContractError(f"{label}.pointer must be a string when specified")
        resolved = resolve_json_pointer(payload, pointer or "", label)
        if not isinstance(resolved, list):
            raise SymbolContractError(f"{label}.pointer must resolve to an array")
        return normalize_symbols(resolved, f"{label}.pointer")

    if kind == "json_object_fields":
        raw_path = source.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise SymbolContractError(f"{label}.path must be a non-empty string")
        json_path = resolve_path(raw_path, repo_root, spec_dir)
        payload = load_json_any(json_path)
        pointer = source.get("pointer")
        if not isinstance(pointer, str) or not pointer:
            raise SymbolContractError(f"{label}.pointer must be a non-empty string")
        resolved = resolve_json_pointer(payload, pointer, label)
        if not isinstance(resolved, list):
            raise SymbolContractError(f"{label}.pointer must resolve to an array")
        fields_raw = source.get("fields")
        if not isinstance(fields_raw, list) or not fields_raw:
            raise SymbolContractError(f"{label}.fields must be a non-empty array")
        fields = normalize_symbols(fields_raw, f"{label}.fields")

        symbols: list[str] = []
        for item_index, item in enumerate(resolved):
            if not isinstance(item, dict):
                raise SymbolContractError(f"{label}.pointer[{item_index}] must be an object")
            for field in fields:
                value = item.get(field)
                if value is None:
                    continue
                if not isinstance(value, str) or not value.strip():
                    raise SymbolContractError(
                        f"{label}.pointer[{item_index}].{field} must be non-empty string when present"
                    )
                symbols.append(value.strip())
        return symbols

    if kind == "regex_scan":
        raw_root = source.get("root")
        if not isinstance(raw_root, str) or not raw_root:
            raise SymbolContractError(f"{label}.root must be a non-empty string")
        root_path = resolve_path(raw_root, repo_root, spec_dir)
        if not root_path.exists() or not root_path.is_dir():
            raise SymbolContractError(f"{label}.root does not exist or is not directory: {root_path}")

        include_raw = source.get("include")
        includes = ["**/*"] if include_raw is None else normalize_symbols(include_raw, f"{label}.include")
        exclude_raw = source.get("exclude")
        excludes = normalize_symbols(exclude_raw, f"{label}.exclude") if exclude_raw is not None else []
        pattern_raw = source.get("pattern")
        if not isinstance(pattern_raw, str) or not pattern_raw:
            raise SymbolContractError(f"{label}.pattern must be a non-empty string")
        try:
            regex = re.compile(pattern_raw)
        except re.error as exc:
            raise SymbolContractError(f"{label}.pattern is invalid regex: {exc}") from exc

        group_raw = source.get("group", 1)
        if not isinstance(group_raw, int) or group_raw < 0:
            raise SymbolContractError(f"{label}.group must be a non-negative integer when specified")
        group_index = int(group_raw)
        encoding = source.get("encoding", "utf-8")
        if not isinstance(encoding, str) or not encoding:
            raise SymbolContractError(f"{label}.encoding must be a non-empty string when specified")

        symbols: list[str] = []
        files: set[Path] = set()
        for pattern in includes:
            for candidate in root_path.glob(pattern):
                if candidate.is_file():
                    files.add(candidate.resolve())

        for path in sorted(files):
            rel = path.relative_to(root_path).as_posix()
            if any(fnmatch.fnmatch(rel, rule) for rule in excludes):
                continue
            text = path.read_text(encoding=encoding)
            for match in regex.finditer(text):
                try:
                    value = match.group(group_index)
                except IndexError as exc:
                    raise SymbolContractError(
                        f"{label}.group={group_index} is out of range for pattern '{pattern_raw}'"
                    ) from exc
                if value:
                    symbols.append(value.strip())
        return symbols

    raise SymbolContractError(f"{label}.kind '{kind}' is not supported")


def collect_idl_function_names(path: Path) -> set[str]:
    payload = load_json_any(path)
    if not isinstance(payload, dict):
        raise SymbolContractError(f"IDL root in '{path}' must be an object")
    functions = payload.get("functions")
    if not isinstance(functions, list):
        raise SymbolContractError("IDL missing required array 'functions'")
    names: set[str] = set()
    for index, item in enumerate(functions):
        if not isinstance(item, dict):
            raise SymbolContractError(f"IDL functions[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise SymbolContractError(f"IDL functions[{index}].name must be a non-empty string")
        names.add(name)
    return names


def render_contract(
    *,
    target_name: str | None,
    mode: str,
    symbols: list[str],
    spec_digest: str,
) -> str:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "mode": mode,
        "generator": TOOL_PATH,
        "spec_sha256": spec_digest,
        "symbol_count": len(symbols),
        "symbols": symbols,
    }
    if target_name:
        payload["target"] = target_name
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--idl", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--mode", choices=sorted(SUPPORTED_MODE))
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        repo_root = Path(args.repo_root).resolve()
        idl_path = Path(args.idl).resolve()
        spec_path = Path(args.spec).resolve()
        out_path = Path(args.out).resolve()

        spec_payload = load_json_any(spec_path)
        if not isinstance(spec_payload, dict):
            raise SymbolContractError(f"spec root in '{spec_path}' must be an object")
        schema_version = spec_payload.get("schema_version", 1)
        if schema_version != 1:
            raise SymbolContractError(f"spec.schema_version must be 1, got {schema_version!r}")
        sources = spec_payload.get("sources")
        if not isinstance(sources, list) or not sources:
            raise SymbolContractError("spec.sources must be a non-empty array")

        mode = parse_mode(args.mode, "mode override") if args.mode else parse_mode(spec_payload.get("mode"), "spec.mode")
        require_full_coverage = bool(spec_payload.get("require_full_coverage", False))
        target_name = spec_payload.get("target")
        if target_name is not None and (not isinstance(target_name, str) or not target_name):
            raise SymbolContractError("spec.target must be a non-empty string when specified")

        discovered: list[str] = []
        spec_dir = spec_path.parent
        for index, source in enumerate(sources):
            if not isinstance(source, dict):
                raise SymbolContractError(f"sources[{index}] must be an object")
            discovered.extend(
                collect_source_symbols(
                    source,
                    source_index=index,
                    repo_root=repo_root,
                    spec_dir=spec_dir,
                )
            )

        symbols = dedupe_sorted(discovered)
        idl_names = collect_idl_function_names(idl_path)
        missing_in_idl = sorted(name for name in symbols if name not in idl_names)
        if missing_in_idl:
            raise SymbolContractError(
                "symbol contract contains symbols not found in IDL: " + ", ".join(missing_in_idl)
            )
        if require_full_coverage and mode == "strict":
            missing_from_contract = sorted(name for name in idl_names if name not in set(symbols))
            if missing_from_contract:
                raise SymbolContractError(
                    "strict symbol contract must cover all IDL symbols; missing: " + ", ".join(missing_from_contract)
                )

        spec_digest = hashlib.sha256(spec_path.read_bytes()).hexdigest()
        output = render_contract(
            target_name=target_name if isinstance(target_name, str) else None,
            mode=mode,
            symbols=symbols,
            spec_digest=spec_digest,
        )
        return write_if_changed(out_path, output, args.check, args.dry_run)

    except SymbolContractError as exc:
        print(f"ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
