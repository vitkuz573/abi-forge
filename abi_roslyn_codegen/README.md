# abi_roslyn_codegen

Roslyn source generator that converts ABI metadata into C# interop code
during compilation.

## Input

- ABI IDL JSON (for example `abi/generated/lumenrtc/lumenrtc.idl.json`).
- Managed handle metadata JSON (for example `abi/bindings/lumenrtc.managed.json`).
- Managed API metadata JSON (for example `abi/bindings/lumenrtc.managed_api.json`).
- MSBuild properties passed through `CompilerVisibleProperty`:
  - `AbiIdlPath`
  - `AbiManagedMetadataPath`
  - `AbiManagedApiMetadataPath`
  - `AbiNamespace`
  - `AbiClassName`
  - `AbiAccessModifier`
  - `AbiCallingConvention`
  - `AbiLibraryExpression`

## Output

- Generated `NativeMethods` (`DllImport`) source.
- Generated `NativeTypes` source (enums/structs/delegates/constants).
- Generated `NativeHandles` source (`SafeHandle` partial methods for release/lifetime).
- Generated managed API sources from `managed_api` metadata:
  callbacks, builder extensions, handle API wrappers, async wrappers.
- All generated sources are added directly to compilation (no checked-in `*.g.cs` required).

### Managed API `output_hints`

`managed_api.output_hints` supports rich hint-name shaping:

- `pattern`: template with `{section}` and `{default}` tokens.
- `prefix`: prepended to generated section hints.
- `suffix`: appended when hint has no `.cs` extension.
- `directory`: prepended path segment.
- `sections`: per-section overrides map.
- direct per-section keys (`callbacks`, `builder`, `handle_api`, `peer_connection_async`).
- `apply_prefix_to_explicit`, `apply_directory_to_explicit`: control whether global layout applies to explicit overrides.

## Handle Contracts

For each handle entry in managed metadata, project source must declare a matching
type (`namespace` + `cs_type`) as:

- `partial class`
- inheriting `System.Runtime.InteropServices.SafeHandle`
- accessibility matching metadata (`public` or `internal`)

Violations are reported as source-generator diagnostics (`ABIGEN008`-`ABIGEN012`).

## Integration

`src/LumenRTC/LumenRTC.csproj` wires the generator as an analyzer project reference
and passes IDL + managed metadata files as `AdditionalFiles`.

Validation command:

```bash
dotnet build src/LumenRTC/LumenRTC.csproj
```
