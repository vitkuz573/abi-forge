using System;
using System.Collections.Generic;
using System.Linq;
using System.Text;
using System.Text.Json;

namespace Abi.RoslynGenerator;

internal static class ManagedApiSourceEmitter
{
    private const int SupportedSchemaVersion = 2;
    private const string DefaultCallbacksHint = "ManagedApi.Callbacks.g.cs";
    private const string DefaultBuilderHint = "ManagedApi.Builder.g.cs";
    private const string DefaultHandleApiHint = "ManagedApi.HandleApi.g.cs";
    private const string DefaultPeerConnectionAsyncHint = "ManagedApi.PeerConnection.Async.g.cs";

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
            var outputHints = ParseOutputHints(root);

            return new ManagedApiModel(
                namespaceName,
                callbacks,
                builder,
                handleApiClasses,
                peerConnectionAsync,
                outputHints);
        }
    }

    public static IReadOnlyList<GeneratedSourceSpec> RenderSources(ManagedApiModel model)
    {
        return new[]
        {
            new GeneratedSourceSpec(
                model.OutputHints.ResolveHint("callbacks", DefaultCallbacksHint),
                RenderCallbacksCode(model)),
            new GeneratedSourceSpec(
                model.OutputHints.ResolveHint("builder", DefaultBuilderHint),
                RenderBuilderCode(model)),
            new GeneratedSourceSpec(
                model.OutputHints.ResolveHint("handle_api", DefaultHandleApiHint),
                RenderHandleApiCode(model)),
            new GeneratedSourceSpec(
                model.OutputHints.ResolveHint("peer_connection_async", DefaultPeerConnectionAsyncHint),
                RenderPeerConnectionAsyncCode(model)),
        };
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
        var builder = new StringBuilder();
        AppendFileHeader(builder, model.NamespaceName);

        builder.AppendLine($"public sealed partial class {model.Builder.ClassName}");
        builder.AppendLine("{");
        RenderMethodItems(builder, model.Builder.Methods, 4);
        builder.AppendLine("}");
        builder.AppendLine();

        return builder.ToString();
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
        var builder = new StringBuilder();
        AppendFileHeader(builder, model.NamespaceName);

        builder.AppendLine($"public sealed partial class {model.PeerConnectionAsync.ClassName}");
        builder.AppendLine("{");
        RenderMethodItems(builder, model.PeerConnectionAsync.Methods, 4);
        builder.AppendLine("}");
        builder.AppendLine();

        return builder.ToString();
    }

    private static ManagedApiOutputHints ParseOutputHints(JsonElement root)
    {
        var outputHints = ManagedApiOutputHints.Default();
        if (!root.TryGetProperty("output_hints", out var outputHintsElement) ||
            outputHintsElement.ValueKind == JsonValueKind.Null)
        {
            return outputHints;
        }

        if (outputHintsElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.output_hints must be an object when present.");
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
            callbacksElement.ValueKind != JsonValueKind.Array)
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

    private static BuilderSpec ParseBuilder(JsonElement root)
    {
        if (!root.TryGetProperty("builder", out var builderElement) ||
            builderElement.ValueKind != JsonValueKind.Object)
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
            handleApiElement.ValueKind != JsonValueKind.Array)
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

    private static PeerConnectionAsyncSpec ParsePeerConnectionAsync(JsonElement root)
    {
        if (!root.TryGetProperty("peer_connection_async", out var asyncElement) ||
            asyncElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.peer_connection_async must be an object.");
        }

        var className = ReadRequiredString(asyncElement, "class", "managed_api.peer_connection_async");
        var methods = ParseMethodItems(asyncElement, "methods", "managed_api.peer_connection_async");
        return new PeerConnectionAsyncSpec(className, methods);
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
        BuilderSpec builder,
        IReadOnlyList<HandleApiClassSpec> handleApiClasses,
        PeerConnectionAsyncSpec peerConnectionAsync,
        ManagedApiOutputHints outputHints)
    {
        NamespaceName = namespaceName;
        Callbacks = callbacks;
        Builder = builder;
        HandleApiClasses = handleApiClasses;
        PeerConnectionAsync = peerConnectionAsync;
        OutputHints = outputHints;
    }

    public string NamespaceName { get; }

    public IReadOnlyList<CallbackClassSpec> Callbacks { get; }

    public BuilderSpec Builder { get; }

    public IReadOnlyList<HandleApiClassSpec> HandleApiClasses { get; }

    public PeerConnectionAsyncSpec PeerConnectionAsync { get; }

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

    public string ResolveHint(string sectionName, string defaultHint)
    {
        var hasExplicit = _sectionHints.TryGetValue(sectionName, out var explicitTemplate);
        var template = hasExplicit ? explicitTemplate : Pattern;
        if (string.IsNullOrWhiteSpace(template))
        {
            template = DefaultPattern;
        }

        var rendered = template
            .Replace("{section}", sectionName)
            .Replace("{default}", defaultHint);

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
