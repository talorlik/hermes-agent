"""Behaviour contracts for ``cron.jobs`` helpers: pin updates, instant arithmetic, one-shot grace,
claim liveness, interval anchoring, duration parsing and skill-list normalisation.

These are the tests of ``tests/cron/test_gateway_startup_import_closure.py`` that pass against the
current ``cron/jobs.py`` and have no upstream counterpart. Instants are rendered in non-UTC offsets
so a wall-clock comparison cannot pass by accident.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest


@pytest.mark.parametrize(
    ("job", "updates", "expected"),
    [
        pytest.param(
            {"model": "m", "provider": "p"},
            {"pinned": False},
            {"provider": None, "model": None},
            id="release-follows-main-model",
        ),
        pytest.param(
            {"model": None},
            {"pinned": True, "model": "explicit/model"},
            {"model": "explicit/model"},
            id="explicit-model-wins-over-pin",
        ),
        pytest.param(
            {"model": "already/pinned"},
            {"pinned": True},
            {},
            id="already-pinned-untouched",
        ),
        pytest.param(
            {"model": "m"},
            {"schedule": "every 1h"},
            {"schedule": "every 1h"},
            id="no-pinned-key-no-op",
        ),
    ],
)
def test_apply_pin_update_rewrites_pin_without_storing_the_flag(job, updates, expected):
    from cron.jobs import _apply_pin_update

    _apply_pin_update(job, updates)

    assert "pinned" not in updates
    assert updates == expected


_A = datetime(2026, 10, 3, 12, 0, tzinfo=timezone(timedelta(hours=2)))  # 10:00Z


_B = datetime(2026, 10, 3, 11, 0, tzinfo=UTC)  # 11:00Z


def test_instant_helpers_compare_absolute_instants_across_offsets():
    from cron.jobs import (
        _elapsed_seconds,
        _instant_after,
        _instant_at_or_before,
        _instant_before,
        _seconds_after,
    )

    assert _elapsed_seconds(_B, _A) == 3600.0
    assert _elapsed_seconds(_A, _B) == -3600.0
    assert _instant_after(_B, _A) is True and _instant_after(_A, _B) is False
    assert _instant_before(_A, _B) is True and _instant_before(_B, _A) is False
    assert (
        _instant_at_or_before(_A, _A) is True and _instant_at_or_before(_B, _A) is False
    )

    later = _seconds_after(_A, 90)
    assert isinstance(later, datetime)
    assert later == _A + timedelta(seconds=90)
    assert later.utcoffset() == _A.utcoffset()


@pytest.mark.parametrize(
    ("offset_seconds", "eligible"),
    [
        pytest.param(-60, True, id="one-minute-late-within-grace"),
        pytest.param(-3600, False, id="one-hour-late-beyond-grace"),
        pytest.param(3600, True, id="future-run"),
    ],
)
def test_one_shot_grace_is_measured_in_elapsed_seconds_not_wall_clock(
    offset_seconds, eligible
):
    from cron.jobs import ONESHOT_GRACE_SECONDS, compute_next_run

    assert ONESHOT_GRACE_SECONDS < 3600
    # Rendered in a non-UTC offset so a wall-clock comparison cannot pass by accident.
    run_at = (
        (datetime.now(UTC) + timedelta(seconds=offset_seconds))
        .astimezone(timezone(timedelta(hours=2)))
        .isoformat()
    )

    result = compute_next_run({"kind": "once", "run_at": run_at})

    assert result == (run_at if eligible else None)
    assert (
        compute_next_run({"kind": "once", "run_at": run_at}, last_run_at=run_at) is None
    )


@pytest.mark.parametrize(
    ("age_seconds", "live"),
    [
        pytest.param(10, True, id="fresh-claim"),
        pytest.param(400, False, id="expired-claim"),
        pytest.param(-60, False, id="future-dated-claim-is-stale"),
    ],
)
def test_claim_liveness_window_counts_elapsed_seconds_from_now(age_seconds, live):
    from cron.jobs import _claim_is_live

    now = datetime.now(UTC)
    claimed_at = (now - timedelta(seconds=age_seconds)).astimezone(
        timezone(timedelta(hours=-5))
    )
    # A foreign host in ``by`` keeps the owner-liveness probe out of the picture.
    claim = {"at": claimed_at.isoformat(), "by": "elsewhere.invalid:1"}

    assert _claim_is_live(claim, now, 300) is live
    assert _claim_is_live(None, now, 300) is False


def test_interval_next_run_anchors_on_the_last_run_instant():
    from cron.jobs import compute_next_run

    last_run = datetime(
        2026, 10, 3, 9, 15, tzinfo=timezone(timedelta(hours=5, minutes=30))
    )
    result = compute_next_run({"kind": "interval", "minutes": 30}, last_run.isoformat())

    assert datetime.fromisoformat(result) == last_run + timedelta(minutes=30)


@pytest.mark.parametrize(
    ("text", "minutes"),
    [("30m", 30), ("2h", 120), ("1d", 1440), ("hour", 60), (" 45 min ", 45)],
)
def test_parse_duration_returns_minutes(text, minutes):
    from cron.jobs import parse_duration

    assert parse_duration(text) == minutes


@pytest.mark.parametrize(
    ("skill", "skills", "expected"),
    [
        ("a", None, ["a"]),
        (None, "solo", ["solo"]),
        ("legacy", ["x", " y ", "x", ""], ["x", "y"]),
        (None, None, []),
    ],
)
def test_normalize_skill_list_merges_legacy_and_multi_skill_inputs(
    skill, skills, expected
):
    from cron.jobs import _normalize_skill_list

    assert _normalize_skill_list(skill, skills) == expected
