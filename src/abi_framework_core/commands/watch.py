from __future__ import annotations

import argparse
import os
import time

from ..core import *  # noqa: F401,F403


def _collect_watch_paths(config: dict[str, Any], target_names: list[str], repo_root: Path) -> dict[str, float]:
    paths: dict[str, float] = {}

    def _add(p: str | Path | None) -> None:
        if not p:
            return
        resolved = Path(p).resolve() if not isinstance(p, Path) else p.resolve()
        if resolved.exists():
            try:
                paths[str(resolved)] = os.stat(resolved).st_mtime
            except OSError:
                pass

    for name in target_names:
        try:
            target = resolve_target(config, name)
        except AbiFrameworkError:
            continue
        header_cfg = target.get("header") or {}
        header_path_raw = header_cfg.get("path")
        if header_path_raw:
            _add(ensure_relative_path(repo_root, header_path_raw).resolve())

        bindings_cfg = target.get("bindings") or {}
        for key in ("metadata_path", "interop_metadata_path"):
            val = bindings_cfg.get(key)
            if isinstance(val, str):
                _add(ensure_relative_path(repo_root, val).resolve())

    return paths


def _poll_changes(watch_map: dict[str, float]) -> list[str]:
    changed: list[str] = []
    for path_str, last_mtime in watch_map.items():
        try:
            current = os.stat(path_str).st_mtime
        except OSError:
            continue
        if current != last_mtime:
            changed.append(path_str)
    return changed


def _refresh_mtimes(watch_map: dict[str, float]) -> None:
    for path_str in list(watch_map.keys()):
        try:
            watch_map[path_str] = os.stat(path_str).st_mtime
        except OSError:
            pass


def _run_command(
    command: str,
    config: dict[str, Any],
    target_names: list[str],
    repo_root: Path,
    skip_binary: bool,
) -> dict[str, dict[str, list[str]]]:
    """Run command and return {target: {functions: [...], enums: [...], structs: [...]}}."""
    snapshots: dict[str, dict[str, list[str]]] = {}
    for name in target_names:
        try:
            if command in ("codegen", "generate", "snapshot", "verify"):
                snap = build_snapshot(
                    config=config,
                    target_name=name,
                    repo_root=repo_root,
                    binary_override=None,
                    skip_binary=skip_binary,
                )
                header = snap.get("header") or {}
                snapshots[name] = {
                    "functions": sorted(header.get("symbols") or []),
                    "enums": sorted((header.get("enums") or {}).keys()),
                    "structs": sorted((header.get("structs") or {}).keys()),
                }
        except AbiFrameworkError as exc:
            print(f"[watch] [{name}] error: {exc}")
    return snapshots


def _print_symbol_diff(
    prev: dict[str, dict[str, list[str]]],
    curr: dict[str, dict[str, list[str]]],
) -> None:
    for name in sorted(set(list(prev.keys()) + list(curr.keys()))):
        prev_fns = set(prev.get(name, {}).get("functions", []))
        curr_fns = set(curr.get(name, {}).get("functions", []))
        added = sorted(curr_fns - prev_fns)
        removed = sorted(prev_fns - curr_fns)
        if added or removed:
            parts: list[str] = []
            for fn in added:
                parts.append(f"+{fn}")
            for fn in removed:
                parts.append(f"-{fn}")
            print(f"[{name}] symbols: {', '.join(parts)}")


def command_watch(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=getattr(args, "target", None))
    command = str(getattr(args, "command", "codegen"))
    skip_binary = bool(getattr(args, "skip_binary", False))
    poll_interval = float(getattr(args, "poll_interval", 0.5))
    debounce = 0.3

    watch_map = _collect_watch_paths(config, target_names, repo_root)
    # Also watch the config file itself
    config_path = Path(args.config).resolve()
    if config_path.exists():
        watch_map[str(config_path)] = os.stat(config_path).st_mtime

    print(f"[watch] watching {len(watch_map)} file(s) for changes (command={command})")
    print("[watch] Ctrl-C to stop")

    prev_snapshots: dict[str, dict[str, list[str]]] = {}
    pending_change_at: float | None = None

    try:
        while True:
            changed = _poll_changes(watch_map)
            if changed:
                pending_change_at = time.monotonic()
                _refresh_mtimes(watch_map)

            if pending_change_at is not None and (time.monotonic() - pending_change_at) >= debounce:
                pending_change_at = None

                # Clear screen
                os.system("cls" if os.name == "nt" else "clear")
                ts = time.strftime("%H:%M:%S")
                print(f"[watch] {ts} re-running {command} ...")

                # Reload config in case it changed
                try:
                    config = load_config(Path(args.config).resolve())
                    target_names = resolve_target_names(config=config, target_name=getattr(args, "target", None))
                except AbiFrameworkError as exc:
                    print(f"[watch] config error: {exc}")
                    time.sleep(poll_interval)
                    continue

                curr_snapshots = _run_command(command, config, target_names, repo_root, skip_binary)
                _print_symbol_diff(prev_snapshots, curr_snapshots)
                prev_snapshots = curr_snapshots

                # Refresh watch map after possible config change
                watch_map = _collect_watch_paths(config, target_names, repo_root)
                if config_path.exists():
                    watch_map[str(config_path)] = os.stat(config_path).st_mtime

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n[watch] stopped")

    return 0
