"""Standalone delivery must never leak an unawaited send coroutine.

The fallback lane builds a ``_send_to_platform`` coroutine and hands it to
a fresh thread pool. If ``submit`` itself raises (the interpreter-shutdown
race this lane exists for), the coroutine must be closed — a leaked
coroutine surfaces as a ``RuntimeWarning: coroutine ... was never awaited``
somewhere later in the process, polluting unrelated tests and logs.

Warning-strict by construction: the test records ALL warnings and asserts
none mention an unawaited coroutine.
"""

from __future__ import annotations

import gc
import warnings
from unittest.mock import AsyncMock, patch

import pytest


class _ExplodingPool:
    """ThreadPoolExecutor whose submit dies like interpreter shutdown."""

    def __init__(self, *args, **kwargs):
        pass

    def submit(self, *args, **kwargs):
        raise RuntimeError(
            "cannot schedule new futures after interpreter shutdown"
        )

    def shutdown(self, *args, **kwargs):
        pass


@pytest.fixture
def slack_home_config(monkeypatch):
    from gateway.config import (
        GatewayConfig,
        HomeChannel,
        Platform,
        PlatformConfig,
    )

    monkeypatch.setenv("SLACK_HOME_CHANNEL", "D123")
    return GatewayConfig(
        platforms={
            Platform.SLACK: PlatformConfig(
                enabled=True,
                home_channel=HomeChannel(
                    platform=Platform.SLACK,
                    chat_id="D123",
                    name="Owner DM",
                    user_id="U123",
                ),
            ),
        },
    )


def test_pool_submit_failure_does_not_leak_send_coroutine(slack_home_config):
    from cron.scheduler import _deliver_result

    job = {"id": "leak-probe", "deliver": "slack"}

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with (
            patch(
                "gateway.config.load_gateway_config",
                return_value=slack_home_config,
            ),
            patch(
                "cron.scheduler.load_config",
                return_value={"cron": {"wrap_response": False}},
            ),
            patch(
                "tools.send_message_tool._send_to_platform",
                new=AsyncMock(return_value={"success": True}),
            ),
            # First lane: asyncio.run refuses (as with a running loop) —
            # production closes that coroutine itself.
            patch("asyncio.run", side_effect=RuntimeError("no loop")),
            # Fallback lane: the pool dies at submit, exactly like the
            # interpreter-shutdown race.
            patch(
                "concurrent.futures.ThreadPoolExecutor", _ExplodingPool
            ),
        ):
            _deliver_result(job, "scheduled result", adapters=None, loop=None)
        gc.collect()

    leaked = [
        w for w in caught if "was never awaited" in str(w.message)
    ]
    assert leaked == [], (
        "standalone delivery leaked unawaited coroutine(s): "
        + "; ".join(str(w.message) for w in leaked)
    )
