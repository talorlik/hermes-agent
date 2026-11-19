"""Unreachable-model retry ladder: the parked instant, and yielding to a nearer natural run.

The two tests the fork had added inside ``tests/cron/test_unreachable_retry.py``; that file is
upstream's again, so they live here.
"""

from datetime import UTC, datetime, timedelta

import pytest

from cron import unreachable_retry as ur


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def test_is_retry_fire_matches_only_the_parked_instant():
    """A ladder re-run matches only the parked instant and schedule expression."""
    job = {
        "schedule": {"kind": "cron", "expr": "0 * * * *"},
        ur.STATE_KEY: {"attempt": 1, "at": "2026-10-05T00:00:00+00:00", "expr": "0 * * * *"},
    }
    assert ur.is_retry_fire(job, "2026-10-05T00:00:00+00:00") is True
    assert ur.is_retry_fire(job, "2026-10-05T01:00:00+00:00") is False
    drifted = {
        "schedule": {"kind": "cron", "expr": "15 * * * *"},
        ur.STATE_KEY: {"attempt": 1, "at": "2026-10-05T00:00:00+00:00", "expr": "0 * * * *"},
    }
    assert ur.is_retry_fire(drifted, "2026-10-05T00:00:00+00:00") is False


def test_retry_yields_to_nearer_natural_run(tmp_cron_home, monkeypatch):
    """The durable store mutation leaves a nearer natural occurrence intact."""
    now = datetime.now(UTC)
    monkeypatch.setattr(ur, "_hermes_now", lambda: now)
    natural_next = (now + timedelta(minutes=1)).isoformat()
    job = {
        "id": "near-natural",
        "name": "near natural",
        "schedule": {"kind": "interval", "minutes": 1},
        "state": "scheduled",
        "next_run_at": natural_next,
    }

    assert ur.plan_retry(job) is False
    assert job.get(ur.STATE_KEY) is None
    assert job["next_run_at"] == natural_next
