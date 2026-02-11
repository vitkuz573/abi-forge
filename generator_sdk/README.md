# Generator SDK (External)

`abi_framework` runs language generators through `bindings.generators` entries with `kind: external`.

Plugin metadata for SDK generators can be declared in a manifest and validated via:

```bash
python3 tools/abi_framework/abi_framework.py validate-plugin-manifest \
  --manifest tools/abi_framework/generator_sdk/plugin.manifest.json

# or auto-discovered from abi/config.json external generators
python3 tools/abi_framework/abi_framework.py validate-plugin-manifest \
  --repo-root . \
  --config abi/config.json
```

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
        "manifest": "tools/my_codegen/plugin.manifest.json",
        "plugin": "my_codegen.stub"
      }
    ]
  }
}
```

You can still provide `command` directly. When `manifest`/`plugin` and `command`
are both set, `abi_framework` enforces that they are identical.

Use `external_generator_stub.py` as a starting point for custom generators.

## Built-in Utility: Symbol Contract Generator

`symbol_contract_generator.py` is a reusable external generator that builds a bindings symbol
contract lockfile from declarative sources.

### Inputs

- `--idl`: ABI IDL JSON path.
- `--spec`: source specification JSON path.
- `--out`: output lockfile path.
- `--repo-root`: repo root for `{repo_root}` path token (default: current directory).
- `--mode`: optional override (`strict` or `required_only`).

### Spec format (`schema_version: 1`)

```json
{
  "schema_version": 1,
  "target": "my_target",
  "mode": "strict",
  "require_full_coverage": true,
  "sources": [
    {
      "kind": "json_array",
      "path": "{repo_root}/abi/bindings/my_target.managed_api.json",
      "pointer": "/required_native_functions"
    },
    {
      "kind": "json_object_fields",
      "path": "{repo_root}/abi/bindings/my_target.managed.json",
      "pointer": "/handles",
      "fields": ["release", "retain"]
    },
    {
      "kind": "regex_scan",
      "root": "{repo_root}/src/MyTarget",
      "include": ["**/*.cs"],
      "exclude": ["**/obj/**", "**/bin/**"],
      "pattern": "NativeMethods\\.(my_[A-Za-z0-9_]+)\\b",
      "group": 1
    },
    {
      "kind": "symbols",
      "symbols": ["my_init", "my_shutdown"]
    }
  ]
}
```

Supported source kinds:

- `symbols`: inline string array.
- `json_array`: array of strings from JSON pointer.
- `json_object_fields`: extract string fields from array of objects.
- `regex_scan`: regex discovery over files under a root directory.

Optional spec key:

- `require_full_coverage` (boolean): when `true` and mode is `strict`,
  generator fails if any IDL function is missing from discovered symbols.

### Output lockfile

```json
{
  "schema_version": 1,
  "target": "my_target",
  "mode": "strict",
  "generator": "tools/abi_framework/generator_sdk/symbol_contract_generator.py",
  "spec_sha256": "<digest>",
  "symbol_count": 2,
  "symbols": ["my_init", "my_shutdown"]
}
```
