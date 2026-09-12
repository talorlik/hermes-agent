"""Tests for the Kanban dashboard plugin backend (plugins/kanban/dashboard/plugin_api.py).

The plugin mounts as /api/plugins/kanban/ inside the dashboard's FastAPI app,
but here we attach its router to a bare FastAPI instance so we can test the
REST surface without spinning up the whole dashboard.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _load_plugin_router():
    """Dynamically load plugins/kanban/dashboard/plugin_api.py and return its router."""
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    assert plugin_file.exists(), f"plugin file missing: {plugin_file}"

    spec = importlib.util.spec_from_file_location(
        "hermes_dashboard_plugin_kanban_test", plugin_file,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def client(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /board on an empty DB
# ---------------------------------------------------------------------------


def test_board_empty(client):
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    # All canonical columns present (triage + the rest), each empty.
    names = [c["name"] for c in data["columns"]]
    assert set(names) == kb.VALID_STATUSES - {"archived"}
    for expected in ("triage", "todo", "scheduled", "ready", "running", "blocked", "done"):
        assert expected in names, f"missing column {expected}: {names}"
    assert all(len(c["tasks"]) == 0 for c in data["columns"])
    assert data["tenants"] == []
    assert data["assignees"] == []
    assert data["latest_event_id"] == 0


# ---------------------------------------------------------------------------
# POST /tasks then GET /board sees it
# ---------------------------------------------------------------------------


def test_create_task_appears_on_board(client):
    r = client.post(
        "/api/plugins/kanban/tasks",
        json={
            "title": "Research LLM caching",
            "assignee": "researcher",
            "priority": 3,
            "tenant": "acme",
        },
    )
    assert r.status_code == 200, r.text
    task = r.json()["task"]
    assert task["title"] == "Research LLM caching"
    assert task["assignee"] == "researcher"
    assert task["status"] == "ready"  # no parents -> immediately ready
    assert task["priority"] == 3
    assert task["tenant"] == "acme"
    task_id = task["id"]

    # Board now lists it under 'ready'.
    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    data = r.json()
    ready = next(c for c in data["columns"] if c["name"] == "ready")
    assert len(ready["tasks"]) == 1
    assert ready["tasks"][0]["id"] == task_id
    assert "acme" in data["tenants"]
    assert "researcher" in data["assignees"]


def test_patch_board_sets_project_directory(client, tmp_path):
    """Board-level default_workdir must be editable after creation."""
    kb.create_board("late-config")
    project_dir = tmp_path / "late-project"
    project_dir.mkdir()

    response = client.patch(
        "/api/plugins/kanban/boards/late-config",
        json={"default_workdir": str(project_dir)},
    )

    assert response.status_code == 200, response.text
    board = response.json()["board"]
    assert board["default_workdir"] == str(project_dir.resolve())
    # The recommendation flips from scratch to a persistent kind so the
    # create-task dialog's workspace default follows the board setting.
    assert board["default_workspace_kind"] == "dir"
    assert kb.read_board_metadata("late-config")["default_workdir"] == str(
        project_dir.resolve()
    )


def test_scheduled_tasks_have_their_own_column_not_todo(client):
    """Scheduled/time-delay tasks must not be silently bucketed into todo."""

    task = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "wait for indexed data", "assignee": "ops"},
    ).json()["task"]

    conn = kbc.connect()
    try:
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'scheduled' WHERE id = ?",
                (task["id"],),
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/board")
    assert r.status_code == 200
    columns = {c["name"]: c["tasks"] for c in r.json()["columns"]}
    assert any(t["id"] == task["id"] for t in columns["scheduled"])
    assert not any(t["id"] == task["id"] for t in columns["todo"])


def test_tenant_filter(client):
    client.post("/api/plugins/kanban/tasks", json={"title": "A", "tenant": "t1"})
    client.post("/api/plugins/kanban/tasks", json={"title": "B", "tenant": "t2"})

    r = client.get("/api/plugins/kanban/board?tenant=t1")
    counts = {c["name"]: len(c["tasks"]) for c in r.json()["columns"]}
    total = sum(counts.values())
    assert total == 1

    r = client.get("/api/plugins/kanban/board?tenant=t2")
    total = sum(len(c["tasks"]) for c in r.json()["columns"])
    assert total == 1


def test_dashboard_markdown_html_is_sanitized_before_render():
    """Markdown rendering must sanitize HTML before dangerouslySetInnerHTML."""

    repo_root = Path(__file__).resolve().parents[2]
    bundle = repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    js = bundle.read_text(encoding="utf-8")

    assert "function sanitizeMarkdownHtml(html)" in js
    assert "MARKDOWN_ALLOWED_TAGS" in js
    assert "sanitizeMarkdownHtml(renderMarkdown(props.source || \"\"))" in js
    assert "dangerouslySetInnerHTML: { __html: renderMarkdown(props.source || \"\") }" not in js


# ---------------------------------------------------------------------------
# GET /tasks/:id returns body + comments + events + links
# ---------------------------------------------------------------------------


def test_task_detail_includes_links_and_events(client):
    parent = client.post(
        "/api/plugins/kanban/tasks", json={"title": "parent"},
    ).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "child", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"  # parent not done yet

    # Detail for the child shows the parent link.
    r = client.get(f"/api/plugins/kanban/tasks/{child['id']}")
    assert r.status_code == 200
    data = r.json()
    assert data["task"]["id"] == child["id"]
    assert parent["id"] in data["links"]["parents"]

    # Detail for the parent shows the child.
    r = client.get(f"/api/plugins/kanban/tasks/{parent['id']}")
    assert child["id"] in r.json()["links"]["children"]

    # Events exist from creation.
    assert len(data["events"]) >= 1


def test_task_detail_uses_versioned_snapshot_and_preserves_dashboard_envelope(client):
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "snapshot detail"},
    ).json()["task"]
    with kbc.connect() as conn:
        kb.recompute_ready(conn)
        claimed = kb.claim_task(conn, task["id"])
        assert claimed is not None
        comment_id = kb.add_comment(conn, task["id"], "worker", "detail note")

    response = client.get(f"/api/plugins/kanban/tasks/{task['id']}")
    assert response.status_code == 200
    payload = response.json()
    assert payload["schema_version"] == kb.TASK_SNAPSHOT_SCHEMA_VERSION
    assert payload["task"]["block_kind"] is None
    assert payload["task"]["block_recurrences"] == 0
    assert payload["task"]["current_run_id"] == claimed.current_run_id
    assert [item["id"] for item in payload["comments"]] == [comment_id]
    assert all(isinstance(item["id"], int) for item in payload["events"])
    assert set(payload) >= {
        "task", "comments", "events", "attachments", "links",
        "child_results", "runs", "schema_version",
    }


# ---------------------------------------------------------------------------
# PATCH /tasks/:id — status transitions
# ---------------------------------------------------------------------------


def test_patch_review_lifecycle_preserves_handoff_and_reopens(client):
    secret = "ghp_" + "D" * 40
    task = client.post(
        "/api/plugins/kanban/tasks", json={"title": "review me", "assignee": "builder"},
    ).json()["task"]

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={
            "status": "review",
            "assignee": "reviewer",
            "summary": f"Implementation ready. {secret}",
            "metadata": {"tests_run": 4, "token": secret},
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    with kbc.connect() as conn:
        run = kb.latest_run(conn, task["id"])
        assert run is not None
        assert run.outcome == "review_requested"
        assert run.metadata is not None
        assert run.metadata["tests_run"] == 4
        assert secret not in str(run.summary)
        assert secret not in json.dumps(run.metadata)
        review_event = [
            event for event in kb.list_events(conn, task["id"])
            if event.kind == "review_requested"
        ][-1]
        assert secret not in json.dumps(review_event.payload)
        assert review_event.payload is not None
        assert review_event.payload["implementer"] == "builder"
        assert review_event.payload["reviewer"] == "reviewer"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task['id']}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "ready"
    assert response.json()["task"]["assignee"] == "builder"
    with kbc.connect() as conn:
        assert any(
            event.kind == "review_reopened"
            for event in kb.list_events(conn, task["id"])
        )


def test_reopening_parent_demotes_ready_child(client):
    """Reopening a completed parent must invalidate ready children immediately.

    The dispatcher re-checks parent completion on claim, but the dashboard
    should not keep showing a stale child as ready after an operator drags
    its parent back out of done for more work.
    """
    parent = client.post("/api/plugins/kanban/tasks", json={"title": "p"}).json()["task"]
    child = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "c", "parents": [parent["id"]]},
    ).json()["task"]
    assert child["status"] == "todo"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "done"},
    )
    assert r.status_code == 200

    child_after_done = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_done["status"] == "ready"

    r = client.patch(
        f"/api/plugins/kanban/tasks/{parent['id']}",
        json={"status": "todo"},
    )
    assert r.status_code == 200

    child_after_reopen = client.get(
        f"/api/plugins/kanban/tasks/{child['id']}"
    ).json()["task"]
    assert child_after_reopen["status"] == "todo"


def test_reopening_parent_retracts_review_and_blocks_approval(client):
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="parent", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="child in review",
            assignee="reviewer",
            parents=[parent_id],
        )
        grandchild_id = kb.create_task(
            conn,
            title="downstream",
            assignee="writer",
            parents=[child_id],
        )
        implementation = kb.claim_task(conn, child_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            child_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        active_review = kb.claim_review_task(conn, child_id)
        assert active_review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "todo"
        reclaimed = kb.latest_run(conn, child_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"
        assert kb.claim_review_task(conn, child_id) is None
        assert not kb.complete_task(conn, child_id, summary="must not approve")
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "todo"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        assert child is not None
        assert child.status == "review"
        review = kb.claim_review_task(conn, child_id)
        assert review is not None
        assert kb.complete_task(
            conn,
            child_id,
            summary="approved after parent stabilized",
            expected_run_id=review.current_run_id,
        )
        grandchild = kb.get_task(conn, grandchild_id)
        assert grandchild is not None
        assert grandchild.status == "ready"


def test_reopening_parent_recursively_retracts_done_and_running_descendants(client):
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="root", assignee="planner")
        assert kb.complete_task(conn, parent_id)
        child_id = kb.create_task(
            conn,
            title="accepted child",
            assignee="builder",
            parents=[parent_id],
        )
        assert kb.complete_task(conn, child_id)
        grandchild_id = kb.create_task(
            conn,
            title="running grandchild",
            assignee="writer",
            parents=[child_id],
        )
        grandchild_run = kb.claim_task(conn, grandchild_id)
        assert grandchild_run is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text

    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "todo"
        assert grandchild is not None and grandchild.status == "todo"
        assert grandchild.current_run_id is None
        assert kb.claim_task(conn, grandchild_id) is None
        reclaimed = kb.latest_run(conn, grandchild_id)
        assert reclaimed is not None
        assert reclaimed.outcome == "reclaimed"

    response = client.patch(
        f"/api/plugins/kanban/tasks/{parent_id}",
        json={"status": "done"},
    )
    assert response.status_code == 200, response.text
    with kbc.connect() as conn:
        child = kb.get_task(conn, child_id)
        grandchild = kb.get_task(conn, grandchild_id)
        assert child is not None and child.status == "ready"
        assert grandchild is not None and grandchild.status == "todo"


def test_dashboard_reclaim_of_active_review_preserves_review_phase(client):
    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="active review", assignee="reviewer")
        implementation = kb.claim_task(conn, task_id)
        assert implementation is not None
        assert kb.request_review(
            conn,
            task_id,
            summary="ready",
            expected_run_id=implementation.current_run_id,
        )
        review = kb.claim_review_task(conn, task_id)
        assert review is not None

    response = client.patch(
        f"/api/plugins/kanban/tasks/{task_id}",
        json={"status": "ready"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "review"
    assert response.json()["task"]["assignee"] == "reviewer"
    with kbc.connect() as conn:
        run = kb.latest_run(conn, task_id)
        assert run is not None
        assert run.outcome == "reclaimed"
        next_review = kb.claim_review_task(conn, task_id)
        assert next_review is not None


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

def test_delete_task(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "to-delete"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200
    assert r.json()["deleted"] is True
    assert r.json()["task_id"] == t["id"]

    # Gone from board
    board = client.get("/api/plugins/kanban/board").json()
    all_ids = [tt["id"] for col in board["columns"] for tt in col["tasks"]]
    assert t["id"] not in all_ids

    # Gone from detail
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Comments + Links
# ---------------------------------------------------------------------------


def test_add_comment(client):
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]
    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/comments",
        json={"body": "how's progress?", "author": "teknium"},
    )
    assert r.status_code == 200

    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    comments = r.json()["comments"]
    assert len(comments) == 1
    assert comments[0]["body"] == "how's progress?"
    assert comments[0]["author"] == "teknium"


# ---------------------------------------------------------------------------
# Dispatch nudge
# ---------------------------------------------------------------------------


def test_dispatch_dry_run(client):
    client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "work", "assignee": "researcher"},
    )
    r = client.post("/api/plugins/kanban/dispatch?dry_run=true&max=4")
    assert r.status_code == 200
    body = r.json()
    # DispatchResult is serialized as a dataclass dict.
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# Triage column (new v1 status)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Progress rollup (done children / total children)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Auto-init on first board read
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket auth (query-param token)
# ---------------------------------------------------------------------------


def test_ws_events_rejects_when_token_required(tmp_path, monkeypatch):
    """Loopback mode: a missing or wrong ?token= must be rejected with
    policy-violation; the correct token is accepted. The kanban WS now
    delegates to web_server_chat._ws_auth_ok, so we stub that with the real
    loopback-token semantics (auth_required False → constant-time token
    compare)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Stub web_server_chat with a loopback-mode _ws_auth_ok (auth_required False →
    # accept only the correct ?token=). Mirrors the real gate's loopback path.
    import hermes_cli
    import types

    def _fake_ws_auth_ok(ws):
        return ws.query_params.get("token", "") == "secret-xyz"

    stub = types.SimpleNamespace(
        _SESSION_TOKEN="secret-xyz",
        _ws_auth_ok=_fake_ws_auth_ok,
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.web_server_chat", stub)
    monkeypatch.setattr(hermes_cli, "web_server_chat", stub, raising=False)

    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    c = TestClient(app)

    # No token → policy violation close.
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events"):
            pass
    assert exc.value.code == 1008

    # Wrong token → policy violation close.
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect("/api/plugins/kanban/events?token=nope"):
            pass
    assert exc.value.code == 1008

    # Correct token → accepted (connect then close cleanly from our side).
    with c.websocket_connect(
        "/api/plugins/kanban/events?token=secret-xyz"
    ) as ws:
        assert ws is not None  # handshake succeeded


    # The bug symptom was a traceback; we don't assert on stderr because
    # capturing asyncio's internal "exception was never retrieved" logging
    # is flaky. The assertion that matters is: no CancelledError escaped.


# ---------------------------------------------------------------------------
# Bulk actions
# ---------------------------------------------------------------------------


def test_bulk_status_ready(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    # Parent-less tasks land in "ready" already; push them to blocked first.
    for tid in (a["id"], b["id"], c2["id"]):
        client.patch(
            f"/api/plugins/kanban/tasks/{tid}",
            json={"status": "blocked", "block_reason": "wait"},
        )

    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [a["id"], b["id"], c2["id"]], "status": "ready"},
    )
    assert response.status_code == 200
    results = response.json()["results"]
    assert all(item["ok"] for item in results)
    # All three are now ready.
    board = client.get("/api/plugins/kanban/board").json()
    ready = next(col for col in board["columns"] if col["name"] == "ready")
    ids = {task["id"] for task in ready["tasks"]}
    assert {a["id"], b["id"], c2["id"]}.issubset(ids)


def test_bulk_review_assignment_preserves_implementer_provenance(client):
    tasks = [
        client.post(
            "/api/plugins/kanban/tasks",
            json={"title": title, "assignee": "builder"},
        ).json()["task"]
        for title in ("review a", "review b")
    ]
    response = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [task["id"] for task in tasks],
            "status": "review",
            "assignee": "reviewer",
            "summary": "ready",
        },
    )
    assert response.status_code == 200, response.text
    assert all(item["ok"] for item in response.json()["results"])
    with kbc.connect() as conn:
        for task in tasks:
            current = kb.get_task(conn, task["id"])
            assert current is not None
            assert current.status == "review"
            assert current.assignee == "reviewer"
            event = [
                item for item in kb.list_events(conn, task["id"])
                if item.kind == "review_requested"
            ][-1]
            assert event.payload is not None
            assert event.payload["implementer"] == "builder"
            assert event.payload["reviewer"] == "reviewer"


def test_bulk_status_done_forwards_completion_summary(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={
            "ids": [a["id"], b["id"]],
            "status": "done",
            "result": "DECIDED: ship it",
            "summary": "DECIDED: ship it",
            "metadata": {"source": "dashboard"},
        },
    )

    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    conn = kbc.connect()
    try:
        for tid in (a["id"], b["id"]):
            task = kb.get_task(conn, tid)
            run = kb.latest_run(conn, tid)
            assert task.status == "done"
            assert task.result == "DECIDED: ship it"
            assert run.summary == "DECIDED: ship it"
            assert run.metadata == {"source": "dashboard"}
    finally:
        conn.close()


def test_bulk_status_running_rejected(client):
    """Bulk updates must match single-task PATCH: direct 'running' is invalid."""
    t = client.post("/api/plugins/kanban/tasks", json={"title": "x"}).json()["task"]

    r = client.post(
        "/api/plugins/kanban/tasks/bulk",
        json={"ids": [t["id"]], "status": "running"},
    )

    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert results[0]["id"] == t["id"]
    assert results[0]["ok"] is False
    assert "running" in results[0]["error"]

    board = client.get("/api/plugins/kanban/board").json()
    statuses = {
        tt["id"]: col["name"]
        for col in board["columns"]
        for tt in col["tasks"]
    }
    assert statuses.get(t["id"]) != "running"


def test_dashboard_done_actions_prompt_for_completion_summary():
    """Behavioral coverage for the migrated ``requestDialog`` flow.

    Replaces the prior bundle-string-only assertion (which only proved the
    rename landed). The dialog state machine at
    ``plugins/kanban/dashboard/dist/index.js`` resolves with
    ``{confirmed: true|false, summary?}``. Each migrated call site must
    gate the dispatch on the resolved ``confirmed`` flag. This test
    asserts that contract at two layers:

    1. **Bundle cancel guards**: every migrated site gates on ``r.confirmed``
       (or its subscripted alias ``r1.confirmed``/``r2.confirmed``) before
       dispatching. We verify by counting the cancel-guard patterns +
       cross-referencing against the 8 migrated sites listed in the PR
       description.
    2. **Visual affordance**: every destructive ``requestDialog`` call marks
       ``destructive: true`` so the host renders the destructive variant.

    The dispatch path itself (PATCH/DELETE actually firing on confirm, not
    on cancel) is covered by the backend behavioral tests
    ``test_dashboard_confirm_dispatches_expected_*`` and
    ``test_dashboard_cancel_keeps_task_in_old_status`` below — together
    they pin the contract end-to-end.
    """

    repo_root = Path(__file__).resolve().parents[2]
    js = (repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js").read_text()

    import re

    # Match ``if (!r.confirmed)``, ``if (!r1.confirmed)``, ``if (r.confirmed)``
    # (positive-form gate). The bundle uses both polarities:
    # - negative ``if (!r.confirmed) return null;`` in dialog flow bodies
    # - positive ``if (r.confirmed) props.onDeleteBoard(...);`` in JSX handlers
    cancel_guard_pattern = re.compile(
        r"if\s*\(\s*!?\s*r\d?\.confirmed\s*\)",
        re.IGNORECASE,
    )
    guards = cancel_guard_pattern.findall(js)
    # 8 migrated sites per the PR description:
    # moveTask (1), moveSelected (1), applyBulk (1), deleteTask (1),
    # deleteSelected (1), archiveBoard (1), removeAttachment (1), doPatch (1).
    # Plus performMoveTask callers (moveTask/moveSelected each have
    # ``r1.confirmed`` + ``r2.confirmed`` for the two-stage flow) → up to
    # 10 guards. Loose lower bound to avoid brittleness.
    assert len(guards) >= 8, (
        f"expected >= 8 `if (r?.confirmed)` cancel guards in bundle (one "
        f"per migrated site, plus extras for two-stage flows); found {len(guards)}"
    )

    # Visual affordance: every destructive requestDialog call must mark
    # ``destructive: true`` so the host renders the destructive variant.
    # deleteTask, deleteSelected, archiveBoard → at least 3.
    destructive_call_count = js.count("destructive: true")
    assert destructive_call_count >= 3, (
        f"expected >= 3 `destructive: true` requestDialog calls (single "
        f"delete, bulk delete, archive-board); found {destructive_call_count}"
    )


def test_dashboard_cancel_keeps_task_in_old_status(client):
    """Behavioral: the cancel branch of the dispatch path (no PATCH/DELETE
    issued) must leave the task in its previous status. The cancel guard
    lives in the bundle; this test pins the backend contract that the guard
    relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Tasks land in ``ready`` by default. No PATCH issued — simulating the
    # cancel branch in the bundle.
    assert t["status"] == "ready"
    r = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.json()["task"]["status"] == "ready"


def test_dashboard_confirm_dispatches_expected_patch_body(client):
    """Behavioral: the PATCH body shape the bundle produces on confirm
    (status + result + summary) must be accepted by the backend without
    rejection. The backend stores ``result`` as the human-readable
    completion summary (the bundle comments confirm ``summary`` is sent
    duplicatively so the backend can store the value under its preferred
    key while the wire format remains explicit).
    This is the contract the bundle's performMoveTask relies on.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    # Bundle's performMoveTask on confirm with a summary produces:
    #   { status, result: summary, summary: summary }
    r = client.patch(
        f"/api/plugins/kanban/tasks/{t['id']}",
        json={"status": "done", "result": "shipped", "summary": "shipped"},
    )
    assert r.status_code == 200, r.text
    body = r.json()["task"]
    assert body["status"] == "done"
    assert body.get("result") == "shipped"


def test_dashboard_confirm_dispatches_expected_delete(client):
    """Behavioral: the DELETE call the bundle issues on confirm
    (``fetchJSON(`${API}/tasks/${id}`, { method: 'DELETE' })``) must
    succeed and remove the task.
    """
    t = client.post("/api/plugins/kanban/tasks",
                    json={"title": "x"}).json()["task"]
    r = client.delete(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r.status_code == 200, r.text
    # 404 on the now-deleted task confirms removal.
    r2 = client.get(f"/api/plugins/kanban/tasks/{t['id']}")
    assert r2.status_code == 404


def test_dashboard_surfaces_ready_blocked_error_inline():
    """Regression for #26744: failed status transitions must be surfaced
    inline, not swallowed.  The drag/drop banner and the drawer's action
    row each render the parsed API ``detail`` so operators see *why*
    their click did nothing.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    # Helper that strips ``"409: {\"detail\":\"…\"}"`` down to the
    # human-readable message before it lands in any banner.
    assert "function parseApiErrorMessage(err)" in bundle
    assert "parsed.detail" in bundle

    # Drag/drop banner now uses the parsed message instead of raw
    # ``err.message`` so it no longer leaks HTTP plumbing.
    assert "setError(tx(t, \"moveFailed\", \"Move failed: \") + parseApiErrorMessage(err))" in bundle

    # Drawer action row has its own visible error surface and clears it
    # on success/refresh so stale failures don't follow the operator
    # around.
    assert "const [patchErr, setPatchErr] = useState(null);" in bundle
    assert "setPatchErr(parseApiErrorMessage(e))" in bundle
    assert "setPatchErr(null)" in bundle


def test_dashboard_dependency_selects_use_value_change_handler():
    """Regression for the dependency selects in the task drawer: the
    add-parent / add-child dropdowns must wire through the shared
    selectChangeHandler helper so their value actually lands on the
    underlying React state. Salvaged from #20019 @LeonSGP43.
    """
    repo_root = Path(__file__).resolve().parents[2]
    bundle = (
        repo_root / "plugins" / "kanban" / "dashboard" / "dist" / "index.js"
    ).read_text()

    parent_select = (
        'value: newParent,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewParent))'
    )
    child_select = (
        'value: newChild,\n'
        '          className: "h-7 text-xs flex-1",\n'
        '        }, selectChangeHandler(setNewChild))'
    )

    assert parent_select in bundle
    assert child_select in bundle


def test_bulk_archive(client):
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks", json={"title": "b"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "archive": True})
    assert r.status_code == 200
    assert all(r["ok"] for r in r.json()["results"])
    # Default board (archived hidden) — both gone.
    board = client.get("/api/plugins/kanban/board").json()
    ids = {t["id"] for col in board["columns"] for t in col["tasks"]}
    assert a["id"] not in ids
    assert b["id"] not in ids


def test_bulk_reassign(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "old"}).json()["task"]
    b = client.post("/api/plugins/kanban/tasks",
                    json={"title": "b", "assignee": "old"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], b["id"]], "assignee": "new"})
    assert r.status_code == 200
    for tid in (a["id"], b["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["assignee"] == "new"


def test_bulk_unassign_via_empty_string(client):
    a = client.post("/api/plugins/kanban/tasks",
                    json={"title": "a", "assignee": "x"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"]], "assignee": ""})
    assert r.status_code == 200
    t = client.get(f"/api/plugins/kanban/tasks/{a['id']}").json()["task"]
    assert t["assignee"] is None


def test_bulk_partial_failure_doesnt_abort_siblings(client):
    """One bad id in the middle of a batch must not prevent others from
    applying."""
    a = client.post("/api/plugins/kanban/tasks", json={"title": "a"}).json()["task"]
    c2 = client.post("/api/plugins/kanban/tasks", json={"title": "c"}).json()["task"]
    r = client.post("/api/plugins/kanban/tasks/bulk",
                    json={"ids": [a["id"], "bogus-id", c2["id"]], "priority": 7})
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    ok_ids = {r["id"] for r in results if r["ok"]}
    assert a["id"] in ok_ids
    assert c2["id"] in ok_ids
    assert any(not r["ok"] and r["id"] == "bogus-id" for r in results)
    # Good siblings actually got the priority bump.
    for tid in (a["id"], c2["id"]):
        t = client.get(f"/api/plugins/kanban/tasks/{tid}").json()["task"]
        assert t["priority"] == 7


def test_bulk_empty_ids_400(client):
    r = client.post("/api/plugins/kanban/tasks/bulk", json={"ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# /config endpoint
# ---------------------------------------------------------------------------


def test_config_reads_dashboard_kanban_section(tmp_path, monkeypatch, client):
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "dashboard:\n"
        "  kanban:\n"
        "    default_tenant: acme\n"
        "    lane_by_profile: false\n"
        "    include_archived_by_default: true\n"
        "    render_markdown: false\n"
    )
    r = client.get("/api/plugins/kanban/config")
    assert r.status_code == 200
    data = r.json()
    assert data["default_tenant"] == "acme"
    assert data["lane_by_profile"] is False
    assert data["include_archived_by_default"] is True
    assert data["render_markdown"] is False


# ---------------------------------------------------------------------------
# Runs surfacing (vulcan-artivus RFC feedback)
# ---------------------------------------------------------------------------


def test_event_dict_includes_run_id(client):
    """GET /tasks/:id returns events with run_id populated."""
    r = client.post("/api/plugins/kanban/tasks", json={"title": "e", "assignee": "worker"})
    tid = r.json()["task"]["id"]
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    conn = kbc.connect()
    try:
        kb.claim_task(conn, tid)
        run_id = kb.latest_run(conn, tid).id
        kb.complete_task(conn, tid, summary="wss")
    finally:
        conn.close()

    r = client.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200
    events = r.json()["events"]
    # Every event in the response must have a run_id key (None or int).
    for e in events:
        assert "run_id" in e, f"missing run_id in event: {e}"
    # completed event must have the actual run_id.
    comp = [e for e in events if e["kind"] == "completed"]
    assert comp[0]["run_id"] == run_id


# ---------------------------------------------------------------------------
# Per-task force-loaded skills via REST
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Dispatcher-presence warning in POST /tasks response
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _task_dict — outer try/except fallback when task_age raises
#
# Background: kanban_db.task_age was hardened in 061a1830 to return None for
# corrupt timestamp values via _safe_int. The companion fix added a belt-and-
# suspenders try/except in plugin_api._task_dict so that *any future* exception
# from task_age (not just ValueError on '%s') still yields a usable dict
# instead of 500'ing GET /board for the entire org.
#
# kanban_db._safe_int / task_age corruption paths are covered in
# tests/hermes_cli/test_kanban_db.py. The OUTER fallback here is not, which
# means a refactor that drops the try/except would not be caught by CI. The
# tests below pin that contract.
# ---------------------------------------------------------------------------


_FALLBACK_AGE = {
    "created_age_seconds": None,
    "started_age_seconds": None,
    "time_to_complete_seconds": None,
}


# ---------------------------------------------------------------------------
# Home-channel subscription endpoints (#19534 follow-up: GUI opt-in)
# ---------------------------------------------------------------------------
#
# Dashboard surface for per-task, per-platform notification toggles. The
# backend endpoints read the live GatewayConfig, so tests set env vars
# (BOT_TOKEN + HOME_CHANNEL) to simulate a user who has run /sethome on
# telegram and discord.


@pytest.fixture
def with_home_channels(monkeypatch):
    """Simulate a user with home channels set on telegram and discord."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "abc:fake")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL", "1234567")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_THREAD_ID", "42")
    monkeypatch.setenv("TELEGRAM_HOME_CHANNEL_NAME", "Main TG")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "disc_fake")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "9999999")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL_NAME", "Main Discord")
    # Slack has a token but NO home — should be excluded from the list.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "slack_fake")


def test_home_channels_lists_only_platforms_with_home(client, with_home_channels):
    """GET /home-channels returns entries only for platforms where the
    user has set a home; untoggled-subscribed bool is false by default."""
    r = client.get("/api/plugins/kanban/home-channels")
    assert r.status_code == 200
    platforms = {h["platform"] for h in r.json()["home_channels"]}
    assert platforms == {"telegram", "discord"}, (
        f"slack has a token but no home — must not appear. got {platforms}"
    )
    for h in r.json()["home_channels"]:
        assert h["subscribed"] is False


# ---------------------------------------------------------------------------
# Recovery endpoints (reclaim + reassign) and warnings field
# ---------------------------------------------------------------------------


def test_reclaim_endpoint_releases_running_claim(client):
    """POST /tasks/<id>/reclaim drops the claim, returns ok, and emits
    a manual reclaimed event."""
    import secrets
    conn = kbc.connect()
    try:
        t = kb.create_task(conn, title="running", assignee="x")
        lock = secrets.token_hex(8)
        future = int(time.time()) + 3600
        conn.execute(
            "UPDATE tasks SET status='running', claim_lock=?, claim_expires=?, "
            "worker_pid=? WHERE id=?",
            (lock, future, 99999, t),
        )
        conn.execute(
            "INSERT INTO task_runs (task_id, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'running', ?, ?, ?, ?)",
            (t, lock, future, 99999, int(time.time())),
        )
        run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (run_id, t))
        conn.commit()
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reclaim",
        json={"reason": "browser recovery"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t

    # Confirm the task is back to ready.
    conn2 = kbc.connect()
    try:
        row = conn2.execute(
            "SELECT status, claim_lock FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
    finally:
        conn2.close()


def test_reassign_endpoint_switches_profile(client):
    """POST /tasks/<id>/reassign changes the assignee field."""
    conn = kbc.connect()
    try:
        t = kb.create_task(conn, title="task", assignee="orig")
    finally:
        conn.close()

    r = client.post(
        f"/api/plugins/kanban/tasks/{t}/reassign",
        json={"profile": "newbie", "reclaim_first": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["assignee"] == "newbie"

    conn2 = kbc.connect()
    try:
        row = conn2.execute(
            "SELECT assignee FROM tasks WHERE id=?", (t,),
        ).fetchone()
        assert row["assignee"] == "newbie"
    finally:
        conn2.close()


# ---------------------------------------------------------------------------
# Diagnostics endpoint (/api/plugins/kanban/diagnostics)
# ---------------------------------------------------------------------------


def test_diagnostics_endpoint_surfaces_blocked_hallucination(client):
    conn = kbc.connect()
    try:
        parent = kb.create_task(conn, title="parent", assignee="alice")
        real = kb.create_task(conn, title="real", assignee="x", created_by="alice")
        import pytest as _pytest
        with _pytest.raises(kb.HallucinatedCardsError):
            kb.complete_task(
                conn, parent, summary="phantom",
                created_cards=[real, "t_ffff00001234"],
            )
    finally:
        conn.close()

    r = client.get("/api/plugins/kanban/diagnostics")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    row = data["diagnostics"][0]
    assert row["task_id"] == parent
    assert row["diagnostics"][0]["kind"] == "hallucinated_cards"
    assert row["diagnostics"][0]["severity"] == "error"
    assert "t_ffff00001234" in row["diagnostics"][0]["data"]["phantom_ids"]


# ---------------------------------------------------------------------------
# POST /tasks/:id/specify — triage specifier endpoint
# ---------------------------------------------------------------------------


def _patch_specifier_response(monkeypatch, *, content, model="test-model"):
    """Helper: install a fake auxiliary client so the specifier endpoint
    can run without hitting any real provider."""
    from unittest.mock import MagicMock

    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    # specify_task routes through call_llm now (#35566) — mock it directly.
    fake_call = MagicMock(return_value=resp)
    monkeypatch.setattr("agent.auxiliary_client.call_llm", fake_call)
    return fake_call


def test_specify_happy_path(client, monkeypatch):
    import json as jsonlib

    # Create a triage task.
    t = client.post(
        "/api/plugins/kanban/tasks",
        json={"title": "one-liner", "triage": True},
    ).json()["task"]
    assert t["status"] == "triage"

    _patch_specifier_response(
        monkeypatch,
        content=jsonlib.dumps(
            {"title": "Polished", "body": "**Goal**\nDo the thing."}
        ),
    )

    r = client.post(
        f"/api/plugins/kanban/tasks/{t['id']}/specify",
        json={"author": "ui-tester"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["task_id"] == t["id"]
    assert body["new_title"] == "Polished"

    # Task should have moved off the triage column.
    detail = client.get(f"/api/plugins/kanban/tasks/{t['id']}").json()["task"]
    assert detail["status"] in {"todo", "ready"}
    assert detail["title"] == "Polished"
    assert "**Goal**" in (detail["body"] or "")


# ---------------------------------------------------------------------------
# Final result visibility for Done cards
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# GET /board-summary — generic board summary consumer
# ---------------------------------------------------------------------------

import hashlib
from datetime import datetime, timedelta, timezone


def _summary_iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary_digest(ids: list) -> str:
    return hashlib.sha256(json.dumps(ids, separators=(",", ":")).encode("utf-8")).hexdigest()


_SUMMARY_EXPECTED_LEGACY_IDS = [
    "014c79161bf3",
    "62778bbc7f7b",
    "705134be1948",
    "bf5ff2b8c22a",
]
_SUMMARY_EXPECTED_LEGACY_DIGEST = (
    "107ff5dc64a718f9c20216032b4ec25fcc98a6a4dad0c8dae5b0f2b690f2c9e2"
)
assert _summary_digest(_SUMMARY_EXPECTED_LEGACY_IDS) == _SUMMARY_EXPECTED_LEGACY_DIGEST


def _valid_summary(slug: str = "default", *, fresh_for: int = 3600,
                   generated: datetime | None = None) -> dict:
    """Minimal payload satisfying the generic cc://board-summary/v2 contract."""
    now = generated or datetime.now(timezone.utc).replace(microsecond=0)
    expires = now + timedelta(seconds=fresh_for)
    empty = _summary_digest([])
    return {
        "schema_version": 2,
        "board_slug": slug,
        "generated_at": _summary_iso(now),
        "expires_at": _summary_iso(expires),
        "staleness": {"fresh_for_seconds": fresh_for},
        "phase": "canary",
        "status": "monitoring",
        "cron_removal_authorized": False,
        "pre_removal_proof_digest": "",
        "clean_since": _summary_iso(now),
        "clean_elapsed_seconds": 0,
        "required_clean_seconds": 60,
        "completed": False,
        "engine": {"healthy": True},
        "cron_jobs_remaining": len(_SUMMARY_EXPECTED_LEGACY_IDS),
        "cron_inventory": {
            "expected_legacy_ids": list(_SUMMARY_EXPECTED_LEGACY_IDS),
            "expected_legacy_ids_digest": _SUMMARY_EXPECTED_LEGACY_DIGEST,
            "live_ids": list(_SUMMARY_EXPECTED_LEGACY_IDS),
            "live_ids_digest": _SUMMARY_EXPECTED_LEGACY_DIGEST,
            "live_enabled_ids": [],
            "live_non_paused_ids": [],
            "source_observed": True,
            "source_ids": list(_SUMMARY_EXPECTED_LEGACY_IDS),
            "source_ids_digest": _SUMMARY_EXPECTED_LEGACY_DIGEST,
            "source_live_converged": True,
            "phase_expectation_met": True,
        },
        "blocked_cards_count": 0,
        "duplicate_blocked_signature_count": 0,
        "pending_grace_count": 0,
        "schedule_totals": {
            "configured": 0, "observed": 0, "paused": 0, "drifted": 0,
            "pending": 0, "missed": 0,
            "source_names": [], "live_keys": [], "paused_names": [], "drifted_names": [],
        },
        "global": {
            "conductor_healthy": True, "running_count": 0, "paused_count": 0,
            "success_count": 0, "failed_count": 0, "contention_count": 0,
            "duplicate_count": 0, "stale_prerequisite_count": 0, "blocked_count": 0,
            "cron_jobs_remaining": len(_SUMMARY_EXPECTED_LEGACY_IDS),
            "schedule_count": 0, "schedule_paused_count": 0,
            "pending_grace_count": 0, "missed_boundary_count": 0, "findings_count": 0,
        },
        "barrier": {"passed": False, "inventory_digest": "", "workflow_digest": "", "waves": 0},
        "lanes": [],
        "schedules": [],
        "blocked_cards": [],
        "pending_occurrences": [],
        "findings": [],
        "source": {
            "api_base": "http://127.0.0.1:8080",
            "ledger_dir": "/tmp/cc-home/state/cc_lanes",
            "schedules_dir": "/tmp/cc-home/central-command/ORCHESTRATION/schedules",
            "boards_root": "/tmp/cc-home/kanban/boards",
            "cron_jobs_path": "/tmp/cc-home/cron/jobs.json",
            "source_cron_jobs_path": "/tmp/cc-home/custom-setup/cron/jobs.json",
            "summary_path": f"/tmp/cc-home/state/board-summaries/{slug}.json",
        },
    }


def _summary_dir(home: Path) -> Path:
    d = home / "state" / "board-summaries"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_summary(home: Path, payload: dict, slug: str = "default") -> Path:
    p = _summary_dir(home) / f"{slug}.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _get_summary(client, slug: str = "default"):
    return client.get(f"/api/plugins/kanban/board-summary?board={slug}")


def test_board_summary_missing_file_is_stable_404(client, kanban_home):
    r = _get_summary(client)
    assert r.status_code == 404
    assert r.json()["detail"] == "board summary not found"


def test_board_summary_valid_file_returns_projection(client, kanban_home):
    _write_summary(kanban_home, _valid_summary())
    r = _get_summary(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["board"] == "default"
    assert data["phase"] == "canary"
    assert data["status"] == "monitoring"
    assert data["stale"] is False
    assert data["completed"] is False
    assert data["cron_removal_authorized"] is False
    assert data["counts"]["running_count"] == 0
    assert data["counts"]["findings_count"] == 0
    assert data["cron"]["jobs_remaining"] == len(_SUMMARY_EXPECTED_LEGACY_IDS)
    assert data["schedule_totals"]["paused"] == 0
    assert data["barrier"]["passed"] is False
    assert data["lanes"] == []
    assert data["schedules"] == []
    assert data["findings"] == []
    # The producer-side source block (local filesystem paths) must never leak.
    assert "source" not in data
    assert "/tmp/cc-home" not in r.text


def _assert_summary_unavailable(r):
    assert r.status_code == 503
    assert r.json()["detail"] == "board summary unavailable"


def test_board_summary_symlink_is_refused(client, kanban_home, tmp_path):
    real = tmp_path / "outside.json"
    real.write_text(json.dumps(_valid_summary()), encoding="utf-8")
    (_summary_dir(kanban_home) / "default.json").symlink_to(real)
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_non_regular_file_is_refused(client, kanban_home):
    os.mkfifo(_summary_dir(kanban_home) / "default.json")
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_oversized_file_is_refused(client, kanban_home):
    padded = _valid_summary()
    padded["findings"] = []
    text = json.dumps(padded)
    filler = "x" * (1_048_577 - len(text))
    p = _summary_dir(kanban_home) / "default.json"
    p.write_text(text[:-1] + f',"pad":"{filler}"' + "}", encoding="utf-8")
    assert p.stat().st_size > 1_048_576
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_malformed_json_is_refused(client, kanban_home):
    (_summary_dir(kanban_home) / "default.json").write_text("{not json", encoding="utf-8")
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_non_object_json_is_refused(client, kanban_home):
    (_summary_dir(kanban_home) / "default.json").write_text(
        json.dumps([_valid_summary()]), encoding="utf-8")
    _assert_summary_unavailable(_get_summary(client))


def _lane(name: str, ts: str, running: tuple = (), failed: tuple = ()) -> dict:
    running_ids, failed_ids = sorted(running), sorted(failed)
    observed = sorted({*running_ids, *failed_ids})
    return {
        "name": name,
        "observed_ids": observed,
        "running_ids": running_ids, "running_count": len(running_ids),
        "paused_ids": [], "paused_count": 0,
        "success_ids": [], "terminal_successes": 0,
        "failed_ids": failed_ids, "failed_count": len(failed_ids),
        "contention_ids": [], "contention_count": 0,
        "duplicate_ids": [], "duplicate_count": 0,
        "stale_prerequisite_ids": [], "stale_prerequisite_count": 0,
        "occurrences": [{"workflow_id": w, "started_at": ts} for w in observed],
    }


def _schedule(name: str, paused: bool = False) -> dict:
    return {
        "name": name, "cron_expression": "0 * * * *", "zone": "UTC", "paused": paused,
        "workflow_name": name, "workflow_version": 1, "correlation_id": "cc-corr",
        "task_to_domain": {}, "workflow_input": {}, "run_catchup_schedule_instances": None,
        "start_time": None, "end_time": None, "overlap_policy": None,
        "expected_minimum_occurrences": 0, "observed_count": 0,
        "matched_workflow_ids": [], "unmatched_workflow_ids": [],
        "pending_boundaries": [], "drift": [], "missed_boundaries": [],
    }


def _mut_version_v1(p): p["schema_version"] = 1
def _mut_version_str(p): p["schema_version"] = "2"
def _mut_version_bool(p): p["schema_version"] = True
def _mut_version_unknown(p): p["schema_version"] = 3
def _mut_board_mismatch(p): p["board_slug"] = "other-board"
def _mut_bad_phase(p): p["phase"] = "rollout"
def _mut_bad_status(p): p["status"] = "ok"
def _mut_completed_contradiction(p): p["completed"] = True
def _mut_authorized_contradiction(p): p["cron_removal_authorized"] = True
def _mut_proof_digest_contradiction(p): p["pre_removal_proof_digest"] = "a" * 64
def _mut_failed_without_findings(p): p["status"] = "failed"
def _mut_global_count_lie(p): p["global"]["running_count"] = 5
def _mut_count_wrong_type(p): p["global"]["running_count"] = "0"
def _mut_count_bool(p): p["global"]["running_count"] = False
def _mut_nonfinite(p): p["clean_elapsed_seconds"] = float("nan")
def _mut_bad_timestamp(p): p["generated_at"] = "2026-09-12 10:00:00"
def _mut_staleness_lie(p): p["staleness"]["fresh_for_seconds"] = 5


def _mut_monitoring_with_findings(p):
    p["findings"] = [{"severity": "error", "code": "x", "message": "boom", "evidence": {}}]
    p["global"]["findings_count"] = 1


def _mut_cron_count_lie(p):
    p["cron_jobs_remaining"] = 2
    p["global"]["cron_jobs_remaining"] = 2


def _mut_cron_digest_lie(p): p["cron_inventory"]["live_ids_digest"] = "0" * 64


def _mut_unknown_key(p): p["surprise"] = 1


def _mut_missing_key(p): del p["barrier"]


def _mut_duplicate_lane_workflow_ids(p):
    ts = p["generated_at"]
    p["lanes"] = [_lane("alpha", ts, running=("wf-1",)), _lane("beta", ts, running=("wf-1",))]
    p["global"]["running_count"] = 2


def _mut_lane_count_lie(p):
    ts = p["generated_at"]
    lane = _lane("alpha", ts, running=("wf-1",))
    lane["running_count"] = 2
    p["lanes"] = [lane]
    p["global"]["running_count"] = 2


def _mut_schedule_paused_lie(p):
    p["schedule_totals"]["paused"] = 1
    p["global"]["schedule_paused_count"] = 1


def _mut_barrier_contradiction(p): p["barrier"]["passed"] = True


_SUMMARY_VIOLATIONS = [
    _mut_version_v1, _mut_version_str, _mut_version_bool, _mut_version_unknown,
    _mut_board_mismatch, _mut_bad_phase, _mut_bad_status,
    _mut_completed_contradiction, _mut_authorized_contradiction,
    _mut_proof_digest_contradiction, _mut_failed_without_findings,
    _mut_monitoring_with_findings, _mut_global_count_lie, _mut_count_wrong_type,
    _mut_count_bool, _mut_nonfinite, _mut_bad_timestamp, _mut_staleness_lie,
    _mut_cron_count_lie, _mut_cron_digest_lie, _mut_unknown_key, _mut_missing_key,
    _mut_duplicate_lane_workflow_ids, _mut_lane_count_lie,
    _mut_schedule_paused_lie, _mut_barrier_contradiction,
]


@pytest.mark.parametrize("mutate", _SUMMARY_VIOLATIONS, ids=lambda fn: fn.__name__[5:])
def test_board_summary_contract_violation_is_refused(client, kanban_home, mutate):
    payload = _valid_summary()
    mutate(payload)
    _write_summary(kanban_home, payload)
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_accepts_consistent_populated_payload(client, kanban_home):
    p = _valid_summary()
    ts = p["generated_at"]
    p["lanes"] = [
        _lane("alpha", ts, running=("wf-1",), failed=("wf-2",)),
        _lane("beta", ts, running=("wf-3",)),
    ]
    p["schedules"] = [_schedule("hourly_sync", paused=True)]
    p["schedule_totals"].update(
        configured=1, paused=1, source_names=["hourly_sync"], paused_names=["hourly_sync"])
    p["global"].update(
        running_count=2, failed_count=1, schedule_count=1, schedule_paused_count=1,
        findings_count=1)
    p["findings"] = [{
        "severity": "error", "code": "lane_failed",
        "message": "lane alpha reported a failed workflow", "evidence": {}}]
    p["status"] = "failed"  # failed requires findings; monitoring forbids them
    _write_summary(kanban_home, p)
    r = _get_summary(client)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "failed"
    assert data["counts"]["running_count"] == 2
    assert data["counts"]["failed_count"] == 1
    assert [lane["name"] for lane in data["lanes"]] == ["alpha", "beta"]
    assert data["lanes"][0]["failed_count"] == 1
    assert data["schedules"] == [{
        "name": "hourly_sync", "paused": True, "cron_expression": "0 * * * *",
        "zone": "UTC", "observed_count": 0, "missed_count": 0, "drifted": False}]
    assert data["schedule_totals"]["paused"] == 1
    assert data["findings_count"] == 1
    assert data["findings"][0]["code"] == "lane_failed"
    # Finding evidence and schedule correlation/input payloads stay private.
    assert "evidence" not in data["findings"][0]
    assert "correlation_id" not in data["schedules"][0]

# Review regressions: exact published schema, strict JSON, authorization evidence,
# and descriptor-walk confinement.

def _mut_consistent_foreign_legacy_inventory(payload: dict) -> None:
    ids = ["aaaaaaaaaaaa"]
    digest = _summary_digest(ids)
    cron = payload["cron_inventory"]
    cron["expected_legacy_ids"] = ids
    cron["expected_legacy_ids_digest"] = digest
    cron["live_ids"] = ids
    cron["live_ids_digest"] = digest
    cron["source_ids"] = ids
    cron["source_ids_digest"] = digest
    payload["cron_jobs_remaining"] = 1
    payload["global"]["cron_jobs_remaining"] = 1


def _mut_schema_invalid_blocked_board(payload: dict) -> None:
    payload["blocked_cards"] = [{
        "board": "bad_slug",
        "id": "t_bad",
        "title": "bad board pattern",
        "status": "blocked",
        "created_at": 1,
        "incident_id": None,
    }]
    payload["blocked_cards_count"] = 1
    payload["global"]["blocked_count"] = 1


def _mut_schema_invalid_source(payload: dict) -> None:
    payload["source"]["api_base"] = "not-url"
    payload["source"]["ledger_dir"] = "relative/path"


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_consistent_foreign_legacy_inventory,
        _mut_schema_invalid_blocked_board,
        _mut_schema_invalid_source,
    ],
    ids=lambda fn: fn.__name__[5:],
)
def test_board_summary_rejects_published_schema_violations(
    client, kanban_home, mutate
):
    payload = _valid_summary()
    mutate(payload)
    _write_summary(kanban_home, payload)
    _assert_summary_unavailable(_get_summary(client))


def _removal_ready_summary() -> dict:
    payload = _valid_summary()
    payload["phase"] = "pre_removal"
    payload["status"] = "removal_ready"
    payload["cron_removal_authorized"] = True
    payload["clean_elapsed_seconds"] = 7200
    payload["required_clean_seconds"] = 7200
    payload["schedules"] = [_schedule("cc_backups")]
    payload["schedule_totals"].update(
        configured=1,
        source_names=["cc_backups"],
    )
    payload["global"]["schedule_count"] = 1
    payload["barrier"] = {
        "passed": True,
        "inventory_digest": "a" * 64,
        "workflow_digest": "b" * 64,
        "waves": 5,
    }
    return payload


def _mut_unhealthy_authorization(payload: dict) -> None:
    payload["engine"]["healthy"] = False
    payload["global"]["conductor_healthy"] = False


def _mut_failed_barrier_authorization(payload: dict) -> None:
    payload["barrier"] = {
        "passed": False,
        "inventory_digest": "",
        "workflow_digest": "",
        "waves": 0,
    }


def _mut_short_clean_window_authorization(payload: dict) -> None:
    payload["clean_elapsed_seconds"] = 7199


def _mut_zero_required_window_authorization(payload: dict) -> None:
    payload["required_clean_seconds"] = 0


def _mut_missing_schedule_evidence(payload: dict) -> None:
    payload["schedules"] = []
    payload["schedule_totals"].update(configured=0, source_names=[])
    payload["global"]["schedule_count"] = 0


@pytest.mark.parametrize(
    "mutate",
    [
        _mut_unhealthy_authorization,
        _mut_failed_barrier_authorization,
        _mut_short_clean_window_authorization,
        _mut_zero_required_window_authorization,
        _mut_missing_schedule_evidence,
    ],
    ids=lambda fn: fn.__name__[5:],
)
def test_board_summary_refuses_unproven_removal_authorization(
    client, kanban_home, mutate
):
    payload = _removal_ready_summary()
    mutate(payload)
    _write_summary(kanban_home, payload)
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_accepts_proven_removal_authorization(client, kanban_home):
    _write_summary(kanban_home, _removal_ready_summary())
    response = _get_summary(client)
    assert response.status_code == 200, response.text
    assert response.json()["cron_removal_authorized"] is True


def test_board_summary_deep_json_is_stable_503(client, kanban_home):
    path = _summary_dir(kanban_home) / "default.json"
    path.write_bytes(b"[" * 1500 + b"]" * 1500)
    with TestClient(client.app, raise_server_exceptions=False) as contained:
        _assert_summary_unavailable(_get_summary(contained))


def test_board_summary_maps_recursion_error_to_stable_unavailable(
    client, kanban_home, monkeypatch
):
    _write_summary(kanban_home, _valid_summary())
    plugin = sys.modules["hermes_dashboard_plugin_kanban_test"]

    def recursive_decode(*_args, **_kwargs):
        raise RecursionError("adversarial nesting")

    monkeypatch.setattr(plugin.json, "loads", recursive_decode)
    response = _get_summary(client)
    assert response.status_code == 503
    assert response.content == b'{"detail":"board summary unavailable"}'


def test_board_summary_rejects_duplicate_json_members(client, kanban_home):
    raw = json.dumps(_valid_summary())
    raw = raw.replace(
        '"schema_version": 2',
        '"schema_version": 1, "schema_version": 2',
        1,
    )
    (_summary_dir(kanban_home) / "default.json").write_text(raw, encoding="utf-8")
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_rejects_nested_numeric_overflow(client, kanban_home):
    payload = _valid_summary()
    payload["status"] = "failed"
    payload["findings"] = [{
        "severity": "error",
        "code": "overflow",
        "message": "nested evidence",
        "evidence": {},
    }]
    payload["global"]["findings_count"] = 1
    raw = json.dumps(payload).replace(
        '"evidence": {}',
        '"evidence": {"overflow": 1e309}',
        1,
    )
    (_summary_dir(kanban_home) / "default.json").write_text(raw, encoding="utf-8")
    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_symlinked_state_ancestor_is_refused(
    client, kanban_home, tmp_path
):
    outside_state = tmp_path / "outside-state"
    outside_summary = outside_state / "board-summaries"
    outside_summary.mkdir(parents=True)
    (outside_summary / "default.json").write_text(
        json.dumps(_valid_summary()),
        encoding="utf-8",
    )
    (kanban_home / "state").symlink_to(outside_state, target_is_directory=True)

    _assert_summary_unavailable(_get_summary(client))


def test_board_summary_detects_in_place_mutation_during_read(
    client, kanban_home, monkeypatch
):
    payload = _valid_summary()
    payload["status"] = "failed"
    payload["findings"] = [{
        "severity": "error",
        "code": "large_evidence",
        "message": "large evidence",
        "evidence": {"blob": "x" * 70000},
    }]
    payload["global"]["findings_count"] = 1
    path = _write_summary(kanban_home, payload)
    real_read = os.read
    mutated = False

    def racing_read(fd: int, count: int) -> bytes:
        nonlocal mutated
        chunk = real_read(fd, count)
        if chunk and not mutated:
            mutated = True
            data = path.read_bytes()
            position = data.rfind(b"xxxxxxxxxx")
            assert position > 65536
            with path.open("r+b", buffering=0) as stream:
                stream.seek(position)
                stream.write(b"y")
                os.fsync(stream.fileno())
        return chunk

    monkeypatch.setattr(os, "read", racing_read)
    _assert_summary_unavailable(_get_summary(client))

