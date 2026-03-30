from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------

def _read_header(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _strip_comments(src: str) -> str:
    """Remove C/C++ block and line comments (best-effort)."""
    # Block comments
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.DOTALL)
    # Line comments
    src = re.sub(r"//[^\n]*", " ", src)
    return src


def _detect_api_macros(src: str) -> list[str]:
    """
    Heuristic: find ALL_CAPS identifiers containing API/EXPORT/PUBLIC/EXTERN
    that appear before function-like declarations.
    """
    # Pattern: line with optional type, then CAPS_MACRO, then identifier followed by (
    candidates: Counter[str] = Counter()

    # Look for: TYPE MACRO name( or MACRO TYPE name(
    macro_before = re.findall(
        r"\b([A-Z][A-Z0-9_]{2,})\s+(?:\w[\w\s\*]*?\s+)?\w+\s*\(",
        src,
    )
    macro_after = re.findall(
        r"\b\w[\w\s]*\b([A-Z][A-Z0-9_]{2,})\s+\w+\s*\(",
        src,
    )

    keywords = {
        "API", "EXPORT", "PUBLIC", "EXTERN", "DECL", "FUNC",
        "CALL", "CDECL", "STDCALL", "WINAPI",
    }
    noise = {
        "NULL", "TRUE", "FALSE", "EOF", "INLINE", "STATIC", "CONST",
        "VOID", "INT", "CHAR", "FLOAT", "DOUBLE", "LONG", "SHORT",
        "UNSIGNED", "SIGNED", "STRUCT", "ENUM", "TYPEDEF", "EXTERN",
        "AUTO", "REGISTER", "VOLATILE", "RESTRICT",
    }

    for tok in macro_before + macro_after:
        if tok in noise:
            continue
        # Must contain at least one keyword fragment
        if any(kw in tok for kw in keywords):
            candidates[tok] += 1

    # Sort by frequency, dedupe
    result = [tok for tok, _ in candidates.most_common()]
    # Separate: API-type macros vs CALL-convention macros
    api_macros = [t for t in result if any(k in t for k in ("API", "EXPORT", "PUBLIC", "DECL", "FUNC", "EXTERN"))]
    return api_macros[:3]  # top 3


def _detect_call_macros(src: str) -> list[str]:
    candidates: Counter[str] = Counter()
    call_kws = {"CALL", "CDECL", "STDCALL", "WINAPI", "FASTCALL"}
    tokens = re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", src)
    noise = {
        "NULL", "TRUE", "FALSE", "EOF", "INLINE", "STATIC", "CONST",
        "VOID", "INT", "API", "EXPORT", "PUBLIC",
    }
    for tok in tokens:
        if tok in noise:
            continue
        if any(kw in tok for kw in call_kws):
            candidates[tok] += 1
    return [tok for tok, _ in candidates.most_common(2)]


def _detect_symbol_prefix(src: str, api_macros: list[str]) -> str:
    """
    Detect common symbol prefix from function names exported with api_macro.
    Falls back to typedef struct names.
    """
    func_names: list[str] = []

    if api_macros:
        for macro in api_macros[:1]:
            # TYPE MACRO funcname( or MACRO TYPE funcname(
            pattern = rf"\b{re.escape(macro)}\b[^;{{]*?\b([a-z][a-z0-9_]{{3,}})\s*\("
            func_names.extend(re.findall(pattern, src))
            pattern2 = rf"\b([a-z][a-z0-9_]{{3,}})\s*\([^)]*\)\s*;"
            # just use all lowercase function names if no match via macro
            if not func_names:
                func_names.extend(re.findall(pattern2, src))

    if not func_names:
        # Fall back: typedef struct names  e.g. "typedef struct mylib_foo_t mylib_foo_t;"
        func_names.extend(re.findall(r"typedef\s+struct\s+\w+\s+([a-z][a-z0-9_]+_t)\s*;", src))

    if not func_names:
        return ""

    # Find longest common prefix that ends at an underscore
    common = func_names[0]
    for name in func_names[1:]:
        while not name.startswith(common):
            common = common[:-1]
        if not common:
            break
    # Trim to last underscore boundary
    if "_" in common:
        idx = common.rfind("_")
        common = common[:idx + 1]
    return common


def _detect_version_macros(src: str, symbol_prefix: str) -> dict[str, str]:
    """
    Detect VERSION_MAJOR/MINOR/PATCH macros.
    Pattern: #define PREFIX_VERSION_MAJOR N
    """
    result: dict[str, str] = {"major": "", "minor": "", "patch": ""}
    upper_prefix = symbol_prefix.upper().rstrip("_")

    for slot, key in [("major", "MAJOR"), ("minor", "MINOR"), ("patch", "PATCH")]:
        # Try prefix-based first
        patterns = [
            rf"#\s*define\s+({re.escape(upper_prefix)}_(?:ABI_)?VERSION_{key})\s",
            rf"#\s*define\s+({re.escape(upper_prefix)}_{key})\s",
            rf"#\s*define\s+(\w+_VERSION_{key})\s",
        ]
        for pat in patterns:
            m = re.search(pat, src)
            if m:
                result[slot] = m.group(1)
                break

    return result


def _count_functions(src: str, api_macros: list[str]) -> int:
    if not api_macros:
        # Rough count: lines with identifier followed by (
        return len(re.findall(r"\b[a-z][a-z0-9_]{3,}\s*\(", src))
    count = 0
    for macro in api_macros[:1]:
        count += len(re.findall(rf"\b{re.escape(macro)}\b", src))
    return count


def _count_enums(src: str) -> int:
    return len(re.findall(r"\btypedef\s+enum\b", src))


def _count_structs(src: str) -> int:
    return len(re.findall(r"\btypedef\s+struct\b", src))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_header_file(header_path: Path) -> dict[str, Any]:
    """
    Statically analyse a C header and return detected config hints.

    Returns a dict with keys:
      api_macro, call_macro, symbol_prefix, version_macros,
      function_count_estimate, enum_count, struct_count,
      suggested_config (ready-to-paste config fragment)
    """
    src_raw = _read_header(header_path)
    src = _strip_comments(src_raw)

    api_macros = _detect_api_macros(src)
    call_macros = _detect_call_macros(src)
    symbol_prefix = _detect_symbol_prefix(src, api_macros)
    version_macros = _detect_version_macros(src, symbol_prefix)

    func_count = _count_functions(src, api_macros)
    enum_count = _count_enums(src)
    struct_count = _count_structs(src)

    # Derive target name from file stem
    target = re.sub(r"[^a-z0-9_]", "_", header_path.stem.lower()).strip("_")

    api_macro = api_macros[0] if api_macros else f"{target.upper()}_API"
    call_macro = call_macros[0] if call_macros else ""

    suggested_config: dict[str, Any] = {
        "header": {
            "path": str(header_path),
            "api_macro": api_macro,
            "symbol_prefix": symbol_prefix,
        }
    }
    if call_macro:
        suggested_config["header"]["call_macro"] = call_macro
    if any(version_macros.values()):
        suggested_config["header"]["version_macros"] = {
            k: v for k, v in {
                "major": version_macros["major"],
                "minor": version_macros["minor"],
                "patch": version_macros["patch"],
            }.items() if v
        }

    return {
        "header_path": str(header_path),
        "target": target,
        "api_macro": api_macro,
        "api_macro_candidates": api_macros,
        "call_macro": call_macro,
        "call_macro_candidates": call_macros,
        "symbol_prefix": symbol_prefix,
        "version_macros": version_macros,
        "function_count_estimate": func_count,
        "enum_count": enum_count,
        "struct_count": struct_count,
        "suggested_config": suggested_config,
    }


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def command_scan_header(args: argparse.Namespace) -> int:
    header_path = Path(args.header).resolve()
    if not header_path.exists():
        print(f"error: header not found: {header_path}", file=sys.stderr)
        return 1

    result = scan_header_file(header_path)

    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return 0

    print(f"Header: {result['header_path']}")
    print(f"Target (inferred): {result['target']}")
    print(f"API macro:         {result['api_macro']}" + (
        f"  (candidates: {', '.join(result['api_macro_candidates'])})" if len(result['api_macro_candidates']) > 1 else ""
    ))
    if result["call_macro"]:
        print(f"Call macro:        {result['call_macro']}")
    print(f"Symbol prefix:     {result['symbol_prefix'] or '(not detected)'}")
    vm = result["version_macros"]
    if any(vm.values()):
        print(f"Version macros:    major={vm['major']} minor={vm['minor']} patch={vm['patch']}")
    print(f"Functions (est.):  {result['function_count_estimate']}")
    print(f"Enums:             {result['enum_count']}")
    print(f"Structs:           {result['struct_count']}")
    print()
    print("Suggested abi/config.json header section:")
    print(json.dumps(result["suggested_config"], indent=2))
    return 0
