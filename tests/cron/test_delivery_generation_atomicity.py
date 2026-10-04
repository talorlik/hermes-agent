"""Delivery-generation publication races against the real SQLite projection and raw jobs.json.

Contract (G01C): ``update_job`` with expected execution/revision tokens must compare the owned
generation and publish jobs.json under one exclusion with the projection writers, in this order:
jobs lock -> outbox RLock -> SQLite ``BEGIN IMMEDIATE`` reservation -> validate -> write/replace.

* rotation-first: a projection rotation that commits before the reservation makes the stale update
  reject with ``None`` and leaves jobs.json byte-identical;
* publication-first: once the reservation is held, an independent *process* rotation cannot commit
  until the JSON publication finishes;
* exceptions inside publication release the reservation; missing projections reject explicitly;
  token-free (legacy) updates are untouched.

This is concurrency exclusion, not a crash-atomic cross-store commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UPDATE = {"last_delivery_unverified": ["old-target"]}

_CHILD_ROTATE = """
import json, sys
from pathlib import Path
from cron import executions, outbox
executions.EXECUTIONS_FILE = Path(sys.argv[1])
outbox.EXECUTIONS_FILE = Path(sys.argv[1])
sys.stdout.write("ready\\n"); sys.stdout.flush()
row = outbox.begin_job_delivery_projection(sys.argv[2], sys.argv[3])
sys.stdout.write(json.dumps({"execution_id": row["execution_id"], "revision": row["revision"]}) + "\\n")
"""


@pytest.fixture
def store(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    from cron import executions, jobs, outbox

    db = home / "cron" / "executions.db"
    monkeypatch.setattr(executions, "EXECUTIONS_FILE", db)
    monkeypatch.setattr(outbox, "EXECUTIONS_FILE", db)
    job = jobs.create_job("generation race", "every 1h")
    return home, db, jobs, outbox, job


def _jobs_bytes(home: Path) -> bytes:
    return (home / "cron" / "jobs.json").read_bytes()


def _stored(home: Path, job_id: str) -> dict:
    return next(j for j in json.loads(_jobs_bytes(home))["jobs"] if j["id"] == job_id)


def _guarded_update(jobs, job, old, updates=UPDATE, name="race-old-update"):
    """Run the stale-token update on a named worker thread; return (thread, answers, errors)."""
    answers, errors = [], []

    def run():
        try:
            answers.append(
                jobs.update_job(
                    job["id"],
                    dict(updates),
                    expected_execution_id=old["execution_id"],
                    expected_projection_revision=old["revision"],
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surfaced by the test
            errors.append(exc)

    return threading.Thread(target=run, name=name), answers, errors


def _pause_before_real_save(jobs, monkeypatch, thread_name):
    """Mirror the frozen reviewer probe: pause *before* the real save_jobs is invoked."""
    at_save, resume = threading.Event(), threading.Event()
    real_save = jobs.save_jobs

    def paused(rows, **kwargs):
        if threading.current_thread().name == thread_name:
            at_save.set()
            assert resume.wait(5), "rotation blocked while only the jobs lock is held"
        return real_save(rows, **kwargs)

    monkeypatch.setattr(jobs, "save_jobs", paused)
    return at_save, resume


@pytest.mark.parametrize(
    "new_execution",
    ["new", "old"],
    ids=["new-execution", "same-execution-new-revision"],
)
def test_rotation_first_rejects_without_byte_change(store, monkeypatch, new_execution):
    home, _db, jobs, outbox, job = store
    jobs.update_job(job["id"], {"name": "kept name"})
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    before = _jobs_bytes(home)
    at_save, resume = _pause_before_real_save(jobs, monkeypatch, "race-old-update")
    worker, answers, errors = _guarded_update(jobs, job, old)
    worker.start()
    try:
        assert at_save.wait(5), "update never reached persistence"
        rotated = outbox.begin_job_delivery_projection(job["id"], new_execution)
        assert rotated["revision"] > old["revision"]
    finally:
        resume.set()
        worker.join(5)
    assert not worker.is_alive() and not errors
    assert answers == [None]
    assert _jobs_bytes(home) == before, "rejected publication must not touch jobs.json"
    stored = _stored(home, job["id"])
    assert stored["name"] == "kept name"
    assert stored.get("last_delivery_unverified") is None
    assert (
        outbox.get_job_delivery_projection(job["id"])["revision"] == rotated["revision"]
    )


def test_rejection_keeps_unrelated_fields_and_accepts_owned_generation(store):
    home, _db, jobs, outbox, job = store
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    jobs.update_job(job["id"], {"name": "renamed before"})
    rotated = outbox.begin_job_delivery_projection(job["id"], "new")
    stale = jobs.update_job(
        job["id"],
        {**UPDATE, "name": "stale rename"},
        expected_execution_id="old",
        expected_projection_revision=old["revision"],
    )
    assert stale is None
    assert _stored(home, job["id"])["name"] == "renamed before"
    owned = jobs.update_job(
        job["id"],
        UPDATE,
        expected_execution_id="new",
        expected_projection_revision=rotated["revision"],
    )
    assert owned is not None
    stored = _stored(home, job["id"])
    assert stored["last_delivery_unverified"] == ["old-target"]
    assert stored["name"] == "renamed before"


def test_save_jobs_guard_rejects_stale_generation_without_write(store):
    home, _db, jobs, outbox, job = store
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    outbox.begin_job_delivery_projection(
        job["id"], "old"
    )  # same execution, new revision
    before = _jobs_bytes(home)
    rows = jobs.load_jobs()
    rows[0]["last_delivery_unverified"] = ["stale"]
    guard = jobs.DeliveryProjectionGuard(
        job_id=job["id"], execution_id="old", revision=old["revision"]
    )
    assert jobs.save_jobs(rows, delivery_guard=guard) is False
    assert _jobs_bytes(home) == before
    current = outbox.get_job_delivery_projection(job["id"])
    assert (
        jobs.save_jobs(
            rows,
            delivery_guard=jobs.DeliveryProjectionGuard(
                job_id=job["id"], execution_id="old", revision=current["revision"]
            ),
        )
        is True
    )
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["stale"]


def test_missing_projection_rejects_explicitly(store):
    home, _db, jobs, outbox, job = store
    before = _jobs_bytes(home)
    assert outbox.get_job_delivery_projection(job["id"]) is None
    assert (
        jobs.update_job(
            job["id"],
            UPDATE,
            expected_execution_id="ghost",
            expected_projection_revision=1,
        )
        is None
    )
    rows = jobs.load_jobs()
    guard = jobs.DeliveryProjectionGuard(
        job_id=job["id"], execution_id="ghost", revision=1
    )
    assert jobs.save_jobs(rows, delivery_guard=guard) is False
    assert _jobs_bytes(home) == before


def test_execution_only_token_is_rejected_without_byte_change(store):
    """An execution id without its owned revision is a partial generation token. It must never be
    accepted as "the current revision": after a same-execution rotation the stale writer would
    otherwise publish over the newer generation."""
    home, _db, jobs, outbox, job = store
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    before = _jobs_bytes(home)
    # Projection still exactly at the owned execution: execution-only is still incomplete.
    assert jobs.update_job(job["id"], UPDATE, expected_execution_id="old") is None
    assert _jobs_bytes(home) == before
    # Same execution, new revision: the classic stale publication that execution-only CAS let through.
    rotated = outbox.begin_job_delivery_projection(job["id"], "old")
    assert rotated["revision"] > old["revision"]
    assert jobs.update_job(job["id"], UPDATE, expected_execution_id="old") is None
    assert _jobs_bytes(home) == before
    rows = jobs.load_jobs()
    rows[0]["last_delivery_unverified"] = ["stale"]
    guard = jobs.DeliveryProjectionGuard(
        job_id=job["id"], execution_id="old", revision=None
    )
    assert jobs.save_jobs(rows, delivery_guard=guard) is False
    assert _jobs_bytes(home) == before
    assert (
        outbox.projection_generation_matches(
            outbox.get_job_delivery_projection(job["id"]), execution_id="old"
        )
        is False
    )
    # The complete owned token for the current generation still publishes.
    assert (
        jobs.update_job(
            job["id"],
            UPDATE,
            expected_execution_id="old",
            expected_projection_revision=rotated["revision"],
        )
        is not None
    )
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["old-target"]


def test_revision_only_token_is_rejected_without_byte_change(store):
    """A revision without its execution id is equally partial: it is ignored today (the update
    becomes token-free), so a stale writer publishes unguarded."""
    home, _db, jobs, outbox, job = store
    current = outbox.begin_job_delivery_projection(job["id"], "old")
    before = _jobs_bytes(home)
    assert (
        jobs.update_job(
            job["id"], UPDATE, expected_projection_revision=current["revision"]
        )
        is None
    )
    assert _jobs_bytes(home) == before
    assert (
        jobs.update_job(
            job["id"], UPDATE, expected_projection_revision=current["revision"] - 1
        )
        is None
    )
    assert _jobs_bytes(home) == before
    assert _stored(home, job["id"]).get("last_delivery_unverified") is None


def test_legacy_token_free_update_is_unaffected(store):
    home, _db, jobs, outbox, job = store
    outbox.begin_job_delivery_projection(job["id"], "old")
    outbox.begin_job_delivery_projection(job["id"], "new")
    assert jobs.update_job(job["id"], UPDATE) is not None
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["old-target"]
    rows = jobs.load_jobs()
    rows[0]["name"] = "legacy save"
    assert jobs.save_jobs(rows) is True
    assert _stored(home, job["id"])["name"] == "legacy save"


def _spawn_child_rotation(db: Path, job_id: str, execution_id: str) -> subprocess.Popen:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "HERMES_HOME": str(db.parents[1]),
    }
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", _CHILD_ROTATE, str(db), job_id, execution_id],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "ready"
    return child


def test_publication_first_blocks_independent_process_rotation(store, monkeypatch):
    home, db, jobs, outbox, job = store
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    at_replace, resume = threading.Event(), threading.Event()
    real_replace = jobs.atomic_replace

    def paused_replace(src, dst):
        if threading.current_thread().name == "race-old-update":
            at_replace.set()
            assert resume.wait(10), "publication never released"
        return real_replace(src, dst)

    monkeypatch.setattr(jobs, "atomic_replace", paused_replace)
    worker, answers, errors = _guarded_update(jobs, job, old)
    worker.start()
    child = None
    try:
        assert at_replace.wait(5), "update never reached the JSON replace boundary"
        child = _spawn_child_rotation(db, job["id"], "new")
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            assert child.poll() is None, (
                "independent process rotated the projection while publication held the reservation"
            )
            time.sleep(0.05)
    finally:
        resume.set()
        worker.join(10)
        if child is not None:
            out, err = child.communicate(timeout=20)
    assert not worker.is_alive() and not errors
    assert answers[0] is not None and answers[0]["last_delivery_unverified"] == [
        "old-target"
    ]
    assert child.returncode == 0, err
    rotated = json.loads(out.strip().splitlines()[-1])
    assert rotated == {"execution_id": "new", "revision": old["revision"] + 1}
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["old-target"]
    assert outbox.get_job_delivery_projection(job["id"])["execution_id"] == "new"


def test_publication_exception_releases_reservation(store, monkeypatch):
    home, _db, jobs, outbox, job = store
    old = outbox.begin_job_delivery_projection(job["id"], "old")
    before = _jobs_bytes(home)

    def exploding_replace(src, dst):
        raise RuntimeError("synthetic publication fault")

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(jobs, "atomic_replace", exploding_replace)
        with pytest.raises(RuntimeError, match="synthetic publication fault"):
            jobs.update_job(
                job["id"],
                UPDATE,
                expected_execution_id="old",
                expected_projection_revision=old["revision"],
            )
    assert _jobs_bytes(home) == before
    started = time.monotonic()
    rotated = outbox.begin_job_delivery_projection(job["id"], "new")
    assert time.monotonic() - started < 2.0, (
        "reservation leaked after the publication exception"
    )
    assert rotated["revision"] == old["revision"] + 1
    assert (
        jobs.update_job(
            job["id"],
            UPDATE,
            expected_execution_id="new",
            expected_projection_revision=rotated["revision"],
        )
        is not None
    )


def test_delivery_verification_uses_invocation_owned_execution(
    store, monkeypatch, caplog
):
    """scheduler_delivery must CAS on the execution it owns, never the newest projection token."""
    import logging

    home, _db, jobs, outbox, job = store
    from cron import scheduler_delivery

    old = outbox.begin_job_delivery_projection(job["id"], "old")
    owned = dict(job, execution_id="old", _delivery_projection_revision=old["revision"])
    seen = []
    real_update = jobs.update_job

    def spy(job_id, updates, **cas):
        seen.append(cas)
        return real_update(job_id, updates, **cas)

    monkeypatch.setattr(jobs, "update_job", spy)
    # Owned generation still current: the CAS carries exactly the invocation's own tokens.
    scheduler_delivery._record_delivery_verification(owned, ["first-target"])
    assert seen == [
        {
            "expected_execution_id": "old",
            "expected_projection_revision": old["revision"],
        }
    ]
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["first-target"]

    # Rotated underneath (same execution, new revision, then a new execution): the recorder keeps
    # its own tokens instead of adopting the newest projection, so the stale write is rejected.
    seen.clear()
    outbox.begin_job_delivery_projection(job["id"], "old")
    outbox.begin_job_delivery_projection(job["id"], "new")
    scheduler_delivery._record_delivery_verification(dict(owned), ["old-target"])
    assert seen == [
        {
            "expected_execution_id": "old",
            "expected_projection_revision": old["revision"],
        }
    ]
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["first-target"]

    # Owned execution without an owned revision: the token is incomplete. The recorder must neither
    # borrow the newest projection revision nor fall back to an execution-only or unguarded write;
    # it refuses, reports why, and leaves jobs.json untouched.
    seen.clear()
    before = _jobs_bytes(home)
    with caplog.at_level(logging.WARNING, logger="cron.scheduler_delivery"):
        scheduler_delivery._record_delivery_verification(
            dict(job, execution_id="old"), ["x"]
        )
    assert seen == []
    assert _jobs_bytes(home) == before
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["first-target"]
    assert any(
        "revision" in record.getMessage() and "not recorded" in record.getMessage()
        for record in caplog.records
    ), caplog.text

    # Token-free asynchronous callback while a modern projection generation exists: the recorder
    # must not take the legacy unguarded write over it (byte-identical jobs.json, projection kept).
    seen.clear()
    before = _jobs_bytes(home)
    legacy = dict(job)
    legacy.pop("execution_id", None)
    scheduler_delivery._record_delivery_verification(legacy, ["legacy-target"])
    assert seen == [{"require_no_delivery_projection": True}]
    assert _jobs_bytes(home) == before
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["first-target"]
    assert outbox.get_job_delivery_projection(job["id"])["execution_id"] == "new"


def test_token_free_callback_publishes_only_while_no_projection_exists(
    store, monkeypatch
):
    """Legacy compatibility is scoped to jobs that never had a projection generation, and the
    no-row check is binding at write time: a generation created between the recorder's cheap
    pre-check and the reserved publication rejects the token-free write (no pre-check reliance)."""
    home, _db, jobs, outbox, job = store
    from cron import scheduler_delivery

    token_free = dict(job)
    token_free.pop("execution_id", None)
    assert outbox.get_job_delivery_projection(job["id"]) is None
    scheduler_delivery._record_delivery_verification(
        dict(token_free), ["legacy-target"]
    )
    assert _stored(home, job["id"])["last_delivery_unverified"] == ["legacy-target"]

    # Fresh job without any projection; the competing generation lands AFTER the pre-check read.
    other = jobs.create_job("race-created generation", "every 1h")
    other_free = dict(other)
    other_free.pop("execution_id", None)
    real_read = outbox.get_job_delivery_projection
    created: list = []

    def read_then_create(job_id):
        projection = real_read(job_id)
        if str(job_id) == other["id"] and not created:
            # Pre-check observed no row; an independent producer begins the generation before
            # the reservation is taken.
            created.append(
                outbox.begin_job_delivery_projection(other["id"], "exec-race")
            )
        return projection

    monkeypatch.setattr(outbox, "get_job_delivery_projection", read_then_create)
    before = _jobs_bytes(home)
    scheduler_delivery._record_delivery_verification(other_free, ["raced-target"])
    assert created, "the competing generation creation never ran"
    assert _jobs_bytes(home) == before
    assert _stored(home, other["id"]).get("last_delivery_unverified") is None
    assert (
        outbox.get_job_delivery_projection(other["id"])["revision"]
        == created[0]["revision"]
    )
