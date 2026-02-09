from __future__ import annotations

from ._core_base import *  # noqa: F401,F403
from ._core_codegen import *  # noqa: F401,F403
from ._core_snapshot import *  # noqa: F401,F403
from ._core_compare import *  # noqa: F401,F403

def resolve_target_names(config: dict[str, Any], target_name: str | None) -> list[str]:
    targets_obj = config.get("targets")
    if not isinstance(targets_obj, dict) or not targets_obj:
        raise AbiFrameworkError("config must define non-empty 'targets' object")
    targets: dict[str, dict[str, Any]] = {}
    for key, value in targets_obj.items():
        if isinstance(key, str) and key and isinstance(value, dict):
            targets[key] = value
    if not targets:
        raise AbiFrameworkError("config must define non-empty 'targets' object")
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
    interop_metadata = resolve_interop_metadata(target=target, target_name=target_name, repo_root=repo_root)
    idl_payload = build_idl_payload(
        target_name=target_name,
        snapshot=snapshot,
        codegen_cfg=codegen_cfg,
        interop_metadata=interop_metadata,
    )
    validate_idl_payload(idl_payload, f"generated IDL payload '{target_name}'")

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
    artifacts: dict[str, Any] = {
        "idl": {
            "path": to_repo_relative(idl_output_path, repo_root),
            "status": idl_status,
        },
    }
    artifact_statuses = [idl_status]

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

    native_header_output_path = codegen_cfg.get("native_header_output_path")
    if isinstance(native_header_output_path, Path):
        native_header_text = render_native_header_from_idl(
            target_name=target_name,
            idl_payload=idl_payload,
            codegen_cfg=codegen_cfg,
        )
        header_status, header_diff = write_artifact_if_changed(
            path=native_header_output_path,
            content=native_header_text,
            dry_run=dry_run,
            check=check,
        )
        artifacts["native_header"] = {
            "path": to_repo_relative(native_header_output_path, repo_root),
            "status": header_status,
        }
        artifact_statuses.append(header_status)
        if print_diff and header_diff:
            print(header_diff)

    native_export_map_output_path = codegen_cfg.get("native_export_map_output_path")
    if isinstance(native_export_map_output_path, Path):
        native_export_map_text = render_native_export_map_from_idl(idl_payload=idl_payload)
        export_map_status, export_map_diff = write_artifact_if_changed(
            path=native_export_map_output_path,
            content=native_export_map_text,
            dry_run=dry_run,
            check=check,
        )
        artifacts["native_export_map"] = {
            "path": to_repo_relative(native_export_map_output_path, repo_root),
            "status": export_map_status,
        }
        artifact_statuses.append(export_map_status)
        if print_diff and export_map_diff:
            print(export_map_diff)

    has_codegen_drift = any(status in {"drift", "would_write"} for status in artifact_statuses)
    has_sync_drift = bool(sync_comparison["missing_symbols"]) or bool(sync_comparison["extra_symbols"])

    return {
        "target": target_name,
        "target_config": target,
        "snapshot": snapshot,
        "idl_payload": idl_payload,
        "idl_output_path_abs": idl_output_path,
        "codegen_config": {
            "idl_output_path": to_repo_relative(idl_output_path, repo_root),
            "native_header_output_path": (
                to_repo_relative(native_header_output_path, repo_root)
                if isinstance(native_header_output_path, Path)
                else None
            ),
            "native_export_map_output_path": (
                to_repo_relative(native_export_map_output_path, repo_root)
                if isinstance(native_export_map_output_path, Path)
                else None
            ),
        },
        "artifacts": artifacts,
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
