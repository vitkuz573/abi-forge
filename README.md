# abi_framework

`abi_framework` is a config-driven, language-agnostic ABI governance tool.

## What it does

- Extracts C ABI surface from headers (`api_macro` + `call_macro` + `symbol_prefix`).
- Captures ABI type surface from headers:
  - `typedef enum ...`
  - `typedef struct ...`
- Optionally checks binary exports (`.so` / `.dylib` / `.dll`).
- Compares current ABI against baselines and classifies changes:
  - `none`
  - `additive`
  - `breaking`
- Enforces ABI semantic-version policy from header macros.
- Generates ABI IDL JSON from the ABI header (schema v2).
- Can generate native ABI artifacts from IDL:
  - C header (`native/include/...`)
  - linker export map (`.map`)
- Runs language generator plugins from config (`bindings.generators`).
- Supports parser backends (`regex`, `clang_preprocess`) for ABI extraction.
- Supports policy rules and TTL-based waivers.
- Supports multi-target configs, changelog output, SARIF output, and release pipeline orchestration.

## Core commands

```bash
# Snapshot one target
python3 tools/abi_framework/abi_framework.py snapshot \
  --repo-root . \
  --config abi/config.json \
  --target lumenrtc \
  --skip-binary

# Verify one target against baseline
python3 tools/abi_framework/abi_framework.py verify \
  --repo-root . \
  --config abi/config.json \
  --target lumenrtc \
  --baseline abi/baselines/lumenrtc.json \
  --skip-binary

# Verify all targets
python3 tools/abi_framework/abi_framework.py verify-all \
  --repo-root . \
  --config abi/config.json \
  --skip-binary \
  --output-dir artifacts/abi

# Generate ABI IDL for one/all targets
python3 tools/abi_framework/abi_framework.py generate \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

# Run full codegen (IDL + configured generators)
python3 tools/abi_framework/abi_framework.py codegen \
  --repo-root . \
  --config abi/config.json \
  --skip-binary \
  --check

# Migrate IDL payload to schema v2
python3 tools/abi_framework/abi_framework.py idl-migrate \
  --input abi/generated/lumenrtc/lumenrtc.idl.json \
  --to-version 2

# Sync generated ABI artifacts and optionally baselines
python3 tools/abi_framework/abi_framework.py sync \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

# Benchmark ABI pipeline
python3 tools/abi_framework/abi_framework.py benchmark \
  --repo-root . \
  --config abi/config.json \
  --skip-binary \
  --iterations 3 \
  --output artifacts/abi/benchmark.report.json

# End-to-end release preparation pipeline
python3 tools/abi_framework/abi_framework.py release-prepare \
  --repo-root . \
  --config abi/config.json \
  --skip-binary \
  --release-tag v1.2.3 \
  --output-dir artifacts/abi/release
```

## Config model

```json
{
  "targets": {
    "my_target": {
      "baseline_path": "abi/baselines/my_target.json",
      "header": {
        "path": "native/include/my_api.h",
        "api_macro": "MY_API",
        "call_macro": "MY_CALL",
        "symbol_prefix": "my_",
        "version_macros": {
          "major": "MY_ABI_VERSION_MAJOR",
          "minor": "MY_ABI_VERSION_MINOR",
          "patch": "MY_ABI_VERSION_PATCH"
        },
        "parser": {
          "backend": "clang_preprocess",
          "compiler": "clang",
          "compiler_candidates": ["clang", "clang-18", "clang-17", "clang++"],
          "args": ["-D_GNU_SOURCE"],
          "include_dirs": ["native/include"],
          "fallback_to_regex": true
        }
      },
      "policy": {
        "max_allowed_classification": "breaking",
        "rules": [
          {
            "id": "no_removed_symbols",
            "severity": "error",
            "message": "Removing symbols is prohibited.",
            "when": { "removed_symbols_count_gt": 0 }
          }
        ],
        "waivers": [
          {
            "id": "temporary-known-drift",
            "severity": "warning",
            "pattern": "known non-critical warning",
            "targets": ["^my_target$"],
            "expires_utc": "2026-12-31T00:00:00Z",
            "owner": "team-abi",
            "reason": "Temporary upstream transition"
          }
        ]
      },
      "bindings": {
        "expected_symbols": ["my_init", "my_shutdown"],
        "symbol_docs": {
          "my_init": "Initializes runtime state."
        },
        "deprecated_symbols": ["my_shutdown"],
        "generators": [
          {
            "name": "stub",
            "kind": "external",
            "enabled": true,
            "command": [
              "python3",
              "tools/codegen_stub.py",
              "{idl}",
              "{repo_root}/artifacts/my_target.stub.txt"
            ]
          }
        ]
      },
      "binary": {
        "path": "native/build/libmyapi.so",
        "allow_non_prefixed_exports": false
      },
      "codegen": {
        "enabled": true,
        "idl_schema_version": 2,
        "idl_output_path": "abi/generated/my_target/my_target.idl.json",
        "native_header_output_path": "native/include/my_api.h",
        "native_export_map_output_path": "native/my_api.map",
        "native_header_guard": "MY_API_H",
        "native_api_macro": "MY_API",
        "native_call_macro": "MY_CALL",
        "native_constants": {
          "MY_CONST_LIMIT": "16"
        },
        "include_symbols_regex": ["^my_"],
        "exclude_symbols": []
      }
    }
  }
}
```

Notes:

- `bindings.expected_symbols` is optional but recommended.
- `codegen` command runs IDL generation plus configured language generators.
- If `codegen.native_header_output_path`/`codegen.native_export_map_output_path` are set,
  `generate`/`codegen`/`sync` also refresh native ABI artifacts from IDL.
- `generate` command generates IDL only.
- `header.parser.compiler_candidates` lets parser auto-pick the first available clang binary.
- Environment override `ABI_CLANG` can force a specific clang executable path.

## Wrapper scripts

- Bash: `scripts/abi.sh`
- PowerShell: `scripts/abi.ps1`

Both wrappers expose:

- `snapshot`
- `baseline`
- `baseline-all`
- `regen` / `regen-baselines`
- `doctor`
- `benchmark`
- `generate`
- `codegen`
- `idl-migrate`
- `sync`
- `release-prepare`
- `changelog`
- `verify` / `check`
- `verify-all` / `check-all`
- `list-targets`
- `init-target`
- `diff`
