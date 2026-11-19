"""A verified recovery resolves an acknowledged incident (``cron.incident_recovery``, fork-only).

The scheduler's own success leaves an operator ack silent. Central Command's healer passes
``include_acked=True`` once it has verified a later live success: the ack becomes ``resolved``
and a repeat of the same error re-opens the incident and alerts again. Delivery incidents stay
as the operator left them.
"""

from cron import incidents


def _point_db(monkeypatch, tmp_path):
    monkeypatch.setattr(incidents, "EXECUTIONS_FILE", tmp_path / "cron" / "executions.db")
    return incidents


def test_verified_recovery_resolves_an_ack_and_a_repeat_reopens_it(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    inc_id, _ = inc.upsert_incident("job-1", "known flaky")
    inc.ack_incident(inc_id)
    delivery_id, _ = inc.upsert_incident("job-1", "send failed", failure_type="delivery")
    inc.ack_incident(delivery_id)

    assert inc.close_incidents_for_recovered_job("job-1", include_acked=True) == 1
    assert inc.get_incident(inc_id)["state"] == "resolved"
    assert inc.get_incident(delivery_id)["state"] == "closed"
    same_id, reopened = inc.upsert_incident("job-1", "known flaky")
    assert same_id == inc_id and reopened is True
    assert inc.get_incident(inc_id)["state"] == "detected"


def test_without_the_switch_an_ack_survives_and_open_incidents_resolve(monkeypatch, tmp_path):
    inc = _point_db(monkeypatch, tmp_path)
    acked_id, _ = inc.upsert_incident("job-1", "known flaky")
    inc.ack_incident(acked_id)
    open_id, _ = inc.upsert_incident("job-1", "provider 503")
    other_job_id, _ = inc.upsert_incident("job-2", "known flaky")
    inc.ack_incident(other_job_id)

    assert inc.close_incidents_for_recovered_job("job-1") == 1
    assert inc.get_incident(open_id)["state"] == "resolved"
    assert inc.get_incident(acked_id)["state"] == "closed"

    assert inc.close_incidents_for_recovered_job("job-1", include_acked=True) == 1
    assert inc.get_incident(acked_id)["state"] == "resolved"
    assert inc.get_incident(other_job_id)["state"] == "closed"


def test_a_keyword_upstream_adds_later_reaches_upstreams_function(monkeypatch, tmp_path):
    from cron.incident_recovery import bind_acked_resolution

    _point_db(monkeypatch, tmp_path)
    seen = []

    def upstream_resolver(job_id, *, reason=None):
        seen.append((job_id, reason))
        return 3

    resolve = bind_acked_resolution(upstream_resolver)

    assert resolve("job-1", reason="verified") == 3
    assert resolve(job_id="job-2", reason="verified", include_acked=True) == 3
    assert seen == [("job-1", "verified"), ("job-2", "verified")]
