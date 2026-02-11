from __future__ import annotations

from ._core_base import *  # noqa: F401,F403

PLUGIN_MANIFEST_SCHEMA_VERSION = 1
SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
PLUGIN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,127}$")
KNOWN_CAPABILITY_KEYS = {
    "supports_check",
    "supports_dry_run",
    "deterministic_output",
    "requires_repo_root",
    "writes_managed_sources",
}
KNOWN_CONTRACT_IO_KEYS = {"name", "path_arg", "required", "description"}
KNOWN_PLUGIN_KEYS = {
    "name",
    "version",
    "description",
    "entrypoint",
    "capabilities",
    "contracts",
    "compatibility",
    "diagnostics",
    "owners",
}
KNOWN_MANIFEST_KEYS = {"schema_version", "package", "plugins", "metadata"}
SCRIPT_SUFFIXES = {".py", ".sh", ".ps1", ".cmd", ".bat"}


def _append_plugin_error(errors: list[str], context: str, message: str) -> None:
    errors.append(f"{context}: {message}")


def _append_plugin_warning(warnings: list[str], context: str, message: str) -> None:
    warnings.append(f"{context}: {message}")


def _validate_io_contract_entries(
    value: Any,
    context: str,
    errors: list[str],
    warnings: list[str],
) -> None:
    if not isinstance(value, list):
        _append_plugin_error(errors, context, "must be an array")
        return
    for index, item in enumerate(value):
        item_context = f"{context}[{index}]"
        if not isinstance(item, dict):
            _append_plugin_error(errors, item_context, "must be an object")
            continue

        unknown = sorted(set(item.keys()) - KNOWN_CONTRACT_IO_KEYS)
        for key in unknown:
            _append_plugin_warning(warnings, item_context, f"unknown key '{key}'")

        name = item.get("name")
        if not isinstance(name, str) or not name:
            _append_plugin_error(errors, item_context, "'name' must be non-empty string")

        path_arg = item.get("path_arg")
        if not isinstance(path_arg, str) or not path_arg:
            _append_plugin_error(errors, item_context, "'path_arg' must be non-empty string")

        required = item.get("required")
        if required is not None and not isinstance(required, bool):
            _append_plugin_error(errors, item_context, "'required' must be boolean when specified")

        description = item.get("description")
        if description is not None and not isinstance(description, str):
            _append_plugin_error(errors, item_context, "'description' must be string when specified")


def validate_plugin_manifest_payload(payload: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    schema_version = payload.get("schema_version")
    if schema_version != PLUGIN_MANIFEST_SCHEMA_VERSION:
        _append_plugin_error(errors, "schema_version", "must be integer 1")

    package = payload.get("package")
    if not isinstance(package, str) or not package:
        _append_plugin_error(errors, "package", "must be non-empty string")

    unknown_manifest_keys = sorted(set(payload.keys()) - KNOWN_MANIFEST_KEYS)
    for key in unknown_manifest_keys:
        _append_plugin_warning(warnings, "manifest", f"unknown key '{key}'")

    plugins = payload.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        _append_plugin_error(errors, "plugins", "must be a non-empty array")
        plugins = []

    seen_names: set[str] = set()
    validated_plugins: list[dict[str, Any]] = []
    for index, plugin in enumerate(plugins):
        context = f"plugins[{index}]"
        if not isinstance(plugin, dict):
            _append_plugin_error(errors, context, "must be an object")
            continue

        unknown_plugin_keys = sorted(set(plugin.keys()) - KNOWN_PLUGIN_KEYS)
        for key in unknown_plugin_keys:
            _append_plugin_warning(warnings, context, f"unknown key '{key}'")

        name = plugin.get("name")
        if not isinstance(name, str) or not name:
            _append_plugin_error(errors, context, "'name' must be non-empty string")
            name = f"<invalid-{index}>"
        elif not PLUGIN_NAME_PATTERN.fullmatch(name):
            _append_plugin_error(errors, context, f"'name' must match {PLUGIN_NAME_PATTERN.pattern}")
        elif name in seen_names:
            _append_plugin_error(errors, context, f"duplicate plugin name '{name}'")
        else:
            seen_names.add(name)

        version = plugin.get("version")
        if not isinstance(version, str) or not version:
            _append_plugin_error(errors, context, "'version' must be non-empty string")
        elif not SEMVER_PATTERN.fullmatch(version):
            _append_plugin_warning(
                warnings,
                context,
                f"'version' does not match semver-like pattern {SEMVER_PATTERN.pattern}",
            )

        entrypoint = plugin.get("entrypoint")
        if not isinstance(entrypoint, dict):
            _append_plugin_error(errors, context, "'entrypoint' must be an object")
            entrypoint = {}
        kind = entrypoint.get("kind")
        if kind != "external":
            _append_plugin_error(errors, f"{context}.entrypoint", "'kind' must be 'external'")
        command = entrypoint.get("command")
        if not isinstance(command, list) or not command:
            _append_plugin_error(errors, f"{context}.entrypoint", "'command' must be non-empty array")
            command = []
        else:
            for token_index, token in enumerate(command):
                if not isinstance(token, str) or not token:
                    _append_plugin_error(
                        errors,
                        f"{context}.entrypoint.command[{token_index}]",
                        "must be non-empty string",
                    )
        if command and "{idl}" not in command:
            _append_plugin_warning(warnings, f"{context}.entrypoint", "command does not include '{idl}' placeholder")

        capabilities = plugin.get("capabilities")
        if capabilities is not None:
            if not isinstance(capabilities, dict):
                _append_plugin_error(errors, f"{context}.capabilities", "must be an object when specified")
            else:
                for key, value in capabilities.items():
                    if key not in KNOWN_CAPABILITY_KEYS:
                        _append_plugin_warning(warnings, f"{context}.capabilities", f"unknown capability '{key}'")
                    if not isinstance(value, bool):
                        _append_plugin_error(errors, f"{context}.capabilities.{key}", "must be boolean")

        contracts = plugin.get("contracts")
        if contracts is not None:
            if not isinstance(contracts, dict):
                _append_plugin_error(errors, f"{context}.contracts", "must be an object when specified")
            else:
                for key in ["inputs", "outputs"]:
                    if key in contracts:
                        _validate_io_contract_entries(
                            contracts.get(key),
                            f"{context}.contracts.{key}",
                            errors,
                            warnings,
                        )

        compatibility = plugin.get("compatibility")
        if compatibility is not None:
            if not isinstance(compatibility, dict):
                _append_plugin_error(errors, f"{context}.compatibility", "must be an object when specified")
            else:
                min_schema = compatibility.get("min_idl_schema_version")
                max_schema = compatibility.get("max_idl_schema_version")
                if min_schema is not None and (not isinstance(min_schema, int) or min_schema <= 0):
                    _append_plugin_error(
                        errors,
                        f"{context}.compatibility.min_idl_schema_version",
                        "must be positive integer",
                    )
                if max_schema is not None and (not isinstance(max_schema, int) or max_schema <= 0):
                    _append_plugin_error(
                        errors,
                        f"{context}.compatibility.max_idl_schema_version",
                        "must be positive integer",
                    )
                if isinstance(min_schema, int) and isinstance(max_schema, int) and min_schema > max_schema:
                    _append_plugin_error(
                        errors,
                        f"{context}.compatibility",
                        "min_idl_schema_version must be <= max_idl_schema_version",
                    )
                target_patterns = compatibility.get("target_patterns")
                if target_patterns is not None:
                    if not isinstance(target_patterns, list):
                        _append_plugin_error(
                            errors,
                            f"{context}.compatibility.target_patterns",
                            "must be an array when specified",
                        )
                    else:
                        for pattern_index, pattern in enumerate(target_patterns):
                            if not isinstance(pattern, str) or not pattern:
                                _append_plugin_error(
                                    errors,
                                    f"{context}.compatibility.target_patterns[{pattern_index}]",
                                    "must be non-empty string",
                                )
                                continue
                            try:
                                re.compile(pattern)
                            except re.error as exc:
                                _append_plugin_error(
                                    errors,
                                    f"{context}.compatibility.target_patterns[{pattern_index}]",
                                    f"invalid regex: {exc}",
                                )

        diagnostics = plugin.get("diagnostics")
        if diagnostics is not None:
            if not isinstance(diagnostics, dict):
                _append_plugin_error(errors, f"{context}.diagnostics", "must be an object when specified")
            else:
                codes = diagnostics.get("codes")
                if codes is not None:
                    if not isinstance(codes, list):
                        _append_plugin_error(errors, f"{context}.diagnostics.codes", "must be an array")
                    else:
                        for code_index, code in enumerate(codes):
                            if not isinstance(code, str) or not code:
                                _append_plugin_error(
                                    errors,
                                    f"{context}.diagnostics.codes[{code_index}]",
                                    "must be non-empty string",
                                )

        owners = plugin.get("owners")
        if owners is not None:
            if not isinstance(owners, list):
                _append_plugin_error(errors, f"{context}.owners", "must be an array when specified")
            else:
                for owner_index, owner in enumerate(owners):
                    if not isinstance(owner, str) or not owner:
                        _append_plugin_error(errors, f"{context}.owners[{owner_index}]", "must be non-empty string")

        validated_plugins.append(
            {
                "name": name,
                "version": version,
                "kind": kind,
            }
        )

    return {
        "schema_version": PLUGIN_MANIFEST_SCHEMA_VERSION,
        "package": package,
        "plugin_count": len(validated_plugins),
        "plugins": validated_plugins,
        "errors": errors,
        "warnings": warnings,
        "status": "pass" if not errors else "fail",
    }


def normalize_manifest_args(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def resolve_plugin_path_token(
    token: str,
    repo_root: Path,
    target_name: str | None = None,
) -> Path | None:
    normalized = token.strip()
    if not normalized:
        return None
    if normalized.startswith("-"):
        return None
    if normalized in {"{check}", "{dry_run}", "{idl}", "{target}"}:
        return None

    if "{repo_root}" in normalized:
        normalized = normalized.replace("{repo_root}", str(repo_root))
    if target_name is not None and "{target}" in normalized:
        normalized = normalized.replace("{target}", target_name)

    if "{" in normalized and "}" in normalized:
        return None

    candidate = Path(normalized)
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve()
    else:
        candidate = candidate.resolve()
    return candidate


def discover_manifest_from_command(
    command: list[Any],
    repo_root: Path,
    target_name: str | None = None,
) -> Path | None:
    for token in command:
        if not isinstance(token, str):
            continue
        path = resolve_plugin_path_token(token, repo_root, target_name=target_name)
        if path is None:
            continue
        if path.suffix.lower() not in SCRIPT_SUFFIXES:
            continue
        return path.parent / "plugin.manifest.json"
    return None


def resolve_generator_manifest_path(
    *,
    generator: dict[str, Any],
    repo_root: Path,
    target_name: str | None = None,
    discover_from_command: bool,
) -> Path | None:
    manifest_token = generator.get("manifest")
    if isinstance(manifest_token, str) and manifest_token:
        return resolve_plugin_path_token(manifest_token, repo_root, target_name=target_name)
    if not discover_from_command:
        return None
    command = generator.get("command")
    if not isinstance(command, list) or not command:
        return None
    return discover_manifest_from_command(command, repo_root, target_name=target_name)


def normalize_external_command_template(value: Any, context: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise AbiFrameworkError(f"{context} must be a non-empty string array")
    out: list[str] = []
    for index, token in enumerate(value):
        if not isinstance(token, str) or not token:
            raise AbiFrameworkError(f"{context}[{index}] must be a non-empty string")
        out.append(token)
    return out


def get_manifest_plugins(manifest_payload: dict[str, Any], context: str) -> list[dict[str, Any]]:
    plugins = manifest_payload.get("plugins")
    if not isinstance(plugins, list) or not plugins:
        raise AbiFrameworkError(f"{context}: 'plugins' must be a non-empty array")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(plugins):
        if not isinstance(item, dict):
            raise AbiFrameworkError(f"{context}: plugins[{index}] must be an object")
        out.append(item)
    return out


def get_manifest_plugin_by_name(
    manifest_payload: dict[str, Any],
    plugin_name: str,
    context: str,
) -> dict[str, Any]:
    plugins = get_manifest_plugins(manifest_payload, context)
    for plugin in plugins:
        name = plugin.get("name")
        if isinstance(name, str) and name == plugin_name:
            return plugin
    raise AbiFrameworkError(f"{context}: plugin '{plugin_name}' not found in manifest")


def get_manifest_plugin_entrypoint_command(plugin_payload: dict[str, Any], context: str) -> list[str]:
    entrypoint = plugin_payload.get("entrypoint")
    if not isinstance(entrypoint, dict):
        raise AbiFrameworkError(f"{context}: plugin entrypoint must be an object")
    kind = entrypoint.get("kind")
    if kind != "external":
        raise AbiFrameworkError(f"{context}: plugin entrypoint kind must be 'external'")
    return normalize_external_command_template(
        entrypoint.get("command"),
        f"{context}.entrypoint.command",
    )


def find_manifest_plugins_by_command(
    manifest_payload: dict[str, Any],
    command_template: list[str],
    context: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for plugin in get_manifest_plugins(manifest_payload, context):
        plugin_name = str(plugin.get("name") or "<unnamed>")
        plugin_command = get_manifest_plugin_entrypoint_command(
            plugin,
            f"{context}.plugins[{plugin_name}]",
        )
        if plugin_command == command_template:
            matches.append(plugin)
    return matches


def load_and_validate_plugin_manifest(manifest_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = load_json(manifest_path)
    report = validate_plugin_manifest_payload(payload)
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        raise AbiFrameworkError(
            f"manifest '{manifest_path}' validation failed: " + "; ".join(str(item) for item in errors)
        )
    return payload, report
