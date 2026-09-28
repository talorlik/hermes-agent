"""The CLI entrypoint must import after install-repair names moved."""

from __future__ import annotations


def test_cli_main_imports_and_binds_moved_updater_surface() -> None:
    """Importing hermes_cli.main must not require names the repair module dropped.

    Historical updater code still resolves the moved names on hermes_cli.main
    via _m(). Those names now live on old_updater_main. The three names that
    no longer exist anywhere must not be part of the import.
    """
    from hermes_cli import main
    from hermes_cli import main_install_repair
    from hermes_cli import old_updater_main

    moved = (
        "ShimQuarantineError",
        "_BYTECODE_FINGERPRINT_FILE",
        "_resolve_install_target_python",
        "_run_install_with_heartbeat",
        "_run_package_only_install",
        "_verify_console_scripts_installed",
        "_verify_core_dependencies_installed",
    )
    for name in moved:
        assert getattr(main, name) is getattr(old_updater_main, name), name

    dropped = (
        "_recover_from_interrupted_install",
        "_install_python_dependencies_with_optional_fallback",
        "_repair_venv_via_import_probes",
    )
    for name in dropped:
        assert not hasattr(main_install_repair, name), name
        assert not hasattr(main, name), name
