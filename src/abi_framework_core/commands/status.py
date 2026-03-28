from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from ..core import load_config, AbiFrameworkError
from .common import get_targets_map


# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

def _use_color() -> bool:
    return sys.stdout.isatty()


def _green(s: str) -> str:
    return f"\033[32m{s}\033[0m" if _use_color() else s


def _yellow(s: str) -> str:
    return f"\033[33m{s}\033[0m" if _use_color() else s


def _red(s: str) -> str:
    return f"\033[31m{s}\033[0m" if _use_color() else s


def _ok(msg: str) -> str:
    return _green("✓") + "  " + msg


def _warn(msg: str) -> str:
    return _yellow("⚠") + "  " + msg


def _fail(msg: str) -> str:
    return _red("✗") + "  " + msg


# ---------------------------------------------------------------------------
# Status command
# ---------------------------------------------------------------------------

def command_status(args: argparse.Namespace) -> int:
    """Show ABI setup health dashboard for a target."""
    repo_root = Path(getattr(args, "repo_root", ".")).resolve()
    config_path = Path(args.config).resolve()
    target = args.target
    skip_binary = getattr(args, "skip_binary", True)

    try:
        config = load_config(config_path)
    except Exception as e:
        print(_fail(f"Could not load config: {e}"), file=sys.stderr)
        return 1

    targets = get_targets_map(config)
    if target not in targets:
        print(_fail(f"Target '{target}' not found in config."), file=sys.stderr)
        return 1

    target_cfg = targets[target]
    has_warnings = False

    print(f"[{target}] ABI status")

    # IDL snapshot
    idl_rel = (
        target_cfg.get("codegen", {}).get("idl_output_path")
        or f"abi/generated/{target}/{target}.idl.json"
    )
    idl_path = repo_root / idl_rel
    idl: dict[str, Any] | None = None
    if idl_path.exists():
        try:
            idl = json.loads(idl_path.read_text(encoding="utf-8"))
            n_funcs = len(idl.get("functions") or [])
            n_enums = len(idl.get("header_types", {}).get("enums") or {})
            n_structs = len(idl.get("header_types", {}).get("structs") or {})
            print(f"  {_ok(f'IDL snapshot        {n_funcs} functions, {n_enums} enums, {n_structs} structs ({idl_path.name})')}")
        except Exception as e:
            print(f"  {_warn(f'IDL snapshot        exists but could not parse: {e}')}")
            has_warnings = True
    else:
        print(f"  {_fail(f'IDL snapshot        missing ({idl_rel})')}")
        has_warnings = True

    # Symbol contract
    contract_path = repo_root / "abi" / "bindings" / f"{target}.symbol_contract.json"
    if contract_path.exists():
        try:
            sc = json.loads(contract_path.read_text(encoding="utf-8"))
            n_symbols = len(sc.get("symbols") or sc.get("required_symbols") or [])
            print(f"  {_ok(f'Symbol contract     {n_symbols} symbols locked')}")
        except Exception:
            print(f"  {_warn('Symbol contract     exists but could not parse')}")
            has_warnings = True
    else:
        print(f"  {_warn('Symbol contract     not found (run codegen to generate)')}")
        has_warnings = True

    # managed.json
    managed_path = repo_root / "abi" / "bindings" / f"{target}.managed.json"
    if managed_path.exists():
        try:
            md = json.loads(managed_path.read_text(encoding="utf-8"))
            n_handles = len(md.get("handles") or [])
            print(f"  {_ok(f'managed.json        {n_handles} handles defined')}")
        except Exception:
            print(f"  {_warn('managed.json        exists but could not parse')}")
            has_warnings = True
    else:
        print(f"  {_warn('managed.json        not found (run scaffold-managed-bindings)')}")
        has_warnings = True

    # managed_api.source.json
    api_source_path = repo_root / "abi" / "bindings" / f"{target}.managed_api.source.json"
    if api_source_path.exists():
        try:
            ad = json.loads(api_source_path.read_text(encoding="utf-8"))
            schema_v = ad.get("schema_version", "?")
            auto_abi = ad.get("auto_abi_surface", {}).get("enabled", False)
            n_api_handles = len(ad.get("handle_api") or [])
            n_api_callbacks = len(ad.get("callbacks") or [])
            n_required = len(ad.get("required_native_functions") or [])

            # Count total opaque handles from IDL
            n_idl_handles = len(idl.get("bindings", {}).get("interop", {}).get("opaque_types") or {}) if idl else 0
            n_idl_funcs = len(idl.get("functions") or []) if idl else 0

            # Count TODO markers in assignment_lines
            todo_count = 0
            for cb in (ad.get("callbacks") or []):
                for field in (cb.get("fields") or []):
                    for line in (field.get("assignment_lines") or []):
                        if "TODO" in str(line):
                            todo_count += 1

            auto_abi_str = "enabled" if auto_abi else "disabled"
            print(f"  {_ok(f'managed_api.source  schema v{schema_v}, auto_abi_surface {auto_abi_str}')}")
            handles_status = f"{n_api_handles} / {n_idl_handles}" if n_idl_handles else str(n_api_handles)
            print(f"    {_arrow('Handles')}         {handles_status} covered")
            print(f"    {_arrow('Callbacks')}       {n_api_callbacks} callback structs")
            todo_indicator = _ok(f"{todo_count} remaining") if todo_count == 0 else _warn(f"{todo_count} remaining")
            print(f"    {_arrow('TODO markers')}    {todo_indicator}")
            if n_required > 0:
                req_status = f"{n_required} / {n_idl_funcs}" if n_idl_funcs else str(n_required)
                print(f"    {_arrow('Required funcs')}  {req_status} covered")

            if todo_count > 0:
                has_warnings = True
        except Exception as e:
            print(f"  {_warn(f'managed_api.source  exists but could not parse: {e}')}")
            has_warnings = True
    else:
        print(f"  {_warn('managed_api.source  not found (run scaffold-managed-api)')}")
        has_warnings = True

    # Codegen drift check
    # Try to run codegen --check to detect drift
    try:
        from .generation import command_codegen
        check_args = argparse.Namespace(
            repo_root=str(repo_root),
            config=str(config_path),
            target=target,
            binary=None,
            skip_binary=True,
            idl_output=None,
            dry_run=False,
            check=True,
            print_diff=False,
            report_json=None,
            fail_on_sync=False,
        )
        check_rc = command_codegen(check_args)
        if check_rc == 0:
            print(f"  {_ok('Codegen artifacts   all generators in sync')}")
        else:
            print(f"  {_warn('Codegen artifacts   generators out of sync (run codegen)')}")
            has_warnings = True
    except Exception:
        print(f"  {_warn('Codegen artifacts   could not check (run codegen manually)')}")
        has_warnings = True

    # Baseline
    baseline_rel = target_cfg.get("baseline_path", f"abi/baselines/{target}.json")
    baseline_path = repo_root / baseline_rel
    if baseline_path.exists():
        print(f"  {_ok(f'Baseline            exists ({baseline_rel})')}")
    else:
        print(f"  {_warn(f'Baseline            missing ({baseline_rel})')}")
        has_warnings = True

    return 1 if has_warnings else 0


def _arrow(label: str) -> str:
    return "\u21b3 " + label
