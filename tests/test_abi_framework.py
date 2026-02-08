from __future__ import annotations

import argparse
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import abi_framework  # noqa: E402


def make_snapshot(version: tuple[int, int, int], functions: dict[str, str]) -> dict[str, object]:
    symbols = sorted(functions.keys())
    return {
        "tool": {"name": "abi_framework", "version": "test"},
        "target": "demo",
        "generated_at_utc": "2026-01-01T00:00:00Z",
        "abi_version": {"major": version[0], "minor": version[1], "patch": version[2]},
        "header": {
            "symbols": symbols,
            "functions": {
                name: {
                    "return_type": "int",
                    "parameters": signature,
                    "signature": f"int ({signature})",
                }
                for name, signature in functions.items()
            },
            "enums": {},
            "structs": {},
        },
        "bindings": {
            "available": True,
            "source": "test",
            "symbol_count": len(symbols),
            "symbols": symbols,
        },
        "binary": {"available": False, "symbols": [], "skipped": True},
    }


class AbiFrameworkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        self._create_demo_repo()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_demo_repo(self) -> None:
        (self.repo_root / "native" / "include").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "abi" / "baselines").mkdir(parents=True, exist_ok=True)

        header = """#ifndef DEMO_H
#define DEMO_H
#include <stdint.h>
#define MY_ABI_VERSION_MAJOR 1
#define MY_ABI_VERSION_MINOR 0
#define MY_ABI_VERSION_PATCH 0
#define MY_API
#define MY_CALL
typedef enum my_result_t {
  MY_OK = 0,
  MY_ERROR = 1
} my_result_t;
MY_API my_result_t MY_CALL my_init(void);
MY_API int MY_CALL my_add(int a, int b);
#endif
"""
        config = {
            "targets": {
                "demo": {
                    "baseline_path": "abi/baselines/demo.json",
                    "header": {
                        "path": "native/include/demo.h",
                        "api_macro": "MY_API",
                        "call_macro": "MY_CALL",
                        "symbol_prefix": "my_",
                        "version_macros": {
                            "major": "MY_ABI_VERSION_MAJOR",
                            "minor": "MY_ABI_VERSION_MINOR",
                            "patch": "MY_ABI_VERSION_PATCH",
                        },
                    },
                    "bindings": {
                        "expected_symbols": ["my_add", "my_init"],
                    },
                    "codegen": {
                        "enabled": True,
                        "idl_output_path": "abi/generated/demo/demo.idl.json",
                    },
                }
            }
        }

        (self.repo_root / "native" / "include" / "demo.h").write_text(header, encoding="utf-8")
        abi_framework.write_json(self.repo_root / "abi" / "config.json", config)

        loaded = abi_framework.load_config(self.repo_root / "abi" / "config.json")
        snapshot = abi_framework.build_snapshot(
            config=loaded,
            target_name="demo",
            repo_root=self.repo_root,
            binary_override=None,
            skip_binary=True,
        )
        abi_framework.write_json(self.repo_root / "abi" / "baselines" / "demo.json", snapshot)

    def test_compare_snapshots_detects_breaking_change(self) -> None:
        baseline = make_snapshot((1, 0, 0), {"my_init": "void", "my_add": "int a, int b"})
        current = make_snapshot((2, 0, 0), {"my_init": "void"})
        report = abi_framework.compare_snapshots(baseline=baseline, current=current)
        self.assertEqual(report["change_classification"], "breaking")
        self.assertEqual(report["required_bump"], "major")
        self.assertEqual(report["status"], "pass")

    def test_generate_writes_idl(self) -> None:
        self._run_generate_for_demo()
        idl_path = self.repo_root / "abi" / "generated" / "demo" / "demo.idl.json"
        self.assertTrue(idl_path.exists())
        self.assertIn("my_init", idl_path.read_text(encoding="utf-8"))
        self.assertIn("my_add", idl_path.read_text(encoding="utf-8"))

    def _run_generate_for_demo(self) -> None:
        exit_code = abi_framework.command_generate(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                binary=None,
                skip_binary=True,
                idl_output=None,
                dry_run=False,
                check=False,
                print_diff=False,
                report_json=None,
                fail_on_sync=False,
            )
        )
        self.assertEqual(exit_code, 0)

    def test_sync_check_detects_codegen_drift(self) -> None:
        self._run_generate_for_demo()
        idl_path = self.repo_root / "abi" / "generated" / "demo" / "demo.idl.json"
        idl_path.write_text("{}\n", encoding="utf-8")

        exit_code = abi_framework.command_sync(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                baseline_root=None,
                binary=None,
                skip_binary=True,
                update_baselines=False,
                check=True,
                print_diff=False,
                no_verify=True,
                fail_on_warnings=False,
                fail_on_sync=False,
                output_dir=None,
                report_json=None,
            )
        )
        self.assertEqual(exit_code, 1)

    def test_release_prepare_smoke(self) -> None:
        self._run_generate_for_demo()
        changelog_path = self.repo_root / "abi" / "CHANGELOG.md"
        output_dir = self.repo_root / "artifacts" / "release"
        exit_code = abi_framework.command_release_prepare(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                baseline_root=None,
                binary=None,
                skip_binary=True,
                require_binaries=False,
                update_baselines=False,
                check_generated=True,
                print_diff=False,
                fail_on_sync=True,
                fail_on_warnings=False,
                release_tag="v1.0.0",
                title="ABI Changelog",
                changelog_output=str(changelog_path),
                output_dir=str(output_dir),
            )
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue((output_dir / "release.prepare.report.json").exists())
        self.assertTrue(changelog_path.exists())


if __name__ == "__main__":
    unittest.main()
