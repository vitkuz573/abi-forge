from __future__ import annotations

import argparse

from ..core import *  # noqa: F401,F403
from .generation import command_codegen, command_sync
from .governance import command_changelog, command_doctor
from .performance import command_benchmark, command_benchmark_gate
from .verification import command_verify_all

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


