# Examples

## Wiring abi-forge into your project

See [LumenRTC](https://github.com/vitkuz573/LumenRTC) for a complete production example.

The key files to look at in LumenRTC:
- `abi/config.json` — target definition, header path, generator plugins, policy
- `scripts/abi.sh` / `scripts/abi.ps1` — convenience wrappers around `abi_framework.py`
- `abi/bindings/lumenrtc.managed_api.source.json` — declarative C# managed API metadata
- `abi/generated/lumenrtc/` — generated IDL + all language binding outputs

## Minimal abi/config.json

```json
{
  "targets": {
    "mylib": {
      "header": {
        "path": "include/mylib.h",
        "symbol_prefix": "mylib_",
        "api_macro": "MYLIB_API"
      },
      "codegen": {
        "enabled": true,
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
