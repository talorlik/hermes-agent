"""Tests for fork upstream sync candidate validation and rollback behavior."""

import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import pytest

from hermes_cli import update_cmd


def _git_observation(
    state: str,
    returncode: int | None,
    *,
    stdout: str = "",
    stderr: str = "",
    exception: str = "",
) -> update_cmd.GitCommandObservation:
    return update_cmd.GitCommandObservation(
        state=state,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        exception=exception,
    )


@pytest.fixture(autouse=True)
def _patch_managed_uv():
    """Make managed uv resolution follow the tests' ``shutil.which`` mocks."""
    import shutil

    with patch(
        "hermes_cli.managed_uv.ensure_uv",
        side_effect=lambda **_kwargs: shutil.which("uv"),
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


def test_validated_noop_returns_immutable_structured_outcome() -> None:
    sha = "a" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 0]),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.phase == "prepare"
    assert outcome.status == "noop"
    assert outcome.pre_sha == sha
    assert outcome.post_sha == sha
    assert outcome.clean is True
    assert outcome.operation_state == "none"
    assert outcome.recovery_ref == sha
    assert outcome.error == ""
    assert outcome.safe_to_continue is True
    with pytest.raises(FrozenInstanceError):
        outcome.status = "failed"


def test_missing_pre_sync_sha_fails_before_git_mutation() -> None:
    sha = "a" * 40

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[None, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(update_cmd, "_has_upstream_remote") as has_upstream,
        patch("subprocess.run", side_effect=AssertionError("Git mutation reached")),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.status == "failed"
    assert outcome.pre_sha is None
    assert outcome.post_sha == sha
    assert "missing or invalid pre-sync HEAD SHA" in outcome.error
    assert outcome.safe_to_continue is False
    assert outcome.safe_to_restore is False
    has_upstream.assert_not_called()


def test_upstream_remote_add_failure_returns_failed_outcome() -> None:
    sha = "a" * 40

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("absent", 2, stderr="No such remote"),
        ),
        patch.object(update_cmd, "_should_skip_upstream_prompt", return_value=False),
        patch.object(
            update_cmd,
            "_add_upstream_remote_observed",
            return_value=_git_observation("failed", 3, stderr="permission denied"),
        ),
        patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"],
            Path("/repo"),
            phase="prepare",
            input_fn=lambda _prompt, _default: "yes",
        )

    assert outcome.status == "failed"
    assert outcome.pre_sha == outcome.post_sha == sha
    assert "upstream remote creation failed" in outcome.error
    assert "returncode=3" in outcome.error
    assert "stderr=permission denied" in outcome.error
    assert outcome.safe_to_restore is True
    assert outcome.safe_to_continue is False


def test_recovery_tag_spawn_error_returns_failed_outcome() -> None:
    sha = "a" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            raise OSError("git spawn failed")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.status == "failed"
    assert outcome.pre_sha == outcome.post_sha == sha
    assert "recovery tag creation failed: git spawn failed" in outcome.error
    assert outcome.safe_to_restore is True
    assert outcome.safe_to_continue is False


def test_missing_post_sync_sha_fails_closed() -> None:
    sha = "a" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, None]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 0]),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.status == "failed"
    assert outcome.pre_sha == sha
    assert outcome.post_sha is None
    assert "missing or invalid post-sync HEAD SHA" in outcome.error
    assert outcome.safe_to_continue is False
    assert outcome.safe_to_restore is False


def test_same_head_with_dirty_index_fails_before_git_mutation() -> None:
    sha = "a" * 40

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd,
            "_capture_checkout_proof",
            return_value=(False, "none", ""),
        ),
        patch.object(update_cmd, "_has_upstream_remote") as has_upstream,
        patch("subprocess.run", side_effect=AssertionError("Git mutation reached")),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.pre_sha == outcome.post_sha == sha
    assert outcome.clean is False
    assert outcome.status == "failed"
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False
    has_upstream.assert_not_called()


def test_same_head_with_merge_state_fails_before_git_mutation() -> None:
    sha = "a" * 40

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd,
            "_capture_checkout_proof",
            return_value=(True, "merge", ""),
        ),
        patch.object(update_cmd, "_has_upstream_remote") as has_upstream,
        patch("subprocess.run", side_effect=AssertionError("Git mutation reached")),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.pre_sha == outcome.post_sha == sha
    assert outcome.clean is True
    assert outcome.operation_state == "merge"
    assert outcome.status == "failed"
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False
    has_upstream.assert_not_called()


def test_merge_spawn_error_rolls_back_and_returns_failed_outcome() -> None:
    sha = "a" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["merge", "--no-edit", "upstream/main"]:
            raise OSError("merge spawn failed")
        if cmd[-3:] == ["reset", "--hard", sha]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[1, 1]),
        patch("hermes_cli.update_cmd_git._fork_sync_strategy", return_value="merge"),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="post_origin_pull"
        )

    assert outcome.status == "failed"
    assert "upstream merge failed: merge spawn failed" in outcome.error
    assert outcome.safe_to_restore is True
    assert outcome.safe_to_continue is False


def test_candidate_validation_exception_rolls_back_and_returns_failed_outcome() -> None:
    sha = "a" * 40
    commands: list[list[str]] = []

    def strict_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["reset", "--hard", sha]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            side_effect=RuntimeError("validator crashed"),
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert sum(command[-3:] == ["reset", "--hard", sha] for command in commands) == 1
    assert outcome.status == "failed"
    assert "candidate validation raised: validator crashed" in outcome.error
    assert outcome.safe_to_restore is True
    assert outcome.safe_to_continue is False


def test_rollback_spawn_error_returns_unsafe_outcome() -> None:
    sha = "a" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["reset", "--hard", sha]:
            raise OSError("reset spawn failed")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            return_value=False,
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.status == "rollback_failed"
    assert "rollback reset failed" in outcome.error
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False


def test_rollback_reset_failure_is_unsafe_even_when_head_observes_unchanged() -> None:
    sha = "a" * 40
    commands: list[list[str]] = []

    def strict_run(cmd, **_kwargs):
        commands.append(cmd)
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["reset", "--hard", sha]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="reset denied")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            return_value=False,
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert sum(command[-3:] == ["reset", "--hard", sha] for command in commands) == 1
    assert outcome.status == "rollback_failed"
    assert "rollback reset failed" in outcome.error
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False


def test_remote_probe_failure_is_not_reported_as_absence_or_not_checked() -> None:
    sha = "a" * 40
    probe = update_cmd.GitCommandObservation(
        state="failed",
        returncode=128,
        stdout="",
        stderr="fatal: cannot read config",
        exception="",
    )
    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[sha, sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(update_cmd, "_observe_upstream_remote", return_value=probe),
        patch.object(update_cmd, "_offer_upstream_remote_observed") as offer,
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="prepare"
        )

    assert outcome.status == "failed"
    assert "returncode=128" in outcome.error
    assert "stderr=fatal: cannot read config" in outcome.error
    assert outcome.status != "not_checked"
    offer.assert_not_called()


@pytest.mark.parametrize(
    ("marker", "expected_state"),
    [
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
        ("BISECT_START", "bisect-start"),
        ("REBASE_HEAD", "rebase-head"),
        ("sequencer", "sequencer"),
    ],
)
def test_checkout_proof_detects_additional_active_git_operations(
    tmp_path, marker: str, expected_state: str
) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    target = git_dir / marker
    if marker == "sequencer":
        target.mkdir()
    else:
        target.write_text("active", encoding="utf-8")

    def strict_git_run(_git_cmd, args, _cwd):
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["rev-parse", "--absolute-git-dir"]:
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{git_dir}\n", stderr=""
            )
        raise AssertionError(f"unexpected Git command: {args}")

    with patch.object(update_cmd, "_git_run", side_effect=strict_git_run):
        clean, operation_state, error = update_cmd._capture_checkout_proof(
            ["git"], tmp_path
        )

    assert clean is True
    assert operation_state == expected_state
    assert error == ""


def test_checkout_proof_does_not_trust_dot_git_metadata_after_git_probe_failure(
    tmp_path,
) -> None:
    foreign_git_dir = tmp_path / "foreign.git"
    foreign_git_dir.mkdir()
    (foreign_git_dir / "MERGE_HEAD").write_text("active\n", encoding="utf-8")
    (tmp_path / ".git").write_text(
        f"gitdir: {foreign_git_dir}\n", encoding="utf-8"
    )

    def strict_git_run(_git_cmd, args, _cwd):
        if args == ["status", "--porcelain=v1", "--untracked-files=all"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args == ["rev-parse", "--absolute-git-dir"]:
            return subprocess.CompletedProcess(
                args,
                128,
                stdout="",
                stderr="fatal: authoritative Git directory unavailable\n",
            )
        raise AssertionError(f"unexpected Git command: {args}")

    with patch.object(update_cmd, "_git_run", side_effect=strict_git_run):
        clean, operation_state, error = update_cmd._capture_checkout_proof(
            ["git"], tmp_path
        )

    assert clean is True
    assert operation_state == "unknown"
    assert "authoritative Git directory unavailable" in error


def test_validate_fork_sync_candidate_direct_failure_rolls_back_once(tmp_path) -> None:
    from hermes_cli import update_cmd_git

    rollback_ref = "a" * 40
    with (
        patch.object(
            update_cmd,
            "_validate_critical_files_syntax",
            return_value=(False, tmp_path / "broken.py", "invalid syntax"),
        ),
        patch.object(
            update_cmd_git, "_rollback_fork_sync_candidate", return_value=True
        ) as rollback,
    ):
        assert (
            update_cmd._validate_fork_sync_candidate(
                ["git"], tmp_path, rollback_ref
            )
            is False
        )

    rollback.assert_called_once_with(["git"], tmp_path, rollback_ref)


def test_push_success_requires_exact_origin_main_readback() -> None:
    pre_sha = "a" * 40
    post_sha = "b" * 40
    remote_sha = "c" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["ls-remote", "--exit-code", "origin", "refs/heads/main"]:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=f"{remote_sha}\trefs/heads/main\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[pre_sha, post_sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            return_value=True,
        ),
        patch.object(
            update_cmd,
            "_push_fork_with_upstream_observed",
            return_value=_git_observation("succeeded", 0),
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="post_origin_pull"
        )

    assert outcome.status == "failed"
    assert outcome.local_integration_completed is True
    assert "origin/main postcondition mismatch" in outcome.error
    assert post_sha in outcome.error
    assert remote_sha in outcome.error
    assert outcome.safe_to_continue is False


def test_push_failure_preserves_returncode_and_stderr_in_outcome() -> None:
    pre_sha = "a" * 40
    post_sha = "b" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[pre_sha, post_sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            return_value=True,
        ),
        patch.object(
            update_cmd,
            "_push_fork_with_upstream_observed",
            return_value=_git_observation("failed", 7, stderr="remote rejected lease"),
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="post_origin_pull"
        )

    assert outcome.status == "failed"
    assert outcome.pre_sha == pre_sha
    assert outcome.post_sha == post_sha
    assert "returncode=7" in outcome.error
    assert "stderr=remote rejected lease" in outcome.error
    assert outcome.local_integration_completed is True
    assert outcome.safe_to_continue is False


def test_keyboard_interrupt_after_integration_returns_outcome_unknown() -> None:
    pre_sha = "a" * 40
    post_sha = "b" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=[pre_sha, post_sha]),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(update_cmd, "_count_commits_between", side_effect=[0, 1]),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            side_effect=KeyboardInterrupt("operator interrupted validation"),
        ),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="post_origin_pull"
        )

    assert outcome.status == "outcome_unknown"
    assert outcome.pre_sha == pre_sha
    assert outcome.post_sha == post_sha
    assert "KeyboardInterrupt: operator interrupted validation" in outcome.error
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False


@pytest.mark.parametrize("interrupt_phase", ["merge", "pull", "push", "final_proof"])
def test_keyboard_interrupt_at_each_mutating_sync_boundary_returns_outcome_unknown(
    interrupt_phase,
) -> None:
    pre_sha = "a" * 40
    post_sha = "b" * 40

    def strict_run(cmd, **_kwargs):
        if cmd[-4:] == ["fetch", "upstream", "main", "--quiet"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if len(cmd) >= 2 and cmd[-2] == "tag":
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-3:] == ["merge", "--no-edit", "upstream/main"]:
            if interrupt_phase == "merge":
                raise KeyboardInterrupt("interrupted merge")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[-4:] == ["pull", "--ff-only", "upstream", "main"]:
            if interrupt_phase == "pull":
                raise KeyboardInterrupt("interrupted pull")
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {cmd}")

    observed_heads = (
        [pre_sha, KeyboardInterrupt("interrupted final proof"), post_sha]
        if interrupt_phase == "final_proof"
        else [pre_sha, post_sha]
    )

    def push(*args, **kwargs):
        if interrupt_phase == "push":
            raise KeyboardInterrupt("interrupted push")
        return _git_observation("succeeded", 0)

    origin_ahead = 1 if interrupt_phase == "merge" else 0
    with (
        patch.object(update_cmd, "_capture_head_sha", side_effect=observed_heads),
        patch.object(
            update_cmd, "_capture_checkout_proof", return_value=(True, "none", "")
        ),
        patch.object(
            update_cmd,
            "_observe_upstream_remote",
            return_value=_git_observation("succeeded", 0),
        ),
        patch.object(
            update_cmd,
            "_count_commits_between",
            side_effect=[origin_ahead, 1],
        ),
        patch(
            "hermes_cli.update_cmd_git._fork_sync_strategy",
            return_value="merge" if origin_ahead else "ff-only",
        ),
        patch(
            "hermes_cli.update_cmd_git._validate_fork_sync_candidate",
            return_value=True,
        ),
        patch.object(update_cmd, "_push_fork_with_upstream_observed", side_effect=push),
        patch("subprocess.run", side_effect=strict_run),
    ):
        outcome = update_cmd._sync_with_upstream_observed(
            ["git"], Path("/repo"), phase="post_origin_pull"
        )

    assert outcome.status == "outcome_unknown"
    assert outcome.pre_sha == pre_sha
    assert outcome.post_sha == post_sha
    assert f"interrupted {interrupt_phase.replace('_', ' ')}" in outcome.error
    assert outcome.safe_to_restore is False
    assert outcome.safe_to_continue is False


def test_updated_outcome_with_push_error_is_not_safe_to_continue() -> None:
    outcome = update_cmd.UpstreamSyncOutcome(
        phase="prepare",
        status="updated",
        pre_sha="a" * 40,
        post_sha="b" * 40,
        clean=True,
        operation_state="none",
        recovery_ref="refs/tags/pre-upstream-sync-test",
        error="fork push failed",
    )

    assert outcome.safe_to_continue is False
    assert outcome.safe_to_restore is False


def test_legacy_bool_true_after_local_sync_when_origin_push_fails() -> None:
    outcome = update_cmd.UpstreamSyncOutcome(
        phase="legacy",
        status="failed",
        pre_sha="a" * 40,
        post_sha="b" * 40,
        clean=True,
        operation_state="none",
        recovery_ref="refs/tags/pre-upstream-sync-20260912-120000",
        error="fork push failed; returncode=7; stderr=remote rejected",
        local_integration_completed=True,
    )

    with (
        patch.object(update_cmd, "_has_upstream_remote", return_value=True),
        patch(
            "hermes_cli.update_cmd_git._sync_with_upstream_observed",
            return_value=outcome,
        ),
    ):
        checked = update_cmd._sync_with_upstream_if_needed(["git"], Path("/repo"))

    assert checked is True
    assert outcome.safe_to_continue is False
    assert "remote rejected" in outcome.error


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
        tag_rc=0,
        test_rc=0,
        head_sha: str | None = "feedbead" * 5,
        calls=None,
    ):
        def side_effect(cmd, **kwargs):
            joined = " ".join(str(c) for c in cmd)
            if calls is not None:
                calls.append(joined)
            if "remote" in joined and "get-url" in joined:
                return subprocess.CompletedProcess(
                    cmd,
                    0,
                    stdout="git@github.com:example/hermes-agent.git\n",
                    stderr="",
                )
            if "status --porcelain=v1 --untracked-files=all" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "rev-parse --absolute-git-dir" in joined:
                return subprocess.CompletedProcess(
                    cmd, 0, stdout="/repo/.git\n", stderr=""
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
            if "tag pre-upstream-sync-" in joined:
                return subprocess.CompletedProcess(
                    cmd,
                    tag_rc,
                    stdout="",
                    stderr="tag creation failed" if tag_rc else "",
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
            if "fetch upstream main --quiet" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "pull --ff-only upstream main" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if joined.endswith("push origin main"):
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            if "reset --hard" in joined or "merge --abort" in joined:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise AssertionError(f"unexpected subprocess command: {cmd}")

        return side_effect

    def _run_sync(
        self,
        *,
        strategy,
        origin_ahead,
        upstream_ahead,
        merge_rc=0,
        tag_rc=0,
        test_rc=0,
        head_sha: str | None = "feedbead" * 5,
        syntax=(True, None, None),
    ):
        from pathlib import Path

        from hermes_cli import update_cmd

        calls = []
        config = {"updates": {"fork_sync_strategy": strategy}} if strategy else {}
        with (
            patch(
                "subprocess.run",
                side_effect=self._make_sync_side_effect(
                    origin_ahead,
                    upstream_ahead,
                    merge_rc=merge_rc,
                    tag_rc=tag_rc,
                    test_rc=test_rc,
                    head_sha=head_sha,
                    calls=calls,
                ),
            ),
            patch("hermes_cli.config.load_config", return_value=config),
            patch(
                "hermes_cli.update_cmd._validate_critical_files_syntax",
                return_value=syntax,
            ),
        ):
            update_cmd._sync_with_upstream_observed(
                ["git"], Path("/repo"), phase="prepare"
            )
        return calls

    def test_merge_strategy_merges_and_pushes_fork(self, capsys):
        calls = self._run_sync(strategy="merge", origin_ahead=2, upstream_ahead=5)

        assert any("merge --no-edit upstream/main" in c for c in calls)
        assert any(c.endswith("push origin main") for c in calls)
        assert not any("--force" in c for c in calls)
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

    def test_merge_strategy_tag_failure_never_merges_or_pushes(self, capsys):
        calls = self._run_sync(
            strategy="merge",
            origin_ahead=2,
            upstream_ahead=5,
            tag_rc=1,
        )

        assert not any("merge --no-edit" in call for call in calls)
        assert not any("push" in call for call in calls)
        assert "recovery tag" in capsys.readouterr().out.lower()

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
        assert any(
            "reset --hard feedbeadfeedbeadfeedbeadfeedbeadfeedbead" in c for c in calls
        )
        assert not any("push" in c for c in calls)
        out = capsys.readouterr().out
        assert "syntax error in a critical file" in out
        assert "Rolled back to feedbeadfe" in out
        assert "nothing was pushed" in out

    def test_merge_strategy_test_failure_rolls_back_and_never_pushes(self, capsys):
        calls = self._run_sync(
            strategy="merge",
            origin_ahead=2,
            upstream_ahead=5,
            test_rc=1,
        )

        assert any(" -m pytest " in f" {c} " for c in calls)
        assert any(
            "reset --hard feedbeadfeedbeadfeedbeadfeedbeadfeedbead" in c for c in calls
        )
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
        assert any(c.endswith("push origin main") for c in calls)
        assert not any("--force" in c for c in calls)
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
        assert any(
            "reset --hard feedbeadfeedbeadfeedbeadfeedbeadfeedbead" in c for c in calls
        )
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

        with (
            patch("subprocess.run", side_effect=side_effect),
            patch("shutil.which", return_value="/portable/uv"),
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

        with (
            patch("subprocess.run", side_effect=side_effect),
            patch("shutil.which", return_value="/portable/uv"),
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


def test_invalid_recovery_ref_is_never_emitted() -> None:
    from hermes_cli import update_cmd_git

    sha = "a" * 40
    with (
        patch.object(update_cmd, "_capture_head_sha", return_value=sha),
        patch.object(
            update_cmd,
            "_capture_checkout_proof",
            return_value=(True, "none", ""),
        ),
    ):
        outcome = update_cmd_git._finish_sync_outcome(
            ["git"],
            Path("/repo"),
            phase="prepare",
            status="failed",
            pre_sha=sha,
            recovery_ref="--upload-pack=evil",
            error="failed",
        )

    assert outcome.recovery_ref is None
