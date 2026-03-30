from __future__ import annotations

import argparse
import filecmp
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .._core_base import (
    TOOL_VERSION,
    IDL_SCHEMA_VERSION,
    IDL_SCHEMA_URI_V1,
    AbiFrameworkError,
    get_abi_forge_sdk_path,
)
from .._core_plugins import (
    get_manifest_plugin_by_name,
    get_manifest_plugins,
    load_and_validate_plugin_manifest,
)


def _make_synthetic_idl(target_name: str = "test_target") -> dict[str, Any]:
    return {
        "idl_schema": IDL_SCHEMA_URI_V1,
        "idl_schema_version": IDL_SCHEMA_VERSION,
        "tool": {"name": "abi_framework", "version": TOOL_VERSION},
        "target": target_name,
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "abi_version": {"major": 1, "minor": 0, "patch": 0},
        "source": {"header_path": "include/test.h"},
        "functions": [
            {
                "name": "test_init",
                "c_return_type": "int",
                "parameters": [],
                "symbol": "test_init",
            },
            {
                "name": "test_cleanup",
                "c_return_type": "void",
                "parameters": [],
                "symbol": "test_cleanup",
            },
        ],
        "enums": [],
        "structs": [],
        "opaque_types": [],
    }


def _render_command(
    command_template: list[str],
    idl_path: Path,
    out_dir: Path,
    repo_root: Path,
    target_name: str,
    extra_flags: list[str],
) -> list[str]:
    sdk_path = get_abi_forge_sdk_path()
    replacements = {
        "{repo_root}": str(out_dir),  # use temp dir as repo_root so outputs land there
        "{target}": target_name,
        "{idl}": str(idl_path),
        "{check}": "--check" if "--check" in extra_flags else "",
        "{dry_run}": "--dry-run" if "--dry-run" in extra_flags else "",
        "{abi_forge_sdk}": str(sdk_path) if sdk_path else "",
    }
    rendered: list[str] = []
    for token in command_template:
        current = token
        for key, val in replacements.items():
            current = current.replace(key, val)
        if current:
            rendered.append(current)
    return rendered


def _run_cmd(rendered: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(rendered, capture_output=True, text=True)


def _snapshot_dir(directory: Path) -> dict[str, float]:
    """Map of relative_path -> mtime for all files under directory."""
    result: dict[str, float] = {}
    for root, _, files in os.walk(directory):
        for fname in files:
            fpath = Path(root) / fname
            try:
                result[str(fpath.relative_to(directory))] = fpath.stat().st_mtime
            except (OSError, ValueError):
                pass
    return result


def _dirs_differ(dir_before: dict[str, float], dir_after: dict[str, float]) -> bool:
    return dir_before != dir_after


def _check_determinism(
    command_template: list[str],
    idl_path: Path,
    target_name: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="abi_test_plugin_det_") as tmp1, \
         tempfile.TemporaryDirectory(prefix="abi_test_plugin_det_") as tmp2:
        d1, d2 = Path(tmp1), Path(tmp2)

        cmd1 = _render_command(command_template, idl_path, d1, d1, target_name, [])
        cmd2 = _render_command(command_template, idl_path, d2, d2, target_name, [])

        r1 = _run_cmd(cmd1)
        r2 = _run_cmd(cmd2)

        if r1.returncode != 0:
            return {
                "check": "determinism",
                "status": "fail",
                "reason": f"first run failed (exit {r1.returncode}): {r1.stderr.strip()}",
            }
        if r2.returncode != 0:
            return {
                "check": "determinism",
                "status": "fail",
                "reason": f"second run failed (exit {r2.returncode}): {r2.stderr.strip()}",
            }

        snap1 = _snapshot_dir(d1)
        snap2 = _snapshot_dir(d2)

        files1 = set(snap1.keys())
        files2 = set(snap2.keys())
        if files1 != files2:
            return {
                "check": "determinism",
                "status": "fail",
                "reason": f"output file sets differ: run1={sorted(files1)} run2={sorted(files2)}",
            }

        for rel in sorted(files1):
            f1 = d1 / rel
            f2 = d2 / rel
            if not filecmp.cmp(str(f1), str(f2), shallow=False):
                return {
                    "check": "determinism",
                    "status": "fail",
                    "reason": f"output file '{rel}' differs between runs",
                }

        return {"check": "determinism", "status": "pass", "output_files": sorted(files1)}


def _check_dry_run(
    command_template: list[str],
    idl_path: Path,
    target_name: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="abi_test_plugin_dry_") as tmp:
        d = Path(tmp)
        before = _snapshot_dir(d)
        cmd = _render_command(command_template, idl_path, d, d, target_name, ["--dry-run"])
        r = _run_cmd(cmd)
        after = _snapshot_dir(d)
        if r.returncode != 0:
            return {
                "check": "dry_run",
                "status": "fail",
                "reason": f"dry-run exited with {r.returncode}: {r.stderr.strip()}",
            }
        if _dirs_differ(before, after):
            new_files = sorted(set(after.keys()) - set(before.keys()))
            return {
                "check": "dry_run",
                "status": "fail",
                "reason": f"dry-run wrote files: {new_files}",
            }
        return {"check": "dry_run", "status": "pass"}


def _check_check_mode(
    command_template: list[str],
    idl_path: Path,
    target_name: str,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="abi_test_plugin_chk_") as tmp:
        d = Path(tmp)
        # First: normal run to create outputs
        cmd_normal = _render_command(command_template, idl_path, d, d, target_name, [])
        r_normal = _run_cmd(cmd_normal)
        if r_normal.returncode != 0:
            return {
                "check": "check_mode",
                "status": "skip",
                "reason": f"normal run failed (exit {r_normal.returncode}), check mode test skipped",
            }

        # Check mode on up-to-date outputs should exit 0
        cmd_check = _render_command(command_template, idl_path, d, d, target_name, ["--check"])
        r_check = _run_cmd(cmd_check)
        if r_check.returncode != 0:
            return {
                "check": "check_mode",
                "status": "fail",
                "reason": f"--check returned {r_check.returncode} on up-to-date outputs (expected 0)",
            }

        # Corrupt an output file, then check mode should exit non-zero
        out_files = list(d.rglob("*"))
        written = [f for f in out_files if f.is_file()]
        if not written:
            return {"check": "check_mode", "status": "skip", "reason": "no output files found"}

        target_file = written[0]
        original = target_file.read_bytes()
        target_file.write_bytes(original + b"\n# corrupted\n")

        cmd_check2 = _render_command(command_template, idl_path, d, d, target_name, ["--check"])
        r_check2 = _run_cmd(cmd_check2)
        if r_check2.returncode == 0:
            return {
                "check": "check_mode",
                "status": "fail",
                "reason": "--check returned 0 on drifted output (expected non-zero)",
            }

        return {"check": "check_mode", "status": "pass"}


def command_test_plugin(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    if not manifest_path.exists():
        print(f"error: manifest not found: {manifest_path}", file=sys.stderr)
        return 2

    manifest_payload, _ = load_and_validate_plugin_manifest(manifest_path)

    plugin_name_arg: str | None = getattr(args, "plugin", None)
    if plugin_name_arg:
        plugin = get_manifest_plugin_by_name(manifest_payload, plugin_name_arg, "test-plugin")
    else:
        plugins = get_manifest_plugins(manifest_payload, "test-plugin")
        if len(plugins) != 1:
            print(
                f"error: manifest has {len(plugins)} plugins; specify --plugin <name>",
                file=sys.stderr,
            )
            return 2
        plugin = plugins[0]

    plugin_name = str(plugin.get("name") or "plugin")
    entrypoint = plugin.get("entrypoint") or {}
    command_template: list[str] = [str(t) for t in (entrypoint.get("command") or [])]
    if not command_template:
        print(f"error: plugin '{plugin_name}' has no command template", file=sys.stderr)
        return 2

    capabilities = plugin.get("capabilities") or {}
    supports_check = bool(capabilities.get("supports_check", False))
    supports_dry_run = bool(capabilities.get("supports_dry_run", False))

    # Prepare IDL
    if getattr(args, "idl", None):
        idl_path = Path(args.idl).resolve()
        if not idl_path.exists():
            print(f"error: IDL file not found: {idl_path}", file=sys.stderr)
            return 2
    else:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".idl.json", delete=False, encoding="utf-8"
        ) as f:
            json.dump(_make_synthetic_idl(), f, indent=2)
            idl_path = Path(f.name)

    target_name = "test_target"
    checks: list[dict[str, Any]] = []

    # Determinism check (always)
    checks.append(_check_determinism(command_template, idl_path, target_name))

    if supports_dry_run:
        checks.append(_check_dry_run(command_template, idl_path, target_name))

    if supports_check:
        checks.append(_check_check_mode(command_template, idl_path, target_name))

    passed = sum(1 for c in checks if c["status"] == "pass")
    failed = sum(1 for c in checks if c["status"] == "fail")
    skipped = sum(1 for c in checks if c["status"] == "skip")
    overall = "pass" if failed == 0 else "fail"

    report = {
        "plugin": plugin_name,
        "manifest": str(manifest_path),
        "status": overall,
        "checks": checks,
        "summary": {"passed": passed, "failed": failed, "skipped": skipped},
    }

    output_path: str | None = getattr(args, "output", None)
    print_json: bool = bool(getattr(args, "print_json", False))

    if output_path:
        Path(output_path).resolve().parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).resolve().write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        print(f"report written to {output_path}")

    if print_json or not output_path:
        print(json.dumps(report, indent=2))

    for c in checks:
        status_icon = "✓" if c["status"] == "pass" else ("✗" if c["status"] == "fail" else "~")
        reason = c.get("reason", "")
        print(f"  {status_icon} {c['check']}: {c['status']}" + (f" — {reason}" if reason else ""))

    fail_on_warnings = bool(getattr(args, "fail_on_warnings", False))
    if fail_on_warnings and skipped > 0:
        return 1
    return 0 if overall == "pass" else 1
