"""Automatic bounded re-runs for scheduled fires that never reached the model.

Inspired by Claude Cowork (desktop changelog v1.46388.1, 2026-09-04): "automatic re-runs
(after 5, 15, and 30 minutes) for a scheduled task that could not reach the model at all,
for example right after the computer wakes behind a VPN."

The class is deliberately narrow: the run must have FAILED with a transient network /
DNS error (``cron.scheduler_preflight._is_transient_provider_resolve_error``) AND the
agent must have completed zero API calls. Nothing was executed and nothing was spent, so
re-running cannot double a side effect — unlike a generic failure retry (see PR #16512),
which has to answer for one-shot dispatch accounting and mid-run side effects. Recurring
jobs only: finite one-shots are pre-claimed by ``claim_dispatch`` (at-most-times, #38758)
and must not regain a consumed dispatch here.

While a retry is pending the failure notice is suppressed (Cowork re-runs silently); a
run that reaches the model — success or not — resets the ladder. Disable with
``cron.retry_unreachable: false`` in config.yaml.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Dict, Optional

from hermes_time import now as _hermes_now

logger = logging.getLogger("cron.scheduler")

# Cowork's ladder: re-run after 5, 15, then 30 minutes; then give up until the
# schedule's own next occurrence.
RETRY_DELAYS_SECONDS: tuple[int, ...] = (300, 900, 1800)

# Persisted on the job while a retry cycle is active: {"attempt": <1-based count of
# retries already scheduled>}. Cleared by any run that reached the model.
STATE_KEY = "unreachable_retry"


@dataclass(frozen=True)
class RetryDecision:
    """One pre-delivery retry decision, reused by the later store write.

    The failure notice may be suppressed only when this exact decision will be
    applied; recomputing after delivery can otherwise yield to a nearer natural
    occurrence and leave the user with neither a notice nor an automatic retry.
    """

    should_retry: bool
    attempt: int = 0
    retry_at: Optional[str] = None


def retry_enabled(cfg: Optional[dict] = None) -> bool:
    """``cron.retry_unreachable`` — default ON (spend-neutral: only fires when zero
    model calls were made)."""
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:  # config unreadable — keep the reliability default
            return True
    cron_cfg = (cfg or {}).get("cron")
    if not isinstance(cron_cfg, dict):
        return True
    return cron_cfg.get("retry_unreachable") is not False


def is_model_unreachable_failure(exc: BaseException, agent: Any = None) -> bool:
    """True when *exc* is a transient network/DNS failure and *agent* (may be ``None``)
    never completed a model call — the run consumed nothing and executed nothing."""
    if int(getattr(agent, "session_api_calls", 0) or 0) > 0:
        return False
    from cron.scheduler_preflight import _is_transient_provider_resolve_error

    return _is_transient_provider_resolve_error(exc)


def _is_recurring(job: Dict[str, Any]) -> bool:
    return job.get("schedule", {}).get("kind") in {"cron", "interval"}


def prepare_retry(job: Dict[str, Any]) -> RetryDecision:
    """Decide whether a retry is schedulable before failure delivery.

    Includes the natural-next-run comparison used by :func:`plan_retry`, so a
    notice is never suppressed for a retry that the store write will decline.
    """
    if not _is_recurring(job) or job.get("state") == "paused":
        return RetryDecision(False)
    state = job.get(STATE_KEY) or {}
    attempt = int(state.get("attempt") or 0)
    if attempt >= len(RETRY_DELAYS_SECONDS) or not retry_enabled():
        return RetryDecision(False)
    retry_dt = _hermes_now() + timedelta(seconds=RETRY_DELAYS_SECONDS[attempt])
    from cron.jobs import _parse_aware  # late: jobs imports this module's helpers

    natural_next = _parse_aware(job.get("next_run_at"))
    if natural_next is not None and natural_next <= retry_dt:
        return RetryDecision(False)
    return RetryDecision(True, attempt=attempt, retry_at=retry_dt.isoformat())


def will_retry(job: Dict[str, Any]) -> bool:
    """Compatibility predicate for callers that need only the decision bit."""
    return prepare_retry(job).should_retry


def clear_state(job: Dict[str, Any]) -> None:
    """A run reached the model (any outcome): the ladder resets."""
    job.pop(STATE_KEY, None)


def plan_retry(job: Dict[str, Any], decision: Optional[RetryDecision] = None) -> bool:
    """Called under the jobs lock AFTER ``_advance_after_run`` computed the schedule's
    natural ``next_run_at`` for a failed, flagged run. Pulls ``next_run_at`` earlier to
    the ladder instant when that is sooner than the natural occurrence; exhausted or
    inapplicable cycles clear state and leave the schedule untouched. Returns True when
    a retry was scheduled."""
    if decision is None:
        decision = prepare_retry(job)
    if not _is_recurring(job) or job.get("state") == "paused" or not retry_enabled():
        clear_state(job)
        return False
    state = job.get(STATE_KEY) or {}
    attempt = int(state.get("attempt") or 0)
    if attempt >= len(RETRY_DELAYS_SECONDS) or not decision.should_retry:
        # Ladder exhausted: fall back to the natural schedule and reset so the NEXT
        # occurrence gets a fresh ladder if the network is still down.
        clear_state(job)
        logger.warning(
            "Job '%s': model unreachable after %d automatic re-runs — waiting for the "
            "scheduled occurrence at %s",
            job.get("name", job.get("id", "?")), attempt, job.get("next_run_at"))
        return False
    if decision.attempt != attempt or not decision.retry_at:
        clear_state(job)
        return False
    delay = RETRY_DELAYS_SECONDS[attempt]
    retry_at = decision.retry_at
    job[STATE_KEY] = {"attempt": attempt + 1}
    job["next_run_at"] = retry_at
    if job.get("state") != "paused":
        job["state"] = "scheduled"
    logger.info(
        "Job '%s': model unreachable with zero API calls — automatic re-run %d/%d in %ds "
        "(at %s)",
        job.get("name", job.get("id", "?")), attempt + 1, len(RETRY_DELAYS_SECONDS),
        delay, retry_at)
    return True
