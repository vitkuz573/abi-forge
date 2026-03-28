# abi-forge

**Reusable ABI governance and polyglot binding generator for C shared libraries.**

From a single C header, abi-forge produces a versioned IDL snapshot and generates bindings for every language you need — with built-in breakage detection and governance.

## What it does

| Stage | What happens |
|-------|-------------|
| **Parse** | Clang-preprocesses your C header into a stable IDL JSON snapshot |
| **Govern** | Diffs every new snapshot against the baseline; flags removals, signature changes, enum shifts as typed errors (ABI001–ABI007, SARIF-compatible) |
| **Generate** | Runs your chosen language generators from the IDL: C#, Python, Rust, TypeScript, Go |
| **Enforce** | CI fails on ABI regressions; waivers require explicit approval with TTL |

## Language targets

| Language | Output | Mechanism |
|----------|--------|-----------|
| **C# (.NET)** | P/Invoke + SafeHandle wrappers + async layer | Roslyn source generator (`abi_roslyn_codegen/`) |
| **Python** | ctypes module with IntEnum, OOP handles, CFUNCTYPE | `generator_sdk/python_bindings_generator.py` |
| **Rust** | `#[repr(C)]` enums + `extern "C"` block | `generator_sdk/rust_ffi_generator.py` |
| **TypeScript** | ffi-napi + OOP wrappers with `[Symbol.dispose]` | `generator_sdk/typescript_bindings_generator.py` |
| **Go** | cgo package with struct wrappers and `runtime.SetFinalizer` | `generator_sdk/go_bindings_generator.py` |

## Real-world example

[LumenRTC](https://github.com/vitkuz573/LumenRTC) — a .NET wrapper over `libwebrtc` — uses abi-forge to govern its C ABI and generate all language bindings from a single IDL snapshot. See `abi/config.json` there for a complete production configuration.

## Quick start

### Requirements

- Python 3.10+
- clang (for header parsing)
- .NET SDK 10+ (optional, only for C# bindings)

### Bootstrap a new target

```bash
# Add abi-forge as a submodule
git submodule add https://github.com/vitkuz573/abi-forge.git tools/abi_framework

# Bootstrap: scaffolds abi/config.json + initial metadata
python3 tools/abi_framework/abi_framework.py bootstrap \
  --target mylib \
  --header path/to/mylib.h \
  --namespace MyLib \
  --generate-python \
  --generate-rust

# Parse header → IDL snapshot
python3 tools/abi_framework/abi_framework.py generate \
  --config abi/config.json --skip-binary

# Generate all bindings from IDL
python3 tools/abi_framework/abi_framework.py codegen \
  --config abi/config.json --skip-binary

# Lock current IDL as baseline (first time)
python3 tools/abi_framework/abi_framework.py generate-baseline --target mylib
```

### ABI governance in CI

```bash
# Fail on regressions
python3 tools/abi_framework/abi_framework.py check \
  --config abi/config.json --skip-binary

# Full SARIF report for GitHub code scanning
python3 tools/abi_framework/abi_framework.py check-all \
  --config abi/config.json --skip-binary --sarif-out abi-report.sarif
```

### Health dashboard

```bash
python3 tools/abi_framework/abi_framework.py status --config abi/config.json
```

## Repository structure

```
abi-forge/
├── abi_framework.py            # CLI entry point
├── src/abi_framework_core/     # core: parse, diff, snapshot, policy, orchestration
│   └── commands/               # subcommands: generate, codegen, check, release, status, …
├── generator_sdk/              # language generators (Python, Rust, TypeScript, Go, C#-scaffold)
│   └── plugin.manifest.json    # plugin declarations for all built-in generators
├── abi_codegen_core/           # shared codegen primitives (native exports, required functions)
├── abi_roslyn_codegen/         # Roslyn source generator for C# P/Invoke + managed API
├── tests/                      # 99 tests covering orchestration, generators, governance
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
        "idl_output": "abi/generated/mylib/mylib.idl.json"
      },
      "baseline_path": "abi/baselines/mylib.json",
      "bindings": {
        "generators": [
          {
            "manifest": "{repo_root}/tools/abi_framework/generator_sdk/plugin.manifest.json",
            "plugins": [
              "abi_framework.python_bindings",
              "abi_framework.rust_ffi",
              "abi_framework.typescript_bindings",
              "abi_framework.go_bindings"
            ]
          }
        ]
      }
    }
  }
}
```

## ABI error types (SARIF rules)

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
