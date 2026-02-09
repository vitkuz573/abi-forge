from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .common import resolve_baseline_for_target

def summarize_timings(values: list[float]) -> dict[str, Any]:
    if not values:
        return {
            "count": 0,
            "min_ms": 0.0,
            "max_ms": 0.0,
            "mean_ms": 0.0,
            "p95_ms": 0.0,
        }
    ordered = sorted(values)
    count = len(ordered)
    p95_index = max(0, int(round((count - 1) * 0.95)))
    return {
        "count": count,
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "mean_ms": round(sum(ordered) / count, 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def command_benchmark(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config = load_config(Path(args.config).resolve())
    target_names = resolve_target_names(config=config, target_name=args.target)
    iterations = max(1, int(args.iterations))

    baseline_root = args.baseline_root
    aggregate: dict[str, Any] = {
        "tool": {"name": "abi_framework", "version": TOOL_VERSION},
        "generated_at_utc": utc_timestamp_now(),
        "iterations": iterations,
        "targets": {},
    }

    for target_name in target_names:
        snapshot_timings: list[float] = []
        verify_timings: list[float] = []
        idl_timings: list[float] = []
        symbol_counts: list[int] = []
        baseline_path = resolve_baseline_for_target(
            repo_root=repo_root,
            config=config,
            target_name=target_name,
            baseline_root=baseline_root,
        )
        baseline_payload = load_snapshot(baseline_path) if baseline_path.exists() else None

        for _ in range(iterations):
            start = time.perf_counter()
            snapshot = build_snapshot(
                config=config,
                target_name=target_name,
                repo_root=repo_root,
                binary_override=args.binary,
                skip_binary=bool(args.skip_binary),
            )
            snapshot_timings.append((time.perf_counter() - start) * 1000.0)
            symbol_counts.append(int((snapshot.get("header") or {}).get("function_count") or 0))

            if baseline_payload is not None:
                start = time.perf_counter()
                raw_report = compare_snapshots(baseline=baseline_payload, current=snapshot)
                effective_policy = resolve_effective_policy(config=config, target_name=target_name)
                _ = apply_policy_to_report(report=raw_report, policy=effective_policy, target_name=target_name)
                verify_timings.append((time.perf_counter() - start) * 1000.0)

            start = time.perf_counter()
            _ = build_codegen_for_target(
                repo_root=repo_root,
                config=config,
                target_name=target_name,
                binary_override=args.binary,
                skip_binary=bool(args.skip_binary),
                idl_output_override=None,
                dry_run=True,
                check=False,
                print_diff=False,
            )
            idl_timings.append((time.perf_counter() - start) * 1000.0)

        aggregate["targets"][target_name] = {
            "snapshot_ms": summarize_timings(snapshot_timings),
            "verify_ms": summarize_timings(verify_timings),
            "generate_idl_ms": summarize_timings(idl_timings),
            "symbol_count": summarize_timings([float(v) for v in symbol_counts]),
            "baseline_present": baseline_payload is not None,
        }

    if args.output:
        write_json(Path(args.output).resolve(), aggregate)
    else:
        print(json.dumps(aggregate, indent=2, sort_keys=True))
    return 0


def command_benchmark_gate(args: argparse.Namespace) -> int:
    report_path = Path(args.report).resolve()
    budget_path = Path(args.budget).resolve()
    if not report_path.exists():
        raise AbiFrameworkError(f"benchmark report file not found: {report_path}")
    if not budget_path.exists():
        raise AbiFrameworkError(f"benchmark budget file not found: {budget_path}")

    report = load_json(report_path)
    budget = load_json(budget_path)

    report_targets = report.get("targets")
    budget_targets = budget.get("targets")
    if not isinstance(report_targets, dict):
        raise AbiFrameworkError("benchmark report is missing object field 'targets'")
    if not isinstance(budget_targets, dict):
        raise AbiFrameworkError("benchmark budget is missing object field 'targets'")

    violations: list[dict[str, Any]] = []
    for target_name, target_budget in sorted(budget_targets.items()):
        if not isinstance(target_budget, dict):
            raise AbiFrameworkError(f"budget.targets.{target_name} must be an object")
        target_report = report_targets.get(target_name)
        if not isinstance(target_report, dict):
            violations.append(
                {
                    "target": target_name,
                    "metric": "*",
                    "field": "*",
                    "actual": None,
                    "threshold": None,
                    "message": "target is missing in benchmark report",
                }
            )
            continue
        for metric_name, metric_budget in sorted(target_budget.items()):
            if not isinstance(metric_budget, dict):
                raise AbiFrameworkError(f"budget.targets.{target_name}.{metric_name} must be an object")
            metric_report = target_report.get(metric_name)
            if not isinstance(metric_report, dict):
                violations.append(
                    {
                        "target": target_name,
                        "metric": metric_name,
                        "field": "*",
                        "actual": None,
                        "threshold": None,
                        "message": "metric missing in benchmark report",
                    }
                )
                continue
            for limit_field, threshold in sorted(metric_budget.items()):
                if not isinstance(threshold, (int, float)):
                    raise AbiFrameworkError(
                        f"budget.targets.{target_name}.{metric_name}.{limit_field} must be numeric"
                    )
                if not limit_field.endswith("_max"):
                    raise AbiFrameworkError(
                        f"unsupported budget field '{limit_field}' for {target_name}.{metric_name}; expected '*_max'"
                    )
                metric_field = limit_field[: -len("_max")]
                actual = metric_report.get(metric_field)
                if not isinstance(actual, (int, float)):
                    violations.append(
                        {
                            "target": target_name,
                            "metric": metric_name,
                            "field": metric_field,
                            "actual": actual,
                            "threshold": threshold,
                            "message": "metric field missing or non-numeric",
                        }
                    )
                    continue
                if float(actual) > float(threshold):
                    violations.append(
                        {
                            "target": target_name,
                            "metric": metric_name,
                            "field": metric_field,
                            "actual": float(actual),
                            "threshold": float(threshold),
                            "message": "performance budget exceeded",
                        }
                    )

    gate_report = {
        "generated_at_utc": utc_timestamp_now(),
        "report": str(report_path),
        "budget": str(budget_path),
        "violations": violations,
        "status": "pass" if not violations else "fail",
    }
    if args.output:
        write_json(Path(args.output).resolve(), gate_report)
    if not violations:
        print("benchmark-gate: pass")
        return 0
    print(f"benchmark-gate: fail ({len(violations)} violation(s))")
    for item in violations:
        print(
            f"  [{item.get('target')}] {item.get('metric')}.{item.get('field')}: "
            f"{item.get('actual')} > {item.get('threshold')} ({item.get('message')})"
        )
    return 1


