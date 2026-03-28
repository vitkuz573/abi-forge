# abi_codegen_core

Reusable code generation primitives for ABI-driven pipelines.

This package contains target-agnostic helpers that can be shared by
project-specific generator plugins:

- JSON/IDL loading and deterministic write/check diff behavior.
- Native export/forwarder rendering from ABI IDL `functions`.
- Required-native-function discovery from metadata text patterns.

Generator plugins that need these helpers can consume this package directly.
