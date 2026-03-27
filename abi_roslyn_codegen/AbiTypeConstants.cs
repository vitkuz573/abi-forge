namespace Abi.RoslynGenerator;

/// <summary>
/// Shared C-to-C# type mapping tables and C# keyword set used across all emitters.
/// </summary>
internal static class AbiTypeConstants
{
    /// <summary>
    /// Complete set of C# reserved keywords. Used to @-escape identifiers that would
    /// otherwise collide with language keywords.
    /// </summary>
    internal static readonly HashSet<string> CSharpKeywords = new(StringComparer.Ordinal)
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

    /// <summary>
    /// C-to-C# primitive type map for the P/Invoke (interop) layer.
    /// <c>char</c> maps to <c>byte</c> (unsigned), matching the native ABI convention
    /// where <c>char</c> is the unsigned byte type used for UTF-8 string buffers.
    /// </summary>
    internal static readonly Dictionary<string, string> InteropPrimitiveTypeMap = new(StringComparer.Ordinal)
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

    /// <summary>
    /// C-to-C# primitive type map for the managed API surface.
    /// <c>char</c> maps to <c>sbyte</c> (signed), matching C standard semantics
    /// for <c>signed char</c>. Also includes <c>intptr_t</c>/<c>uintptr_t</c>
    /// which appear in managed-facing handle and pointer signatures.
    /// </summary>
    internal static readonly Dictionary<string, string> ManagedPrimitiveTypeMap = new(StringComparer.Ordinal)
    {
        ["void"] = "void",
        ["bool"] = "bool",
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
    };
}
