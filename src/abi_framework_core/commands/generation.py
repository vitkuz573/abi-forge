from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .common import resolve_baseline_for_target

def command_generate(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)

    if args.idl_output and len(target_names) != 1:
        raise AbiFrameworkError("--idl-output can only be used with a single target via --target.")

    aggregate: dict[str, Any] = {
        "generated_at_utc": utc_timestamp_now(),
        "results": {},
    }
    exit_code = 0

    for target_name in target_names:
        result = build_codegen_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
            idl_output_override=args.idl_output,
            dry_run=args.dry_run,
            check=args.check,
            print_diff=args.print_diff,
        )
        aggregate["results"][target_name] = {
            "artifacts": result["artifacts"],
            "sync": result["sync"],
            "has_codegen_drift": result["has_codegen_drift"],
            "has_sync_drift": result["has_sync_drift"],
            "abi_version": result["snapshot"].get("abi_version"),
        }

        artifacts = result["artifacts"]
        idl_status = ((artifacts.get("idl") or {}).get("status")) or "unknown"
        native_header_status = ((artifacts.get("native_header") or {}).get("status")) or "n/a"
        native_map_status = ((artifacts.get("native_export_map") or {}).get("status")) or "n/a"
        print(f"[{target_name}] generate: idl={idl_status} native_header={native_header_status} map={native_map_status}")
        print_sync_comparison(target_name, result["sync"])

        if args.check and result["has_codegen_drift"]:
            exit_code = 1
        if bool(args.fail_on_sync) and result["has_sync_drift"]:
            exit_code = 1

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    return exit_code



def command_codegen(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)
    if args.idl_output and len(target_names) != 1:
        raise AbiFrameworkError("--idl-output can only be used with a single target via --target.")

    aggregate: dict[str, Any] = {
        "generated_at_utc": utc_timestamp_now(),
        "results": {},
        "summary": {
            "target_count": len(target_names),
            "generator_fail_count": 0,
            "warning_count": 0,
        },
    }
    exit_code = 0

    for target_name in target_names:
        target = resolve_target(config, target_name)
        generated = build_codegen_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
            skip_binary=bool(args.skip_binary),
            idl_output_override=args.idl_output,
            dry_run=bool(args.dry_run),
            check=bool(args.check),
            print_diff=bool(args.print_diff),
        )

        idl_path = generated.get("idl_output_path_abs")
        if not isinstance(idl_path, Path):
            raise AbiFrameworkError(f"internal error: no idl path resolved for target '{target_name}'")

        generator_results = run_code_generators_for_target(
            repo_root=repo_root,
            target_name=target_name,
            target=target,
            idl_path=idl_path,
            check=bool(args.check),
            dry_run=bool(args.dry_run),
        )
        generator_failed = any(item.get("status") != "pass" for item in generator_results)
        if generator_failed:
            exit_code = 1
            aggregate["summary"]["generator_fail_count"] += 1

        if bool(args.check) and bool(generated.get("has_codegen_drift")):
            exit_code = 1
        if bool(args.fail_on_sync) and bool(generated.get("has_sync_drift")):
            exit_code = 1

        artifacts = generated.get("artifacts") if isinstance(generated, dict) else {}
        if not isinstance(artifacts, dict):
            artifacts = {}
        idl_status = ((artifacts.get("idl") or {}).get("status")) or "unknown"
        native_header_status = ((artifacts.get("native_header") or {}).get("status")) or "n/a"
        native_map_status = ((artifacts.get("native_export_map") or {}).get("status")) or "n/a"
        print(f"[{target_name}] idl={idl_status} native_header={native_header_status} map={native_map_status}")
        if not generator_results:
            print(f"[{target_name}] generator: none configured")
        for item in generator_results:
            print(f"[{target_name}] generator {item.get('name')}: {item.get('status')}")
            if item.get("status") != "pass":
                stderr = str(item.get("stderr") or "")
                if stderr:
                    print(f"  stderr: {stderr}")

        aggregate["results"][target_name] = {
            "idl": generated.get("artifacts"),
            "sync": generated.get("sync"),
            "has_codegen_drift": generated.get("has_codegen_drift"),
            "has_sync_drift": generated.get("has_sync_drift"),
            "generators": generator_results,
        }

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    return exit_code


def command_sync(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else None
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    aggregate: dict[str, Any] = {
        "generated_at_utc": utc_timestamp_now(),
        "results": {},
    }
    final_status = 0

    for target_name in target_names:
        result = build_codegen_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            binary_override=args.binary,
            skip_binary=args.skip_binary,
            idl_output_override=None,
            dry_run=bool(args.check),
            check=bool(args.check),
            print_diff=bool(args.print_diff),
        )

        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=args.baseline_root,
        )
        baseline_written = False
        if bool(args.update_baselines) and not bool(args.check):
            write_json(baseline_path, result["snapshot"])
            baseline_written = True
            print(f"[{target_name}] baseline updated: {baseline_path}")

        report: dict[str, Any] | None = None
        if not bool(args.no_verify):
            if not baseline_path.exists():
                raise AbiFrameworkError(f"Baseline does not exist for target '{target_name}': {baseline_path}")
            baseline = load_snapshot(baseline_path)
            raw_report = compare_snapshots(baseline=baseline, current=result["snapshot"])
            effective_policy = resolve_effective_policy(config=config, target_name=target_name)
            report = apply_policy_to_report(
                report=raw_report,
                policy=effective_policy,
                target_name=target_name,
            )
            print(
                f"[{target_name}] verify={report.get('status')} "
                f"classification={report.get('change_classification')} "
                f"required_bump={report.get('required_bump')}"
            )
            if report.get("status") != "pass":
                final_status = 1
            if bool(args.fail_on_warnings) and get_message_list(report, "warnings"):
                final_status = 1

        if bool(args.check) and bool(result["has_codegen_drift"]):
            final_status = 1
        if bool(args.fail_on_sync) and bool(result["has_sync_drift"]):
            final_status = 1

        aggregate["results"][target_name] = {
            "artifacts": result["artifacts"],
            "sync": result["sync"],
            "has_codegen_drift": result["has_codegen_drift"],
            "has_sync_drift": result["has_sync_drift"],
            "baseline_path": to_repo_relative(baseline_path, repo_root),
            "baseline_updated": baseline_written,
            "verify_report": report,
        }

        if output_dir:
            if report is not None:
                write_json(output_dir / f"{target_name}.sync.verify.report.json", report)
                write_markdown_report(output_dir / f"{target_name}.sync.verify.report.md", report)
            write_json(output_dir / f"{target_name}.sync.codegen.report.json", aggregate["results"][target_name])

    aggregate["summary"] = {
        "target_count": len(target_names),
        "codegen_drift_count": sum(
            1 for item in aggregate["results"].values() if isinstance(item, dict) and bool(item.get("has_codegen_drift"))
        ),
        "sync_drift_count": sum(
            1 for item in aggregate["results"].values() if isinstance(item, dict) and bool(item.get("has_sync_drift"))
        ),
    }

    if args.report_json:
        write_json(Path(args.report_json).resolve(), aggregate)
    if output_dir:
        write_json(output_dir / "sync.aggregate.report.json", aggregate)

    return final_status

