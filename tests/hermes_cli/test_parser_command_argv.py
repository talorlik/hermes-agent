"""Tests for command_argv and related parser utilities.

Regression test for missing command_argv import that broke CLI startup.
The function was removed during refactoring but is required by hermes_bootstrap,
_early_recovery, and venv_sync to detect pm commands during early init.
"""

import pytest


def test_command_argv_extracts_subcommand_and_args():
    """command_argv returns subcommand and its args, stripping top-level flags."""
    from hermes_cli._parser import command_argv

    # Simple subcommand
    assert command_argv(["chat"]) == ["chat"]
    assert command_argv(["chat", "-q", "hello"]) == ["chat", "-q", "hello"]

    # Top-level flags before subcommand
    assert command_argv(["--profile", "test", "chat"]) == ["chat"]
    assert command_argv(["-p", "test", "chat", "-q", "hello"]) == ["chat", "-q", "hello"]

    # Multiple top-level flags
    assert command_argv(["--profile", "test", "--verbose", "chat"]) == ["chat"]

    # No subcommand (help/version)
    assert command_argv([]) == []
    assert command_argv(["--help"]) == []
    assert command_argv(["--version"]) == []

    # Double dash separator
    assert command_argv(["--", "chat"]) == ["chat"]
    assert command_argv(["--profile", "test", "--", "chat"]) == ["chat"]


def test_command_argv_recognizes_pm_repair():
    """Regression: hermes_bootstrap needs command_argv to detect pm repair."""
    from hermes_cli._parser import command_argv

    # Direct pm repair
    assert command_argv(["pm", "repair"])[:2] == ["pm", "repair"]

    # With profile flag
    assert command_argv(["--profile", "test", "pm", "repair"])[:2] == ["pm", "repair"]

    # Other pm commands
    assert command_argv(["pm", "install"])[:2] == ["pm", "install"]
    assert command_argv(["-p", "dev", "pm", "lock"])[:2] == ["pm", "lock"]


def test_command_argv_handles_value_flags_correctly():
    """command_argv must skip both flag and value for top-level value flags."""
    from hermes_cli._parser import command_argv

    # Short value flags
    assert command_argv(["-p", "profile-name", "chat"]) == ["chat"]
    assert command_argv(["-c", "session-name", "chat"]) == ["chat"]

    # Long value flags
    assert command_argv(["--profile", "name", "chat"]) == ["chat"]
    assert command_argv(["--continue", "session", "chat"]) == ["chat"]

    # Inline value (--flag=value)
    assert command_argv(["--profile=test", "chat"]) == ["chat"]


def test_command_argv_with_edge_cases():
    """command_argv handles edge cases in argv parsing."""
    from hermes_cli._parser import command_argv

    # Single dash (stdin)
    assert command_argv(["-"]) == ["-"]

    # Flag-like argument after subcommand
    assert command_argv(["chat", "--help"]) == ["chat", "--help"]

    # Mixed short flags
    assert command_argv(["-vp", "test", "chat"]) == ["chat"]


def test_command_argv_import_availability():
    """Regression: command_argv must be importable from hermes_cli._parser.

    This function is imported during bootstrap before most dependencies are available,
    so it must be present and work with stdlib only.
    """
    from hermes_cli._parser import command_argv

    # Verify it's callable
    assert callable(command_argv)

    # Verify it works with minimal input
    result = command_argv(["pm", "repair"])
    assert isinstance(result, list)
    assert result == ["pm", "repair"]


def test_command_argv_used_by_bootstrap():
    """Verify command_argv is actually used by hermes_bootstrap as expected."""
    from hermes_cli._parser import command_argv

    # Simulate the exact check done in hermes_bootstrap.py
    # _pm_repair = command_argv(sys.argv[1:])[:2] == ["pm", "repair"]

    test_cases = [
        (["pm", "repair"], True),
        (["--profile", "test", "pm", "repair"], True),
        (["pm", "install"], False),
        (["chat"], False),
        ([], False),
    ]

    for argv, is_pm_repair in test_cases:
        result = command_argv(argv)[:2] == ["pm", "repair"]
        assert result == is_pm_repair, f"Failed for argv={argv}"


def test_command_argv_used_by_early_recovery():
    """Verify command_argv is used by _early_recovery.py as expected."""
    from hermes_cli._parser import command_argv

    # Simulate the check: if not explicit and args[:1] == ["pm"]:
    test_cases = [
        (["pm", "repair"], True),
        (["pm", "install"], True),
        (["--profile", "test", "pm", "lock"], True),
        (["chat"], False),
        (["gateway"], False),
        ([], False),
    ]

    for argv, is_pm_command in test_cases:
        result = command_argv(argv)[:1] == ["pm"]
        assert result == is_pm_command, f"Failed for argv={argv}"


def test_command_argv_used_by_venv_sync():
    """Verify command_argv is used by venv_sync.py as expected."""
    from hermes_cli._parser import command_argv

    # Simulate the check: if (command_argv(argv)[:1] == ["pm"]
    test_cases = [
        (["pm", "install"], True),
        (["--profile", "dev", "pm", "repair"], True),
        (["chat", "-q", "hello"], False),
        (["gateway", "serve"], False),
    ]

    for argv, is_pm_command in test_cases:
        result = command_argv(argv)[:1] == ["pm"]
        assert result == is_pm_command, f"Failed for argv={argv}"
