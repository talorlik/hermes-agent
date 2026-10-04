"""Real import and storage contracts for the damaged cron declaration boundaries.

Only stdlib imports at collection: a broken cron package must produce a test failure,
not prevent collecting the regression or the independent default-config check.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[2]


class JobsImportContract(unittest.TestCase):
    def _run_isolated(self, source: str) -> None:
        with tempfile.TemporaryDirectory(prefix="cron-import-contract-") as directory:
            home = Path(directory)
            env = {
                key: os.environ[key]
                for key in ("PATH", "SYSTEMROOT", "WINDIR")
                if key in os.environ
            }
            env.update(
                HOME=str(home),
                USERPROFILE=str(home),
                HERMES_HOME=str(home / "hermes"),
                HERMES_KANBAN_HOME=str(home / "hermes"),
                HERMES_RUNTIME_DIR=str(home / "runtime"),
                CC_STATE_DIR=str(home / "cc"),
                TMPDIR=str(home),
                TEMP=str(home),
                TMP=str(home),
                PYTHONPATH=str(ROOT),
                PYTHONDONTWRITEBYTECODE="1",
                PYTHONHASHSEED="0",
                TZ="UTC",
                LANG="C.UTF-8",
            )
            result = subprocess.run(
                [sys.executable, "-B", "-c", textwrap.dedent(source)],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_parse_import_and_restored_call_sites(self) -> None:
        with self.subTest(contract="whole-file parse"):
            ast.parse((ROOT / "cron/jobs.py").read_text(encoding="utf-8"))
        with self.subTest(
            contract="package exports, scheduling, and durable mutations"
        ):
            self._run_isolated("""
                import json
                import os
                from datetime import datetime, timedelta, timezone
                from pathlib import Path
                import unittest
                from zoneinfo import ZoneInfo

                import cron
                from cron import jobs, outbox
                from cron.jobs import (
                    get_job_script_failure_policy, mark_job_deferred,
                    resnapshot_job, resnapshot_all_unpinned,
                )

                check = unittest.TestCase()
                root = Path.cwd()
                assert Path(jobs.__file__).resolve() == root / "cron/jobs.py"
                for name in cron.__all__:
                    assert hasattr(cron, name), name
                assert cron.create_job is jobs.create_job
                assert callable(cron.tick)
                assert jobs._normalize_skill_list("a", ["a", "b", "a"]) == ["a", "b"]
                assert jobs.parse_duration("2 hours") == 120
                assert jobs.parse_duration("hour") == 60
                with check.assertRaises(ValueError):
                    jobs.parse_duration("invalid")
                before = datetime.now(timezone.utc)
                next_run = datetime.fromisoformat(jobs.compute_next_run({"kind": "interval", "minutes": 30}))
                assert 1798 <= (next_run - before).total_seconds() <= 1802
                zone = ZoneInfo("America/New_York")
                first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
                second = first.replace(fold=1)
                assert jobs._instant_before(first, second)
                assert not jobs._instant_before(second, first)
                assert jobs._elapsed_seconds(second, first) == 3600
                schedule = {"kind": "cron", "expr": "0 9 * * *"}
                raw = datetime(2026, 10, 3, 9, tzinfo=timezone.utc)
                assert jobs._classify_stale_cron_next_run(schedule, raw, raw) == jobs.STALE_CRON_MATCH
                shifted = raw.astimezone(timezone(timedelta(hours=2)))
                assert jobs._classify_stale_cron_next_run(schedule, raw, shifted) == jobs.STALE_CRON_TIMEZONE_MIGRATION

                home = Path(os.environ["HERMES_HOME"])
                home.mkdir(parents=True, exist_ok=True)
                assert jobs._main_model_pin() == (None, None)
                for scope in (home / "a", home / "b", home / "a"):
                    with jobs.use_cron_store(scope):
                        old = jobs.get_catch_up_occurrence_count()
                        jobs.record_catch_up_occurrence()
                        assert jobs.get_catch_up_occurrence_count() == old + 1
                        assert old == (1 if scope == home / "a" and (scope / "seen").exists() else 0)
                        (scope / "seen").touch()

                job = jobs.create_job("probe", "every 1h", skills=["a", "a", "b"],
                                      provider="local", model="explicit", base_url="http://localhost:8000/",
                                      script="probe.py", script_failure_policy="fail_closed")
                job_id = job["id"]
                assert job["base_url"] == "http://localhost:8000"
                assert get_job_script_failure_policy(jobs.get_job(job_id)) == "fail_closed"
                assert get_job_script_failure_policy({}) == "continue"
                with check.assertRaises(ValueError):
                    get_job_script_failure_policy({"script_failure_policy": None})
                with check.assertRaises(ValueError):
                    jobs.create_job("probe", "every 1h", script_failure_policy="fail_closed")
                with check.assertRaises(ValueError):
                    jobs.update_job(job_id, {"script": None})
                jobs.update_job(job_id, {"script": None, "script_failure_policy": "continue"})
                assert jobs.pause_job(job_id)["state"] == "paused"
                assert jobs.resume_job(job_id)["state"] == "scheduled"
                triggered = jobs.trigger_job(job_id, extra_prompt="one fire")
                assert triggered["manual_run_prompt"] == "one fire"
                assert triggered["manual_run_at"] == triggered["next_run_at"]
                assert (triggered["provider"], triggered["model"]) == ("local", "explicit")
                assert resnapshot_job(job_id)["model"] == "explicit"
                assert resnapshot_all_unpinned() == []
                assert jobs.update_job(job_id, {"pinned": False})["model"] is None
                assert jobs.get_job("missing") is None
                assert jobs.update_job("missing", {"name": "absent"}) is None

                projection = outbox.begin_job_delivery_projection(job_id, "execution-a")
                updates = {"last_delivery_unverified": ["local:test"]}
                assert jobs.update_job(job_id, updates, expected_execution_id="stale") is None
                assert jobs.update_job(job_id, updates, expected_execution_id="execution-a",
                                       expected_projection_revision=projection["revision"] + 1) is None
                assert jobs.update_job(job_id, updates, expected_execution_id="execution-a",
                                       expected_projection_revision=projection["revision"])
                assert outbox.finalize_job_delivery_projection(
                    job_id, execution_id="execution-a", expected_revision=projection["revision"],
                    last_status="delivered", last_delivery_error=None,
                    last_delivery_unverified=[], last_delivery_queued=[])
                assert jobs.get_job(job_id)["last_status"] == "delivered"
                assert jobs.get_job(job_id)["last_delivery_unverified"] == []

                once = jobs.create_job("one shot", "in 30m")
                owner = "contract-owner"
                jobs.update_job(once["id"], {"fire_claim": {"by": owner},
                    "run_claim": {"by": owner}, "repeat": {"times": 1, "completed": 1}})
                retry = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
                assert not mark_job_deferred(once["id"], retry, expected_fire_owner="stale")
                assert jobs.get_job(once["id"])["repeat"]["completed"] == 1
                assert mark_job_deferred(once["id"], retry, expected_fire_owner=owner,
                                         reason="busy", occurrence_key="slot", attempts=2)
                deferred = jobs.get_job(once["id"])
                assert deferred["repeat"]["completed"] == 0
                assert deferred["next_run_at"] == deferred["schedule"]["run_at"] == retry
                assert deferred["fire_claim"] is None and deferred["run_claim"] is None
                assert deferred["last_defer"]["occurrence_key"] == "slot"
                assert deferred["last_defer"]["attempts"] == 2
                assert not mark_job_deferred("missing", retry)
                stored = json.loads((home / "cron/jobs.json").read_text(encoding="utf-8"))
                assert {row["id"] for row in stored["jobs"]} == {job_id, once["id"]}
                print("PASS: real cron package, scheduling, policy, pins, delivery CAS, deferral, and storage")
            """)

    def test_default_retention_policy_keeps_terminal_history(self) -> None:
        self._run_isolated("""
            import json
            import os
            from pathlib import Path
            from hermes_cli.config_defaults import DEFAULT_CONFIG

            assert "max_terminal_executions" in DEFAULT_CONFIG["cron"], "missing registered cron ceiling"
            assert DEFAULT_CONFIG["cron"]["max_terminal_executions"] is None
            from cron import executions

            home = Path(os.environ["HERMES_HOME"])
            home.mkdir(parents=True, exist_ok=True)
            config = home / "config.yaml"
            config.write_text(json.dumps({"cron": DEFAULT_CONFIG["cron"]}), encoding="utf-8")
            assert executions.terminal_execution_ceiling() is None
            first = executions.create_execution("retention-contract", source="test")
            assert executions.finish_execution(first["id"], success=True)["status"] == "completed"
            config.write_text('cron:\\n  max_terminal_executions: invalid\\n', encoding="utf-8")
            second = executions.create_execution("retention-contract", source="test")
            assert executions.finish_execution(second["id"], success=True)["status"] == "completed"
            assert executions.get_execution(first["id"])["status"] == "completed"
            assert executions.get_execution(second["id"])["status"] == "completed"
            print("PASS: registered default, genuine ledger finish, malformed-policy preservation")
        """)


if __name__ == "__main__":
    unittest.main()
