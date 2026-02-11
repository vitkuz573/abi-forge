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
        "suffix",
        "sections",
    };

    private static readonly Dictionary<string, string> PrimitiveTypeMap = new(StringComparer.Ordinal)
    {
        ["void"] = "void",
        ["char"] = "sbyte",
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
        ["intptr_t"] = "nint",
        ["uintptr_t"] = "nuint",
        ["float"] = "float",
        ["double"] = "double",
        ["bool"] = "bool",
    };

    private static readonly HashSet<string> CSharpKeywords = new(StringComparer.Ordinal)
    {
        "abstract", "as", "base", "bool", "break", "byte", "case", "catch", "char", "checked",
        "class", "const", "continue", "decimal", "default", "delegate", "do", "double", "else",
        "enum", "event", "explicit", "extern", "false", "finally", "fixed", "float", "for",
        "foreach", "goto", "if", "implicit", "in", "int", "interface", "internal", "is", "lock",
        "long", "namespace", "new", "null", "object", "operator", "out", "override", "params",
        "private", "protected", "public", "readonly", "ref", "return", "sbyte", "sealed",
        "short", "sizeof", "stackalloc", "static", "string", "struct", "switch", "this", "throw",
        "true", "try", "typeof", "uint", "ulong", "unchecked", "unsafe", "ushort", "using",
        "virtual", "void", "volatile", "while",
    };

    private static readonly HashSet<string> PublicScalarTypeNames = new(StringComparer.Ordinal)
    {
        "void",
        "bool",
        "byte",
        "sbyte",
        "char",
        "short",
        "ushort",
        "int",
        "uint",
        "long",
        "ulong",
        "float",
        "double",
        "decimal",
        "string",
        "IntPtr",
        "UIntPtr",
        "nint",
        "nuint",
    };

    public static ManagedApiModel ParseManagedApiMetadata(
        string text,
        IdlModel idlModel,
        IdlTypeModel idlTypeModel,
        ManagedHandlesModel handlesModel)
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
            var autoAbiSurface = ParseAutoAbiSurface(root);
            var autoSources = BuildAutoAbiSurfaceSources(
                autoAbiSurface,
                idlModel,
                idlTypeModel,
                handlesModel,
                namespaceName);
            var outputHints = ParseOutputHints(root, "managed_api");

            return new ManagedApiModel(
                namespaceName,
                callbacks,
                builder,
                handleApiClasses,
                peerConnectionAsync,
                customSections,
                autoSources,
                outputHints);
        }
    }

    public static IReadOnlyList<GeneratedSourceSpec> RenderSources(ManagedApiModel model)
    {
        var renderedSections = new List<RenderedManagedSection>();

        foreach (var callbackClass in model.Callbacks)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: callbackClass.ClassName,
                defaultHint: BuildManagedSectionDefaultHint(callbackClass.ClassName),
                sourceText: RenderCallbackClassCode(model.NamespaceName, callbackClass)));
        }

        if (model.Builder != null)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: model.Builder.ClassName,
                defaultHint: BuildManagedSectionDefaultHint(model.Builder.ClassName),
                sourceText: RenderSingleClassSectionCode(
                    model.NamespaceName,
                    model.Builder.ClassName,
                    model.Builder.Methods)));
        }

        foreach (var classSpec in model.HandleApiClasses)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: classSpec.ClassName,
                defaultHint: BuildManagedSectionDefaultHint(classSpec.ClassName),
                sourceText: RenderSingleHandleApiClassCode(model.NamespaceName, classSpec)));
        }

        if (model.PeerConnectionAsync != null)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: model.PeerConnectionAsync.ClassName,
                defaultHint: BuildManagedSectionDefaultHint(model.PeerConnectionAsync.ClassName),
                sourceText: RenderSingleClassSectionCode(
                    model.NamespaceName,
                    model.PeerConnectionAsync.ClassName,
                    model.PeerConnectionAsync.Methods)));
        }

        foreach (var section in model.CustomSections)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: section.SectionName,
                defaultHint: section.DefaultHint,
                sourceText: RenderSingleClassSectionCode(model.NamespaceName, section.ClassName, section.Methods)));
        }

        foreach (var autoSource in model.AutoSources)
        {
            renderedSections.Add(new RenderedManagedSection(
                sectionName: autoSource.SectionName,
                defaultHint: autoSource.DefaultHint,
                sourceText: autoSource.SourceText));
        }

        var result = new List<GeneratedSourceSpec>(renderedSections.Count);
        foreach (var section in renderedSections)
        {
            result.Add(new GeneratedSourceSpec(
                model.OutputHints.ResolveHint(section.SectionName, section.DefaultHint, model.NamespaceName),
                section.SourceText));
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

    private static string RenderCallbackClassCode(string namespaceName, CallbackClassSpec callbackClass)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
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
        return builder.ToString();
    }

    private static string RenderSingleHandleApiClassCode(
        string namespaceName,
        HandleApiClassSpec classSpec)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
        builder.AppendLine($"public sealed partial class {classSpec.ClassName}");
        builder.AppendLine("{");
        RenderMethodItems(builder, classSpec.Members, 4);
        builder.AppendLine("}");
        builder.AppendLine();
        return builder.ToString();
    }

    private readonly struct RenderedManagedSection
    {
        public RenderedManagedSection(string sectionName, string defaultHint, string sourceText)
        {
            SectionName = sectionName;
            DefaultHint = defaultHint;
            SourceText = sourceText;
        }

        public string SectionName { get; }

        public string DefaultHint { get; }

        public string SourceText { get; }
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
        var suffix = ReadOptionalString(outputHintsElement, "suffix", outputHints.Suffix);
        ValidateOutputHintKeys(outputHintsElement, contextPrefix);

        CollectSectionHintsFromSectionsObject(outputHintsElement, sectionHints, contextPrefix);

        return new ManagedApiOutputHints(
            pattern,
            suffix,
            sectionHints);
    }

    private static void CollectSectionHintsFromSectionsObject(
        JsonElement outputHintsElement,
        Dictionary<string, string> sectionHints,
        string contextPrefix)
    {
        if (!outputHintsElement.TryGetProperty("sections", out var sectionsElement) ||
            sectionsElement.ValueKind == JsonValueKind.Null)
        {
            return;
        }

        if (sectionsElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException($"{contextPrefix}.output_hints.sections must be an object when present.");
        }

        foreach (var property in sectionsElement.EnumerateObject())
        {
            if (property.Value.ValueKind != JsonValueKind.String)
            {
                throw new GeneratorException(
                    $"{contextPrefix}.output_hints.sections.{property.Name} must be a string.");
            }

            sectionHints[property.Name] = property.Value.GetString() ?? string.Empty;
        }
    }

    private static void ValidateOutputHintKeys(JsonElement outputHintsElement, string contextPrefix)
    {
        foreach (var property in outputHintsElement.EnumerateObject())
        {
            if (OutputHintsReservedKeys.Contains(property.Name))
            {
                continue;
            }

            throw new GeneratorException(
                $"{contextPrefix}.output_hints.{property.Name} is not supported; use {contextPrefix}.output_hints.sections.{property.Name}.");
        }
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

    private static AutoAbiSurfaceSpec ParseAutoAbiSurface(JsonElement root)
    {
        if (!root.TryGetProperty("auto_abi_surface", out var autoElement) ||
            autoElement.ValueKind == JsonValueKind.Null)
        {
            return AutoAbiSurfaceSpec.Disabled();
        }

        if (autoElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.auto_abi_surface must be an object when present.");
        }

        var enabled = ReadOptionalBool(autoElement, "enabled", true);
        var methodPrefix = ReadOptionalString(autoElement, "method_prefix", "Abi");
        var sectionSuffix = ReadOptionalString(autoElement, "section_suffix", "_abi_surface");
        var globalSection = ReadOptionalString(autoElement, "global_section", "global");
        var globalClass = ReadOptionalString(autoElement, "global_class", "Global");
        var includeDeprecated = ReadOptionalBool(autoElement, "include_deprecated", false);
        var coverage = ParseAutoAbiCoverage(autoElement);
        var publicFacade = ParseAutoAbiPublicFacade(autoElement);

        return new AutoAbiSurfaceSpec(
            enabled,
            methodPrefix,
            sectionSuffix,
            globalSection,
            globalClass,
            includeDeprecated,
            publicFacade,
            coverage);
    }

    private static AutoAbiPublicFacadeSpec ParseAutoAbiPublicFacade(JsonElement autoElement)
    {
        if (!autoElement.TryGetProperty("public_facade", out var publicFacadeElement) ||
            publicFacadeElement.ValueKind == JsonValueKind.Null)
        {
            return AutoAbiPublicFacadeSpec.Disabled();
        }

        if (publicFacadeElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.auto_abi_surface.public_facade must be an object when present.");
        }

        var enabled = ReadOptionalBool(publicFacadeElement, "enabled", false);
        var classSuffix = ReadOptionalString(publicFacadeElement, "class_suffix", "_abi_facade");
        var methodPrefix = ReadOptionalString(publicFacadeElement, "method_prefix", "Raw");
        var typedMethodPrefix = ReadOptionalString(publicFacadeElement, "typed_method_prefix", "Typed");
        var sectionSuffix = ReadOptionalString(publicFacadeElement, "section_suffix", "_abi_facade");
        var allowIntPtr = ReadOptionalBool(publicFacadeElement, "allow_int_ptr", false);
        var safeFacade = ParseAutoAbiSafeFacade(publicFacadeElement);
        return new AutoAbiPublicFacadeSpec(
            enabled,
            classSuffix,
            methodPrefix,
            typedMethodPrefix,
            sectionSuffix,
            allowIntPtr,
            safeFacade);
    }

    private static AutoAbiSafeFacadeSpec ParseAutoAbiSafeFacade(JsonElement publicFacadeElement)
    {
        if (!publicFacadeElement.TryGetProperty("safe_facade", out var safeElement) ||
            safeElement.ValueKind == JsonValueKind.Null)
        {
            return AutoAbiSafeFacadeSpec.Default();
        }

        if (safeElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.auto_abi_surface.public_facade.safe_facade must be an object when present.");
        }

        var enabled = ReadOptionalBool(safeElement, "enabled", true);
        var classSuffix = ReadOptionalString(safeElement, "class_suffix", "_abi_safe");
        var methodPrefix = ReadOptionalString(safeElement, "method_prefix", string.Empty);
        var tryMethodPrefix = ReadOptionalString(safeElement, "try_method_prefix", "Try");
        var asyncMethodSuffix = ReadOptionalString(safeElement, "async_method_suffix", "Async");
        var sectionSuffix = ReadOptionalString(safeElement, "section_suffix", "_abi_safe");
        var exceptionType = ReadOptionalString(
            safeElement,
            "exception_type",
            "global::System.InvalidOperationException");
        return new AutoAbiSafeFacadeSpec(
            enabled,
            classSuffix,
            methodPrefix,
            tryMethodPrefix,
            asyncMethodSuffix,
            sectionSuffix,
            exceptionType);
    }

    private static AutoAbiCoverageSpec ParseAutoAbiCoverage(JsonElement autoElement)
    {
        if (!autoElement.TryGetProperty("coverage", out var coverageElement) ||
            coverageElement.ValueKind == JsonValueKind.Null)
        {
            return AutoAbiCoverageSpec.Default();
        }

        if (coverageElement.ValueKind != JsonValueKind.Object)
        {
            throw new GeneratorException("managed_api.auto_abi_surface.coverage must be an object when present.");
        }

        var strict = ReadOptionalBool(coverageElement, "strict", true);
        var waivers = new Dictionary<string, string>(StringComparer.Ordinal);
        if (coverageElement.TryGetProperty("waived_functions", out var waivedFunctionsElement) &&
            waivedFunctionsElement.ValueKind != JsonValueKind.Null)
        {
            if (waivedFunctionsElement.ValueKind != JsonValueKind.Array)
            {
                throw new GeneratorException("managed_api.auto_abi_surface.coverage.waived_functions must be an array when present.");
            }

            var index = 0;
            foreach (var item in waivedFunctionsElement.EnumerateArray())
            {
                var context = $"managed_api.auto_abi_surface.coverage.waived_functions[{index}]";
                if (item.ValueKind == JsonValueKind.String)
                {
                    var functionName = item.GetString();
                    if (string.IsNullOrWhiteSpace(functionName))
                    {
                        throw new GeneratorException(context + " must be a non-empty string.");
                    }

                    waivers[functionName!] = "waived";
                }
                else if (item.ValueKind == JsonValueKind.Object)
                {
                    var functionName = ReadRequiredString(item, "name", context);
                    var reason = ReadOptionalString(item, "reason", "waived");
                    waivers[functionName] = reason;
                }
                else
                {
                    throw new GeneratorException(context + " must be a string or object.");
                }

                index++;
            }
        }

        return new AutoAbiCoverageSpec(strict, waivers);
    }

    private static IReadOnlyList<AutoManagedSourceSpec> BuildAutoAbiSurfaceSources(
        AutoAbiSurfaceSpec spec,
        IdlModel idlModel,
        IdlTypeModel idlTypeModel,
        ManagedHandlesModel handlesModel,
        string namespaceName)
    {
        if (!spec.Enabled)
        {
            return Array.Empty<AutoManagedSourceSpec>();
        }

        var sources = new List<AutoManagedSourceSpec>();
        var knownSections = new HashSet<string>(StringComparer.Ordinal);
        var handles = handlesModel.Handles
            .Where(item => string.Equals(item.NamespaceName, namespaceName, StringComparison.Ordinal))
            .OrderBy(item => item.CsType, StringComparer.Ordinal)
            .ToArray();
        var handleTypeByCTypeKey = BuildHandleTypeByCTypeKey(handles);
        var handleByCTypeKey = BuildHandleByCTypeKey(handles);
        var publicHandleTypeNames = new HashSet<string>(
            handles.Select(static item => item.CsType),
            StringComparer.Ordinal);
        var handleReleaseMethods = new HashSet<string>(
            handles
                .Where(static item => !string.IsNullOrWhiteSpace(item.ReleaseMethod))
                .Select(static item => item.ReleaseMethod),
            StringComparer.Ordinal);
        var ownerByKey = BuildHandleOwnerSpecs(handles);
        var globalOwner = new AutoAbiOwnerSpec(
            ownerKey: "__global__",
            sectionStem: spec.GlobalSection,
            classStem: spec.GlobalClass,
            ownerTypeName: null,
            handle: null);
        ownerByKey[globalOwner.OwnerKey] = globalOwner;
        var methodsByOwnerKey = new Dictionary<string, List<AutoAbiMethodSpec>>(StringComparer.Ordinal);
        var usedNamesByOwner = new Dictionary<string, HashSet<string>>(StringComparer.Ordinal);
        var classifiedFunctions = new HashSet<string>(StringComparer.Ordinal);
        var unclassifiedFunctions = new List<string>();
        var methodPrefix = string.IsNullOrWhiteSpace(spec.MethodPrefix) ? "Abi" : spec.MethodPrefix;

        foreach (var function in idlModel.Functions.OrderBy(item => item.Name, StringComparer.Ordinal))
        {
            if (!spec.IncludeDeprecated && function.Deprecated)
            {
                classifiedFunctions.Add(function.Name);
                continue;
            }

            if (handleReleaseMethods.Contains(function.Name))
            {
                classifiedFunctions.Add(function.Name);
                continue;
            }

            if (function.Parameters.Any(parameter => parameter.Variadic))
            {
                if (!spec.Coverage.IsWaived(function.Name))
                {
                    unclassifiedFunctions.Add(function.Name);
                }

                continue;
            }

            var owner = globalOwner;
            var ownerParameterOffset = 0;
            if (function.Parameters.Count > 0)
            {
                var firstParameterType = ParseCTypeInfo(function.Parameters[0].CType);
                var firstParameterKey = BuildCTypeKey(firstParameterType.BaseType, firstParameterType.PointerDepth);
                if (handleByCTypeKey.TryGetValue(firstParameterKey, out var matchedOwner))
                {
                    owner = matchedOwner;
                    ownerParameterOffset = 1;
                }
            }

            if (!usedNamesByOwner.TryGetValue(owner.OwnerKey, out var usedNames))
            {
                usedNames = new HashSet<string>(StringComparer.Ordinal);
                usedNamesByOwner[owner.OwnerKey] = usedNames;
            }

            var ownerStem = owner.Handle == null
                ? string.Empty
                : BuildHandleStem(ParseCTypeInfo(owner.Handle.CHandleType).BaseType);
            var method = BuildAbiForwardMethod(
                function,
                idlModel,
                idlTypeModel,
                handleTypeByCTypeKey,
                ownerStem,
                methodPrefix,
                ownerParameterOffset,
                usedNames);
            if (method == null)
            {
                if (!spec.Coverage.IsWaived(function.Name))
                {
                    unclassifiedFunctions.Add(function.Name);
                }

                continue;
            }

            if (!methodsByOwnerKey.TryGetValue(owner.OwnerKey, out var methods))
            {
                methods = new List<AutoAbiMethodSpec>();
                methodsByOwnerKey[owner.OwnerKey] = methods;
            }

            methods.Add(method);
            classifiedFunctions.Add(function.Name);
        }

        ValidateAutoAbiCoverage(spec, idlModel, classifiedFunctions, unclassifiedFunctions);

        foreach (var owner in ownerByKey.Values.OrderBy(item => item.SectionStem, StringComparer.Ordinal))
        {
            if (!methodsByOwnerKey.TryGetValue(owner.OwnerKey, out var methods) || methods.Count == 0)
            {
                continue;
            }

            var internalSectionName = owner.SectionStem + spec.SectionSuffix;
            var internalSurfaceClassName = BuildAutoSurfaceClassName(
                owner.ClassStem,
                spec.SectionSuffix,
                "AbiSurface");
            var internalSourceText = RenderAutoAbiSurfaceCode(
                namespaceName,
                internalSurfaceClassName,
                owner,
                methods);
            AddAutoSource(sources, knownSections, internalSectionName, internalSourceText);

            if (!spec.PublicFacade.Enabled)
            {
                continue;
            }

            var facadeMethods = BuildPublicFacadeMethods(
                spec,
                methods,
                publicHandleTypeNames,
                handleTypeByCTypeKey,
                owner.IsHandleOwner);
            if (facadeMethods.Count > 0)
            {
                var facadeSectionName = owner.SectionStem + spec.PublicFacade.SectionSuffix;
                var facadeClassName = BuildAutoSurfaceClassName(
                    owner.ClassStem,
                    spec.PublicFacade.ClassSuffix,
                    "AbiFacade");
                var facadeSourceText = RenderAutoAbiFacadeCode(
                    namespaceName,
                    facadeClassName,
                    internalSurfaceClassName,
                    owner,
                    facadeMethods);
                AddAutoSource(sources, knownSections, facadeSectionName, facadeSourceText);

                if (spec.PublicFacade.SafeFacade.Enabled)
                {
                    var safeSectionName = owner.SectionStem + spec.PublicFacade.SafeFacade.SectionSuffix;
                    var safeClassName = BuildAutoSurfaceClassName(
                        owner.ClassStem,
                        spec.PublicFacade.SafeFacade.ClassSuffix,
                        "AbiSafe");
                    var safeSourceText = RenderAutoAbiSafeFacadeCode(
                        namespaceName,
                        safeClassName,
                        facadeClassName,
                        owner,
                        methods,
                        facadeMethods,
                        spec.PublicFacade,
                        publicHandleTypeNames);
                    if (!string.IsNullOrWhiteSpace(safeSourceText))
                    {
                        AddAutoSource(sources, knownSections, safeSectionName, safeSourceText);
                    }
                }
            }
        }

        return sources;
    }

    private static void AddAutoSource(
        ICollection<AutoManagedSourceSpec> sources,
        ISet<string> knownSections,
        string sectionName,
        string sourceText)
    {
        if (!knownSections.Add(sectionName))
        {
            throw new GeneratorException(
                $"managed_api.auto_abi_surface produced duplicate section '{sectionName}'.");
        }

        var defaultHint = BuildManagedSectionDefaultHint(sectionName);
        sources.Add(new AutoManagedSourceSpec(sectionName, defaultHint, sourceText));
    }

    private static IReadOnlyDictionary<string, string> BuildHandleTypeByCTypeKey(
        IEnumerable<ManagedHandleSpec> handles)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var handle in handles)
        {
            if (string.IsNullOrWhiteSpace(handle.CHandleType))
            {
                continue;
            }

            var cTypeInfo = ParseCTypeInfo(handle.CHandleType);
            if (cTypeInfo.PointerDepth <= 0)
            {
                continue;
            }

            result[BuildCTypeKey(cTypeInfo.BaseType, cTypeInfo.PointerDepth)] = handle.CsType;
        }

        return result;
    }

    private static IReadOnlyDictionary<string, AutoAbiOwnerSpec> BuildHandleByCTypeKey(
        IEnumerable<ManagedHandleSpec> handles)
    {
        var result = new Dictionary<string, AutoAbiOwnerSpec>(StringComparer.Ordinal);
        foreach (var handle in handles)
        {
            if (string.IsNullOrWhiteSpace(handle.CHandleType))
            {
                continue;
            }

            var cTypeInfo = ParseCTypeInfo(handle.CHandleType);
            if (cTypeInfo.PointerDepth <= 0)
            {
                continue;
            }

            var key = BuildCTypeKey(cTypeInfo.BaseType, cTypeInfo.PointerDepth);
            result[key] = new AutoAbiOwnerSpec(
                ownerKey: handle.CsType,
                sectionStem: handle.CsType,
                classStem: handle.CsType,
                ownerTypeName: handle.CsType,
                handle: handle);
        }

        return result;
    }

    private static Dictionary<string, AutoAbiOwnerSpec> BuildHandleOwnerSpecs(IEnumerable<ManagedHandleSpec> handles)
    {
        var result = new Dictionary<string, AutoAbiOwnerSpec>(StringComparer.Ordinal);
        foreach (var handle in handles)
        {
            result[handle.CsType] = new AutoAbiOwnerSpec(
                ownerKey: handle.CsType,
                sectionStem: handle.CsType,
                classStem: handle.CsType,
                ownerTypeName: handle.CsType,
                handle: handle);
        }

        return result;
    }

    private static AutoAbiMethodSpec? BuildAbiForwardMethod(
        FunctionSpec function,
        IdlModel idlModel,
        IdlTypeModel idlTypeModel,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey,
        string ownerStem,
        string methodPrefix,
        int ownerParameterOffset,
        HashSet<string> usedNames)
    {
        var methodStem = DeriveFunctionStem(function.Name, ownerStem);
        var methodName = EnsureUniqueMethodName(
            BuildPascalIdentifier(methodPrefix + "_" + methodStem, "AbiCall"),
            usedNames);

        var parameters = new List<AutoAbiParameterSpec>();
        var invocationArguments = new List<string>();
        for (var index = ownerParameterOffset; index < function.Parameters.Count; index++)
        {
            var parameter = function.Parameters[index];
            var mapped = MapManagedParameter(function, parameter, idlModel);
            var parameterName = SanitizeParameterName(parameter.Name, "arg" + index);
            parameters.Add(new AutoAbiParameterSpec(parameterName, mapped.TypeName, mapped.Modifier, parameter.CType));
            invocationArguments.Add(BuildInvocationArgument(mapped.Modifier, parameterName));
        }

        var returnType = MapManagedType(function.CReturnType, idlModel);
        var asyncSpec = TryBuildAsyncSpec(
            function,
            idlModel,
            idlTypeModel,
            handleTypeByCTypeKey,
            ownerParameterOffset);
        return new AutoAbiMethodSpec(
            methodName,
            methodStem,
            returnType,
            function.CReturnType,
            parameters,
            invocationArguments,
            function.Name,
            IsStatusLikeFunction(function, ownerParameterOffset),
            asyncSpec);
    }

    private static AutoAbiAsyncSpec? TryBuildAsyncSpec(
        FunctionSpec function,
        IdlModel idlModel,
        IdlTypeModel idlTypeModel,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey,
        int ownerParameterOffset)
    {
        var callbackIndices = new List<int>();
        for (var i = ownerParameterOffset; i < function.Parameters.Count; i++)
        {
            var parameter = function.Parameters[i];
            var info = ParseCTypeInfo(parameter.CType);
            if (info.PointerDepth == 0 &&
                info.BaseType.EndsWith("_cb", StringComparison.Ordinal))
            {
                callbackIndices.Add(i);
            }
        }

        if (callbackIndices.Count < 2)
        {
            return null;
        }

        var successIndex = -1;
        foreach (var index in callbackIndices)
        {
            if (function.Parameters[index].Name.IndexOf("success", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                successIndex = index;
                break;
            }
        }

        if (successIndex < 0)
        {
            successIndex = callbackIndices[0];
        }

        var failureIndex = -1;
        foreach (var index in callbackIndices)
        {
            if (index == successIndex)
            {
                continue;
            }

            var parameterName = function.Parameters[index].Name;
            if (parameterName.IndexOf("failure", StringComparison.OrdinalIgnoreCase) >= 0 ||
                parameterName.IndexOf("error", StringComparison.OrdinalIgnoreCase) >= 0)
            {
                failureIndex = index;
                break;
            }
        }

        if (failureIndex < 0 || failureIndex == successIndex)
        {
            failureIndex = -1;
            foreach (var index in callbackIndices)
            {
                if (index != successIndex)
                {
                    failureIndex = index;
                    break;
                }
            }

            if (failureIndex < 0 || failureIndex == successIndex)
            {
                return null;
            }
        }

        var userDataIndex = -1;
        for (var index = ownerParameterOffset; index < function.Parameters.Count; index++)
        {
            if (index == successIndex || index == failureIndex)
            {
                continue;
            }

            var parameter = function.Parameters[index];
            var parameterType = ParseCTypeInfo(parameter.CType);
            if (parameterType.PointerDepth == 1 &&
                string.Equals(parameterType.BaseType, "void", StringComparison.Ordinal))
            {
                userDataIndex = index;
                if (parameter.Name.IndexOf("user_data", StringComparison.OrdinalIgnoreCase) >= 0)
                {
                    break;
                }
            }
        }

        if (userDataIndex < 0)
        {
            return null;
        }

        var successParameter = function.Parameters[successIndex];
        var failureParameter = function.Parameters[failureIndex];
        var successDelegateType = MapManagedType(successParameter.CType, idlModel);
        var failureDelegateType = MapManagedType(failureParameter.CType, idlModel);
        if (!idlTypeModel.CallbackTypedefs.TryGetValue(successDelegateType, out var successDelegateSpec))
        {
            return null;
        }

        if (!idlTypeModel.CallbackTypedefs.TryGetValue(failureDelegateType, out var failureDelegateSpec))
        {
            return null;
        }

        if (successDelegateSpec.Parameters.Count == 0 || failureDelegateSpec.Parameters.Count == 0)
        {
            return null;
        }

        var exposedParameters = new List<AutoAbiParameterSpec>();
        var invocationArgumentsByIndex = new Dictionary<int, string>();
        for (var index = ownerParameterOffset; index < function.Parameters.Count; index++)
        {
            if (index == successIndex || index == failureIndex || index == userDataIndex)
            {
                continue;
            }

            var parameter = function.Parameters[index];
            var mapped = MapManagedParameter(function, parameter, idlModel);
            var parameterName = SanitizeParameterName(parameter.Name, "arg" + index);
            exposedParameters.Add(new AutoAbiParameterSpec(parameterName, mapped.TypeName, mapped.Modifier, parameter.CType));
            invocationArgumentsByIndex[index] = BuildInvocationArgument(mapped.Modifier, parameterName);
        }

        var invocationArguments = new List<string>();
        for (var index = ownerParameterOffset; index < function.Parameters.Count; index++)
        {
            if (index == successIndex)
            {
                invocationArguments.Add("success");
                continue;
            }

            if (index == failureIndex)
            {
                invocationArguments.Add("failure");
                continue;
            }

            if (index == userDataIndex)
            {
                invocationArguments.Add("userData");
                continue;
            }

            if (!invocationArgumentsByIndex.TryGetValue(index, out var argument))
            {
                return null;
            }

            invocationArguments.Add(argument);
        }

        var successLambdaParameters = BuildCallbackLambdaParameterNames(successDelegateSpec, "successArg");
        var failureLambdaParameters = BuildCallbackLambdaParameterNames(failureDelegateSpec, "failureArg");
        if (successLambdaParameters.Count != successDelegateSpec.Parameters.Count ||
            failureLambdaParameters.Count != failureDelegateSpec.Parameters.Count)
        {
            return null;
        }

        var successValues = new List<AutoAbiConvertedValueSpec>();
        for (var index = 1; index < successDelegateSpec.Parameters.Count; index++)
        {
            var callbackParameter = successDelegateSpec.Parameters[index];
            var callbackParameterName = successLambdaParameters[index];
            successValues.Add(ConvertCallbackValue(callbackParameter.CType, callbackParameterName, idlModel, handleTypeByCTypeKey));
        }

        var publicReturnType = "Task";
        var taskResultType = "bool";
        var successSetResultExpression = "true";
        if (successValues.Count == 1)
        {
            publicReturnType = $"Task<{successValues[0].TypeName}>";
            taskResultType = successValues[0].TypeName;
            successSetResultExpression = successValues[0].Expression;
        }
        else if (successValues.Count > 1)
        {
            var tupleType = "(" + string.Join(", ", successValues.Select(item => item.TypeName)) + ")";
            var tupleExpression = "(" + string.Join(", ", successValues.Select(item => item.Expression)) + ")";
            publicReturnType = $"Task<{tupleType}>";
            taskResultType = tupleType;
            successSetResultExpression = tupleExpression;
        }

        var failureMessageExpression = "\"Native callback reported failure.\"";
        if (failureDelegateSpec.Parameters.Count > 1)
        {
            var converted = ConvertCallbackValue(
                failureDelegateSpec.Parameters[1].CType,
                failureLambdaParameters[1],
                idlModel,
                handleTypeByCTypeKey);
            if (string.Equals(converted.TypeName, "string", StringComparison.Ordinal))
            {
                failureMessageExpression = converted.Expression;
            }
            else
            {
                failureMessageExpression =
                    $"global::System.Convert.ToString({converted.Expression}) ?? \"Native callback reported failure.\"";
            }
        }

        return new AutoAbiAsyncSpec(
            exposedParameters,
            invocationArguments,
            publicReturnType,
            taskResultType,
            successDelegateType,
            failureDelegateType,
            successLambdaParameters,
            failureLambdaParameters,
            successLambdaParameters[0],
            failureLambdaParameters[0],
            successSetResultExpression,
            failureMessageExpression);
    }

    private static IReadOnlyList<string> BuildCallbackLambdaParameterNames(DelegateSpec delegateSpec, string fallbackPrefix)
    {
        var result = new List<string>(delegateSpec.Parameters.Count);
        var usedNames = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < delegateSpec.Parameters.Count; index++)
        {
            var parameter = delegateSpec.Parameters[index];
            var fallback = fallbackPrefix + index.ToString();
            var name = SanitizeParameterName(parameter.Name, fallback);
            name = EnsureUniqueMethodName(name, usedNames);
            result.Add(name);
        }

        return result;
    }

    private static AutoAbiConvertedValueSpec ConvertCallbackValue(
        string cType,
        string parameterName,
        IdlModel idlModel,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey)
    {
        var handleType = ResolveHandleTypeByCType(cType, handleTypeByCTypeKey);
        if (!string.IsNullOrWhiteSpace(handleType))
        {
            return new AutoAbiConvertedValueSpec(
                handleType + "?",
                $"{parameterName} == IntPtr.Zero ? null : new {handleType}({parameterName})");
        }

        if (IsCStringPointer(cType))
        {
            return new AutoAbiConvertedValueSpec("string", $"Utf8String.Read({parameterName})");
        }

        var mappedType = MapManagedType(cType, idlModel);
        return new AutoAbiConvertedValueSpec(mappedType, parameterName);
    }

    private static bool IsCStringPointer(string cType)
    {
        var info = ParseCTypeInfo(cType);
        return info.PointerDepth == 1 &&
            string.Equals(info.BaseType, "char", StringComparison.Ordinal);
    }

    private static bool IsStatusLikeFunction(FunctionSpec function, int ownerParameterOffset)
    {
        var returnType = StripCTypeQualifiers(function.CReturnType);
        if (string.Equals(returnType, "lrtc_result_t", StringComparison.Ordinal))
        {
            return true;
        }

        if (!string.Equals(returnType, "int", StringComparison.Ordinal) &&
            !string.Equals(returnType, "int32_t", StringComparison.Ordinal))
        {
            return false;
        }

        var functionName = function.Name;
        if (functionName.Contains("_set_", StringComparison.Ordinal) ||
            functionName.Contains("_add_", StringComparison.Ordinal) ||
            functionName.Contains("_remove_", StringComparison.Ordinal) ||
            functionName.Contains("_replace_", StringComparison.Ordinal) ||
            functionName.Contains("_update", StringComparison.Ordinal) ||
            functionName.Contains("_initialize", StringComparison.Ordinal) ||
            functionName.Contains("_insert", StringComparison.Ordinal) ||
            functionName.Contains("_send", StringComparison.Ordinal) ||
            functionName.EndsWith("_stop", StringComparison.Ordinal) ||
            functionName.EndsWith("_copy_i420", StringComparison.Ordinal) ||
            functionName.EndsWith("_to_argb", StringComparison.Ordinal))
        {
            return true;
        }

        for (var index = ownerParameterOffset; index < function.Parameters.Count; index++)
        {
            var parameter = function.Parameters[index];
            var info = ParseCTypeInfo(parameter.CType);
            var rawParameterType = parameter.CType.TrimStart();
            var isConstPointer = rawParameterType.StartsWith("const ", StringComparison.Ordinal);
            if (info.PointerDepth == 1 &&
                !isConstPointer)
            {
                return true;
            }
        }

        return false;
    }

    private static void ValidateAutoAbiCoverage(
        AutoAbiSurfaceSpec spec,
        IdlModel idlModel,
        IReadOnlyCollection<string> classifiedFunctions,
        IReadOnlyCollection<string> unclassifiedFunctions)
    {
        var knownFunctions = new HashSet<string>(
            idlModel.Functions.Select(static function => function.Name),
            StringComparer.Ordinal);
        var unknownWaivers = spec.Coverage.WaivedFunctions.Keys
            .Where(name => !knownFunctions.Contains(name))
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (unknownWaivers.Length > 0)
        {
            throw new GeneratorException(
                "managed_api.auto_abi_surface.coverage.waived_functions contains unknown functions: " +
                string.Join(", ", unknownWaivers));
        }

        if (!spec.Coverage.Strict)
        {
            return;
        }

        var missing = new List<string>();
        foreach (var function in idlModel.Functions)
        {
            if (!spec.IncludeDeprecated && function.Deprecated)
            {
                continue;
            }

            if (classifiedFunctions.Contains(function.Name) || spec.Coverage.IsWaived(function.Name))
            {
                continue;
            }

            missing.Add(function.Name);
        }

        if (unclassifiedFunctions.Count > 0)
        {
            missing.AddRange(unclassifiedFunctions.Where(name => !spec.Coverage.IsWaived(name)));
        }

        var uniqueMissing = missing
            .Distinct(StringComparer.Ordinal)
            .OrderBy(name => name, StringComparer.Ordinal)
            .ToArray();
        if (uniqueMissing.Length == 0)
        {
            return;
        }

        throw new GeneratorException(
            "managed_api.auto_abi_surface coverage is incomplete. " +
            "Unclassified ABI functions: " + string.Join(", ", uniqueMissing));
    }

    private static IReadOnlyList<AutoAbiFacadeMethodSpec> BuildPublicFacadeMethods(
        AutoAbiSurfaceSpec spec,
        IReadOnlyList<AutoAbiMethodSpec> methods,
        HashSet<string> publicHandleTypeNames,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey,
        bool allowHandleReturnConversion)
    {
        var result = new List<AutoAbiFacadeMethodSpec>();
        var usedNames = new HashSet<string>(StringComparer.Ordinal);
        foreach (var method in methods)
        {
            if (IsRawPublicFacadeSignatureAllowed(method, spec.PublicFacade, publicHandleTypeNames))
            {
                var rawMethodName = DerivePublicFacadeMethodName(
                    method.MethodName,
                    spec.MethodPrefix,
                    spec.PublicFacade.MethodPrefix);
                rawMethodName = EnsureUniqueMethodName(rawMethodName, usedNames);

                result.Add(new AutoAbiFacadeMethodSpec(
                    publicMethodName: rawMethodName,
                    returnType: method.ReturnType,
                    parameters: method.Parameters,
                    forwardedArguments: method.Parameters
                        .Select(parameter => BuildInvocationArgument(parameter.Modifier, parameter.ParameterName))
                        .ToArray(),
                    innerMethodName: method.MethodName,
                    nativeFunctionName: method.NativeFunctionName,
                    handleReturnType: null,
                    methodStem: method.MethodStem,
                    isTyped: false,
                    isStatusLike: method.IsStatusLike));
            }

            if (!spec.PublicFacade.Enabled)
            {
                continue;
            }

            var typedMethod = BuildTypedPublicFacadeMethod(
                method,
                spec,
                publicHandleTypeNames,
                handleTypeByCTypeKey,
                allowHandleReturnConversion,
                usedNames);
            if (typedMethod != null)
            {
                result.Add(typedMethod);
            }
        }

        return result;
    }

    private static AutoAbiFacadeMethodSpec? BuildTypedPublicFacadeMethod(
        AutoAbiMethodSpec method,
        AutoAbiSurfaceSpec spec,
        HashSet<string> publicHandleTypeNames,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey,
        bool allowHandleReturnConversion,
        HashSet<string> usedNames)
    {
        var convertedParameters = new List<AutoAbiParameterSpec>(method.Parameters.Count);
        var forwardedArguments = new List<string>(method.Parameters.Count);
        var converted = false;

        foreach (var parameter in method.Parameters)
        {
            var typedHandleName = ResolveHandleTypeByCType(parameter.CType, handleTypeByCTypeKey);
            if (!string.IsNullOrWhiteSpace(typedHandleName) &&
                string.IsNullOrWhiteSpace(parameter.Modifier) &&
                IsPointerLikeType(parameter.TypeName))
            {
                convertedParameters.Add(new AutoAbiParameterSpec(
                    parameter.ParameterName,
                    typedHandleName + "?",
                    modifier: null,
                    cType: parameter.CType));
                forwardedArguments.Add(parameter.ParameterName + "?.DangerousGetHandle() ?? IntPtr.Zero");
                converted = true;
                continue;
            }

            convertedParameters.Add(parameter);
            forwardedArguments.Add(BuildInvocationArgument(parameter.Modifier, parameter.ParameterName));
        }

        string? handleReturnType = null;
        var typedReturnType = method.ReturnType;
        var typedReturnHandleName = ResolveHandleTypeByCType(method.ReturnCType, handleTypeByCTypeKey);
        if (allowHandleReturnConversion &&
            !string.IsNullOrWhiteSpace(typedReturnHandleName) &&
            IsPointerLikeType(method.ReturnType))
        {
            typedReturnType = typedReturnHandleName + "?";
            handleReturnType = typedReturnHandleName;
            converted = true;
        }

        if (!converted)
        {
            return null;
        }

        if (!IsPublicTypeAllowed(typedReturnType, spec.PublicFacade, publicHandleTypeNames))
        {
            return null;
        }

        foreach (var parameter in convertedParameters)
        {
            if (!IsPublicTypeAllowed(parameter.TypeName, spec.PublicFacade, publicHandleTypeNames))
            {
                return null;
            }
        }

        var typedMethodName = DerivePublicFacadeMethodName(
            method.MethodName,
            spec.MethodPrefix,
            spec.PublicFacade.TypedMethodPrefix);
        typedMethodName = EnsureUniqueMethodName(typedMethodName, usedNames);

        return new AutoAbiFacadeMethodSpec(
            publicMethodName: typedMethodName,
            returnType: typedReturnType,
            parameters: convertedParameters,
            forwardedArguments: forwardedArguments,
            innerMethodName: method.MethodName,
            nativeFunctionName: method.NativeFunctionName,
            handleReturnType: handleReturnType,
            methodStem: method.MethodStem,
            isTyped: true,
            isStatusLike: method.IsStatusLike);
    }

    private static bool IsRawPublicFacadeSignatureAllowed(
        AutoAbiMethodSpec method,
        AutoAbiPublicFacadeSpec facadeSpec,
        HashSet<string> publicHandleTypeNames)
    {
        if (!IsPublicTypeAllowed(method.ReturnType, facadeSpec, publicHandleTypeNames))
        {
            return false;
        }

        foreach (var parameter in method.Parameters)
        {
            if (!IsPublicTypeAllowed(parameter.TypeName, facadeSpec, publicHandleTypeNames))
            {
                return false;
            }
        }

        return true;
    }

    private static bool IsPointerLikeType(string typeName)
    {
        return string.Equals(typeName, "IntPtr", StringComparison.Ordinal) ||
            string.Equals(typeName, "UIntPtr", StringComparison.Ordinal) ||
            string.Equals(typeName, "nint", StringComparison.Ordinal) ||
            string.Equals(typeName, "nuint", StringComparison.Ordinal);
    }

    private static string? ResolveHandleTypeByCType(
        string cType,
        IReadOnlyDictionary<string, string> handleTypeByCTypeKey)
    {
        var cTypeInfo = ParseCTypeInfo(cType);
        if (cTypeInfo.PointerDepth <= 0)
        {
            return null;
        }

        var key = BuildCTypeKey(cTypeInfo.BaseType, cTypeInfo.PointerDepth);
        return handleTypeByCTypeKey.TryGetValue(key, out var handleTypeName) ? handleTypeName : null;
    }

    private static string BuildCTypeKey(string baseType, int pointerDepth)
    {
        return baseType + "#" + pointerDepth.ToString();
    }

    private static bool IsPublicTypeAllowed(
        string typeName,
        AutoAbiPublicFacadeSpec facadeSpec,
        HashSet<string> publicHandleTypeNames)
    {
        var normalized = typeName.Trim();
        if (normalized.StartsWith("global::", StringComparison.Ordinal))
        {
            normalized = normalized.Substring("global::".Length);
        }

        if (normalized.EndsWith("?", StringComparison.Ordinal))
        {
            normalized = normalized.Substring(0, normalized.Length - 1);
        }

        if (!facadeSpec.AllowIntPtr &&
            (string.Equals(normalized, "IntPtr", StringComparison.Ordinal) ||
             string.Equals(normalized, "UIntPtr", StringComparison.Ordinal) ||
             string.Equals(normalized, "nint", StringComparison.Ordinal) ||
             string.Equals(normalized, "nuint", StringComparison.Ordinal)))
        {
            return false;
        }

        if (PublicScalarTypeNames.Contains(normalized))
        {
            return true;
        }

        if (publicHandleTypeNames.Contains(normalized))
        {
            return true;
        }

        var lastDot = normalized.LastIndexOf('.');
        if (lastDot >= 0 && lastDot + 1 < normalized.Length)
        {
            var shortName = normalized.Substring(lastDot + 1);
            return publicHandleTypeNames.Contains(shortName);
        }

        return false;
    }

    private static bool IsPublicTaskTypeAllowed(
        string typeName,
        AutoAbiPublicFacadeSpec facadeSpec,
        HashSet<string> publicHandleTypeNames)
    {
        var normalized = typeName.Trim();
        if (string.Equals(normalized, "Task", StringComparison.Ordinal) ||
            string.Equals(normalized, "global::System.Threading.Tasks.Task", StringComparison.Ordinal))
        {
            return true;
        }

        var taskPrefix = "Task<";
        if (!normalized.StartsWith(taskPrefix, StringComparison.Ordinal) ||
            !normalized.EndsWith(">", StringComparison.Ordinal))
        {
            return false;
        }

        var inner = normalized.Substring(taskPrefix.Length, normalized.Length - taskPrefix.Length - 1).Trim();
        if (inner.StartsWith("(", StringComparison.Ordinal) &&
            inner.EndsWith(")", StringComparison.Ordinal))
        {
            var tupleInner = inner.Substring(1, inner.Length - 2);
            var parts = tupleInner.Split(',');
            foreach (var part in parts)
            {
                if (!IsPublicTypeAllowed(part.Trim(), facadeSpec, publicHandleTypeNames))
                {
                    return false;
                }
            }

            return true;
        }

        return IsPublicTypeAllowed(inner, facadeSpec, publicHandleTypeNames);
    }

    private static string DerivePublicFacadeMethodName(
        string internalMethodName,
        string internalPrefix,
        string facadePrefix)
    {
        var stem = internalMethodName;
        if (!string.IsNullOrWhiteSpace(internalPrefix) &&
            internalMethodName.StartsWith(internalPrefix, StringComparison.Ordinal) &&
            internalMethodName.Length > internalPrefix.Length)
        {
            stem = internalMethodName.Substring(internalPrefix.Length);
        }

        if (string.IsNullOrWhiteSpace(facadePrefix))
        {
            return BuildPascalIdentifier(stem, internalMethodName);
        }

        return BuildPascalIdentifier(facadePrefix + "_" + stem, internalMethodName);
    }

    private static string BuildAutoSurfaceClassName(string handleType, string sectionSuffix, string fallbackSuffix)
    {
        var sectionName = handleType + sectionSuffix;
        var className = BuildPascalIdentifier(sectionName, handleType + fallbackSuffix);
        if (string.Equals(className, handleType, StringComparison.Ordinal))
        {
            className += fallbackSuffix;
        }

        return className;
    }

    private static string RenderAutoAbiSurfaceCode(
        string namespaceName,
        string surfaceClassName,
        AutoAbiOwnerSpec owner,
        IReadOnlyList<AutoAbiMethodSpec> methods)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
        builder.AppendLine($"internal static class {surfaceClassName}");
        builder.AppendLine("{");

        foreach (var method in methods)
        {
            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// ABI forwarder for <c>{method.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            var allParameters = new List<string>();
            if (owner.IsHandleOwner)
            {
                allParameters.Add($"this {owner.OwnerTypeName} owner");
            }

            allParameters.AddRange(method.Parameters.Select(BuildParameterSignature));
            AppendLine(builder, 4, $"internal static {method.ReturnType} {method.MethodName}({string.Join(", ", allParameters)})");
            AppendLine(builder, 4, "{");
            if (owner.IsHandleOwner)
            {
                AppendLine(builder, 8, "if (owner is null)");
                AppendLine(builder, 8, "{");
                AppendLine(builder, 12, "throw new global::System.ArgumentNullException(nameof(owner));");
                AppendLine(builder, 8, "}");
            }

            var invocationArguments = new List<string>();
            if (owner.IsHandleOwner)
            {
                invocationArguments.Add("owner.DangerousGetHandle()");
            }

            invocationArguments.AddRange(method.InvocationArguments);
            if (string.Equals(method.ReturnType, "void", StringComparison.Ordinal))
            {
                AppendLine(builder, 8, $"NativeMethods.{method.NativeFunctionName}({string.Join(", ", invocationArguments)});");
            }
            else
            {
                AppendLine(builder, 8, $"var result = NativeMethods.{method.NativeFunctionName}({string.Join(", ", invocationArguments)});");
                AppendLine(builder, 8, "return result;");
            }
            AppendLine(builder, 4, "}");
            builder.AppendLine();
        }

        builder.AppendLine("}");
        builder.AppendLine();
        return builder.ToString();
    }

    private static string RenderAutoAbiFacadeCode(
        string namespaceName,
        string facadeClassName,
        string internalSurfaceClassName,
        AutoAbiOwnerSpec owner,
        IReadOnlyList<AutoAbiFacadeMethodSpec> methods)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
        builder.AppendLine($"public static class {facadeClassName}");
        builder.AppendLine("{");

        foreach (var method in methods)
        {
            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// Public facade over <c>{method.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            var allParameters = new List<string>();
            if (owner.IsHandleOwner)
            {
                allParameters.Add($"this {owner.OwnerTypeName} owner");
            }

            allParameters.AddRange(method.Parameters.Select(BuildParameterSignature));
            AppendLine(
                builder,
                4,
                $"public static {method.ReturnType} {method.PublicMethodName}({string.Join(", ", allParameters)})");
            AppendLine(builder, 4, "{");

            var invocationArguments = new List<string>();
            if (owner.IsHandleOwner)
            {
                invocationArguments.Add("owner");
            }

            invocationArguments.AddRange(method.ForwardedArguments);
            var invocation =
                $"{internalSurfaceClassName}.{method.InnerMethodName}({string.Join(", ", invocationArguments)})";

            if (string.Equals(method.ReturnType, "void", StringComparison.Ordinal))
            {
                AppendLine(builder, 8, invocation + ";");
            }
            else if (!string.IsNullOrWhiteSpace(method.HandleReturnType))
            {
                AppendLine(builder, 8, $"var raw = {invocation};");
                AppendLine(builder, 8, "if (raw == IntPtr.Zero)");
                AppendLine(builder, 8, "{");
                AppendLine(builder, 12, "return null;");
                AppendLine(builder, 8, "}");
                AppendLine(builder, 8, $"return new {method.HandleReturnType}(raw);");
            }
            else
            {
                AppendLine(builder, 8, $"return {invocation};");
            }

            AppendLine(builder, 4, "}");
            builder.AppendLine();
        }

        builder.AppendLine("}");
        builder.AppendLine();
        return builder.ToString();
    }

    private static string RenderAutoAbiSafeFacadeCode(
        string namespaceName,
        string safeFacadeClassName,
        string facadeClassName,
        AutoAbiOwnerSpec owner,
        IReadOnlyList<AutoAbiMethodSpec> methods,
        IReadOnlyList<AutoAbiFacadeMethodSpec> facadeMethods,
        AutoAbiPublicFacadeSpec facadeSpec,
        HashSet<string> publicHandleTypeNames)
    {
        var safeSpec = facadeSpec.SafeFacade;
        if (!safeSpec.Enabled)
        {
            return string.Empty;
        }

        var preferredFacadeMethods = facadeMethods
            .GroupBy(item => item.InnerMethodName, StringComparer.Ordinal)
            .Select(group => group.OrderByDescending(item => item.IsTyped).First())
            .ToArray();

        var statusMethods = preferredFacadeMethods
            .Where(static method => method.IsStatusLike)
            .Where(static method => !string.Equals(method.ReturnType, "void", StringComparison.Ordinal))
            .ToArray();
        var asyncMethods = methods
            .Where(static method => method.AsyncSpec != null)
            .Where(method => method.AsyncSpec != null &&
                method.AsyncSpec.Parameters.All(parameter =>
                    IsPublicTypeAllowed(parameter.TypeName, facadeSpec, publicHandleTypeNames)) &&
                IsPublicTaskTypeAllowed(method.AsyncSpec.PublicReturnType, facadeSpec, publicHandleTypeNames))
            .ToArray();

        if (statusMethods.Length == 0 && asyncMethods.Length == 0)
        {
            return string.Empty;
        }

        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
        builder.AppendLine($"public static class {safeFacadeClassName}");
        builder.AppendLine("{");

        var usedMethodNames = new HashSet<string>(StringComparer.Ordinal);
        foreach (var method in statusMethods)
        {
            var statusType = method.ReturnType;
            var tryMethodName = EnsureUniqueMethodName(
                BuildMethodNameFromStem(safeSpec.TryMethodPrefix, method.MethodStem, method.PublicMethodName),
                usedMethodNames);
            var safeMethodName = EnsureUniqueMethodName(
                BuildMethodNameFromStem(safeSpec.MethodPrefix, method.MethodStem, method.PublicMethodName),
                usedMethodNames);

            var tryParameters = new List<string>();
            if (owner.IsHandleOwner)
            {
                tryParameters.Add($"this {owner.OwnerTypeName} owner");
            }

            tryParameters.AddRange(method.Parameters.Select(BuildParameterSignature));
            tryParameters.Add($"out {statusType} statusCode");

            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// Try wrapper over <c>{method.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            AppendLine(builder, 4, $"public static bool {tryMethodName}({string.Join(", ", tryParameters)})");
            AppendLine(builder, 4, "{");
            var callArguments = BuildFacadeCallArguments(owner, method.Parameters);
            AppendLine(
                builder,
                8,
                $"statusCode = {facadeClassName}.{method.PublicMethodName}({string.Join(", ", callArguments)});");
            AppendLine(builder, 8, $"return {BuildStatusSuccessExpression(statusType, "statusCode")};");
            AppendLine(builder, 4, "}");
            builder.AppendLine();

            var safeParameters = new List<string>();
            if (owner.IsHandleOwner)
            {
                safeParameters.Add($"this {owner.OwnerTypeName} owner");
            }

            safeParameters.AddRange(method.Parameters.Select(BuildParameterSignature));
            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// Throwing wrapper over <c>{method.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            AppendLine(builder, 4, $"public static void {safeMethodName}({string.Join(", ", safeParameters)})");
            AppendLine(builder, 4, "{");
            AppendLine(
                builder,
                8,
                $"var status = {facadeClassName}.{method.PublicMethodName}({string.Join(", ", callArguments)});");
            AppendLine(builder, 8, $"if (!({BuildStatusSuccessExpression(statusType, "status")}))");
            AppendLine(builder, 8, "{");
            AppendLine(
                builder,
                12,
                $"throw new {safeSpec.ExceptionType}(\"{method.NativeFunctionName} failed with status \" + status.ToString() + \".\");");
            AppendLine(builder, 8, "}");
            AppendLine(builder, 4, "}");
            builder.AppendLine();
        }

        var hasAsyncMethods = false;
        foreach (var method in asyncMethods)
        {
            var asyncSpec = method.AsyncSpec!;
            var asyncBaseName = BuildMethodNameFromStem(safeSpec.MethodPrefix, method.MethodStem, method.MethodName);
            if (!string.IsNullOrWhiteSpace(safeSpec.AsyncMethodSuffix))
            {
                asyncBaseName += safeSpec.AsyncMethodSuffix;
            }

            var asyncMethodName = EnsureUniqueMethodName(asyncBaseName, usedMethodNames);
            var asyncParameters = new List<string>();
            if (owner.IsHandleOwner)
            {
                asyncParameters.Add($"this {owner.OwnerTypeName} owner");
            }

            asyncParameters.AddRange(asyncSpec.Parameters.Select(BuildParameterSignature));
            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// Task wrapper over callback-based <c>{method.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            AppendLine(
                builder,
                4,
                $"public static {asyncSpec.PublicReturnType} {asyncMethodName}({string.Join(", ", asyncParameters)})");
            AppendLine(builder, 4, "{");
            if (owner.IsHandleOwner)
            {
                AppendLine(builder, 8, "if (owner is null)");
                AppendLine(builder, 8, "{");
                AppendLine(builder, 12, "throw new global::System.ArgumentNullException(nameof(owner));");
                AppendLine(builder, 8, "}");
            }

            AppendLine(
                builder,
                8,
                $"var tcs = new global::System.Threading.Tasks.TaskCompletionSource<{asyncSpec.TaskResultType}>(global::System.Threading.Tasks.TaskCreationOptions.RunContinuationsAsynchronously);");
            AppendLine(builder, 8, $"var state = new AsyncInvocationState<{asyncSpec.TaskResultType}>(tcs);");
            AppendLine(builder, 8, "var userDataHandle = GCHandle.Alloc(state);");
            AppendLine(builder, 8, "state.AttachHandle(userDataHandle);");
            AppendLine(builder, 8, "var userData = GCHandle.ToIntPtr(userDataHandle);");
            builder.AppendLine();

            AppendLine(
                builder,
                8,
                $"{asyncSpec.SuccessDelegateType} success = ({string.Join(", ", asyncSpec.SuccessLambdaParameters)}) =>");
            AppendLine(builder, 8, "{");
            AppendLine(
                builder,
                12,
                $"var callbackState = AsyncInvocationState<{asyncSpec.TaskResultType}>.FromUserData({asyncSpec.SuccessUserDataParameter});");
            AppendLine(builder, 12, "if (callbackState is null || !callbackState.TryComplete())");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "return;");
            AppendLine(builder, 12, "}");
            AppendLine(builder, 12, $"callbackState.Tcs.TrySetResult({asyncSpec.SuccessSetResultExpression});");
            AppendLine(builder, 8, "};");
            builder.AppendLine();

            AppendLine(
                builder,
                8,
                $"{asyncSpec.FailureDelegateType} failure = ({string.Join(", ", asyncSpec.FailureLambdaParameters)}) =>");
            AppendLine(builder, 8, "{");
            AppendLine(
                builder,
                12,
                $"var callbackState = AsyncInvocationState<{asyncSpec.TaskResultType}>.FromUserData({asyncSpec.FailureUserDataParameter});");
            AppendLine(builder, 12, "if (callbackState is null || !callbackState.TryComplete())");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "return;");
            AppendLine(builder, 12, "}");
            AppendLine(builder, 12, $"var message = {asyncSpec.FailureMessageExpression};");
            AppendLine(
                builder,
                12,
                $"callbackState.Tcs.TrySetException(new {safeSpec.ExceptionType}(message));");
            AppendLine(builder, 8, "};");
            builder.AppendLine();

            AppendLine(builder, 8, "state.SetDelegates(success, failure);");
            AppendLine(builder, 8, "try");
            AppendLine(builder, 8, "{");
            var nativeArguments = new List<string>();
            if (owner.IsHandleOwner)
            {
                nativeArguments.Add("owner.DangerousGetHandle()");
            }

            nativeArguments.AddRange(asyncSpec.InvocationArguments);
            AppendLine(
                builder,
                12,
                $"NativeMethods.{method.NativeFunctionName}({string.Join(", ", nativeArguments)});");
            AppendLine(builder, 8, "}");
            AppendLine(builder, 8, "catch (global::System.Exception ex)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "if (state.TryComplete())");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "state.Tcs.TrySetException(ex);");
            AppendLine(builder, 12, "}");
            AppendLine(builder, 8, "}");
            AppendLine(builder, 8, "return state.Tcs.Task;");
            AppendLine(builder, 4, "}");
            builder.AppendLine();
            hasAsyncMethods = true;
        }

        if (hasAsyncMethods)
        {
            AppendLine(builder, 4, "private sealed class AsyncInvocationState<TResult>");
            AppendLine(builder, 4, "{");
            AppendLine(builder, 8, "private GCHandle _selfHandle;");
            AppendLine(builder, 8, "private int _completed;");
            AppendLine(builder, 8, "private Delegate? _success;");
            AppendLine(builder, 8, "private Delegate? _failure;");
            builder.AppendLine();
            AppendLine(builder, 8, "public AsyncInvocationState(global::System.Threading.Tasks.TaskCompletionSource<TResult> tcs)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "Tcs = tcs;");
            AppendLine(builder, 8, "}");
            builder.AppendLine();
            AppendLine(builder, 8, "public global::System.Threading.Tasks.TaskCompletionSource<TResult> Tcs { get; }");
            builder.AppendLine();
            AppendLine(builder, 8, "public void AttachHandle(GCHandle handle)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "_selfHandle = handle;");
            AppendLine(builder, 8, "}");
            builder.AppendLine();
            AppendLine(builder, 8, "public void SetDelegates(Delegate success, Delegate failure)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "_success = success;");
            AppendLine(builder, 12, "_failure = failure;");
            AppendLine(builder, 8, "}");
            builder.AppendLine();
            AppendLine(builder, 8, "public bool TryComplete()");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "if (global::System.Threading.Interlocked.Exchange(ref _completed, 1) != 0)");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "return false;");
            AppendLine(builder, 12, "}");
            builder.AppendLine();
            AppendLine(builder, 12, "_success = null;");
            AppendLine(builder, 12, "_failure = null;");
            AppendLine(builder, 12, "if (_selfHandle.IsAllocated)");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "_selfHandle.Free();");
            AppendLine(builder, 12, "}");
            builder.AppendLine();
            AppendLine(builder, 12, "return true;");
            AppendLine(builder, 8, "}");
            builder.AppendLine();
            AppendLine(builder, 8, "public static AsyncInvocationState<TResult>? FromUserData(IntPtr userData)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "if (userData == IntPtr.Zero)");
            AppendLine(builder, 12, "{");
            AppendLine(builder, 16, "return null;");
            AppendLine(builder, 12, "}");
            builder.AppendLine();
            AppendLine(builder, 12, "var handle = GCHandle.FromIntPtr(userData);");
            AppendLine(builder, 12, "return handle.Target as AsyncInvocationState<TResult>;");
            AppendLine(builder, 8, "}");
            AppendLine(builder, 4, "}");
        }

        builder.AppendLine("}");
        builder.AppendLine();
        return builder.ToString();
    }

    private static string BuildMethodNameFromStem(string prefix, string stem, string fallback)
    {
        if (string.IsNullOrWhiteSpace(prefix))
        {
            return BuildPascalIdentifier(stem, fallback);
        }

        return BuildPascalIdentifier(prefix + "_" + stem, fallback);
    }

    private static IReadOnlyList<string> BuildFacadeCallArguments(
        AutoAbiOwnerSpec owner,
        IReadOnlyList<AutoAbiParameterSpec> parameters)
    {
        var args = new List<string>();
        if (owner.IsHandleOwner)
        {
            args.Add("owner");
        }

        args.AddRange(parameters.Select(parameter =>
            BuildInvocationArgument(parameter.Modifier, parameter.ParameterName)));
        return args;
    }

    private static string BuildStatusSuccessExpression(string returnType, string statusVariable)
    {
        var normalized = returnType.Trim();
        if (normalized.EndsWith("?", StringComparison.Ordinal))
        {
            normalized = normalized.Substring(0, normalized.Length - 1);
        }

        if (string.Equals(normalized, "LrtcResult", StringComparison.Ordinal) ||
            normalized.EndsWith(".LrtcResult", StringComparison.Ordinal))
        {
            return statusVariable + " == " + normalized + ".Ok";
        }

        return statusVariable + " == 0";
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

    private static string BuildParameterSignature(ParameterRenderSpec spec, string parameterName)
    {
        var modifier = string.IsNullOrWhiteSpace(spec.Modifier) ? string.Empty : spec.Modifier + " ";
        return modifier + spec.TypeName + " " + parameterName;
    }

    private static string BuildParameterSignature(AutoAbiParameterSpec parameter)
    {
        var modifier = string.IsNullOrWhiteSpace(parameter.Modifier) ? string.Empty : parameter.Modifier + " ";
        return modifier + parameter.TypeName + " " + parameter.ParameterName;
    }

    private static string BuildInvocationArgument(string? modifier, string parameterName)
    {
        if (string.IsNullOrWhiteSpace(modifier))
        {
            return parameterName;
        }

        return modifier + " " + parameterName;
    }

    private static ParameterRenderSpec MapManagedParameter(
        FunctionSpec function,
        ParameterSpec parameter,
        IdlModel model)
    {
        var baseline = MapManagedParameterBaseline(parameter, model);
        var key = BuildFunctionParameterOverrideKey(function.Name, parameter.Name);
        if (!model.FunctionParameterOverrides.TryGetValue(key, out var overrideSpec))
        {
            return baseline;
        }

        var hasManagedTypeOverride = !string.IsNullOrWhiteSpace(overrideSpec.ManagedType);
        var typeName = string.IsNullOrWhiteSpace(overrideSpec.ManagedType)
            ? baseline.TypeName
            : overrideSpec.ManagedType!;
        var modifier = string.IsNullOrWhiteSpace(overrideSpec.Modifier)
            ? (hasManagedTypeOverride ? null : baseline.Modifier)
            : overrideSpec.Modifier;
        var marshalAsI1 = overrideSpec.MarshalAsI1
            ?? (string.IsNullOrWhiteSpace(overrideSpec.ManagedType)
                ? baseline.MarshalAsI1
                : string.Equals(typeName, "bool", StringComparison.Ordinal));

        return new ParameterRenderSpec(typeName, modifier, marshalAsI1);
    }

    private static ParameterRenderSpec MapManagedParameterBaseline(ParameterSpec parameter, IdlModel model)
    {
        var info = ParseCTypeInfo(parameter.CType);
        if (info.PointerDepth == 0)
        {
            var scalarType = MapManagedBaseType(info.BaseType, model);
            return new ParameterRenderSpec(
                scalarType,
                modifier: null,
                marshalAsI1: string.Equals(scalarType, "bool", StringComparison.Ordinal));
        }

        if (info.PointerDepth > 1)
        {
            return new ParameterRenderSpec("IntPtr", modifier: null, marshalAsI1: false);
        }

        if (model.StructNames.Contains(info.BaseType))
        {
            var structType = MapManagedBaseType(info.BaseType, model);
            return new ParameterRenderSpec(structType, modifier: "ref", marshalAsI1: false);
        }

        return new ParameterRenderSpec("IntPtr", modifier: null, marshalAsI1: false);
    }

    private static string MapManagedType(string cType, IdlModel model)
    {
        if (cType == "...")
        {
            return "IntPtr";
        }

        var info = ParseCTypeInfo(cType);
        if (info.PointerDepth > 0)
        {
            return "IntPtr";
        }

        return MapManagedBaseType(info.BaseType, model);
    }

    private static string MapManagedBaseType(string cTypeBase, IdlModel model)
    {
        var stripped = StripCTypeQualifiers(cTypeBase);

        if (PrimitiveTypeMap.TryGetValue(stripped, out var primitive))
        {
            return primitive;
        }

        if (model.EnumNames.Contains(stripped) || model.StructNames.Contains(stripped))
        {
            return BuildPascalIdentifier(stripped, "IntPtr", stripTypedefSuffix: true);
        }

        if (stripped.EndsWith("_t", StringComparison.Ordinal))
        {
            return BuildPascalIdentifier(stripped, "IntPtr", stripTypedefSuffix: true);
        }

        if (stripped.EndsWith("_cb", StringComparison.Ordinal))
        {
            return BuildPascalIdentifier(stripped, "IntPtr", stripTypedefSuffix: false);
        }

        return BuildPascalIdentifier(stripped, "IntPtr", stripTypedefSuffix: false);
    }

    private static CTypeInfo ParseCTypeInfo(string cType)
    {
        var stripped = StripCTypeQualifiers(cType);
        var pointerDepth = stripped.Count(ch => ch == '*');
        var baseType = stripped.Replace("*", string.Empty).Trim();
        return new CTypeInfo(baseType, pointerDepth);
    }

    private static string StripCTypeQualifiers(string value)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return string.Empty;
        }

        var text = value.Trim()
            .Replace("const", string.Empty)
            .Replace("volatile", string.Empty)
            .Replace("restrict", string.Empty)
            .Replace("struct ", string.Empty)
            .Replace("enum ", string.Empty)
            .Replace("\t", " ")
            .Replace("\r", " ")
            .Replace("\n", " ");
        while (text.Contains("  ", StringComparison.Ordinal))
        {
            text = text.Replace("  ", " ");
        }

        text = text.Replace(" *", "*")
            .Replace("* ", "*")
            .Trim();
        return text;
    }

    private static bool CTypeMatchesHandle(string parameterType, CTypeInfo handleType)
    {
        var parameterInfo = ParseCTypeInfo(parameterType);
        if (parameterInfo.PointerDepth != handleType.PointerDepth)
        {
            return false;
        }

        return string.Equals(parameterInfo.BaseType, handleType.BaseType, StringComparison.Ordinal);
    }

    private static string BuildHandleStem(string handleBaseType)
    {
        var stem = handleBaseType;
        if (stem.StartsWith("lrtc_", StringComparison.Ordinal))
        {
            stem = stem.Substring("lrtc_".Length);
        }

        if (stem.EndsWith("_t", StringComparison.Ordinal))
        {
            stem = stem.Substring(0, stem.Length - 2);
        }

        return stem;
    }

    private static string DeriveFunctionStem(string functionName, string handleStem)
    {
        var handlePrefix = "lrtc_" + handleStem + "_";
        if (functionName.StartsWith(handlePrefix, StringComparison.Ordinal) &&
            functionName.Length > handlePrefix.Length)
        {
            return functionName.Substring(handlePrefix.Length);
        }

        if (functionName.StartsWith("lrtc_", StringComparison.Ordinal) &&
            functionName.Length > "lrtc_".Length)
        {
            return functionName.Substring("lrtc_".Length);
        }

        return functionName;
    }

    private static string EnsureUniqueMethodName(string candidate, HashSet<string> usedNames)
    {
        if (usedNames.Add(candidate))
        {
            return candidate;
        }

        var suffix = 2;
        while (!usedNames.Add(candidate + suffix.ToString()))
        {
            suffix++;
        }

        return candidate + suffix.ToString();
    }

    private static string BuildPascalIdentifier(
        string value,
        string fallback,
        bool stripTypedefSuffix = false)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return fallback;
        }

        var candidate = value.Trim();
        if (stripTypedefSuffix && candidate.EndsWith("_t", StringComparison.Ordinal))
        {
            candidate = candidate.Substring(0, candidate.Length - 2);
        }

        var tokens = candidate
            .Split(new[] { '_', '-', '.', '/', ' ' }, StringSplitOptions.RemoveEmptyEntries);
        var builder = new StringBuilder();
        foreach (var token in tokens)
        {
            var alnum = new string(token.Where(char.IsLetterOrDigit).ToArray());
            if (alnum.Length == 0)
            {
                continue;
            }

            builder.Append(char.ToUpperInvariant(alnum[0]));
            if (alnum.Length > 1)
            {
                builder.Append(alnum.Substring(1));
            }
        }

        var result = builder.Length == 0 ? fallback : builder.ToString();
        if (!char.IsLetter(result[0]) && result[0] != '_')
        {
            result = "_" + result;
        }

        return CSharpKeywords.Contains(result) ? "@" + result : result;
    }

    private static string SanitizeParameterName(string value, string fallback)
    {
        var candidate = string.IsNullOrWhiteSpace(value) ? fallback : value.Trim();
        var chars = candidate.ToCharArray();
        for (var i = 0; i < chars.Length; i++)
        {
            if (char.IsLetterOrDigit(chars[i]) || chars[i] == '_')
            {
                continue;
            }

            chars[i] = '_';
        }

        var sanitized = new string(chars);
        if (string.IsNullOrWhiteSpace(sanitized))
        {
            sanitized = fallback;
        }

        if (!char.IsLetter(sanitized[0]) && sanitized[0] != '_')
        {
            sanitized = "_" + sanitized;
        }

        return CSharpKeywords.Contains(sanitized) ? "@" + sanitized : sanitized;
    }

    private static string BuildFunctionParameterOverrideKey(string functionName, string parameterName)
    {
        return functionName + "::" + parameterName;
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
        IReadOnlyList<AutoManagedSourceSpec> autoSources,
        ManagedApiOutputHints outputHints)
    {
        NamespaceName = namespaceName;
        Callbacks = callbacks;
        Builder = builder;
        HandleApiClasses = handleApiClasses;
        PeerConnectionAsync = peerConnectionAsync;
        CustomSections = customSections;
        AutoSources = autoSources;
        OutputHints = outputHints;
    }

    public string NamespaceName { get; }

    public IReadOnlyList<CallbackClassSpec> Callbacks { get; }

    public BuilderSpec? Builder { get; }

    public IReadOnlyList<HandleApiClassSpec> HandleApiClasses { get; }

    public PeerConnectionAsyncSpec? PeerConnectionAsync { get; }

    public IReadOnlyList<CustomClassSectionSpec> CustomSections { get; }

    public IReadOnlyList<AutoManagedSourceSpec> AutoSources { get; }

    public ManagedApiOutputHints OutputHints { get; }
}

internal sealed class AutoManagedSourceSpec
{
    public AutoManagedSourceSpec(string sectionName, string defaultHint, string sourceText)
    {
        SectionName = sectionName;
        DefaultHint = defaultHint;
        SourceText = sourceText;
    }

    public string SectionName { get; }

    public string DefaultHint { get; }

    public string SourceText { get; }
}

internal sealed class AutoAbiSurfaceSpec
{
    public AutoAbiSurfaceSpec(
        bool enabled,
        string methodPrefix,
        string sectionSuffix,
        string globalSection,
        string globalClass,
        bool includeDeprecated,
        AutoAbiPublicFacadeSpec publicFacade,
        AutoAbiCoverageSpec coverage)
    {
        Enabled = enabled;
        MethodPrefix = string.IsNullOrWhiteSpace(methodPrefix) ? "Abi" : methodPrefix.Trim();
        SectionSuffix = string.IsNullOrWhiteSpace(sectionSuffix) ? "_abi_surface" : sectionSuffix.Trim();
        GlobalSection = string.IsNullOrWhiteSpace(globalSection) ? "global" : globalSection.Trim();
        GlobalClass = string.IsNullOrWhiteSpace(globalClass) ? "Global" : globalClass.Trim();
        IncludeDeprecated = includeDeprecated;
        PublicFacade = publicFacade;
        Coverage = coverage;
    }

    public bool Enabled { get; }

    public string MethodPrefix { get; }

    public string SectionSuffix { get; }

    public string GlobalSection { get; }

    public string GlobalClass { get; }

    public bool IncludeDeprecated { get; }

    public AutoAbiPublicFacadeSpec PublicFacade { get; }

    public AutoAbiCoverageSpec Coverage { get; }

    public static AutoAbiSurfaceSpec Disabled()
    {
        return new AutoAbiSurfaceSpec(
            enabled: false,
            methodPrefix: "Abi",
            sectionSuffix: "_abi_surface",
            globalSection: "global",
            globalClass: "Global",
            includeDeprecated: false,
            publicFacade: AutoAbiPublicFacadeSpec.Disabled(),
            coverage: AutoAbiCoverageSpec.Default());
    }
}

internal sealed class AutoAbiPublicFacadeSpec
{
    public AutoAbiPublicFacadeSpec(
        bool enabled,
        string classSuffix,
        string methodPrefix,
        string typedMethodPrefix,
        string sectionSuffix,
        bool allowIntPtr,
        AutoAbiSafeFacadeSpec safeFacade)
    {
        Enabled = enabled;
        ClassSuffix = string.IsNullOrWhiteSpace(classSuffix) ? "_abi_facade" : classSuffix.Trim();
        MethodPrefix = methodPrefix?.Trim() ?? string.Empty;
        TypedMethodPrefix = typedMethodPrefix?.Trim() ?? string.Empty;
        SectionSuffix = string.IsNullOrWhiteSpace(sectionSuffix) ? "_abi_facade" : sectionSuffix.Trim();
        AllowIntPtr = allowIntPtr;
        SafeFacade = safeFacade;
    }

    public bool Enabled { get; }

    public string ClassSuffix { get; }

    public string MethodPrefix { get; }

    public string TypedMethodPrefix { get; }

    public string SectionSuffix { get; }

    public bool AllowIntPtr { get; }

    public AutoAbiSafeFacadeSpec SafeFacade { get; }

    public static AutoAbiPublicFacadeSpec Disabled()
    {
        return new AutoAbiPublicFacadeSpec(
            enabled: false,
            classSuffix: "_abi_facade",
            methodPrefix: "Raw",
            typedMethodPrefix: "Typed",
            sectionSuffix: "_abi_facade",
            allowIntPtr: false,
            safeFacade: AutoAbiSafeFacadeSpec.Default());
    }
}

internal sealed class AutoAbiSafeFacadeSpec
{
    public AutoAbiSafeFacadeSpec(
        bool enabled,
        string classSuffix,
        string methodPrefix,
        string tryMethodPrefix,
        string asyncMethodSuffix,
        string sectionSuffix,
        string exceptionType)
    {
        Enabled = enabled;
        ClassSuffix = string.IsNullOrWhiteSpace(classSuffix) ? "_abi_safe" : classSuffix.Trim();
        MethodPrefix = methodPrefix?.Trim() ?? string.Empty;
        TryMethodPrefix = string.IsNullOrWhiteSpace(tryMethodPrefix) ? "Try" : tryMethodPrefix.Trim();
        AsyncMethodSuffix = string.IsNullOrWhiteSpace(asyncMethodSuffix) ? "Async" : asyncMethodSuffix.Trim();
        SectionSuffix = string.IsNullOrWhiteSpace(sectionSuffix) ? "_abi_safe" : sectionSuffix.Trim();
        ExceptionType = string.IsNullOrWhiteSpace(exceptionType)
            ? "global::System.InvalidOperationException"
            : exceptionType.Trim();
    }

    public bool Enabled { get; }

    public string ClassSuffix { get; }

    public string MethodPrefix { get; }

    public string TryMethodPrefix { get; }

    public string AsyncMethodSuffix { get; }

    public string SectionSuffix { get; }

    public string ExceptionType { get; }

    public static AutoAbiSafeFacadeSpec Default()
    {
        return new AutoAbiSafeFacadeSpec(
            enabled: true,
            classSuffix: "_abi_safe",
            methodPrefix: string.Empty,
            tryMethodPrefix: "Try",
            asyncMethodSuffix: "Async",
            sectionSuffix: "_abi_safe",
            exceptionType: "global::System.InvalidOperationException");
    }
}

internal sealed class AutoAbiCoverageSpec
{
    public AutoAbiCoverageSpec(bool strict, Dictionary<string, string> waivedFunctions)
    {
        Strict = strict;
        WaivedFunctions = waivedFunctions;
    }

    public bool Strict { get; }

    public Dictionary<string, string> WaivedFunctions { get; }

    public bool IsWaived(string functionName)
    {
        return WaivedFunctions.ContainsKey(functionName);
    }

    public static AutoAbiCoverageSpec Default()
    {
        return new AutoAbiCoverageSpec(
            strict: true,
            waivedFunctions: new Dictionary<string, string>(StringComparer.Ordinal));
    }
}

internal sealed class AutoAbiOwnerSpec
{
    public AutoAbiOwnerSpec(
        string ownerKey,
        string sectionStem,
        string classStem,
        string? ownerTypeName,
        ManagedHandleSpec? handle)
    {
        OwnerKey = ownerKey;
        SectionStem = sectionStem;
        ClassStem = classStem;
        OwnerTypeName = ownerTypeName;
        Handle = handle;
    }

    public string OwnerKey { get; }

    public string SectionStem { get; }

    public string ClassStem { get; }

    public string? OwnerTypeName { get; }

    public ManagedHandleSpec? Handle { get; }

    public bool IsHandleOwner => !string.IsNullOrWhiteSpace(OwnerTypeName);
}

internal sealed class AutoAbiConvertedValueSpec
{
    public AutoAbiConvertedValueSpec(string typeName, string expression)
    {
        TypeName = typeName;
        Expression = expression;
    }

    public string TypeName { get; }

    public string Expression { get; }
}

internal sealed class AutoAbiAsyncSpec
{
    public AutoAbiAsyncSpec(
        IReadOnlyList<AutoAbiParameterSpec> parameters,
        IReadOnlyList<string> invocationArguments,
        string publicReturnType,
        string taskResultType,
        string successDelegateType,
        string failureDelegateType,
        IReadOnlyList<string> successLambdaParameters,
        IReadOnlyList<string> failureLambdaParameters,
        string successUserDataParameter,
        string failureUserDataParameter,
        string successSetResultExpression,
        string failureMessageExpression)
    {
        Parameters = parameters;
        InvocationArguments = invocationArguments;
        PublicReturnType = publicReturnType;
        TaskResultType = taskResultType;
        SuccessDelegateType = successDelegateType;
        FailureDelegateType = failureDelegateType;
        SuccessLambdaParameters = successLambdaParameters;
        FailureLambdaParameters = failureLambdaParameters;
        SuccessUserDataParameter = successUserDataParameter;
        FailureUserDataParameter = failureUserDataParameter;
        SuccessSetResultExpression = successSetResultExpression;
        FailureMessageExpression = failureMessageExpression;
    }

    public IReadOnlyList<AutoAbiParameterSpec> Parameters { get; }

    public IReadOnlyList<string> InvocationArguments { get; }

    public string PublicReturnType { get; }

    public string TaskResultType { get; }

    public string SuccessDelegateType { get; }

    public string FailureDelegateType { get; }

    public IReadOnlyList<string> SuccessLambdaParameters { get; }

    public IReadOnlyList<string> FailureLambdaParameters { get; }

    public string SuccessUserDataParameter { get; }

    public string FailureUserDataParameter { get; }

    public string SuccessSetResultExpression { get; }

    public string FailureMessageExpression { get; }
}

internal sealed class AutoAbiMethodSpec
{
    public AutoAbiMethodSpec(
        string methodName,
        string methodStem,
        string returnType,
        string returnCType,
        IReadOnlyList<AutoAbiParameterSpec> parameters,
        IReadOnlyList<string> invocationArguments,
        string nativeFunctionName,
        bool isStatusLike,
        AutoAbiAsyncSpec? asyncSpec)
    {
        MethodName = methodName;
        MethodStem = methodStem;
        ReturnType = returnType;
        ReturnCType = returnCType;
        Parameters = parameters;
        InvocationArguments = invocationArguments;
        NativeFunctionName = nativeFunctionName;
        IsStatusLike = isStatusLike;
        AsyncSpec = asyncSpec;
    }

    public string MethodName { get; }

    public string MethodStem { get; }

    public string ReturnType { get; }

    public string ReturnCType { get; }

    public IReadOnlyList<AutoAbiParameterSpec> Parameters { get; }

    public IReadOnlyList<string> InvocationArguments { get; }

    public string NativeFunctionName { get; }

    public bool IsStatusLike { get; }

    public AutoAbiAsyncSpec? AsyncSpec { get; }
}

internal sealed class AutoAbiParameterSpec
{
    public AutoAbiParameterSpec(string parameterName, string typeName, string? modifier, string cType)
    {
        ParameterName = parameterName;
        TypeName = typeName;
        Modifier = modifier;
        CType = cType;
    }

    public string ParameterName { get; }

    public string TypeName { get; }

    public string? Modifier { get; }

    public string CType { get; }
}

internal sealed class AutoAbiFacadeMethodSpec
{
    public AutoAbiFacadeMethodSpec(
        string publicMethodName,
        string returnType,
        IReadOnlyList<AutoAbiParameterSpec> parameters,
        IReadOnlyList<string> forwardedArguments,
        string innerMethodName,
        string nativeFunctionName,
        string? handleReturnType,
        string methodStem,
        bool isTyped,
        bool isStatusLike)
    {
        PublicMethodName = publicMethodName;
        ReturnType = returnType;
        Parameters = parameters;
        ForwardedArguments = forwardedArguments;
        InnerMethodName = innerMethodName;
        NativeFunctionName = nativeFunctionName;
        HandleReturnType = handleReturnType;
        MethodStem = methodStem;
        IsTyped = isTyped;
        IsStatusLike = isStatusLike;
    }

    public string PublicMethodName { get; }

    public string ReturnType { get; }

    public IReadOnlyList<AutoAbiParameterSpec> Parameters { get; }

    public IReadOnlyList<string> ForwardedArguments { get; }

    public string InnerMethodName { get; }

    public string NativeFunctionName { get; }

    public string? HandleReturnType { get; }

    public string MethodStem { get; }

    public bool IsTyped { get; }

    public bool IsStatusLike { get; }
}

internal sealed class ManagedApiOutputHints
{
    private const string DefaultPattern = "{default}";
    private const string DefaultSuffix = ".g.cs";

    private readonly Dictionary<string, string> _sectionHints;

    public ManagedApiOutputHints(
        string pattern,
        string suffix,
        Dictionary<string, string> sectionHints)
    {
        Pattern = string.IsNullOrWhiteSpace(pattern) ? DefaultPattern : pattern.Trim();
        Suffix = string.IsNullOrWhiteSpace(suffix) ? DefaultSuffix : suffix.Trim();
        _sectionHints = sectionHints;
    }

    public string Pattern { get; }

    public string Suffix { get; }

    public static ManagedApiOutputHints Default()
    {
        return new ManagedApiOutputHints(
            pattern: DefaultPattern,
            suffix: DefaultSuffix,
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

        if (!candidate.EndsWith(".cs", StringComparison.OrdinalIgnoreCase))
        {
            candidate += Suffix;
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
