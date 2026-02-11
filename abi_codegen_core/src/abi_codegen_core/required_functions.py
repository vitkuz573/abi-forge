from __future__ import annotations

import re
from typing import Any


def iter_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(iter_strings(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(iter_strings(item))
        return result
    return []


def _first_capture(match: re.Match[str]) -> str:
    if match.lastindex:
        for index in range(1, match.lastindex + 1):
            value = match.group(index)
            if value:
                return value
    return match.group(0)


def derive_required_functions(
    payload: dict[str, Any],
    idl_names: set[str],
    native_call_patterns: list[str],
    function_name_pattern: str | None = None,
) -> list[str]:
    compiled_patterns: list[re.Pattern[str]] = []
    for raw in native_call_patterns:
        try:
            compiled_patterns.append(re.compile(raw))
        except re.error as exc:
            raise SystemExit(f"Invalid native call regex pattern '{raw}': {exc}") from exc

    function_filter: re.Pattern[str] | None = None
    if function_name_pattern:
        try:
            function_filter = re.compile(function_name_pattern)
        except re.error as exc:
            raise SystemExit(f"Invalid function_name_pattern '{function_name_pattern}': {exc}") from exc

    discovered: set[str] = set()
    for text in iter_strings(payload):
        for pattern in compiled_patterns:
            for match in pattern.finditer(text):
                candidate = _first_capture(match)
                if function_filter and not function_filter.fullmatch(candidate):
                    continue
                if candidate in idl_names:
                    discovered.add(candidate)
    return sorted(discovered)
