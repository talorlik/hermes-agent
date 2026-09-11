"""Tests for fork upstream sync candidate validation and rollback behavior."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _patch_managed_uv():
    """Make managed uv resolution follow the tests' ``shutil.which`` mocks."""
    import shutil

    with patch(
        "hermes_cli.managed_uv.ensure_uv", side_effect=lambda **_kwargs: shutil.which("uv")
    ):
        yield


def test_fork_sync_gate_runs_only_dedicated_candidate_tests() -> None:
    calls: list[list[str]] = []

    def side_effect(cmd, **kwargs):
        calls.append([str(part) for part in cmd])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=side_effect):
        passed, detail = update_cmd._run_fork_sync_tests(Path("/repo"))

    assert passed is True
    assert detail == ""
    pytest_call = next(call for call in calls if call[1:3] == ["-m", "pytest"])
    assert "tests/hermes_cli/test_fork_sync_strategy.py" in pytest_call
    assert "tests/hermes_cli/test_update_post_pull_syntax_guard.py" in pytest_call
    assert "tests/hermes_cli/test_cmd_update.py" not in pytest_call


class TestForkSyncStrategy:
    """``updates.fork_sync_strategy`` governs the fork upstream sync when the
    fork carries its own commits on top of upstream.

    "ff_only" (default) preserves the historical skip-with-notice behavior;
    "merge" merges upstream/main into main (local commits preserved, conflict
    aborts cleanly with nothing changed) and pushes the result to origin.
    """

    @staticmethod
    def _make_sync_side_effect(
        origin_ahead,
        upstream_ahead,
        merge_rc=0,
        test_rc=0,
        head_sha: str | None = "feedbead1234567890",
        calls=None,
    ):
        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if calls is not None:
                calls.append(joined)
            if "remote" in joined and "get-url" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="git@github.com:example/hermes-agent.git\n", stderr=""
                )
            if "rev-parse HEAD" in joined:
                if head_sha is None:
                    return subprocess.CompletedProcess(
                        cmd, 1, stdout="", stderr="cannot resolve HEAD"
                    )
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{head_sha}\n", stderr=""
                )
            if "rev-list" in joined and "upstream/main..origin/main" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{origin_ahead}\n", stderr=""
                )
            if "rev-list" in joined and "origin/main..upstream/main" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout=f"{upstream_ahead}\n", stderr=""
                )
            if "merge" in joined and "--no-edit" in joined:
                return subprocess.CompletedProcess(cmd, merge_rc, stdout="", stderr="")
            if "-c import pytest, pytest_asyncio" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if " -m pytest " in f" {joined} ":
                return subprocess.CompletedProcess(
                    cmd,
                    test_rc,
                    stdout="targeted updater tests\n",
                    stderr="test failure\n" if test_rc else "",
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        return side_effect

    def _run_sync(
        self,
        *,
        strategy,
        origin_ahead,
        upstream_ahead,
        merge_rc=0,
        test_rc=0,
        head_sha: str | None = "feedbead1234567890",
        syntax=(True, None, None),
    ):
        from pathlib import Path

        from hermes_cli import update_cmd

        calls = []
        config = {"updates": {"fork_sync_strategy": strategy}} if strategy else {}
        with patch(
            "subprocess.run",
            side_effect=self._make_sync_side_effect(
                origin_ahead,
                upstream_ahead,
                merge_rc=merge_rc,
                test_rc=test_rc,
                head_sha=head_sha,
                calls=calls,
            ),
        ), patch("hermes_cli.config.load_config", return_value=config), patch(
            "hermes_cli.update_cmd._validate_critical_files_syntax",
            return_value=syntax,
        ):
            update_cmd._sync_with_upstream_if_needed(["git"], Path("/repo"))
        return calls

    def test_merge_strategy_merges_and_pushes_fork(self, capsys):
        calls = self._run_sync(strategy="merge", origin_ahead=2, upstream_ahead=5)

        assert any("merge --no-edit upstream/main" in c for c in calls)
        assert any("push origin main --force-with-lease" in c for c in calls)
        # Recovery anchor tag is laid down before the merge.
        tag_idx = next(i for i, c in enumerate(calls) if "tag pre-upstream-sync-" in c)
        merge_idx = next(
            i for i, c in enumerate(calls) if "merge --no-edit upstream/main" in c
        )
        test_idx = next(i for i, c in enumerate(calls) if " -m pytest " in f" {c} ")
        push_idx = next(i for i, c in enumerate(calls) if "push origin main" in c)
        assert tag_idx < merge_idx
        assert merge_idx < test_idx < push_idx
        test_command = calls[test_idx]
        assert "tests/hermes_cli/test_fork_sync_strategy.py" in test_command
        assert "tests/hermes_cli/test_update_post_pull_syntax_guard.py" in test_command
        out = capsys.readouterr().out
        assert "Merged upstream/main" in out
        assert "Fork synced with upstream" in out

    def test_merge_strategy_conflict_aborts_and_never_pushes(self, capsys):
        calls = self._run_sync(
            strategy="merge", origin_ahead=2, upstream_ahead=5, merge_rc=1
        )

        assert any("merge --abort" in c for c in calls)
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "sync stopped, nothing was changed" in out

    def test_merge_strategy_syntax_failure_rolls_back_and_never_pushes(self, capsys):
        """The post-merge syntax guard resets to the pre-merge SHA instead of
        pushing a merged tree whose critical files no longer parse."""
        calls = self._run_sync(
            strategy="merge",
            origin_ahead=2,
            upstream_ahead=5,
            syntax=(False, "/repo/cli.py", "SyntaxError: invalid syntax"),
        )

        assert any("merge --no-edit upstream/main" in c for c in calls)
        assert any("reset --hard feedbead1234567890" in c for c in calls)
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "syntax error in a critical file" in out
        assert "Rolled back to feedbead12" in out
        assert "nothing was pushed" in out

    def test_merge_strategy_test_failure_rolls_back_and_never_pushes(self, capsys):
        calls = self._run_sync(
            strategy="merge",
            origin_ahead=2,
            upstream_ahead=5,
            test_rc=1,
        )

        assert any(" -m pytest " in f" {c} " for c in calls)
        assert any("reset --hard feedbead1234567890" in c for c in calls)
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "targeted updater tests failed" in out
        assert "nothing was pushed" in out

    def test_merge_strategy_missing_pre_sync_head_never_merges_or_pushes(self, capsys):
        calls = self._run_sync(
            strategy="merge",
            origin_ahead=2,
            upstream_ahead=5,
            head_sha=None,
        )

        assert not any("merge --no-edit" in c for c in calls)
        assert not any("push" in c for c in calls)
        assert "Could not capture the pre-sync HEAD" in capsys.readouterr().out

    def test_default_ff_only_preserves_skip_notice(self, capsys):
        calls = self._run_sync(strategy=None, origin_ahead=2, upstream_ahead=5)

        assert not any("merge --no-edit" in c for c in calls)
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "Skipping upstream sync to preserve your changes" in out
        assert "fork_sync_strategy: merge" in out

    def test_unknown_strategy_falls_back_to_ff_only(self, capsys):
        calls = self._run_sync(strategy="yolo", origin_ahead=1, upstream_ahead=3)

        assert not any("merge --no-edit" in c for c in calls)
        assert not any("push" in c for c in calls)
        assert "Skipping upstream sync" in capsys.readouterr().out

    def test_merge_strategy_noop_when_upstream_not_ahead(self, capsys):
        calls = self._run_sync(strategy="merge", origin_ahead=2, upstream_ahead=0)

        assert not any("merge --no-edit" in c for c in calls)
        assert not any("push" in c for c in calls)
        assert "Fork is up to date with upstream" in capsys.readouterr().out

    def test_ff_only_fast_forward_path_unchanged(self, capsys):
        """Strictly-behind forks still fast-forward and push, regardless of
        strategy (invariant: the merge option must not regress the ff path)."""
        calls = self._run_sync(strategy=None, origin_ahead=0, upstream_ahead=4)

        assert any("pull --ff-only upstream main" in c for c in calls)
        assert any("push origin main --force-with-lease" in c for c in calls)
        test_idx = next(i for i, c in enumerate(calls) if " -m pytest " in f" {c} ")
        push_idx = next(i for i, c in enumerate(calls) if "push origin main" in c)
        assert test_idx < push_idx
        test_command = calls[test_idx]
        assert "tests/hermes_cli/test_fork_sync_strategy.py" in test_command
        assert "tests/hermes_cli/test_update_post_pull_syntax_guard.py" in test_command
        assert "Fork synced with upstream" in capsys.readouterr().out

    def test_ff_only_test_failure_rolls_back_and_never_pushes(self, capsys):
        calls = self._run_sync(
            strategy=None,
            origin_ahead=0,
            upstream_ahead=4,
            test_rc=1,
        )

        assert any("pull --ff-only upstream main" in c for c in calls)
        assert any(" -m pytest " in f" {c} " for c in calls)
        assert any("reset --hard feedbead1234567890" in c for c in calls)
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "targeted updater tests failed" in out
        assert "nothing was pushed" in out

    def test_ff_only_missing_pre_sync_head_never_pulls_or_pushes(self, capsys):
        calls = self._run_sync(
            strategy=None,
            origin_ahead=0,
            upstream_ahead=4,
            head_sha=None,
        )

        assert not any("pull --ff-only" in c for c in calls)
        assert not any("push" in c for c in calls)
        assert "Could not capture the pre-sync HEAD" in capsys.readouterr().out

    def test_test_runner_bootstraps_pytest_with_uv_when_missing(self):
        import sys
        from pathlib import Path

        from hermes_cli import update_cmd

        calls = []

        def side_effect(cmd, **kwargs):
            calls.append([str(part) for part in cmd])
            joined = " ".join(str(part) for part in cmd)
            if "-c import pytest, pytest_asyncio" in joined:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")
            return subprocess.CompletedProcess(cmd, 0, stdout="passed\n", stderr="")

        with patch("subprocess.run", side_effect=side_effect), patch(
            "shutil.which", return_value="/portable/uv"
        ):
            passed, detail = update_cmd._run_fork_sync_tests(Path("/repo"))

        assert passed is True
        assert detail == ""
        assert [
            "/portable/uv",
            "pip",
            "install",
            "--python",
            sys.executable,
            "pytest==9.1.1",
            "pytest-asyncio==1.3.0",
        ] in calls
        pytest_call = next(call for call in calls if call[1:3] == ["-m", "pytest"])
        assert pytest_call[0] == sys.executable
        assert "tests/hermes_cli/test_fork_sync_strategy.py" in pytest_call
        assert "tests/hermes_cli/test_update_post_pull_syntax_guard.py" in pytest_call

    def test_test_runner_dependency_bootstrap_failure_stops_before_tests(self):
        from pathlib import Path

        from hermes_cli import update_cmd

        calls = []

        def side_effect(cmd, **kwargs):
            calls.append([str(part) for part in cmd])
            joined = " ".join(str(part) for part in cmd)
            if "-c import pytest, pytest_asyncio" in joined:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="missing")
            return subprocess.CompletedProcess(
                cmd, 1, stdout="install stdout\n", stderr="install stderr\n"
            )

        with patch("subprocess.run", side_effect=side_effect), patch(
            "shutil.which", return_value="/portable/uv"
        ):
            passed, detail = update_cmd._run_fork_sync_tests(Path("/repo"))

        assert passed is False
        assert "install stdout" in detail
        assert "install stderr" in detail
        assert not any(call[1:3] == ["-m", "pytest"] for call in calls)

    def test_failed_pytest_detail_preserves_bounded_stdout_and_stderr(self):
        from pathlib import Path

        from hermes_cli import update_cmd

        stderr = "\n".join(f"stderr-{index}" for index in range(10))

        def side_effect(cmd, **kwargs):
            joined = " ".join(str(part) for part in cmd)
            if "-c import pytest, pytest_asyncio" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return subprocess.CompletedProcess(
                cmd,
                1,
                stdout="stdout-marker\n",
                stderr=stderr,
            )

        with patch("subprocess.run", side_effect=side_effect):
            passed, detail = update_cmd._run_fork_sync_tests(Path("/repo"))

        assert passed is False
        assert "stdout-marker" in detail
        assert "stderr-9" in detail
        assert "stderr-0" not in detail
