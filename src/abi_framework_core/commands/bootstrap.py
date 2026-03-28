from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .targets import command_init_target, command_scaffold_managed_api, command_scaffold_managed_bindings
from .generation import command_generate


def command_bootstrap(args: argparse.Namespace) -> int:
    """Full setup pipeline: init-target → generate → scaffold-managed-bindings → scaffold-managed-api."""
    repo_root = Path(getattr(args, "repo_root", ".")).resolve()
    target = args.target
    generate_python = getattr(args, "generate_python", False)
    generate_rust = getattr(args, "generate_rust", False)

    print(f"[bootstrap] Starting setup for target '{target}'...")

    # Step 1: init-target
    init_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=args.config,
        target=args.target,
        header_path=args.header_path,
        api_macro=getattr(args, "api_macro", "") or "",
        call_macro=getattr(args, "call_macro", "") or "",
        symbol_prefix=getattr(args, "symbol_prefix", "") or "",
        version_major_macro=getattr(args, "version_major_macro", "") or "",
        version_minor_macro=getattr(args, "version_minor_macro", "") or "",
        version_patch_macro=getattr(args, "version_patch_macro", "") or "",
        binary_path=getattr(args, "binary_path", None),
        baseline_path=None,
        create_baseline=True,
        no_create_baseline=False,
        binding_symbol=None,
        add_generators="none",
        force=getattr(args, "force", False),
    )
    rc = command_init_target(init_args)
    if rc != 0:
        print(f"[bootstrap] ✗ init-target failed (exit {rc})", file=sys.stderr)
        return rc
    print(f"[bootstrap] ✓ init-target: target '{target}' initialized")

    # Step 2: generate IDL
    generate_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=args.config,
        target=args.target,
        binary=None,
        skip_binary=True,
        idl_output=None,
        dry_run=False,
        check=False,
        print_diff=False,
        report_json=None,
        fail_on_sync=False,
    )
    rc = command_generate(generate_args)
    if rc != 0:
        print(f"[bootstrap] ✗ generate failed (exit {rc})", file=sys.stderr)
        return rc

    # Find the generated IDL to report stats
    idl_path = repo_root / "abi" / "generated" / target / f"{target}.idl.json"
    idl_stats = ""
    if idl_path.exists():
        import json
        try:
            idl = json.loads(idl_path.read_text(encoding="utf-8"))
            n_funcs = len(idl.get("functions") or [])
            n_enums = len(idl.get("header_types", {}).get("enums") or {})
            n_structs = len(idl.get("header_types", {}).get("structs") or {})
            idl_stats = f" ({n_funcs} functions, {n_enums} enums, {n_structs} structs)"
        except Exception:
            pass
    print(f"[bootstrap] ✓ generate: IDL created{idl_stats}")

    # Step 3: scaffold-managed-bindings
    bindings_out = repo_root / "abi" / "bindings" / f"{target}.managed.json"
    scaffold_bindings_args = argparse.Namespace(
        repo_root=str(repo_root),
        idl=str(idl_path),
        namespace=args.namespace,
        out=str(bindings_out),
        symbol_prefix=getattr(args, "symbol_prefix", None) or None,
        force=True,
        check=False,
        dry_run=False,
    )
    rc = command_scaffold_managed_bindings(scaffold_bindings_args)
    if rc != 0:
        print(f"[bootstrap] ✗ scaffold-managed-bindings failed (exit {rc})", file=sys.stderr)
        return rc

    n_handles = 0
    if bindings_out.exists():
        try:
            import json
            bd = json.loads(bindings_out.read_text(encoding="utf-8"))
            n_handles = len(bd.get("handles") or [])
        except Exception:
            pass
    print(f"[bootstrap] ✓ scaffold-managed-bindings: {n_handles} handles → {bindings_out.relative_to(repo_root)}")

    # Step 4: scaffold-managed-api
    api_out = repo_root / "abi" / "bindings" / f"{target}.managed_api.source.json"
    scaffold_api_args = argparse.Namespace(
        repo_root=str(repo_root),
        idl=str(idl_path),
        namespace=args.namespace,
        out=str(api_out),
        symbol_prefix=getattr(args, "symbol_prefix", None) or None,
        force=True,
        update=False,
        check=False,
        dry_run=False,
    )
    rc = command_scaffold_managed_api(scaffold_api_args)
    if rc != 0:
        print(f"[bootstrap] ✗ scaffold-managed-api failed (exit {rc})", file=sys.stderr)
        return rc

    n_api_handles = 0
    n_api_callbacks = 0
    if api_out.exists():
        try:
            import json
            ad = json.loads(api_out.read_text(encoding="utf-8"))
            n_api_handles = len(ad.get("handle_api") or [])
            n_api_callbacks = len(ad.get("callbacks") or [])
        except Exception:
            pass
    print(f"[bootstrap] ✓ scaffold-managed-api: {n_api_handles} handles, {n_api_callbacks} callbacks → {api_out.relative_to(repo_root)}")

    # Step 5 (optional): generate-python-bindings
    if generate_python and idl_path.exists():
        py_out = repo_root / "abi" / "generated" / target / f"{target}_ctypes.py"
        try:
            _run_generator_sdk(repo_root, "python_bindings_generator", idl_path, py_out)
            print(f"[bootstrap] ✓ generate-python-bindings: {py_out.relative_to(repo_root)}")
        except Exception as e:
            print(f"[bootstrap] ⚠ generate-python-bindings failed: {e}", file=sys.stderr)

    # Step 6 (optional): generate-rust-ffi
    if generate_rust and idl_path.exists():
        rs_out = repo_root / "abi" / "generated" / target / f"{target}_ffi.rs"
        try:
            _run_generator_sdk(repo_root, "rust_ffi_generator", idl_path, rs_out)
            print(f"[bootstrap] ✓ generate-rust-ffi: {rs_out.relative_to(repo_root)}")
        except Exception as e:
            print(f"[bootstrap] ⚠ generate-rust-ffi failed: {e}", file=sys.stderr)

    # Print checklist
    print("")
    print("Setup complete! TODO checklist:")
    print(f"  1. Fill in TODO markers in {api_out.relative_to(repo_root)}")
    print(f"  2. Verify release functions in {bindings_out.relative_to(repo_root)}")
    print("  3. Run: abi_framework codegen --config abi/config.json --skip-binary")
    print("  4. dotnet build -> Roslyn generates all C# interop automatically")
    return 0


def _run_generator_sdk(repo_root: Path, module_name: str, idl_path: Path, out_path: Path) -> None:
    """Run a generator_sdk module directly."""
    sdk_path = repo_root / "tools" / "abi_framework" / "generator_sdk"
    if str(sdk_path) not in sys.path:
        sys.path.insert(0, str(sdk_path))

    if module_name == "python_bindings_generator":
        import python_bindings_generator as mod  # type: ignore[import]
        import json
        idl = json.loads(idl_path.read_text(encoding="utf-8"))
        content = mod.generate_bindings(idl)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    elif module_name == "rust_ffi_generator":
        import rust_ffi_generator as mod  # type: ignore[import]
        import json
        idl = json.loads(idl_path.read_text(encoding="utf-8"))
        content = mod.generate_rust_ffi(idl)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
    else:
        raise ValueError(f"Unknown generator: {module_name}")
