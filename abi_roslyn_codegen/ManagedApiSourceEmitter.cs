using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace Abi.RoslynGenerator;

internal static class ManagedApiSourceEmitter
{
    private const int SupportedSchemaVersion = 2;

    private static readonly HashSet<string> OutputHintsReservedKeys = new(StringComparer.Ordinal)
    {
        "pattern",
        "prefix",
        "suffix",
        "directory",
        "apply_prefix_to_explicit",
        "apply_directory_to_explicit",
        "sections",
    };

    private static readonly BuiltInSectionRenderer[] BuiltInSectionRenderers =
    {
        new(
            sectionName: "callbacks",
            hasContent: static model => model.Callbacks.Count > 0,
            render: static model => RenderCallbacksCode(model)),
        new(
            sectionName: "builder",
            hasContent: static model => model.Builder != null,
            render: static model => RenderBuilderCode(model)),
        new(
            sectionName: "handle_api",
            hasContent: static model => model.HandleApiClasses.Count > 0,
            render: static model => RenderHandleApiCode(model)),
        new(
            sectionName: "peer_connection_async",
            hasContent: static model => model.PeerConnectionAsync != null,
            render: static model => RenderPeerConnectionAsyncCode(model)),
    };

    public static ManagedApiModel ParseManagedApiMetadata(string text, IdlModel idlModel)
    {
        JsonDocument document;
        try
        {
            document = JsonDocument.Parse(text);
        }
        catch (JsonException ex)
        {
            throw new GeneratorException($"Managed API metadata JSON is invalid: {ex.Message}");
        }

        using (document)
        {
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException("Managed API metadata root must be an object.");
            }

            var schemaVersion = ReadRequiredInt(root, "schema_version", "managed_api");
            if (schemaVersion != SupportedSchemaVersion)
            {
                throw new GeneratorException(
                    $"managed_api.schema_version must be {SupportedSchemaVersion}, got {schemaVersion}.");
            }

            var namespaceName = ReadRequiredString(root, "namespace", "managed_api");

            var requiredNativeFunctions = ReadStringArray(root, "required_native_functions", "managed_api");
            ValidateRequiredNativeFunctions(requiredNativeFunctions, idlModel);

            var callbacks = ParseCallbacks(root);
            var builder = ParseBuilder(root);
            var handleApiClasses = ParseHandleApi(root);
            var peerConnectionAsync = ParsePeerConnectionAsync(root);
            var customSections = ParseCustomSections(root);
            var outputHints = ParseOutputHints(root, "managed_api");

            return new ManagedApiModel(
                namespaceName,
                callbacks,
                builder,
                handleApiClasses,
                peerConnectionAsync,
                customSections,
                outputHints);
        }
    }

    public static IReadOnlyList<GeneratedSourceSpec> RenderSources(ManagedApiModel model)
    {
        var result = new List<GeneratedSourceSpec>();
        foreach (var builtIn in BuiltInSectionRenderers)
        {
            if (!builtIn.HasContent(model))
            {
                continue;
            }

            result.Add(new GeneratedSourceSpec(
                model.OutputHints.ResolveHint(
                    builtIn.SectionName,
                    BuildManagedSectionDefaultHint(builtIn.SectionName),
                    model.NamespaceName),
                builtIn.Render(model)));
        }

        foreach (var section in model.CustomSections)
        {
            result.Add(new GeneratedSourceSpec(
                model.OutputHints.ResolveHint(section.SectionName, section.DefaultHint, model.NamespaceName),
                RenderSingleClassSectionCode(model.NamespaceName, section.ClassName, section.Methods)));
        }

        return result;
    }

    private static string BuildManagedSectionDefaultHint(string sectionName)
    {
        if (string.IsNullOrWhiteSpace(sectionName))
        {
            return "ManagedApi.g.cs";
        }

        var tokens = sectionName
            .Split(new[] { '_', '-', '.', '/' }, StringSplitOptions.RemoveEmptyEntries);
        var builder = new StringBuilder("ManagedApi.");
        foreach (var token in tokens)
        {
            var trimmed = token.Trim();
            if (trimmed.Length == 0)
            {
                continue;
            }

            builder.Append(char.ToUpperInvariant(trimmed[0]));
            if (trimmed.Length > 1)
            {
                builder.Append(trimmed.Substring(1));
            }
        }

        if (builder.Length <= "ManagedApi.".Length)
        {
            return "ManagedApi.g.cs";
        }

        builder.Append(".g.cs");
        return builder.ToString();
    }

    private readonly struct BuiltInSectionRenderer
    {
        public BuiltInSectionRenderer(
            string sectionName,
            Func<ManagedApiModel, bool> hasContent,
            Func<ManagedApiModel, string> render)
        {
            SectionName = sectionName;
            HasContent = hasContent;
            Render = render;
        }

        public string SectionName { get; }

        public Func<ManagedApiModel, bool> HasContent { get; }

        public Func<ManagedApiModel, string> Render { get; }
    }

    private static string RenderCallbacksCode(ManagedApiModel model)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, model.NamespaceName);

        foreach (var callbackClass in model.Callbacks)
        {
            builder.AppendLine("/// <summary>");
            builder.AppendLine($"/// {callbackClass.Summary}");
            builder.AppendLine("/// </summary>");
            builder.AppendLine($"public sealed class {callbackClass.ClassName}");
            builder.AppendLine("{");

            foreach (var field in callbackClass.Fields)
            {
                AppendLine(builder, 4, $"public {field.ManagedType} {field.ManagedName};");
            }

            builder.AppendLine();

            foreach (var field in callbackClass.Fields)
            {
                AppendLine(builder, 4, $"private {field.DelegateType}? {field.DelegateField};");
            }

            builder.AppendLine();
            AppendLine(builder, 4, $"internal {callbackClass.NativeStruct} BuildNative()");
            AppendLine(builder, 4, "{");

            foreach (var field in callbackClass.Fields)
            {
                var assignmentLines = RenderAssignment(field.DelegateField, field.AssignmentLines, callbackClass.ClassName);
                foreach (var line in assignmentLines)
                {
                    AppendLine(builder, 8, line);
                }
            }

            builder.AppendLine();
            AppendLine(builder, 8, $"return new {callbackClass.NativeStruct}");
            AppendLine(builder, 8, "{");
            foreach (var field in callbackClass.Fields)
            {
                AppendLine(builder, 12, $"{field.NativeField} = {field.DelegateField},");
            }
            AppendLine(builder, 8, "};");
            AppendLine(builder, 4, "}");
            builder.AppendLine("}");
            builder.AppendLine();
        }

        return builder.ToString();
    }

    private static string RenderBuilderCode(ManagedApiModel model)
    {
        if (model.Builder == null)
        {
            return string.Empty;
        }
        return RenderSingleClassSectionCode(model.NamespaceName, model.Builder.ClassName, model.Builder.Methods);
    }

    private static string RenderHandleApiCode(ManagedApiModel model)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, model.NamespaceName);

        foreach (var classSpec in model.HandleApiClasses)
        {
            builder.AppendLine($"public sealed partial class {classSpec.ClassName}");
            builder.AppendLine("{");
            RenderMethodItems(builder, classSpec.Members, 4);
            builder.AppendLine("}");
            builder.AppendLine();
        }

        return builder.ToString();
    }

    private static string RenderPeerConnectionAsyncCode(ManagedApiModel model)
    {
        if (model.PeerConnectionAsync == null)
        {
            return string.Empty;
        }
        return RenderSingleClassSectionCode(
            model.NamespaceName,
            model.PeerConnectionAsync.ClassName,
            model.PeerConnectionAsync.Methods);
    }

    private static string RenderSingleClassSectionCode(
        string namespaceName,
        string className,
        IReadOnlyList<MethodItemSpec> methods)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);

        builder.AppendLine($"public sealed partial class {className}");
        builder.AppendLine("{");
        RenderMethodItems(builder, methods, 4);
        builder.AppendLine("}");
        builder.AppendLine();

        return builder.ToString();
    }

    private static ManagedApiOutputHints ParseOutputHints(JsonElement root, string contextPrefix)
    {
        var outputHints = ManagedApiOutputHints.Default();
        if (!root.TryGetProperty("output_hints", out var outputHintsElement) ||
            outputHintsElement.ValueKind == JsonValueKind.Null)
        {
            return outputHints;
        }

        if (outputHintsElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException($"{contextPrefix}.output_hints must be an object when present.");
        }

        var sectionHints = new Dictionary<string, string>(StringComparer.Ordinal);
        var pattern = ReadOptionalString(outputHintsElement, "pattern", outputHints.Pattern);
        var prefix = ReadOptionalString(outputHintsElement, "prefix", outputHints.Prefix);
        var suffix = ReadOptionalString(outputHintsElement, "suffix", outputHints.Suffix);
        var directory = ReadOptionalString(outputHintsElement, "directory", outputHints.Directory);
        var applyPrefixToExplicit = ReadOptionalBool(
            outputHintsElement,
            "apply_prefix_to_explicit",
            outputHints.ApplyPrefixToExplicit);
        var applyDirectoryToExplicit = ReadOptionalBool(
            outputHintsElement,
            "apply_directory_to_explicit",
            outputHints.ApplyDirectoryToExplicit);

        CollectSectionHintsFromSectionsObject(outputHintsElement, sectionHints);
        CollectSectionHintsFromKnownKeys(outputHintsElement, sectionHints);
        CollectSectionHintsFromCustomKeys(outputHintsElement, sectionHints);

        return new ManagedApiOutputHints(
            pattern,
            prefix,
            suffix,
            directory,
            applyPrefixToExplicit,
            applyDirectoryToExplicit,
            sectionHints);
    }

    private static void CollectSectionHintsFromSectionsObject(
        JsonElement outputHintsElement,
        Dictionary<string, string> sectionHints)
    {
        if (!outputHintsElement.TryGetProperty("sections", out var sectionsElement) ||
            sectionsElement.ValueKind == JsonValueKind.Null)
        {
            return;
        }

        if (sectionsElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.output_hints.sections must be an object when present.");
        }

        foreach (var property in sectionsElement.EnumerateObject())
        {
            if (property.Value.ValueKind != JsonValueKind.String)
            {
                throw new GeneratorException(
                    $"managed_api.output_hints.sections.{property.Name} must be a string.");
            }

            sectionHints[property.Name] = property.Value.GetString() ?? string.Empty;
        }
    }

    private static void CollectSectionHintsFromKnownKeys(
        JsonElement outputHintsElement,
        Dictionary<string, string> sectionHints)
    {
        MapOptionalSectionHint(outputHintsElement, sectionHints, "callbacks", "callbacks");
        MapOptionalSectionHint(outputHintsElement, sectionHints, "builder", "builder");
        MapOptionalSectionHint(outputHintsElement, sectionHints, "handle_api", "handle_api");
        MapOptionalSectionHint(outputHintsElement, sectionHints, "peer_connection_async", "peer_connection_async");
    }

    private static void CollectSectionHintsFromCustomKeys(
        JsonElement outputHintsElement,
        Dictionary<string, string> sectionHints)
    {
        foreach (var property in outputHintsElement.EnumerateObject())
        {
            if (OutputHintsReservedKeys.Contains(property.Name))
            {
                continue;
            }

            if (property.Value.ValueKind != JsonValueKind.String)
            {
                throw new GeneratorException(
                    $"managed_api.output_hints.{property.Name} must be a string.");
            }

            sectionHints[property.Name] = property.Value.GetString() ?? string.Empty;
        }
    }

    private static void MapOptionalSectionHint(
        JsonElement outputHintsElement,
        Dictionary<string, string> sectionHints,
        string key,
        string sectionName)
    {
        if (!outputHintsElement.TryGetProperty(key, out var token))
        {
            return;
        }

        if (token.ValueKind != JsonValueKind.String)
        {
            throw new GeneratorException($"managed_api.output_hints.{key} must be a string.");
        }

        sectionHints[sectionName] = token.GetString() ?? string.Empty;
    }

    private static void ValidateRequiredNativeFunctions(
        IReadOnlyList<string> requiredFunctions,
        IdlModel idlModel)
    {
        var idlNames = new HashSet<string>(
            idlModel.Functions.Select(static function => function.Name),
            StringComparer.Ordinal);
        var missing = requiredFunctions
            .Where(name => !idlNames.Contains(name))
            .Distinct(StringComparer.Ordinal)
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (missing.Length > 0)
        {
            throw new GeneratorException(
                "managed_api.required_native_functions references unknown ABI functions: " +
                string.Join(", ", missing));
        }
    }

    private static IReadOnlyList<CallbackClassSpec> ParseCallbacks(JsonElement root)
    {
        if (!root.TryGetProperty("callbacks", out var callbacksElement) ||
            callbacksElement.ValueKind == JsonValueKind.Null)
        {
            return Array.Empty<CallbackClassSpec>();
        }
        if (callbacksElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException("managed_api.callbacks must be an array.");
        }

        var callbacks = new List<CallbackClassSpec>();
        var seenClasses = new HashSet<string>(StringComparer.Ordinal);

        var callbackIndex = 0;
        foreach (var callbackElement in callbacksElement.EnumerateArray())
        {
            var context = $"managed_api.callbacks[{callbackIndex}]";
            if (callbackElement.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException($"{context} must be an object.");
            }

            var className = ReadRequiredString(callbackElement, "class", context);
            if (!seenClasses.Add(className))
            {
                throw new GeneratorException($"Duplicate callback class '{className}'.");
            }

            var summary = ReadRequiredString(callbackElement, "summary", context);
            var nativeStruct = ReadRequiredString(callbackElement, "native_struct", context);

            if (!callbackElement.TryGetProperty("fields", out var fieldsElement) ||
                fieldsElement.ValueKind != JsonValueKind.Array)
            {
                throw new GeneratorException($"{context}.fields must be an array.");
            }

            var fields = new List<CallbackFieldSpec>();
            var fieldIndex = 0;
            foreach (var fieldElement in fieldsElement.EnumerateArray())
            {
                var fieldContext = $"{context}.fields[{fieldIndex}]";
                if (fieldElement.ValueKind != JsonValueKind.Object)
                {
                    throw new GeneratorException($"{fieldContext} must be an object.");
                }

                var managedName = ReadRequiredString(fieldElement, "managed_name", fieldContext);
                var managedType = ReadRequiredString(fieldElement, "managed_type", fieldContext);
                var delegateField = ReadRequiredString(fieldElement, "delegate_field", fieldContext);
                var delegateType = ReadRequiredString(fieldElement, "delegate_type", fieldContext);
                var nativeField = ReadRequiredString(fieldElement, "native_field", fieldContext);
                var assignmentLines = ReadStringArray(fieldElement, "assignment_lines", fieldContext);

                fields.Add(new CallbackFieldSpec(
                    managedName,
                    managedType,
                    delegateField,
                    delegateType,
                    nativeField,
                    assignmentLines));
                fieldIndex++;
            }

            callbacks.Add(new CallbackClassSpec(className, summary, nativeStruct, fields));
            callbackIndex++;
        }

        return callbacks;
    }

    private static BuilderSpec? ParseBuilder(JsonElement root)
    {
        if (!root.TryGetProperty("builder", out var builderElement) ||
            builderElement.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        if (builderElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.builder must be an object.");
        }

        var className = ReadRequiredString(builderElement, "class", "managed_api.builder");
        var methods = ParseMethodItems(builderElement, "methods", "managed_api.builder");
        return new BuilderSpec(className, methods);
    }

    private static IReadOnlyList<HandleApiClassSpec> ParseHandleApi(JsonElement root)
    {
        if (!root.TryGetProperty("handle_api", out var handleApiElement) ||
            handleApiElement.ValueKind == JsonValueKind.Null)
        {
            return Array.Empty<HandleApiClassSpec>();
        }
        if (handleApiElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException("managed_api.handle_api must be an array.");
        }

        var result = new List<HandleApiClassSpec>();
        var seenClasses = new HashSet<string>(StringComparer.Ordinal);
        var classIndex = 0;

        foreach (var classElement in handleApiElement.EnumerateArray())
        {
            var context = $"managed_api.handle_api[{classIndex}]";
            if (classElement.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException($"{context} must be an object.");
            }

            var className = ReadRequiredString(classElement, "class", context);
            if (!seenClasses.Add(className))
            {
                throw new GeneratorException($"Duplicate handle_api class '{className}'.");
            }

            var members = ParseMethodItems(classElement, "members", context);
            result.Add(new HandleApiClassSpec(className, members));
            classIndex++;
        }

        return result;
    }

    private static PeerConnectionAsyncSpec? ParsePeerConnectionAsync(JsonElement root)
    {
        if (!root.TryGetProperty("peer_connection_async", out var asyncElement) ||
            asyncElement.ValueKind == JsonValueKind.Null)
        {
            return null;
        }
        if (asyncElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.peer_connection_async must be an object.");
        }

        var className = ReadRequiredString(asyncElement, "class", "managed_api.peer_connection_async");
        var methods = ParseMethodItems(asyncElement, "methods", "managed_api.peer_connection_async");
        return new PeerConnectionAsyncSpec(className, methods);
    }

    private static IReadOnlyList<CustomClassSectionSpec> ParseCustomSections(JsonElement root)
    {
        if (!root.TryGetProperty("custom_sections", out var sectionsElement) ||
            sectionsElement.ValueKind == JsonValueKind.Null)
        {
            return Array.Empty<CustomClassSectionSpec>();
        }
        if (sectionsElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException("managed_api.custom_sections must be an array.");
        }

        var result = new List<CustomClassSectionSpec>();
        var seenNames = new HashSet<string>(StringComparer.Ordinal);
        var index = 0;
        foreach (var item in sectionsElement.EnumerateArray())
        {
            var context = $"managed_api.custom_sections[{index}]";
            if (item.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException($"{context} must be an object.");
            }

            var sectionName = ReadRequiredString(item, "name", context);
            if (!seenNames.Add(sectionName))
            {
                throw new GeneratorException($"Duplicate custom section '{sectionName}'.");
            }

            var className = ReadRequiredString(item, "class", context);
            var methods = ParseMethodItems(item, "methods", context);
            var defaultHint = ReadOptionalString(item, "default_hint", className + ".g.cs");

            result.Add(new CustomClassSectionSpec(sectionName, className, methods, defaultHint));
            index++;
        }

        return result;
    }

    private static IReadOnlyList<MethodItemSpec> ParseMethodItems(
        JsonElement parent,
        string key,
        string context)
    {
        if (!parent.TryGetProperty(key, out var methodsElement) ||
            methodsElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException($"{context}.{key} must be an array.");
        }

        var methods = new List<MethodItemSpec>();
        var methodIndex = 0;
        foreach (var methodElement in methodsElement.EnumerateArray())
        {
            var methodContext = $"{context}.{key}[{methodIndex}]";
            if (methodElement.ValueKind != JsonValueKind.Object)
            {
                throw new GeneratorException($"{methodContext} must be an object.");
            }

            methods.Add(ParseMethodItem(methodElement, methodContext));
            methodIndex++;
        }

        return methods;
    }

    private static MethodItemSpec ParseMethodItem(JsonElement element, string context)
    {
        var hasLine = TryGetString(element, "line", out var line);
        var hasSignature = TryGetString(element, "signature", out var signature);
        var hasLines = element.TryGetProperty("lines", out var linesElement);

        var variants = (hasLine ? 1 : 0) + (hasSignature ? 1 : 0) + (hasLines ? 1 : 0);
        if (variants != 1)
        {
            throw new GeneratorException($"{context} must define exactly one of: line, signature, lines.");
        }

        if (hasLine)
        {
            return MethodItemSpec.ForLine(line!);
        }

        if (hasSignature)
        {
            var body = ReadStringArray(element, "body", context);
            return MethodItemSpec.ForSignature(signature!, body);
        }

        if (linesElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException($"{context}.lines must be an array.");
        }

        var lines = ReadStringArray(element, "lines", context);
        return MethodItemSpec.ForLines(lines);
    }

    private static IReadOnlyList<string> RenderAssignment(
        string delegateField,
        IReadOnlyList<string> assignmentLines,
        string context)
    {
        var normalized = NormalizeBlock(assignmentLines);
        if (normalized.Count == 0)
        {
            throw new GeneratorException($"{context}.assignment_lines must not be empty.");
        }

        if (normalized.Count == 1)
        {
            return new[] { $"{delegateField} = {normalized[0]};" };
        }

        var rendered = new List<string> { $"{delegateField} = {normalized[0]}" };
        var continuation = normalized.Skip(1).ToList();

        if (continuation.Count > 0 &&
            !string.IsNullOrWhiteSpace(continuation[0]) &&
            !continuation[0].TrimStart().StartsWith("{", StringComparison.Ordinal))
        {
            for (var i = 0; i < continuation.Count; i++)
            {
                if (!string.IsNullOrEmpty(continuation[i]))
                {
                    continuation[i] = "    " + continuation[i];
                }
            }
        }

        rendered.AddRange(continuation);
        rendered[rendered.Count - 1] = rendered[rendered.Count - 1] + ";";
        return rendered;
    }

    private static void RenderMethodItems(
        StringBuilder builder,
        IReadOnlyList<MethodItemSpec> methodItems,
        int classIndent)
    {
        foreach (var method in methodItems)
        {
            switch (method.Kind)
            {
                case MethodItemKind.Line:
                    AppendLine(builder, classIndent, method.Line!);
                    builder.AppendLine();
                    break;
                case MethodItemKind.Signature:
                    AppendLine(builder, classIndent, method.Signature!);
                    AppendLine(builder, classIndent, "{");
                    foreach (var bodyLine in NormalizeBlock(method.Body!))
                    {
                        AppendLine(builder, classIndent + 4, bodyLine);
                    }
                    AppendLine(builder, classIndent, "}");
                    builder.AppendLine();
                    break;
                case MethodItemKind.Lines:
                    foreach (var line in NormalizeBlock(method.Lines!))
                    {
                        AppendLine(builder, classIndent, line);
                    }
                    builder.AppendLine();
                    break;
                default:
                    throw new GeneratorException($"Unsupported method item kind '{method.Kind}'.");
            }
        }
    }

    private static IReadOnlyList<string> NormalizeBlock(IReadOnlyList<string> lines)
    {
        if (lines.Count == 0)
        {
            return lines;
        }

        var minIndent = int.MaxValue;
        var hasNonEmpty = false;

        foreach (var line in lines)
        {
            if (string.IsNullOrWhiteSpace(line))
            {
                continue;
            }

            hasNonEmpty = true;
            var indent = 0;
            while (indent < line.Length && line[indent] == ' ')
            {
                indent++;
            }

            if (indent < minIndent)
            {
                minIndent = indent;
            }
        }

        if (!hasNonEmpty || minIndent <= 0)
        {
            return lines.ToArray();
        }

        var normalized = new string[lines.Count];
        for (var i = 0; i < lines.Count; i++)
        {
            var line = lines[i];
            normalized[i] = line.Length >= minIndent ? line.Substring(minIndent) : string.Empty;
        }

        return normalized;
    }

    private static void AppendFileHeader(StringBuilder builder, string namespaceName)
    {
        builder.AppendLine("// <auto-generated />");
        builder.AppendLine($"// Generated by abi_roslyn_codegen source generator {AbiInteropSourceEmitter.ToolVersion}");
        builder.AppendLine("#nullable enable");
        builder.AppendLine($"namespace {namespaceName};");
        builder.AppendLine();
    }

    private static void AppendLine(StringBuilder builder, int spaces, string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            builder.AppendLine();
            return;
        }

        builder.Append(' ', spaces);
        builder.AppendLine(value);
    }

    private static string ReadRequiredString(JsonElement element, string key, string context)
    {
        if (element.TryGetProperty(key, out var value) &&
            value.ValueKind == JsonValueKind.String &&
            !string.IsNullOrWhiteSpace(value.GetString()))
        {
            return value.GetString()!;
        }

        throw new GeneratorException($"{context}: '{key}' must be a non-empty string.");
    }

    private static string ReadOptionalString(JsonElement element, string key, string fallback)
    {
        if (element.TryGetProperty(key, out var value) &&
            value.ValueKind == JsonValueKind.String &&
            !string.IsNullOrWhiteSpace(value.GetString()))
        {
            return value.GetString()!;
        }

        return fallback;
    }

    private static int ReadRequiredInt(JsonElement element, string key, string context)
    {
        if (element.TryGetProperty(key, out var value) &&
            value.ValueKind == JsonValueKind.Number &&
            value.TryGetInt32(out var number))
        {
            return number;
        }

        throw new GeneratorException($"{context}: '{key}' must be an integer.");
    }

    private static bool ReadOptionalBool(JsonElement element, string key, bool fallback)
    {
        if (element.TryGetProperty(key, out var value))
        {
            if (value.ValueKind == JsonValueKind.True)
            {
                return true;
            }

            if (value.ValueKind == JsonValueKind.False)
            {
                return false;
            }
        }

        return fallback;
    }

    private static bool TryGetString(JsonElement element, string key, out string? value)
    {
        if (element.TryGetProperty(key, out var token) &&
            token.ValueKind == JsonValueKind.String &&
            !string.IsNullOrWhiteSpace(token.GetString()))
        {
            value = token.GetString()!;
            return true;
        }

        value = null;
        return false;
    }

    private static IReadOnlyList<string> ReadStringArray(JsonElement element, string key, string context)
    {
        if (!element.TryGetProperty(key, out var arrayElement) ||
            arrayElement.ValueKind != JsonValueKind.Array)
        {
            throw new GeneratorException($"{context}.{key} must be an array.");
        }

        var result = new List<string>();
        var index = 0;
        foreach (var item in arrayElement.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String)
            {
                throw new GeneratorException($"{context}.{key}[{index}] must be a string.");
            }

            result.Add(item.GetString() ?? string.Empty);
            index++;
        }

        return result;
    }
}

internal sealed class GeneratedSourceSpec
{
    public GeneratedSourceSpec(string hintName, string sourceText)
    {
        HintName = hintName;
        SourceText = sourceText;
    }

    public string HintName { get; }

    public string SourceText { get; }
}

internal sealed class ManagedApiModel
{
    public ManagedApiModel(
        string namespaceName,
        IReadOnlyList<CallbackClassSpec> callbacks,
        BuilderSpec? builder,
        IReadOnlyList<HandleApiClassSpec> handleApiClasses,
        PeerConnectionAsyncSpec? peerConnectionAsync,
        IReadOnlyList<CustomClassSectionSpec> customSections,
        ManagedApiOutputHints outputHints)
    {
        NamespaceName = namespaceName;
        Callbacks = callbacks;
        Builder = builder;
        HandleApiClasses = handleApiClasses;
        PeerConnectionAsync = peerConnectionAsync;
        CustomSections = customSections;
        OutputHints = outputHints;
    }

    public string NamespaceName { get; }

    public IReadOnlyList<CallbackClassSpec> Callbacks { get; }

    public BuilderSpec? Builder { get; }

    public IReadOnlyList<HandleApiClassSpec> HandleApiClasses { get; }

    public PeerConnectionAsyncSpec? PeerConnectionAsync { get; }

    public IReadOnlyList<CustomClassSectionSpec> CustomSections { get; }

    public ManagedApiOutputHints OutputHints { get; }
}

internal sealed class ManagedApiOutputHints
{
    private const string DefaultPattern = "{default}";
    private const string DefaultSuffix = ".g.cs";

    private readonly Dictionary<string, string> _sectionHints;

    public ManagedApiOutputHints(
        string pattern,
        string prefix,
        string suffix,
        string directory,
        bool applyPrefixToExplicit,
        bool applyDirectoryToExplicit,
        Dictionary<string, string> sectionHints)
    {
        Pattern = string.IsNullOrWhiteSpace(pattern) ? DefaultPattern : pattern.Trim();
        Prefix = prefix ?? string.Empty;
        Suffix = string.IsNullOrWhiteSpace(suffix) ? DefaultSuffix : suffix.Trim();
        Directory = directory ?? string.Empty;
        ApplyPrefixToExplicit = applyPrefixToExplicit;
        ApplyDirectoryToExplicit = applyDirectoryToExplicit;
        _sectionHints = sectionHints;
    }

    public string Pattern { get; }

    public string Prefix { get; }

    public string Suffix { get; }

    public string Directory { get; }

    public bool ApplyPrefixToExplicit { get; }

    public bool ApplyDirectoryToExplicit { get; }

    public static ManagedApiOutputHints Default()
    {
        return new ManagedApiOutputHints(
            pattern: DefaultPattern,
            prefix: string.Empty,
            suffix: DefaultSuffix,
            directory: string.Empty,
            applyPrefixToExplicit: false,
            applyDirectoryToExplicit: false,
            sectionHints: new Dictionary<string, string>(StringComparer.Ordinal));
    }

    public string ResolveHint(string sectionName, string defaultHint, string namespaceName)
    {
        var hasExplicit = _sectionHints.TryGetValue(sectionName, out var explicitTemplate);
        var template = hasExplicit ? explicitTemplate : Pattern;
        if (string.IsNullOrWhiteSpace(template))
        {
            template = DefaultPattern;
        }

        var rendered = ApplyTemplateTokens(template, sectionName, defaultHint, namespaceName);

        if (string.IsNullOrWhiteSpace(rendered))
        {
            rendered = defaultHint;
        }

        var candidate = rendered.Trim().Replace('\\', '/');

        if (!hasExplicit || ApplyPrefixToExplicit)
        {
            candidate = (Prefix ?? string.Empty) + candidate;
        }

        if (!candidate.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
        {
            candidate += Suffix;
        }

        if ((!hasExplicit || ApplyDirectoryToExplicit) && !string.IsNullOrWhiteSpace(Directory))
        {
            candidate = CombinePath(Directory, candidate);
        }

        return NormalizeHintName(candidate, defaultHint);
    }

    private static string ApplyTemplateTokens(
        string template,
        string sectionName,
        string defaultHint,
        string namespaceName)
    {
        var sectionSnake = ToSnakeOrKebabCase(sectionName, separator: '_');
        var sectionKebab = ToSnakeOrKebabCase(sectionName, separator: '-');
        var sectionPath = sectionName.Replace('.', '/').Replace('-', '/').Replace('_', '/');
        var sectionPascal = ToPascalCase(sectionName);
        var namespacePath = string.IsNullOrWhiteSpace(namespaceName)
            ? string.Empty
            : namespaceName.Replace('.', '/');
        var defaultStem = StripCsExtension(defaultHint);

        return template
            .Replace("{section}", sectionName)
            .Replace("{section_pascal}", sectionPascal)
            .Replace("{section_snake}", sectionSnake)
            .Replace("{section_kebab}", sectionKebab)
            .Replace("{section_path}", sectionPath)
            .Replace("{default}", defaultHint)
            .Replace("{default_stem}", defaultStem)
            .Replace("{default_name}", defaultStem)
            .Replace("{namespace}", namespaceName)
            .Replace("{namespace_path}", namespacePath);
    }

    private static string ToPascalCase(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        var tokens = value
            .Split(new[] { '_', '-', '.', '/' }, StringSplitOptions.RemoveEmptyEntries);

        var builder = new StringBuilder();
        foreach (var token in tokens)
        {
            var trimmed = token.Trim();
            if (trimmed.Length == 0)
            {
                continue;
            }

            builder.Append(char.ToUpperInvariant(trimmed[0]));
            if (trimmed.Length > 1)
            {
                builder.Append(trimmed.Substring(1));
            }
        }

        return builder.ToString();
    }

    private static string ToSnakeOrKebabCase(string value, char separator)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        var text = value.Replace('.', separator).Replace('-', separator).Replace('_', separator);
        var doubled = new string(separator, 2);
        while (text.IndexOf(doubled, StringComparison.Ordinal) >= 0)
        {
            text = text.Replace(doubled, new string(separator, 1));
        }

        return text.Trim(separator).ToLowerInvariant();
    }

    private static string StripCsExtension(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        return value.EndsWith(".cs", StringComparison.OrdinalIgnoreCase)
            ? value.Substring(0, value.Length - 3)
            : value;
    }

    private static string CombinePath(string left, string right)
    {
        var normalizedLeft = left.Replace('\\', '/').Trim('/');
        var normalizedRight = right.Replace('\\', '/').TrimStart('/');
        if (string.IsNullOrWhiteSpace(normalizedLeft))
        {
            return normalizedRight;
        }

        if (string.IsNullOrWhiteSpace(normalizedRight))
        {
            return normalizedLeft;
        }

        return normalizedLeft + "/" + normalizedRight;
    }

    private static string NormalizeHintName(string value, string fallback)
    {
        var normalized = string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        normalized = normalized.Replace('\\', '/');

        while (normalized.StartsWith("./", StringComparison.Ordinal))
        {
            normalized = normalized.Substring(2);
        }

        while (normalized.StartsWith("/", StringComparison.Ordinal))
        {
            normalized = normalized.Substring(1);
        }

        while (normalized.Contains("//", StringComparison.Ordinal))
        {
            normalized = normalized.Replace("//", "/");
        }

        if (string.IsNullOrWhiteSpace(normalized))
        {
            normalized = fallback;
        }

        if (!normalized.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
        {
            normalized += DefaultSuffix;
        }

        var chars = normalized.ToCharArray();
        for (var i = 0; i < chars.Length; i++)
        {
            var current = chars[i];
            if (char.IsLetterOrDigit(current) || current == '.' || current == '_' || current == '-' || current == '/')
            {
                continue;
            }

            chars[i] = '_';
        }

        var sanitized = new string(chars);
        return string.IsNullOrWhiteSpace(sanitized) ? fallback : sanitized;
    }
}

internal sealed class CallbackClassSpec
{
    public CallbackClassSpec(
        string className,
        string summary,
        string nativeStruct,
        IReadOnlyList<CallbackFieldSpec> fields)
    {
        ClassName = className;
        Summary = summary;
        NativeStruct = nativeStruct;
        Fields = fields;
    }

    public string ClassName { get; }

    public string Summary { get; }

    public string NativeStruct { get; }

    public IReadOnlyList<CallbackFieldSpec> Fields { get; }
}

internal sealed class CallbackFieldSpec
{
    public CallbackFieldSpec(
        string managedName,
        string managedType,
        string delegateField,
        string delegateType,
        string nativeField,
        IReadOnlyList<string> assignmentLines)
    {
        ManagedName = managedName;
        ManagedType = managedType;
        DelegateField = delegateField;
        DelegateType = delegateType;
        NativeField = nativeField;
        AssignmentLines = assignmentLines;
    }

    public string ManagedName { get; }

    public string ManagedType { get; }

    public string DelegateField { get; }

    public string DelegateType { get; }

    public string NativeField { get; }

    public IReadOnlyList<string> AssignmentLines { get; }
}

internal sealed class BuilderSpec
{
    public BuilderSpec(string className, IReadOnlyList<MethodItemSpec> methods)
    {
        ClassName = className;
        Methods = methods;
    }

    public string ClassName { get; }

    public IReadOnlyList<MethodItemSpec> Methods { get; }
}

internal sealed class HandleApiClassSpec
{
    public HandleApiClassSpec(string className, IReadOnlyList<MethodItemSpec> members)
    {
        ClassName = className;
        Members = members;
    }

    public string ClassName { get; }

    public IReadOnlyList<MethodItemSpec> Members { get; }
}

internal sealed class PeerConnectionAsyncSpec
{
    public PeerConnectionAsyncSpec(string className, IReadOnlyList<MethodItemSpec> methods)
    {
        ClassName = className;
        Methods = methods;
    }

    public string ClassName { get; }

    public IReadOnlyList<MethodItemSpec> Methods { get; }
}

internal sealed class CustomClassSectionSpec
{
    public CustomClassSectionSpec(
        string sectionName,
        string className,
        IReadOnlyList<MethodItemSpec> methods,
        string defaultHint)
    {
        SectionName = sectionName;
        ClassName = className;
        Methods = methods;
        DefaultHint = defaultHint;
    }

    public string SectionName { get; }

    public string ClassName { get; }

    public IReadOnlyList<MethodItemSpec> Methods { get; }

    public string DefaultHint { get; }
}

internal sealed class MethodItemSpec
{
    private MethodItemSpec(
        MethodItemKind kind,
        string? line,
        string? signature,
        IReadOnlyList<string>? body,
        IReadOnlyList<string>? lines)
    {
        Kind = kind;
        Line = line;
        Signature = signature;
        Body = body;
        Lines = lines;
    }

    public MethodItemKind Kind { get; }

    public string? Line { get; }

    public string? Signature { get; }

    public IReadOnlyList<string>? Body { get; }

    public IReadOnlyList<string>? Lines { get; }

    public static MethodItemSpec ForLine(string line)
    {
        return new MethodItemSpec(MethodItemKind.Line, line, null, null, null);
    }

    public static MethodItemSpec ForSignature(string signature, IReadOnlyList<string> body)
    {
        return new MethodItemSpec(MethodItemKind.Signature, null, signature, body, null);
    }

    public static MethodItemSpec ForLines(IReadOnlyList<string> lines)
    {
        return new MethodItemSpec(MethodItemKind.Lines, null, null, null, lines);
    }
}

internal enum MethodItemKind
{
    Line,
    Signature,
    Lines,
}
