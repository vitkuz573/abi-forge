using System;
using System.Collections.Generic;
using System.Collections.Immutable;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using Microsoft.CodeAnalysis;
using Microsoft.CodeAnalysis.CSharp;
using Microsoft.CodeAnalysis.CSharp.Syntax;
using Microsoft.CodeAnalysis.Text;

namespace Abi.RoslynGenerator;

[Generator(LanguageNames.CSharp)]
public sealed class AbiInteropGenerator : IIncrementalGenerator
{
    private static readonly DiagnosticDescriptor MissingIdlDescriptor = new(
        id: "ABIGEN001",
        title: "ABI IDL file not found",
        messageFormat: "ABI IDL file '{0}' was not found in AdditionalFiles; configure AdditionalFiles and AbiIdlPath",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MultipleIdlDescriptor = new(
        id: "ABIGEN002",
        title: "Multiple ABI IDL files matched",
        messageFormat: "Multiple AdditionalFiles match ABI IDL path '{0}': {1}; keep exactly one match",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor EmptyIdlDescriptor = new(
        id: "ABIGEN003",
        title: "ABI IDL file is empty",
        messageFormat: "ABI IDL file '{0}' is empty or unreadable",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor GenerationFailedDescriptor = new(
        id: "ABIGEN004",
        title: "ABI source generation failed",
        messageFormat: "Failed to generate interop from '{0}' because {1}",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MissingManagedMetadataDescriptor = new(
        id: "ABIGEN005",
        title: "Managed metadata file not found",
        messageFormat: "Managed metadata file '{0}' was not found in AdditionalFiles; configure AdditionalFiles and AbiManagedMetadataPath",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MultipleManagedMetadataDescriptor = new(
        id: "ABIGEN006",
        title: "Multiple managed metadata files matched",
        messageFormat: "Multiple AdditionalFiles match managed metadata path '{0}': {1}; keep exactly one match",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor EmptyManagedMetadataDescriptor = new(
        id: "ABIGEN007",
        title: "Managed metadata file is empty",
        messageFormat: "Managed metadata file '{0}' is empty or unreadable",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MissingManagedHandleTypeDescriptor = new(
        id: "ABIGEN008",
        title: "Managed handle type was auto-generated",
        messageFormat: "Managed handle '{0}' was not found in compilation; generator emitted fallback SafeHandle class",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Warning,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor InvalidManagedHandleTypeDescriptor = new(
        id: "ABIGEN009",
        title: "Managed handle base type must be partial class",
        messageFormat: "Managed handle '{0}' must be declared as a partial class in project source",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor ManagedHandleBaseTypeDescriptor = new(
        id: "ABIGEN010",
        title: "Managed handle base type must inherit SafeHandle",
        messageFormat: "Managed handle '{0}' must derive from System.Runtime.InteropServices.SafeHandle",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor ManagedHandleAccessMismatchDescriptor = new(
        id: "ABIGEN011",
        title: "Managed handle accessibility mismatch",
        messageFormat: "Managed handle '{0}' metadata access '{1}' does not match declared accessibility '{2}'",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor DuplicateManagedHandleDescriptor = new(
        id: "ABIGEN012",
        title: "Duplicate managed handle metadata entry",
        messageFormat: "Managed metadata contains duplicate handle entry '{0}'",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MissingManagedApiMetadataDescriptor = new(
        id: "ABIGEN013",
        title: "Managed API metadata file not found",
        messageFormat: "Managed API metadata file '{0}' was not found in AdditionalFiles; configure AdditionalFiles and AbiManagedApiMetadataPath",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor MultipleManagedApiMetadataDescriptor = new(
        id: "ABIGEN014",
        title: "Multiple managed API metadata files matched",
        messageFormat: "Multiple AdditionalFiles match managed API metadata path '{0}': {1}; keep exactly one match",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    private static readonly DiagnosticDescriptor EmptyManagedApiMetadataDescriptor = new(
        id: "ABIGEN015",
        title: "Managed API metadata file is empty",
        messageFormat: "Managed API metadata file '{0}' is empty or unreadable",
        category: "Abi.SourceGenerator",
        DiagnosticSeverity.Error,
        isEnabledByDefault: true
    );

    public void Initialize(IncrementalGeneratorInitializationContext context)
    {
        var optionsProvider = context.AnalyzerConfigOptionsProvider
            .Select(static (provider, _) => GeneratorOptions.From(provider.GlobalOptions));

        var additionalFilesProvider = context.AdditionalTextsProvider
            .Select(static (file, cancellationToken) => new AdditionalFileSnapshot(
                file.Path,
                file.GetText(cancellationToken)?.ToString()
            ))
            .Collect();

        var generationInputProvider = context.CompilationProvider.Combine(additionalFilesProvider.Combine(optionsProvider));
        context.RegisterSourceOutput(generationInputProvider, static (spc, input) =>
        {
            Execute(spc, input.Left, input.Right.Left, input.Right.Right);
        });
    }

    private static void Execute(
        SourceProductionContext context,
        Compilation compilation,
        ImmutableArray<AdditionalFileSnapshot> files,
        GeneratorOptions options)
    {
        var matchedIdlFile = ResolveSingleAdditionalFile(
            context,
            files,
            options.IdlPath,
            options.MatchesIdlPath,
            MissingIdlDescriptor,
            MultipleIdlDescriptor,
            EmptyIdlDescriptor);
        if (!matchedIdlFile.HasValue)
        {
            return;
        }

        var matchedManagedFile = ResolveSingleAdditionalFile(
            context,
            files,
            options.ManagedMetadataPath,
            options.MatchesManagedMetadataPath,
            MissingManagedMetadataDescriptor,
            MultipleManagedMetadataDescriptor,
            EmptyManagedMetadataDescriptor);
        if (!matchedManagedFile.HasValue)
        {
            return;
        }

        var matchedManagedApiFile = ResolveSingleAdditionalFile(
            context,
            files,
            options.ManagedApiMetadataPath,
            options.MatchesManagedApiMetadataPath,
            MissingManagedApiMetadataDescriptor,
            MultipleManagedApiMetadataDescriptor,
            EmptyManagedApiMetadataDescriptor);
        if (!matchedManagedApiFile.HasValue)
        {
            return;
        }

        try
        {
            var model = AbiInteropSourceEmitter.ParseIdl(matchedIdlFile.Value.Content!);
            var typeModel = AbiInteropTypesSourceEmitter.ParseIdl(matchedIdlFile.Value.Content!);
            var source = AbiInteropSourceEmitter.RenderCode(model, options);
            var typesSource = AbiInteropTypesSourceEmitter.RenderTypesCode(typeModel, options);

            var managedHandlesModel = AbiInteropTypesSourceEmitter.ParseManagedMetadata(matchedManagedFile.Value.Content!);
            var handleValidation = ValidateManagedHandleTypes(context, compilation, managedHandlesModel);
            if (!handleValidation.IsValid)
            {
                return;
            }

            var handlesSource = AbiInteropTypesSourceEmitter.RenderHandlesCode(
                typeModel,
                managedHandlesModel,
                options,
                handleValidation.AutoGeneratedHandleTypeNames);

            var interopSources = new (string Section, string SourceText)[]
            {
                ("abi", source),
                ("types", typesSource),
                ("handles", handlesSource),
            };
            foreach (var generated in interopSources)
            {
                context.AddSource(
                    ResolveInteropHint(typeModel, options, generated.Section),
                    SourceText.From(generated.SourceText, Encoding.UTF8)
                );
            }

            var managedApiModel = ManagedApiSourceEmitter.ParseManagedApiMetadata(
                matchedManagedApiFile.Value.Content!,
                model);
            var managedApiSources = ManagedApiSourceEmitter.RenderSources(managedApiModel);
            foreach (var generatedSource in managedApiSources)
            {
                context.AddSource(
                    generatedSource.HintName,
                    SourceText.From(generatedSource.SourceText, Encoding.UTF8)
                );
            }
        }
        catch (GeneratorException ex)
        {
            context.ReportDiagnostic(
                Diagnostic.Create(GenerationFailedDescriptor, Location.None, matchedIdlFile.Value.Path, ex.Message)
            );
        }
        catch (Exception ex)
        {
            context.ReportDiagnostic(
                Diagnostic.Create(GenerationFailedDescriptor, Location.None, matchedIdlFile.Value.Path, ex.Message)
            );
        }
    }

    private static AdditionalFileSnapshot? ResolveSingleAdditionalFile(
        SourceProductionContext context,
        ImmutableArray<AdditionalFileSnapshot> files,
        string configuredPath,
        Func<string, bool> matcher,
        DiagnosticDescriptor missingDescriptor,
        DiagnosticDescriptor multipleDescriptor,
        DiagnosticDescriptor emptyDescriptor)
    {
        var matches = files
            .Where(file => matcher(file.Path))
            .OrderBy(file => file.Path, StringComparer.Ordinal)
            .ToArray();

        if (matches.Length == 0)
        {
            context.ReportDiagnostic(
                Diagnostic.Create(missingDescriptor, Location.None, configuredPath)
            );
            return null;
        }

        if (matches.Length > 1)
        {
            var preview = string.Join(", ", matches.Take(3).Select(item => item.Path));
            if (matches.Length > 3)
            {
                preview += ", ...";
            }

            context.ReportDiagnostic(
                Diagnostic.Create(multipleDescriptor, Location.None, configuredPath, preview)
            );
            return null;
        }

        var match = matches[0];
        if (string.IsNullOrWhiteSpace(match.Content))
        {
            context.ReportDiagnostic(
                Diagnostic.Create(emptyDescriptor, Location.None, match.Path)
            );
            return null;
        }

        return match;
    }

    private static HandleValidationResult ValidateManagedHandleTypes(
        SourceProductionContext context,
        Compilation compilation,
        ManagedHandlesModel handlesModel)
    {
        var safeHandleType = compilation.GetTypeByMetadataName("System.Runtime.InteropServices.SafeHandle");
        if (safeHandleType is null)
        {
            context.ReportDiagnostic(
                Diagnostic.Create(
                    GenerationFailedDescriptor,
                    Location.None,
                    "<compilation>",
                    "type System.Runtime.InteropServices.SafeHandle is unavailable"
                )
            );
            return new HandleValidationResult(false, new HashSet<string>(StringComparer.Ordinal));
        }

        var seenHandles = new HashSet<string>(StringComparer.Ordinal);
        var success = true;
        var autoGenerated = new HashSet<string>(StringComparer.Ordinal);

        foreach (var handle in handlesModel.Handles)
        {
            var fullTypeName = BuildManagedTypeFullName(handle.NamespaceName, handle.CsType);
            if (!seenHandles.Add(fullTypeName))
            {
                context.ReportDiagnostic(
                    Diagnostic.Create(DuplicateManagedHandleDescriptor, Location.None, fullTypeName)
                );
                success = false;
                continue;
            }

            var handleType = compilation.GetTypeByMetadataName(fullTypeName);
            if (handleType is null)
            {
                context.ReportDiagnostic(
                    Diagnostic.Create(MissingManagedHandleTypeDescriptor, Location.None, fullTypeName)
                );
                autoGenerated.Add(fullTypeName);
                continue;
            }

            if (!IsPartialClass(handleType, context.CancellationToken))
            {
                context.ReportDiagnostic(
                    Diagnostic.Create(InvalidManagedHandleTypeDescriptor, Location.None, fullTypeName)
                );
                success = false;
            }

            if (!InheritsFrom(handleType, safeHandleType))
            {
                context.ReportDiagnostic(
                    Diagnostic.Create(ManagedHandleBaseTypeDescriptor, Location.None, fullTypeName)
                );
                success = false;
            }

            var expectedAccessibility = string.Equals(handle.Access, "public", StringComparison.Ordinal)
                ? Accessibility.Public
                : Accessibility.Internal;
            if (handleType.DeclaredAccessibility != expectedAccessibility)
            {
                context.ReportDiagnostic(
                    Diagnostic.Create(
                        ManagedHandleAccessMismatchDescriptor,
                        Location.None,
                        fullTypeName,
                        HandleAccessText(expectedAccessibility),
                        HandleAccessText(handleType.DeclaredAccessibility)
                    )
                );
                success = false;
            }
        }

        return new HandleValidationResult(success, autoGenerated);
    }

    private static bool IsPartialClass(INamedTypeSymbol symbol, System.Threading.CancellationToken cancellationToken)
    {
        if (symbol.TypeKind != TypeKind.Class || symbol.DeclaringSyntaxReferences.Length == 0)
        {
            return false;
        }

        foreach (var reference in symbol.DeclaringSyntaxReferences)
        {
            if (reference.GetSyntax(cancellationToken) is not ClassDeclarationSyntax declaration)
            {
                return false;
            }

            if (!declaration.Modifiers.Any(static token => token.IsKind(SyntaxKind.PartialKeyword)))
            {
                return false;
            }
        }

        return true;
    }

    private static bool InheritsFrom(INamedTypeSymbol symbol, INamedTypeSymbol baseType)
    {
        for (INamedTypeSymbol? current = symbol; current != null; current = current.BaseType)
        {
            if (SymbolEqualityComparer.Default.Equals(current, baseType))
            {
                return true;
            }
        }

        return false;
    }

    private static string BuildManagedTypeFullName(string namespaceName, string typeName)
    {
        return string.IsNullOrWhiteSpace(namespaceName)
            ? typeName
            : namespaceName + "." + typeName;
    }

    private static string HandleAccessText(Accessibility accessibility)
    {
        return accessibility switch
        {
            Accessibility.Public => "public",
            Accessibility.Internal => "internal",
            Accessibility.Private => "private",
            Accessibility.Protected => "protected",
            Accessibility.ProtectedAndInternal => "private protected",
            Accessibility.ProtectedOrInternal => "protected internal",
            _ => accessibility.ToString().ToLowerInvariant(),
        };
    }

    private static string ResolveInteropHint(
        IdlTypeModel model,
        GeneratorOptions options,
        string sectionName)
    {
        var defaultHint = BuildInteropSectionDefaultHint(options.ClassName, sectionName);
        return model.OutputHints.ResolveHint(
            sectionName,
            defaultHint,
            options.ClassName,
            options.NamespaceName,
            model.TargetName);
    }

    private static string BuildInteropSectionDefaultHint(string className, string sectionName)
    {
        var effectiveClassName = string.IsNullOrWhiteSpace(className)
            ? "NativeMethods"
            : className.Trim();
        var sanitizedClass = Regex.Replace(effectiveClassName, "[^A-Za-z0-9_]+", "_");
        if (string.IsNullOrWhiteSpace(sanitizedClass))
        {
            sanitizedClass = "NativeMethods";
        }

        var tokens = sectionName
            .Split(new[] { '_', '-', '.', '/' }, StringSplitOptions.RemoveEmptyEntries);
        var sectionBuilder = new StringBuilder();
        foreach (var token in tokens)
        {
            var trimmed = token.Trim();
            if (trimmed.Length == 0)
            {
                continue;
            }

            sectionBuilder.Append(char.ToUpperInvariant(trimmed[0]));
            if (trimmed.Length > 1)
            {
                sectionBuilder.Append(trimmed.Substring(1));
            }
        }

        var sanitizedSection = sectionBuilder.ToString();
        if (string.IsNullOrWhiteSpace(sanitizedSection))
        {
            sanitizedSection = "Interop";
        }

        return sanitizedClass + "." + sanitizedSection + ".g.cs";
    }

    private readonly struct HandleValidationResult
    {
        public HandleValidationResult(bool isValid, HashSet<string> autoGeneratedHandleTypeNames)
        {
            IsValid = isValid;
            AutoGeneratedHandleTypeNames = autoGeneratedHandleTypeNames;
        }

        public bool IsValid { get; }

        public HashSet<string> AutoGeneratedHandleTypeNames { get; }
    }

    private readonly struct AdditionalFileSnapshot
    {
        public AdditionalFileSnapshot(string path, string? content)
        {
            Path = path;
            Content = content;
        }

        public string Path { get; }

        public string? Content { get; }
    }
}
