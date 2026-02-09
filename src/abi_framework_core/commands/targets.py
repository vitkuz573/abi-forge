from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .common import get_targets_map

def command_list_targets(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    for name in sorted(targets.keys()):
        print(name)
    return 0



def command_init_target(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve()

    if config_path.exists():
        config = load_config(config_path)
    else:
        config = {
            "policy": {
                "waiver_requirements": dict(DEFAULT_WAIVER_REQUIREMENTS),
            },
            "targets": {},
        }

    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config root must contain object 'targets'.")

    if args.target in targets and not args.force:
        raise AbiFrameworkError(
            f"Target '{args.target}' already exists in config. Use --force to overwrite."
        )

    baseline_rel = args.baseline_path or f"abi/baselines/{args.target}.json"

    target_entry: dict[str, Any] = {
        "baseline_path": baseline_rel,
        "header": {
            "path": args.header_path,
            "api_macro": args.api_macro,
            "call_macro": args.call_macro,
            "symbol_prefix": args.symbol_prefix,
            "parser": {
                "backend": "clang_preprocess",
                "compiler": "clang",
                "compiler_candidates": default_parser_compiler_candidates_for_config(),
                "args": [],
                "include_dirs": [],
                "fallback_to_regex": True,
            },
            "version_macros": {
                "major": args.version_major_macro,
                "minor": args.version_minor_macro,
                "patch": args.version_patch_macro,
            },
            "types": {
                "enable_enums": True,
                "enable_structs": True,
                "enum_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "struct_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "ignore_enums": [],
                "ignore_structs": [],
                "struct_tail_addition_is_breaking": True,
            },
        },
        "codegen": {
            "enabled": True,
            "idl_output_path": f"abi/generated/{args.target}.idl.json",
        },
    }

    if args.binding_symbol:
        target_entry["bindings"] = {
            "expected_symbols": args.binding_symbol,
        }

    if args.binary_path:
        target_entry["binary"] = {
            "path": args.binary_path,
            "allow_non_prefixed_exports": False,
        }

    targets[args.target] = target_entry
    if not isinstance(config.get("policy"), dict):
        config["policy"] = {}
    root_policy = config["policy"]
    if not isinstance(root_policy.get("waiver_requirements"), dict):
        root_policy["waiver_requirements"] = dict(DEFAULT_WAIVER_REQUIREMENTS)
    config["targets"] = targets
    write_json(config_path, config)

    if args.create_baseline:
        snapshot = build_snapshot(
            config=config,
            target_name=args.target,
            repo_root=repo_root,
            binary_override=None,
            skip_binary=True,
        )
        baseline_path = ensure_relative_path(repo_root, baseline_rel).resolve()
        write_json(baseline_path, snapshot)
        print(f"Created baseline: {baseline_path}")

    print(f"Target '{args.target}' initialized in {config_path}")
    return 0


