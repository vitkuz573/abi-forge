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
    __init__.py                       # public API surface for wrapper imports
    core.py                           # domain engine (parse/compare/policy/codegen primitives)
    commands.py                       # command orchestration over core services
    cli.py                            # argparse wiring and process exit behavior
  schemas/                            # JSON schemas (config/snapshot/report/idl)
  tests/                              # unit/integration tests for stable behavior
```

## Layer Responsibilities

- `core.py`
  - Owns ABI domain behavior: snapshots, diffing, policy evaluation, IDL generation, artifact renderers.
  - Contains no argument-parser wiring.
- `commands.py`
  - Orchestrates multi-target flows and report outputs (`generate`, `verify-all`, `sync`, `release-prepare`, etc.).
  - Uses `core.py` primitives as the only business dependency.
- `cli.py`
  - Defines command-line interface contracts and command routing.
  - Converts domain errors to stable process exit codes.
- `abi_framework.py`
  - Preserves old import and executable path (`tools/abi_framework/abi_framework.py`).
  - Re-exports public API expected by scripts/tests.

## Public Contract

- CLI commands and flags are the supported interface.
- Python imports should target `abi_framework_core` modules directly.
- No compatibility shims for legacy module import paths.

## Extension Rules

- Add domain logic to `core.py`.
- Add workflow orchestration to `commands.py`.
- Add or change CLI surface only in `cli.py`.
- Keep wrapper thin; avoid adding business logic there.
