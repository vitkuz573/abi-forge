from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .common import get_targets_map, resolve_baseline_for_target, resolve_binary_for_target

def build_waiver_audit_for_target(target_name: str, effective_policy: dict[str, Any]) -> dict[str, Any]:
    now = now_utc()
    requirements = normalize_waiver_requirements(
        effective_policy.get("waiver_requirements"),
        f"effective policy '{target_name}'",
    )
    warn_days_raw = requirements.get("warn_expiring_within_days")
    warn_days = int(warn_days_raw) if isinstance(warn_days_raw, int) else 30

    waivers_raw = effective_policy.get("waivers")
    waivers = [item for item in waivers_raw if isinstance(item, PolicyWaiver)] if isinstance(waivers_raw, list) else []
    entries: list[dict[str, Any]] = []
    for waiver in waivers:
        if not any(pattern.search(target_name) for pattern in waiver.target_patterns):
            continue

        missing_metadata: list[str] = []
        if bool(requirements.get("require_owner")) and not waiver.owner:
            missing_metadata.append("owner")
        if bool(requirements.get("require_reason")) and not waiver.reason:
            missing_metadata.append("reason")
        if bool(requirements.get("require_expires_utc")) and not waiver.expires_utc:
            missing_metadata.append("expires_utc")
        if bool(requirements.get("require_approved_by")) and not waiver.approved_by:
            missing_metadata.append("approved_by")
        if bool(requirements.get("require_ticket")) and not waiver.ticket:
            missing_metadata.append("ticket")

        status = "active"
        expires_in_days = None
        expired = False
        expiring_soon = False
        if waiver.expires_utc:
            expires_at = parse_utc_timestamp(waiver.expires_utc)
            expires_in_days = round((expires_at - now).total_seconds() / 86400.0, 3)
            if expires_at < now:
                status = "expired"
                expired = True
            elif expires_in_days <= float(warn_days):
                expiring_soon = True

        if missing_metadata:
            status = "invalid_metadata"

        entries.append(
            {
                "waiver_id": waiver.waiver_id,
                "severity": waiver.severity,
                "status": status,
                "expired": expired,
                "expiring_soon": expiring_soon,
                "expires_in_days": expires_in_days,
                "created_utc": waiver.created_utc,
                "expires_utc": waiver.expires_utc,
                "owner": waiver.owner,
                "approved_by": waiver.approved_by,
                "ticket": waiver.ticket,
                "reason": waiver.reason,
                "missing_metadata": missing_metadata,
                "pattern": waiver.message_pattern.pattern,
            }
        )

    return {
        "target": target_name,
        "waiver_requirements": requirements,
        "waiver_count": len(entries),
        "expired_count": sum(1 for item in entries if bool(item.get("expired"))),
        "expiring_soon_count": sum(1 for item in entries if bool(item.get("expiring_soon"))),
        "invalid_metadata_count": sum(1 for item in entries if bool(item.get("missing_metadata"))),
        "entries": entries,
    }


def command_waiver_audit(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)

    results: dict[str, dict[str, Any]] = {}
    expired_total = 0
    invalid_total = 0
    expiring_soon_total = 0
    for target_name in target_names:
        effective_policy = resolve_effective_policy(config=config, target_name=target_name)
        audit = build_waiver_audit_for_target(target_name, effective_policy)
        results[target_name] = audit
        expired_total += int(audit.get("expired_count") or 0)
        invalid_total += int(audit.get("invalid_metadata_count") or 0)
        expiring_soon_total += int(audit.get("expiring_soon_count") or 0)

    aggregate = {
        "generated_at_utc": utc_timestamp_now(),
        "results": results,
        "summary": {
            "target_count": len(target_names),
            "waiver_count": sum(int(item.get("waiver_count") or 0) for item in results.values()),
            "expired_count": expired_total,
            "expiring_soon_count": expiring_soon_total,
            "invalid_metadata_count": invalid_total,
        },
    }

    for target_name in sorted(results.keys()):
        item = results[target_name]
        print(
            f"[{target_name}] waivers={item.get('waiver_count', 0)} "
            f"expired={item.get('expired_count', 0)} "
            f"expiring_soon={item.get('expiring_soon_count', 0)} "
            f"invalid_metadata={item.get('invalid_metadata_count', 0)}"
        )

    if args.output:
        write_json(Path(args.output).resolve(), aggregate)
    elif bool(args.print_json):
        print(json.dumps(aggregate, indent=2, sort_keys=True))

    if bool(args.fail_on_expired) and expired_total > 0:
        return 1
    if bool(args.fail_on_missing_metadata) and invalid_total > 0:
        return 1
    if bool(args.fail_on_expiring_soon) and expiring_soon_total > 0:
        return 1
    return 0


def command_doctor(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    issues: list[tuple[str, str, str]] = []
    schema_ok, schema_note = validate_with_jsonschema_if_available("config", config)
    if not schema_ok and schema_note:
        issues.append(("warning", "global", f"jsonschema fallback mode: {schema_note}"))

    for target_name in sorted(targets.keys()):
        target = targets[target_name]
        header_cfg = target.get("header")
        if not isinstance(header_cfg, dict):
            issues.append(("error", target_name, "missing header config"))
            continue

        header_path_value = header_cfg.get("path")
        if not isinstance(header_path_value, str) or not header_path_value:
            issues.append(("error", target_name, "header.path is missing"))
        else:
            header_path = ensure_relative_path(repo_root, header_path_value).resolve()
            if not header_path.exists():
                issues.append(("error", target_name, f"header file not found: {header_path}"))
        try:
            parser_cfg = resolve_header_parser_config(header_cfg=header_cfg, repo_root=repo_root)
            if parser_cfg["backend"] == "clang_preprocess":
                try:
                    resolve_parser_compiler(parser_cfg)
                except AbiFrameworkError as exc:
                    severity = "warning" if parser_cfg.get("fallback_to_regex", True) else "error"
                    issues.append((severity, target_name, str(exc)))
        except AbiFrameworkError as exc:
            issues.append(("error", target_name, f"header.parser config invalid: {exc}"))

        bindings_cfg = target.get("bindings")
        if bindings_cfg is not None:
            if not isinstance(bindings_cfg, dict):
                issues.append(("error", target_name, "bindings must be an object when specified"))
            else:
                expected_symbols = bindings_cfg.get("expected_symbols")
                if expected_symbols is not None:
                    if not isinstance(expected_symbols, list):
                        issues.append(("error", target_name, "bindings.expected_symbols must be an array"))
                    elif not expected_symbols:
                        issues.append(("warning", target_name, "bindings.expected_symbols is empty"))

        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        if not baseline_path.exists():
            severity = "error" if bool(args.require_baselines) else "warning"
            issues.append((severity, target_name, f"baseline missing: {baseline_path}"))

        binary_value, unresolved = resolve_binary_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
        )
        if not unresolved and binary_value:
            binary_path = Path(binary_value)
            if not binary_path.exists():
                severity = "error" if bool(args.require_binaries) else "warning"
                issues.append((severity, target_name, f"binary missing: {binary_path}"))
        elif bool(args.require_binaries):
            issues.append(("error", target_name, "binary path is not configured"))

        try:
            codegen_cfg = resolve_codegen_config(target=target, target_name=target_name, repo_root=repo_root)
            idl_output = codegen_cfg.get("idl_output_path")
            if not isinstance(idl_output, Path):
                idl_output = ensure_relative_path(repo_root, f"abi/generated/{target_name}.idl.json").resolve()
            if not idl_output.parent.exists():
                issues.append(("warning", target_name, f"IDL output parent does not exist yet: {idl_output.parent}"))

            resolve_bindings_metadata(target=target, target_name=target_name, repo_root=repo_root)
            normalize_generator_entries(target_name=target_name, target=target)

        except AbiFrameworkError as exc:
            issues.append(("error", target_name, f"codegen config invalid: {exc}"))

        try:
            effective_policy = resolve_effective_policy(config=config, target_name=target_name)
            waiver_audit = build_waiver_audit_for_target(target_name, effective_policy)
            expired_count = int(waiver_audit.get("expired_count") or 0)
            invalid_count = int(waiver_audit.get("invalid_metadata_count") or 0)
            expiring_soon_count = int(waiver_audit.get("expiring_soon_count") or 0)
            if expired_count > 0:
                issues.append(("warning", target_name, f"expired waivers detected: {expired_count}"))
            if invalid_count > 0:
                issues.append(("error", target_name, f"waivers with missing required metadata: {invalid_count}"))
            if expiring_soon_count > 0:
                issues.append(("warning", target_name, f"waivers expiring soon: {expiring_soon_count}"))
        except AbiFrameworkError as exc:
            issues.append(("error", target_name, f"policy config invalid: {exc}"))

    error_count = sum(1 for sev, _, _ in issues if sev == "error")
    warning_count = sum(1 for sev, _, _ in issues if sev == "warning")

    if not issues:
        print("abi_framework doctor: healthy")
        return 0

    print("abi_framework doctor: issues found")
    for severity, target_name, message in issues:
        print(f"  [{severity}] {target_name}: {message}")

    if error_count > 0:
        return 1
    if bool(args.fail_on_warnings) and warning_count > 0:
        return 1
    return 0


def command_changelog(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    if args.baseline and not args.target:
        raise AbiFrameworkError("--baseline can only be used together with --target.")

    if args.target:
        if args.target not in targets:
            known = ", ".join(sorted(targets.keys()))
            raise AbiFrameworkError(f"Unknown target '{args.target}'. Known targets: {known}")
        target_names = [args.target]
    else:
        target_names = sorted(targets.keys())

    results_by_target: dict[str, dict[str, Any]] = {}
    sarif_results: list[dict[str, Any]] = []

    for target_name in target_names:
        if args.baseline and args.target:
            baseline_path = Path(args.baseline).resolve()
        else:
            baseline_path = resolve_baseline_for_target(
                repo_root=repo_root,
                config=config,
                target_name=target_name,
                baseline_root=args.baseline_root,
            )
        if not baseline_path.exists():
            raise AbiFrameworkError(f"Baseline does not exist for target '{target_name}': {baseline_path}")

        current = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
        )
        baseline = load_snapshot(baseline_path)
        raw_report = compare_snapshots(baseline=baseline, current=current)
        effective_policy = resolve_effective_policy(config=config, target_name=target_name)
        report = apply_policy_to_report(
            report=raw_report,
            policy=effective_policy,
            target_name=target_name,
        )
        results_by_target[target_name] = report

        current_header = current.get("header")
        source_path = None
        if isinstance(current_header, dict):
            path_value = current_header.get("path")
            if isinstance(path_value, str):
                source_path = path_value
        sarif_results.extend(
            build_sarif_results_for_target(
                target_name=target_name,
                report=report,
                source_path=source_path,
            )
        )

    generated_at_utc = utc_timestamp_now()
    changelog = render_changelog_document(
        title=str(args.title),
        release_tag=args.release_tag,
        generated_at_utc=generated_at_utc,
        results_by_target=results_by_target,
    )

    if args.output:
        output_path = Path(args.output).resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(changelog, encoding="utf-8")
        print(f"Wrote changelog: {output_path}")
    else:
        print(changelog, end="")

    aggregate = {
        "generated_at_utc": generated_at_utc,
        "results": results_by_target,
        "summary": build_aggregate_summary(results_by_target),
    }

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    if args.sarif_report:
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    has_failing = any(report.get("status") != "pass" for report in results_by_target.values())
    has_warnings = aggregate["summary"]["warning_count"] > 0
    if bool(args.fail_on_failing) and has_failing:
        return 1
    if bool(args.fail_on_warnings) and has_warnings:
        return 1
    return 0

