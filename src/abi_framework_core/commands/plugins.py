from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ..core import *  # noqa: F401,F403
from .common import get_targets_map


def _normalize_command_for_validation(value: Any, context: str, errors: list[str]) -> list[str] | None:
    if value is None:
        return None
    try:
        return normalize_external_command_template(value, context)
    except AbiFrameworkError as exc:
        errors.append(str(exc))
        return None


def _resolve_generator_manifest_with_checks(
    *,
    generator: dict[str, Any],
    repo_root: Path,
    target_name: str,
    context: str,
) -> tuple[Path | None, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    manifest_path = resolve_generator_manifest_path(
        generator=generator,
        repo_root=repo_root,
        target_name=target_name,
        discover_from_command=True,
    )
    if manifest_path is None:
        errors.append(f"{context}: unable to discover plugin manifest path")
        return None, errors, warnings

    manifest_path = manifest_path.resolve()
    if not manifest_path.exists():
        errors.append(f"{context}: manifest file not found at '{manifest_path}'")
        return None, errors, warnings

    try:
        manifest_payload, _ = load_and_validate_plugin_manifest(manifest_path)
    except AbiFrameworkError as exc:
        errors.append(f"{context}: {exc}")
        return manifest_path, errors, warnings

    plugin_name_raw = generator.get("plugin")
    plugin_name = plugin_name_raw.strip() if isinstance(plugin_name_raw, str) else None
    command_template = _normalize_command_for_validation(
        generator.get("command"),
        f"{context}.command",
        errors,
    )

    selected_plugin: dict[str, Any] | None = None
    if plugin_name:
        try:
            selected_plugin = get_manifest_plugin_by_name(
                manifest_payload,
                plugin_name,
                f"{context}.manifest",
            )
        except AbiFrameworkError as exc:
            errors.append(str(exc))
    elif command_template is not None:
        try:
            matches = find_manifest_plugins_by_command(
                manifest_payload,
                command_template,
                f"{context}.manifest",
            )
            if len(matches) == 1:
                selected_plugin = matches[0]
            elif len(matches) == 0:
                warnings.append(
                    f"{context}: command is not linked to a plugin entry; "
                    "add explicit 'plugin' to enforce deterministic binding"
                )
            else:
                names = ", ".join(str(item.get("name") or "<unnamed>") for item in matches)
                errors.append(
                    f"{context}: command matches multiple plugins in manifest ({names}); "
                    "set explicit 'plugin'"
                )
        except AbiFrameworkError as exc:
            errors.append(str(exc))
    else:
        warnings.append(f"{context}: no 'plugin' and no 'command'; plugin selection cannot be validated")

    if selected_plugin is not None and command_template is not None:
        try:
            plugin_command = get_manifest_plugin_entrypoint_command(
                selected_plugin,
                f"{context}.manifest.plugin",
            )
            if plugin_command != command_template:
                errors.append(
                    f"{context}: command does not match manifest entrypoint for plugin "
                    f"'{selected_plugin.get('name')}'"
                )
        except AbiFrameworkError as exc:
            errors.append(str(exc))

    return manifest_path, errors, warnings


def _discover_manifests_from_config(
    config_path: Path,
    repo_root: Path,
    target_name: str | None,
) -> tuple[list[Path], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    manifests: list[Path] = []
    seen: set[Path] = set()

    try:
        config = load_config(config_path)
    except AbiFrameworkError as exc:
        errors.append(f"config: {exc}")
        return manifests, errors, warnings
    targets = get_targets_map(config)

    selected_targets: list[tuple[str, dict[str, Any]]] = []
    if target_name:
        if target_name not in targets:
            errors.append(f"config.target: unknown target '{target_name}'")
            return manifests, errors, warnings
        selected_targets.append((target_name, targets[target_name]))
    else:
        selected_targets = sorted(targets.items(), key=lambda item: item[0])

    for current_target_name, target_payload in selected_targets:
        bindings = target_payload.get("bindings")
        if not isinstance(bindings, dict):
            continue
        generators = bindings.get("generators")
        if not isinstance(generators, list):
            continue

        for index, generator in enumerate(generators):
            context = f"{current_target_name}.bindings.generators[{index}]"
            if not isinstance(generator, dict):
                errors.append(f"{context}: must be an object")
                continue
            if not bool(generator.get("enabled", True)):
                continue
            kind = str(generator.get("kind") or "external").strip().lower()
            if kind != "external":
                continue

            manifest_path, generator_errors, generator_warnings = _resolve_generator_manifest_with_checks(
                generator=generator,
                repo_root=repo_root,
                target_name=current_target_name,
                context=context,
            )
            errors.extend(generator_errors)
            warnings.extend(generator_warnings)
            if manifest_path is None:
                continue
            if manifest_path not in seen:
                seen.add(manifest_path)
                manifests.append(manifest_path)

    return manifests, errors, warnings


def _validate_single_manifest(manifest_path: Path) -> dict[str, Any]:
    payload = load_json(manifest_path)
    report = validate_plugin_manifest_payload(payload)
    report["manifest"] = str(manifest_path)
    return report


def command_validate_plugin_manifest(args: argparse.Namespace) -> int:
    manual_manifest_tokens = normalize_manifest_args(getattr(args, "manifest", None))
    repo_root_value = getattr(args, "repo_root", ".")
    repo_root = Path(repo_root_value).resolve()

    manifests_to_validate: list[Path] = []
    discovery_errors: list[str] = []
    discovery_warnings: list[str] = []
    seen_manifests: set[Path] = set()

    for token in manual_manifest_tokens:
        path = Path(token).resolve()
        if not path.exists():
            discovery_errors.append(f"manifest: file not found '{path}'")
            continue
        if path not in seen_manifests:
            seen_manifests.add(path)
            manifests_to_validate.append(path)

    config_value = getattr(args, "config", None)
    target_name = getattr(args, "target", None)
    if isinstance(config_value, str) and config_value:
        config_path = Path(config_value).resolve()
        if not config_path.exists():
            discovery_errors.append(f"config: file not found '{config_path}'")
        else:
            discovered, config_errors, config_warnings = _discover_manifests_from_config(
                config_path=config_path,
                repo_root=repo_root,
                target_name=target_name,
            )
            discovery_errors.extend(config_errors)
            discovery_warnings.extend(config_warnings)
            for path in discovered:
                if path not in seen_manifests:
                    seen_manifests.add(path)
                    manifests_to_validate.append(path)

    if not manifests_to_validate and not discovery_errors:
        discovery_errors.append("manifest: no manifests provided or discovered")

    manifest_reports: list[dict[str, Any]] = []
    for manifest_path in manifests_to_validate:
        try:
            report = _validate_single_manifest(manifest_path)
        except AbiFrameworkError as exc:
            report = {
                "schema_version": PLUGIN_MANIFEST_SCHEMA_VERSION,
                "manifest": str(manifest_path),
                "package": None,
                "plugin_count": 0,
                "plugins": [],
                "errors": [str(exc)],
                "warnings": [],
                "status": "fail",
            }
        manifest_reports.append(report)

    aggregate_errors = list(discovery_errors)
    aggregate_warnings = list(discovery_warnings)
    total_plugins = 0
    for report in manifest_reports:
        manifest_ref = report.get("manifest")
        total_plugins += int(report.get("plugin_count", 0))
        for item in report.get("errors", []):
            aggregate_errors.append(f"{manifest_ref}: {item}")
        for item in report.get("warnings", []):
            aggregate_warnings.append(f"{manifest_ref}: {item}")

    status = "pass" if not aggregate_errors else "fail"
    aggregate_report: dict[str, Any] = {
        "schema_version": PLUGIN_MANIFEST_SCHEMA_VERSION,
        "status": status,
        "manifest_count": len(manifest_reports),
        "plugin_count": total_plugins,
        "manifests": manifest_reports,
        "errors": aggregate_errors,
        "warnings": aggregate_warnings,
    }

    output_value = getattr(args, "output", None)
    if isinstance(output_value, str) and output_value:
        write_json(Path(output_value).resolve(), aggregate_report)

    if bool(getattr(args, "print_json", False)):
        print(json.dumps(aggregate_report, indent=2, sort_keys=True))

    for report in manifest_reports:
        manifest_ref = report.get("manifest")
        manifest_status = report.get("status")
        plugin_count = int(report.get("plugin_count", 0))
        print(f"plugin-manifest: {manifest_status} ({manifest_ref}, {plugin_count} plugin(s))")
        for item in report.get("errors", []):
            print("  error: " + str(item))
        for item in report.get("warnings", []):
            print("  warning: " + str(item))

    for item in discovery_errors:
        print("plugin-manifest discovery error: " + item)
    for item in discovery_warnings:
        print("plugin-manifest discovery warning: " + item)

    if status != "pass":
        return 1
    if bool(getattr(args, "fail_on_warnings", False)) and aggregate_warnings:
        return 1
    return 0
