from __future__ import annotations

import argparse

from .generation import command_codegen, command_generate
from .verification import command_verify_all
from .plugins import command_validate_plugin_manifest


def command_gen(args: argparse.Namespace) -> int:
    """Generate IDL + run all code generators in one shot (generate + codegen)."""
    gen_args = argparse.Namespace(
        repo_root=args.repo_root,
        config=args.config,
        target=getattr(args, "target", None),
        binary=getattr(args, "binary", None),
        skip_binary=getattr(args, "skip_binary", False),
        idl_output=None,
        dry_run=getattr(args, "dry_run", False),
        check=getattr(args, "check", False),
        print_diff=getattr(args, "print_diff", False),
        report_json=None,
        fail_on_sync=getattr(args, "fail_on_sync", False),
        force_regen=getattr(args, "force_regen", False),
    )
    rc = command_generate(gen_args)
    if rc != 0:
        return rc

    codegen_args = argparse.Namespace(
        repo_root=args.repo_root,
        config=args.config,
        target=getattr(args, "target", None),
        binary=getattr(args, "binary", None),
        skip_binary=getattr(args, "skip_binary", False),
        idl_output=None,
        dry_run=getattr(args, "dry_run", False),
        check=getattr(args, "check", False),
        print_diff=getattr(args, "print_diff", False),
        report_json=getattr(args, "report_json", None),
        fail_on_sync=getattr(args, "fail_on_sync", False),
        force_regen=getattr(args, "force_regen", False),
    )
    return command_codegen(codegen_args)


def command_check(args: argparse.Namespace) -> int:
    """Full local CI suite: plugin validation + codegen drift + ABI verification."""
    exit_code = 0
    repo_root = getattr(args, "repo_root", ".")
    config = getattr(args, "config", "abi/config.json")
    skip_binary = getattr(args, "skip_binary", False)
    fail_on_warnings = getattr(args, "fail_on_warnings", False)
    output_dir = getattr(args, "output_dir", None)

    print("[check] 1/3 validate-plugin-manifest...")
    manifest_args = argparse.Namespace(
        manifest=None,
        config=config,
        repo_root=repo_root,
        target=None,
        output=None,
        print_json=False,
        fail_on_warnings=fail_on_warnings,
    )
    rc = command_validate_plugin_manifest(manifest_args)
    if rc != 0:
        exit_code = rc
    print(f"[check] 1/3 validate-plugin-manifest: {'FAIL' if rc != 0 else 'pass'}")

    print("[check] 2/3 codegen --check --fail-on-sync...")
    codegen_args = argparse.Namespace(
        repo_root=repo_root,
        config=config,
        target=getattr(args, "target", None),
        binary=None,
        skip_binary=skip_binary,
        idl_output=None,
        dry_run=False,
        check=True,
        print_diff=getattr(args, "print_diff", False),
        report_json=getattr(args, "report_json", None),
        fail_on_sync=True,
        force_regen=False,
    )
    rc = command_codegen(codegen_args)
    if rc != 0:
        exit_code = rc
    print(f"[check] 2/3 codegen: {'FAIL' if rc != 0 else 'pass'}")

    print("[check] 3/3 verify-all...")
    verify_args = argparse.Namespace(
        repo_root=repo_root,
        config=config,
        baseline_root=None,
        binary=None,
        skip_binary=skip_binary,
        output_dir=output_dir,
        sarif_report=None,
        fail_on_warnings=fail_on_warnings,
        output_format="text",
    )
    rc = command_verify_all(verify_args)
    if rc != 0:
        exit_code = rc
    print(f"[check] 3/3 verify-all: {'FAIL' if rc != 0 else 'pass'}")

    print(f"[check] result: {'FAIL' if exit_code != 0 else 'pass'}")
    return exit_code
