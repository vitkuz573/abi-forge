# Generator SDK (External)

`abi_framework` runs language generators through `bindings.generators` entries with `kind: external`.

## Contract

- Input: ABI IDL path via `{idl}` placeholder in command arguments.
- Environment:
  - `repo_root` placeholder resolves to repository root.
  - `target` placeholder resolves to current target name.
- Exit code:
  - `0`: success
  - non-zero: generator failure (pipeline fails in `codegen --check`)

## Minimal config

```json
{
  "bindings": {
    "generators": [
      {
        "name": "my-generator",
        "kind": "external",
        "enabled": true,
        "command": [
          "python3",
          "tools/abi_framework/generator_sdk/external_generator_stub.py",
          "{idl}",
          "{repo_root}/artifacts/codegen/{target}.stub.txt"
        ]
      }
    ]
  }
}
```

Use `external_generator_stub.py` as a starting point for custom generators.
