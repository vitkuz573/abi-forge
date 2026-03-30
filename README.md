# abi-forge

**Reusable ABI governance and polyglot binding generator for C shared libraries.**

From a single C header, abi-forge produces a versioned IDL snapshot and generates bindings for every language you need — with built-in breakage detection and governance.

## What it does

| Stage | What happens |
|-------|-------------|
| **Parse** | Clang-preprocesses your C header into a stable IDL JSON snapshot |
| **Govern** | Diffs every new snapshot against the baseline; flags removals, signature changes, enum shifts as typed errors (ABI001–ABI007) |
| **Generate** | Runs your chosen language generators from the IDL: C#, Python, Rust, TypeScript, Go |
| **Enforce** | CI fails on ABI regressions; waivers require explicit approval with TTL |

## Language targets

| Language | Output | Mechanism |
|----------|--------|-----------|
| **C# (.NET)** | P/Invoke + SafeHandle wrappers + async managed layer | Roslyn source generator (`AbiForge.RoslynGenerator` NuGet) |
| **Python** | ctypes module with IntEnum, OOP handles, CFUNCTYPE | `generator_sdk/python_bindings_generator.py` |
| **Rust** | `#[repr(C)]` enums + `extern "C"` block | `generator_sdk/rust_ffi_generator.py` |
| **TypeScript** | ffi-napi + OOP wrappers with `[Symbol.dispose]` | `generator_sdk/typescript_bindings_generator.py` |
| **Go** | cgo package with struct wrappers and `runtime.SetFinalizer` | `generator_sdk/go_bindings_generator.py` |

## Real-world example

[LumenRTC](https://github.com/vitkuz573/LumenRTC) — a .NET wrapper over `libwebrtc` — uses abi-forge to govern its C ABI and generate all language bindings from a single IDL snapshot. See `abi/config.json` there for a complete production configuration.

## Quick start

### Install

```bash
pip install abi-forge
```

Requires Python 3.10+ and clang. .NET SDK 10+ only needed for C# bindings.

### Bootstrap a new target

```bash
# Bootstrap: scaffolds abi/config.json + initial metadata files
abi_framework bootstrap \
  --repo-root . \
  --target mylib \
  --header path/to/mylib.h \
  --namespace MyLib

# Parse header → IDL snapshot
abi_framework generate \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

# Generate all bindings from IDL
abi_framework codegen \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

# Lock current IDL as baseline (first time)
abi_framework generate-baseline \
  --repo-root . \
  --config abi/config.json \
  --target mylib
```

### ABI governance in CI

```bash
# Fail on regressions
abi_framework verify-all \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

# GitHub Actions annotations format
abi_framework verify-all \
  --repo-root . \
  --config abi/config.json \
  --skip-binary \
  --output-format annotations

# Generate CI workflow file (GitHub Actions or GitLab CI)
abi_framework ci-config \
  --provider github \
  --config abi/config.json \
  --output .github/workflows/abi.yml
```

### Watch mode (development)

```bash
# Re-run codegen automatically when headers or metadata change
abi_framework watch \
  --repo-root . \
  --config abi/config.json \
  --target mylib
```

### Health check

```bash
abi_framework status --repo-root . --config abi/config.json
abi_framework doctor --repo-root . --config abi/config.json
```

## Repository structure

```
abi-forge/
├── src/abi_framework_core/     # core: parse, diff, snapshot, policy, orchestration
│   └── commands/               # subcommands: generate, codegen, verify, watch, ci-config, …
├── generator_sdk/              # language generators
│   ├── plugin.manifest.json    # plugin declarations for all built-in generators
│   ├── python_bindings_generator.py
│   ├── rust_ffi_generator.py
│   ├── typescript_bindings_generator.py
│   ├── go_bindings_generator.py
│   ├── native_exports_generator.py
│   ├── native_impl_handles_generator.py
│   ├── symbol_contract_generator.py
│   ├── managed_api_metadata_generator.py
│   ├── managed_api_scaffold_generator.py
│   └── csharp/                 # Roslyn source generator (C# P/Invoke + managed API)
├── abi_codegen_core/           # shared codegen primitives
├── tests/                      # 101 tests covering orchestration, generators, governance
└── schemas/                    # JSON schemas for IDL, config, managed_api
```

## Config overview (`abi/config.json`)

```jsonc
{
  "targets": {
    "mylib": {
      "header": {
        "path": "include/mylib.h",
        "symbol_prefix": "mylib_",
        "api_macro": "MYLIB_API"
      },
      "codegen": {
        "idl_output_path": "abi/generated/mylib/mylib.idl.json"
      },
      "baseline_path": "abi/baselines/mylib.json",
      "bindings": {
        "generators": [
          { "name": "python",    "kind": "external", "plugin": "abi_framework.python_bindings" },
          { "name": "rust",      "kind": "external", "plugin": "abi_framework.rust_ffi" },
          { "name": "typescript","kind": "external", "plugin": "abi_framework.typescript_bindings" },
          { "name": "go",        "kind": "external", "plugin": "abi_framework.go_bindings" }
        ]
      }
    }
  }
}
```

## Built-in plugins

| Plugin name | Output |
|-------------|--------|
| `abi_framework.python_bindings` | Python ctypes module |
| `abi_framework.rust_ffi` | Rust FFI bindings |
| `abi_framework.typescript_bindings` | TypeScript ffi-napi bindings |
| `abi_framework.go_bindings` | Go cgo bindings |
| `abi_framework.symbol_contract` | Symbol contract lockfile |
| `abi_framework.native_exports` | C/C++ export forwarding stubs |
| `abi_framework.native_impl_handles` | C++ opaque handle struct header |
| `abi_framework.managed_api_metadata` | Normalized managed API metadata JSON |
| `abi_framework.managed_api_scaffold` | Scaffold managed_api.source.json (one-time) |
| `abi_framework.managed_bindings_scaffold` | Scaffold managed.json SafeHandle definitions (one-time) |

## Plugin authoring

```bash
# Scaffold a new generator plugin
abi_framework new-plugin \
  --name mypkg.my_generator \
  --lang python \
  --output-dir ./my_generator

# Validate the manifest
abi_framework validate-plugin-manifest \
  --repo-root . \
  --config abi/config.json

# Run the test harness (determinism, dry-run, check-mode checks)
abi_framework test-plugin \
  --manifest ./my_generator/plugin.manifest.json
```

## Incremental generator cache

Generators with `deterministic_output: true` in their manifest are cached by SHA256 of the rendered command + IDL file. Subsequent runs skip unchanged generators. Invalidated automatically when the IDL or generator arguments change.

Cache lives in `.abi-forge-cache/{target}/{generator}.gen.cache.json`.

Force regeneration: `abi_framework codegen ... --force-regen`

## ABI error types

| Rule | Meaning |
|------|---------|
| `ABI001` | Function removed from public API |
| `ABI002` | Function signature changed |
| `ABI003` | Enum value added, removed, or renumbered |
| `ABI004` | Struct layout changed |
| `ABI005` | Bindings metadata mismatch |
| `ABI006` | Version policy violation |
| `ABI007` | Warning (non-breaking) |

## License

MIT
