from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "tools" / "abi_codegen_core" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from abi_codegen_core.required_functions import derive_required_functions


class RequiredFunctionsTests(unittest.TestCase):
    def test_discovers_required_native_functions_from_patterns(self) -> None:
        payload = {
            "callbacks": [
                {"line": "NativeMethods.my_start(handle);"},
                {"line": "NativeMethods.my_stop(handle); NativeMethods.other();"},
            ]
        }
        idl_names = {"my_start", "my_stop", "my_unused"}
        patterns = [r"\bNativeMethods\.([A-Za-z_][A-Za-z0-9_]*)\b"]

        actual = derive_required_functions(payload, idl_names, patterns)
        self.assertEqual(actual, ["my_start", "my_stop"])

    def test_applies_function_name_filter(self) -> None:
        payload = {"x": "NativeMethods.my_start(); NativeMethods.debug_probe();"}
        idl_names = {"my_start", "debug_probe"}
        patterns = [r"\bNativeMethods\.([A-Za-z_][A-Za-z0-9_]*)\b"]

        actual = derive_required_functions(payload, idl_names, patterns, function_name_pattern=r"^my_[a-z0-9_]+$")
        self.assertEqual(actual, ["my_start"])


if __name__ == "__main__":
    unittest.main()
