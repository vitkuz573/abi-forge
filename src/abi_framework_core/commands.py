from __future__ import annotations

import argparse

from .core import *  # noqa: F401,F403

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


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_release_subjects(repo_root: Path, files: list[Path]) -> list[dict[str, Any]]:
    subjects: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for file_path in files:
        resolved = file_path.resolve()
        if resolved in seen:
            continue
        if not resolved.exists() or not resolved.is_file():
            continue
        seen.add(resolved)
        subjects.append(
            {
                "name": to_repo_relative(resolved, repo_root),
                "digest": {"sha256": sha256_file(resolved)},
                "size_bytes": resolved.stat().st_size,
            }
        )
    return sorted(subjects, key=lambda item: str(item.get("name")))


def write_cyclonedx_sbom(
    *,
    output_path: Path,
    release_tag: str | None,
    generated_at_utc: str,
    subjects: list[dict[str, Any]],
) -> None:
    components: list[dict[str, Any]] = []
    for subject in subjects:
        name = subject.get("name")
        digest = (subject.get("digest") or {}).get("sha256")
        if not isinstance(name, str) or not isinstance(digest, str):
            continue
        components.append(
            {
                "type": "file",
                "name": name,
                "version": digest,
                "hashes": [{"alg": "SHA-256", "content": digest}],
            }
        )

    payload = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "timestamp": generated_at_utc,
            "tools": [{"vendor": "LumenRTC", "name": "abi_framework", "version": TOOL_VERSION}],
            "component": {
                "type": "application",
                "name": "lumenrtc-abi-release",
                "version": release_tag or "unversioned",
            },
        },
        "components": components,
    }
    write_json(output_path, payload)


def write_release_attestation(
    *,
    output_path: Path,
    release_tag: str | None,
    generated_at_utc: str,
    subjects: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> None:
    payload = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [
            {
                "name": item.get("name"),
                "digest": item.get("digest"),
            }
            for item in subjects
        ],
        "predicateType": ATTESTATION_PREDICATE_TYPE,
        "predicate": {
            "buildDefinition": {
                "buildType": ATTESTATION_BUILD_TYPE,
                "externalParameters": {
                    "release_tag": release_tag,
                    **parameters,
                },
            },
            "runDetails": {
                "builder": {
                    "id": "lumenrtc.dev/abi_framework",
                    "version": TOOL_VERSION,
                },
                "metadata": {
                    "invocationId": str(uuid.uuid4()),
                    "finishedOn": generated_at_utc,
                },
            },
        },
    }
    write_json(output_path, payload)


def command_release_prepare(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    output_dir = Path(args.output_dir).resolve() if args.output_dir else (repo_root / "artifacts" / "abi" / "release")
    output_dir.mkdir(parents=True, exist_ok=True)
    benchmark_budget = getattr(args, "benchmark_budget", None)
    emit_sbom = bool(getattr(args, "emit_sbom", False))
    emit_attestation = bool(getattr(args, "emit_attestation", False))

    doctor_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        baseline_root=args.baseline_root,
        binary=args.binary,
        require_baselines=True,
        require_binaries=bool(args.require_binaries),
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    doctor_exit = command_doctor(doctor_args)
    if doctor_exit != 0:
        return doctor_exit

    sync_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        update_baselines=bool(args.update_baselines),
        check=bool(args.check_generated),
        print_diff=bool(args.print_diff),
        no_verify=True,
        fail_on_warnings=bool(args.fail_on_warnings),
        fail_on_sync=bool(args.fail_on_sync),
        output_dir=str(output_dir / "sync"),
        report_json=str(output_dir / "sync.aggregate.report.json"),
    )
    sync_exit = command_sync(sync_args)
    if sync_exit != 0:
        return sync_exit

    codegen_report_path = output_dir / "codegen.aggregate.report.json"
    codegen_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        binary=args.binary,
        skip_binary=args.skip_binary,
        idl_output=None,
        dry_run=False,
        check=bool(args.check_generated),
        print_diff=bool(args.print_diff),
        fail_on_sync=bool(args.fail_on_sync),
        report_json=str(codegen_report_path),
    )
    codegen_exit = command_codegen(codegen_args)
    if codegen_exit != 0:
        return codegen_exit

    verify_output_dir = output_dir / "verify"
    verify_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        output_dir=str(verify_output_dir),
        sarif_report=str(output_dir / "verify.aggregate.report.sarif.json"),
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    verify_exit = command_verify_all(verify_args)
    if verify_exit != 0:
        return verify_exit

    changelog_output = (
        Path(args.changelog_output).resolve()
        if args.changelog_output
        else (repo_root / "abi" / "CHANGELOG.md")
    )
    changelog_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        baseline=None,
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        title=args.title,
        release_tag=args.release_tag,
        output=str(changelog_output),
        report_json=str(output_dir / "changelog.aggregate.report.json"),
        sarif_report=str(output_dir / "changelog.aggregate.report.sarif.json"),
        fail_on_failing=True,
        fail_on_warnings=bool(args.fail_on_warnings),
    )
    changelog_exit = command_changelog(changelog_args)
    if changelog_exit != 0:
        return changelog_exit

    benchmark_report_path = output_dir / "benchmark.aggregate.report.json"
    benchmark_args = argparse.Namespace(
        repo_root=str(repo_root),
        config=str(Path(args.config).resolve()),
        target=None,
        baseline_root=args.baseline_root,
        binary=args.binary,
        skip_binary=args.skip_binary,
        iterations=3,
        output=str(benchmark_report_path),
    )
    benchmark_exit = command_benchmark(benchmark_args)
    if benchmark_exit != 0:
        return benchmark_exit

    benchmark_gate_report_path = None
    if benchmark_budget:
        benchmark_gate_report_path = output_dir / "benchmark.gate.report.json"
        benchmark_gate_args = argparse.Namespace(
            report=str(benchmark_report_path),
            budget=str(Path(benchmark_budget).resolve()),
            output=str(benchmark_gate_report_path),
        )
        benchmark_gate_exit = command_benchmark_gate(benchmark_gate_args)
        if benchmark_gate_exit != 0:
            return benchmark_gate_exit

    verify_aggregate = load_json(verify_output_dir / "aggregate.report.json")
    sync_aggregate = load_json(output_dir / "sync.aggregate.report.json")
    codegen_aggregate = load_json(codegen_report_path)
    changelog_aggregate = load_json(output_dir / "changelog.aggregate.report.json")

    html_output_path = output_dir / "release.prepare.report.html"
    html_output_path.write_text(
        render_release_html_report(
            release_tag=args.release_tag,
            generated_at_utc=utc_timestamp_now(),
            verify_summary=verify_aggregate.get("summary") if isinstance(verify_aggregate, dict) else None,
            sync_summary=sync_aggregate.get("summary") if isinstance(sync_aggregate, dict) else None,
            codegen_summary=codegen_aggregate.get("summary") if isinstance(codegen_aggregate, dict) else None,
            changelog_summary=changelog_aggregate.get("summary") if isinstance(changelog_aggregate, dict) else None,
        ),
        encoding="utf-8",
    )

    manifest = {
        "generated_at_utc": utc_timestamp_now(),
        "release_tag": args.release_tag,
        "artifacts": {
            "output_dir": to_repo_relative(output_dir, repo_root),
            "verify_dir": to_repo_relative(verify_output_dir, repo_root),
            "changelog": to_repo_relative(changelog_output, repo_root),
            "sync_report": to_repo_relative(output_dir / "sync.aggregate.report.json", repo_root),
            "codegen_report": to_repo_relative(codegen_report_path, repo_root),
            "benchmark_report": to_repo_relative(benchmark_report_path, repo_root),
            "html_report": to_repo_relative(html_output_path, repo_root),
            "verify_sarif": to_repo_relative(output_dir / "verify.aggregate.report.sarif.json", repo_root),
            "changelog_report": to_repo_relative(output_dir / "changelog.aggregate.report.json", repo_root),
            "changelog_sarif": to_repo_relative(output_dir / "changelog.aggregate.report.sarif.json", repo_root),
            "benchmark_gate_report": (
                to_repo_relative(benchmark_gate_report_path, repo_root)
                if isinstance(benchmark_gate_report_path, Path)
                else None
            ),
        },
        "options": {
            "update_baselines": bool(args.update_baselines),
            "check_generated": bool(args.check_generated),
            "skip_binary": bool(args.skip_binary),
            "fail_on_warnings": bool(args.fail_on_warnings),
            "benchmark_budget": benchmark_budget,
            "emit_sbom": emit_sbom,
            "emit_attestation": emit_attestation,
        },
        "status": "pass",
    }

    sbom_path = output_dir / "release.sbom.cdx.json"
    attestation_path = output_dir / "release.attestation.json"
    if emit_sbom:
        manifest["artifacts"]["sbom"] = to_repo_relative(sbom_path, repo_root)
    if emit_attestation:
        manifest["artifacts"]["attestation"] = to_repo_relative(attestation_path, repo_root)

    manifest_path = output_dir / "release.prepare.report.json"
    write_json(manifest_path, manifest)

    subject_paths = [
        changelog_output,
        output_dir / "sync.aggregate.report.json",
        codegen_report_path,
        benchmark_report_path,
        html_output_path,
        output_dir / "verify.aggregate.report.sarif.json",
        output_dir / "changelog.aggregate.report.json",
        output_dir / "changelog.aggregate.report.sarif.json",
        verify_output_dir / "aggregate.report.json",
        manifest_path,
    ]
    if isinstance(benchmark_gate_report_path, Path):
        subject_paths.append(benchmark_gate_report_path)

    if emit_sbom:
        sbom_subjects = build_release_subjects(repo_root, subject_paths)
        write_cyclonedx_sbom(
            output_path=sbom_path,
            release_tag=args.release_tag,
            generated_at_utc=utc_timestamp_now(),
            subjects=sbom_subjects,
        )
        subject_paths.append(sbom_path)

    if emit_attestation:
        attestation_subjects = build_release_subjects(repo_root, subject_paths)
        write_release_attestation(
            output_path=attestation_path,
            release_tag=args.release_tag,
            generated_at_utc=utc_timestamp_now(),
            subjects=attestation_subjects,
            parameters={
                "config": to_repo_relative(Path(args.config).resolve(), repo_root),
                "skip_binary": bool(args.skip_binary),
                "update_baselines": bool(args.update_baselines),
                "check_generated": bool(args.check_generated),
                "fail_on_warnings": bool(args.fail_on_warnings),
            },
        )

    print(f"release-prepare completed: {output_dir}")
    return 0


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


def get_targets_map(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config is missing required object: 'targets'.")
    out: dict[str, dict[str, Any]] = {}
    for name, payload in targets.items():
        if isinstance(name, str) and isinstance(payload, dict):
            out[name] = payload
    if not out:
        raise AbiFrameworkError("Config has no valid targets.")
    return out


def command_list_targets(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config).resolve())
    targets = get_targets_map(config)

    for name in sorted(targets.keys()):
        print(name)
    return 0


def resolve_baseline_for_target(repo_root: Path, config: dict[str, Any], target_name: str, baseline_root: str | None) -> Path:
    target = resolve_target(config, target_name)
    if baseline_root:
        return ensure_relative_path(repo_root, f"{baseline_root.rstrip('/')}/{target_name}.json").resolve()

    baseline_path = target.get("baseline_path")
    if isinstance(baseline_path, str) and baseline_path:
        return ensure_relative_path(repo_root, baseline_path).resolve()

    return ensure_relative_path(repo_root, f"abi/baselines/{target_name}.json").resolve()


def resolve_binary_for_target(repo_root: Path, config: dict[str, Any], target_name: str, binary_override: str | None) -> tuple[str | None, bool]:
    if binary_override:
        return binary_override, False
    target = resolve_target(config, target_name)
    binary_cfg = target.get("binary")
    if isinstance(binary_cfg, dict):
        path_value = binary_cfg.get("path")
        if isinstance(path_value, str) and path_value:
            return str(ensure_relative_path(repo_root, path_value).resolve()), False
    return None, True


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


def command_init_target(args: argparse.Namespace) -> int:
    repo_root = Path(args.repo_root).resolve()
    config_path = Path(args.config).resolve()

    if config_path.exists():
        config = load_config(config_path)
    else:
        config = {
            "policy": {
                "waiver_requirements": dict(DEFAULT_WAIVER_REQUIREMENTS),
            },
            "targets": {},
        }

    targets = config.get("targets")
    if not isinstance(targets, dict):
        raise AbiFrameworkError("Config root must contain object 'targets'.")

    if args.target in targets and not args.force:
        raise AbiFrameworkError(
            f"Target '{args.target}' already exists in config. Use --force to overwrite."
        )

    baseline_rel = args.baseline_path or f"abi/baselines/{args.target}.json"

    target_entry: dict[str, Any] = {
        "baseline_path": baseline_rel,
        "header": {
            "path": args.header_path,
            "api_macro": args.api_macro,
            "call_macro": args.call_macro,
            "symbol_prefix": args.symbol_prefix,
            "parser": {
                "backend": "clang_preprocess",
                "compiler": "clang",
                "compiler_candidates": default_parser_compiler_candidates_for_config(),
                "args": [],
                "include_dirs": [],
                "fallback_to_regex": True,
            },
            "version_macros": {
                "major": args.version_major_macro,
                "minor": args.version_minor_macro,
                "patch": args.version_patch_macro,
            },
            "types": {
                "enable_enums": True,
                "enable_structs": True,
                "enum_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "struct_name_pattern": f"^{re.escape(args.symbol_prefix)}",
                "ignore_enums": [],
                "ignore_structs": [],
                "struct_tail_addition_is_breaking": True,
            },
        },
        "codegen": {
            "enabled": True,
            "idl_output_path": f"abi/generated/{args.target}.idl.json",
        },
    }

    if args.binding_symbol:
        target_entry["bindings"] = {
            "expected_symbols": args.binding_symbol,
        }

    if args.binary_path:
        target_entry["binary"] = {
            "path": args.binary_path,
            "allow_non_prefixed_exports": False,
        }

    targets[args.target] = target_entry
    if not isinstance(config.get("policy"), dict):
        config["policy"] = {}
    root_policy = config["policy"]
    if not isinstance(root_policy.get("waiver_requirements"), dict):
        root_policy["waiver_requirements"] = dict(DEFAULT_WAIVER_REQUIREMENTS)
    config["targets"] = targets
    write_json(config_path, config)

    if args.create_baseline:
        snapshot = build_snapshot(
            config=config,
            target_name=args.target,
            repo_root=repo_root,
            binary_override=None,
            skip_binary=True,
        )
        baseline_path = ensure_relative_path(repo_root, baseline_rel).resolve()
        write_json(baseline_path, snapshot)
        print(f"Created baseline: {baseline_path}")

    print(f"Target '{args.target}' initialized in {config_path}")
    return 0


