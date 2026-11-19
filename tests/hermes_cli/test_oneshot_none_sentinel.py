"""G-ONESHOT-ISOLATION: exact ``none`` is an empty tool set, before discovery."""

from __future__ import annotations

import argparse

import pytest

from hermes_cli.gateway_chat import _requested_policy
from hermes_cli.oneshot_toolset_sentinel import _precheck_explicit_toolsets


def test_exact_none_is_an_explicit_empty_toolset() -> None:
    assert _precheck_explicit_toolsets("none") == ([], None)
    assert _precheck_explicit_toolsets(["none"]) == ([], None)


def test_mixed_or_misspelled_none_refuses_before_discovery() -> None:
    mixed = _precheck_explicit_toolsets("none,web")
    assert mixed is not None and mixed[1] is not None
    assert mixed[0] is None
    assert "only toolset entry" in mixed[1]
    spelled = _precheck_explicit_toolsets("None")
    assert spelled is not None
    assert spelled[0] is None


def test_a_normal_toolset_is_not_the_sentinel() -> None:
    assert _precheck_explicit_toolsets("web") is None
    assert _precheck_explicit_toolsets(None) is None


def test_oneshot_policy_sends_the_none_sentinel_as_written() -> None:
    # The frozen launch request compares literally on resume, so `-z -t none`
    # must send what `chat -t none` sends. The gateway maps it to no tools.
    args = argparse.Namespace(oneshot=True, toolsets="none")
    assert _requested_policy(args)["toolsets"] == ["none"]


def test_oneshot_policy_refuses_a_mixed_none_list() -> None:
    args = argparse.Namespace(oneshot=True, toolsets="none,web")
    with pytest.raises(SystemExit) as caught:
        _requested_policy(args)
    assert caught.value.code == 2
