from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .common import get_targets_map, resolve_baseline_for_target

def command_snapshot(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    snapshot = build_snapshot(
        config=config,
        target_name=args.target,
        repo_root=repo_root,
        binary_override=args.binary,
        skip_binary=args.skip_binary,
    )

    if args.output:
        write_json(Path(args.output).resolve(), snapshot)
    else:
        print(json.dumps(snapshot, indent=2, sort_keys=True))

    print(
        f"Snapshot created for target '{args.target}' with "
        f"{snapshot['header']['function_count']} header symbols, "
        f"{snapshot['header']['enum_count']} enums, "
        f"{snapshot['header']['struct_count']} structs, and "
        f"{(snapshot.get('bindings') or {}).get('symbol_count', 0)} binding symbols.",
        file=sys.stderr,
    )
    return 0


def command_verify(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())

    current = build_snapshot(
        config=config,
        target_name=args.target,
        repo_root=repo_root,
        binary_override=args.binary,
        skip_binary=args.skip_binary,
    )
    baseline = load_snapshot(Path(args.baseline).resolve())

    raw_report = compare_snapshots(baseline=baseline, current=current)
    effective_policy = resolve_effective_policy(config=config, target_name=str(args.target))
    report = apply_policy_to_report(
        report=raw_report,
        policy=effective_policy,
        target_name=str(args.target),
    )

    if args.current_output:
        write_json(Path(args.current_output).resolve(), current)
    if args.report:
        write_json(Path(args.report).resolve(), report)
    if args.markdown_report:
        write_markdown_report(Path(args.markdown_report).resolve(), report)
    if args.sarif_report:
        current_header = current.get("header")
        source_path = None
        if isinstance(current_header, dict):
            path_value = current_header.get("path")
            if isinstance(path_value, str):
                source_path = path_value
        sarif_results = build_sarif_results_for_target(
            target_name=str(args.target),
            report=report,
            source_path=source_path,
        )
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    print_report(report)
    status_ok = report.get("status") == "pass"
    effective_fail_on_warnings = bool(args.fail_on_warnings) or bool(effective_policy.get("fail_on_warnings", False))
    if status_ok and effective_fail_on_warnings:
        status_ok = not bool(get_message_list(report, "warnings"))
    return 0 if status_ok else 1


def command_diff(args: argparse.Namespace) -> int:
    baseline = load_snapshot(Path(args.baseline).resolve())
    current = load_snapshot(Path(args.current).resolve())
    report = compare_snapshots(baseline=baseline, current=current)

    if args.report:
        write_json(Path(args.report).resolve(), report)
    if args.markdown_report:
        write_markdown_report(Path(args.markdown_report).resolve(), report)
    if args.sarif_report:
        sarif_results = build_sarif_results_for_target(
            target_name="diff",
            report=report,
            source_path=None,
        )
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    print_report(report)
    status_ok = report.get("status") == "pass"
    if status_ok and bool(args.fail_on_warnings):
        status_ok = not bool(get_message_list(report, "warnings"))
    return 0 if status_ok else 1



def command_verify_all(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())

    targets = get_targets_map(config)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    final_status = 0
    aggregate: dict[str, Any] = {
        "status": "pass",
        "generated_at_utc": utc_timestamp_now(),
        "results": {},
    }
    sarif_results: list[dict[str, Any]] = []

    for target_name in sorted(targets.keys()):
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

        aggregate["results"][target_name] = report

        print(
            f"[{target_name}] {report.get('status')} "
            f"(classification={report.get('change_classification')}, required_bump={report.get('required_bump')})"
        )
        if report.get("status") != "pass":
            final_status = 1

        if output_dir:
            write_json(output_dir / f"{target_name}.current.json", current)
            write_json(output_dir / f"{target_name}.report.json", report)
            write_markdown_report(output_dir / f"{target_name}.report.md", report)

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

    aggregate["summary"] = build_aggregate_summary(aggregate["results"])

    if final_status != 0:
        aggregate["status"] = "fail"
    else:
        fail_on_warnings_global = bool(args.fail_on_warnings)
        fail_on_warnings_policy = any(
            bool((report.get("policy") or {}).get("fail_on_warnings", False))
            for report in aggregate["results"].values()
            if isinstance(report, dict)
        )
        if (fail_on_warnings_global or fail_on_warnings_policy) and aggregate["summary"]["warning_count"] > 0:
            final_status = 1
            aggregate["status"] = "fail"

    if final_status != 0 and aggregate["status"] != "fail":
        final_status = 1
        aggregate["status"] = "fail"

    if output_dir:
        write_json(output_dir / "aggregate.report.json", aggregate)
        write_sarif_report(output_dir / "aggregate.report.sarif.json", sarif_results)
    if args.sarif_report:
        write_sarif_report(Path(args.sarif_report).resolve(), sarif_results)

    return final_status


def command_regen_baselines(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    regenerated: list[str] = []
    for target_name in sorted(targets.keys()):
        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        snapshot = build_snapshot(
            config=config,
            target_name=target_name,
            repo_root=repo_root,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
        )
        write_json(baseline_path, snapshot)
        regenerated.append(f"{target_name} -> {baseline_path}")

    for line in regenerated:
        print(f"Regenerated baseline: {line}")

    if bool(args.verify):
        verify_args = argparse.Namespace(
            repo_root=str(repo_root),
            config=str(Path(args.config).resolve()),
            baseline_root=args.baseline_root,
            binary=args.binary,
            skip_binary=args.skip_binary,
            output_dir=args.output_dir,
            sarif_report=args.sarif_report,
            fail_on_warnings=args.fail_on_warnings,
        )
        return command_verify_all(verify_args)

    return 0


