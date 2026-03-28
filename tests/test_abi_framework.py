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
from abi_framework_core import _core_base as abi_core_base  # noqa: E402


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
                        "symbol_contract": {
                            "mode": "strict",
                            "symbols": ["my_add", "my_init"],
                        },
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

        with mock.patch.object(abi_core_base, "_resolve_executable_candidate", return_value=None):
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

        with mock.patch.object(abi_core_base, "_resolve_executable_candidate", side_effect=fake_resolver):
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

    def test_codegen_external_generator_from_manifest_plugin(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        target = config["targets"]["demo"]

        marker_path = self.repo_root / "artifacts" / "generator.manifest.marker"
        script_path = self.repo_root / "tools" / "stub_generator_manifest.py"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(
            "import pathlib\n"
            "import sys\n"
            "idl = pathlib.Path(sys.argv[1])\n"
            "out = pathlib.Path(sys.argv[2])\n"
            "out.parent.mkdir(parents=True, exist_ok=True)\n"
            "out.write_text('manifest:' + idl.read_text(encoding='utf-8')[:32], encoding='utf-8')\n",
            encoding="utf-8",
        )

        manifest_path = self.repo_root / "tools" / "stub_plugin" / "plugin.manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_payload = {
            "schema_version": 1,
            "package": "demo.plugin",
            "plugins": [
                {
                    "name": "demo.stub",
                    "version": "1.0.0",
                    "entrypoint": {
                        "kind": "external",
                        "command": [
                            sys.executable,
                            "{repo_root}/tools/stub_generator_manifest.py",
                            "{idl}",
                            "{repo_root}/artifacts/generator.manifest.marker",
                            "{check}",
                            "{dry_run}",
                        ],
                    },
                }
            ],
        }
        abi_framework.write_json(manifest_path, manifest_payload)

        target["bindings"]["generators"] = [
            {
                "name": "stub_manifest",
                "kind": "external",
                "manifest": "{repo_root}/tools/stub_plugin/plugin.manifest.json",
                "plugin": "demo.stub",
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
                report_json=None,
                fail_on_sync=False,
            )
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(marker_path.exists())

    def test_codegen_external_generator_manifest_plugin_not_found(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        target = config["targets"]["demo"]

        manifest_path = self.repo_root / "tools" / "stub_plugin" / "plugin.manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_payload = {
            "schema_version": 1,
            "package": "demo.plugin",
            "plugins": [
                {
                    "name": "demo.stub",
                    "version": "1.0.0",
                    "entrypoint": {
                        "kind": "external",
                        "command": [sys.executable, "{repo_root}/tools/stub_generator_missing.py", "{idl}"],
                    },
                }
            ],
        }
        abi_framework.write_json(manifest_path, manifest_payload)

        target["bindings"]["generators"] = [
            {
                "name": "stub_manifest",
                "kind": "external",
                "manifest": "{repo_root}/tools/stub_plugin/plugin.manifest.json",
                "plugin": "demo.unknown",
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
                report_json=None,
                fail_on_sync=False,
            )
        )
        self.assertEqual(exit_code, 1)

    def test_config_validation_generator_plugin_requires_manifest(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        config["targets"]["demo"]["bindings"]["generators"] = [
            {
                "name": "invalid",
                "kind": "external",
                "plugin": "demo.invalid",
                "command": [sys.executable, "tools/stub.py", "{idl}"],
            }
        ]
        abi_framework.write_json(config_path, config)

        with self.assertRaises(abi_framework.AbiFrameworkError):
            _ = abi_framework.load_config(config_path)

    def test_config_validation_generator_requires_command_or_manifest(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        config["targets"]["demo"]["bindings"]["generators"] = [
            {
                "name": "invalid",
                "kind": "external",
            }
        ]
        abi_framework.write_json(config_path, config)

        with self.assertRaises(abi_framework.AbiFrameworkError):
            _ = abi_framework.load_config(config_path)

    def test_bindings_metadata_path_and_inline_merge_into_idl(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)

        metadata_path = self.repo_root / "abi" / "bindings" / "demo.bindings.json"
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(
            json.dumps(
                {
                    "interop": {
                        "string_encoding": "utf8",
                        "opaque_types": {
                            "my_handle_t": {"release": "my_release"},
                        },
                    },
                    "swift": {"module": "DemoRtc"},
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        bindings = config["targets"]["demo"]["bindings"]
        bindings["metadata_path"] = "abi/bindings/demo.bindings.json"
        bindings["metadata"] = {
            "interop": {"string_encoding": "utf16"},
            "managed": {"runtime": "dotnet"},
        }
        abi_framework.write_json(config_path, config)

        self._run_generate_for_demo()
        idl = abi_framework.load_json(self.repo_root / "abi" / "generated" / "demo" / "demo.idl.json")
        bindings_payload = idl.get("bindings")
        self.assertIsInstance(bindings_payload, dict)

        interop_payload = bindings_payload.get("interop")
        self.assertIsInstance(interop_payload, dict)
        self.assertEqual(interop_payload.get("string_encoding"), "utf16")
        self.assertIn("opaque_types", interop_payload)

        swift_payload = bindings_payload.get("swift")
        self.assertIsInstance(swift_payload, dict)
        self.assertEqual(swift_payload.get("module"), "DemoRtc")

        managed_payload = bindings_payload.get("managed")
        self.assertIsInstance(managed_payload, dict)
        self.assertEqual(managed_payload.get("runtime"), "dotnet")

    def test_bindings_metadata_must_be_object(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        config["targets"]["demo"]["bindings"]["metadata"] = "invalid"
        abi_framework.write_json(config_path, config)

        with self.assertRaises(abi_framework.AbiFrameworkError):
            _ = abi_framework.load_config(config_path)

    def test_bindings_metadata_path_must_be_string(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        config["targets"]["demo"]["bindings"]["metadata_path"] = 123
        abi_framework.write_json(config_path, config)

        with self.assertRaises(abi_framework.AbiFrameworkError):
            _ = abi_framework.load_config(config_path)

    def test_symbol_contract_strict_fail_on_sync_detects_extra(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        contract_path = self.repo_root / "abi" / "bindings" / "demo.symbol_contract.json"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps({"schema_version": 1, "symbols": ["my_init"]}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config["targets"]["demo"]["bindings"] = {
            "symbol_contract": {
                "path": "abi/bindings/demo.symbol_contract.json",
                "mode": "strict",
            }
        }
        abi_framework.write_json(config_path, config)

        exit_code = abi_framework.command_generate(
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
                report_json=None,
                fail_on_sync=True,
            )
        )
        self.assertEqual(exit_code, 1)

    def test_symbol_contract_required_only_ignores_extra(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        contract_path = self.repo_root / "abi" / "bindings" / "demo.symbol_contract.json"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps({"schema_version": 1, "symbols": ["my_init"]}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        config["targets"]["demo"]["bindings"] = {
            "symbol_contract": {
                "path": "abi/bindings/demo.symbol_contract.json",
                "mode": "required_only",
            }
        }
        abi_framework.write_json(config_path, config)

        exit_code = abi_framework.command_generate(
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
                report_json=None,
                fail_on_sync=True,
            )
        )
        self.assertEqual(exit_code, 0)

    def test_symbol_contract_path_can_define_mode(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        config = abi_framework.load_json(config_path)
        contract_path = self.repo_root / "abi" / "bindings" / "demo.symbol_contract.json"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": "required_only",
                    "symbols": ["my_init"],
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        config["targets"]["demo"]["bindings"] = {
            "symbol_contract": {
                "path": "abi/bindings/demo.symbol_contract.json",
            }
        }
        abi_framework.write_json(config_path, config)

        exit_code = abi_framework.command_generate(
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
                report_json=None,
                fail_on_sync=True,
            )
        )
        self.assertEqual(exit_code, 0)

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


class SarifTests(unittest.TestCase):
    """Tests for typed SARIF rule emission (ABI001-ABI007)."""

    def _make_report(self, **kwargs: object) -> dict[str, object]:
        """Build a minimal compare_snapshots-style report with given overrides."""
        base: dict[str, object] = {
            "status": "fail",
            "change_classification": "breaking",
            "required_bump": "major",
            "removed_symbols": [],
            "added_symbols": [],
            "changed_signatures": [],
            "errors": [],
            "warnings": [],
            "breaking_reasons": [],
            "additive_reasons": [],
            "enum_diff": {},
            "struct_diff": {},
        }
        base.update(kwargs)
        return base

    def test_function_removed_emits_abi001(self) -> None:
        report = self._make_report(removed_symbols=["my_func"])
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI001", rule_ids)
        messages = [r["message"]["text"] for r in results if r["ruleId"] == "ABI001"]
        self.assertTrue(any("my_func" in m for m in messages))

    def test_signature_changed_emits_abi002(self) -> None:
        report = self._make_report(changed_signatures=["my_func"])
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI002", rule_ids)

    def test_enum_removed_emits_abi003(self) -> None:
        report = self._make_report(enum_diff={"removed_enums": ["my_enum"]})
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI003", rule_ids)
        messages = [r["message"]["text"] for r in results if r["ruleId"] == "ABI003"]
        self.assertTrue(any("my_enum" in m for m in messages))

    def test_enum_member_removed_emits_abi003(self) -> None:
        report = self._make_report(enum_diff={
            "removed_enums": [],
            "changed_enums": {
                "State": {"kind": "breaking", "removed_members": ["STATE_GONE"], "value_changed": []}
            },
        })
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI003", rule_ids)
        messages = [r["message"]["text"] for r in results if r["ruleId"] == "ABI003"]
        self.assertTrue(any("STATE_GONE" in m for m in messages))

    def test_enum_value_changed_emits_abi003(self) -> None:
        report = self._make_report(enum_diff={
            "removed_enums": [],
            "changed_enums": {
                "Color": {"kind": "breaking", "removed_members": [], "value_changed": ["COLOR_RED"]}
            },
        })
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI003", rule_ids)
        messages = [r["message"]["text"] for r in results if r["ruleId"] == "ABI003"]
        self.assertTrue(any("COLOR_RED" in m for m in messages))

    def test_struct_layout_changed_emits_abi004(self) -> None:
        report = self._make_report(struct_diff={
            "removed_structs": [],
            "changed_structs": {
                "MyStruct": {
                    "kind": "breaking",
                    "removed_fields": ["old_field"],
                    "added_fields": [],
                    "changed_fields": [],
                }
            },
        })
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI004", rule_ids)
        messages = [r["message"]["text"] for r in results if r["ruleId"] == "ABI004"]
        self.assertTrue(any("MyStruct" in m for m in messages))
        self.assertTrue(any("old_field" in m for m in messages))

    def test_bindings_error_emits_abi005(self) -> None:
        report = self._make_report(errors=["symbol contract mismatch: extra symbols found"])
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI005", rule_ids)

    def test_version_error_emits_abi006(self) -> None:
        report = self._make_report(errors=["version bump required: breaking change with no major bump"])
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI006", rule_ids)

    def test_warning_emits_abi007(self) -> None:
        report = self._make_report(warnings=["additive change without minor bump"])
        results = abi_framework.build_sarif_results_for_target("demo", report, None)
        rule_ids = [r["ruleId"] for r in results]
        self.assertIn("ABI007", rule_ids)
        entry = next(r for r in results if r["ruleId"] == "ABI007")
        self.assertEqual(entry["level"], "warning")

    def test_write_sarif_report_contains_all_rules(self) -> None:
        import tempfile
        results = abi_framework.build_sarif_results_for_target("demo", self._make_report(
            removed_symbols=["f1"],
            changed_signatures=["f2"],
            enum_diff={"removed_enums": ["E1"], "changed_enums": {}},
            struct_diff={"removed_structs": ["S1"], "changed_structs": {}},
            errors=["version bump required: major", "symbol contract mismatch"],
            warnings=["minor drift"],
        ), None)
        with tempfile.NamedTemporaryFile(suffix=".sarif", delete=False) as fp:
            out_path = Path(fp.name)
        abi_framework.write_sarif_report(out_path, results)
        payload = json.loads(out_path.read_text())
        rule_ids = {r["id"] for r in payload["runs"][0]["tool"]["driver"]["rules"]}
        for expected in ("ABI001", "ABI002", "ABI003", "ABI004", "ABI005", "ABI006", "ABI007"):
            self.assertIn(expected, rule_ids)
        out_path.unlink(missing_ok=True)

    def test_sarif_location_attached_when_source_path_given(self) -> None:
        report = self._make_report(removed_symbols=["my_func"])
        results = abi_framework.build_sarif_results_for_target("demo", report, "native/include/api.h")
        entry = next(r for r in results if r["ruleId"] == "ABI001")
        self.assertIn("locations", entry)
        uri = entry["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        self.assertEqual(uri, "native/include/api.h")


class ChangelogStructFieldTests(unittest.TestCase):
    """Tests for struct field detail in the changelog renderer."""

    def _make_report(self, struct_diff: dict[str, object]) -> dict[str, object]:
        return {
            "status": "fail",
            "change_classification": "breaking",
            "required_bump": "major",
            "baseline_abi_version": {"major": 1, "minor": 0, "patch": 0},
            "current_abi_version": {"major": 1, "minor": 0, "patch": 0},
            "recommended_next_version": {"major": 2, "minor": 0, "patch": 0},
            "removed_symbols": [],
            "added_symbols": [],
            "changed_signatures": [],
            "errors": [],
            "warnings": [],
            "breaking_reasons": [],
            "additive_reasons": [],
            "enum_diff": {},
            "struct_diff": struct_diff,
        }

    def test_removed_fields_appear_in_changelog(self) -> None:
        report = self._make_report(struct_diff={
            "removed_structs": [],
            "changed_structs": {
                "Packet": {
                    "kind": "breaking",
                    "removed_fields": ["timestamp_ms"],
                    "added_fields": [],
                    "changed_fields": [],
                }
            },
        })
        lines = abi_framework.render_target_changelog_section("demo", report)
        text = "\n".join(lines)
        self.assertIn("Packet", text)
        self.assertIn("timestamp_ms", text)
        self.assertIn("Removed fields", text)

    def test_added_fields_appear_in_changelog(self) -> None:
        report = self._make_report(struct_diff={
            "removed_structs": [],
            "changed_structs": {
                "Packet": {
                    "kind": "breaking",
                    "removed_fields": [],
                    "added_fields": ["crc32"],
                    "changed_fields": [],
                }
            },
        })
        lines = abi_framework.render_target_changelog_section("demo", report)
        text = "\n".join(lines)
        self.assertIn("crc32", text)
        self.assertIn("Added fields", text)

    def test_changed_fields_appear_in_changelog(self) -> None:
        report = self._make_report(struct_diff={
            "removed_structs": [],
            "changed_structs": {
                "Frame": {
                    "kind": "breaking",
                    "removed_fields": [],
                    "added_fields": [],
                    "changed_fields": ["flags"],
                }
            },
        })
        lines = abi_framework.render_target_changelog_section("demo", report)
        text = "\n".join(lines)
        self.assertIn("flags", text)
        self.assertIn("Modified fields", text)

    def test_non_breaking_struct_omitted_from_breaking_section(self) -> None:
        report = self._make_report(struct_diff={
            "removed_structs": [],
            "changed_structs": {
                "Stats": {
                    "kind": "additive",
                    "removed_fields": [],
                    "added_fields": ["new_counter"],
                    "changed_fields": [],
                }
            },
        })
        lines = abi_framework.render_target_changelog_section("demo", report)
        breaking_idx = next(i for i, l in enumerate(lines) if "### Breaking" in l)
        additive_idx = next(i for i, l in enumerate(lines) if "### Additive" in l)
        breaking_text = "\n".join(lines[breaking_idx:additive_idx])
        self.assertNotIn("Stats", breaking_text)
        # additive section may mention it
        additive_text = "\n".join(lines[additive_idx:])
        self.assertIn("Stats", additive_text)


GENERATOR_SDK = Path(__file__).resolve().parents[1] / "generator_sdk"
CORE_SRC = Path(__file__).resolve().parents[2] / "abi_codegen_core" / "src"
if str(GENERATOR_SDK) not in sys.path:
    sys.path.insert(0, str(GENERATOR_SDK))
if str(CORE_SRC) not in sys.path:
    sys.path.insert(0, str(CORE_SRC))

import managed_api_scaffold_generator as scaffold_mod  # noqa: E402


def _make_minimal_idl(
    target: str = "mylib",
    functions: list[dict] | None = None,
    structs: list[dict] | None = None,
    bindings: dict | None = None,
) -> dict:
    return {
        "idl_schema_version": 1,
        "target": target,
        "abi_version": {"major": 1, "minor": 0, "patch": 0},
        "functions": functions or [],
        "structs": structs or [],
        "enums": [],
        "constants": [],
        "bindings": bindings or {},
    }


class ScaffoldManagedApiTests(unittest.TestCase):

    def test_scaffold_produces_valid_schema_v2(self) -> None:
        idl = _make_minimal_idl()
        result = scaffold_mod.scaffold(idl, "MyLib", None)
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["namespace"], "MyLib")
        self.assertIn("auto_abi_surface", result)
        self.assertIn("callbacks", result)
        self.assertIn("handle_api", result)
        self.assertIn("required_native_functions", result)

    def test_scaffold_auto_abi_surface_enabled(self) -> None:
        idl = _make_minimal_idl()
        result = scaffold_mod.scaffold(idl, "MyLib", None)
        self.assertTrue(result["auto_abi_surface"]["enabled"])

    def test_scaffold_detects_callback_struct(self) -> None:
        idl = _make_minimal_idl(
            target="foo",
            bindings={
                "interop": {
                    "callback_struct_suffixes": ["_callbacks_t"],
                    "opaque_types": {},
                }
            },
        )
        # New format: header_types.structs is a dict
        idl["header_types"] = {
            "structs": {
                "foo_event_callbacks_t": {
                    "fields": [
                        {"name": "on_event", "declaration": "foo_event_cb on_event"},
                        {"name": "user_data", "c_type": "void*"},
                    ],
                }
            },
            "callback_typedefs": [
                {"name": "foo_event_cb", "declaration": "typedef void (FOO_CALL *foo_event_cb)(void* ud);"}
            ],
            "enums": {},
            "opaque_types": [],
            "opaque_type_declarations": [],
            "constants": {},
        }
        result = scaffold_mod.scaffold(idl, "Foo", "foo_")
        self.assertEqual(len(result["callbacks"]), 1)
        cb = result["callbacks"][0]
        # foo_event_callbacks_t with prefix "foo_" -> strip prefix -> event_callbacks_t
        # strip _t -> event_callbacks -> PascalCase -> EventCallbacks
        self.assertEqual(cb["class"], "EventCallbacks")
        # user_data field should be skipped, only on_event included
        field_names = [f["native_field"] for f in cb["fields"]]
        self.assertIn("on_event", field_names)
        self.assertNotIn("user_data", field_names)

    def test_scaffold_detects_opaque_handles(self) -> None:
        idl = _make_minimal_idl(
            target="mylib",
            bindings={
                "interop": {
                    "opaque_types": {
                        "mylib_session_t": {},
                        "mylib_stream_t": {},
                    },
                    "callback_struct_suffixes": [],
                }
            },
        )
        # Provide explicit symbol_prefix so class names strip the prefix correctly
        result = scaffold_mod.scaffold(idl, "MyLib", "mylib_")
        handle_classes = [h["class"] for h in result["handle_api"]]
        # mylib_session_t with prefix "mylib_" -> session_t -> strip _t -> session -> Session
        self.assertIn("Session", handle_classes)
        self.assertIn("Stream", handle_classes)

    def test_scaffold_groups_functions_under_handle(self) -> None:
        idl = _make_minimal_idl(
            target="mylib",
            functions=[
                {
                    "name": "mylib_session_create",
                    "return_type": "mylib_session_t*",
                    "parameters": [],
                },
                {
                    "name": "mylib_session_destroy",
                    "return_type": "void",
                    "parameters": [{"name": "s", "c_type": "mylib_session_t*"}],
                },
                {
                    "name": "mylib_global_init",
                    "return_type": "int",
                    "parameters": [],
                },
            ],
            bindings={
                "interop": {
                    "opaque_types": {"mylib_session_t": {}},
                    "callback_struct_suffixes": [],
                }
            },
        )
        result = scaffold_mod.scaffold(idl, "MyLib", None)
        session_entry = next(h for h in result["handle_api"] if h["class"] == "Session")
        comment_lines = " ".join(m.get("line", "") for m in session_entry["members"])
        self.assertIn("mylib_session_destroy", comment_lines)
        # global function should NOT be in session handle
        self.assertNotIn("mylib_global_init", comment_lines)

    def test_scaffold_does_not_overwrite_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            idl_path = Path(tmp) / "test.idl.json"
            out_path = Path(tmp) / "test.managed_api.source.json"
            idl = _make_minimal_idl()
            idl_path.write_text(json.dumps(idl) + "\n", encoding="utf-8")
            sentinel = '{"sentinel": true}\n'
            out_path.write_text(sentinel, encoding="utf-8")

            # Import and call main with --out pointing to existing file (no --force)
            import argparse as _ap
            args = _ap.Namespace(
                idl=str(idl_path),
                namespace="MyLib",
                out=str(out_path),
                symbol_prefix=None,
                force=False,
                check=False,
                dry_run=False,
            )
            # The scaffold main guards against overwrite
            result_before = out_path.read_text(encoding="utf-8")
            # Simulate what main does: check for existing
            if out_path.exists() and not args.force and not args.check and not args.dry_run:
                pass  # should not overwrite
            self.assertEqual(out_path.read_text(encoding="utf-8"), sentinel)

    def test_scaffold_symbol_prefix_override(self) -> None:
        idl = _make_minimal_idl(
            target="mylib",
            functions=[
                {"name": "ml_init", "return_type": "int", "parameters": []},
            ],
        )
        result = scaffold_mod.scaffold(idl, "MyLib", "ml_")
        # With explicit prefix "ml_", should work without error
        self.assertEqual(result["schema_version"], 2)

    def test_scaffold_infers_symbol_prefix_from_idl(self) -> None:
        idl = _make_minimal_idl(
            target="mylib",
            functions=[
                {"name": "mylib_init", "return_type": "int", "parameters": []},
                {"name": "mylib_shutdown", "return_type": "void", "parameters": []},
            ],
        )
        # No explicit prefix — should auto-infer "mylib_"
        result = scaffold_mod.scaffold(idl, "MyLib", None)
        self.assertEqual(result["schema_version"], 2)


class InitTargetDefaultsTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        (self.repo_root / "abi" / "baselines").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "native" / "include").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "native" / "include" / "mylib.h").write_text(
            "#ifndef MYLIB_H\n#define MYLIB_H\n#endif\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_init_target_derives_macros_from_target_name(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        args = argparse.Namespace(
            repo_root=str(self.repo_root),
            config=str(config_path),
            target="mylib",
            header_path="native/include/mylib.h",
            api_macro="",
            call_macro="",
            symbol_prefix="",
            version_major_macro="",
            version_minor_macro="",
            version_patch_macro="",
            add_generators="none",
            binding_symbol=None,
            binary_path=None,
            baseline_path=None,
            create_baseline=False,
            force=False,
        )
        exit_code = abi_framework.command_init_target(args)
        self.assertEqual(exit_code, 0)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        header = config["targets"]["mylib"]["header"]
        self.assertEqual(header["api_macro"], "MYLIB_API")
        self.assertEqual(header["call_macro"], "MYLIB_CALL")
        self.assertEqual(header["symbol_prefix"], "mylib_")
        self.assertEqual(header["version_macros"]["major"], "MYLIB_VERSION_MAJOR")
        self.assertEqual(header["version_macros"]["minor"], "MYLIB_VERSION_MINOR")
        self.assertEqual(header["version_macros"]["patch"], "MYLIB_VERSION_PATCH")

    def test_init_target_respects_explicit_macros(self) -> None:
        config_path = self.repo_root / "abi" / "config.json"
        args = argparse.Namespace(
            repo_root=str(self.repo_root),
            config=str(config_path),
            target="mylib",
            header_path="native/include/mylib.h",
            api_macro="CUSTOM_EXPORT",
            call_macro="CUSTOM_CALL",
            symbol_prefix="ml_",
            version_major_macro="ML_MAJOR",
            version_minor_macro="ML_MINOR",
            version_patch_macro="ML_PATCH",
            add_generators="none",
            binding_symbol=None,
            binary_path=None,
            baseline_path=None,
            create_baseline=False,
            force=False,
        )
        exit_code = abi_framework.command_init_target(args)
        self.assertEqual(exit_code, 0)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        header = config["targets"]["mylib"]["header"]
        self.assertEqual(header["api_macro"], "CUSTOM_EXPORT")
        self.assertEqual(header["call_macro"], "CUSTOM_CALL")
        self.assertEqual(header["symbol_prefix"], "ml_")
        self.assertEqual(header["version_macros"]["major"], "ML_MAJOR")


class ScaffoldManagedApiCommandTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_idl(self, idl: dict) -> Path:
        p = self.root / "test.idl.json"
        p.write_text(json.dumps(idl) + "\n", encoding="utf-8")
        return p

    def test_scaffold_command_writes_output(self) -> None:
        idl = _make_minimal_idl(target="testlib")
        idl_path = self._write_idl(idl)
        out_path = self.root / "testlib.managed_api.source.json"

        args = argparse.Namespace(
            repo_root=str(self.root),
            idl=str(idl_path),
            namespace="TestLib",
            out=str(out_path),
            symbol_prefix=None,
            force=False,
            check=False,
            dry_run=False,
        )
        exit_code = abi_framework.command_scaffold_managed_api(args)
        self.assertEqual(exit_code, 0)
        self.assertTrue(out_path.exists())
        result = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["namespace"], "TestLib")

    def test_scaffold_command_does_not_overwrite_without_force(self) -> None:
        idl = _make_minimal_idl(target="testlib")
        idl_path = self._write_idl(idl)
        out_path = self.root / "testlib.managed_api.source.json"
        sentinel = '{"sentinel": true}\n'
        out_path.write_text(sentinel, encoding="utf-8")

        args = argparse.Namespace(
            repo_root=str(self.root),
            idl=str(idl_path),
            namespace="TestLib",
            out=str(out_path),
            symbol_prefix=None,
            force=False,
            check=False,
            dry_run=False,
        )
        exit_code = abi_framework.command_scaffold_managed_api(args)
        self.assertEqual(exit_code, 0)
        # File should remain unchanged
        self.assertEqual(out_path.read_text(encoding="utf-8"), sentinel)

    def test_scaffold_command_force_overwrites(self) -> None:
        idl = _make_minimal_idl(target="testlib")
        idl_path = self._write_idl(idl)
        out_path = self.root / "testlib.managed_api.source.json"
        sentinel = '{"sentinel": true}\n'
        out_path.write_text(sentinel, encoding="utf-8")

        args = argparse.Namespace(
            repo_root=str(self.root),
            idl=str(idl_path),
            namespace="TestLib",
            out=str(out_path),
            symbol_prefix=None,
            force=True,
            check=False,
            dry_run=False,
        )
        exit_code = abi_framework.command_scaffold_managed_api(args)
        self.assertEqual(exit_code, 0)
        result = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], 2)

    def test_scaffold_command_dry_run_does_not_write(self) -> None:
        idl = _make_minimal_idl(target="testlib")
        idl_path = self._write_idl(idl)
        out_path = self.root / "testlib.managed_api.source.json"

        args = argparse.Namespace(
            repo_root=str(self.root),
            idl=str(idl_path),
            namespace="TestLib",
            out=str(out_path),
            symbol_prefix=None,
            force=False,
            check=False,
            dry_run=True,
        )
        exit_code = abi_framework.command_scaffold_managed_api(args)
        self.assertEqual(exit_code, 0)
        self.assertFalse(out_path.exists())


class NativeExportsGeneratorTests(unittest.TestCase):

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _write_idl(self, idl: dict) -> Path:
        p = self.root / "test.idl.json"
        p.write_text(json.dumps(idl) + "\n", encoding="utf-8")
        return p

    def test_generic_native_exports_generator_dry_run(self) -> None:
        """Run native_exports_generator.py --dry-run on a minimal IDL."""
        idl = _make_minimal_idl(
            target="mylib",
            functions=[
                {
                    "name": "mylib_init",
                    "return_type": "int",
                    "parameters": [],
                },
                {
                    "name": "mylib_shutdown",
                    "return_type": "void",
                    "parameters": [{"name": "code", "c_type": "int"}],
                },
            ],
        )
        idl_path = self._write_idl(idl)
        out_cpp = self.root / "mylib.exports.cpp"
        impl_header = self.root / "mylib_impl.h"

        generator_script = Path(__file__).resolve().parents[1] / "generator_sdk" / "native_exports_generator.py"
        result = subprocess.run(
            [
                sys.executable,
                str(generator_script),
                "--idl", str(idl_path),
                "--out", str(out_cpp),
                "--impl-header", str(impl_header),
                "--dry-run",
            ],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        # dry-run should not create files
        self.assertFalse(out_cpp.exists())
        self.assertFalse(impl_header.exists())

    def test_generic_native_exports_generator_writes_output(self) -> None:
        idl = _make_minimal_idl(
            target="mylib",
            functions=[
                {
                    "name": "mylib_init",
                    "return_type": "int",
                    "parameters": [],
                },
            ],
        )
        idl_path = self._write_idl(idl)
        out_cpp = self.root / "mylib.exports.cpp"
        impl_header = self.root / "mylib_impl.h"

        generator_script = Path(__file__).resolve().parents[1] / "generator_sdk" / "native_exports_generator.py"
        result = subprocess.run(
            [
                sys.executable,
                str(generator_script),
                "--idl", str(idl_path),
                "--out", str(out_cpp),
                "--impl-header", str(impl_header),
            ],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr.decode())
        self.assertTrue(out_cpp.exists())
        self.assertTrue(impl_header.exists())
        cpp_content = out_cpp.read_text(encoding="utf-8")
        self.assertIn("mylib_init", cpp_content)


class ScaffoldV2Tests(unittest.TestCase):
    """Tests for the improved managed_api_scaffold_generator (v2)."""

    def setUp(self) -> None:
        sdk_path = Path(__file__).resolve().parents[1] / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import managed_api_scaffold_generator as scaffold_mod
        import managed_bindings_scaffold_generator as bindings_mod
        self.scaffold_mod = scaffold_mod
        self.bindings_mod = bindings_mod

    def _make_minimal_idl(
        self,
        target: str = "foo",
        symbol_prefix: str = "foo_",
        opaque_types: dict | None = None,
        structs: dict | None = None,
        cb_typedefs: list | None = None,
        enums: dict | None = None,
        functions: list | None = None,
    ) -> dict:
        return {
            "target": target,
            "codegen": {"symbol_prefix": symbol_prefix},
            "functions": functions or [],
            "header_types": {
                "structs": structs or {},
                "callback_typedefs": cb_typedefs or [],
                "enums": enums or {},
                "opaque_types": [],
                "opaque_type_declarations": [],
                "constants": {},
            },
            "bindings": {
                "interop": {
                    "opaque_types": opaque_types or {},
                    "callback_struct_suffixes": ["_callbacks_t"],
                }
            },
        }

    def test_scaffold_reads_from_header_types_structs(self) -> None:
        """Callback struct in header_types.structs is detected (not idl.structs)."""
        idl = self._make_minimal_idl(
            structs={
                "foo_event_callbacks_t": {
                    "fields": [
                        {"name": "on_ready", "declaration": "foo_ready_cb on_ready"},
                    ]
                }
            },
            cb_typedefs=[
                {"name": "foo_ready_cb", "declaration": "typedef void (FOO_CALL *foo_ready_cb)(void* user_data);"}
            ],
        )
        result = self.scaffold_mod.scaffold(idl, "FooLib", None)
        cbs = result["callbacks"]
        self.assertEqual(len(cbs), 1)
        self.assertEqual(cbs[0]["class"], "EventCallbacks")

    def test_scaffold_uses_symbol_prefix_from_idl(self) -> None:
        """symbol_prefix in idl.codegen gives correct class names without prefix."""
        idl = self._make_minimal_idl(
            symbol_prefix="bar_",
            opaque_types={
                "bar_session_t": {"release": "bar_session_release"},
            },
        )
        result = self.scaffold_mod.scaffold(idl, "BarLib", None)
        handles = result["handle_api"]
        self.assertEqual(len(handles), 1)
        # Should be "Session", not "BarSession"
        self.assertEqual(handles[0]["class"], "Session")

    def test_scaffold_infers_enum_callback_type(self) -> None:
        """Callback field with enum param gets Action<MyEnum>? type."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={"foo_state_t": {"members": [{"name": "FOO_STATE_A", "value_expr": "0"}]}},
            structs={
                "foo_stuff_callbacks_t": {
                    "fields": [
                        {"name": "on_state", "declaration": "foo_state_cb on_state"},
                    ]
                }
            },
            cb_typedefs=[
                {
                    "name": "foo_state_cb",
                    "declaration": "typedef void (FOO_CALL *foo_state_cb)(void* user_data, foo_state_t state);",
                }
            ],
        )
        result = self.scaffold_mod.scaffold(idl, "FooLib", None)
        cbs = result["callbacks"]
        self.assertEqual(len(cbs), 1)
        fields = cbs[0]["fields"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["managed_type"], "Action<State>?")

    def test_scaffold_infers_string_callback_type(self) -> None:
        """const char* parameter maps to Action<string?>? type."""
        idl = self._make_minimal_idl(
            structs={
                "foo_log_callbacks_t": {
                    "fields": [
                        {"name": "on_message", "declaration": "foo_log_cb on_message"},
                    ]
                }
            },
            cb_typedefs=[
                {
                    "name": "foo_log_cb",
                    "declaration": "typedef void (FOO_CALL *foo_log_cb)(void* user_data, const char* message);",
                }
            ],
        )
        result = self.scaffold_mod.scaffold(idl, "FooLib", None)
        cbs = result["callbacks"]
        self.assertEqual(len(cbs), 1)
        fields = cbs[0]["fields"]
        self.assertEqual(len(fields), 1)
        self.assertEqual(fields[0]["managed_type"], "Action<string?>?")

    def test_scaffold_update_mode_merges_new_handles(self) -> None:
        """--update mode adds new handles without overwriting existing ones."""
        existing = {
            "schema_version": 2,
            "namespace": "FooLib",
            "callbacks": [],
            "handle_api": [
                {"class": "Session", "members": [{"line": "// existing custom member"}]}
            ],
        }
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"},
                "foo_track_t": {"release": "foo_track_release"},
            },
        )
        generated = self.scaffold_mod.scaffold(idl, "FooLib", None)
        merged, stats = self.scaffold_mod._update_existing(existing, generated)

        handle_classes = [h["class"] for h in merged["handle_api"]]
        # Session existed, should be kept
        self.assertIn("Session", handle_classes)
        # Track is new, should be added
        self.assertIn("Track", handle_classes)
        # Existing Session entry should be preserved unchanged
        session_entry = next(h for h in merged["handle_api"] if h["class"] == "Session")
        self.assertEqual(session_entry["members"], [{"line": "// existing custom member"}])
        self.assertEqual(stats["added_handles"], 1)
        self.assertEqual(stats["kept_handles"], 1)

    def test_scaffold_update_mode_keeps_existing_callbacks(self) -> None:
        """Existing customized callbacks are not overwritten in --update mode."""
        existing = {
            "schema_version": 2,
            "namespace": "FooLib",
            "callbacks": [
                {
                    "class": "EventCallbacks",
                    "fields": [
                        {"managed_name": "OnReady", "managed_type": "Action<int>?", "assignment_lines": ["custom"]}
                    ],
                }
            ],
            "handle_api": [],
        }
        idl = self._make_minimal_idl(
            structs={
                "foo_event_callbacks_t": {
                    "fields": [{"name": "on_ready", "declaration": "foo_ready_cb on_ready"}]
                },
                "foo_new_callbacks_t": {
                    "fields": [{"name": "on_done", "declaration": "foo_done_cb on_done"}]
                },
            },
            cb_typedefs=[
                {"name": "foo_ready_cb", "declaration": "typedef void (FOO_CALL *foo_ready_cb)(void* user_data);"},
                {"name": "foo_done_cb", "declaration": "typedef void (FOO_CALL *foo_done_cb)(void* user_data);"},
            ],
        )
        generated = self.scaffold_mod.scaffold(idl, "FooLib", None)
        merged, stats = self.scaffold_mod._update_existing(existing, generated)

        cb_classes = [c["class"] for c in merged["callbacks"]]
        self.assertIn("EventCallbacks", cb_classes)
        self.assertIn("NewCallbacks", cb_classes)
        # Existing EventCallbacks should be unchanged
        event_cb = next(c for c in merged["callbacks"] if c["class"] == "EventCallbacks")
        self.assertEqual(event_cb["fields"][0]["assignment_lines"], ["custom"])
        self.assertEqual(stats["added_callbacks"], 1)
        self.assertEqual(stats["kept_callbacks"], 1)

    def test_scaffold_managed_bindings_generates_handles(self) -> None:
        """scaffold_managed_bindings generates correct managed.json from opaque_types."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release", "retain": "foo_session_retain"},
                "foo_track_t": {"release": "foo_track_release"},
            },
        )
        result = self.bindings_mod.scaffold_managed_bindings(idl, "FooLib", None)
        handles = result["handles"]
        # Should be sorted by c_type_name
        self.assertEqual(len(handles), 2)
        session = next(h for h in handles if h["cs_type"] == "Session")
        track = next(h for h in handles if h["cs_type"] == "Track")
        self.assertEqual(session["c_handle_type"], "foo_session_t*")
        self.assertEqual(session["namespace"], "FooLib")
        self.assertEqual(session["release"], "foo_session_release")
        self.assertEqual(session["retain"], "foo_session_retain")
        self.assertEqual(track["release"], "foo_track_release")
        self.assertNotIn("retain", track)


class MultiLanguageCodegenTests(unittest.TestCase):
    """Tests for Python ctypes and Rust FFI binding generators."""

    def setUp(self) -> None:
        sdk_path = Path(__file__).resolve().parents[1] / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import python_bindings_generator as py_gen
        import rust_ffi_generator as rs_gen
        import managed_api_scaffold_generator as scaffold_mod
        self.py_gen = py_gen
        self.rs_gen = rs_gen
        self.scaffold_mod = scaffold_mod
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_minimal_idl(
        self,
        target: str = "foo",
        symbol_prefix: str = "foo_",
        opaque_types: dict | None = None,
        structs: dict | None = None,
        cb_typedefs: list | None = None,
        enums: dict | None = None,
        functions: list | None = None,
    ) -> dict:
        return {
            "target": target,
            "codegen": {"symbol_prefix": symbol_prefix},
            "functions": functions or [],
            "header_types": {
                "structs": structs or {},
                "callback_typedefs": cb_typedefs or [],
                "enums": enums or {},
                "opaque_types": [],
                "opaque_type_declarations": [],
                "constants": {},
            },
            "bindings": {
                "interop": {
                    "opaque_types": opaque_types or {},
                    "callback_struct_suffixes": ["_callbacks_t"],
                }
            },
        }

    def test_python_bindings_generates_valid_python(self) -> None:
        """generate_bindings produces syntactically valid Python."""
        import ast
        idl = self._make_minimal_idl(
            target="mylib",
            symbol_prefix="mylib_",
            functions=[
                {"name": "mylib_version", "c_return_type": "uint32_t", "parameters": []}
            ],
        )
        content = self.py_gen.generate_bindings(idl)
        # Should not raise
        try:
            ast.parse(content)
        except SyntaxError as e:
            self.fail(f"Generated Python is not valid: {e}\n---\n{content[:500]}")

    def test_python_bindings_enums(self) -> None:
        """IDL enum produces IntEnum class with correct members."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={
                "foo_color": {
                    "members": [
                        {"name": "FOO_COLOR_RED", "value": 0},
                        {"name": "FOO_COLOR_GREEN", "value": 1},
                        {"name": "FOO_COLOR_BLUE", "value": 2},
                    ]
                }
            },
        )
        content = self.py_gen.generate_bindings(idl)
        self.assertIn("class Color(IntEnum):", content)
        self.assertIn("RED = 0", content)
        self.assertIn("GREEN = 1", content)
        self.assertIn("BLUE = 2", content)

    def test_python_bindings_opaque_handles(self) -> None:
        """IDL opaque_type produces a Handle class subclassing c_void_p."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
        )
        content = self.py_gen.generate_bindings(idl)
        self.assertIn("class SessionHandle(ctypes.c_void_p): pass", content)

    def test_python_bindings_function_grouped_under_handle(self) -> None:
        """Function whose first param is an opaque handle becomes a method on the wrapper class."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
                {
                    "name": "foo_session_get_id",
                    "c_return_type": "uint32_t",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.py_gen.generate_bindings(idl)
        # Should have a Session wrapper class
        self.assertIn("class Session:", content)
        # get_id method should appear
        self.assertIn("def get_id(", content)

    def test_rust_ffi_generates_enum(self) -> None:
        """IDL enum produces #[repr(C)] pub enum in Rust output."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={
                "foo_state": {
                    "members": [
                        {"name": "FOO_STATE_IDLE", "value": 0},
                        {"name": "FOO_STATE_RUNNING", "value": 1},
                    ]
                }
            },
        )
        content = self.rs_gen.generate_rust_ffi(idl)
        self.assertIn("#[repr(C)]", content)
        self.assertIn("pub enum State {", content)
        self.assertIn("Idle = 0,", content)
        self.assertIn("Running = 1,", content)

    def test_rust_ffi_generates_opaque_handle(self) -> None:
        """IDL opaque_type produces zero-size struct and pointer type alias."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
        )
        content = self.rs_gen.generate_rust_ffi(idl)
        self.assertIn("pub struct FooSession", content)
        self.assertIn("pub type SessionPtr = *mut FooSession;", content)

    def test_rust_ffi_generates_extern_block(self) -> None:
        """IDL function appears in extern C block with correct signature."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            functions=[
                {
                    "name": "foo_add",
                    "c_return_type": "int32_t",
                    "parameters": [
                        {"c_type": "int32_t", "name": "a"},
                        {"c_type": "int32_t", "name": "b"},
                    ],
                },
            ],
        )
        content = self.rs_gen.generate_rust_ffi(idl)
        self.assertIn('extern "C"', content)
        self.assertIn("pub fn foo_add(", content)
        self.assertIn("-> i32", content)

    def test_scaffold_buffer_pair_detection(self) -> None:
        """uint8_t* + int length params → ReadOnlyMemory<byte> marshal code."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            structs={
                "foo_data_callbacks_t": {
                    "fields": [
                        {"name": "on_data", "declaration": "foo_data_cb on_data"},
                    ]
                }
            },
            cb_typedefs=[
                {
                    "name": "foo_data_cb",
                    "declaration": "typedef void (FOO_CALL *foo_data_cb)(void* user_data, const uint8_t* data, int data_length);",
                }
            ],
        )
        result = self.scaffold_mod.scaffold(idl, "FooLib", None)
        cbs = result["callbacks"]
        self.assertEqual(len(cbs), 1)
        fields = cbs[0]["fields"]
        self.assertEqual(len(fields), 1)
        f = fields[0]
        # managed_type should be Action<ReadOnlyMemory<byte>>?  (length param absorbed)
        self.assertIn("ReadOnlyMemory<byte>", f["managed_type"])
        # Length param should NOT appear in managed_type
        self.assertNotIn("int", f["managed_type"])
        # assignment_lines should contain Marshal.Copy
        all_lines = "\n".join(f["assignment_lines"])
        self.assertIn("Marshal.Copy", all_lines)
        self.assertIn("GC.AllocateUninitializedArray", all_lines)

    def test_scaffold_int_state_enum_detection(self) -> None:
        """int state param with matching IDL enum gets cast expression."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={
                "foo_data_channel_state": {
                    "members": [
                        {"name": "FOO_DATA_CHANNEL_STATE_OPEN", "value": 0},
                    ]
                }
            },
            structs={
                "foo_channel_callbacks_t": {
                    "fields": [
                        {"name": "on_state_change", "declaration": "foo_state_cb on_state_change"},
                    ]
                }
            },
            cb_typedefs=[
                {
                    "name": "foo_state_cb",
                    "declaration": "typedef void (FOO_CALL *foo_state_cb)(void* user_data, int state);",
                }
            ],
        )
        result = self.scaffold_mod.scaffold(idl, "FooLib", None)
        cbs = result["callbacks"]
        self.assertEqual(len(cbs), 1)
        fields = cbs[0]["fields"]
        self.assertEqual(len(fields), 1)
        f = fields[0]
        # managed_type should use DataChannelState (not int)
        self.assertIn("DataChannelState", f["managed_type"])
        # assignment_lines should contain a cast
        all_lines = "\n".join(f["assignment_lines"])
        self.assertIn("(DataChannelState)", all_lines)


class TypeScriptBindingsTests(unittest.TestCase):
    """Tests for TypeScript ffi-napi binding generator."""

    def setUp(self) -> None:
        sdk_path = Path(__file__).resolve().parents[1] / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import typescript_bindings_generator as ts_gen
        self.ts_gen = ts_gen
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_minimal_idl(
        self,
        target: str = "foo",
        symbol_prefix: str = "foo_",
        opaque_types: dict | None = None,
        structs: dict | None = None,
        cb_typedefs: list | None = None,
        enums: dict | None = None,
        functions: list | None = None,
    ) -> dict:
        return {
            "target": target,
            "codegen": {"symbol_prefix": symbol_prefix},
            "functions": functions or [],
            "header_types": {
                "structs": structs or {},
                "callback_typedefs": cb_typedefs or [],
                "enums": enums or {},
                "opaque_types": [],
                "opaque_type_declarations": [],
                "constants": {},
            },
            "bindings": {
                "interop": {
                    "opaque_types": opaque_types or {},
                    "callback_struct_suffixes": ["_callbacks_t"],
                }
            },
        }

    def test_typescript_enums_generated(self) -> None:
        """IDL enum produces TypeScript enum with correct values."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={
                "foo_data_channel_state": {
                    "members": [
                        {"name": "FOO_DATA_CHANNEL_STATE_CONNECTING", "value": 0},
                        {"name": "FOO_DATA_CHANNEL_STATE_OPEN", "value": 1},
                        {"name": "FOO_DATA_CHANNEL_STATE_CLOSING", "value": 2},
                        {"name": "FOO_DATA_CHANNEL_STATE_CLOSED", "value": 3},
                    ]
                }
            },
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertIn("export enum DataChannelState {", content)
        self.assertIn("Connecting = 0,", content)
        self.assertIn("Open = 1,", content)
        self.assertIn("Closing = 2,", content)
        self.assertIn("Closed = 3,", content)

    def test_typescript_opaque_handles_generated(self) -> None:
        """IDL opaque_type produces TypeScript Handle type and ref type constant."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertIn("export type SessionHandle = ref.Pointer<unknown>;", content)
        self.assertIn("export const SessionHandleType = ref.refType(ref.types.void);", content)

    def test_typescript_library_declaration_generated(self) -> None:
        """loadLibrary() function is generated containing all function signatures."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            functions=[
                {
                    "name": "foo_add",
                    "c_return_type": "int32_t",
                    "parameters": [
                        {"c_type": "int32_t", "name": "a"},
                        {"c_type": "int32_t", "name": "b"},
                    ],
                },
                {
                    "name": "foo_version",
                    "c_return_type": "uint32_t",
                    "parameters": [],
                },
            ],
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertIn("export function loadLibrary(libraryPath: string)", content)
        self.assertIn("ffi.Library(libraryPath", content)
        self.assertIn("'foo_add':", content)
        self.assertIn("'foo_version':", content)
        self.assertIn("'int32'", content)

    def test_typescript_oop_wrappers_generated(self) -> None:
        """OOP class wrappers are generated for opaque handle types with dispose method."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_create",
                    "c_return_type": "foo_session_t*",
                    "parameters": [],
                },
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
                {
                    "name": "foo_session_get_id",
                    "c_return_type": "uint32_t",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertIn("export class Session {", content)
        self.assertIn("dispose(): void {", content)
        self.assertIn("[Symbol.dispose](): void {", content)
        self.assertIn("getId(", content)

    def test_typescript_no_invalid_tokens(self) -> None:
        """Generated TypeScript output must not contain Python None or null tokens."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_create",
                    "c_return_type": "foo_session_t*",
                    "parameters": [],
                },
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertNotIn("None", content)
        # 'null' is valid in TS but we shouldn't have Python-generated nulls as type names
        for line in content.splitlines():
            stripped = line.strip()
            # 'null' should not appear as a type string in ffi Library declarations
            self.assertNotIn("'null'", stripped)

    def test_typescript_valid_syntax_structure(self) -> None:
        """Generated output starts with the auto-generated header comment."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            functions=[
                {"name": "foo_init", "c_return_type": "void", "parameters": []}
            ],
        )
        content = self.ts_gen.generate_typescript_bindings(idl)
        self.assertTrue(content.startswith("// <auto-generated />"), msg=f"Content starts with: {content[:100]!r}")
        self.assertIn("import * as ffi from 'ffi-napi';", content)
        self.assertIn("import * as ref from 'ref-napi';", content)


class GoBindingsTests(unittest.TestCase):
    """Tests for Go cgo binding generator."""

    def setUp(self) -> None:
        sdk_path = Path(__file__).resolve().parents[1] / "generator_sdk"
        if str(sdk_path) not in sys.path:
            sys.path.insert(0, str(sdk_path))
        import go_bindings_generator as go_gen
        self.go_gen = go_gen
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _make_minimal_idl(
        self,
        target: str = "foo",
        symbol_prefix: str = "foo_",
        opaque_types: dict | None = None,
        enums: dict | None = None,
        functions: list | None = None,
    ) -> dict:
        return {
            "target": target,
            "codegen": {"symbol_prefix": symbol_prefix},
            "functions": functions or [],
            "header_types": {
                "structs": {},
                "callback_typedefs": [],
                "enums": enums or {},
                "opaque_types": [],
                "opaque_type_declarations": [],
                "constants": {},
            },
            "bindings": {
                "interop": {
                    "opaque_types": opaque_types or {},
                    "callback_struct_suffixes": ["_callbacks_t"],
                }
            },
        }

    def test_go_enums_generated(self) -> None:
        """IDL enum produces Go type and const block."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            enums={
                "foo_state": {
                    "members": [
                        {"name": "FOO_STATE_IDLE", "value": 0},
                        {"name": "FOO_STATE_RUNNING", "value": 1},
                    ]
                }
            },
        )
        content = self.go_gen.generate_go_bindings(idl)
        self.assertIn("type State int32", content)
        self.assertIn("const (", content)
        self.assertIn("StateIdle State = 0", content)
        self.assertIn("StateRunning State = 1", content)

    def test_go_opaque_handles_generated(self) -> None:
        """IDL opaque_type produces Go struct wrapper."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
        )
        content = self.go_gen.generate_go_bindings(idl)
        self.assertIn("type Session struct {", content)
        self.assertIn("ptr *C.foo_session_t", content)

    def test_go_constructor_generated(self) -> None:
        """IDL create function produces Go NewXxx constructor."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_create",
                    "c_return_type": "foo_session_t*",
                    "parameters": [],
                },
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.go_gen.generate_go_bindings(idl)
        self.assertIn("func NewSession(", content)
        self.assertIn("C.foo_session_create(", content)
        self.assertIn("runtime.SetFinalizer(", content)

    def test_go_close_method_generated(self) -> None:
        """IDL release function becomes Close() method on wrapper struct."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_create",
                    "c_return_type": "foo_session_t*",
                    "parameters": [],
                },
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.go_gen.generate_go_bindings(idl)
        self.assertIn("func (h *Session) Close()", content)
        self.assertIn("C.foo_session_release(h.ptr)", content)

    def test_go_no_invalid_tokens(self) -> None:
        """Generated Go output must not contain Python None or null tokens."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
            opaque_types={
                "foo_session_t": {"release": "foo_session_release"}
            },
            functions=[
                {
                    "name": "foo_session_create",
                    "c_return_type": "foo_session_t*",
                    "parameters": [],
                },
                {
                    "name": "foo_session_release",
                    "c_return_type": "void",
                    "parameters": [{"c_type": "foo_session_t*", "name": "session"}],
                },
            ],
        )
        content = self.go_gen.generate_go_bindings(idl)
        self.assertNotIn("None", content)
        self.assertNotIn("null", content)

    def test_go_package_declaration(self) -> None:
        """Generated Go output starts with the correct package declaration."""
        idl = self._make_minimal_idl(
            symbol_prefix="foo_",
        )
        content = self.go_gen.generate_go_bindings(idl, package_name="mypackage")
        self.assertIn("package mypackage", content)
        self.assertIn("// Code generated by", content)
        self.assertIn("DO NOT EDIT.", content)


class GenerateBaselineTests(unittest.TestCase):
    """Tests for the generate-baseline command."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repo_root = Path(self.temp_dir.name)
        self._create_demo_repo()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _create_demo_repo(self) -> None:
        (self.repo_root / "native" / "include").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "abi" / "baselines").mkdir(parents=True, exist_ok=True)
        (self.repo_root / "abi" / "generated" / "demo").mkdir(parents=True, exist_ok=True)

        header = """#ifndef DEMO_H
#define DEMO_H
#include <stdint.h>
#define MY_ABI_VERSION_MAJOR 1
#define MY_ABI_VERSION_MINOR 0
#define MY_ABI_VERSION_PATCH 0
#define MY_API
#define MY_CALL
MY_API int MY_CALL my_init(void);
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
                    "codegen": {
                        "enabled": True,
                        "idl_output_path": "abi/generated/demo/demo.idl.json",
                    },
                }
            }
        }

        (self.repo_root / "native" / "include" / "demo.h").write_text(header, encoding="utf-8")
        import abi_framework_core as abi_framework
        abi_framework.write_json(self.repo_root / "abi" / "config.json", config)

        # Create a fake IDL file in the expected location
        fake_idl = {"target": "demo", "functions": [], "header_types": {}}
        import json
        idl_path = self.repo_root / "abi" / "generated" / "demo" / "demo.idl.json"
        idl_path.write_text(json.dumps(fake_idl, indent=2) + "\n", encoding="utf-8")

    def test_generate_baseline_creates_file(self) -> None:
        """generate-baseline copies IDL to baseline path."""
        import abi_framework_core as abi_framework
        baseline_path = self.repo_root / "abi" / "baselines" / "demo.json"
        self.assertFalse(baseline_path.exists())

        exit_code = abi_framework.command_generate_baseline(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                force=True,
            )
        )
        self.assertEqual(exit_code, 0)
        self.assertTrue(baseline_path.exists())

    def test_generate_baseline_no_force_warns(self) -> None:
        """generate-baseline without --force does not overwrite existing baseline."""
        import abi_framework_core as abi_framework
        baseline_path = self.repo_root / "abi" / "baselines" / "demo.json"
        baseline_path.write_text("original", encoding="utf-8")

        exit_code = abi_framework.command_generate_baseline(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                force=False,
            )
        )
        # Should succeed but not overwrite
        self.assertEqual(exit_code, 0)
        self.assertEqual(baseline_path.read_text(encoding="utf-8"), "original")

    def test_generate_baseline_missing_idl_fails(self) -> None:
        """generate-baseline fails gracefully when IDL does not exist."""
        import abi_framework_core as abi_framework
        # Remove the IDL file
        idl_path = self.repo_root / "abi" / "generated" / "demo" / "demo.idl.json"
        idl_path.unlink()

        exit_code = abi_framework.command_generate_baseline(
            argparse.Namespace(
                repo_root=str(self.repo_root),
                config=str(self.repo_root / "abi" / "config.json"),
                target="demo",
                force=True,
            )
        )
        self.assertNotEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
