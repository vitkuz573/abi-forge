# ABI Framework Architecture

## Goals

- Keep ABI governance logic deterministic and testable.
- Separate pure ABI engine logic from CLI orchestration.
- Keep architecture clean; avoid backward-compatibility shims in runtime code.

## Layout

```text
tools/abi_framework/
  abi_framework.py                    # CLI entrypoint
  src/abi_framework_core/
    __init__.py                       # package export surface
    core.py                           # aggregated domain API (re-export of split core modules)
    _core_base.py                     # schema/config validation + header parsing foundations
    _core_codegen.py                  # idl/native artifact generation + generator execution
    _core_plugins.py                  # plugin manifest validation + command/manifest binding helpers
    _core_snapshot.py                 # binary export extraction + snapshot assembly
    _core_compare.py                  # diff/classification + markdown/sarif/html rendering
    _core_policy.py                   # policy rules/waivers + policy application
    _core_orchestration.py            # cross-domain orchestration helpers
    commands/
      __init__.py                     # command export surface
      common.py                       # shared command helpers (target/baseline/binary resolution)
      generation.py                   # generate/codegen/sync
      verification.py                 # snapshot/verify/diff/verify-all/regen-baselines
      governance.py                   # waiver-audit/doctor/changelog
      performance.py                  # benchmark/benchmark-gate
      release.py                      # release-prepare + sbom/attestation emit
      targets.py                      # list-targets/init-target
    cli.py                            # argparse wiring and process exit behavior
  schemas/                            # JSON schemas (config/snapshot/report/idl)
  tests/                              # unit/integration tests for stable behavior
```

## Layer Responsibilities

- `core.py` + `_core_*`
  - Own ABI domain behavior: snapshots, diffing, policy evaluation, IDL generation, artifact renderers.
  - `core.py` is intentionally thin and re-exports split modules.
- `commands/*`
  - Orchestrates multi-target flows and report outputs, split by responsibility.
  - Uses `core.py` primitives as the only business dependency.
- `cli.py`
  - Defines command-line interface contracts and command routing.
  - Converts domain errors to stable process exit codes.
- `abi_framework.py`
  - CLI executable entrypoint (`tools/abi_framework/abi_framework.py`).

## Public Contract

- CLI commands and flags are the supported interface.
- Python imports should target `abi_framework_core` modules directly.
- No compatibility shims for legacy module import paths.

## Extension Rules

- Add domain logic to the relevant `_core_*` module.
- Keep `core.py` as thin re-export glue.
- Add workflow orchestration to the relevant `commands/*` module.
- Add or change CLI surface only in `cli.py`.
- Keep `abi_framework.py` thin; avoid adding business logic there.
