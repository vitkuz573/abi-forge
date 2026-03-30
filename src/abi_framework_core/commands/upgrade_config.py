from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Migration rules
# ---------------------------------------------------------------------------

_PLUGIN_RENAMES: dict[str, str] = {
    "abi_framework.managed_api": "abi_framework.native_impl_handles",
}

_DEPRECATED_GENERATOR_FIELDS = {"manifest", "command"}  # replaced by plugin+kind


def _upgrade_generator(gen: dict[str, Any], target_name: str, idx: int) -> tuple[dict[str, Any], list[str]]:
    """Upgrade a single generator entry. Returns (new_gen, list_of_changes)."""
    changes: list[str] = []
    g = dict(gen)

    # Rename plugin
    plugin = g.get("plugin")
    if isinstance(plugin, str) and plugin in _PLUGIN_RENAMES:
        new_plugin = _PLUGIN_RENAMES[plugin]
        changes.append(f"  [{target_name}].generators[{idx}]: plugin {plugin!r} → {new_plugin!r}")
        g["plugin"] = new_plugin

    # Add missing kind="external" when plugin is set but kind is missing
    if g.get("plugin") and not g.get("kind"):
        g["kind"] = "external"
        changes.append(f"  [{target_name}].generators[{idx}]: added kind=\"external\"")

    # Remove deprecated manifest/command fields when plugin is set
    if g.get("plugin"):
        for field in list(g.keys()):
            if field in _DEPRECATED_GENERATOR_FIELDS:
                del g[field]
                changes.append(f"  [{target_name}].generators[{idx}]: removed deprecated field {field!r}")

    return g, changes


def _upgrade_config(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Apply all migrations to the config payload. Returns (new_payload, changes)."""
    all_changes: list[str] = []
    targets = payload.get("targets")
    if not isinstance(targets, dict):
        return payload, all_changes

    new_targets: dict[str, Any] = {}
    for target_name, target in targets.items():
        if not isinstance(target, dict):
            new_targets[target_name] = target
            continue
        bindings = target.get("bindings")
        if not isinstance(bindings, dict):
            new_targets[target_name] = target
            continue
        generators = bindings.get("generators")
        if not isinstance(generators, list):
            new_targets[target_name] = target
            continue

        new_generators: list[Any] = []
        for idx, gen in enumerate(generators):
            if not isinstance(gen, dict):
                new_generators.append(gen)
                continue
            new_gen, changes = _upgrade_generator(gen, target_name, idx)
            new_generators.append(new_gen)
            all_changes.extend(changes)

        new_target = dict(target)
        new_target["bindings"] = dict(bindings)
        new_target["bindings"]["generators"] = new_generators
        new_targets[target_name] = new_target

    new_payload = dict(payload)
    new_payload["targets"] = new_targets
    return new_payload, all_changes


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------

def command_upgrade_config(args: argparse.Namespace) -> int:
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        return 1

    raw = config_path.read_text(encoding="utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON in {config_path}: {e}", file=sys.stderr)
        return 1

    new_payload, changes = _upgrade_config(payload)

    if not changes:
        print("upgrade-config: already up to date, no changes needed.")
        return 0

    print(f"upgrade-config: {len(changes)} change(s) detected:")
    for line in changes:
        print(line)

    if getattr(args, "check", False):
        print("upgrade-config: --check mode, not writing changes.")
        return 1

    if getattr(args, "dry_run", False):
        print("\nWould write:")
        print(json.dumps(new_payload, indent=2))
        return 0

    new_raw = json.dumps(new_payload, indent=2) + "\n"
    config_path.write_text(new_raw, encoding="utf-8")
    print(f"upgrade-config: written {config_path}")
    return 0
