from __future__ import annotations

"""
new-lib: Full project scaffolding wizard.

Given a target name (and optional existing header), scaffold a complete project:
  - include/{target}.h           stub C header (or copy existing)
  - native/src/{target}.c        stub implementation
  - native/CMakeLists.txt        minimal CMake
  - abi/config.json              pre-filled ABI config
  - appveyor.yml                 ready-to-run CI
  - .gitignore
  - README.md                    getting-started stub

Optional (--dotnet):
  - src/{Namespace}/{Namespace}.csproj
  - src/{Namespace}/README.md
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from .scan_header import scan_header_file


# ---------------------------------------------------------------------------
# Template helpers
# ---------------------------------------------------------------------------

def _upper(s: str) -> str:
    return s.upper().replace("-", "_")


def _pascal(s: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[_\-\s]+", s) if w)


# ---------------------------------------------------------------------------
# File templates
# ---------------------------------------------------------------------------

def _stub_header(target: str, api_macro: str, call_macro: str, symbol_prefix: str) -> str:
    guard = f"{_upper(target)}_H"
    call = f" {call_macro}" if call_macro else ""
    return f"""\
#ifndef {guard}
#define {guard}

#ifdef __cplusplus
extern "C" {{
#endif

#ifndef {api_macro}
#  if defined(_WIN32)
#    define {api_macro} __declspec(dllexport)
#  else
#    define {api_macro} __attribute__((visibility("default")))
#  endif
#endif

/* ---- version ---- */
#define {_upper(target)}_VERSION_MAJOR 0
#define {_upper(target)}_VERSION_MINOR 1
#define {_upper(target)}_VERSION_PATCH 0

/* ---- opaque handle ---- */
typedef struct {symbol_prefix}context_t {symbol_prefix}context_t;

/* ---- lifecycle ---- */
{api_macro} {symbol_prefix}context_t *{call}{symbol_prefix}context_create(void);
{api_macro} void{call} {symbol_prefix}context_destroy({symbol_prefix}context_t *ctx);

/* ---- ABI version query ---- */
{api_macro} int{call} {symbol_prefix}abi_version_major(void);
{api_macro} int{call} {symbol_prefix}abi_version_minor(void);
{api_macro} int{call} {symbol_prefix}abi_version_patch(void);

#ifdef __cplusplus
}}
#endif
#endif /* {guard} */
"""


def _stub_impl(target: str, symbol_prefix: str) -> str:
    return f"""\
#include "{target}.h"
#include <stdlib.h>

struct {symbol_prefix}context_t {{
    int reserved;
}};

{symbol_prefix}context_t *{symbol_prefix}context_create(void) {{
    return calloc(1, sizeof({symbol_prefix}context_t));
}}

void {symbol_prefix}context_destroy({symbol_prefix}context_t *ctx) {{
    free(ctx);
}}

int {symbol_prefix}abi_version_major(void) {{ return {_upper(target)}_VERSION_MAJOR; }}
int {symbol_prefix}abi_version_minor(void) {{ return {_upper(target)}_VERSION_MINOR; }}
int {symbol_prefix}abi_version_patch(void) {{ return {_upper(target)}_VERSION_PATCH; }}
"""


def _cmake(target: str) -> str:
    return f"""\
cmake_minimum_required(VERSION 3.16)
project({target} C)

add_library({target} SHARED
    src/{target}.c
)

target_include_directories({target} PUBLIC
    ${{CMAKE_CURRENT_SOURCE_DIR}}/../include
)

set_target_properties({target} PROPERTIES
    C_STANDARD 11
    POSITION_INDEPENDENT_CODE ON
)

if(MSVC)
    target_compile_options({target} PRIVATE /W4)
else()
    target_compile_options({target} PRIVATE -Wall -Wextra)
endif()
"""


def _abi_config(
    target: str,
    header_rel: str,
    api_macro: str,
    call_macro: str,
    symbol_prefix: str,
    dotnet: bool,
    python: bool,
    rust: bool,
    typescript: bool,
    go: bool,
) -> dict[str, Any]:
    generators: list[dict[str, Any]] = [
        {"name": "symbol_contract", "kind": "external", "plugin": "abi_framework.symbol_contract"},
    ]
    if dotnet:
        generators += [
            {"name": "native_exports", "kind": "external", "plugin": "abi_framework.native_exports"},
            {"name": "managed_api_metadata", "kind": "external", "plugin": "abi_framework.managed_api_metadata"},
            {"name": "native_impl_handles", "kind": "external", "plugin": "abi_framework.native_impl_handles"},
        ]
    if python:
        generators.append({"name": "python_bindings", "kind": "external", "plugin": "abi_framework.python_bindings"})
    if rust:
        generators.append({"name": "rust_ffi", "kind": "external", "plugin": "abi_framework.rust_ffi"})
    if typescript:
        generators.append({"name": "typescript_bindings", "kind": "external", "plugin": "abi_framework.typescript_bindings"})
    if go:
        generators.append({"name": "go_bindings", "kind": "external", "plugin": "abi_framework.go_bindings"})

    header_cfg: dict[str, Any] = {
        "path": header_rel,
        "api_macro": api_macro,
        "symbol_prefix": symbol_prefix,
        "version_macros": {
            "major": f"{_upper(target)}_VERSION_MAJOR",
            "minor": f"{_upper(target)}_VERSION_MINOR",
            "patch": f"{_upper(target)}_VERSION_PATCH",
        },
        "parser": {
            "backend": "clang_preprocess",
            "fallback_to_regex": True,
        },
        "types": {
            "enable_enums": True,
            "enable_structs": True,
        },
    }
    if call_macro:
        header_cfg["call_macro"] = call_macro

    return {
        "targets": {
            target: {
                "baseline_path": f"abi/baselines/{target}.json",
                "header": header_cfg,
                "codegen": {
                    "idl_output_path": f"abi/generated/{target}/{target}.idl.json",
                    "native_header_output_path": f"native/include/{target}.h",
                    "native_export_map_output_path": f"native/{target}.map",
                },
                "bindings": {
                    "generators": generators,
                },
            }
        }
    }


def _csproj(namespace: str, target: str) -> str:
    return f"""\
<Project Sdk="Microsoft.NET.Sdk">

  <PropertyGroup>
    <TargetFramework>net10.0</TargetFramework>
    <Nullable>enable</Nullable>
    <ImplicitUsings>enable</ImplicitUsings>
    <AllowUnsafeBlocks>true</AllowUnsafeBlocks>
    <RootNamespace>{namespace}</RootNamespace>
    <AssemblyName>{namespace}</AssemblyName>
  </PropertyGroup>

  <ItemGroup>
    <PackageReference Include="AbiForge.RoslynGenerator" Version="*" PrivateAssets="all" />
  </ItemGroup>

  <!-- ABI IDL + managed metadata wired into Roslyn source generator -->
  <ItemGroup>
    <AdditionalFiles Include="..\\..\\abi\\generated\\{target}\\{target}.idl.json">
      <AbiForgeTarget>{target}</AbiForgeTarget>
    </AdditionalFiles>
    <AdditionalFiles Include="..\\..\\abi\\bindings\\{target}.managed_api.json">
      <AbiForgeRole>managed_api</AbiForgeRole>
    </AdditionalFiles>
    <AdditionalFiles Include="..\\..\\abi\\bindings\\{target}.managed.json">
      <AbiForgeRole>managed_bindings</AbiForgeRole>
    </AdditionalFiles>
  </ItemGroup>

</Project>
"""


def _appveyor(target: str, dotnet: bool) -> str:
    dotnet_block = ""
    if dotnet:
        dotnet_block = """\

      if ! dotnet --list-sdks | grep -q '^10\\.'; then
        curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh
        bash /tmp/dotnet-install.sh --channel 10.0 --install-dir "$HOME/.dotnet"
      fi
      export PATH="$HOME/.dotnet:$PATH"
      dotnet --info
"""

    dotnet_build = ""
    if dotnet:
        dotnet_build = f"""
      dotnet build src/{_pascal(target)}/{_pascal(target)}.csproj -v minimal
"""

    return f"""\
version: "{{build}}"

max_jobs: 1
image: Ubuntu2204

build: off
test: off

environment:
  DOTNET_SKIP_FIRST_TIME_EXPERIENCE: "1"
  DOTNET_CLI_TELEMETRY_OPTOUT: "1"
  NUGET_XMLDOC_MODE: "skip"

install:
  - sh: |
      set -euo pipefail
      pip install abi-forge --break-system-packages
{dotnet_block}
      if ! command -v clang >/dev/null 2>&1; then
        sudo apt-get update && sudo apt-get install -y clang
      fi
      clang --version && python3 --version

build_script:
  - sh: |
      set -euo pipefail
{dotnet_block and "      export PATH=$HOME/.dotnet:$PATH" or ""}
      mkdir -p artifacts/abi

      abi_framework check --skip-binary --output-dir artifacts/abi
{dotnet_build}
artifacts:
  - path: artifacts/abi/**
    name: abi-report
"""


def _gitignore() -> str:
    return """\
# Build outputs
native/build/
*.o *.obj *.a *.so *.dylib *.dll *.lib
*.pdb *.ilk *.exp

# .NET
bin/ obj/
*.user

# Python
__pycache__/ *.pyc *.pyo .venv/

# ABI forge cache
.abi-forge-cache/

# IDE
.idea/ .vs/ .vscode/
*.suo *.sln.docstates

# OS
.DS_Store Thumbs.db
"""


def _readme(target: str, namespace: str | None, dotnet: bool) -> str:
    dotnet_section = ""
    if dotnet:
        dotnet_section = f"""
## .NET bindings

The `{namespace}` project uses `AbiForge.RoslynGenerator` to generate all C# interop at build time
from `abi/generated/{target}/{target}.idl.json` and `abi/bindings/*.json`.

```bash
dotnet build src/{namespace}/{namespace}.csproj
```
"""
    return f"""\
# {_pascal(target)}

> Generated by `abi_framework new-lib`

## Quick start

```bash
# Install abi-forge
pip install abi-forge

# Build native library
cmake -S native -B native/build && cmake --build native/build
{dotnet_section}
# Parse header → IDL → run all generators
abi_framework gen --skip-binary

# Lock current IDL as baseline (first time)
abi_framework generate-baseline

# Verify nothing broke
abi_framework check --skip-binary
```

## ABI workflow

```bash
abi_framework gen --skip-binary          # regenerate everything
abi_framework check --skip-binary        # local CI check
abi_framework generate-baseline          # update baseline after intentional change
abi_framework doctor                     # diagnose config + environment
abi_framework status --target {target}   # show per-target health
```
"""


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------

def command_new_lib(args: argparse.Namespace) -> int:
    target: str = args.target
    output_dir = Path(getattr(args, "output_dir", target)).resolve()
    namespace: str = getattr(args, "namespace", None) or _pascal(target)
    dotnet: bool = bool(getattr(args, "dotnet", False))
    python: bool = bool(getattr(args, "python", False))
    rust: bool = bool(getattr(args, "rust", False))
    typescript: bool = bool(getattr(args, "typescript", False))
    go: bool = bool(getattr(args, "go", False))
    existing_header: str | None = getattr(args, "header", None)
    force: bool = bool(getattr(args, "force", False))

    if output_dir.exists() and not force:
        # Check if it has any non-.git content
        entries = [e for e in output_dir.iterdir() if e.name not in (".git",)]
        if entries:
            print(
                f"error: '{output_dir}' already exists and is non-empty. Use --force to overwrite.",
                file=sys.stderr,
            )
            return 1

    # --- Detect config from existing header ---
    api_macro = getattr(args, "api_macro", "") or f"{_upper(target)}_API"
    call_macro = getattr(args, "call_macro", "") or ""
    symbol_prefix = getattr(args, "symbol_prefix", "") or f"{target}_"

    if existing_header:
        hp = Path(existing_header).resolve()
        if hp.exists():
            detected = scan_header_file(hp)
            if not getattr(args, "api_macro", ""):
                api_macro = detected["api_macro"] or api_macro
            if not getattr(args, "call_macro", ""):
                call_macro = detected["call_macro"] or call_macro
            if not getattr(args, "symbol_prefix", ""):
                symbol_prefix = detected["symbol_prefix"] or symbol_prefix
            print(f"[new-lib] Detected from header: api_macro={api_macro!r} symbol_prefix={symbol_prefix!r}")

    # --- Scaffold files ---
    def write(rel: str, content: str) -> None:
        p = output_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        print(f"  + {rel}")

    print(f"[new-lib] Scaffolding '{target}' in {output_dir} ...")

    # Header
    if existing_header and Path(existing_header).exists():
        import shutil
        dest = output_dir / "include" / Path(existing_header).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(existing_header, dest)
        print(f"  + include/{Path(existing_header).name}  (copied)")
        header_rel = f"include/{Path(existing_header).name}"
    else:
        write(f"include/{target}.h", _stub_header(target, api_macro, call_macro, symbol_prefix))
        header_rel = f"include/{target}.h"

    # Native stub
    write(f"native/src/{target}.c", _stub_impl(target, symbol_prefix))
    write("native/CMakeLists.txt", _cmake(target))

    # ABI config
    cfg = _abi_config(
        target=target,
        header_rel=header_rel,
        api_macro=api_macro,
        call_macro=call_macro,
        symbol_prefix=symbol_prefix,
        dotnet=dotnet,
        python=python,
        rust=rust,
        typescript=typescript,
        go=go,
    )
    write("abi/config.json", json.dumps(cfg, indent=2) + "\n")

    # .NET project
    if dotnet:
        write(f"src/{namespace}/{namespace}.csproj", _csproj(namespace, target))

    # CI + misc
    write("appveyor.yml", _appveyor(target, dotnet))
    write(".gitignore", _gitignore())
    write("README.md", _readme(target, namespace if dotnet else None, dotnet))

    print()
    print(f"[new-lib] Done! Next steps:")
    print(f"  cd {output_dir}")
    print(f"  abi_framework gen --skip-binary          # parse header → IDL → generators")
    print(f"  abi_framework generate-baseline          # lock initial baseline")
    print(f"  abi_framework check --skip-binary        # verify everything")
    if dotnet:
        print(f"  dotnet build src/{namespace}/{namespace}.csproj")
    return 0
