from __future__ import annotations

from ._core_base import *  # noqa: F401,F403

def parse_snapshot_version(snapshot: dict[str, Any], label: str) -> AbiVersion:
    version_obj = snapshot.get("abi_version")
    if not isinstance(version_obj, dict):
        raise AbiFrameworkError(f"Snapshot '{label}' is missing abi_version.")
    try:
        major = int(version_obj["major"])
        minor = int(version_obj["minor"])
        patch = int(version_obj["patch"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AbiFrameworkError(f"Snapshot '{label}' has invalid abi_version format.") from exc
    return AbiVersion(major=major, minor=minor, patch=patch)


def as_symbol_set(snapshot: dict[str, Any], section: str) -> set[str]:
    payload = snapshot.get(section)
    if not isinstance(payload, dict):
        raise AbiFrameworkError(f"Snapshot is missing section '{section}'.")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list):
        raise AbiFrameworkError(f"Snapshot section '{section}' is missing symbols array.")
    return {str(x) for x in symbols}


def get_header_types(snapshot: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    header = snapshot.get("header")
    if not isinstance(header, dict):
        return {}, {}

    enums = header.get("enums")
    structs = header.get("structs")

    out_enums = enums if isinstance(enums, dict) else {}
    out_structs = structs if isinstance(structs, dict) else {}
    return out_enums, out_structs


def compare_enum_sets(base_enums: dict[str, Any], curr_enums: dict[str, Any]) -> dict[str, Any]:
    base_names = set(base_enums.keys())
    curr_names = set(curr_enums.keys())

    removed_enums = sorted(base_names - curr_names)
    added_enums = sorted(curr_names - base_names)

    changed_enums: dict[str, Any] = {}
    breaking_changes: list[str] = []
    additive_changes: list[str] = []

    for name in sorted(base_names & curr_names):
        base_members = base_enums[name].get("members")
        curr_members = curr_enums[name].get("members")
        if not isinstance(base_members, list) or not isinstance(curr_members, list):
            changed_enums[name] = {
                "kind": "unknown",
                "reason": "enum members payload malformed",
            }
            breaking_changes.append(f"enum {name} malformed")
            continue

        base_map = {str(item.get("name")): item for item in base_members if isinstance(item, dict)}
        curr_map = {str(item.get("name")): item for item in curr_members if isinstance(item, dict)}

        removed_members = sorted(set(base_map.keys()) - set(curr_map.keys()))
        added_members = sorted(set(curr_map.keys()) - set(base_map.keys()))

        value_changed: list[str] = []
        for member_name in sorted(set(base_map.keys()) & set(curr_map.keys())):
            b = base_map[member_name]
            c = curr_map[member_name]
            if (b.get("value"), b.get("value_expr")) != (c.get("value"), c.get("value_expr")):
                value_changed.append(member_name)

        if removed_members or value_changed:
            changed_enums[name] = {
                "kind": "breaking",
                "removed_members": removed_members,
                "added_members": added_members,
                "value_changed": value_changed,
            }
            if removed_members:
                breaking_changes.append(f"enum {name} removed members: {', '.join(removed_members)}")
            if value_changed:
                breaking_changes.append(f"enum {name} changed values: {', '.join(value_changed)}")
            continue

        if added_members:
            changed_enums[name] = {
                "kind": "additive",
                "removed_members": [],
                "added_members": added_members,
                "value_changed": [],
            }
            additive_changes.append(f"enum {name} added members: {', '.join(added_members)}")

    if removed_enums:
        breaking_changes.append("removed enums: " + ", ".join(removed_enums))
    if added_enums:
        additive_changes.append("added enums: " + ", ".join(added_enums))

    return {
        "removed_enums": removed_enums,
        "added_enums": added_enums,
        "changed_enums": changed_enums,
        "breaking_changes": breaking_changes,
        "additive_changes": additive_changes,
    }


def compare_struct_sets(base_structs: dict[str, Any], curr_structs: dict[str, Any], struct_tail_addition_is_breaking: bool) -> dict[str, Any]:
    base_names = set(base_structs.keys())
    curr_names = set(curr_structs.keys())

    removed_structs = sorted(base_names - curr_names)
    added_structs = sorted(curr_names - base_names)

    changed_structs: dict[str, Any] = {}
    breaking_changes: list[str] = []
    additive_changes: list[str] = []

    for name in sorted(base_names & curr_names):
        base_fields = base_structs[name].get("fields")
        curr_fields = curr_structs[name].get("fields")
        if not isinstance(base_fields, list) or not isinstance(curr_fields, list):
            changed_structs[name] = {
                "kind": "unknown",
                "reason": "struct fields payload malformed",
            }
            breaking_changes.append(f"struct {name} malformed")
            continue

        base_decls = [normalize_ws(str(item.get("declaration"))) for item in base_fields if isinstance(item, dict)]
        curr_decls = [normalize_ws(str(item.get("declaration"))) for item in curr_fields if isinstance(item, dict)]

        if base_decls == curr_decls:
            continue

        base_names_seq = [str(item.get("name")) for item in base_fields if isinstance(item, dict)]
        curr_names_seq = [str(item.get("name")) for item in curr_fields if isinstance(item, dict)]

        removed_fields = sorted(set(base_names_seq) - set(curr_names_seq))
        added_fields = sorted(set(curr_names_seq) - set(base_names_seq))

        common = set(base_names_seq) & set(curr_names_seq)
        changed_fields: list[str] = []
        for field_name in sorted(common):
            b_idx = base_names_seq.index(field_name)
            c_idx = curr_names_seq.index(field_name)
            if base_decls[b_idx] != curr_decls[c_idx] or b_idx != c_idx:
                changed_fields.append(field_name)

        base_is_prefix = len(curr_decls) >= len(base_decls) and curr_decls[: len(base_decls)] == base_decls
        additive_tail = base_is_prefix and not struct_tail_addition_is_breaking

        if additive_tail:
            changed_structs[name] = {
                "kind": "additive",
                "removed_fields": removed_fields,
                "added_fields": added_fields,
                "changed_fields": changed_fields,
                "base_is_prefix": base_is_prefix,
            }
            additive_changes.append(f"struct {name} tail extended")
        else:
            changed_structs[name] = {
                "kind": "breaking",
                "removed_fields": removed_fields,
                "added_fields": added_fields,
                "changed_fields": changed_fields,
                "base_is_prefix": base_is_prefix,
            }
            breaking_changes.append(f"struct {name} layout changed")

    if removed_structs:
        breaking_changes.append("removed structs: " + ", ".join(removed_structs))
    if added_structs:
        additive_changes.append("added structs: " + ", ".join(added_structs))

    return {
        "removed_structs": removed_structs,
        "added_structs": added_structs,
        "changed_structs": changed_structs,
        "breaking_changes": breaking_changes,
        "additive_changes": additive_changes,
    }


def compare_layout_probes(base_header: dict[str, Any], curr_header: dict[str, Any]) -> dict[str, Any]:
    base_layout = base_header.get("layout_probe")
    curr_layout = curr_header.get("layout_probe")

    out = {
        "available_in_baseline": False,
        "available_in_current": False,
        "checked_structs": 0,
        "breaking_changes": [],
        "warnings": [],
    }

    if isinstance(base_layout, dict) and bool(base_layout.get("available")):
        out["available_in_baseline"] = True
    if isinstance(curr_layout, dict) and bool(curr_layout.get("available")):
        out["available_in_current"] = True

    if out["available_in_baseline"] and not out["available_in_current"]:
        out["warnings"].append("layout probe unavailable in current snapshot while baseline had layout data")
        return out
    if out["available_in_current"] and not out["available_in_baseline"]:
        out["warnings"].append("layout probe available in current snapshot but baseline has no layout data")
        return out
    if not out["available_in_baseline"] and not out["available_in_current"]:
        return out

    base_structs_obj = base_layout.get("structs") if isinstance(base_layout, dict) else {}
    curr_structs_obj = curr_layout.get("structs") if isinstance(curr_layout, dict) else {}
    if not isinstance(base_structs_obj, dict) or not isinstance(curr_structs_obj, dict):
        out["warnings"].append("layout probe payload malformed")
        return out

    shared_structs = sorted(set(base_structs_obj.keys()) & set(curr_structs_obj.keys()))
    out["checked_structs"] = len(shared_structs)

    for struct_name in shared_structs:
        base_entry = base_structs_obj.get(struct_name)
        curr_entry = curr_structs_obj.get(struct_name)
        if not isinstance(base_entry, dict) or not isinstance(curr_entry, dict):
            out["breaking_changes"].append(f"layout {struct_name}: malformed entry")
            continue

        base_size = base_entry.get("size")
        curr_size = curr_entry.get("size")
        base_alignment = base_entry.get("alignment")
        curr_alignment = curr_entry.get("alignment")
        if base_size != curr_size:
            out["breaking_changes"].append(
                f"layout {struct_name}: size changed ({base_size} -> {curr_size})"
            )
        if base_alignment != curr_alignment:
            out["breaking_changes"].append(
                f"layout {struct_name}: alignment changed ({base_alignment} -> {curr_alignment})"
            )

        base_offsets = base_entry.get("offsets")
        curr_offsets = curr_entry.get("offsets")
        if not isinstance(base_offsets, dict) or not isinstance(curr_offsets, dict):
            out["breaking_changes"].append(f"layout {struct_name}: offsets payload malformed")
            continue

        for field_name in sorted(set(base_offsets.keys()) & set(curr_offsets.keys())):
            base_offset = base_offsets.get(field_name)
            curr_offset = curr_offsets.get(field_name)
            if base_offset != curr_offset:
                out["breaking_changes"].append(
                    f"layout {struct_name}.{field_name}: offset changed ({base_offset} -> {curr_offset})"
                )

    return out


def classify_change(has_breaking: bool, has_additive: bool) -> tuple[str, str]:
    if has_breaking:
        return "breaking", "major"
    if has_additive:
        return "additive", "minor"
    return "none", "none"


def recommended_version(baseline: AbiVersion, required_bump: str) -> AbiVersion:
    if required_bump == "major":
        return AbiVersion(baseline.major + 1, 0, 0)
    if required_bump == "minor":
        return AbiVersion(baseline.major, baseline.minor + 1, 0)
    return AbiVersion(baseline.major, baseline.minor, baseline.patch + 1)


def validate_version_policy(
    baseline_version: AbiVersion,
    current_version: AbiVersion,
    required_bump: str,
) -> tuple[bool, list[str]]:
    errors: list[str] = []

    if current_version.as_tuple() < baseline_version.as_tuple():
        errors.append(
            f"ABI version regressed: baseline {baseline_version.as_tuple()} -> current {current_version.as_tuple()}."
        )
        return False, errors

    if required_bump == "major":
        if current_version.major <= baseline_version.major:
            errors.append(
                "Breaking ABI changes detected but ABI major version was not increased "
                f"(baseline {baseline_version.major}, current {current_version.major})."
            )
            return False, errors
    elif required_bump == "minor":
        if current_version.major == baseline_version.major and current_version.minor <= baseline_version.minor:
            errors.append(
                "Additive ABI changes detected but ABI minor version was not increased "
                f"(baseline {baseline_version.major}.{baseline_version.minor}, "
                f"current {current_version.major}.{current_version.minor})."
            )
            return False, errors

    return True, errors


def compare_snapshots(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    base_header = baseline.get("header", {})
    curr_header = current.get("header", {})
    base_funcs = base_header.get("functions")
    curr_funcs = curr_header.get("functions")
    if not isinstance(base_funcs, dict) or not isinstance(curr_funcs, dict):
        raise AbiFrameworkError("Snapshots must include header.functions objects.")

    base_names = set(base_funcs.keys())
    curr_names = set(curr_funcs.keys())
    removed = sorted(base_names - curr_names)
    added = sorted(curr_names - base_names)
    changed = sorted(
        name
        for name in (base_names & curr_names)
        if base_funcs[name].get("signature") != curr_funcs[name].get("signature")
    )

    if removed:
        warnings.append(f"Header symbols removed since baseline: {', '.join(removed)}")
    if changed:
        warnings.append(f"Header signatures changed since baseline: {', '.join(changed)}")

    baseline_version = parse_snapshot_version(baseline, "baseline")
    current_version = parse_snapshot_version(current, "current")

    curr_header_symbols = as_symbol_set(current, "header")
    bindings_payload = current.get("bindings")
    if isinstance(bindings_payload, dict):
        binding_symbols = bindings_payload.get("symbols")
        if isinstance(binding_symbols, list) and binding_symbols:
            curr_binding_symbols = {str(x) for x in binding_symbols}
            missing_in_bindings = sorted(curr_header_symbols - curr_binding_symbols)
            extra_in_bindings = sorted(curr_binding_symbols - curr_header_symbols)
            if missing_in_bindings:
                errors.append(
                    "Header symbols missing in configured bindings: " + ", ".join(missing_in_bindings)
                )
            if extra_in_bindings:
                errors.append(
                    "Configured bindings symbols not present in header: " + ", ".join(extra_in_bindings)
                )
        else:
            warnings.append("Bindings symbol checks skipped: bindings.symbols is not configured.")
    else:
        warnings.append("Bindings symbol checks skipped: no bindings section in snapshot.")

    binary_payload = current.get("binary", {})
    binary_available = bool(binary_payload.get("available"))
    binary_skipped = bool(binary_payload.get("skipped"))
    if binary_available:
        curr_binary_symbols = as_symbol_set(current, "binary")
        missing_in_binary = sorted(curr_header_symbols - curr_binary_symbols)
        extra_prefixed_binary = sorted(curr_binary_symbols - curr_header_symbols)
        if missing_in_binary:
            errors.append(
                "Header symbols missing in native binary exports: " + ", ".join(missing_in_binary)
            )
        if extra_prefixed_binary:
            errors.append(
                "Native binary exports prefixed ABI symbols not present in header: " + ", ".join(extra_prefixed_binary)
            )

        allow_non_prefixed = bool(binary_payload.get("allow_non_prefixed_exports", False))
        non_prefixed = binary_payload.get("non_prefixed_exports")
        if isinstance(non_prefixed, list) and non_prefixed and not allow_non_prefixed:
            max_preview = 25
            preview = ", ".join(non_prefixed[:max_preview])
            if len(non_prefixed) > max_preview:
                preview += ", ..."
            errors.append(
                "Native binary exports non-ABI symbols. "
                f"Count={len(non_prefixed)}. Examples: {preview}"
            )
        if bool(binary_payload.get("potential_calling_convention_mismatch", False)):
            warnings.append(
                "Binary exports contain decorated symbols suggestive of calling-convention drift "
                "(e.g., _symbol@N). Review ABI calling conventions."
            )
        export_tool_errors = binary_payload.get("export_tool_errors")
        if isinstance(export_tool_errors, list) and export_tool_errors:
            warnings.append(
                f"Some export tools failed while scanning binary ({len(export_tool_errors)} failures). "
                "Results were produced from available tools."
            )
    elif not binary_skipped:
        warnings.append(
            "Binary export checks were not executed because the binary path does not exist yet."
        )

    base_enums, base_structs = get_header_types(baseline)
    curr_enums, curr_structs = get_header_types(current)

    struct_tail_breaking = True
    current_policy = current.get("policy")
    if isinstance(current_policy, dict):
        type_policy = current_policy.get("type_policy")
        if isinstance(type_policy, dict):
            struct_tail_breaking = bool(type_policy.get("struct_tail_addition_is_breaking", True))

    enum_diff = compare_enum_sets(base_enums=base_enums, curr_enums=curr_enums)
    struct_diff = compare_struct_sets(
        base_structs=base_structs,
        curr_structs=curr_structs,
        struct_tail_addition_is_breaking=struct_tail_breaking,
    )
    layout_diff = compare_layout_probes(base_header=base_header, curr_header=curr_header)

    function_breaking = bool(removed or changed)
    function_additive = bool(added)

    breaking_reasons: list[str] = []
    additive_reasons: list[str] = []

    if function_breaking:
        if removed:
            breaking_reasons.append("removed function symbols")
        if changed:
            breaking_reasons.append("changed function signatures")
    if function_additive:
        additive_reasons.append("added function symbols")

    breaking_reasons.extend(enum_diff["breaking_changes"])
    additive_reasons.extend(enum_diff["additive_changes"])
    breaking_reasons.extend(struct_diff["breaking_changes"])
    additive_reasons.extend(struct_diff["additive_changes"])
    if layout_diff["breaking_changes"]:
        breaking_reasons.extend(layout_diff["breaking_changes"])
    if layout_diff["warnings"]:
        warnings.extend(layout_diff["warnings"])

    change_classification, required_bump = classify_change(
        has_breaking=bool(breaking_reasons),
        has_additive=bool(additive_reasons),
    )

    version_ok, version_errors = validate_version_policy(
        baseline_version=baseline_version,
        current_version=current_version,
        required_bump=required_bump,
    )
    errors.extend(version_errors)

    recommended = recommended_version(baseline=baseline_version, required_bump=required_bump)

    status = "pass" if not errors else "fail"
    report = {
        "status": status,
        "change_classification": change_classification,
        "required_bump": required_bump,
        "baseline_abi_version": baseline_version.as_dict(),
        "current_abi_version": current_version.as_dict(),
        "recommended_next_version": recommended.as_dict(),
        "version_policy_satisfied": version_ok,
        "removed_symbols": removed,
        "added_symbols": added,
        "changed_signatures": changed,
        "enum_diff": enum_diff,
        "struct_diff": struct_diff,
        "layout_diff": layout_diff,
        "breaking_reasons": breaking_reasons,
        "additive_reasons": additive_reasons,
        "errors": errors,
        "warnings": warnings,
    }
    validate_report_payload(report, "compare report")
    return report


def print_report(report: dict[str, Any]) -> None:
    status = report.get("status", "unknown")
    print(f"ABI check status: {status}")

    removed = report.get("removed_symbols", [])
    added = report.get("added_symbols", [])
    changed = report.get("changed_signatures", [])
    print(f"Removed symbols: {len(removed)}")
    print(f"Added symbols: {len(added)}")
    print(f"Changed signatures: {len(changed)}")

    classification = report.get("change_classification")
    required_bump = report.get("required_bump")
    recommended = report.get("recommended_next_version")
    print(f"Change classification: {classification}")
    print(f"Required bump: {required_bump}")
    print(f"Recommended next version: {recommended}")

    warnings = report.get("warnings", [])
    errors = report.get("errors", [])

    if warnings:
        print("Warnings:")
        for warning in warnings:
            print(f"  - {warning}")
    if errors:
        print("Errors:")
        for error in errors:
            print(f"  - {error}")


def write_markdown_report(path: Path, report: dict[str, Any]) -> None:
    lines: list[str] = []
    lines.append(f"# ABI Report ({report.get('status', 'unknown')})")
    lines.append("")
    lines.append(f"- Baseline ABI version: `{report.get('baseline_abi_version')}`")
    lines.append(f"- Current ABI version: `{report.get('current_abi_version')}`")
    lines.append(f"- Change classification: `{report.get('change_classification')}`")
    lines.append(f"- Required bump: `{report.get('required_bump')}`")
    lines.append(f"- Recommended next version: `{report.get('recommended_next_version')}`")
    lines.append(f"- Removed symbols: `{len(report.get('removed_symbols', []))}`")
    lines.append(f"- Added symbols: `{len(report.get('added_symbols', []))}`")
    lines.append(f"- Changed signatures: `{len(report.get('changed_signatures', []))}`")
    lines.append("")

    breaking_reasons = report.get("breaking_reasons", [])
    additive_reasons = report.get("additive_reasons", [])
    warnings = report.get("warnings", [])
    errors = report.get("errors", [])

    if breaking_reasons:
        lines.append("## Breaking Reasons")
        for reason in breaking_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if additive_reasons:
        lines.append("## Additive Reasons")
        for reason in additive_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    if warnings:
        lines.append("## Warnings")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    if errors:
        lines.append("## Errors")
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def version_dict_to_str(value: Any) -> str:
    if isinstance(value, dict):
        major = value.get("major")
        minor = value.get("minor")
        patch = value.get("patch")
        if isinstance(major, int) and isinstance(minor, int) and isinstance(patch, int):
            return f"{major}.{minor}.{patch}"
    return "n/a"


def append_markdown_list(lines: list[str], items: list[str], indent: str = "") -> None:
    for item in items:
        lines.append(f"{indent}- {item}")


def render_target_changelog_section(target_name: str, report: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lines.append(f"## {target_name}")
    lines.append("")
    lines.append(f"- Status: `{report.get('status', 'unknown')}`")
    lines.append(f"- Change classification: `{report.get('change_classification', 'unknown')}`")
    lines.append(f"- Required bump: `{report.get('required_bump', 'none')}`")
    lines.append(f"- Baseline ABI version: `{version_dict_to_str(report.get('baseline_abi_version'))}`")
    lines.append(f"- Current ABI version: `{version_dict_to_str(report.get('current_abi_version'))}`")
    lines.append(f"- Recommended next version: `{version_dict_to_str(report.get('recommended_next_version'))}`")
    lines.append("")

    breaking_reasons = get_message_list(report, "breaking_reasons")
    additive_reasons = get_message_list(report, "additive_reasons")
    removed_symbols = get_message_list(report, "removed_symbols")
    added_symbols = get_message_list(report, "added_symbols")
    changed_signatures = get_message_list(report, "changed_signatures")

    enum_diff = report.get("enum_diff")
    struct_diff = report.get("struct_diff")
    enum_diff_obj = enum_diff if isinstance(enum_diff, dict) else {}
    struct_diff_obj = struct_diff if isinstance(struct_diff, dict) else {}

    lines.append("### Breaking")
    if not breaking_reasons and not removed_symbols and not changed_signatures:
        lines.append("- None.")
    else:
        if breaking_reasons:
            lines.append("- Reasons:")
            append_markdown_list(lines, breaking_reasons, indent="  ")
        if removed_symbols:
            lines.append("- Removed function symbols:")
            append_markdown_list(lines, removed_symbols, indent="  ")
        if changed_signatures:
            lines.append("- Changed function signatures:")
            append_markdown_list(lines, changed_signatures, indent="  ")

    removed_enums = get_message_list(enum_diff_obj, "removed_enums")
    if removed_enums:
        lines.append("- Removed enums:")
        append_markdown_list(lines, removed_enums, indent="  ")

    changed_enums = enum_diff_obj.get("changed_enums")
    if isinstance(changed_enums, dict):
        for enum_name in sorted(changed_enums.keys()):
            detail = changed_enums[enum_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "breaking":
                continue
            lines.append(f"- Enum `{enum_name}` changed (breaking):")
            removed_members = get_message_list(detail, "removed_members")
            changed_members = get_message_list(detail, "value_changed")
            if removed_members:
                lines.append("  - Removed members:")
                append_markdown_list(lines, removed_members, indent="    ")
            if changed_members:
                lines.append("  - Members with changed values:")
                append_markdown_list(lines, changed_members, indent="    ")

    removed_structs = get_message_list(struct_diff_obj, "removed_structs")
    if removed_structs:
        lines.append("- Removed structs:")
        append_markdown_list(lines, removed_structs, indent="  ")

    changed_structs = struct_diff_obj.get("changed_structs")
    if isinstance(changed_structs, dict):
        for struct_name in sorted(changed_structs.keys()):
            detail = changed_structs[struct_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "breaking":
                continue
            lines.append(f"- Struct `{struct_name}` layout changed (breaking).")

    lines.append("")
    lines.append("### Additive")
    if not additive_reasons and not added_symbols:
        lines.append("- None.")
    else:
        if additive_reasons:
            lines.append("- Reasons:")
            append_markdown_list(lines, additive_reasons, indent="  ")
        if added_symbols:
            lines.append("- Added function symbols:")
            append_markdown_list(lines, added_symbols, indent="  ")

    added_enums = get_message_list(enum_diff_obj, "added_enums")
    if added_enums:
        lines.append("- Added enums:")
        append_markdown_list(lines, added_enums, indent="  ")

    if isinstance(changed_enums, dict):
        for enum_name in sorted(changed_enums.keys()):
            detail = changed_enums[enum_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "additive":
                continue
            added_members = get_message_list(detail, "added_members")
            if added_members:
                lines.append(f"- Enum `{enum_name}` added members:")
                append_markdown_list(lines, added_members, indent="  ")

    added_structs = get_message_list(struct_diff_obj, "added_structs")
    if added_structs:
        lines.append("- Added structs:")
        append_markdown_list(lines, added_structs, indent="  ")

    if isinstance(changed_structs, dict):
        for struct_name in sorted(changed_structs.keys()):
            detail = changed_structs[struct_name]
            if not isinstance(detail, dict):
                continue
            if str(detail.get("kind")) != "additive":
                continue
            lines.append(f"- Struct `{struct_name}` was extended (additive tail).")

    warnings = get_message_list(report, "warnings")
    errors = get_message_list(report, "errors")
    if warnings:
        lines.append("")
        lines.append("### Warnings")
        append_markdown_list(lines, warnings)
    if errors:
        lines.append("")
        lines.append("### Errors")
        append_markdown_list(lines, errors)

    lines.append("")
    return lines


def render_changelog_document(
    title: str,
    release_tag: str | None,
    generated_at_utc: str,
    results_by_target: dict[str, dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- Generated at (UTC): `{generated_at_utc}`")
    if release_tag:
        lines.append(f"- Release tag: `{release_tag}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Target | Status | Classification | Required bump | Baseline | Current | Recommended |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for target_name in sorted(results_by_target.keys()):
        report = results_by_target[target_name]
        lines.append(
            f"| {target_name} | {report.get('status', 'unknown')} | "
            f"{report.get('change_classification', 'unknown')} | "
            f"{report.get('required_bump', 'none')} | "
            f"{version_dict_to_str(report.get('baseline_abi_version'))} | "
            f"{version_dict_to_str(report.get('current_abi_version'))} | "
            f"{version_dict_to_str(report.get('recommended_next_version'))} |"
        )
    lines.append("")

    for target_name in sorted(results_by_target.keys()):
        lines.extend(render_target_changelog_section(target_name, results_by_target[target_name]))

    return "\n".join(lines) + "\n"


def render_release_html_report(
    *,
    release_tag: str | None,
    generated_at_utc: str,
    verify_summary: dict[str, Any] | None,
    sync_summary: dict[str, Any] | None,
    codegen_summary: dict[str, Any] | None,
    changelog_summary: dict[str, Any] | None,
) -> str:
    verify_summary_obj = verify_summary if isinstance(verify_summary, dict) else {}
    sync_summary_obj = sync_summary if isinstance(sync_summary, dict) else {}
    codegen_summary_obj = codegen_summary if isinstance(codegen_summary, dict) else {}
    changelog_summary_obj = changelog_summary if isinstance(changelog_summary, dict) else {}

    def cell(value: Any) -> str:
        return html.escape(str(value))

    rows = [
        ("Verify Targets", verify_summary_obj.get("target_count", 0), verify_summary_obj.get("fail_count", 0), verify_summary_obj.get("warning_count", 0)),
        ("Sync Artifacts", sync_summary_obj.get("target_count", 0), sync_summary_obj.get("codegen_drift_count", 0), sync_summary_obj.get("sync_drift_count", 0)),
        ("Run Generators", codegen_summary_obj.get("target_count", 0), codegen_summary_obj.get("generator_fail_count", 0), codegen_summary_obj.get("warning_count", 0)),
        ("Build Changelog", changelog_summary_obj.get("target_count", 0), changelog_summary_obj.get("fail_count", 0), changelog_summary_obj.get("warning_count", 0)),
    ]

    table_rows = "\n".join(
        f"<tr><td>{cell(name)}</td><td>{cell(a)}</td><td>{cell(b)}</td><td>{cell(c)}</td></tr>"
        for name, a, b, c in rows
    )

    tag_line = f"<p><strong>Release tag:</strong> {cell(release_tag)}</p>" if release_tag else ""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>ABI Release Report</title>
  <style>
    :root {{
      --bg: #f6f7fb;
      --card: #ffffff;
      --text: #0f172a;
      --muted: #475569;
      --line: #d8dee9;
      --accent: #0f766e;
      --warn: #b45309;
      --err: #b91c1c;
    }}
    body {{
      margin: 0;
      font-family: "IBM Plex Sans", "Segoe UI", sans-serif;
      color: var(--text);
      background: linear-gradient(120deg, #eef6ff 0%, var(--bg) 55%, #f5fff5 100%);
      padding: 24px;
    }}
    .card {{
      max-width: 1100px;
      margin: 0 auto;
      background: var(--card);
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 24px;
      box-shadow: 0 18px 45px rgba(15, 23, 42, 0.08);
    }}
    h1 {{
      margin-top: 0;
      margin-bottom: 8px;
      font-size: 1.55rem;
      letter-spacing: 0.01em;
    }}
    .meta {{
      color: var(--muted);
      margin-bottom: 20px;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 12px;
    }}
    th, td {{
      text-align: left;
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      font-size: 0.96rem;
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      letter-spacing: 0.02em;
      font-size: 0.84rem;
      text-transform: uppercase;
    }}
    .legend {{
      margin-top: 16px;
      color: var(--muted);
      font-size: 0.88rem;
    }}
    .accent {{ color: var(--accent); }}
    .warn {{ color: var(--warn); }}
    .err {{ color: var(--err); }}
  </style>
</head>
<body>
  <section class="card">
    <h1>ABI Release Report</h1>
    <p class="meta"><strong>Generated (UTC):</strong> {cell(generated_at_utc)}</p>
    {tag_line}
    <table>
      <thead>
        <tr>
          <th>Pipeline Stage</th>
          <th>Targets/Items</th>
          <th>Failures/Drift</th>
          <th>Warnings/Sync Drift</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
    <p class="legend">
      <span class="accent">Green</span> implies no hard failures.
      <span class="warn">Warnings</span> should be reviewed.
      <span class="err">Failures</span> block safe release.
    </p>
  </section>
</body>
</html>
"""


def get_message_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def build_sarif_results_for_target(target_name: str, report: dict[str, Any], source_path: str | None) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    location = None
    if source_path:
        location = {
            "physicalLocation": {
                "artifactLocation": {
                    "uri": source_path,
                },
                "region": {
                    "startLine": 1,
                },
            }
        }

    for message in get_message_list(report, "errors"):
        result: dict[str, Any] = {
            "ruleId": "ABI001",
            "level": "error",
            "message": {
                "text": f"[{target_name}] {message}",
            },
        }
        if location:
            result["locations"] = [location]
        results.append(result)

    for message in get_message_list(report, "warnings"):
        result = {
            "ruleId": "ABI002",
            "level": "warning",
            "message": {
                "text": f"[{target_name}] {message}",
            },
        }
        if location:
            result["locations"] = [location]
        results.append(result)

    return results


def write_sarif_report(path: Path, results: list[dict[str, Any]]) -> None:
    payload = {
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "abi_framework",
                        "version": TOOL_VERSION,
                        "rules": [
                            {
                                "id": "ABI001",
                                "name": "AbiFrameworkError",
                                "shortDescription": {
                                    "text": "ABI compatibility error",
                                },
                                "defaultConfiguration": {
                                    "level": "error",
                                },
                            },
                            {
                                "id": "ABI002",
                                "name": "AbiFrameworkWarning",
                                "shortDescription": {
                                    "text": "ABI compatibility warning",
                                },
                                "defaultConfiguration": {
                                    "level": "warning",
                                },
                            },
                        ],
                    }
                },
                "results": results,
            }
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_aggregate_summary(results_by_target: dict[str, dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "target_count": len(results_by_target),
        "pass_count": 0,
        "fail_count": 0,
        "error_count": 0,
        "warning_count": 0,
        "classification": {
            "none": 0,
            "additive": 0,
            "breaking": 0,
        },
    }

    for report in results_by_target.values():
        if report.get("status") == "pass":
            summary["pass_count"] += 1
        else:
            summary["fail_count"] += 1
        summary["error_count"] += len(get_message_list(report, "errors"))
        summary["warning_count"] += len(get_message_list(report, "warnings"))
        classification = str(report.get("change_classification", "none"))
        if classification in summary["classification"]:
            summary["classification"][classification] += 1

    return summary


CLASSIFICATION_ORDER = {
    "none": 0,
    "additive": 1,
    "breaking": 2,
}


