"""Both updater call sites act on the typed upstream-sync outcome, never on a Boolean.

The Boolean wrapper answers "was upstream checked", which reads the same for a declined
sync and for a candidate that failed validation and was rolled back. These tests drive the
real ``_prepare_checkout_for_update`` / ``_pull_updates`` against real Git checkouts. Where a
test replaces the sync it replaces the typed owner, so the verdict under test is the call
site's; the tests at the end run the real owner and the real tree audit end to end.
"""
import inspect
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import update_cmd, update_cmd_git, update_cmd_integrity
from hermes_cli.update_receipt import read_sync_quarantine
from tests.hermes_cli.test_update_existing_branch_baseline import checkout, prepare  # noqa: F401
from tests.hermes_cli.test_update_target_identity import git

READ_ONLY_GIT = {"rev-parse", "status", "remote"}


def observed(root, status, *, error="", integrated=False, after=None):
    """A typed-owner stand-in reporting *status* for the checkout as it stands.

    *after* runs once the outcome is captured: something that happens to the checkout between
    the sync returning and the call site acting on it.
    """

    def sync(_git_cmd, _cwd, *, phase, assume_yes, input_fn, **_owner_kwargs):
        sha = git(root, "rev-parse", "HEAD")
        outcome = update_cmd.UpstreamSyncOutcome(
            phase=phase, status=status, pre_sha=sha, post_sha=sha, clean=True,
            operation_state="none", recovery_ref=sha, error=error,
            local_integration_completed=integrated)
        if after is not None:
            after()
        return outcome

    return sync


def run_site(site, root):
    """Drive one of the two call sites; *root* is prepared by the caller's fixture."""
    if site == "prepare":
        return prepare(root, is_fork=True)
    return update_cmd._pull_updates(
        ["git"], "main", None, prompt_for_restore=False, gw_input_fn=None,
        discard_local_changes=False, keep_stash=False, sync_upstream=True, assume_yes=True)


@pytest.fixture
def fork_level_with_origin(checkout):
    root, old, tip = checkout
    git(root, "checkout", "-q", "--detach", tip)
    return root, old, tip


@pytest.fixture
def site_checkout(request, checkout):
    """The checkout as each call site finds it: level with origin, or one commit behind on main."""
    root, old, tip = checkout
    if request.param == "prepare":
        git(root, "checkout", "-q", "--detach", tip)
    else:
        git(root, "checkout", "-q", "main")
        git(root, "reset", "-q", "--hard", old)
    return request.param, root, old, tip


both_sites = pytest.mark.parametrize("site_checkout", ["prepare", "post_origin_pull"], indirect=True)


@pytest.mark.parametrize("status, repair", [
    ("rollback_failed", "restore_recovery_ref"), ("outcome_unknown", "verify_checkout_then_retry")])
def test_unrecoverable_sync_outcome_quarantines_with_a_structured_repair(
        fork_level_with_origin, monkeypatch, capsys, status, repair):
    root, _old, tip = fork_level_with_origin
    monkeypatch.setattr(
        update_cmd, "_sync_with_upstream_observed", observed(root, status, error="reset failed"))
    with pytest.raises(SystemExit) as exc:
        prepare(root, is_fork=True)
    assert exc.value.code == 1
    evidence = read_sync_quarantine(root)
    assert (evidence["status"], evidence["phase"]) == (status, "prepare")
    assert evidence["repair"] == {
        "action": repair, "recovery_ref": tip, "expected_head": tip, "observed_head": tip}
    # The marker is consumed, not just written: the next update names the repair it requires.
    capsys.readouterr()
    with pytest.raises(SystemExit):
        update_cmd._refuse_existing_sync_quarantine()
    assert f"Required repair: {repair}" in capsys.readouterr().out


@both_sites
@pytest.mark.parametrize("boolean_answer", [True, False])
def test_failed_and_rolled_back_sync_is_never_green(site_checkout, monkeypatch, boolean_answer):
    """Unmoved HEAD plus a clean tree is what a rejected, rolled-back candidate leaves.

    A Boolean seam on the facade answering either way must not be able to turn that into a
    continued update: the call sites read the typed owner only.
    """
    site, root, old, tip = site_checkout
    monkeypatch.setattr(update_cmd._m(), "_sync_with_upstream_if_needed", lambda *a, **k: boolean_answer)
    monkeypatch.setattr(
        update_cmd, "_sync_with_upstream_observed",
        observed(root, "failed", error="upstream candidate validation failed"))
    with pytest.raises(SystemExit) as exc:
        run_site(site, root)
    assert exc.value.code == 1
    # The late site had already merged origin; a rejected candidate puts the pre-update head back.
    assert git(root, "rev-parse", "HEAD") == (tip if site == "prepare" else old)
    assert git(root, "rev-parse", "origin/main") == tip
    assert read_sync_quarantine(root) is None


@both_sites
def test_branch_switched_after_the_sync_is_never_reset(site_checkout, monkeypatch, capsys):
    """The rollback acts on whatever is checked out; a branch that appeared since is not ours."""
    site, root, _old, tip = site_checkout
    monkeypatch.setattr(
        update_cmd, "_sync_with_upstream_observed",
        observed(root, "failed", error="upstream candidate validation failed",
                 after=lambda: git(root, "checkout", "-qb", "unexpected")))
    with pytest.raises(SystemExit) as exc:
        run_site(site, root)
    assert exc.value.code == 1
    assert git(root, "rev-parse", "unexpected") == tip
    assert git(root, "branch", "--show-current") == "unexpected"
    assert git(root, "rev-parse", "main") == tip
    output = capsys.readouterr().out
    assert "checkout is on 'unexpected'" in output
    assert "Rolling back" not in output
    assert read_sync_quarantine(root) is None


@pytest.mark.parametrize("head_capturable", [True, False])
def test_typed_decline_is_attested_by_an_owner_that_ran_no_mutating_command(
        fork_level_with_origin, monkeypatch, head_capturable):
    """``nothing_attempted`` is the observed sync's own statement, and it is true by construction."""
    root, _old, tip = fork_level_with_origin
    if not head_capturable:
        monkeypatch.setattr(update_cmd, "_capture_head_sha", lambda *_: None)
    commands = []
    real_run = subprocess.run

    def recording_run(command, **kwargs):
        commands.append(command[1:])
        return real_run(command, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    outcome = update_cmd_git._sync_with_upstream_observed(["git"], root, phase="prepare", assume_yes=True)
    monkeypatch.setattr(subprocess, "run", real_run)

    assert outcome.status == "not_checked" and outcome.nothing_attempted
    assert outcome.safe_to_continue is head_capturable
    assert {command[0] for command in commands} <= READ_ONLY_GIT
    assert [c for c in commands if c[0] == "remote"] == [["remote", "get-url", "upstream"]]
    assert git(root, "rev-parse", "HEAD") == tip and not git(root, "remote")


def test_real_typed_decline_continues_and_reports_upstream_as_not_checked(fork_level_with_origin):
    """No stand-in: the real owner declines on a fork with no upstream remote."""
    root, _old, tip = fork_level_with_origin
    plan = prepare(root, is_fork=True)
    assert plan.commit_count == 0
    assert plan.upstream_checked is False
    assert git(root, "rev-parse", "HEAD") == tip and not git(root, "remote")
    assert read_sync_quarantine(root) is None


def test_unpushable_fork_does_not_fail_an_update_whose_local_sync_is_validated(
        fork_level_with_origin, monkeypatch):
    """A run after an unpushed sync re-validates the same commit and fails the same push."""
    root, _old, tip = fork_level_with_origin
    monkeypatch.setattr(
        update_cmd, "_sync_with_upstream_observed",
        observed(root, "failed", error="fork push failed", integrated=True))
    plan = prepare(root, is_fork=True)
    assert plan.upstream_checked is True
    assert plan.commit_count == 0
    assert git(root, "rev-parse", "HEAD") == tip
    assert read_sync_quarantine(root) is None


def test_boolean_false_is_not_a_typed_no_attempt_and_is_refused(fork_level_with_origin):
    """The Boolean API answers False for declined, failed, and rolled back alike."""
    root, _old, tip = fork_level_with_origin
    outcome = update_cmd_git._observe_legacy_upstream_sync(
        lambda *a, **k: False, ["git"], root, phase="prepare")
    assert outcome.status == "outcome_unknown"
    assert not outcome.safe_to_continue and not outcome.nothing_attempted
    assert outcome.repair["action"] == "verify_checkout_then_retry"
    with pytest.raises(SystemExit) as exc:
        update_cmd._enforce_upstream_sync_outcome(
            outcome, ["git"], rollback_sha=None, rollback_branch=None, windows_gateway_resume=None)
    assert exc.value.code == 1
    assert git(root, "rev-parse", "HEAD") == tip


def test_public_boolean_wrapper_signature_is_unchanged():
    parameters = inspect.signature(update_cmd_git._sync_with_upstream_if_needed).parameters
    assert [(name, p.kind.name, p.default) for name, p in parameters.items()] == [
        ("git_cmd", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty),
        ("cwd", "POSITIONAL_OR_KEYWORD", inspect.Parameter.empty),
        ("assume_yes", "KEYWORD_ONLY", False),
        ("input_fn", "KEYWORD_ONLY", None)]
    assert update_cmd._m()._sync_with_upstream_if_needed is update_cmd_git._sync_with_upstream_if_needed


@pytest.mark.parametrize("status, integrated, checked", [
    ("noop", False, True), ("updated", True, True), ("failed", True, True),
    ("failed", False, False), ("rollback_failed", False, False), ("outcome_unknown", False, False)])
def test_public_boolean_wrapper_keeps_its_checked_contract(
        fork_level_with_origin, monkeypatch, status, integrated, checked):
    root, _old, _tip = fork_level_with_origin
    monkeypatch.setattr(update_cmd, "_has_upstream_remote", lambda *_: True)
    monkeypatch.setattr(
        update_cmd_git, "_sync_with_upstream_observed", observed(root, status, integrated=integrated))
    assert update_cmd_git._sync_with_upstream_if_needed(["git"], root) is checked


def test_public_boolean_wrapper_answers_false_for_a_declined_sync(fork_level_with_origin):
    root, _old, _tip = fork_level_with_origin
    assert update_cmd_git._sync_with_upstream_if_needed(["git"], root, assume_yes=True) is False
    assert not git(root, "remote")


@pytest.fixture
def fork_with_upstream(tmp_path, monkeypatch):
    """A clone of a bare fork plus a bare upstream, holding Python the real audit can judge.

    Nothing about the audit is replaced: ``failure_lines`` is wrapped only to record what the
    real function returned. The import list is narrowed to the modules this tree ships (data,
    as in ``test_update_integrity_gate``), and the candidate gate's last step is stubbed
    because the tree carries no updater tests for it to run.
    """
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "config", "user.name", "Fixture")
    git(seed, "config", "user.email", "fixture@example.invalid")
    (seed / "hermes_constants.py").write_text("X = 1\n", encoding="utf-8")
    # The launcher imports the checkout's own bootstrap; without it the installed one runs.
    (seed / "hermes_bootstrap.py").write_text("", encoding="utf-8")
    (seed / "cron").mkdir()
    (seed / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (seed / "cron" / "jobs.py").write_text("Y = 1\n", encoding="utf-8")
    git(seed, "add", "-A")
    git(seed, "-c", "commit.gpgsign=false", "commit", "-qm", "base")
    origin, upstream, clone = tmp_path / "origin.git", tmp_path / "upstream.git", tmp_path / "clone"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(upstream))
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    git(clone, "config", "user.name", "Fixture")
    git(clone, "config", "user.email", "fixture@example.invalid")
    git(clone, "remote", "add", "upstream", str(upstream))
    monkeypatch.setattr(update_cmd._m(), "PROJECT_ROOT", clone)
    monkeypatch.setattr(update_cmd_integrity, "INTEGRITY_IMPORT_MODULES", ("hermes_constants", "cron.jobs"))
    monkeypatch.setattr(update_cmd_git, "_run_fork_sync_tests", lambda _cwd: (True, ""))
    audits = []
    real_audit = update_cmd_integrity.failure_lines

    def recording_audit(*args, **kwargs):
        findings = real_audit(*args, **kwargs)
        audits.append(findings)
        return findings

    monkeypatch.setattr(update_cmd_integrity, "failure_lines", recording_audit)

    def publish(jobs_source):
        (seed / "cron" / "jobs.py").write_text(jobs_source, encoding="utf-8")
        git(seed, "-c", "commit.gpgsign=false", "commit", "-qam", "upstream change")
        git(seed, "push", "-q", str(upstream), "main")
        return git(seed, "rev-parse", "HEAD")

    return SimpleNamespace(clone=clone, origin=origin, publish=publish, audits=audits,
                           base=git(clone, "rev-parse", "HEAD"))


def test_real_audit_accepts_a_valid_upstream_candidate_and_the_update_proceeds(fork_with_upstream):
    f = fork_with_upstream
    tip = f.publish("Y = 2\n")
    plan = prepare(f.clone, is_fork=True)
    assert f.audits == [None]  # the real audit ran on the candidate and passed it
    assert git(f.clone, "rev-parse", "HEAD") == tip
    assert git(f.origin, "rev-parse", "main") == tip
    assert plan.upstream_checked and plan.commit_count >= 1
    assert read_sync_quarantine(f.clone) is None


def test_noncritical_python_corruption_reaches_the_real_audit_and_stops_the_update(
        fork_with_upstream, capsys):
    """``cron/jobs.py`` is not a critical file: only the tree audit can see it is broken."""
    f = fork_with_upstream
    f.publish(") -> List[str]:\n")
    with pytest.raises(SystemExit) as exc:
        prepare(f.clone, is_fork=True)
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "syntax error in a critical file" not in output
    assert "tree integrity audit" in output
    (findings,) = f.audits
    assert findings is not None and any("cron/jobs.py" in line for line in findings)
    # Rejected, rolled back, and not published: the fork and the checkout hold the old commit.
    assert git(f.clone, "rev-parse", "HEAD") == f.base
    assert git(f.origin, "rev-parse", "main") == f.base
    assert not git(f.clone, "status", "--porcelain")
    assert read_sync_quarantine(f.clone) is None


def test_branch_switched_during_candidate_validation_is_never_reset(
        fork_with_upstream, monkeypatch, capsys):
    """The owner's own rollback is a ``reset --hard``: it proves the checkout is still its own.

    Validation runs candidate code. A branch that appears while it runs is not the sync's, so
    the owner refuses the rollback, reports that it did not run, and leaves a structured repair.
    """
    f = fork_with_upstream
    candidate = f.publish(") -> List[str]:\n")
    (f.clone / "user-owned.txt").write_text("user-owned stash\n", encoding="utf-8")
    git(f.clone, "stash", "push", "-qu", "-m", "user-owned")
    stash = git(f.clone, "rev-parse", "refs/stash")
    audit = update_cmd_integrity.failure_lines

    def switch_after_audit(*args, **kwargs):
        findings = audit(*args, **kwargs)
        assert findings and git(f.clone, "rev-parse", "HEAD") == candidate
        git(f.clone, "checkout", "-qb", "unexpected")
        return findings

    monkeypatch.setattr(update_cmd_integrity, "failure_lines", switch_after_audit)
    with pytest.raises(SystemExit) as exc:
        prepare(f.clone, is_fork=True)
    assert exc.value.code == 1
    assert git(f.clone, "branch", "--show-current") == "unexpected"
    assert git(f.clone, "rev-parse", "HEAD") == candidate
    assert git(f.clone, "rev-parse", "unexpected") == candidate
    assert git(f.clone, "rev-parse", "refs/stash") == stash
    assert git(f.clone, "show", "stash@{0}^3:user-owned.txt") == "user-owned stash"
    assert git(f.origin, "rev-parse", "main") == f.base
    output = capsys.readouterr().out
    assert "Rollback not attempted" in output and "Rolled back" not in output
    # Not restored, and said so: the sync's branch still holds the rejected candidate.
    evidence = read_sync_quarantine(f.clone)
    assert evidence["status"] == "rollback_failed"
    assert "rollback refused" in evidence["error"] and "refs/heads/unexpected" in evidence["error"]
    repair = evidence["repair"]
    assert (repair["action"], repair["expected_head"], repair["observed_head"]) == (
        "restore_recovery_ref", f.base, candidate)
    assert git(f.clone, "rev-parse", repair["recovery_ref"]) == f.base


def test_rollback_resets_only_the_checkout_identity_it_was_given(fork_with_upstream):
    """Positive control and the refusals: the same reset runs for the matching identity only."""
    f = fork_with_upstream
    candidate = f.publish("Y = 2\n")
    git(f.clone, "pull", "-q", "--ff-only", "upstream", "main")
    branch = git(f.clone, "symbolic-ref", "HEAD")
    landed = update_cmd_git._capture_checkout_identity(["git"], f.clone)
    assert landed == update_cmd_git.CheckoutIdentity(ref=branch, head=candidate)
    with pytest.raises(TypeError):  # the identity is not optional
        update_cmd_git._rollback_fork_sync_candidate(["git"], f.clone, f.base)
    for stale in (None,
                  update_cmd_git.CheckoutIdentity(ref=branch, head=f.base),
                  update_cmd_git.CheckoutIdentity(ref="refs/heads/elsewhere", head=candidate)):
        refused = update_cmd_git._rollback_fork_sync_candidate(
            ["git"], f.clone, f.base, expected=stale)
        assert refused.state == "refused" and not refused.ok
        assert git(f.clone, "rev-parse", "HEAD") == candidate
    rolled = update_cmd_git._rollback_fork_sync_candidate(
        ["git"], f.clone, f.base, expected=landed)
    assert rolled.ok
    assert (git(f.clone, "symbolic-ref", "HEAD"), git(f.clone, "rev-parse", "HEAD")) == (branch, f.base)
