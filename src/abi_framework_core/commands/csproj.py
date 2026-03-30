from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def command_gen_csproj_snippet(args: argparse.Namespace) -> int:
    """
    Emit the AdditionalFiles XML ItemGroup needed to wire up AbiForge.RoslynGenerator
    in a .csproj file.

    For each target, outputs:
      <AdditionalFiles Include="..." AbiForgeTarget="..." />   ← IDL snapshot
      <AdditionalFiles Include="..." AbiForgeRole="managed_api" />
      <AdditionalFiles Include="..." AbiForgeRole="managed_bindings" />
    """
    config_path = Path(args.config).resolve()
    if not config_path.exists():
        print(f"error: config not found: {config_path}", file=sys.stderr)
        return 1

    repo_root = Path(getattr(args, "repo_root", ".")).resolve()

    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: invalid JSON: {e}", file=sys.stderr)
        return 1

    targets: dict[str, Any] = payload.get("targets") or {}
    filter_target = getattr(args, "target", None)
    if filter_target:
        if filter_target not in targets:
            print(f"error: target '{filter_target}' not found in config", file=sys.stderr)
            return 1
        targets = {filter_target: targets[filter_target]}

    # Optional: csproj path to compute relative paths from
    csproj_path_str = getattr(args, "csproj", None)
    csproj_dir = Path(csproj_path_str).resolve().parent if csproj_path_str else repo_root

    lines: list[str] = ["<ItemGroup>"]

    for target_name, target in targets.items():
        if not isinstance(target, dict):
            continue

        codegen = target.get("codegen") or {}
        bindings = target.get("bindings") or {}

        # IDL snapshot
        idl_path_raw = codegen.get("idl_output_path")
        if idl_path_raw:
            idl_abs = (repo_root / idl_path_raw).resolve()
            idl_rel = _make_rel(idl_abs, csproj_dir)
            lines.append(f'  <AdditionalFiles Include="{idl_rel}">')
            lines.append(f'    <AbiForgeTarget>{target_name}</AbiForgeTarget>')
            lines.append(f'  </AdditionalFiles>')

        # managed_api.json  (used by Roslyn generator)
        # Detect from generator entries or conventional path
        managed_api_path = _find_generator_output(bindings, "managed_api_json", repo_root) \
            or repo_root / "abi" / "bindings" / f"{target_name}.managed_api.json"
        if managed_api_path.exists():
            lines.append(f'  <AdditionalFiles Include="{_make_rel(managed_api_path, csproj_dir)}">')
            lines.append(f'    <AbiForgeRole>managed_api</AbiForgeRole>')
            lines.append(f'  </AdditionalFiles>')

        # managed.json (SafeHandle definitions)
        managed_bindings_path = _find_generator_output(bindings, "managed_bindings", repo_root) \
            or repo_root / "abi" / "bindings" / f"{target_name}.managed.json"
        if managed_bindings_path.exists():
            lines.append(f'  <AdditionalFiles Include="{_make_rel(managed_bindings_path, csproj_dir)}">')
            lines.append(f'    <AbiForgeRole>managed_bindings</AbiForgeRole>')
            lines.append(f'  </AdditionalFiles>')

    lines.append("</ItemGroup>")

    snippet = "\n".join(lines)

    out = getattr(args, "output", None)
    if out:
        Path(out).write_text(snippet + "\n", encoding="utf-8")
        print(f"Written: {out}")
    else:
        print(snippet)

    return 0


def _make_rel(abs_path: Path, base_dir: Path) -> str:
    """Return a relative path suitable for MSBuild (forward-slash-free on Windows too)."""
    try:
        return str(abs_path.relative_to(base_dir)).replace("/", "\\")
    except ValueError:
        return str(abs_path)


def _find_generator_output(
    bindings: dict[str, Any],
    output_name: str,
    repo_root: Path,
) -> Path | None:
    """
    Scan generator entries for a contract output matching output_name and return
    its resolved path (expanding {repo_root}/{target} tokens is non-trivial here,
    so we only handle the most common literal paths).
    """
    generators = bindings.get("generators") or []
    for gen in generators:
        if not isinstance(gen, dict):
            continue
        contracts = gen.get("contracts") or {}
        outputs = contracts.get("outputs") or []
        for out in outputs:
            if isinstance(out, dict) and out.get("name") == output_name:
                path_arg = out.get("path_arg")
                if path_arg:
                    cmd = gen.get("command") or []
                    try:
                        idx = cmd.index(path_arg)
                        raw = str(cmd[idx + 1])
                        raw = raw.replace("{repo_root}", str(repo_root))
                        p = Path(raw)
                        if not p.is_absolute():
                            p = repo_root / p
                        if p.exists():
                            return p
                    except (ValueError, IndexError):
                        pass
    return None
