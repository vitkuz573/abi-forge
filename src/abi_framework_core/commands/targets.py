from __future__ import annotations

import argparse
import json as _json
import sys

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

    # Derive sensible defaults from the target name when not provided
    target_upper = args.target.upper().replace("-", "_")
    target_lower = args.target.lower().replace("-", "_")
    api_macro = getattr(args, "api_macro", None) or f"{target_upper}_API"
    call_macro = getattr(args, "call_macro", None) or f"{target_upper}_CALL"
    symbol_prefix = getattr(args, "symbol_prefix", None) or f"{target_lower}_"

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
            "api_macro": api_macro,
            "call_macro": call_macro,
            "symbol_prefix": symbol_prefix,
            "parser": {
                "backend": "clang_preprocess",
                "compiler": "clang",
                "compiler_candidates": default_parser_compiler_candidates_for_config(),
                "args": [],
                "include_dirs": [],
                "fallback_to_regex": True,
            },
            "version_macros": {
                "major": getattr(args, "version_major_macro", None) or f"{target_upper}_VERSION_MAJOR",
                "minor": getattr(args, "version_minor_macro", None) or f"{target_upper}_VERSION_MINOR",
                "patch": getattr(args, "version_patch_macro", None) or f"{target_upper}_VERSION_PATCH",
            },
            "types": {
                "enable_enums": True,
                "enable_structs": True,
                "enum_name_pattern": f"^{re.escape(symbol_prefix)}",
                "struct_name_pattern": f"^{re.escape(symbol_prefix)}",
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
            "symbol_contract": {
                "mode": "strict",
                "symbols": args.binding_symbol,
            },
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


def _print_next_steps(out_path: Path) -> None:
    print("")
    print("Next steps:")
    print(f"  1. Edit {out_path.name} and fill in TODO markers in callbacks[].fields[].assignment_lines")
    print("  2. Run: abi_framework codegen --config abi/config.json --skip-binary")
    print("  3. The Roslyn source generator produces all C# interop automatically from the IDL")
    print("")
    print("Tip: auto_abi_surface is enabled — ALL P/Invoke methods are generated for free.")
    print("     Only callback lambda bodies and handle API members need manual implementation.")


def command_scaffold_managed_api(args: argparse.Namespace) -> int:
    """Scaffold a managed_api.source.json from an IDL JSON."""
    generator_sdk = Path(__file__).resolve().parents[3] / "generator_sdk"
    if str(generator_sdk) not in sys.path:
        sys.path.insert(0, str(generator_sdk))

    CORE_SRC = Path(__file__).resolve().parents[4] / "abi_codegen_core" / "src"
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))

    try:
        import managed_api_scaffold_generator as scaffold_mod  # type: ignore[import]
    except ImportError:
        repo_root_attr = getattr(args, "repo_root", ".")
        repo_root_p = Path(repo_root_attr).resolve()
        sdk_path = repo_root_p / "tools" / "abi_framework" / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import managed_api_scaffold_generator as scaffold_mod  # type: ignore[import]

    from abi_codegen_core.common import load_json_object, write_if_changed  # type: ignore[import]

    idl_path = Path(args.idl).resolve()
    out_path_raw = getattr(args, "out", None)
    out_path: Path
    if out_path_raw:
        out_path = Path(out_path_raw).resolve()
    else:
        stem = idl_path.stem
        for suf in (".idl",):
            stem = stem.removesuffix(suf)
        out_path = idl_path.parent.parent.parent / "bindings" / f"{stem}.managed_api.source.json"

    force = getattr(args, "force", False)
    update = getattr(args, "update", False)
    check = getattr(args, "check", False)
    dry_run = getattr(args, "dry_run", False)

    idl = load_json_object(idl_path)
    generated = scaffold_mod.scaffold(idl, args.namespace, getattr(args, "symbol_prefix", None))

    if update and out_path.exists():
        existing = load_json_object(out_path)
        merged, stats = scaffold_mod._update_existing(existing, generated)
        content = _json.dumps(merged, ensure_ascii=False, indent=2) + "\n"
        if not check and not dry_run:
            print(f"scaffold --update: added {stats['added_callbacks']} callbacks, "
                  f"{stats['added_handles']} handles; "
                  f"kept {stats['kept_callbacks']} callbacks, {stats['kept_handles']} handles")
    elif out_path.exists() and not force and not check and not dry_run:
        print(f"scaffold-managed-api: '{out_path}' exists. Use --force to overwrite or --update to merge.")
        return 0
    else:
        content = _json.dumps(generated, ensure_ascii=False, indent=2) + "\n"

    status = write_if_changed(out_path, content, check, dry_run)
    if status == 0 and not check and not dry_run:
        if not update:
            print(f"Scaffolded: {out_path}")
        _print_next_steps(out_path)
    return status


def command_scaffold_managed_bindings(args: argparse.Namespace) -> int:
    """Scaffold managed.json (SafeHandle definitions) from IDL opaque_types."""
    generator_sdk = Path(__file__).resolve().parents[3] / "generator_sdk"
    if str(generator_sdk) not in sys.path:
        sys.path.insert(0, str(generator_sdk))

    CORE_SRC = Path(__file__).resolve().parents[4] / "abi_codegen_core" / "src"
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))

    try:
        import managed_bindings_scaffold_generator as bindings_mod  # type: ignore[import]
    except ImportError:
        repo_root = Path(getattr(args, "repo_root", ".")).resolve()
        sdk_path = repo_root / "tools" / "abi_framework" / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import managed_bindings_scaffold_generator as bindings_mod  # type: ignore[import]

    from abi_codegen_core.common import load_json_object, write_if_changed  # type: ignore[import]

    idl_path = Path(args.idl).resolve()
    out_path_raw = getattr(args, "out", None)
    if out_path_raw:
        out_path = Path(out_path_raw).resolve()
    else:
        stem = idl_path.stem
        for suf in (".idl",):
            stem = stem.removesuffix(suf)
        out_path = idl_path.parent.parent.parent / "bindings" / f"{stem}.managed.json"

    force = getattr(args, "force", False)
    check = getattr(args, "check", False)
    dry_run = getattr(args, "dry_run", False)

    if out_path.exists() and not force and not check and not dry_run:
        print(f"scaffold-managed-bindings: '{out_path}' exists. Use --force to overwrite.")
        return 0

    idl = load_json_object(idl_path)
    result = bindings_mod.scaffold_managed_bindings(idl, args.namespace, getattr(args, "symbol_prefix", None))
    content = _json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    status = write_if_changed(out_path, content, check, dry_run)
    if status == 0 and not check and not dry_run:
        print(f"Scaffolded: {out_path}")
        print("")
        print("Next steps:")
        print(f"  1. Review generated handles in {out_path.name}")
        print("  2. Verify release/retain functions match your C API")
        print("  3. Run: abi_framework scaffold-managed-api --idl <idl> --namespace <ns>")
        print("  4. Run: abi_framework codegen --config abi/config.json --skip-binary")
    return status


def _load_generator_module(args: argparse.Namespace, module_name: str) -> Any:
    """Load a generator_sdk module, searching standard paths."""
    generator_sdk = Path(__file__).resolve().parents[3] / "generator_sdk"
    if str(generator_sdk) not in sys.path:
        sys.path.insert(0, str(generator_sdk))
    try:
        import importlib
        return importlib.import_module(module_name)
    except ImportError:
        repo_root = Path(getattr(args, "repo_root", ".")).resolve()
        sdk_path = repo_root / "tools" / "abi_framework" / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import importlib
        return importlib.import_module(module_name)


def command_generate_python_bindings(args: argparse.Namespace) -> int:
    """Generate Python ctypes bindings from IDL JSON."""
    import json
    from abi_codegen_core.common import write_if_changed  # type: ignore[import]

    CORE_SRC = Path(__file__).resolve().parents[4] / "abi_codegen_core" / "src"
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))

    mod = _load_generator_module(args, "python_bindings_generator")

    idl_path = Path(args.idl).resolve()
    out_path = Path(args.out).resolve()
    check = getattr(args, "check", False)
    dry_run = getattr(args, "dry_run", False)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = mod.generate_bindings(idl, getattr(args, "symbol_prefix", None))

    status = write_if_changed(out_path, content, check, dry_run)
    if status == 0 and not check and not dry_run:
        print(f"Generated Python ctypes bindings: {out_path}")
    return status


def command_generate_rust_ffi(args: argparse.Namespace) -> int:
    """Generate Rust FFI bindings from IDL JSON."""
    import json
    from abi_codegen_core.common import write_if_changed  # type: ignore[import]

    CORE_SRC = Path(__file__).resolve().parents[4] / "abi_codegen_core" / "src"
    if str(CORE_SRC) not in sys.path:
        sys.path.insert(0, str(CORE_SRC))

    mod = _load_generator_module(args, "rust_ffi_generator")

    idl_path = Path(args.idl).resolve()
    out_path = Path(args.out).resolve()
    check = getattr(args, "check", False)
    dry_run = getattr(args, "dry_run", False)

    with idl_path.open("r", encoding="utf-8") as f:
        idl = json.load(f)

    content = mod.generate_rust_ffi(idl, getattr(args, "symbol_prefix", None))

    status = write_if_changed(out_path, content, check, dry_run)
    if status == 0 and not check and not dry_run:
        print(f"Generated Rust FFI bindings: {out_path}")
    return status


