from __future__ import annotations

from ._core_base import *  # noqa: F401,F403
from ._core_codegen import *  # noqa: F401,F403

def parse_nm_exports(output: str) -> list[str]:
    exports: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        type_code = parts[-2]
        symbol = parts[-1]
        if symbol in {"|", "<"}:
            continue
        if len(type_code) != 1:
            continue
        # nm uses lowercase for local symbols (except GNU unique "u").
        if type_code == "U":
            continue
        if not (type_code.isupper() or type_code == "u"):
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


def parse_readelf_exports(output: str) -> list[str]:
    exports: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.rstrip()
        parts = line.split()
        if len(parts) < 8:
            continue
        number_token = parts[0]
        if not number_token.endswith(":") or not number_token[:-1].isdigit():
            continue
        bind = parts[4].upper()
        visibility = parts[5].upper()
        section = parts[6].upper()
        name = parts[7]
        if section == "UND":
            continue
        if bind not in {"GLOBAL", "WEAK", "GNU_UNIQUE", "UNIQUE"}:
            continue
        if visibility in {"HIDDEN", "INTERNAL"}:
            continue
        if name and name != "0":
            exports.add(name)
    return sorted(exports)


def parse_objdump_exports(output: str) -> list[str]:
    exports: set[str] = set()
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 7:
            continue
        if not re.fullmatch(r"[0-9A-Fa-f]+", parts[0]):
            continue
        binding = parts[1].lower()
        if binding not in {"g", "w", "u"}:
            continue
        section = parts[3]
        if section == "*UND*":
            continue
        name = parts[-1]
        if name and name != "*UND*":
            exports.add(name)
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


def build_export_command_specs(binary_path: Path) -> list[tuple[str, list[str], str]]:
    specs: list[tuple[str, list[str], str]] = []
    if sys.platform.startswith("linux"):
        specs.extend(
            [
                ("nm", ["nm", "-D", "--defined-only", str(binary_path)], "nm"),
                ("llvm-nm", ["llvm-nm", "-D", "--defined-only", str(binary_path)], "nm"),
                ("readelf", ["readelf", "-Ws", str(binary_path)], "readelf"),
                ("objdump", ["objdump", "-T", str(binary_path)], "objdump"),
            ]
        )
    elif sys.platform == "darwin":
        specs.extend(
            [
                ("nm", ["nm", "-gU", str(binary_path)], "nm"),
                ("llvm-nm", ["llvm-nm", "-gU", str(binary_path)], "nm"),
            ]
        )
    elif os.name == "nt":
        specs.extend(
            [
                ("dumpbin", ["dumpbin", "/exports", str(binary_path)], "dumpbin"),
                ("llvm-nm", ["llvm-nm", "--defined-only", str(binary_path)], "nm"),
                ("nm", ["nm", "--defined-only", str(binary_path)], "nm"),
            ]
        )
    else:
        specs.extend(
            [
                ("nm", ["nm", "--defined-only", str(binary_path)], "nm"),
                ("llvm-nm", ["llvm-nm", "--defined-only", str(binary_path)], "nm"),
                ("objdump", ["objdump", "-T", str(binary_path)], "objdump"),
            ]
        )
    return specs


def parse_exports_with_format(output: str, parse_format: str) -> list[str]:
    if parse_format == "dumpbin":
        return parse_dumpbin_exports(output)
    if parse_format == "readelf":
        return parse_readelf_exports(output)
    if parse_format == "objdump":
        return parse_objdump_exports(output)
    return parse_nm_exports(output)


def extract_binary_exports(binary_path: Path, symbol_prefix: str, allow_non_prefixed_exports: bool) -> dict[str, Any]:
    if not binary_path.exists():
        return {
            "available": False,
            "path": str(binary_path),
            "tool": None,
            "tools": [],
            "symbol_count": 0,
            "symbols": [],
            "raw_export_count": 0,
            "non_prefixed_export_count": 0,
            "non_prefixed_exports": [],
            "allow_non_prefixed_exports": allow_non_prefixed_exports,
            "export_tool_errors": [],
        }

    command_specs = build_export_command_specs(binary_path)
    raw_exports: set[str] = set()
    tools_info: list[dict[str, Any]] = []
    tool_errors: list[str] = []

    for tool_name, command, parse_format in command_specs:
        exe = command[0]
        if shutil.which(exe) is None:
            continue
        try:
            proc = subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            message = exc.stderr.strip() or exc.stdout.strip() or "unknown command failure"
            tool_errors.append(f"{' '.join(command)}: {message}")
            continue
        parsed_exports = parse_exports_with_format(proc.stdout, parse_format=parse_format)
        raw_exports = set(parsed_exports)
        tools_info.append(
            {
                "tool": tool_name,
                "command": " ".join(command),
                "parse_format": parse_format,
                "export_count": len(parsed_exports),
            }
        )
        # Command specs are ordered by preference; use the first successful tool
        # to avoid mixing parser-specific interpretations of symbol tables.
        break

    if not tools_info:
        if tool_errors:
            raise AbiFrameworkError("Failed to query binary exports. " + " | ".join(tool_errors))
        raise AbiFrameworkError(
            "No export listing tool found. Install one of: nm, llvm-nm, readelf, objdump, dumpbin."
        )

    canonical_symbols: set[str] = set()
    non_prefixed: list[str] = []
    decorated_exports: list[str] = []
    for raw_symbol in sorted(raw_exports):
        canonical = canonicalize_prefixed_symbol(raw_symbol, symbol_prefix)
        normalized = raw_symbol[1:] if raw_symbol.startswith("_") else raw_symbol
        if normalized != raw_symbol or (normalized.rsplit("@", 1)[-1].isdigit() and "@" in normalized):
            decorated_exports.append(raw_symbol)
        if canonical is None:
            non_prefixed.append(raw_symbol)
            continue
        canonical_symbols.add(canonical)

    return {
        "available": True,
        "path": str(binary_path),
        "tool": tools_info[0]["command"] if tools_info else None,
        "tools": tools_info,
        "export_tool_error_count": len(tool_errors),
        "export_tool_errors": tool_errors,
        "symbol_count": len(canonical_symbols),
        "symbols": sorted(canonical_symbols),
        "raw_export_count": len(raw_exports),
        "decorated_export_count": len(decorated_exports),
        "decorated_exports": sorted(decorated_exports)[:50],
        "potential_calling_convention_mismatch": bool(decorated_exports) and os.name != "nt",
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
    parser_cfg = resolve_header_parser_config(header_cfg=header_cfg, repo_root=repo_root)

    version_macros_cfg = require_dict(header_cfg.get("version_macros"), "header.version_macros")
    version_macros = {
        "major": require_str(version_macros_cfg.get("major"), "header.version_macros.major"),
        "minor": require_str(version_macros_cfg.get("minor"), "header.version_macros.minor"),
        "patch": require_str(version_macros_cfg.get("patch"), "header.version_macros.patch"),
    }

    header_payload, abi_version, parser_info = parse_c_header(
        header_path=header_path,
        api_macro=api_macro,
        call_macro=call_macro,
        symbol_prefix=symbol_prefix,
        version_macros=version_macros,
        type_policy=type_policy,
        parser_cfg=parser_cfg,
    )
    header_payload["path"] = to_repo_relative(header_path, repo_root)
    header_payload["parser"] = parser_info
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
        "generated_at_utc": utc_timestamp_now(),
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


