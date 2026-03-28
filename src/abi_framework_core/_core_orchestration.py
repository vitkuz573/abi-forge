from __future__ import annotations

from ._core_base import *  # noqa: F401,F403
from ._core_codegen import *  # noqa: F401,F403
from ._core_snapshot import *  # noqa: F401,F403
from ._core_compare import *  # noqa: F401,F403


def build_symbol_contract_sync_comparison(
    *,
    generated_symbols: set[str],
    symbol_contract: dict[str, Any],
) -> dict[str, Any]:
    contract_symbols = set(get_message_list(symbol_contract, "symbols"))
    contract_mode = str(symbol_contract.get("mode", "strict"))
    configured = bool(symbol_contract.get("configured"))
    declared = bool(symbol_contract.get("declared"))
    source = str(symbol_contract.get("source", "not_configured"))
    missing_symbols = sorted(contract_symbols - generated_symbols)
    extra_symbols = sorted(generated_symbols - contract_symbols) if configured and contract_mode == "strict" else []

    return {
        "mode": "symbol_contract" if configured else "not_configured",
        "contract_mode": contract_mode,
        "declared": declared,
        "configured": configured,
        "source": source,
        "required_symbol_count": len(contract_symbols),
        "generated_symbol_count": len(generated_symbols),
        "missing_symbols": missing_symbols,
        "extra_symbols": extra_symbols,
    }


def has_symbol_contract_sync_drift(comparison: dict[str, Any]) -> bool:
    return bool(get_message_list(comparison, "missing_symbols")) or bool(get_message_list(comparison, "extra_symbols"))


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


def _compute_header_sha256(header_path: Path) -> str:
    """Compute SHA256 of a header file."""
    try:
        data = header_path.read_bytes()
        return hashlib.sha256(data).hexdigest()
    except OSError:
        return ""


def _compute_config_hash(target: dict[str, Any]) -> str:
    """Compute a stable hash of config sections that affect IDL generation."""
    header_cfg = target.get("header") or {}
    codegen_cfg_raw = target.get("codegen") or {}
    bindings_cfg = target.get("bindings") or {}
    relevant = {
        "header": {
            k: v for k, v in header_cfg.items()
            if k not in ("path",)  # path is covered by header SHA
        },
        "codegen": codegen_cfg_raw,
        "bindings_keys": sorted(bindings_cfg.keys()),
    }
    canonical = json.dumps(relevant, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _read_idl_cache(cache_path: Path) -> dict[str, Any]:
    """Read .idl.cache file, return empty dict on any error."""
    try:
        if cache_path.exists():
            return json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_idl_cache(cache_path: Path, header_sha256: str, config_hash: str, idl_sha256: str) -> None:
    """Write .idl.cache file alongside the IDL JSON."""
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "header_sha256": header_sha256,
            "config_hash": config_hash,
            "idl_sha256": idl_sha256,
            "generated_at": dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        cache_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass  # caching is best-effort


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
    force_regen: bool = False,
) -> dict[str, Any]:
    target = resolve_target(config, target_name)
    codegen_cfg = resolve_codegen_config(target=target, target_name=target_name, repo_root=repo_root)

    # Determine IDL output path early so we can check the cache
    if idl_output_override:
        idl_output_path = ensure_relative_path(repo_root, idl_output_override).resolve()
    else:
        configured = codegen_cfg.get("idl_output_path")
        if isinstance(configured, Path):
            idl_output_path = configured
        else:
            idl_output_path = ensure_relative_path(repo_root, f"abi/generated/{target_name}.idl.json").resolve()

    cache_path = idl_output_path.with_suffix("").with_suffix(".idl.cache")

    # Compute current header SHA256 and config hash for cache comparison
    header_cfg = target.get("header") or {}
    header_path_value = header_cfg.get("path") or ""
    header_path = ensure_relative_path(repo_root, header_path_value).resolve() if header_path_value else None
    current_header_sha = _compute_header_sha256(header_path) if header_path else ""
    current_config_hash = _compute_config_hash(target)

    # Check cache: if IDL exists and header+config+idl content unchanged, skip re-parsing
    idl_payload: dict[str, Any] | None = None
    snapshot: dict[str, Any] | None = None
    cache_hit = False

    if not force_regen and idl_output_path.exists() and current_header_sha:
        cached = _read_idl_cache(cache_path)
        if (
            cached.get("header_sha256") == current_header_sha
            and cached.get("config_hash") == current_config_hash
        ):
            try:
                idl_text_on_disk = idl_output_path.read_bytes()
                # Also verify IDL file hasn't been externally modified
                current_idl_sha = hashlib.sha256(idl_text_on_disk).hexdigest()
                cached_idl_sha = cached.get("idl_sha256", "")
                if not cached_idl_sha or cached_idl_sha == current_idl_sha:
                    idl_payload = json.loads(idl_text_on_disk.decode("utf-8"))
                    if isinstance(idl_payload, dict) and idl_payload:
                        cache_hit = True
                        print(f"[{target_name}] IDL up to date (cached), skipping header parse")
                    else:
                        idl_payload = None
            except (OSError, json.JSONDecodeError):
                idl_payload = None

    if not cache_hit:
        snapshot = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=binary_override,
            skip_binary=skip_binary,
        )
        bindings_metadata = resolve_bindings_metadata(target=target, target_name=target_name, repo_root=repo_root)
        idl_payload = build_idl_payload(
            target_name=target_name,
            snapshot=snapshot,
            codegen_cfg=codegen_cfg,
            bindings_metadata=bindings_metadata,
        )
    else:
        # For sync comparison we still need the snapshot, but we can build a lightweight version
        # from the cached IDL functions list so we don't re-parse the header
        bindings_metadata = resolve_bindings_metadata(target=target, target_name=target_name, repo_root=repo_root)

    validate_idl_payload(idl_payload, f"generated IDL payload '{target_name}'")

    idl_text = json.dumps(idl_payload, indent=2, sort_keys=True) + "\n"
    idl_status, idl_diff = write_artifact_if_changed(
        path=idl_output_path,
        content=idl_text,
        dry_run=dry_run,
        check=check,
    )
    # Write cache file after successful IDL write (not in dry_run or check mode)
    if not cache_hit and not dry_run and not check and current_header_sha:
        idl_sha = hashlib.sha256(idl_text.encode("utf-8")).hexdigest()
        _write_idl_cache(cache_path, current_header_sha, current_config_hash, idl_sha)
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
    symbol_contract = resolve_bindings_symbol_contract(
        target=target,
        target_name=target_name,
        repo_root=repo_root,
    )
    sync_comparison = build_symbol_contract_sync_comparison(
        generated_symbols=generated_symbols,
        symbol_contract=symbol_contract,
    )

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
    has_sync_drift = has_symbol_contract_sync_drift(sync_comparison)

    return {
        "target": target_name,
        "target_config": target,
        "snapshot": snapshot or {},
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
    contract_mode = str(comparison.get("contract_mode", "strict"))
    source = str(comparison.get("source", "not_configured"))
    declared = bool(comparison.get("declared"))
    missing = get_message_list(comparison, "missing_symbols")
    extra = get_message_list(comparison, "extra_symbols")

    if mode == "not_configured":
        if declared:
            print(f"[{target_name}] bindings sync: configured but empty symbols ({source})")
        else:
            print(f"[{target_name}] bindings sync: not configured")
        return

    if not missing and not extra:
        print(f"[{target_name}] bindings sync: clean ({contract_mode}, {source})")
        return

    print(f"[{target_name}] bindings sync: drift ({contract_mode}, {source})")
    if missing:
        print(f"  missing required symbols: {', '.join(missing)}")
    if extra:
        print(f"  extra generated symbols: {', '.join(extra)}")
