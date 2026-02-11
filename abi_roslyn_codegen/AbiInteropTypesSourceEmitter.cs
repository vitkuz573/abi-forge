using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace Abi.RoslynGenerator;

internal static class AbiInteropTypesSourceEmitter
{
    private static readonly Dictionary<string, string> PrimitiveTypeMap = new(StringComparer.Ordinal)
    {
        ["void"] = "void",
        ["bool"] = "bool",
        ["char"] = "byte",
        ["signed char"] = "sbyte",
        ["unsigned char"] = "byte",
        ["short"] = "short",
        ["unsigned short"] = "ushort",
        ["int"] = "int",
        ["unsigned int"] = "uint",
        ["long"] = "nint",
        ["unsigned long"] = "nuint",
        ["long long"] = "long",
        ["unsigned long long"] = "ulong",
        ["int8_t"] = "sbyte",
        ["uint8_t"] = "byte",
        ["int16_t"] = "short",
        ["uint16_t"] = "ushort",
        ["int32_t"] = "int",
        ["uint32_t"] = "uint",
        ["int64_t"] = "long",
        ["uint64_t"] = "ulong",
        ["size_t"] = "nuint",
        ["ssize_t"] = "nint",
        ["float"] = "float",
        ["double"] = "double",
    };

    private static readonly HashSet<string> CSharpKeywords = new(StringComparer.Ordinal)
    {
        "abstract", "as", "base", "bool", "break", "byte", "case", "catch", "char", "checked", "class",
        "const", "continue", "decimal", "default", "delegate", "do", "double", "else", "enum", "event",
        "explicit", "extern", "false", "finally", "fixed", "float", "for", "foreach", "goto", "if",
        "implicit", "in", "int", "interface", "internal", "is", "lock", "long", "namespace", "new",
        "null", "object", "operator", "out", "override", "params", "private", "protected", "public",
        "readonly", "ref", "return", "sbyte", "sealed", "short", "sizeof", "stackalloc", "static", "string",
        "struct", "switch", "this", "throw", "true", "try", "typeof", "uint", "ulong", "unchecked",
        "unsafe", "ushort", "using", "virtual", "void", "volatile", "while",
    };

    private static readonly Regex CallbackTypedefRegex = new(
        "^typedef\\s+(?<ret>.+?)\\s*\\(\\s*(?:(?<call>[A-Za-z_][A-Za-z0-9_]*)\\s+)?\\*\\s*(?<name>[A-Za-z_][A-Za-z0-9_]*)\\s*\\)\\s*\\((?<params>.*)\\)\\s*;?\\s*$",
        RegexOptions.Compiled
    );

    private static readonly Regex FunctionPointerFieldRegex = new(
        "^(?<ret>.+?)\\(\\s*\\*\\s*(?<name>[A-Za-z_][A-Za-z0-9_]*)\\s*\\)\\s*\\((?<params>.*)\\)$",
        RegexOptions.Compiled
    );

    public static IdlTypeModel ParseIdl(string text)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(text);
        }
        catch (JsonException ex)
        {
            throw new GeneratorException($"IDL JSON is invalid: {ex.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException("IDL root must be an object.");
            }

            var callbackTypedefCallTokens = ParseInteropStringSet(root, "callback_typedef_call_tokens");
            var callbackStructSuffixes = ParseInteropStringList(root, "callback_struct_suffixes");
            if (callbackStructSuffixes.Count == 0)
            {
                callbackStructSuffixes.Add("_callbacks_t");
            }

            var enums = ParseEnums(root);
            var structs = ParseStructs(root);
            var delegates = ParseCallbackTypedefs(root, callbackTypedefCallTokens);
            var constants = ParseConstants(root);
            var callbackFieldOverrides = ParseBindingsOverrides(root, "callback_field_overrides");
            var structFieldOverrides = ParseBindingsOverrides(root, "struct_field_overrides");
            var structLayoutOverrides = ParseStructLayoutOverrides(root);
            var functionNames = ParseFunctionNames(root);
            var functionFirstParams = ParseFunctionFirstParameterTypes(root);

            return new IdlTypeModel(
                enums,
                structs,
                delegates,
                constants,
                callbackFieldOverrides,
                structFieldOverrides,
                structLayoutOverrides,
                functionNames,
                functionFirstParams,
                callbackTypedefCallTokens,
                callbackStructSuffixes);
        }
    }

    public static ManagedHandlesModel ParseManagedMetadata(string text)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(text);
        }
        catch (JsonException ex)
        {
            throw new GeneratorException($"Managed metadata JSON is invalid: {ex.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException("Managed metadata root must be an object.");
            }

            if (!root.TryGetProperty("handles", out var handlesObj) || handlesObj.ValueKind != JsonValueKind.Array)
            {
                throw new GeneratorException("Managed metadata must contain array 'handles'.");
            }

            var handles = new List<ManagedHandleSpec>();
            foreach (var item in handlesObj.EnumerateArray())
            {
                if (item.ValueKind != JsonValueKind.Object)
                {
                    continue;
                }

                var @namespace = ReadRequiredString(item, "namespace", "managed handle entry");
                var csType = ReadRequiredString(item, "cs_type", "managed handle entry");
                var release = ReadRequiredString(item, "release", "managed handle entry");
                var access = ReadOptionalString(item, "access", "public").ToLowerInvariant();
                var cHandleType = ReadOptionalString(item, "c_handle_type", string.Empty);

                if (!string.Equals(access, "public", StringComparison.Ordinal) &&
                    !string.Equals(access, "internal", StringComparison.Ordinal))
                {
                    throw new GeneratorException($"Managed handle '{csType}' has unsupported access '{access}'.");
                }

                handles.Add(new ManagedHandleSpec(@namespace, csType, release, access, cHandleType));
            }

            return new ManagedHandlesModel(handles);
        }
    }

    public static string RenderTypesCode(IdlTypeModel model, GeneratorOptions options)
    {
        var delegates = new Dictionary<string, DelegateSpec>(model.CallbackTypedefs, StringComparer.Ordinal);
        var callbackFieldTypes = new Dictionary<string, string>(StringComparer.Ordinal);

        var signatureToDelegate = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var kv in delegates)
        {
            signatureToDelegate[BuildDelegateSignatureKey(kv.Value)] = kv.Key;
        }

        var generatedDelegatePrefix = DetermineGeneratedDelegatePrefix(delegates.Keys);

        foreach (var kv in model.Structs.Where(item => IsCallbackStructName(item.Key, model)))
        {
            foreach (var field in kv.Value.Fields)
            {
                var functionPointer = ParseFunctionPointerField(field.Declaration);
                if (functionPointer == null)
                {
                    continue;
                }

                var overrideName = model.CallbackFieldOverrides.TryGetValue(field.Name, out var overrideValue)
                    ? overrideValue ?? string.Empty
                    : string.Empty;
                var signature = BuildDelegateSignatureKey(functionPointer);
                var delegateName = overrideName;
                if (string.IsNullOrWhiteSpace(delegateName) && signatureToDelegate.TryGetValue(signature, out var existing))
                {
                    delegateName = existing;
                }
                if (string.IsNullOrWhiteSpace(delegateName))
                {
                    var generated = ToManagedTypeName(field.Name, stripTypedefSuffix: false) + "Cb";
                    delegateName = !string.IsNullOrWhiteSpace(generatedDelegatePrefix) &&
                        !generated.StartsWith(generatedDelegatePrefix, StringComparison.Ordinal)
                        ? generatedDelegatePrefix + generated
                        : generated;
                }

                callbackFieldTypes[field.Name] = delegateName;
                if (!delegates.ContainsKey(delegateName))
                {
                    delegates[delegateName] = functionPointer;
                }
            }
        }

        var builder = new StringBuilder();
        builder.AppendLine("// <auto-generated />");
        builder.AppendLine($"// Generated by abi_roslyn_codegen source generator {AbiInteropSourceEmitter.ToolVersion}");
        builder.AppendLine("#nullable enable");
        builder.AppendLine("using System;");
        builder.AppendLine("using System.Runtime.InteropServices;");
        builder.AppendLine();
        builder.AppendLine($"namespace {options.NamespaceName};");
        builder.AppendLine();

        foreach (var enumEntry in model.Enums.OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            RenderEnum(builder, enumEntry.Key, enumEntry.Value);
        }

        if (model.Constants.Count > 0)
        {
            var constantsClassName = SanitizeIdentifier(options.ConstantsClassName, "AbiConstants");
            var constantPrefix = DetermineCommonConstantPrefix(model.Constants.Keys, model.FunctionNames);
            builder.AppendLine($"internal static class {constantsClassName}");
            builder.AppendLine("{");
            foreach (var constant in model.Constants.OrderBy(item => item.Key, StringComparer.Ordinal))
            {
                var managedName = ToManagedConstantName(constant.Key, constantPrefix);
                builder.AppendLine($"    public const int {managedName} = {constant.Value};");
            }
            builder.AppendLine("}");
            builder.AppendLine();
        }

        foreach (var structEntry in model.Structs
            .Where(item => !IsCallbackStructName(item.Key, model))
            .OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            RenderStruct(builder, model, structEntry.Key, structEntry.Value, callbackFieldTypes);
        }

        foreach (var delegateEntry in delegates.OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            RenderDelegate(builder, model, delegateEntry.Key, delegateEntry.Value);
        }

        foreach (var structEntry in model.Structs
            .Where(item => IsCallbackStructName(item.Key, model))
            .OrderBy(item => item.Key, StringComparer.Ordinal))
        {
            RenderStruct(builder, model, structEntry.Key, structEntry.Value, callbackFieldTypes);
        }

        return builder.ToString();
    }

    public static string RenderHandlesCode(IdlTypeModel model, ManagedHandlesModel handlesModel, GeneratorOptions options)
    {
        var sortedHandles = handlesModel.Handles
            .OrderBy(item => item.NamespaceName, StringComparer.Ordinal)
            .ThenBy(item => item.CsType, StringComparer.Ordinal)
            .ToArray();

        var builder = new StringBuilder();
        builder.AppendLine("// <auto-generated />");
        builder.AppendLine($"// Generated by abi_roslyn_codegen source generator {AbiInteropSourceEmitter.ToolVersion}");
        builder.AppendLine("#nullable enable");
        builder.AppendLine("using System;");
        builder.AppendLine();

        string? currentNamespace = null;
        foreach (var handle in sortedHandles)
        {
            if (!model.FunctionNames.Contains(handle.ReleaseMethod))
            {
                throw new GeneratorException(
                    $"Managed handle '{handle.CsType}' references unknown release method '{handle.ReleaseMethod}'.");
            }

            if (!string.IsNullOrWhiteSpace(handle.CHandleType) &&
                model.FunctionFirstParameterTypes.TryGetValue(handle.ReleaseMethod, out var firstParamType) &&
                !string.Equals(firstParamType, handle.CHandleType, StringComparison.Ordinal))
            {
                throw new GeneratorException(
                    $"Managed handle '{handle.CsType}' expects '{handle.CHandleType}' but '{handle.ReleaseMethod}' takes '{firstParamType}'.");
            }

            if (!string.Equals(currentNamespace, handle.NamespaceName, StringComparison.Ordinal))
            {
                if (currentNamespace != null)
                {
                    builder.AppendLine();
                }
                builder.AppendLine($"namespace {handle.NamespaceName};");
                builder.AppendLine();
                currentNamespace = handle.NamespaceName;
            }

            var access = string.Equals(handle.Access, "internal", StringComparison.Ordinal)
                ? "internal"
                : "public";

            builder.AppendLine($"{access} sealed partial class {handle.CsType}");
            builder.AppendLine("{");
            builder.AppendLine("    public override bool IsInvalid => handle == IntPtr.Zero;");
            builder.AppendLine();
            builder.AppendLine("    protected override bool ReleaseHandle()");
            builder.AppendLine("    {");
            builder.AppendLine($"        global::{options.NamespaceName}.{options.ClassName}.{handle.ReleaseMethod}(handle);");
            builder.AppendLine("        return true;");
            builder.AppendLine("    }");
            builder.AppendLine("}");
            builder.AppendLine();
        }

        return builder.ToString();
    }

    private static Dictionary<string, EnumSpec> ParseEnums(JsonElement root)
    {
        var result = new Dictionary<string, EnumSpec>(StringComparer.Ordinal);
        if (!TryGetHeaderObject(root, "enums", out var enumsObj))
        {
            return result;
        }

        foreach (var property in enumsObj.EnumerateObject())
        {
            if (property.Value.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var members = new List<EnumMemberSpec>();
            if (property.Value.TryGetProperty("members", out var membersObj) && membersObj.ValueKind == JsonValueKind.Array)
            {
                foreach (var memberObj in membersObj.EnumerateArray())
                {
                    if (memberObj.ValueKind != JsonValueKind.Object)
                    {
                        continue;
                    }

                    var name = ReadRequiredString(memberObj, "name", $"enum '{property.Name}' member");
                    int? value = null;
                    if (memberObj.TryGetProperty("value", out var valueObj) && valueObj.ValueKind == JsonValueKind.Number)
                    {
                        if (valueObj.TryGetInt32(out var intValue))
                        {
                            value = intValue;
                        }
                    }
                    members.Add(new EnumMemberSpec(name, value));
                }
            }

            result[property.Name] = new EnumSpec(members);
        }

        return result;
    }

    private static Dictionary<string, StructSpec> ParseStructs(JsonElement root)
    {
        var result = new Dictionary<string, StructSpec>(StringComparer.Ordinal);
        if (!TryGetHeaderObject(root, "structs", out var structsObj))
        {
            return result;
        }

        foreach (var property in structsObj.EnumerateObject())
        {
            if (property.Value.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var fields = new List<StructFieldSpec>();
            if (property.Value.TryGetProperty("fields", out var fieldsObj) && fieldsObj.ValueKind == JsonValueKind.Array)
            {
                foreach (var fieldObj in fieldsObj.EnumerateArray())
                {
                    if (fieldObj.ValueKind != JsonValueKind.Object)
                    {
                        continue;
                    }

                    var name = ReadRequiredString(fieldObj, "name", $"struct '{property.Name}' field");
                    var declaration = ReadRequiredString(fieldObj, "declaration", $"struct '{property.Name}' field");
                    fields.Add(new StructFieldSpec(name, declaration));
                }
            }

            result[property.Name] = new StructSpec(fields);
        }

        return result;
    }

    private static Dictionary<string, DelegateSpec> ParseCallbackTypedefs(
        JsonElement root,
        HashSet<string> allowedCallTokens)
    {
        var result = new Dictionary<string, DelegateSpec>(StringComparer.Ordinal);

        if (!TryGetHeaderArray(root, "callback_typedefs", out var callbacksObj))
        {
            return result;
        }

        foreach (var item in callbacksObj.EnumerateArray())
        {
            string? declaration = null;
            if (item.ValueKind == JsonValueKind.String)
            {
                declaration = item.GetString();
            }
            else if (item.ValueKind == JsonValueKind.Object &&
                     item.TryGetProperty("declaration", out var declarationObj) &&
                     declarationObj.ValueKind == JsonValueKind.String)
            {
                declaration = declarationObj.GetString();
            }

            if (string.IsNullOrWhiteSpace(declaration))
            {
                continue;
            }

            var parsed = ParseCallbackTypedefDeclaration(declaration!, allowedCallTokens);
            if (parsed != null)
            {
                result[parsed.Name] = parsed;
            }
        }

        return result;
    }

    private static Dictionary<string, string> ParseConstants(JsonElement root)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        if (!TryGetHeaderObject(root, "constants", out var constantsObj))
        {
            return result;
        }

        foreach (var property in constantsObj.EnumerateObject())
        {
            switch (property.Value.ValueKind)
            {
                case JsonValueKind.String:
                    result[property.Name] = property.Value.GetString() ?? string.Empty;
                    break;
                case JsonValueKind.Number:
                    result[property.Name] = property.Value.GetRawText();
                    break;
            }
        }

        return result;
    }

    private static HashSet<string> ParseInteropStringSet(JsonElement root, string key)
    {
        var result = new HashSet<string>(StringComparer.Ordinal);
        var values = ParseInteropStringList(root, key);
        foreach (var value in values)
        {
            result.Add(value);
        }
        return result;
    }

    private static List<string> ParseInteropStringList(JsonElement root, string key)
    {
        var result = new List<string>();
        if (!TryGetInteropObject(root, out var interopObj))
        {
            return result;
        }
        if (!interopObj.TryGetProperty(key, out var token) || token.ValueKind != JsonValueKind.Array)
        {
            return result;
        }

        foreach (var item in token.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String)
            {
                continue;
            }
            var value = item.GetString();
            if (!string.IsNullOrWhiteSpace(value))
            {
                result.Add(value!.Trim());
            }
        }
        return result;
    }

    private static bool TryGetInteropObject(JsonElement root, out JsonElement interopObj)
    {
        interopObj = default;
        if (!root.TryGetProperty("bindings", out var bindingsObj) || bindingsObj.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        if (!bindingsObj.TryGetProperty("interop", out interopObj) || interopObj.ValueKind != JsonValueKind.Object)
        {
            interopObj = default;
            return false;
        }
        return true;
    }

    private static Dictionary<string, string> ParseBindingsOverrides(JsonElement root, string key)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);

        if (!TryGetInteropObject(root, out var interopObj))
        {
            return result;
        }
        if (!interopObj.TryGetProperty(key, out var overridesObj) || overridesObj.ValueKind != JsonValueKind.Object)
        {
            return result;
        }

        foreach (var property in overridesObj.EnumerateObject())
        {
            if (property.Value.ValueKind == JsonValueKind.String)
            {
                var value = property.Value.GetString();
                if (!string.IsNullOrWhiteSpace(value))
                {
                    result[property.Name] = value!;
                }
            }
        }

        return result;
    }

    private static Dictionary<string, StructLayoutOverrideSpec> ParseStructLayoutOverrides(JsonElement root)
    {
        var result = new Dictionary<string, StructLayoutOverrideSpec>(StringComparer.Ordinal);

        if (!TryGetInteropObject(root, out var interopObj))
        {
            return result;
        }
        if (!interopObj.TryGetProperty("struct_layout_overrides", out var overridesObj) ||
            overridesObj.ValueKind != JsonValueKind.Object)
        {
            return result;
        }

        foreach (var property in overridesObj.EnumerateObject())
        {
            if (property.Value.ValueKind == JsonValueKind.Number)
            {
                if (property.Value.TryGetInt32(out var packValue) && packValue > 0)
                {
                    result[property.Name] = new StructLayoutOverrideSpec("Sequential", packValue);
                }
                continue;
            }

            if (property.Value.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var layout = ReadOptionalString(property.Value, "layout", "Sequential");
            var pack = ReadOptionalPositiveInt(property.Value, "pack");
            var normalizedLayout = NormalizeLayoutKind(layout);
            result[property.Name] = new StructLayoutOverrideSpec(normalizedLayout, pack);
        }

        return result;
    }

    private static HashSet<string> ParseFunctionNames(JsonElement root)
    {
        var result = new HashSet<string>(StringComparer.Ordinal);
        if (!root.TryGetProperty("functions", out var functionsObj) || functionsObj.ValueKind != JsonValueKind.Array)
        {
            return result;
        }

        foreach (var functionObj in functionsObj.EnumerateArray())
        {
            if (functionObj.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            if (functionObj.TryGetProperty("name", out var nameObj) && nameObj.ValueKind == JsonValueKind.String)
            {
                var value = nameObj.GetString();
                if (!string.IsNullOrWhiteSpace(value))
                {
                    result.Add(value!);
                }
            }
        }

        return result;
    }

    private static Dictionary<string, string> ParseFunctionFirstParameterTypes(JsonElement root)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        if (!root.TryGetProperty("functions", out var functionsObj) || functionsObj.ValueKind != JsonValueKind.Array)
        {
            return result;
        }

        foreach (var functionObj in functionsObj.EnumerateArray())
        {
            if (functionObj.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var name = ReadOptionalString(functionObj, "name", string.Empty);
            if (string.IsNullOrWhiteSpace(name))
            {
                continue;
            }

            if (!functionObj.TryGetProperty("parameters", out var paramsObj) || paramsObj.ValueKind != JsonValueKind.Array)
            {
                continue;
            }

            var firstParam = paramsObj.EnumerateArray().FirstOrDefault();
            if (firstParam.ValueKind != JsonValueKind.Object)
            {
                continue;
            }

            var cType = ReadOptionalString(firstParam, "c_type", string.Empty);
            if (!string.IsNullOrWhiteSpace(cType))
            {
                result[name] = NormalizeCType(cType);
            }
        }

        return result;
    }

    private static string DetermineGeneratedDelegatePrefix(IEnumerable<string> delegateNames)
    {
        var names = delegateNames
            .Where(name => !string.IsNullOrWhiteSpace(name))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (names.Length < 2)
        {
            return string.Empty;
        }

        var prefix = names[0];
        for (var idx = 1; idx < names.Length && prefix.Length > 0; idx++)
        {
            var candidate = names[idx];
            var max = Math.Min(prefix.Length, candidate.Length);
            var pos = 0;
            while (pos < max && prefix[pos] == candidate[pos])
            {
                pos++;
            }

            prefix = prefix.Substring(0, pos);
        }

        return prefix.Length >= 2 ? prefix : string.Empty;
    }

    private static string DetermineCommonConstantPrefix(
        IEnumerable<string> names,
        IEnumerable<string> functionNames)
    {
        var constants = names
            .Where(name => !string.IsNullOrWhiteSpace(name))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (constants.Length < 2)
        {
            var inferredPrefix = DetermineCommonIdentifierPrefix(functionNames);
            if (string.IsNullOrWhiteSpace(inferredPrefix))
            {
                return string.Empty;
            }

            var candidate = inferredPrefix.ToUpperInvariant();
            if (!candidate.EndsWith("_", StringComparison.Ordinal))
            {
                candidate += "_";
            }

            return constants.All(name => name.StartsWith(candidate, StringComparison.Ordinal))
                ? candidate
                : string.Empty;
        }

        var prefix = constants[0];
        for (var idx = 1; idx < constants.Length && prefix.Length > 0; idx++)
        {
            var candidate = constants[idx];
            var max = Math.Min(prefix.Length, candidate.Length);
            var pos = 0;
            while (pos < max && prefix[pos] == candidate[pos])
            {
                pos++;
            }

            prefix = prefix.Substring(0, pos);
        }

        var marker = prefix.LastIndexOf('_');
        if (marker < 0)
        {
            return string.Empty;
        }

        return prefix.Substring(0, marker + 1);
    }

    private static string DetermineCommonIdentifierPrefix(IEnumerable<string> names)
    {
        var symbols = names
            .Where(name => !string.IsNullOrWhiteSpace(name))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (symbols.Length == 0)
        {
            return string.Empty;
        }

        var prefix = symbols[0];
        for (var idx = 1; idx < symbols.Length && prefix.Length > 0; idx++)
        {
            var candidate = symbols[idx];
            var max = Math.Min(prefix.Length, candidate.Length);
            var pos = 0;
            while (pos < max && prefix[pos] == candidate[pos])
            {
                pos++;
            }

            prefix = prefix.Substring(0, pos);
        }

        var marker = prefix.LastIndexOf('_');
        if (marker < 0)
        {
            return string.Empty;
        }

        return prefix.Substring(0, marker + 1);
    }

    private static void RenderEnum(StringBuilder builder, string enumName, EnumSpec spec)
    {
        var managedName = ToManagedTypeName(enumName, stripTypedefSuffix: true);
        var memberNames = spec.Members.Select(item => item.Name).Where(name => !string.IsNullOrWhiteSpace(name)).ToArray();

        var commonPrefix = string.Empty;
        if (memberNames.Length > 0)
        {
            commonPrefix = memberNames[0];
            for (var idx = 1; idx < memberNames.Length && commonPrefix.Length > 0; idx++)
            {
                var item = memberNames[idx];
                var max = Math.Min(commonPrefix.Length, item.Length);
                var pos = 0;
                while (pos < max && commonPrefix[pos] == item[pos])
                {
                    pos++;
                }
                commonPrefix = commonPrefix.Substring(0, pos);
            }

            var marker = commonPrefix.LastIndexOf('_');
            if (marker >= 0)
            {
                commonPrefix = commonPrefix.Substring(0, marker + 1);
            }
        }

        builder.AppendLine($"internal enum {managedName}");
        builder.AppendLine("{");
        foreach (var member in spec.Members)
        {
            var trimmed = member.Name;
            if (!string.IsNullOrWhiteSpace(commonPrefix) &&
                trimmed.StartsWith(commonPrefix, StringComparison.Ordinal))
            {
                trimmed = trimmed.Substring(commonPrefix.Length);
            }
            trimmed = trimmed.Trim('_');
            var managedMemberName = ToManagedTypeName(trimmed.ToLowerInvariant(), stripTypedefSuffix: false);

            if (member.Value.HasValue)
            {
                builder.AppendLine($"    {managedMemberName} = {member.Value.Value},");
            }
            else
            {
                builder.AppendLine($"    {managedMemberName},");
            }
        }
        builder.AppendLine("}");
        builder.AppendLine();
    }

    private static void RenderStruct(
        StringBuilder builder,
        IdlTypeModel model,
        string structName,
        StructSpec spec,
        IReadOnlyDictionary<string, string> callbackFieldTypes)
    {
        var managedName = ToManagedTypeName(structName, stripTypedefSuffix: true);
        var layoutSpec = model.StructLayoutOverrides.TryGetValue(structName, out var overrideSpec)
            ? overrideSpec
            : StructLayoutOverrideSpec.Default;
        if (layoutSpec.Pack.HasValue)
        {
            builder.AppendLine($"[StructLayout(LayoutKind.{layoutSpec.LayoutKind}, Pack = {layoutSpec.Pack.Value})]");
        }
        else
        {
            builder.AppendLine($"[StructLayout(LayoutKind.{layoutSpec.LayoutKind})]");
        }
        builder.AppendLine($"internal struct {managedName}");
        builder.AppendLine("{");

        foreach (var field in spec.Fields)
        {
            if (callbackFieldTypes.TryGetValue(field.Name, out var overrideDelegate))
            {
                builder.AppendLine($"    public {overrideDelegate}? {field.Name};");
                builder.AppendLine();
                continue;
            }

            var overrideKey = structName + "." + field.Name;
            if (model.StructFieldOverrides.TryGetValue(overrideKey, out var overriddenType) &&
                !string.IsNullOrWhiteSpace(overriddenType))
            {
                builder.AppendLine($"    public {overriddenType} {field.Name};");
                builder.AppendLine();
                continue;
            }

            var arrayMatch = Regex.Match(field.Declaration,
                "^(?<type>.+?)\\s+" + Regex.Escape(field.Name) + "\\s*\\[(?<len>\\d+)\\]$",
                RegexOptions.CultureInvariant);
            if (arrayMatch.Success)
            {
                var cType = NormalizeCType(arrayMatch.Groups["type"].Value);
                var length = arrayMatch.Groups["len"].Value;
                var managedType = MapManagedBaseType(cType, model);
                builder.AppendLine($"    [MarshalAs(UnmanagedType.ByValArray, SizeConst = {length})]");
                builder.AppendLine($"    public {managedType}[] {field.Name};");
                builder.AppendLine();
                continue;
            }

            var cTypeForField = ExtractFieldType(field.Declaration, field.Name);
            var managedFieldType = MapManagedFieldType(cTypeForField, model);

            var stripped = StripCTypeQualifiers(cTypeForField);
            if (string.Equals(stripped, "bool", StringComparison.Ordinal))
            {
                builder.AppendLine("    [MarshalAs(UnmanagedType.I1)]");
            }
            if (stripped.EndsWith("_cb", StringComparison.Ordinal))
            {
                managedFieldType += "?";
            }

            builder.AppendLine($"    public {managedFieldType} {field.Name};");
            builder.AppendLine();
        }

        builder.AppendLine("}");
        builder.AppendLine();
    }

    private static void RenderDelegate(StringBuilder builder, IdlTypeModel model, string delegateName, DelegateSpec spec)
    {
        var returnType = MapManagedBaseType(spec.ReturnType, model);
        var parameters = new List<string>();

        for (var idx = 0; idx < spec.Parameters.Count; idx++)
        {
            var parameter = spec.Parameters[idx];
            var parameterType = parameter.Variadic
                ? "IntPtr"
                : MapManagedParameterType(parameter.CType, model);
            var parameterName = SanitizeIdentifier(parameter.Name, $"arg{idx}");
            parameters.Add(parameterType + " " + parameterName);
        }

        builder.AppendLine("[UnmanagedFunctionPointer(CallingConvention.Cdecl)]");
        builder.AppendLine($"internal delegate {returnType} {delegateName}({string.Join(", ", parameters)});");
        builder.AppendLine();
    }

    private static bool IsCallbackStructName(string structName, IdlTypeModel model)
    {
        foreach (var suffix in model.CallbackStructSuffixes)
        {
            if (structName.EndsWith(suffix, StringComparison.Ordinal))
            {
                return true;
            }
        }
        return false;
    }

    private static string ExtractFieldType(string declaration, string fieldName)
    {
        var decl = declaration.Trim();
        if (decl.EndsWith(fieldName, StringComparison.Ordinal))
        {
            return NormalizeCType(decl.Substring(0, decl.Length - fieldName.Length).Trim());
        }

        return NormalizeCType(decl);
    }

    private static string MapManagedParameterType(string cType, IdlTypeModel model)
    {
        var stripped = StripCTypeQualifiers(cType);
        if (stripped.IndexOf('*') >= 0)
        {
            return "IntPtr";
        }

        return MapManagedBaseType(stripped, model);
    }

    private static string MapManagedFieldType(string cType, IdlTypeModel model)
    {
        var stripped = StripCTypeQualifiers(cType);
        if (stripped.IndexOf('*') >= 0)
        {
            return "IntPtr";
        }
        if (model.EnumNames.Contains(stripped))
        {
            return "int";
        }

        return MapManagedBaseType(stripped, model);
    }

    private static string MapManagedBaseType(string cTypeBase, IdlTypeModel model)
    {
        var stripped = StripCTypeQualifiers(cTypeBase);

        if (PrimitiveTypeMap.TryGetValue(stripped, out var primitive))
        {
            return primitive;
        }

        if (model.EnumNames.Contains(stripped) || model.StructNames.Contains(stripped))
        {
            return ToManagedTypeName(stripped, stripTypedefSuffix: true);
        }

        if (stripped.EndsWith("_cb", StringComparison.Ordinal))
        {
            return ToManagedTypeName(stripped, stripTypedefSuffix: false);
        }

        if (stripped.EndsWith("_t", StringComparison.Ordinal))
        {
            return ToManagedTypeName(stripped, stripTypedefSuffix: true);
        }

        return "IntPtr";
    }

    private static DelegateSpec? ParseCallbackTypedefDeclaration(
        string declaration,
        HashSet<string> allowedCallTokens)
    {
        var match = CallbackTypedefRegex.Match(declaration.Trim());
        if (!match.Success)
        {
            return null;
        }

        var callToken = match.Groups["call"].Success ? match.Groups["call"].Value.Trim() : string.Empty;
        if (allowedCallTokens.Count > 0 &&
            !string.IsNullOrWhiteSpace(callToken) &&
            !allowedCallTokens.Contains(callToken))
        {
            return null;
        }

        var name = match.Groups["name"].Value;
        var managedName = ToManagedTypeName(name, stripTypedefSuffix: false);
        return new DelegateSpec(
            managedName,
            NormalizeCType(match.Groups["ret"].Value),
            ParseParameterList(match.Groups["params"].Value));
    }

    private static DelegateSpec? ParseFunctionPointerField(string declaration)
    {
        var match = FunctionPointerFieldRegex.Match(declaration.Trim());
        if (!match.Success)
        {
            return null;
        }

        return new DelegateSpec(
            string.Empty,
            NormalizeCType(match.Groups["ret"].Value),
            ParseParameterList(match.Groups["params"].Value));
    }

    private static List<DelegateParameterSpec> ParseParameterList(string raw)
    {
        var value = raw.Trim();
        if (value.Length == 0 || string.Equals(value, "void", StringComparison.Ordinal))
        {
            return new List<DelegateParameterSpec>();
        }

        var parts = new List<string>();
        var token = new StringBuilder();
        var depth = 0;
        foreach (var ch in value)
        {
            if (ch == ',' && depth == 0)
            {
                var current = token.ToString().Trim();
                if (current.Length > 0)
                {
                    parts.Add(current);
                }
                token.Clear();
                continue;
            }

            token.Append(ch);
            if (ch == '(' || ch == '[')
            {
                depth++;
            }
            else if (ch == ')' || ch == ']')
            {
                depth = Math.Max(0, depth - 1);
            }
        }

        var tail = token.ToString().Trim();
        if (tail.Length > 0)
        {
            parts.Add(tail);
        }

        var result = new List<DelegateParameterSpec>(parts.Count);
        for (var idx = 0; idx < parts.Count; idx++)
        {
            var part = NormalizeCType(parts[idx]);
            part = Regex.Replace(part, "\\*([A-Za-z_])", "* $1");
            if (string.Equals(part, "...", StringComparison.Ordinal))
            {
                result.Add(new DelegateParameterSpec($"arg{idx}", "...", true));
                continue;
            }

            var regularMatch = Regex.Match(part, "^(?<left>.+?)\\s+(?<name>[A-Za-z_][A-Za-z0-9_]*)$");
            if (regularMatch.Success)
            {
                result.Add(new DelegateParameterSpec(
                    regularMatch.Groups["name"].Value,
                    NormalizeCType(regularMatch.Groups["left"].Value),
                    false));
                continue;
            }

            result.Add(new DelegateParameterSpec($"arg{idx}", part, false));
        }

        return result;
    }

    private static string BuildDelegateSignatureKey(DelegateSpec delegateSpec)
    {
        var parameterTypes = delegateSpec.Parameters
            .Select(item => NormalizeCType(item.CType))
            .ToArray();
        return NormalizeCType(delegateSpec.ReturnType) + "|" + string.Join(",", parameterTypes);
    }

    private static bool TryGetHeaderObject(JsonElement root, string section, out JsonElement sectionObj)
    {
        sectionObj = default;
        if (!root.TryGetProperty("header_types", out var headerObj) || headerObj.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        if (!headerObj.TryGetProperty(section, out sectionObj) || sectionObj.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        return true;
    }

    private static bool TryGetHeaderArray(JsonElement root, string section, out JsonElement sectionObj)
    {
        sectionObj = default;
        if (!root.TryGetProperty("header_types", out var headerObj) || headerObj.ValueKind != JsonValueKind.Object)
        {
            return false;
        }
        if (!headerObj.TryGetProperty(section, out sectionObj) || sectionObj.ValueKind != JsonValueKind.Array)
        {
            return false;
        }
        return true;
    }

    private static string ReadRequiredString(JsonElement obj, string key, string context)
    {
        if (obj.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String)
        {
            var text = value.GetString();
            if (!string.IsNullOrWhiteSpace(text))
            {
                return text!;
            }
        }

        throw new GeneratorException($"{context} is missing required string '{key}'.");
    }

    private static string ReadOptionalString(JsonElement obj, string key, string fallback)
    {
        if (obj.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.String)
        {
            var text = value.GetString();
            if (!string.IsNullOrWhiteSpace(text))
            {
                return text!;
            }
        }

        return fallback;
    }

    private static int? ReadOptionalPositiveInt(JsonElement obj, string key)
    {
        if (!obj.TryGetProperty(key, out var value) || value.ValueKind != JsonValueKind.Number)
        {
            return null;
        }

        if (!value.TryGetInt32(out var number) || number <= 0)
        {
            return null;
        }

        return number;
    }

    private static string NormalizeLayoutKind(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return "Sequential";
        }

        var normalized = value.Trim().ToLowerInvariant();
        if (normalized == "sequential")
        {
            return "Sequential";
        }
        if (normalized == "explicit")
        {
            return "Explicit";
        }
        if (normalized == "auto")
        {
            return "Auto";
        }

        return "Sequential";
    }

    private static string NormalizeCType(string value)
    {
        var text = Regex.Replace(value, "\\s+", " ").Trim();
        text = Regex.Replace(text, "\\s*\\*\\s*", "*");
        return text;
    }

    private static string StripCTypeQualifiers(string value)
    {
        var text = NormalizeCType(value);
        text = Regex.Replace(text, "\\b(const|volatile|restrict)\\b", " ");
        text = Regex.Replace(text, "\\b(struct|enum)\\s+", " ");
        text = Regex.Replace(text, "\\s+", " ").Trim();
        text = Regex.Replace(text, "\\s*\\*\\s*", "*");
        return text;
    }

    private static string ToManagedTypeName(string cIdentifier, bool stripTypedefSuffix)
    {
        var value = cIdentifier;
        if (stripTypedefSuffix && value.EndsWith("_t", StringComparison.Ordinal))
        {
            value = value.Substring(0, value.Length - 2);
        }

        var parts = value.Split(new[] { '_' }, StringSplitOptions.RemoveEmptyEntries);
        var builder = new StringBuilder();
        foreach (var rawPart in parts)
        {
            var part = rawPart.Trim();
            if (part.Length == 0)
            {
                continue;
            }

            builder.Append(char.ToUpperInvariant(part[0]));
            if (part.Length > 1)
            {
                builder.Append(part.Substring(1));
            }
        }

        var joined = builder.ToString();
        return string.IsNullOrWhiteSpace(joined) ? "IntPtr" : joined;
    }

    private static string ToManagedConstantName(string macroName, string constantPrefix)
    {
        var trimmed = !string.IsNullOrWhiteSpace(constantPrefix) &&
            macroName.StartsWith(constantPrefix, StringComparison.Ordinal)
            ? macroName.Substring(constantPrefix.Length)
            : macroName;
        return ToManagedTypeName(trimmed.ToLowerInvariant(), stripTypedefSuffix: false);
    }

    private static string SanitizeIdentifier(string value, string fallback)
    {
        var candidate = string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        if (!Regex.IsMatch(candidate, "^[A-Za-z_][A-Za-z0-9_]*$"))
        {
            candidate = Regex.Replace(candidate, "[^A-Za-z0-9_]", "_");
            if (!Regex.IsMatch(candidate, "^[A-Za-z_].*$"))
            {
                candidate = "_" + candidate;
            }
        }

        return CSharpKeywords.Contains(candidate) ? "@" + candidate : candidate;
    }
}

internal sealed class IdlTypeModel
{
    public IdlTypeModel(
        Dictionary<string, EnumSpec> enums,
        Dictionary<string, StructSpec> structs,
        Dictionary<string, DelegateSpec> callbackTypedefs,
        Dictionary<string, string> constants,
        Dictionary<string, string> callbackFieldOverrides,
        Dictionary<string, string> structFieldOverrides,
        Dictionary<string, StructLayoutOverrideSpec> structLayoutOverrides,
        HashSet<string> functionNames,
        Dictionary<string, string> functionFirstParameterTypes,
        HashSet<string> callbackTypedefCallTokens,
        IReadOnlyList<string> callbackStructSuffixes)
    {
        Enums = enums;
        Structs = structs;
        CallbackTypedefs = callbackTypedefs;
        Constants = constants;
        CallbackFieldOverrides = callbackFieldOverrides;
        StructFieldOverrides = structFieldOverrides;
        StructLayoutOverrides = structLayoutOverrides;
        FunctionNames = functionNames;
        FunctionFirstParameterTypes = functionFirstParameterTypes;
        CallbackTypedefCallTokens = callbackTypedefCallTokens;
        CallbackStructSuffixes = callbackStructSuffixes;

        EnumNames = new HashSet<string>(enums.Keys, StringComparer.Ordinal);
        StructNames = new HashSet<string>(structs.Keys, StringComparer.Ordinal);
    }

    public Dictionary<string, EnumSpec> Enums { get; }

    public Dictionary<string, StructSpec> Structs { get; }

    public Dictionary<string, DelegateSpec> CallbackTypedefs { get; }

    public Dictionary<string, string> Constants { get; }

    public Dictionary<string, string> CallbackFieldOverrides { get; }

    public Dictionary<string, string> StructFieldOverrides { get; }

    public Dictionary<string, StructLayoutOverrideSpec> StructLayoutOverrides { get; }

    public HashSet<string> FunctionNames { get; }

    public Dictionary<string, string> FunctionFirstParameterTypes { get; }

    public HashSet<string> CallbackTypedefCallTokens { get; }

    public IReadOnlyList<string> CallbackStructSuffixes { get; }

    public HashSet<string> EnumNames { get; }

    public HashSet<string> StructNames { get; }
}

internal sealed class StructLayoutOverrideSpec
{
    public static readonly StructLayoutOverrideSpec Default = new("Sequential", null);

    public StructLayoutOverrideSpec(string layoutKind, int? pack)
    {
        LayoutKind = string.IsNullOrWhiteSpace(layoutKind) ? "Sequential" : layoutKind;
        Pack = pack;
    }

    public string LayoutKind { get; }

    public int? Pack { get; }
}

internal sealed class EnumSpec
{
    public EnumSpec(IReadOnlyList<EnumMemberSpec> members)
    {
        Members = members;
    }

    public IReadOnlyList<EnumMemberSpec> Members { get; }
}

internal sealed class EnumMemberSpec
{
    public EnumMemberSpec(string name, int? value)
    {
        Name = name;
        Value = value;
    }

    public string Name { get; }

    public int? Value { get; }
}

internal sealed class StructSpec
{
    public StructSpec(IReadOnlyList<StructFieldSpec> fields)
    {
        Fields = fields;
    }

    public IReadOnlyList<StructFieldSpec> Fields { get; }
}

internal sealed class StructFieldSpec
{
    public StructFieldSpec(string name, string declaration)
    {
        Name = name;
        Declaration = declaration;
    }

    public string Name { get; }

    public string Declaration { get; }
}

internal sealed class DelegateSpec
{
    public DelegateSpec(string name, string returnType, IReadOnlyList<DelegateParameterSpec> parameters)
    {
        Name = name;
        ReturnType = returnType;
        Parameters = parameters;
    }

    public string Name { get; }

    public string ReturnType { get; }

    public IReadOnlyList<DelegateParameterSpec> Parameters { get; }
}

internal sealed class DelegateParameterSpec
{
    public DelegateParameterSpec(string name, string cType, bool variadic)
    {
        Name = name;
        CType = cType;
        Variadic = variadic;
    }

    public string Name { get; }

    public string CType { get; }

    public bool Variadic { get; }
}

internal sealed class ManagedHandlesModel
{
    public ManagedHandlesModel(IReadOnlyList<ManagedHandleSpec> handles)
    {
        Handles = handles;
    }

    public IReadOnlyList<ManagedHandleSpec> Handles { get; }
}

internal sealed class ManagedHandleSpec
{
    public ManagedHandleSpec(string namespaceName, string csType, string releaseMethod, string access, string cHandleType)
    {
        NamespaceName = namespaceName;
        CsType = csType;
        ReleaseMethod = releaseMethod;
        Access = access;
        CHandleType = cHandleType;
    }

    public string NamespaceName { get; }

    public string CsType { get; }

    public string ReleaseMethod { get; }

    public string Access { get; }

    public string CHandleType { get; }
}
