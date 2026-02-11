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
  - `AbiConstantsClassName`
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

- `pattern`: template tokens include:
  `{section}`, `{section_pascal}`, `{section_snake}`, `{section_kebab}`, `{section_path}`,
  `{default}`, `{default_stem}`, `{default_name}`,
  `{namespace}`, `{namespace_path}`.
- `suffix`: appended when hint has no `.cs` extension.
- `sections`: per-section overrides map.
- Built-in managed API emissions are class-driven: section keys come from class names, not fixed labels.
- Canonical keys only: `pattern`, `suffix`, `sections`.

### Interop Binding Overrides

The generator consumes `bindings.interop` metadata embedded in IDL:

- `struct_layout_overrides`: per-struct `StructLayout` overrides (`pack`, optional `layout`).
- `callback_typedef_call_tokens`: optional allowed calling-convention tokens for callback typedef parsing.
- `callback_struct_suffixes`: optional callback-struct suffix list (default: `["_callbacks_t"]`).
- `functions.<name>.parameters.<param>`:
  - `managed_type`
  - `modifier` (`ref` / `out` / `in` / `none`)
  - `marshal_as_i1` or `marshal_as: "i1"`
- `output_hints` for interop source hint naming (`abi`, `types`, `handles`):
  - tokens: `{section}`, `{section_pascal}`, `{section_snake}`, `{section_kebab}`, `{section_path}`,
    `{class}`, `{class_path}`, `{namespace}`, `{namespace_path}`, `{target}`,
    `{default}`, `{default_stem}`, `{default_name}`
  - canonical keys: `pattern`, `suffix`, `sections`

### Managed API Optional Sections

`managed_api` supports optional built-in sections:

- `callbacks`
- `builder`
- `handle_api`
- `peer_connection_async`

and optional extensibility sections:

- `custom_sections[]` entries with:
  - `name` (output-hint section key)
  - `class`
  - `methods`
  - optional `default_hint`

## Handle Contracts

For each handle entry in managed metadata, project source may declare a matching
type (`namespace` + `cs_type`) as:

- `partial class`
- inheriting `System.Runtime.InteropServices.SafeHandle`
- accessibility matching metadata (`public` or `internal`)

If a handle type is missing in project source, generator emits a fallback `SafeHandle`
class from metadata and reports a warning (`ABIGEN008`) instead of failing generation.

Violations are reported as source-generator diagnostics (`ABIGEN008`-`ABIGEN012`).

## Integration

`src/LumenRTC/LumenRTC.csproj` wires the generator as an analyzer project reference
and passes IDL + managed metadata files as `AdditionalFiles`.

Validation command:

```bash
dotnet build src/LumenRTC/LumenRTC.csproj
```
