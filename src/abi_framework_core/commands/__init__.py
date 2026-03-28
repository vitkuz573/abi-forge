from .generation import command_codegen, command_generate, command_sync
from .governance import command_changelog, command_doctor, command_waiver_audit
from .performance import command_benchmark, command_benchmark_gate
from .plugins import command_validate_plugin_manifest
from .release import command_release_prepare
from .targets import (
    command_init_target,
    command_list_targets,
    command_scaffold_managed_api,
    command_scaffold_managed_bindings,
    command_generate_python_bindings,
    command_generate_rust_ffi,
)
from .verification import (
    command_diff,
    command_regen_baselines,
    command_snapshot,
    command_verify,
    command_verify_all,
)
from .bootstrap import command_bootstrap
from .status import command_status

__all__ = [
    "command_benchmark",
    "command_benchmark_gate",
    "command_bootstrap",
    "command_changelog",
    "command_codegen",
    "command_diff",
    "command_doctor",
    "command_generate",
    "command_generate_python_bindings",
    "command_generate_rust_ffi",
    "command_init_target",
    "command_list_targets",
    "command_regen_baselines",
    "command_release_prepare",
    "command_scaffold_managed_api",
    "command_scaffold_managed_bindings",
    "command_snapshot",
    "command_status",
    "command_sync",
    "command_validate_plugin_manifest",
    "command_verify",
    "command_verify_all",
    "command_waiver_audit",
]
