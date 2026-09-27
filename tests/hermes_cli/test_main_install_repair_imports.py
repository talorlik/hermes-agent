"""Regression tests for hermes_cli.main_install_repair imports.

After G-UPSTREAM-HYGIENE fork sync, several symbols imported by main.py
were missing from main_install_repair.py, breaking CLI startup.
"""

import pytest


def test_recover_from_interrupted_install_importable():
    """_recover_from_interrupted_install must be importable from main_install_repair.
    
    Regression for ImportError after fork sync that removed PM refactor stubs.
    """
    from hermes_cli.main_install_repair import _recover_from_interrupted_install
    assert callable(_recover_from_interrupted_install)


def test_shim_quarantine_error_importable():
    """ShimQuarantineError exception class must be importable."""
    from hermes_cli.main_install_repair import ShimQuarantineError
    assert issubclass(ShimQuarantineError, RuntimeError)


def test_all_main_imports_exist():
    """All symbols that main.py imports from main_install_repair must exist.
    
    Covers the full import list from main.py lines 1000-1027.
    """
    from hermes_cli import main_install_repair
    
    required_symbols = [
        # First import block (lines 1000-1003)
        "_cleanup_quarantined_exes",
        "_recover_from_interrupted_install",
        # Second import block (lines 1004-1027)
        "ShimQuarantineError",
        "_UPDATE_REEXEC_ENV",
        "_clear_lazy_refresh_incomplete_marker",
        "_clear_marker_file",
        "_clear_update_incomplete_marker",
        "_install_python_dependencies_with_optional_fallback",
        "_is_termux_env",
        "_is_windows",
        "_is_windows_npm_path",
        "_lazy_refresh_marker_path",
        "_pytest_owns_live_checkout",
        "_reexec_dependency_sync_off_windows_shim",
        "_repair_venv_via_import_probes",
        "_resolve_install_target_python",
        "_resolve_node_runtime_npm",
        "_resolve_update_branch",
        "_run_install_with_heartbeat",
        "_run_package_only_install",
        "_update_marker_path",
        "_venv_scripts_dir",
        "_verify_console_scripts_installed",
        "_verify_core_dependencies_installed",
    ]
    
    for symbol_name in required_symbols:
        assert hasattr(main_install_repair, symbol_name), \
            f"main_install_repair.py must export {symbol_name} (imported by main.py)"


def test_recover_function_is_no_op_stub():
    """_recover_from_interrupted_install is a stub that doesn't crash.
    
    The full recovery implementation was removed in the PM refactor;
    the stub exists only to satisfy imports. It should not raise.
    """
    from hermes_cli.main_install_repair import _recover_from_interrupted_install
    # Should not raise
    _recover_from_interrupted_install()


def test_stubs_accept_expected_signatures():
    """PM refactor stubs must accept the signatures main.py/update_cmd use."""
    from hermes_cli.main_install_repair import (
        _install_python_dependencies_with_optional_fallback,
        _repair_venv_via_import_probes,
        _resolve_install_target_python,
        _run_install_with_heartbeat,
        _run_package_only_install,
        _verify_console_scripts_installed,
        _verify_core_dependencies_installed,
    )
    from pathlib import Path
    
    # These are all no-op stubs; just verify they accept the right args
    _run_package_only_install(["pip", "install", "foo"])
    _run_package_only_install(["pip", "install", "foo"], env={"FOO": "bar"})
    
    result = _repair_venv_via_import_probes(["pip", "install"])
    assert result == "healthy"
    
    _install_python_dependencies_with_optional_fallback(["pip", "install"])
    _install_python_dependencies_with_optional_fallback(["pip"], env={}, group="all")
    
    _verify_console_scripts_installed(["pip"], env={})
    _verify_core_dependencies_installed(["pip"], env={}, group="all")
    
    # Returns None (stub)
    assert _resolve_install_target_python(["pip"], {}) is None
    
    _run_install_with_heartbeat(
        ["pip", "install", "foo"],
        env={},
        cwd=Path("/tmp"),
        timeout=300,
    )
