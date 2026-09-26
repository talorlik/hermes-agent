"""Tests for the cronjob tool schema shape.

Guards the description text that flags ``schedule`` (and ``prompt``) as
REQUIRED for ``action=create`` — the load-bearing fix for description-driven
models (e.g. Grok) that omit schedule when the schema only lists ``action``
in ``required[]``. See issue #32427 / PR #32448.
"""

from __future__ import annotations

from unittest.mock import patch


def test_cronjob_schema_action_description_flags_create_requirements():
    """`action` description must state schedule + prompt are required for create."""
    from tools.cronjob_tools import CRONJOB_SCHEMA

    action_desc = CRONJOB_SCHEMA["parameters"]["properties"]["action"]["description"]
    assert "action=create" in action_desc
    assert "schedule" in action_desc
    assert "REQUIRED" in action_desc


def test_cronjob_schema_exposes_script_failure_policy_enum():
    from tools.cronjob_tools import CRONJOB_SCHEMA

    policy = CRONJOB_SCHEMA["parameters"]["properties"]["script_failure_policy"]

    assert policy["type"] == "string"
    assert policy["enum"] == ["continue", "fail_closed"]
    assert policy["default"] == "continue"
    assert "requires" in policy["description"].lower()
    assert "script" in policy["description"].lower()


def test_cronjob_handler_forwards_script_failure_policy():
    from tools.cronjob_tools import _cronjob_handler

    with patch("tools.cronjob_tools.cronjob", return_value="{}") as cronjob:
        _cronjob_handler(
            {
                "action": "create",
                "script": "gate.py",
                "script_failure_policy": "fail_closed",
            }
        )

    assert cronjob.call_args.kwargs["script_failure_policy"] == "fail_closed"


