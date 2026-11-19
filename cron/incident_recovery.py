"""Resolving an acknowledged cron incident after a verified recovery (fork-only module).

Upstream's ``cron.incidents.close_incidents_for_recovered_job`` marks a job's open
(``detected`` / ``alerted``) incidents ``resolved`` after a successful run and leaves an
operator ack (``closed``) silent for good. That is right for the scheduler: its own success is
not evidence the operator's concern is gone.

Central Command's incident healer (``hermes-custom-setup``,
``central-command/ORCHESTRATION/scripts/incident_success_heal.py``) holds stronger evidence, a
later live workflow completion it verified itself, and calls
``close_incidents_for_recovered_job(job_id, include_acked=True)``. An ack is not a heal: once the
job is proven healthy the ack becomes ``resolved``, so a repeat of the same error re-opens the
incident and alerts again instead of staying silent.

``cron/incidents.py`` rebinds its ``close_incidents_for_recovered_job`` through
:func:`bind_acked_resolution` at the end of the file. That rebinding is the only fork edit in the
upstream file. Without ``include_acked`` the call is upstream's function, unchanged.
"""

from __future__ import annotations

from typing import Any, Callable


def resolve_acked_incidents(job_id: str) -> int:
    """Mark the job's acknowledged (``closed``) incidents ``resolved``; returns how many.

    Delivery incidents are left alone: the caller's proof is a completed run, which says
    nothing about a channel the operator silenced.
    """
    # Late import: ``cron.incidents`` imports this module while it is still loading.
    from cron import incidents

    now = incidents._hermes_now().isoformat()
    with incidents._transaction() as conn:
        cursor = conn.execute(
            """UPDATE cron_incidents SET state='resolved', closed_at=?
               WHERE job_id=? AND state='closed'
                 AND (failure_type IS NULL OR failure_type != 'delivery')""",
            (now, str(job_id or "")),
        )
        return int(cursor.rowcount or 0)


def bind_acked_resolution(close_open: Callable[..., int]) -> Callable[..., int]:
    """Add the keyword-only ``include_acked`` switch to upstream's open-incident resolver.

    Every other argument goes to upstream's function untouched, so a parameter upstream adds
    later keeps working. A narrower signature would raise ``TypeError`` on it, which the
    scheduler logs at DEBUG and moves on: incident resolution would stop without a visible error.
    """

    def close_incidents_for_recovered_job(
        *args: Any, include_acked: bool = False, **kwargs: Any
    ) -> int:
        resolved = close_open(*args, **kwargs)
        if include_acked:
            # ``job_id`` is upstream's first parameter, positional or by name.
            resolved += resolve_acked_incidents(args[0] if args else kwargs["job_id"])
        return resolved

    close_incidents_for_recovered_job.__doc__ = close_open.__doc__
    return close_incidents_for_recovered_job
