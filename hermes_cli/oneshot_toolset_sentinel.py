"""Oneshot toolset sentinel: exact ``none`` is an explicit empty tool set.

Fork-owned (G-ONESHOT-ISOLATION). The checks live here and not in
``hermes_cli/oneshot.py`` so the fork carries no edit on that upstream file
and an upstream rewrite of it merges without a conflict.
"""

from __future__ import annotations

import sys


# Reserved oneshot-only sentinel. Exact lowercase "none" is an explicit empty
# tool set. It is not a toolset name. The TUI also calls _normalize_toolsets,
# so this check stays out of that helper.
_TOOLSETS_NONE_SENTINEL = "none"
_ALL_TOOLSETS = {"all", "*"}


def _raw_toolset_tokens(toolsets: object = None) -> list[str]:
    """Split explicit toolsets while preserving blank comma segments."""
    if not toolsets:
        return []
    raw_items = [toolsets] if isinstance(toolsets, str) else toolsets
    if not isinstance(raw_items, (list, tuple)):
        raw_items = [raw_items]
    tokens: list[str] = []
    for item in raw_items:
        if isinstance(item, str):
            tokens.extend(part.strip() for part in item.split(","))
        else:
            tokens.append(str(item).strip())
    return tokens


def _precheck_explicit_toolsets(
    toolsets: object = None,
) -> tuple[list[str] | None, str | None] | None:
    """Resolve the sentinel before discovery.

    None means this is not a sentinel decision. ``([], None)`` is an explicit
    empty set. ``(None, message)`` is a refusal the caller exits 2 on.
    """
    raw = _raw_toolset_tokens(toolsets)
    # The same list upstream's oneshot normalizer yields: blank segments dropped.
    # Computed here so this module depends on no private upstream helper.
    normalized = [token for token in raw if token]
    if not normalized:
        return None
    if any(token.lower() == _TOOLSETS_NONE_SENTINEL for token in raw):
        if raw != [_TOOLSETS_NONE_SENTINEL]:
            return None, (
                "hermes -z: --toolsets 'none' selects an explicit empty tool "
                "set and must be the only toolset entry, spelled exactly "
                f"lowercase; got: {', '.join(raw)}. Pass 'none' alone, or "
                "remove it.\n"
            )
        return [], None
    if any(token in _ALL_TOOLSETS for token in normalized) and len(normalized) > 1:
        return None, (
            "hermes -z: --toolsets 'all' enables every toolset and must be "
            f"the only toolset entry; got: {', '.join(normalized)}. Pass "
            "'all' alone, or list specific toolsets.\n"
        )
    return None


def refuse_malformed_oneshot_toolsets(toolsets: object = None) -> None:
    """Exit 2 with the refusal text when a oneshot toolset list misuses a reserved name.

    The single call site is ``hermes_cli.gateway_chat._requested_policy``. An
    accepted ``none`` returns silently and is sent as written: the gateway maps
    exact ``["none"]`` to the empty tool set, and the frozen launch request must
    stay equal to what ``hermes chat -t none`` and earlier sessions sent, or
    resume refuses.
    """
    decision = _precheck_explicit_toolsets(toolsets)
    if decision is not None and decision[1]:
        sys.stderr.write(decision[1])
        raise SystemExit(2)
