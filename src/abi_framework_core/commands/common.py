from __future__ import annotations

from ..core import *  # noqa: F401,F403

def get_targets_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config is missing required object: 'targets'.")
    out: dict[str, dict[str, Any]] = {}
    for name, payload in targets.items():
        if isinstance(name, str) and isinstance(payload, dict):
            out[name] = payload
    if not out:
        raise AbiFrameworkError("Config has no valid targets.")
    return out



def resolve_baseline_for_target(repo_root: Path, config: dict[str, Any], target_name: str, baseline_root: str | None) -> Path:
    target = resolve_target(config, target_name)
    if baseline_root:
        return ensure_relative_path(repo_root, f"{baseline_root.rstrip('/')}/{target_name}.json").resolve()

    baseline_path = target.get("baseline_path")
    if isinstance(baseline_path, str) and baseline_path:
        return ensure_relative_path(repo_root, baseline_path).resolve()

    return ensure_relative_path(repo_root, f"abi/baselines/{target_name}.json").resolve()


def resolve_binary_for_target(repo_root: Path, config: dict[str, Any], target_name: str, binary_override: str | None) -> tuple[str | None, bool]:
    if binary_override:
        return binary_override, False
    target = resolve_target(config, target_name)
    binary_cfg = target.get("binary")
    if isinstance(binary_cfg, dict):
        path_value = binary_cfg.get("path")
        if isinstance(path_value, str) and path_value:
            return str(ensure_relative_path(repo_root, path_value).resolve()), False
    return None, True


