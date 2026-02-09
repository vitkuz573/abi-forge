from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import abi_framework_core as abi_framework  # noqa: E402
from abi_framework_core import core as abi_core  # noqa: E402


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

    def test_policy_rules_and_waivers(self) -> None:
        baseline = make_snapshot((1, 0, 0), {"my_init": "void", "my_add": "int a, int b"})
        current = make_snapshot((2, 0, 0), {"my_init": "void"})
        raw_report = abi_framework.compare_snapshots(baseline=baseline, current=current)

        config = {
            "targets": {
                "demo": {
                    "policy": {
                        "rules": [
                            {
                                "id": "no_removed_symbols",
                                "severity": "error",
                                "message": "no symbol removals allowed",
                                "when": {"removed_symbols_count_gt": 0},
                            }
                        ],
                        "waivers": [
                            {
                                "id": "temporary-waive-removal",
                                "severity": "error",
                                "pattern": "no symbol removals allowed",
                                "targets": ["^demo$"],
                                "expires_utc": "2099-01-01T00:00:00Z",
                                "owner": "test",
                            }
                        ],
                    }
                }
            }
        }

        effective_policy = abi_framework.resolve_effective_policy(config=config, target_name="demo")
        report = abi_framework.apply_policy_to_report(
            report=raw_report,
            policy=effective_policy,
            target_name="demo",
        )

        self.assertEqual(report["status"], "pass")
        self.assertTrue(report.get("policy_rules_applied"))
        self.assertTrue(report.get("waivers_applied"))
        self.assertFalse(report.get("errors"))

    def test_parser_backend_fallback(self) -> None:
        header_path = self.repo_root / "native" / "include" / "demo.h"
        type_policy = abi_framework.build_type_policy(
            {
                "types": {
                    "enable_enums": True,
                    "enable_structs": True,
                    "enum_name_pattern": "^my_",
                    "struct_name_pattern": "^my_",
                    "ignore_enums": [],
                    "ignore_structs": [],
                    "struct_tail_addition_is_breaking": True,
                }
            },
            "my_",
        )
        version_macros = {
            "major": "MY_ABI_VERSION_MAJOR",
            "minor": "MY_ABI_VERSION_MINOR",
            "patch": "MY_ABI_VERSION_PATCH",
        }

        with mock.patch.object(abi_core, "_resolve_executable_candidate", return_value=None):
            header_payload, abi_version, parser_info = abi_framework.parse_c_header(
                header_path=header_path,
                api_macro="MY_API",
                call_macro="MY_CALL",
                symbol_prefix="my_",
                version_macros=version_macros,
                type_policy=type_policy,
                parser_cfg={
                    "backend": "clang_preprocess",
                    "compiler": "definitely-not-a-real-compiler",
                    "compiler_candidates": [],
                    "fallback_to_regex": True,
                },
            )
            self.assertEqual(abi_version.major, 1)
            self.assertTrue(header_payload["function_count"] >= 2)
            self.assertEqual(parser_info["backend"], "regex")
            self.assertTrue(parser_info["fallback_used"])

            with self.assertRaises(abi_framework.AbiFrameworkError):
                abi_framework.parse_c_header(
                    header_path=header_path,
                    api_macro="MY_API",
                    call_macro="MY_CALL",
                    symbol_prefix="my_",
                    version_macros=version_macros,
                    type_policy=type_policy,
                    parser_cfg={
                        "backend": "clang_preprocess",
                        "compiler": "definitely-not-a-real-compiler",
                        "compiler_candidates": [],
                        "fallback_to_regex": False,
                    },
                )

    def test_sanitize_c_decl_text_strips_attributes(self) -> None:
        raw = '__attribute__((visibility("default"))) __cdecl _Bool * value'
        sanitized = abi_framework.sanitize_c_decl_text(raw)
        normalized = abi_framework.normalize_c_type(raw)

        self.assertEqual(sanitized, "bool * value")
        self.assertEqual(normalized, "bool*value")

    def test_resolve_parser_compiler_uses_candidates(self) -> None:
        parser_cfg = {
            "backend": "clang_preprocess",
            "compiler": "clang-does-not-exist",
            "compiler_candidates": ["clang-missing", "clang-18"],
            "args": [],
            "include_dirs": [],
            "fallback_to_regex": True,
        }

        def fake_resolver(candidate: str) -> str | None:
            if candidate == "clang-18":
                return "/usr/bin/clang-18"
            return None

        with mock.patch.object(abi_core, "_resolve_executable_candidate", side_effect=fake_resolver):
            resolved, meta = abi_framework.resolve_parser_compiler(parser_cfg)

        self.assertEqual(resolved, "/usr/bin/clang-18")
        self.assertEqual(meta["compiler_selected"], "clang-18")
        self.assertEqual(meta["compiler_requested"], "clang-does-not-exist")
        self.assertIn("clang-does-not-exist", meta["compiler_candidates"])

    def test_parse_readelf_exports_filters_non_exports(self) -> None:
        readelf_output = """
Symbol table '.dynsym' contains 6 entries:
   Num:    Value          Size Type    Bind   Vis      Ndx Name
     0: 0000000000000000     0 NOTYPE  LOCAL  DEFAULT  UND
     1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND _Znam@GLIBCXX_3.4
     2: 0000000000001234    16 FUNC    GLOBAL DEFAULT   12 lrtc_ok
     3: 0000000000001244    16 FUNC    LOCAL  DEFAULT   12 _ZLhelper
     4: 0000000000001254    16 FUNC    WEAK   DEFAULT   12 lrtc_weak
     5: 0000000000001264    16 FUNC    GLOBAL HIDDEN    12 hidden_symbol
""".strip()
        exports = abi_framework.parse_readelf_exports(readelf_output)
        self.assertEqual(exports, ["lrtc_ok", "lrtc_weak"])

    def test_parse_objdump_exports_filters_non_exports(self) -> None:
        objdump_output = """
0000000000000000      DF *UND*  0000000000000000  Base        strlen
0000000000001234 g    DF .text  0000000000000010  Base        lrtc_ok
0000000000001244 l    DF .text  0000000000000010  Base        _ZLhelper
0000000000001254 w    DF .text  0000000000000010  Base        lrtc_weak
0000000000001264 u    DF .text  0000000000000010  Base        lrtc_unique
""".strip()
        exports = abi_framework.parse_objdump_exports(objdump_output)
        self.assertEqual(exports, ["lrtc_ok", "lrtc_unique", "lrtc_weak"])

    def test_extract_binary_exports_uses_first_successful_tool(self) -> None:
        binary_path = self.repo_root / "native" / "libdummy.so"
        binary_path.parent.mkdir(parents=True, exist_ok=True)
        binary_path.write_bytes(b"\x7fELF")

        command_specs = [
            ("nm", ["nm", "-D", "--defined-only", str(binary_path)], "nm"),
            ("readelf", ["readelf", "-Ws", str(binary_path)], "readelf"),
        ]

        with mock.patch.object(abi_framework, "build_export_command_specs", return_value=command_specs):
            with mock.patch.object(abi_framework.shutil, "which", return_value="/usr/bin/fake-tool"):
                run_results = [
                    subprocess.CompletedProcess(
                        args=command_specs[0][1],
                        returncode=0,
                        stdout="0000000000001234 T lrtc_only\n",
                        stderr="",
                    ),
                    subprocess.CompletedProcess(
                        args=command_specs[1][1],
                        returncode=0,
                        stdout="   1: 0000000000001244 16 FUNC GLOBAL DEFAULT 12 should_not_be_used\n",
                        stderr="",
                    ),
                ]
                with mock.patch.object(abi_framework.subprocess, "run", side_effect=run_results) as run_mock:
                    payload = abi_framework.extract_binary_exports(
                        binary_path=binary_path,
                        symbol_prefix="lrtc_",
                        allow_non_prefixed_exports=False,
                    )

        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(payload["symbols"], ["lrtc_only"])
        self.assertEqual(payload["raw_export_count"], 1)
        self.assertEqual(payload["tool"], "nm -D --defined-only " + str(binary_path))

    def test_extract_header_auxiliary_types_and_constants(self) -> None:
        header_text = """
#define LRTC_MAX_ICE_SERVERS 8
#define LRTC_BUFFER_SIZE (4 * 1024)
typedef struct lrtc_factory_t lrtc_factory_t;
typedef struct lrtc_peer_connection_t lrtc_peer_connection_t;
typedef void (LUMENRTC_CALL *lrtc_log_message_cb)(void* user_data, const char* message);
typedef void (LUMENRTC_CALL *lrtc_void_cb)(void* user_data);
""".strip()

        opaque = abi_framework.extract_opaque_struct_typedefs(header_text, symbol_prefix="lrtc_")
        callbacks = abi_framework.extract_callback_typedefs(
            header_text,
            symbol_prefix="lrtc_",
            call_macro="LUMENRTC_CALL",
        )
        constants = abi_framework.extract_prefixed_define_constants(header_text, macro_prefix="LRTC_")

        self.assertEqual([item["name"] for item in opaque], ["lrtc_factory_t", "lrtc_peer_connection_t"])
        self.assertEqual([item["name"] for item in callbacks], ["lrtc_log_message_cb", "lrtc_void_cb"])
        self.assertEqual(constants, {"LRTC_BUFFER_SIZE": "(4 * 1024)", "LRTC_MAX_ICE_SERVERS": "8"})

    def test_render_native_header_and_export_map_from_idl(self) -> None:
        idl_payload = {
            "target": "demo",
            "abi_version": {"major": 1, "minor": 2, "patch": 3},
            "functions": [
                {"name": "my_set_callback", "c_return_type": "void", "parameters": [{"name": "cb", "c_type": "my_log_cb"}]},
                {"name": "my_initialize", "c_return_type": "int32_t", "parameters": []},
            ],
            "header_types": {
                "constants": {"MY_LIMIT": "32"},
                "opaque_types": ["my_handle_t"],
                "callback_typedefs": [
                    {"name": "my_log_cb", "declaration": "typedef void (MY_CALL *my_log_cb)(void* user_data, const char* message);"}
                ],
                "enums": {
                    "my_mode_t": {
                        "members": [
                            {"name": "MY_MODE_A", "value_expr": "0"},
                            {"name": "MY_MODE_B", "value_expr": "1"},
                        ]
                    }
                },
                "structs": {
                    "my_opts_t": {
                        "fields": [
                            {"name": "enabled", "declaration": "int enabled"},
                        ]
                    }
                },
            },
        }
        cfg = {
            "native_header_guard": "MY_HEADER_H",
            "native_api_macro": "MY_API",
            "native_call_macro": "MY_CALL",
            "native_constants": {},
            "version_macro_names": {"major": "MY_ABI_MAJOR", "minor": "MY_ABI_MINOR", "patch": "MY_ABI_PATCH"},
        }

        header_text = abi_framework.render_native_header_from_idl("demo", idl_payload, cfg)
        export_map_text = abi_framework.render_native_export_map_from_idl(idl_payload)

        self.assertIn("#ifndef MY_HEADER_H", header_text)
        self.assertIn("#define MY_LIMIT 32", header_text)
        self.assertIn("#define MY_ABI_MAJOR 1", header_text)
        self.assertIn("typedef struct my_handle_t my_handle_t;", header_text)
        self.assertIn("typedef void (MY_CALL *my_log_cb)(void* user_data, const char* message);", header_text)
        self.assertIn("typedef enum my_mode_t {", header_text)
        self.assertIn("typedef struct my_opts_t {", header_text)
        self.assertIn("MY_API int32_t MY_CALL my_initialize(void);", header_text)
        self.assertIn("MY_API void MY_CALL my_set_callback(my_log_cb cb);", header_text)

        self.assertIn("global:", export_map_text)
        self.assertIn("my_initialize;", export_map_text)
        self.assertIn("my_set_callback;", export_map_text)

    def test_codegen_external_generator(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        target = config["targets"]["demo"]

        marker_path = self.repo_root / "artifacts" / "generator.marker"
        script_path = self.repo_root / "tools" / "stub_generator.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "import pathlib\n"
            "import sys\n"
            "idl = pathlib.Path(sys.argv[1])\n"
            "out = pathlib.Path(sys.argv[2])\n"
            "out.parent.mkdir(parents=True, exist_ok=True)\n"
            "out.write_text('generated:' + idl.read_text(encoding='utf-8')[:32], encoding='utf-8')\n",
            encoding="utf-8",
        )

        target["bindings"]["generators"] = [
            {
                "name": "stub",
                "kind": "external",
                "command": [sys.executable, str(script_path), "{idl}", str(marker_path)],
            }
        ]
        abi_framework.write_json(config_path, config)

        exit_code = abi_framework.command_codegen(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(config_path),
                target="demo",
                binary=None,
                skip_binary=True,
                idl_output=None,
                dry_run=False,
                check=False,
                print_diff=False,
                report_json=str(self.repo_root / "artifacts" / "codegen.json"),
                fail_on_sync=False,
            )
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(marker_path.exists())

    def test_benchmark_command(self) -> None:
        output_path = self.repo_root / "artifacts" / "benchmark.json"
        exit_code = abi_framework.command_benchmark(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                baseline_root=None,
                binary=None,
                skip_binary=True,
                iterations=1,
                output=str(output_path),
            )
        )
        self.assertEqual(exit_code, 0)
        payload = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertIn("targets", payload)
        self.assertIn("demo", payload["targets"])

    def test_validate_idl_payload_rejects_non_v1(self) -> None:
        payload = {
            "idl_schema_version": 0,
            "idl_schema": "https://lumenrtc.dev/abi_framework/idl.schema.v1.json",
            "tool": {"name": "abi_framework", "version": "test"},
            "target": "demo",
            "abi_version": {"major": 1, "minor": 0, "patch": 0},
            "functions": [],
        }
        with self.assertRaises(abi_framework.AbiFrameworkError):
            abi_framework.validate_idl_payload(payload, "legacy idl")

    def test_waiver_requirements_are_enforced(self) -> None:
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
                    "policy": {
                        "waiver_requirements": {
                            "require_owner": True,
                            "require_reason": True,
                            "require_expires_utc": True,
                        },
                        "waivers": [
                            {
                                "id": "missing-fields",
                                "severity": "warning",
                                "pattern": "drift",
                                "targets": ["^demo$"],
                            }
                        ],
                    },
                }
            }
        }
        with self.assertRaises(abi_framework.AbiFrameworkError):
            _ = abi_framework.resolve_effective_policy(config=config, target_name="demo")

    def test_waiver_audit_detects_expired(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        config["policy"] = {
            "waiver_requirements": {
                "require_owner": True,
                "require_reason": True,
                "require_expires_utc": True,
                "require_approved_by": False,
                "require_ticket": False,
                "max_ttl_days": 500,
                "warn_expiring_within_days": 30,
            }
        }
        config["targets"]["demo"]["policy"] = {
            "waivers": [
                {
                    "id": "expired-waiver",
                    "severity": "warning",
                    "pattern": "demo",
                    "targets": ["^demo$"],
                    "created_utc": "2024-01-01T00:00:00Z",
                    "expires_utc": "2025-01-01T00:00:00Z",
                    "owner": "abi-team",
                    "reason": "temporary",
                }
            ]
        }
        abi_framework.write_json(config_path, config)

        exit_code = abi_framework.command_waiver_audit(
            argparse.Namespace(
                config=str(config_path),
                target="demo",
                output=str(self.repo_root / "artifacts" / "waiver.audit.json"),
                print_json=False,
                fail_on_expired=True,
                fail_on_missing_metadata=False,
                fail_on_expiring_soon=False,
            )
        )
        self.assertEqual(exit_code, 1)

    def test_benchmark_gate_detects_violation(self) -> None:
        report_path = self.repo_root / "artifacts" / "bench.json"
        budget_path = self.repo_root / "abi" / "bench.budget.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_payload = {
            "targets": {
                "demo": {
                    "snapshot_ms": {"mean_ms": 100.0, "p95_ms": 150.0},
                }
            }
        }
        budget_payload = {
            "targets": {
                "demo": {
                    "snapshot_ms": {
                        "mean_ms_max": 50.0,
                    }
                }
            }
        }
        report_path.write_text(json.dumps(report_payload, indent=2) + "\n", encoding="utf-8")
        budget_path.write_text(json.dumps(budget_payload, indent=2) + "\n", encoding="utf-8")

        exit_code = abi_framework.command_benchmark_gate(
            argparse.Namespace(
                report=str(report_path),
                budget=str(budget_path),
                output=str(self.repo_root / "artifacts" / "bench.gate.json"),
            )
        )
        self.assertEqual(exit_code, 1)

    def test_release_prepare_emits_sbom_and_attestation(self) -> None:
        self._run_generate_for_demo()
        changelog_path = self.repo_root / "abi" / "CHANGELOG.md"
        output_dir = self.repo_root / "artifacts" / "release-enterprise"
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
                benchmark_budget=None,
                emit_sbom=True,
                emit_attestation=True,
            )
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue((output_dir / "release.sbom.cdx.json").exists())
        self.assertTrue((output_dir / "release.attestation.json").exists())


if __name__ == "__main__":
    unittest.main()
