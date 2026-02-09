from __future__ import annotations

from ._core_base import *  # noqa: F401,F403
from ._core_compare import version_dict_to_str

def normalize_c_type(value: str) -> str:
    text = sanitize_c_decl_text(value)
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

    native_header_output_value = raw.get("native_header_output_path")
    native_header_output_path = None
    if isinstance(native_header_output_value, str) and native_header_output_value:
        native_header_output_path = ensure_relative_path(repo_root, native_header_output_value).resolve()

    native_export_map_output_value = raw.get("native_export_map_output_path")
    native_export_map_output_path = None
    if isinstance(native_export_map_output_value, str) and native_export_map_output_value:
        native_export_map_output_path = ensure_relative_path(repo_root, native_export_map_output_value).resolve()

    native_header_guard = raw.get("native_header_guard")
    if native_header_guard is not None and (not isinstance(native_header_guard, str) or not native_header_guard):
        raise AbiFrameworkError(f"target '{target_name}'.codegen.native_header_guard must be string when specified")

    header_cfg = target.get("header")
    native_api_macro = raw.get("native_api_macro")
    native_call_macro = raw.get("native_call_macro")
    if native_api_macro is None and isinstance(header_cfg, dict):
        native_api_macro = header_cfg.get("api_macro")
    if native_call_macro is None and isinstance(header_cfg, dict):
        native_call_macro = header_cfg.get("call_macro")
    if native_api_macro is not None and (not isinstance(native_api_macro, str) or not native_api_macro):
        raise AbiFrameworkError(f"target '{target_name}'.codegen.native_api_macro must be string when specified")
    if native_call_macro is not None and (not isinstance(native_call_macro, str) or not native_call_macro):
        raise AbiFrameworkError(f"target '{target_name}'.codegen.native_call_macro must be string when specified")

    version_macro_names: dict[str, str] = {
        "major": "ABI_VERSION_MAJOR",
        "minor": "ABI_VERSION_MINOR",
        "patch": "ABI_VERSION_PATCH",
    }
    if isinstance(header_cfg, dict):
        raw_version_macros = header_cfg.get("version_macros")
        if isinstance(raw_version_macros, dict):
            for key in ["major", "minor", "patch"]:
                value = raw_version_macros.get(key)
                if isinstance(value, str) and value:
                    version_macro_names[key] = value

    native_constants_raw = raw.get("native_constants")
    native_constants: dict[str, str] = {}
    if native_constants_raw is not None:
        if not isinstance(native_constants_raw, dict):
            raise AbiFrameworkError(
                f"target '{target_name}'.codegen.native_constants must be an object when specified"
            )
        for key, value in native_constants_raw.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                native_constants[key] = value

    idl_schema_version_value = raw.get("idl_schema_version")
    if idl_schema_version_value is None:
        idl_schema_version = IDL_SCHEMA_VERSION
    else:
        if not isinstance(idl_schema_version_value, int):
            raise AbiFrameworkError(
                f"target '{target_name}'.codegen.idl_schema_version must be integer when specified"
            )
        if idl_schema_version_value != IDL_SCHEMA_VERSION:
            raise AbiFrameworkError(
                f"target '{target_name}'.codegen.idl_schema_version={idl_schema_version_value} is not supported; "
                f"only {IDL_SCHEMA_VERSION} is supported"
            )
        idl_schema_version = idl_schema_version_value

    bindings_cfg = target.get("bindings")
    symbol_docs: dict[str, str] = {}
    deprecated_symbols: set[str] = set()
    if isinstance(bindings_cfg, dict):
        raw_docs = bindings_cfg.get("symbol_docs")
        if raw_docs is not None:
            if not isinstance(raw_docs, dict):
                raise AbiFrameworkError(f"target '{target_name}'.bindings.symbol_docs must be an object when specified")
            for key, value in raw_docs.items():
                if isinstance(key, str) and key and isinstance(value, str) and value.strip():
                    symbol_docs[key] = value.strip()
        raw_deprecated = bindings_cfg.get("deprecated_symbols")
        if raw_deprecated is not None:
            if not isinstance(raw_deprecated, list):
                raise AbiFrameworkError(
                    f"target '{target_name}'.bindings.deprecated_symbols must be an array when specified"
                )
            for item in raw_deprecated:
                if isinstance(item, str) and item:
                    deprecated_symbols.add(item)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "idl_output_path": idl_output_path,
        "native_header_output_path": native_header_output_path,
        "native_export_map_output_path": native_export_map_output_path,
        "native_header_guard": native_header_guard,
        "native_api_macro": native_api_macro,
        "native_call_macro": native_call_macro,
        "native_constants": native_constants,
        "version_macro_names": version_macro_names,
        "include_symbols": include_symbols,
        "exclude_symbols": exclude_symbols,
        "include_patterns": include_patterns,
        "exclude_patterns": exclude_patterns,
        "idl_schema_version": idl_schema_version,
        "symbol_docs": symbol_docs,
        "deprecated_symbols": deprecated_symbols,
    }


def _merge_nested_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge_nested_dicts(out[key], value)
        else:
            out[key] = value
    return out


def resolve_interop_metadata(target: dict[str, Any], target_name: str, repo_root: Path) -> dict[str, Any]:
    bindings_cfg = target.get("bindings")
    if not isinstance(bindings_cfg, dict):
        return {}

    metadata: dict[str, Any] = {}

    metadata_path_value = bindings_cfg.get("interop_metadata_path")
    if isinstance(metadata_path_value, str) and metadata_path_value:
        metadata_path = ensure_relative_path(repo_root, metadata_path_value).resolve()
        try:
            text = metadata_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AbiFrameworkError(
                f"Unable to read interop metadata '{metadata_path}': {exc}"
            ) from exc
        try:
            loaded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AbiFrameworkError(
                f"Interop metadata '{metadata_path}' is not valid JSON: {exc}"
            ) from exc
        if not isinstance(loaded, dict):
            raise AbiFrameworkError(
                f"Interop metadata '{metadata_path}' must be a JSON object"
            )
        metadata = loaded

    inline = bindings_cfg.get("interop_metadata")
    if isinstance(inline, dict) and inline:
        metadata = _merge_nested_dicts(metadata, inline)

    if metadata and not isinstance(metadata, dict):
        raise AbiFrameworkError(
            f"target '{target_name}'.bindings.interop_metadata must resolve to an object"
        )

    return metadata


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
    abi_version_obj = snapshot.get("abi_version")
    since_abi = version_dict_to_str(abi_version_obj)
    symbol_docs = codegen_cfg.get("symbol_docs")
    if not isinstance(symbol_docs, dict):
        symbol_docs = {}
    deprecated_symbols = codegen_cfg.get("deprecated_symbols")
    if not isinstance(deprecated_symbols, set):
        deprecated_symbols = set()

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
            "documentation": str(symbol_docs.get(symbol, "")),
            "deprecated": symbol in deprecated_symbols,
            "availability": {
                "since_abi": since_abi,
            },
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
    interop_metadata: dict[str, Any] | None = None,
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
    idl_schema_version = IDL_SCHEMA_VERSION
    idl_schema_uri = IDL_SCHEMA_URI_V1

    header_path = str(header.get("path", ""))
    parser_info = header.get("parser")
    parser_backend = None
    if isinstance(parser_info, dict):
        parser_backend = parser_info.get("backend_requested", parser_info.get("backend"))

    payload = {
        "idl_schema_version": idl_schema_version,
        "idl_schema": idl_schema_uri,
        "tool": {
            "name": "abi_framework",
            "version": TOOL_VERSION,
        },
        "content_fingerprint": content_fingerprint,
        "target": target_name,
        "abi_version": snapshot.get("abi_version"),
        "source": {
            "header_path": header_path,
            "parser_backend": parser_backend,
        },
        "summary": {
            "function_count": len(records),
            "enum_count": int(header.get("enum_count") or 0),
            "struct_count": int(header.get("struct_count") or 0),
        },
        "functions": records,
        "header_types": {
            "enums": header.get("enums", {}),
            "structs": header.get("structs", {}),
            "opaque_types": header.get("opaque_types", []),
            "opaque_type_declarations": header.get("opaque_type_declarations", []),
            "callback_typedefs": header.get("callback_typedefs", []),
            "constants": header.get("constants", {}),
        },
        "codegen": {
            "enabled": bool(codegen_cfg.get("enabled", True)),
            "include_symbols": sorted(codegen_cfg.get("include_symbols", [])),
            "exclude_symbols": sorted(codegen_cfg.get("exclude_symbols", [])),
        },
    }
    if interop_metadata:
        payload["bindings"] = {
            "interop": interop_metadata,
        }
    return payload


def is_c_typedef_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*_t", value))


def derive_opaque_type_names_from_idl(idl_payload: dict[str, Any]) -> list[str]:
    header_types = idl_payload.get("header_types")
    if not isinstance(header_types, dict):
        header_types = {}

    enums_obj = header_types.get("enums")
    structs_obj = header_types.get("structs")
    enum_names = set(enums_obj.keys()) if isinstance(enums_obj, dict) else set()
    struct_names = set(structs_obj.keys()) if isinstance(structs_obj, dict) else set()

    explicit = header_types.get("opaque_types")
    if isinstance(explicit, list):
        names: list[str] = []
        seen: set[str] = set()
        for item in explicit:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if not is_c_typedef_name(name):
                continue
            if name in seen:
                continue
            seen.add(name)
            names.append(name)
        if names:
            return names

    candidates: set[str] = set()
    token_pattern = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*_t\b")

    functions = idl_payload.get("functions")
    if isinstance(functions, list):
        for item in functions:
            if not isinstance(item, dict):
                continue
            return_type = str(item.get("c_return_type") or "")
            candidates.update(token_pattern.findall(return_type))
            params = item.get("parameters")
            if isinstance(params, list):
                for param in params:
                    if not isinstance(param, dict):
                        continue
                    c_type = str(param.get("c_type") or "")
                    candidates.update(token_pattern.findall(c_type))

    if isinstance(structs_obj, dict):
        for struct in structs_obj.values():
            if not isinstance(struct, dict):
                continue
            fields = struct.get("fields")
            if not isinstance(fields, list):
                continue
            for field in fields:
                if not isinstance(field, dict):
                    continue
                declaration = str(field.get("declaration") or "")
                candidates.update(token_pattern.findall(declaration))

    out = [
        name
        for name in sorted(candidates)
        if name not in enum_names and name not in struct_names and is_c_typedef_name(name)
    ]
    return out


def collect_opaque_type_declarations(idl_payload: dict[str, Any]) -> list[str]:
    header_types = idl_payload.get("header_types")
    if not isinstance(header_types, dict):
        header_types = {}

    raw_decls = header_types.get("opaque_type_declarations")
    declarations: list[str] = []
    if isinstance(raw_decls, list):
        seen: set[str] = set()
        for item in raw_decls:
            if not isinstance(item, str):
                continue
            decl = normalize_ws(item)
            if not decl:
                continue
            if not decl.endswith(";"):
                decl += ";"
            if decl in seen:
                continue
            seen.add(decl)
            declarations.append(decl)
    if declarations:
        return declarations

    names = derive_opaque_type_names_from_idl(idl_payload)
    return [f"typedef struct {name} {name};" for name in names]


def collect_callback_typedef_declarations(idl_payload: dict[str, Any]) -> list[str]:
    header_types = idl_payload.get("header_types")
    if not isinstance(header_types, dict):
        return []
    raw = header_types.get("callback_typedefs")
    if not isinstance(raw, list):
        return []
    declarations: list[str] = []
    seen: set[str] = set()
    for item in raw:
        declaration = None
        if isinstance(item, dict):
            value = item.get("declaration")
            if isinstance(value, str):
                declaration = sanitize_c_decl_text(value)
        elif isinstance(item, str):
            declaration = sanitize_c_decl_text(item)
        if not declaration:
            continue
        declaration = re.sub(r"\(\s+", "(", declaration)
        declaration = re.sub(r"\s+\)", ")", declaration)
        if not declaration.endswith(";"):
            declaration += ";"
        if declaration in seen:
            continue
        seen.add(declaration)
        declarations.append(declaration)
    return declarations


def collect_native_constants(idl_payload: dict[str, Any], codegen_cfg: dict[str, Any]) -> dict[str, str]:
    constants: dict[str, str] = {}
    header_types = idl_payload.get("header_types")
    if isinstance(header_types, dict):
        raw_constants = header_types.get("constants")
        if isinstance(raw_constants, dict):
            for key, value in raw_constants.items():
                if isinstance(key, str) and key and isinstance(value, str) and value:
                    constants[key] = normalize_ws(value)

    overrides = codegen_cfg.get("native_constants")
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if isinstance(key, str) and key and isinstance(value, str) and value:
                constants[key] = normalize_ws(value)

    return {name: constants[name] for name in sorted(constants.keys())}


def render_c_parameter_for_declaration(param: dict[str, Any], index: int) -> str:
    c_type = normalize_c_type(str(param.get("c_type") or "void"))
    if bool(param.get("variadic")) or c_type == "...":
        return "..."
    name = str(param.get("name") or f"arg{index}")
    if re.search(r"\(\s*\*\s*\)", c_type):
        return re.sub(r"\(\s*\*\s*\)", f"(*{name})", c_type, count=1)
    return f"{c_type} {name}".strip()


def render_native_header_from_idl(target_name: str, idl_payload: dict[str, Any], codegen_cfg: dict[str, Any]) -> str:
    api_macro = str(codegen_cfg.get("native_api_macro") or "ABI_API")
    call_macro = str(codegen_cfg.get("native_call_macro") or "ABI_CALL")
    header_guard_raw = codegen_cfg.get("native_header_guard")
    if isinstance(header_guard_raw, str) and header_guard_raw:
        header_guard = header_guard_raw
    else:
        base = re.sub(r"[^A-Za-z0-9_]", "_", target_name).upper()
        header_guard = f"{base}_H" if not base.endswith("_H") else base

    api_base = api_macro[:-4] if api_macro.endswith("_API") else api_macro
    export_switch = f"{api_base}_EXPORTS"
    dll_switch = f"{api_base}_DLL"

    version_macros = codegen_cfg.get("version_macro_names")
    if not isinstance(version_macros, dict):
        version_macros = {}
    version_major_name = str(version_macros.get("major") or "ABI_VERSION_MAJOR")
    version_minor_name = str(version_macros.get("minor") or "ABI_VERSION_MINOR")
    version_patch_name = str(version_macros.get("patch") or "ABI_VERSION_PATCH")

    abi_version = idl_payload.get("abi_version")
    if not isinstance(abi_version, dict):
        abi_version = {}
    major = int(abi_version.get("major") or 0)
    minor = int(abi_version.get("minor") or 0)
    patch = int(abi_version.get("patch") or 0)

    header_types = idl_payload.get("header_types")
    if not isinstance(header_types, dict):
        header_types = {}
    enums_obj = header_types.get("enums")
    structs_obj = header_types.get("structs")
    enums = enums_obj if isinstance(enums_obj, dict) else {}
    structs = structs_obj if isinstance(structs_obj, dict) else {}

    constants = collect_native_constants(idl_payload=idl_payload, codegen_cfg=codegen_cfg)
    for key in [version_major_name, version_minor_name, version_patch_name]:
        constants.pop(key, None)

    lines: list[str] = []
    lines.append(f"#ifndef {header_guard}")
    lines.append(f"#define {header_guard}")
    lines.append("")
    lines.append("/* Auto-generated by abi_framework from ABI IDL. Do not edit manually. */")
    lines.append("")
    lines.append("#ifdef __cplusplus")
    lines.append('extern "C" {')
    lines.append("#endif")
    lines.append("")
    lines.append("#include <stddef.h>")
    lines.append("#include <stdint.h>")
    lines.append("#include <stdbool.h>")
    lines.append("")
    lines.append("#if defined(_WIN32)")
    lines.append(f"  #if defined({export_switch})")
    lines.append(f"    #define {api_macro} __declspec(dllexport)")
    lines.append(f"  #elif defined({dll_switch})")
    lines.append(f"    #define {api_macro} __declspec(dllimport)")
    lines.append("  #else")
    lines.append(f"    #define {api_macro}")
    lines.append("  #endif")
    lines.append(f"  #define {call_macro} __cdecl")
    lines.append("#else")
    lines.append(f'  #define {api_macro} __attribute__((visibility("default")))')
    lines.append(f"  #define {call_macro}")
    lines.append("#endif")
    lines.append("")
    for name, value in constants.items():
        lines.append(f"#define {name} {value}")
    lines.append(f"#define {version_major_name} {major}")
    lines.append(f"#define {version_minor_name} {minor}")
    lines.append(f"#define {version_patch_name} {patch}")
    lines.append("")

    opaque_typedefs = collect_opaque_type_declarations(idl_payload)
    for declaration in opaque_typedefs:
        lines.append(declaration)
    if opaque_typedefs:
        lines.append("")

    callback_typedefs = collect_callback_typedef_declarations(idl_payload)
    for declaration in callback_typedefs:
        lines.append(declaration)
    if callback_typedefs:
        lines.append("")

    for enum_name in sorted(enums.keys()):
        enum_obj = enums.get(enum_name)
        if not isinstance(enum_obj, dict):
            continue
        lines.append(f"typedef enum {enum_name} {{")
        members = enum_obj.get("members")
        if isinstance(members, list):
            for member in members:
                if not isinstance(member, dict):
                    continue
                member_name = str(member.get("name") or "")
                if not member_name:
                    continue
                value_expr = member.get("value_expr")
                value = member.get("value")
                if isinstance(value_expr, str) and value_expr:
                    lines.append(f"  {member_name} = {value_expr},")
                elif isinstance(value, int):
                    lines.append(f"  {member_name} = {value},")
                else:
                    lines.append(f"  {member_name},")
        lines.append(f"}} {enum_name};")
        lines.append("")

    for struct_name in sorted(structs.keys()):
        struct_obj = structs.get(struct_name)
        if not isinstance(struct_obj, dict):
            continue
        lines.append(f"typedef struct {struct_name} {{")
        fields = struct_obj.get("fields")
        if isinstance(fields, list):
            for field in fields:
                if not isinstance(field, dict):
                    continue
                declaration = normalize_ws(str(field.get("declaration") or ""))
                if not declaration:
                    continue
                lines.append(f"  {declaration};")
        lines.append(f"}} {struct_name};")
        lines.append("")

    functions = idl_payload.get("functions")
    if isinstance(functions, list):
        for item in sorted(functions, key=lambda obj: str(obj.get("name") if isinstance(obj, dict) else "")):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            return_type = normalize_c_type(str(item.get("c_return_type") or "void"))
            params = item.get("parameters")
            params_out: list[str] = []
            if isinstance(params, list):
                for idx, param in enumerate(params):
                    if not isinstance(param, dict):
                        continue
                    params_out.append(render_c_parameter_for_declaration(param, idx))
            params_text = ", ".join(params_out) if params_out else "void"
            lines.append(f"{api_macro} {return_type} {call_macro} {name}({params_text});")

    lines.append("")
    lines.append("#ifdef __cplusplus")
    lines.append("}")
    lines.append("#endif")
    lines.append("")
    lines.append(f"#endif /* {header_guard} */")
    lines.append("")
    return "\n".join(lines)


def render_native_export_map_from_idl(idl_payload: dict[str, Any]) -> str:
    functions = idl_payload.get("functions")
    symbols: list[str] = []
    if isinstance(functions, list):
        for item in functions:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name:
                symbols.append(name)
    symbols = sorted(set(symbols))

    lines: list[str] = []
    lines.append("{")
    lines.append("  global:")
    for symbol in symbols:
        lines.append(f"    {symbol};")
    lines.append("")
    lines.append("  local:")
    lines.append("    *;")
    lines.append("};")
    lines.append("")
    return "\n".join(lines)


def normalize_generator_entries(target_name: str, target: dict[str, Any]) -> list[dict[str, Any]]:
    bindings_cfg = target.get("bindings")
    if not isinstance(bindings_cfg, dict):
        return []

    generators_raw = bindings_cfg.get("generators")
    if generators_raw is None:
        return []

    if not isinstance(generators_raw, list):
        raise AbiFrameworkError(f"target '{target_name}'.bindings.generators must be an array when specified")

    entries: list[dict[str, Any]] = []
    for idx, item in enumerate(generators_raw):
        if not isinstance(item, dict):
            raise AbiFrameworkError(f"target '{target_name}'.bindings.generators[{idx}] must be an object")
        if not bool(item.get("enabled", True)):
            continue
        name = str(item.get("name") or f"generator_{idx}")
        kind = str(item.get("kind") or "external").strip().lower()
        entry: dict[str, Any] = {
            "name": name,
            "kind": kind,
            "enabled": True,
            "options": item.get("options") if isinstance(item.get("options"), dict) else {},
        }
        if kind == "external":
            command = item.get("command")
            if not isinstance(command, list) or not command or not all(isinstance(x, str) and x for x in command):
                raise AbiFrameworkError(
                    f"target '{target_name}'.bindings.generators[{idx}].command must be a non-empty string array"
                )
            entry["command"] = command
        else:
            raise AbiFrameworkError(
                f"target '{target_name}'.bindings.generators[{idx}].kind must be external"
            )
        entries.append(entry)

    return entries


def run_generator_entry(
    *,
    repo_root: Path,
    target_name: str,
    generator: dict[str, Any],
    idl_path: Path,
    check: bool,
    dry_run: bool,
) -> dict[str, Any]:
    name = str(generator.get("name") or "generator")
    kind = str(generator.get("kind") or "external")

    if kind == "builtin":
        builtin = str(generator.get("builtin") or name).strip().lower()
        raise AbiFrameworkError(
            f"Builtin generator '{builtin}' is not supported; use kind='external' for target '{target_name}'"
        )

    if kind == "external":
        command_template = generator.get("command")
        if not isinstance(command_template, list):
            raise AbiFrameworkError(f"Generator '{name}' for target '{target_name}' is missing command array")
        replacements = {
            "{repo_root}": str(repo_root),
            "{target}": target_name,
            "{idl}": str(idl_path),
            "{check}": "--check" if check else "",
            "{dry_run}": "--dry-run" if dry_run else "",
        }
        rendered: list[str] = []
        for token in command_template:
            current = token
            for key, value in replacements.items():
                current = current.replace(key, value)
            if current:
                rendered.append(current)
        proc = subprocess.run(rendered, capture_output=True, text=True)
        status = "pass" if proc.returncode == 0 else "fail"
        return {
            "name": name,
            "kind": "external",
            "status": status,
            "command": " ".join(shlex.quote(item) for item in rendered),
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "exit_code": proc.returncode,
        }

    raise AbiFrameworkError(f"Unsupported generator kind '{kind}' for target '{target_name}'")


def run_code_generators_for_target(
    *,
    repo_root: Path,
    target_name: str,
    target: dict[str, Any],
    idl_path: Path,
    check: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    entries = normalize_generator_entries(target_name=target_name, target=target)
    results: list[dict[str, Any]] = []
    for entry in entries:
        result = run_generator_entry(
            repo_root=repo_root,
            target_name=target_name,
            generator=entry,
            idl_path=idl_path,
            check=check,
            dry_run=dry_run,
        )
        results.append(result)
    return results


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
