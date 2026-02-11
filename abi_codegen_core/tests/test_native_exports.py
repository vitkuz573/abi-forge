from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC_ROOT = REPO_ROOT / "tools" / "abi_codegen_core" / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from abi_codegen_core.native_exports import NativeExportRenderOptions, render_exports, render_impl_header


class NativeExportsRenderTests(unittest.TestCase):
    def test_renders_with_configurable_macros_and_prefixes(self) -> None:
        functions = [
            {
                "name": "abc_ping",
                "c_return_type": "int32_t",
                "parameters": [{"name": "value", "c_type": "int32_t"}],
            },
            {
                "name": "abc_shutdown",
                "c_return_type": "void",
                "parameters": [],
            },
        ]
        options = NativeExportRenderOptions(
            header_include="my_api.h",
            impl_header_include="my_api_impl.h",
            api_macro="MY_API",
            call_macro="MY_CALL",
            impl_prefix="my_impl_",
            symbol_prefix="abc_",
        )

        exports = render_exports(functions, options, "tools/custom.py")
        impl_header = render_impl_header(functions, options)

        self.assertIn('#include "my_api.h"', exports)
        self.assertIn('#include "my_api_impl.h"', exports)
        self.assertIn("MY_API int32_t MY_CALL abc_ping(int32_t value)", exports)
        self.assertIn("return my_impl_ping(value);", exports)
        self.assertIn("MY_API void MY_CALL abc_shutdown(void)", exports)
        self.assertIn("my_impl_shutdown();", exports)

        self.assertIn('#include "my_api.h"', impl_header)
        self.assertIn("int32_t MY_CALL my_impl_ping(int32_t value);", impl_header)
        self.assertIn("void MY_CALL my_impl_shutdown(void);", impl_header)


if __name__ == "__main__":
    unittest.main()
