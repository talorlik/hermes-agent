"""Bootstrap argv scanning must survive a parser rewrite.

``hermes_bootstrap`` imports ``command_argv`` before dependency activation so
``hermes pm repair`` can run on a broken tree. The 2026-09-26 oneshot isolation
rewrite of ``_parser.py`` dropped the function while the import stayed.
"""

from hermes_cli._parser import command_argv


def test_command_argv_is_importable_for_bootstrap() -> None:
    assert command_argv(["pm", "repair"])[:2] == ["pm", "repair"]


def test_command_argv_skips_top_level_flags_and_their_values() -> None:
    assert command_argv(["--profile", "desk", "pm", "repair"])[:2] == ["pm", "repair"]
    assert command_argv(["-p", "desk", "-m", "gpt", "kanban", "list"]) == ["kanban", "list"]
    assert command_argv(["--model=gpt", "status"]) == ["status"]


def test_command_argv_stops_at_double_dash_and_empty() -> None:
    assert command_argv([]) == []
    assert command_argv(["--"]) == []
    assert command_argv(["--", "pm", "repair"]) == ["pm", "repair"]
