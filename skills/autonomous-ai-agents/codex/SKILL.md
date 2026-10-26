---
name: codex
description: "Use when delegating to Codex CLI or checking its auth."
version: 1.0.1
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Coding-Agent, Codex, OpenAI, Code-Review, Refactoring]
    related_skills: [claude-code, hermes-agent]
---

# Codex CLI

Delegate coding tasks to [Codex](https://github.com/openai/codex) via the Hermes terminal. Codex is OpenAI's autonomous coding agent CLI.

## When to use

- Building features
- Refactoring
- PR reviews
- Batch issue fixing

Requires the codex CLI and a git repository.

## Prerequisites

- Codex installed: `npm install -g @openai/codex`
- OpenAI auth configured: either `OPENAI_API_KEY` or Codex OAuth credentials
  from the Codex CLI login flow
- Building and review run inside a git repository. An auth probe outside one
  needs `--skip-git-repo-check`. "Not inside a trusted directory" is a trust
  refusal, not an auth failure.
- Use `pty=true` for interactive or long Codex sessions. A short `codex exec`
  one-shot can finish without a PTY.

For Hermes itself, `model.provider: openai-codex` uses Hermes-managed Codex
OAuth from `~/.hermes/auth.json` after `hermes auth add openai-codex`. The
standalone CLI uses `~/.codex/auth.json`. Status on one store does not prove
the other, and neither local status proves the responses API accepts the token.
Do not treat a missing `OPENAI_API_KEY` alone as proof that Codex auth is missing.

## One-Shot Tasks

```
terminal(command="codex exec 'Add dark mode toggle to settings'", workdir="~/project", pty=true)
```

For scratch work (Codex needs a git repo):
```
terminal(command="cd $(mktemp -d) && git init && codex exec 'Build a snake game in Python'", pty=true)
```

## Background Mode (Long Tasks)

```
# Start in background with PTY
terminal(command="codex exec --sandbox workspace-write 'Refactor the auth module'", workdir="~/project", background=true, pty=true)
# Returns session_id

# Monitor progress
process(action="poll", session_id="<id>")
process(action="log", session_id="<id>")

# Send input if Codex asks a question
process(action="submit", session_id="<id>", data="yes")

# Kill if needed
process(action="kill", session_id="<id>")
```

## Key Flags

| Flag | Effect |
|------|--------|
| `exec "prompt"` | One-shot execution, exits when done |
| `--sandbox workspace-write` (`-s`) | Sandboxed but auto-approves file changes in the workspace (the recommended auto-build mode) |
| `--dangerously-bypass-approvals-and-sandbox` | No sandbox, no approvals (fastest, most dangerous; `--yolo` still works as a hidden alias) |
| `--sandbox danger-full-access` | No Codex sandbox; useful when the host service context breaks bubblewrap |

> **Deprecated:** `--full-auto` still works but the live CLI warns to use `--sandbox workspace-write` instead.

## Hermes Gateway Caveat

When invoking the Codex CLI from a Hermes gateway/service context (for example,
Telegram-driven agent sessions), Codex `workspace-write` sandboxing may fail even
when the same command works in the user's interactive shell. A typical symptom is
bubblewrap/user-namespace errors such as `setting up uid map: Permission denied`
or `loopback: Failed RTM_NEWADDR: Operation not permitted`.

In that context, prefer:

```
codex exec --sandbox danger-full-access "<task>"
```

Use process boundaries as the safety layer instead: explicit `workdir`, clean git
status before launch, narrow task prompts, `git diff` review, targeted tests, and
human/agent confirmation before committing broad changes.

## PR Reviews

Clone to a temp directory for safe review:

```
terminal(command="REVIEW=$(mktemp -d) && git clone https://github.com/user/repo.git $REVIEW && cd $REVIEW && gh pr checkout 42 && codex review --base origin/main", pty=true)
```

## Parallel Issue Fixing with Worktrees

```
# Create worktrees
terminal(command="git worktree add -b fix/issue-78 ~/.hermes/cache/scratch/issue-78 main", workdir="~/project")
terminal(command="git worktree add -b fix/issue-99 ~/.hermes/cache/scratch/issue-99 main", workdir="~/project")

# Launch Codex in each
terminal(command="codex --sandbox workspace-write exec 'Fix issue #78: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-78", background=true, pty=true)
terminal(command="codex --sandbox workspace-write exec 'Fix issue #99: <description>. Commit when done.'", workdir="~/.hermes/cache/scratch/issue-99", background=true, pty=true)

# Monitor
process(action="list")

# After completion, push and create PRs
terminal(command="cd ~/.hermes/cache/scratch/issue-78 && git push -u origin fix/issue-78")
terminal(command="gh pr create --repo user/repo --head fix/issue-78 --title 'fix: ...' --body '...'")

# Cleanup
terminal(command="git worktree remove ~/.hermes/cache/scratch/issue-78", workdir="~/project")
```

## Batch PR Reviews

```
# Fetch all PR refs
terminal(command="git fetch origin '+refs/pull/*/head:refs/remotes/origin/pr/*'", workdir="~/project")

# Review multiple PRs in parallel
terminal(command="codex exec 'Review PR #86. git diff origin/main...origin/pr/86'", workdir="~/project", background=true, pty=true)
terminal(command="codex exec 'Review PR #87. git diff origin/main...origin/pr/87'", workdir="~/project", background=true, pty=true)

# Post results
terminal(command="gh pr comment 86 --body '<review>'", workdir="~/project")
```

## Commit review from a gateway

`codex exec review --commit` rejects `--sandbox`. Do not pass that flag to the review subcommand. From a gateway session, review in the worktree with `pty=true`:

```
codex exec -m gpt-6-astra -s danger-full-access --ephemeral --ignore-rules -o <outfile> "<prompt>"
```

Put the commit SHA, the defect, and the proof in the prompt. Tell the reviewer a missing ledger claim is not a code defect when that claim is a later commit: a squash deletes the branch SHA, so the claim cannot exist on the reviewed commit.

## Auth status is not a live call

`codex login status` only reads `~/.codex/auth.json`. "Logged in using ChatGPT"
can coexist with `HTTP 401` on `wss://api.openai.com/v1/responses`. Do not report
Codex signed out from that status line, from a compaction summary of an older
401, or from a desktop activity line such as "used Codex unauthenticated": the
activity summarizer echoes a search query as the thing that was used.

Before telling the user to run `codex login`, or before repeating an
unauthenticated claim, run a harmless exact-response probe:

```
codex exec -m gpt-6-astra -s read-only --ephemeral --ignore-rules --skip-git-repo-check 'Respond with OK only.'
```

Require exit 0 and the exact token `OK`. Do not print tokens. If the probe
succeeds, say Codex is authenticated. Do not delete `~/.codex/auth.json`, and
do not treat a stale id-token subscription date as a dead account. If a later
exec returns 401, recovery is `codex login` and the user approving the ChatGPT
browser prompt. Do not delete the auth file first.

## Rules

1. **PTY for interactive sessions** — long or interactive Codex hangs without a PTY. A short `codex exec` probe does not need one.
2. **Git repo for build and review** — use `mktemp -d && git init` for scratch work. An auth probe outside a trusted directory must pass `--skip-git-repo-check`; that refusal is not an auth failure.
3. **Use `exec` for one-shots** — `codex exec "prompt"` runs and exits cleanly
4. **`--sandbox workspace-write` for building** — auto-approves changes within the sandbox (`--full-auto` is deprecated for this)
5. **Background for long tasks** — use `background=true` and monitor with `process` tool
6. **Don't interfere** — monitor with `poll`/`log`, be patient with long-running tasks
7. **Parallel is fine** — run multiple Codex processes at once for batch work
