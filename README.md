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
- Generates ABI IDL JSON from the ABI header.
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

# Sync generated ABI artifacts and optionally baselines
python3 tools/abi_framework/abi_framework.py sync \
  --repo-root . \
  --config abi/config.json \
  --skip-binary

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
        }
      },
      "bindings": {
        "expected_symbols": ["my_init", "my_shutdown"]
      },
      "binary": {
        "path": "native/build/libmyapi.so",
        "allow_non_prefixed_exports": false
      },
      "codegen": {
        "enabled": true,
        "idl_output_path": "abi/generated/my_target/my_target.idl.json",
        "include_symbols_regex": ["^my_"],
        "exclude_symbols": []
      }
    }
  }
}
```

Notes:

- `bindings.expected_symbols` is optional but recommended.
- `codegen` in `abi_framework` is only about ABI IDL generation.
- Language-specific code generation (for example C#) should be done by separate tools that consume the generated IDL.

## Wrapper scripts

- Bash: `scripts/abi.sh`
- PowerShell: `scripts/abi.ps1`

Both wrappers expose:

- `snapshot`
- `baseline`
- `baseline-all`
- `regen` / `regen-baselines`
- `doctor`
- `generate`
- `sync`
- `release-prepare`
- `changelog`
- `verify` / `check`
- `verify-all` / `check-all`
- `list-targets`
- `init-target`
- `diff`
