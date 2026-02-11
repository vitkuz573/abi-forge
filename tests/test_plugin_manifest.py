from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import abi_framework_core as abi_framework  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_ROOT = Path(__file__).resolve().parent / "conformance"
VALID_FIXTURE = FIXTURES_ROOT / "plugin_manifest.valid.json"
INVALID_FIXTURE = FIXTURES_ROOT / "plugin_manifest.invalid.json"
LUMENRTC_PLUGIN_MANIFEST = REPO_ROOT / "tools" / "lumenrtc_codegen" / "plugin.manifest.json"
SDK_PLUGIN_MANIFEST = REPO_ROOT / "tools" / "abi_framework" / "generator_sdk" / "plugin.manifest.json"
ABI_FRAMEWORK_ENTRYPOINT = REPO_ROOT / "tools" / "abi_framework" / "abi_framework.py"
ABI_CONFIG_PATH = REPO_ROOT / "abi" / "config.json"


class PluginManifestValidationTests(unittest.TestCase):
    def test_validate_conformance_valid_fixture(self) -> None:
        exit_code = abi_framework.command_validate_plugin_manifest(
            argparse.Namespace(
                manifest=str(VALID_FIXTURE),
                output=None,
                print_json=False,
                fail_on_warnings=False,
            )
        )
        self.assertEqual(exit_code, 0)

    def test_validate_conformance_invalid_fixture(self) -> None:
        exit_code = abi_framework.command_validate_plugin_manifest(
            argparse.Namespace(
                manifest=str(INVALID_FIXTURE),
                output=None,
                print_json=False,
                fail_on_warnings=False,
            )
        )
        self.assertEqual(exit_code, 1)

    def test_validate_project_manifests(self) -> None:
        manifests = [LUMENRTC_PLUGIN_MANIFEST, SDK_PLUGIN_MANIFEST]
        for manifest in manifests:
            with self.subTest(manifest=manifest.name):
                exit_code = abi_framework.command_validate_plugin_manifest(
                    argparse.Namespace(
                        manifest=str(manifest),
                        output=None,
                        print_json=False,
                        fail_on_warnings=False,
                    )
                )
                self.assertEqual(exit_code, 0)

    def test_validate_from_config_discovers_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "plugin.config.report.json"
            exit_code = abi_framework.command_validate_plugin_manifest(
                argparse.Namespace(
                    manifest=[],
                    config=str(ABI_CONFIG_PATH),
                    repo_root=str(REPO_ROOT),
                    target="lumenrtc",
                    output=str(report_path),
                    print_json=False,
                    fail_on_warnings=False,
                )
            )
            self.assertEqual(exit_code, 0)
            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report.get("status"), "pass")
            self.assertGreaterEqual(report.get("manifest_count", 0), 2)
            self.assertGreaterEqual(report.get("plugin_count", 0), 4)

    def test_validate_from_config_fails_when_manifest_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / "abi").mkdir(parents=True, exist_ok=True)
            (repo_root / "tools" / "broken").mkdir(parents=True, exist_ok=True)

            config = {
                "targets": {
                    "demo": {
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
                            "generators": [
                                {
                                    "name": "broken",
                                    "kind": "external",
                                    "command": [
                                        "python3",
                                        "{repo_root}/tools/broken/generator.py",
                                        "--idl",
                                        "{idl}",
                                    ],
                                }
                            ]
                        },
                    }
                }
            }
            config_path = repo_root / "abi" / "config.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

            exit_code = abi_framework.command_validate_plugin_manifest(
                argparse.Namespace(
                    manifest=[],
                    config=str(config_path),
                    repo_root=str(repo_root),
                    target="demo",
                    output=None,
                    print_json=False,
                    fail_on_warnings=False,
                )
            )
            self.assertEqual(exit_code, 1)

    def test_validate_from_config_fails_when_plugin_binding_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repo_root = Path(temp_dir)
            (repo_root / "abi").mkdir(parents=True, exist_ok=True)
            (repo_root / "tools" / "demo_plugin").mkdir(parents=True, exist_ok=True)

            manifest_payload = {
                "schema_version": 1,
                "package": "demo.plugin",
                "plugins": [
                    {
                        "name": "demo.generator",
                        "version": "1.0.0",
                        "entrypoint": {
                            "kind": "external",
                            "command": [
                                "python3",
                                "{repo_root}/tools/demo_plugin/generator.py",
                                "--idl",
                                "{idl}",
                            ],
                        },
                    }
                ],
            }
            (repo_root / "tools" / "demo_plugin" / "plugin.manifest.json").write_text(
                json.dumps(manifest_payload, indent=2) + "\n",
                encoding="utf-8",
            )

            config = {
                "targets": {
                    "demo": {
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
                            "generators": [
                                {
                                    "name": "demo",
                                    "kind": "external",
                                    "manifest": "{repo_root}/tools/demo_plugin/plugin.manifest.json",
                                    "plugin": "demo.unknown",
                                }
                            ]
                        },
                    }
                }
            }
            config_path = repo_root / "abi" / "config.json"
            config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")

            exit_code = abi_framework.command_validate_plugin_manifest(
                argparse.Namespace(
                    manifest=[],
                    config=str(config_path),
                    repo_root=str(repo_root),
                    target="demo",
                    output=None,
                    print_json=False,
                    fail_on_warnings=False,
                )
            )
            self.assertEqual(exit_code, 1)

    def test_cli_validate_plugin_manifest_emits_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            report_path = Path(temp_dir) / "plugin.report.json"
            command = [
                "python3",
                str(ABI_FRAMEWORK_ENTRYPOINT),
                "validate-plugin-manifest",
                "--manifest",
                str(VALID_FIXTURE),
                "--output",
                str(report_path),
                "--print-json",
            ]
            result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, msg=(result.stdout + "\n" + result.stderr))
            self.assertTrue(report_path.exists())

            report = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(report.get("status"), "pass")
            self.assertEqual(report.get("plugin_count"), 1)


if __name__ == "__main__":
    unittest.main()
