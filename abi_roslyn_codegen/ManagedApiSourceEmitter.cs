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
            var autoSources = BuildAutoAbiSurfaceSources(autoAbiSurface, idlModel, handlesModel, namespaceName);
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
        var includeDeprecated = ReadOptionalBool(autoElement, "include_deprecated", false);
        var publicFacade = ParseAutoAbiPublicFacade(autoElement);

        return new AutoAbiSurfaceSpec(enabled, methodPrefix, sectionSuffix, includeDeprecated, publicFacade);
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
        var sectionSuffix = ReadOptionalString(publicFacadeElement, "section_suffix", "_abi_facade");
        var allowIntPtr = ReadOptionalBool(publicFacadeElement, "allow_int_ptr", false);
        return new AutoAbiPublicFacadeSpec(enabled, classSuffix, methodPrefix, sectionSuffix, allowIntPtr);
    }

    private static IReadOnlyList<AutoManagedSourceSpec> BuildAutoAbiSurfaceSources(
        AutoAbiSurfaceSpec spec,
        IdlModel idlModel,
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
        var publicHandleTypeNames = new HashSet<string>(
            handles.Select(static item => item.CsType),
            StringComparer.Ordinal);

        foreach (var handle in handles)
        {
            var methods = BuildHandleAbiMethods(spec, idlModel, handle);
            if (methods.Count == 0)
            {
                continue;
            }

            var internalSectionName = handle.CsType + spec.SectionSuffix;
            var internalSurfaceClassName = BuildAutoSurfaceClassName(
                handle.CsType,
                spec.SectionSuffix,
                "AbiSurface");
            var internalSourceText = RenderAutoAbiSurfaceCode(
                namespaceName,
                internalSurfaceClassName,
                handle,
                methods);
            AddAutoSource(sources, knownSections, internalSectionName, internalSourceText);

            if (spec.PublicFacade.Enabled)
            {
                var facadeMethods = BuildPublicFacadeMethods(
                    spec,
                    methods,
                    publicHandleTypeNames);
                if (facadeMethods.Count > 0)
                {
                    var facadeSectionName = handle.CsType + spec.PublicFacade.SectionSuffix;
                    var facadeClassName = BuildAutoSurfaceClassName(
                        handle.CsType,
                        spec.PublicFacade.ClassSuffix,
                        "AbiFacade");
                    var facadeSourceText = RenderAutoAbiFacadeCode(
                        namespaceName,
                        facadeClassName,
                        internalSurfaceClassName,
                        handle,
                        facadeMethods);
                    AddAutoSource(sources, knownSections, facadeSectionName, facadeSourceText);
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

    private static IReadOnlyList<AutoAbiMethodSpec> BuildHandleAbiMethods(
        AutoAbiSurfaceSpec spec,
        IdlModel idlModel,
        ManagedHandleSpec handle)
    {
        if (string.IsNullOrWhiteSpace(handle.CHandleType))
        {
            return Array.Empty<AutoAbiMethodSpec>();
        }

        var methodPrefix = string.IsNullOrWhiteSpace(spec.MethodPrefix) ? "Abi" : spec.MethodPrefix;
        var usedNames = new HashSet<string>(StringComparer.Ordinal);
        var methods = new List<AutoAbiMethodSpec>();
        var handleType = ParseCTypeInfo(handle.CHandleType);
        var handleStem = BuildHandleStem(handleType.BaseType);

        foreach (var function in idlModel.Functions.OrderBy(item => item.Name, StringComparer.Ordinal))
        {
            if (!spec.IncludeDeprecated && function.Deprecated)
            {
                continue;
            }

            if (string.Equals(function.Name, handle.ReleaseMethod, StringComparison.Ordinal))
            {
                continue;
            }

            if (function.Parameters.Count == 0 || function.Parameters.Any(parameter => parameter.Variadic))
            {
                continue;
            }

            var firstParameter = function.Parameters[0];
            if (!CTypeMatchesHandle(firstParameter.CType, handleType))
            {
                continue;
            }

            var method = BuildHandleAbiForwardMethod(function, idlModel, handleStem, methodPrefix, usedNames);
            if (method != null)
            {
                methods.Add(method);
            }
        }

        return methods;
    }

    private static AutoAbiMethodSpec? BuildHandleAbiForwardMethod(
        FunctionSpec function,
        IdlModel idlModel,
        string handleStem,
        string methodPrefix,
        HashSet<string> usedNames)
    {
        var methodStem = DeriveFunctionStem(function.Name, handleStem);
        var methodName = EnsureUniqueMethodName(
            BuildPascalIdentifier(methodPrefix + "_" + methodStem, "AbiCall"),
            usedNames);

        var parameters = new List<AutoAbiParameterSpec>();
        var invocationArguments = new List<string> { "owner.DangerousGetHandle()" };

        for (var index = 1; index < function.Parameters.Count; index++)
        {
            var parameter = function.Parameters[index];
            var mapped = MapManagedParameter(function, parameter, idlModel);
            var parameterName = SanitizeParameterName(parameter.Name, "arg" + index);
            parameters.Add(new AutoAbiParameterSpec(parameterName, mapped.TypeName, mapped.Modifier));
            invocationArguments.Add(BuildInvocationArgument(mapped.Modifier, parameterName));
        }

        var returnType = MapManagedType(function.CReturnType, idlModel);
        return new AutoAbiMethodSpec(
            methodName,
            returnType,
            parameters,
            invocationArguments,
            function.Name);
    }

    private static IReadOnlyList<AutoAbiFacadeMethodSpec> BuildPublicFacadeMethods(
        AutoAbiSurfaceSpec spec,
        IReadOnlyList<AutoAbiMethodSpec> methods,
        HashSet<string> publicHandleTypeNames)
    {
        var result = new List<AutoAbiFacadeMethodSpec>();
        var usedNames = new HashSet<string>(StringComparer.Ordinal);
        foreach (var method in methods)
        {
            if (!IsPublicFacadeSignature(method, spec.PublicFacade, publicHandleTypeNames))
            {
                continue;
            }

            var publicMethodName = DerivePublicFacadeMethodName(
                method.MethodName,
                spec.MethodPrefix,
                spec.PublicFacade.MethodPrefix);
            publicMethodName = EnsureUniqueMethodName(publicMethodName, usedNames);
            result.Add(new AutoAbiFacadeMethodSpec(publicMethodName, method));
        }

        return result;
    }

    private static bool IsPublicFacadeSignature(
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
        ManagedHandleSpec handle,
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
            var allParameters = new List<string> { $"this {handle.CsType} owner" };
            allParameters.AddRange(method.Parameters.Select(BuildParameterSignature));
            AppendLine(builder, 4, $"internal static {method.ReturnType} {method.MethodName}({string.Join(", ", allParameters)})");
            AppendLine(builder, 4, "{");
            AppendLine(builder, 8, "if (owner is null)");
            AppendLine(builder, 8, "{");
            AppendLine(builder, 12, "throw new global::System.ArgumentNullException(nameof(owner));");
            AppendLine(builder, 8, "}");
            if (string.Equals(method.ReturnType, "void", StringComparison.Ordinal))
            {
                AppendLine(builder, 8, $"NativeMethods.{method.NativeFunctionName}({string.Join(", ", method.InvocationArguments)});");
            }
            else
            {
                AppendLine(builder, 8, $"var result = NativeMethods.{method.NativeFunctionName}({string.Join(", ", method.InvocationArguments)});");
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
        ManagedHandleSpec handle,
        IReadOnlyList<AutoAbiFacadeMethodSpec> methods)
    {
        var builder = new StringBuilder();
        AppendFileHeader(builder, namespaceName);
        builder.AppendLine($"public static class {facadeClassName}");
        builder.AppendLine("{");

        foreach (var method in methods)
        {
            AppendLine(builder, 4, "/// <summary>");
            AppendLine(builder, 4, $"/// Public facade over <c>{method.Inner.NativeFunctionName}</c>.");
            AppendLine(builder, 4, "/// </summary>");
            var allParameters = new List<string> { $"this {handle.CsType} owner" };
            allParameters.AddRange(method.Inner.Parameters.Select(BuildParameterSignature));
            AppendLine(
                builder,
                4,
                $"public static {method.Inner.ReturnType} {method.PublicMethodName}({string.Join(", ", allParameters)})");
            AppendLine(builder, 4, "{");

            var forwardedArguments = method.Inner.Parameters
                .Select(parameter => BuildInvocationArgument(parameter.Modifier, parameter.ParameterName))
                .ToList();
            var invocation = $"{internalSurfaceClassName}.{method.Inner.MethodName}(owner";
            if (forwardedArguments.Count > 0)
            {
                invocation += ", " + string.Join(", ", forwardedArguments);
            }

            invocation += ")";

            if (string.Equals(method.Inner.ReturnType, "void", StringComparison.Ordinal))
            {
                AppendLine(builder, 8, invocation + ";");
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
        bool includeDeprecated,
        AutoAbiPublicFacadeSpec publicFacade)
    {
        Enabled = enabled;
        MethodPrefix = string.IsNullOrWhiteSpace(methodPrefix) ? "Abi" : methodPrefix.Trim();
        SectionSuffix = string.IsNullOrWhiteSpace(sectionSuffix) ? "_abi_surface" : sectionSuffix.Trim();
        IncludeDeprecated = includeDeprecated;
        PublicFacade = publicFacade;
    }

    public bool Enabled { get; }

    public string MethodPrefix { get; }

    public string SectionSuffix { get; }

    public bool IncludeDeprecated { get; }

    public AutoAbiPublicFacadeSpec PublicFacade { get; }

    public static AutoAbiSurfaceSpec Disabled()
    {
        return new AutoAbiSurfaceSpec(
            enabled: false,
            methodPrefix: "Abi",
            sectionSuffix: "_abi_surface",
            includeDeprecated: false,
            publicFacade: AutoAbiPublicFacadeSpec.Disabled());
    }
}

internal sealed class AutoAbiPublicFacadeSpec
{
    public AutoAbiPublicFacadeSpec(
        bool enabled,
        string classSuffix,
        string methodPrefix,
        string sectionSuffix,
        bool allowIntPtr)
    {
        Enabled = enabled;
        ClassSuffix = string.IsNullOrWhiteSpace(classSuffix) ? "_abi_facade" : classSuffix.Trim();
        MethodPrefix = methodPrefix?.Trim() ?? string.Empty;
        SectionSuffix = string.IsNullOrWhiteSpace(sectionSuffix) ? "_abi_facade" : sectionSuffix.Trim();
        AllowIntPtr = allowIntPtr;
    }

    public bool Enabled { get; }

    public string ClassSuffix { get; }

    public string MethodPrefix { get; }

    public string SectionSuffix { get; }

    public bool AllowIntPtr { get; }

    public static AutoAbiPublicFacadeSpec Disabled()
    {
        return new AutoAbiPublicFacadeSpec(
            enabled: false,
            classSuffix: "_abi_facade",
            methodPrefix: "Raw",
            sectionSuffix: "_abi_facade",
            allowIntPtr: false);
    }
}

internal sealed class AutoAbiMethodSpec
{
    public AutoAbiMethodSpec(
        string methodName,
        string returnType,
        IReadOnlyList<AutoAbiParameterSpec> parameters,
        IReadOnlyList<string> invocationArguments,
        string nativeFunctionName)
    {
        MethodName = methodName;
        ReturnType = returnType;
        Parameters = parameters;
        InvocationArguments = invocationArguments;
        NativeFunctionName = nativeFunctionName;
    }

    public string MethodName { get; }

    public string ReturnType { get; }

    public IReadOnlyList<AutoAbiParameterSpec> Parameters { get; }

    public IReadOnlyList<string> InvocationArguments { get; }

    public string NativeFunctionName { get; }
}

internal sealed class AutoAbiParameterSpec
{
    public AutoAbiParameterSpec(string parameterName, string typeName, string? modifier)
    {
        ParameterName = parameterName;
        TypeName = typeName;
        Modifier = modifier;
    }

    public string ParameterName { get; }

    public string TypeName { get; }

    public string? Modifier { get; }
}

internal sealed class AutoAbiFacadeMethodSpec
{
    public AutoAbiFacadeMethodSpec(string publicMethodName, AutoAbiMethodSpec inner)
    {
        PublicMethodName = publicMethodName;
        Inner = inner;
    }

    public string PublicMethodName { get; }

    public AutoAbiMethodSpec Inner { get; }
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
