from __future__ import annotations

import argparse
import sys

from .core import AbiFrameworkError
from .commands import (
    command_benchmark,
    command_benchmark_gate,
    command_changelog,
    command_codegen,
    command_diff,
    command_doctor,
    command_generate,
    command_init_target,
    command_list_targets,
    command_regen_baselines,
    command_release_prepare,
    command_snapshot,
    command_sync,
    command_validate_plugin_manifest,
    command_verify,
    command_verify_all,
    command_waiver_audit,
)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="abi_framework",
        description="Config-driven ABI governance framework (snapshot/verify/diff/bootstrap/release).",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    snapshot = sub.add_parser("snapshot", help="Generate current ABI snapshot.")
    snapshot.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    snapshot.add_argument("--config", required=True, help="Path to ABI config JSON.")
    snapshot.add_argument("--target", required=True, help="Target name from config targets map.")
    snapshot.add_argument("--binary", help="Override binary path for export checks.")
    snapshot.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    snapshot.add_argument("--output", help="Write snapshot JSON to path.")
    snapshot.set_defaults(func=command_snapshot)

    verify = sub.add_parser("verify", help="Compare current ABI state with baseline snapshot.")
    verify.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    verify.add_argument("--config", required=True, help="Path to ABI config JSON.")
    verify.add_argument("--target", required=True, help="Target name from config targets map.")
    verify.add_argument("--baseline", required=True, help="Path to baseline snapshot JSON.")
    verify.add_argument("--binary", help="Override binary path for export checks.")
    verify.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    verify.add_argument("--current-output", help="Write current snapshot JSON to path.")
    verify.add_argument("--report", help="Write verify report JSON to path.")
    verify.add_argument("--markdown-report", help="Write verify report as Markdown.")
    verify.add_argument("--sarif-report", help="Write verify report as SARIF (for CI/code scanning).")
    verify.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    verify.set_defaults(func=command_verify)

    verify_all = sub.add_parser("verify-all", help="Verify all targets from config.")
    verify_all.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    verify_all.add_argument("--config", required=True, help="Path to ABI config JSON.")
    verify_all.add_argument("--baseline-root", help="Baseline directory (default: target baseline_path or abi/baselines).")
    verify_all.add_argument("--binary", help="Override binary path for export checks (applies to each target).")
    verify_all.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction for all targets.")
    verify_all.add_argument("--output-dir", help="Directory to write per-target current/report artifacts.")
    verify_all.add_argument("--sarif-report", help="Write aggregate SARIF report.")
    verify_all.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    verify_all.set_defaults(func=command_verify_all)

    regen = sub.add_parser("regen-baselines", help="Regenerate baseline snapshots for all targets.")
    regen.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    regen.add_argument("--config", required=True, help="Path to ABI config JSON.")
    regen.add_argument("--baseline-root", help="Baseline directory override (otherwise target baseline_path is used).")
    regen.add_argument("--binary", help="Override binary path for export checks.")
    regen.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction while regenerating.")
    regen.add_argument("--verify", action="store_true", help="Run verify-all after regeneration.")
    regen.add_argument("--output-dir", help="Verification output dir (effective with --verify).")
    regen.add_argument("--sarif-report", help="Verification SARIF output (effective with --verify).")
    regen.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures during --verify.")
    regen.set_defaults(func=command_regen_baselines)

    doctor = sub.add_parser("doctor", help="Run ABI environment/config diagnostics.")
    doctor.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    doctor.add_argument("--config", required=True, help="Path to ABI config JSON.")
    doctor.add_argument("--baseline-root", help="Baseline directory override.")
    doctor.add_argument("--binary", help="Override binary path for checks.")
    doctor.add_argument("--require-baselines", action="store_true", help="Fail if any baseline is missing.")
    doctor.add_argument("--require-binaries", action="store_true", help="Fail if any binary is missing/unconfigured.")
    doctor.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    doctor.set_defaults(func=command_doctor)

    waiver_audit = sub.add_parser("waiver-audit", help="Audit waiver metadata, expiry, and policy compliance.")
    waiver_audit.add_argument("--config", required=True, help="Path to ABI config JSON.")
    waiver_audit.add_argument("--target", help="Optional target name. If omitted, all targets are audited.")
    waiver_audit.add_argument("--output", help="Write waiver audit report JSON to path.")
    waiver_audit.add_argument("--print-json", action="store_true", help="Print aggregate waiver audit JSON.")
    waiver_audit.add_argument("--fail-on-expired", action="store_true", help="Fail when expired waivers are present.")
    waiver_audit.add_argument("--fail-on-missing-metadata", action="store_true", help="Fail when waiver metadata requirements are not met.")
    waiver_audit.add_argument("--fail-on-expiring-soon", action="store_true", help="Fail when waivers are close to expiration.")
    waiver_audit.set_defaults(func=command_waiver_audit)

    changelog = sub.add_parser("changelog", help="Generate markdown ABI changelog from baseline vs current.")
    changelog.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    changelog.add_argument("--config", required=True, help="Path to ABI config JSON.")
    changelog.add_argument("--target", help="Optional target name. If omitted, all targets are included.")
    changelog.add_argument("--baseline", help="Explicit baseline path (only valid with --target).")
    changelog.add_argument("--baseline-root", help="Baseline directory override.")
    changelog.add_argument("--binary", help="Override binary path for export checks.")
    changelog.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    changelog.add_argument("--title", default="ABI Changelog", help="Changelog title.")
    changelog.add_argument("--release-tag", help="Optional release tag (display only).")
    changelog.add_argument("--output", help="Write markdown changelog to file.")
    changelog.add_argument("--report-json", help="Write aggregate changelog data as JSON.")
    changelog.add_argument("--sarif-report", help="Write SARIF for changelog diagnostics.")
    changelog.add_argument("--fail-on-failing", action="store_true", help="Return non-zero if any target report failed.")
    changelog.add_argument("--fail-on-warnings", action="store_true", help="Return non-zero if warnings are present.")
    changelog.set_defaults(func=command_changelog)

    generate = sub.add_parser("generate", help="Generate ABI IDL artifacts.")
    generate.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    generate.add_argument("--config", required=True, help="Path to ABI config JSON.")
    generate.add_argument("--target", help="Optional target name. If omitted, all targets are processed.")
    generate.add_argument("--binary", help="Override binary path for export checks.")
    generate.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    generate.add_argument("--idl-output", help="IDL output path (single-target mode only).")
    generate.add_argument("--dry-run", action="store_true", help="Do not write files; only compute outputs.")
    generate.add_argument("--check", action="store_true", help="Fail when generated artifacts drift from files on disk.")
    generate.add_argument("--print-diff", action="store_true", help="Print unified diff for changed artifacts.")
    generate.add_argument("--report-json", help="Write aggregate generation report JSON.")
    generate.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbol contract.")
    generate.set_defaults(func=command_generate)

    codegen = sub.add_parser("codegen", help="Run ABI IDL generation and configured language generators.")
    codegen.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    codegen.add_argument("--config", required=True, help="Path to ABI config JSON.")
    codegen.add_argument("--target", help="Optional target name. If omitted, all targets are processed.")
    codegen.add_argument("--binary", help="Override binary path for export checks.")
    codegen.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    codegen.add_argument("--idl-output", help="IDL output path (single-target mode only).")
    codegen.add_argument("--dry-run", action="store_true", help="Do not write files; run generators in dry-run mode if supported.")
    codegen.add_argument("--check", action="store_true", help="Fail when generated artifacts or generators are not up-to-date.")
    codegen.add_argument("--print-diff", action="store_true", help="Print unified diff for changed artifacts.")
    codegen.add_argument("--report-json", help="Write aggregate codegen report JSON.")
    codegen.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbol contract.")
    codegen.set_defaults(func=command_codegen)

    sync = sub.add_parser("sync", help="Sync generated ABI artifacts and optionally baselines.")
    sync.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    sync.add_argument("--config", required=True, help="Path to ABI config JSON.")
    sync.add_argument("--target", help="Optional target name. If omitted, all targets are processed.")
    sync.add_argument("--baseline-root", help="Baseline directory override.")
    sync.add_argument("--binary", help="Override binary path for export checks.")
    sync.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    sync.add_argument("--update-baselines", action="store_true", help="Write current snapshots to baseline files.")
    sync.add_argument("--check", action="store_true", help="Check mode: do not write files and fail on drift.")
    sync.add_argument("--print-diff", action="store_true", help="Print unified diff for changed artifacts.")
    sync.add_argument("--no-verify", action="store_true", help="Skip baseline comparison and policy checks.")
    sync.add_argument("--fail-on-warnings", action="store_true", help="Treat ABI warnings as failures.")
    sync.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbol contract.")
    sync.add_argument("--output-dir", help="Directory for sync reports.")
    sync.add_argument("--report-json", help="Write aggregate sync report JSON.")
    sync.set_defaults(func=command_sync)

    release_prepare = sub.add_parser("release-prepare", help="Run end-to-end ABI release preparation pipeline.")
    release_prepare.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    release_prepare.add_argument("--config", required=True, help="Path to ABI config JSON.")
    release_prepare.add_argument("--baseline-root", help="Baseline directory override.")
    release_prepare.add_argument("--binary", help="Override binary path for export checks.")
    release_prepare.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    release_prepare.add_argument("--require-binaries", action="store_true", help="Require binaries to be present during doctor checks.")
    release_prepare.add_argument("--update-baselines", action="store_true", help="Refresh baselines before verification.")
    release_prepare.add_argument("--check-generated", action="store_true", help="Fail if generated artifacts are out of date.")
    release_prepare.add_argument("--print-diff", action="store_true", help="Print unified diff for generated artifact drift.")
    release_prepare.add_argument("--fail-on-sync", action="store_true", help="Fail if generated ABI symbols drift from configured bindings symbol contract.")
    release_prepare.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    release_prepare.add_argument("--release-tag", help="Release tag displayed in changelog/report.")
    release_prepare.add_argument("--title", default="ABI Changelog", help="Changelog title.")
    release_prepare.add_argument("--changelog-output", help="Path for markdown changelog output.")
    release_prepare.add_argument("--output-dir", help="Directory for release preparation artifacts.")
    release_prepare.add_argument("--benchmark-budget", help="Optional performance budget JSON for benchmark gate.")
    release_prepare.add_argument("--emit-sbom", action="store_true", help="Emit CycloneDX SBOM for release artifacts.")
    release_prepare.add_argument("--emit-attestation", action="store_true", help="Emit in-toto/SLSA-style provenance attestation.")
    release_prepare.set_defaults(func=command_release_prepare)

    diff = sub.add_parser("diff", help="Compare two snapshot files.")
    diff.add_argument("--baseline", required=True, help="Path to baseline snapshot JSON.")
    diff.add_argument("--current", required=True, help="Path to current snapshot JSON.")
    diff.add_argument("--report", help="Write diff report JSON to path.")
    diff.add_argument("--markdown-report", help="Write diff report as Markdown.")
    diff.add_argument("--sarif-report", help="Write diff report as SARIF.")
    diff.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    diff.set_defaults(func=command_diff)

    benchmark = sub.add_parser("benchmark", help="Benchmark ABI pipeline timings.")
    benchmark.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve relative paths (default: current directory).",
    )
    benchmark.add_argument("--config", required=True, help="Path to ABI config JSON.")
    benchmark.add_argument("--target", help="Optional target name. If omitted, all targets are benchmarked.")
    benchmark.add_argument("--baseline-root", help="Baseline directory override.")
    benchmark.add_argument("--binary", help="Override binary path for export checks.")
    benchmark.add_argument("--skip-binary", action="store_true", help="Skip binary export extraction.")
    benchmark.add_argument("--iterations", default=3, type=int, help="Iterations per target (default: 3).")
    benchmark.add_argument("--output", help="Write benchmark report JSON to path.")
    benchmark.set_defaults(func=command_benchmark)

    benchmark_gate = sub.add_parser("benchmark-gate", help="Enforce benchmark report against performance budgets.")
    benchmark_gate.add_argument("--report", required=True, help="Benchmark report JSON path (from benchmark command).")
    benchmark_gate.add_argument("--budget", required=True, help="Budget JSON path.")
    benchmark_gate.add_argument("--output", help="Write gate report JSON path.")
    benchmark_gate.set_defaults(func=command_benchmark_gate)

    plugin_manifest = sub.add_parser("validate-plugin-manifest", help="Validate external plugin manifest JSON.")
    plugin_manifest.add_argument(
        "--manifest",
        action="append",
        help="Path to plugin manifest JSON (repeatable).",
    )
    plugin_manifest.add_argument(
        "--config",
        help="Optional ABI config JSON. When provided, manifests are auto-discovered from external generators.",
    )
    plugin_manifest.add_argument(
        "--repo-root",
        default=".",
        help="Repository root used to resolve generator command paths in --config mode.",
    )
    plugin_manifest.add_argument(
        "--target",
        help="Optional target name filter for --config mode.",
    )
    plugin_manifest.add_argument("--output", help="Write validation report JSON path.")
    plugin_manifest.add_argument("--print-json", action="store_true", help="Print validation report JSON.")
    plugin_manifest.add_argument("--fail-on-warnings", action="store_true", help="Treat warnings as failures.")
    plugin_manifest.set_defaults(func=command_validate_plugin_manifest)

    list_targets = sub.add_parser("list-targets", help="List target names from config.")
    list_targets.add_argument("--config", required=True, help="Path to ABI config JSON.")
    list_targets.set_defaults(func=command_list_targets)

    init_target = sub.add_parser("init-target", help="Bootstrap a new ABI target in config and create baseline.")
    init_target.add_argument("--repo-root", default=".", help="Repository root for relative path resolution.")
    init_target.add_argument("--config", required=True, help="Path to ABI config JSON.")
    init_target.add_argument("--target", required=True, help="Target name to initialize.")
    init_target.add_argument("--header-path", required=True, help="Header path relative to repo root.")
    init_target.add_argument("--api-macro", default="LUMENRTC_API", help="Export macro used in ABI declarations.")
    init_target.add_argument("--call-macro", default="LUMENRTC_CALL", help="Calling convention macro used in ABI declarations.")
    init_target.add_argument("--symbol-prefix", default="lrtc_", help="ABI symbol prefix (for function export matching).")
    init_target.add_argument("--version-major-macro", required=True, help="ABI major version macro name.")
    init_target.add_argument("--version-minor-macro", required=True, help="ABI minor version macro name.")
    init_target.add_argument("--version-patch-macro", required=True, help="ABI patch version macro name.")
    init_target.add_argument("--binding-symbol", action="append", help="Optional symbol_contract symbol (repeatable).")
    init_target.add_argument("--binary-path", help="Optional native binary path.")
    init_target.add_argument("--baseline-path", help="Baseline path (default abi/baselines/<target>.json).")
    init_target.add_argument("--no-create-baseline", action="store_true", help="Do not create baseline immediately.")
    init_target.add_argument("--force", action="store_true", help="Overwrite target if already exists.")
    init_target.set_defaults(func=command_init_target)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if hasattr(args, "no_create_baseline"):
        args.create_baseline = not bool(args.no_create_baseline)

    try:
        return int(args.func(args))
    except AbiFrameworkError as exc:
        print(f"abi_framework error: {exc}", file=sys.stderr)
        return 2



if __name__ == "__main__":
    raise SystemExit(main())
