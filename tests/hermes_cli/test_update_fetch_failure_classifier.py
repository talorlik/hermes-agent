"""Fetch-failure classification for `hermes update` / `hermes update --check`.

A GitHub-side HTTP 429 (rate limit / outage) used to be reported as the
generic "Failed to fetch updates from origin." — or worse, matched the
"unable to access" branch and got called a local network error. The
classifier must call out rate limiting / outages explicitly, and the raw
stderr line must always be printed alongside the diagnosis.
"""

import os

import pytest

from hermes_cli import update_cmd


RATE_LIMIT_STDERR = (
    "error: RPC failed; HTTP 429 curl 22 The requested URL returned error: 429\n"
    "fatal: expected flush after ref listing"
)
CURL_429_STDERR = (
    "fatal: unable to access 'https://github.com/NousResearch/hermes-agent.git/':"
    " The requested URL returned error: 429"
)


class TestClassifyFetchFailure:
    def test_http_429_rpc_failure_reports_rate_limit(self):
        msg = update_cmd._classify_fetch_failure(RATE_LIMIT_STDERR)
        assert "rate limiting" in msg
        assert "try again in 5 minutes" in msg

    def test_curl_unable_to_access_429_is_rate_limit_not_network(self):
        # "unable to access" also appears here — 429 must win.
        msg = update_cmd._classify_fetch_failure(CURL_429_STDERR)
        assert "rate limiting" in msg
        assert "Network error" not in msg

    def test_rate_limit_phrase_without_code(self):
        msg = update_cmd._classify_fetch_failure("fatal: GitHub rate limit exceeded")
        assert "rate limiting" in msg

    def test_5xx_reports_outage(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: unable to access 'https://github.com/x.git/':"
            " The requested URL returned error: 503"
        )
        assert "outage" in msg
        assert "githubstatus.com" in msg

    def test_dns_failure_reports_network_error(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: unable to access 'https://github.com/x.git/':"
            " Could not resolve host: github.com"
        )
        assert msg.startswith("✗ Network error")

    def test_username_prompt_401_reports_github_not_user_credentials(self):
        # What GitHub's HTTP 401 looks like once the terminal prompt is
        # disabled — must NOT be blamed on the user's credentials.
        msg = update_cmd._classify_fetch_failure(
            "fatal: could not read Username for 'https://github.com':"
            " terminal prompts disabled"
        )
        assert "GitHub" in msg and "outage" in msg
        assert "check your git credentials" not in msg

    def test_auth_failure(self):
        msg = update_cmd._classify_fetch_failure(
            "fatal: Authentication failed for 'https://github.com/x.git/'"
        )
        assert "Authentication failed" in msg

    def test_ssh_publickey_denial_reports_ssh_auth_not_generic(self):
        # git wraps OpenSSH's own rejection as "Could not read from remote
        # repository" — never "Authentication failed" — so this needs its
        # own rule ahead of the generic fallback (#82169).
        msg = update_cmd._classify_fetch_failure(
            "git@github.com: Permission denied (publickey).\n"
            "fatal: Could not read from remote repository."
        )
        assert "SSH authentication failed" in msg
        assert "https://github.com/NousResearch/hermes-agent.git" in msg

    def test_ssh_host_key_failure_reports_ssh_auth(self):
        msg = update_cmd._classify_fetch_failure(
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "Host key verification failed.\n"
            "fatal: Could not read from remote repository."
        )
        assert "SSH authentication failed" in msg

    def test_unknown_falls_back_to_generic(self):
        msg = update_cmd._classify_fetch_failure("fatal: something novel")
        assert msg == "✗ Failed to fetch updates from origin."


class TestPrintFetchFailure:
    def test_prints_diagnosis_and_first_raw_line(self, capsys):
        update_cmd._print_fetch_failure(RATE_LIMIT_STDERR)
        out = capsys.readouterr().out
        assert "rate limiting" in out
        assert "HTTP 429" in out
        # raw first stderr line preserved for diagnosability
        assert "error: RPC failed" in out

    def test_empty_stderr_prints_only_diagnosis(self, capsys):
        update_cmd._print_fetch_failure("")
        out = capsys.readouterr().out.strip().splitlines()
        assert out == ["✗ Failed to fetch updates from origin."]


def test_update_network_git_calls_never_prompt_for_credentials():
    """Every `git fetch`/`pull`/`push` in the updater runs with prompts disabled.

    Live incident (Sep 2026): a GitHub-side 401 made `hermes update` sit on
    ``Username for 'https://github.com':`` instead of failing with a diagnosis.
    """
    import os
    import subprocess

    kw = update_cmd._no_prompt_git_kwargs()
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["env"]["GIT_TERMINAL_PROMPT"] == "0"
    # Only the prompt is disabled — credential helpers / askpass stay
    # configured so a private-fork origin still authenticates.
    assert "GIT_CONFIG_COUNT" not in kw["env"] or kw["env"]["GIT_CONFIG_COUNT"] == os.environ.get("GIT_CONFIG_COUNT")


@pytest.mark.usefixtures("python_less_fixture_tree_passes_audit")
def test_update_and_upstream_network_calls_disable_terminal_prompts(monkeypatch, tmp_path):
    """Exercise origin fetch and fork fetch/pull/push, not their source spelling.

    Real Git over a bare origin, a bare upstream one commit ahead, and a clone: the sync has to
    capture a real HEAD and prove a clean checkout before it reaches the network commands, so a
    mocked Git that answers every command with empty output never gets there. The tree holds one
    text file, hence the named audit opt-in and the candidate-test seam; the subject is the
    environment of the network spawns.
    """
    import subprocess
    from hermes_cli import update_cmd_git

    real_run = subprocess.run

    def git(cwd, *args):
        env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
        return real_run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                         "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True,
                        capture_output=True, text=True, env=env).stdout.strip()

    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    (seed / "content.txt").write_text("base\n", encoding="utf-8")
    git(seed, "add", "content.txt")
    git(seed, "commit", "-qm", "base")
    git(tmp_path, "clone", "-q", "--bare", str(seed), "origin.git")
    (seed / "content.txt").write_text("upstream\n", encoding="utf-8")
    git(seed, "commit", "-qam", "upstream")
    upstream_tip = git(seed, "rev-parse", "HEAD")
    git(tmp_path, "clone", "-q", "--bare", str(seed), "upstream.git")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(tmp_path / "origin.git"), str(clone))
    git(clone, "remote", "add", "upstream", str(tmp_path / "upstream.git"))

    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    monkeypatch.setenv("GCM_INTERACTIVE", "Always")
    monkeypatch.setenv("GIT_ASKPASS", "fixture-askpass")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "credential.helper")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "fixture-helper")
    # The candidate gate runs the checkout's own updater tests, which this tree does not have.
    monkeypatch.setattr(update_cmd_git, "_run_fork_sync_tests", lambda _cwd: (True, ""))
    network = {"fetch", "pull", "push", "ls-remote"}
    calls = []

    def run(cmd, **kwargs):
        if cmd[1] in network:
            calls.append((cmd[1:], kwargs))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    update_cmd._git_run(["git"], ["fetch", "origin", "main"], cwd=clone, network=True, check=True)
    assert update_cmd_git._sync_with_upstream_if_needed(["git"], clone, assume_yes=True)
    # The sync really landed and published: HEAD and the fork both hold the upstream commit.
    assert git(clone, "rev-parse", "HEAD") == upstream_tip
    assert git(tmp_path / "origin.git", "rev-parse", "main") == upstream_tip
    # ls-remote is the origin/main postcondition read-back: a network spawn like the others.
    assert [args[0] for args, _ in calls] == ["fetch", "fetch", "pull", "push", "ls-remote"]
    for args, kwargs in calls:
        assert kwargs["stdin"] is subprocess.DEVNULL, args
        env = kwargs["env"]
        assert env["GIT_TERMINAL_PROMPT"] == "0", args
        assert env["GCM_INTERACTIVE"] == "Never", args
        assert env["GIT_ASKPASS"] == "fixture-askpass", args
        assert env["GIT_CONFIG_COUNT"] == "1", args
        assert env["GIT_CONFIG_VALUE_0"] == "fixture-helper", args
