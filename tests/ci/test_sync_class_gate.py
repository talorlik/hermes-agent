from __future__ import annotations

import sys
from pathlib import Path
from typing import TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))

from sync_class_gate import delta_allowed


class _Flags(TypedDict):
    equals_upstream: bool
    equals_fork: bool
    pin_keys_only: bool
    contract_ok: bool
    executable_drop: bool


def _gate(
    kind: str,
    *,
    path: str = "plugins/example.py",
    equals_upstream: bool = False,
    equals_fork: bool = False,
    pin_keys_only: bool = False,
    contract_ok: bool = False,
    executable_drop: bool = False,
) -> bool:
    return delta_allowed(
        path,
        kind,
        equals_upstream=equals_upstream,
        equals_fork=equals_fork,
        pin_keys_only=pin_keys_only,
        contract_ok=contract_ok,
        executable_drop=executable_drop,
    )


def test_generated_unspecified_allows_only_an_upstream_match() -> None:
    assert _gate("generated-unspecified", equals_upstream=True) is True
    assert _gate(
        "generated-unspecified",
        equals_upstream=False,
        equals_fork=True,
        pin_keys_only=True,
        contract_ok=True,
        executable_drop=False,
    ) is False


def test_generated_specified_allows_only_pin_keys() -> None:
    assert _gate("generated-specified", pin_keys_only=True) is True
    assert _gate(
        "generated-specified",
        equals_upstream=True,
        equals_fork=True,
        pin_keys_only=False,
        contract_ok=True,
        executable_drop=False,
    ) is False


def test_structured_pin_allows_only_pin_keys_and_contract() -> None:
    assert _gate("structured-pin", pin_keys_only=True, contract_ok=True) is True
    assert _gate(
        "structured-pin",
        equals_upstream=True,
        equals_fork=True,
        pin_keys_only=True,
        contract_ok=False,
        executable_drop=False,
    ) is False
    assert _gate(
        "structured-pin",
        equals_upstream=True,
        equals_fork=True,
        pin_keys_only=False,
        contract_ok=True,
        executable_drop=False,
    ) is False


def test_auth_hold_allows_only_a_fork_match() -> None:
    assert _gate("auth-hold", equals_fork=True) is True
    assert _gate(
        "auth-hold",
        equals_upstream=True,
        equals_fork=False,
        pin_keys_only=True,
        contract_ok=True,
        executable_drop=False,
    ) is False


def test_build_output_is_never_allowed() -> None:
    assert _gate(
        "build-output",
        equals_upstream=True,
        equals_fork=True,
        pin_keys_only=True,
        contract_ok=True,
        executable_drop=False,
    ) is False


def test_owned_source_stops_on_executable_drop() -> None:
    assert _gate(
        "owned-source",
        equals_upstream=True,
        equals_fork=True,
        pin_keys_only=True,
        contract_ok=True,
        executable_drop=True,
    ) is False
    assert _gate("owned-source", executable_drop=False) is True


def test_mystery_unowned_and_other_kinds_are_refused() -> None:
    permissive: _Flags = {
        "equals_upstream": True,
        "equals_fork": True,
        "pin_keys_only": True,
        "contract_ok": True,
        "executable_drop": False,
    }
    for kind in ("mystery", "unowned", "generated_unspecified", ""):
        assert _gate(kind, **permissive) is False


def test_path_does_not_change_the_kind_decision() -> None:
    allowed: _Flags = {
        "equals_upstream": True,
        "equals_fork": False,
        "pin_keys_only": False,
        "contract_ok": False,
        "executable_drop": False,
    }
    assert delta_allowed("a.py", "generated-unspecified", **allowed) is True
    assert delta_allowed("other/lock.json", "generated-unspecified", **allowed) is True
    assert delta_allowed("a.py", "unowned", **allowed) is False
    assert delta_allowed("other/lock.json", "unowned", **allowed) is False
