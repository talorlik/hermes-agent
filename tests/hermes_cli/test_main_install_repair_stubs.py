"""Tests for main_install_repair import stubs.

Regression test for missing imports that broke CLI startup after PM refactor.
These functions were removed in commit 8b7eae99ef (fix(pm): own interpreter
selection and dependency recovery) but are still imported by main.py.
"""

import pytest


def test_recover_from_interrupted_install_importable():
    """_recover_from_interrupted_install must be importable."""
    from hermes_cli.main_install_repair import _recover_from_interrupted_install

    assert callable(_recover_from_interrupted_install)


def test_recover_from_interrupted_install_stub_is_safe():
    """_recover_from_interrupted_install stub can be called without errors."""
    from hermes_cli.main_install_repair import _recover_from_interrupted_install

    # Should not raise - it's a stub that clears any stale markers
    _recover_from_interrupted_install()


def test_all_updater_surface_imports():
    """All symbols imported by main.py from main_install_repair must exist."""
    # These are the imports from main.py lines 1000-1027
    from hermes_cli.main_install_repair import (
        ShimQuarantineError,
        _UPDATE_REEXEC_ENV,
        _cleanup_quarantined_exes,
        _clear_lazy_refresh_incomplete_marker,
        _clear_marker_file,
        _clear_update_incomplete_marker,
        _install_python_dependencies_with_optional_fallback,
        _is_termux_env,
        _is_windows,
        _is_windows_npm_path,
        _lazy_refresh_marker_path,
        _pytest_owns_live_checkout,
        _reexec_dependency_sync_off_windows_shim,
        _recover_from_interrupted_install,
        _repair_venv_via_import_probes,
        _resolve_install_target_python,
        _resolve_node_runtime_npm,
        _resolve_update_branch,
        _run_install_with_heartbeat,
        _run_package_only_install,
        _update_marker_path,
        _venv_scripts_dir,
        _verify_console_scripts_installed,
        _verify_core_dependencies_installed,
    )

    # Verify key types
    assert isinstance(ShimQuarantineError, type)
    assert issubclass(ShimQuarantineError, RuntimeError)
    assert isinstance(_UPDATE_REEXEC_ENV, str)

    # Verify all are callable except the exception class and constant
    assert callable(_cleanup_quarantined_exes)
    assert callable(_clear_lazy_refresh_incomplete_marker)
    assert callable(_clear_marker_file)
    assert callable(_clear_update_incomplete_marker)
    assert callable(_install_python_dependencies_with_optional_fallback)
    assert callable(_is_termux_env)
    assert callable(_is_windows)
    assert callable(_is_windows_npm_path)
    assert callable(_lazy_refresh_marker_path)
    assert callable(_pytest_owns_live_checkout)
    assert callable(_reexec_dependency_sync_off_windows_shim)
    assert callable(_recover_from_interrupted_install)
    assert callable(_repair_venv_via_import_probes)
    assert callable(_resolve_install_target_python)
    assert callable(_resolve_node_runtime_npm)
    assert callable(_resolve_update_branch)
    assert callable(_run_install_with_heartbeat)
    assert callable(_run_package_only_install)
    assert callable(_update_marker_path)
    assert callable(_venv_scripts_dir)
    assert callable(_verify_console_scripts_installed)
    assert callable(_verify_core_dependencies_installed)


def test_shim_quarantine_error_is_exception():
    """ShimQuarantineError is a proper exception class."""
    from hermes_cli.main_install_repair import ShimQuarantineError

    # Can be raised and caught
    with pytest.raises(ShimQuarantineError, match="test"):
        raise ShimQuarantineError("test")

    # Is a RuntimeError subclass
    with pytest.raises(RuntimeError):
        raise ShimQuarantineError("test")


def test_repair_venv_via_import_probes_stub():
    """_repair_venv_via_import_probes stub returns expected type."""
    from hermes_cli.main_install_repair import _repair_venv_via_import_probes

    # Stub returns "healthy" status
    result = _repair_venv_via_import_probes(["python", "-m", "pip"])
    assert isinstance(result, str)
    assert result == "healthy"


def test_resolve_install_target_python_stub():
    """_resolve_install_target_python stub returns expected type."""
    from hermes_cli.main_install_repair import _resolve_install_target_python

    # Stub returns None
    result = _resolve_install_target_python(["python"], None)
    assert result is None


def test_stub_functions_accept_expected_signatures():
    """Stub functions accept the signatures their call sites expect."""
    from hermes_cli.main_install_repair import (
        _install_python_dependencies_with_optional_fallback,
        _repair_venv_via_import_probes,
        _resolve_install_target_python,
        _run_install_with_heartbeat,
        _run_package_only_install,
        _verify_console_scripts_installed,
        _verify_core_dependencies_installed,
    )

    # These should all accept their expected arguments without errors
    _run_package_only_install(["echo", "test"], env=None)
    _repair_venv_via_import_probes(["echo"], env=None)
    _install_python_dependencies_with_optional_fallback(["echo"], env=None, group="all")
    _verify_console_scripts_installed(["echo"], env=None)
    _verify_core_dependencies_installed(["echo"], env=None, group="all")
    _resolve_install_target_python(["echo"], None)
    _run_install_with_heartbeat(["echo"], env=None, cwd=None, timeout=None)
