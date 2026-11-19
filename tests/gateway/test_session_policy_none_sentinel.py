"""G-ONESHOT-ISOLATION: exact ``toolsets=['none']`` is the reserved empty tool set at the policy.

Fork-only. The mapping is a four-line hunk in ``gateway/session_policy.py``; this file holds its
proof so the upstream test module next to it stays byte-identical to upstream.
"""
from __future__ import annotations

import pytest

from gateway.session_policy import build_policy
from persistence.core.errors import AuthorityRefusal

_CFG = {'platform_toolsets': {'cli': ['terminal']}}


def _refusal(params):
    """The ``AuthorityRefusal`` a real ``build_policy`` raises for *params*."""
    with pytest.raises(AuthorityRefusal) as caught:
        build_policy(params, dict(_CFG))
    return caught.value


def test_oneshot_none_sentinel_is_an_explicit_empty_toolset(tmp_path):
    """`hermes -z -t none` is the reserved empty toolset, including on bypass launches."""
    params = {
        'source': 'cli',
        'cwd': str(tmp_path),
        'model': 'fixture',
        'toolsets': ['none'],
        'safe_mode': True,
        'ignore_user_config': True,
    }
    policy = build_policy(params, dict(_CFG), profile_terminal=False)
    assert tuple(policy.toolsets) == ()
    mixed = _refusal({'cwd': str(tmp_path), 'source': 'cli', 'toolsets': ['none', 'terminal']})
    assert mixed.field == 'toolsets' and 'none' in mixed.detail
    for bad in (['None'], ['none', 'none']):
        refusal = _refusal({'cwd': str(tmp_path), 'source': 'cli', 'toolsets': bad})
        assert refusal.field == 'toolsets'
