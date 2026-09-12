# Fork Change Ledger

Auditable record of every fork-only change `talorlik/hermes-agent` carries on
top of `NousResearch/hermes-agent` (`upstream`, pull-only). Required by
ARD-010 (Central Command orchestration plan): the autonomous upstream
conflict resolver (`cc_resolve_upstream_conflict`) resolves conflicted files
by ledger ownership and never guesses; a conflicted file with no owning entry
is `unknown_fork_change` and blocks the merge.

Verification: `python scripts/ci/check_fork_ledger.py` (the weekly
`post_verify` gate). It resolves both refs once, reads this ledger from the
pinned fork commit, and uses only those immutable OIDs afterward. A merge is
an exempt upstream sync only when it has exactly two parents in fork/upstream
order and its tree equals `git merge-tree --write-tree`; octopus, conflicted,
or manually altered merges remain mapped work. Every work commit must be
claimed by exactly one entry. A Commits field may list full 40-character SHAs,
non-empty `<sha>..<sha>` ranges, `none` (path ownership with no commit claim),
or `self`. Explicit claims must be inside the evaluated work range and the
claimant must be the effective owner of every changed path across all declared
Owned-Files paths, even when a path is absent from the current three-dot delta.
`self` additionally requires that the specific entry changed relative to the
commit's first parent. Do not put a branch-only SHA in G-FORK-LEDGER; that SHA
changes on merge or squash.

Current-path ownership is the three-dot `upstream/main...fork-ref` changed
path set, including both source and destination of renames. Git output is
parsed as NUL-delimited bytes with reversible decoding. Plain Owned-Files and
Path-Precedence paths cover ordinary names; edge whitespace, ASCII controls,
DEL, and non-UTF-8 bytes require a JSON string literal. JSON syntax permits
representable controls but never NUL. Every current path needs exactly one
owner, or a Path-Precedence row that names one of the entries that list it.
Missing and ambiguous current paths fail closed. Entries missing a required
field (Commits, Owned-Files, Intent, Protected-Invariant, Tests,
Retirement-Condition, Disposition) fail the check. Tests:
`tests/ci/test_check_fork_ledger.py`.

Owned-Files lists the paths the entry owns for conflict resolution, as they
exist on the current `main` (upstream refactors can relocate a path; entries
note the original location when it moved). Dispositions: `active` (delta
present, intent still needed), `absorbed-upstream` (upstream now ships the
change; entry is removed after the next clean sync shows no residual delta),
`retiring` (scheduled for removal by a named plan item).

## G-UPDATE-FORKSYNC: automatic upstream merge in `hermes update`
- Commits: cbeb0229a8e345a7a2adaf5ff698d49a65c55da7, a2c03c83df48a73f3165d4b515c3398884a3661f, a9ac57c43a884142abd27057775c143f78f22ca7, b04eaa31610d8c3ba569425db9228b3c4ed07ae0, 3eee8924155495442cf25ad9d787260c23c695e9, 66375c3670805d5a72c6a9ce5bc30bdf688f3274, 4ab5388525fe048fd06fad61394f3abeddc61b00
- Owned-Files:
  - hermes_cli/update_cmd.py
  - hermes_cli/update_cmd_git.py
  - hermes_cli/config_defaults.py
  - tests/hermes_cli/test_cmd_update.py
  - tests/hermes_cli/test_fork_sync_strategy.py
  - tests/hermes_cli/test_update_fleet_restart_pending.py
- Intent: `updates.fork_sync_strategy: merge` makes `hermes update` merge `upstream/main` into `main` (fork commits preserved, recovery tag before merge, post-merge syntax guard with rollback, candidate test run before push, no-op pull accepted). Default stays `ff_only`; unknown values fall back to `ff_only`.
- Protected-Invariant: An update never force-pushes or rewrites `main`; a conflict or a post-merge syntax/test failure aborts with the pre-merge SHA restored and nothing pushed; upstream code merged by the sync cannot reach origin unchecked.
- Tests: tests/hermes_cli/test_fork_sync_strategy.py, tests/hermes_cli/test_cmd_update.py
- Retirement-Condition: Upstream ships an equivalent fork-sync merge strategy in `hermes update`, or the fork stops carrying local commits (P5/P6 cutover replaces the mechanism with `cc_hermes_update` + `cc_resolve_upstream_conflict` and upstream absorbs the remainder).
- Disposition: active

## G-CRON-DURABLE: durable scheduler outcomes, detached runs, fail-closed scripts
- Commits: baa76e498e5b91539de84cbc3684763b656ac9c0, c70f07ebfa72568dbf27e2be0c6349a3df32e633, 77318d25fdc898c60d5c3af02afa2f8c188e993c, 17573db2d074a31d3b354f24108c369f6a83d5a7, 6bcd2426e168d071336e755eedd992dfa059a167, a2eacbd8886cc7fbb5c7c4622da613b9e4d2a684, 2e8c9404efae4efc634fda3a53cb6ea7d48f51ac, 96170f7609607cd7e30cd821b6d82b82a3e954f7, 36346f27754969e99d016b73a924b9fd5abb4994, 803f3249f1b61009c9081ae9474618d7876603a1
- Owned-Files:
  - cron/deferrals.py
  - cron/outcomes.py
  - cron/executions.py
  - cron/incidents.py
  - cron/jobs.py
  - cron/monitor.py
  - cron/outbox.py
  - cron/scheduler.py
  - cron/scheduler_delivery.py
  - cron/scheduler_script.py
  - hermes_cli/cron.py
  - hermes_cli/subcommands/cron.py
  - hermes_cli/main.py
  - tools/cronjob_tools.py
  - tools/cronjob_job_args.py
  - tests/cron/test_deferred_obligations.py
  - tests/cron/test_delivery_no_unawaited_coroutines.py
  - tests/cron/test_delivery_outbox.py
  - tests/cron/test_detached_runs.py
  - tests/cron/test_execution_ledger.py
  - tests/cron/test_execution_schema_migration.py
  - tests/cron/test_incident_lifecycle.py
  - tests/cron/test_monitor_commit_safety.py
  - tests/cron/test_outcome_serializers.py
  - tests/cron/test_pre_script_typed_outcomes.py
  - tests/cron/test_recurring_eagain_redispatch.py
  - tests/cron/test_cron_script_failure_policy.py
  - tests/cron/test_cronjob_schema.py
  - tests/cron/test_script_claim_heartbeat.py
  - tests/hermes_cli/test_cron.py
  - tests/hermes_cli/test_cron_exit_code_propagation.py
  - website/docs/user-guide/features/cron.md
- Intent: Typed durable outcomes and an execution ledger for scheduled jobs, detached runs that survive gateway restarts with propagated finalization exit codes, delivery outbox, incident lifecycle, and fail-closed handling of script errors (a failing pre/post script fails the run instead of silently passing).
- Protected-Invariant: A scheduled job's outcome is always durably recorded; script failure never reports success; detached finalization exit codes reach the caller; upstream schema migrations must not drop the executions ledger.
- Tests: tests/cron/, tests/hermes_cli/test_cron.py, tests/hermes_cli/test_cron_exit_code_propagation.py
- Retirement-Condition: Conductor cutover completes and P6-B retires `cron/deferrals.py` and `cron/outcomes.py` after confirming no remaining caller; the rest retires if upstream ships equivalent durable-outcome semantics.
- Disposition: active

## G-KANBAN-LIFECYCLE: durable Kanban lifecycle contracts and CAS guards
- Commits: 5b437da587b342a777670ee13e9b3ff3a72138a6, f89207ba9c9db124f171fb270a8fb5a542eb7eb3, 2b54b66a8eae0e56515385e732a1b37a9015fb58, 9a8ec0e30dce7d9ef6543e57c5b7000e83df66f8, d963c8d5870cbef3787c5d2c3729d75f26854398, dd933de38411a3cfa5a05dc8ce7434c1b07f1d71, db0ccf1fff6b2debdb7a1414398887209fe180d7, 27cb612eae6c3ebe7b752ee2309e8deefea0456e, c40cd2458e2b940b9e9a5ed4491348aa917d579b, e1ff3ace899ccbf44396bb1997c157f4d390170f, 83143076bd14092ed2e0f61543c6e9621ce078f5
- Owned-Files:
  - hermes_cli/kanban.py
  - hermes_cli/kanban_db.py
  - hermes_cli/kanban_db_connect.py
  - hermes_cli/kanban_parser.py
  - plugins/kanban/dashboard/plugin_api.py
  - tools/kanban_tools.py
  - tests/hermes_cli/test_kanban_cli_exit_status.py
  - tests/hermes_cli/test_kanban_db.py
  - tests/hermes_cli/test_kanban_durable_text_canonicalizer.py
  - tests/hermes_cli/test_kanban_expected_run_id_flag.py
  - tests/hermes_cli/test_kanban_lifecycle_receipts.py
  - tests/hermes_cli/test_kanban_task_snapshot.py
  - tests/hermes_cli/test_kanban_write_txn_busy_retry.py
  - tests/plugins/test_kanban_dashboard_plugin.py
  - tests/tools/test_kanban_tools.py
- Intent: Compare-and-swap guards on card lifecycle transitions (claim, complete with expected status, expected-run-id), idempotent guarded comments, claim-replay hardening, lifecycle authorship receipts, and busy-retry on write transactions, so concurrent agents cannot corrupt board state or double-claim cards.
- Protected-Invariant: A lifecycle transition observed under a stale expectation fails with a nonzero exit instead of silently overwriting; replayed claims and repeated guarded comments are idempotent; lifecycle receipts identify the author.
- Tests: tests/hermes_cli/test_kanban_db.py, tests/hermes_cli/test_kanban_cli_exit_status.py, tests/tools/test_kanban_tools.py
- Retirement-Condition: Upstream ships equivalent CAS lifecycle guards and receipts for the Kanban board.
- Disposition: active

## G-KANBAN-DEEPLINKS: browser task deep links in the Kanban dashboard
- Commits: d185efb251c71090f5e53d5fe15e0f11acc64115, bee04af2d491c84e8de68c482c6a82a5a492788a, 4465666d76be5e05dc57456e99cffa64310fab6a
- Owned-Files:
  - plugins/kanban/dashboard/dist/index.js
  - apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx
  - website/docs/user-guide/features/kanban.md
- Intent: Deep links from Kanban dashboard cards into browser tasks, plus reconciliation of the automated formatter output for the bundled dashboard test.
- Protected-Invariant: Dashboard card links resolve to their task views; the dist bundle and its test stay in sync.
- Tests: apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx
- Retirement-Condition: Upstream ships task deep links in the Kanban dashboard bundle.
- Disposition: active

## G-ONESHOT-ISOLATION: explicit zero-tool isolation for oneshot runs
- Commits: fb4f091bb9f62045a79f02551b852b5164e06350, 2904a8044a2d227e0bd82391f43ad66d948c265f, 802c7eb2900f7b1bcdf1e8bc58be1b959ca66729, b701e8ebef483e55ee6834b517b370c36912826b, cddbbe69ffe9efbe54f70f9d5596186ade525465, c24e5c7e2fd5cfca92d31d241afc4934c63f4269, bcfaa77ca624d41ef119ee0f5b5861aa4e629f09
- Owned-Files:
  - agent/agent_init.py
  - hermes_cli/main.py
  - hermes_cli/oneshot.py
  - model_tools.py
  - tests/hermes_cli/test_mcp_startup.py
  - tests/hermes_cli/test_oneshot_skills.py
  - tests/test_model_tools.py
- Intent: A oneshot invocation that requests zero tools gets exactly zero tools: no MCP startup, no builtin tool discovery leaking into the run.
- Protected-Invariant: Explicitly tool-less oneshot runs never load or expose any toolset.
- Tests: tests/hermes_cli/test_oneshot_skills.py, tests/hermes_cli/test_mcp_startup.py, tests/test_model_tools.py
- Retirement-Condition: Upstream enforces explicit zero-tool isolation on the oneshot path.
- Disposition: active

## G-TELEGRAM-MDV2: escape chunk indicators on the standalone Telegram send path
- Commits: 37a9fe9e2c38671e177a47d13f75b6707b2e9cc7
- Owned-Files:
  - tools/send_message_senders.py
  - tests/tools/test_send_message_tool.py
- Intent: `truncate_message` appends raw ` (N/M)` chunk suffixes; bare parentheses are reserved in MarkdownV2, so every chunk of a long report was rejected and delivery fell back to plain text. Mirrors the gateway adapter's escaping on the standalone `_send_telegram` path. Originally landed in `tools/send_message_tool.py`; upstream's decomposition relocated the owned logic to `tools/send_message_senders.py`.
- Protected-Invariant: Multi-chunk MarkdownV2 Telegram sends deliver with formatting intact; chunk indicators are escaped identically on gateway and standalone paths.
- Tests: tests/tools/test_send_message_tool.py
- Retirement-Condition: Upstream escapes chunk indicators on the standalone send path.
- Disposition: active

## G-SKILL-CLAUDE-OAUTH: verify Claude OAuth before delegation, current model examples
- Commits: 6669bcc547aa40dade503aab6f85b5a8479c2d38, 18df376dcdabfffded2d3384fe2169ab4bda014d
- Owned-Files:
  - skills/autonomous-ai-agents/claude-code/SKILL.md
  - tests/skills/test_claude_code_skill.py
- Intent: The claude-code delegation skill verifies Claude CLI OAuth health before dispatching work (an expired login fails fast with a clear remediation) and shows currently configured model examples.
- Protected-Invariant: Delegation to Claude Code never proceeds on an expired/absent OAuth session without surfacing the failure.
- Tests: tests/skills/test_claude_code_skill.py
- Retirement-Condition: Upstream skill gains an equivalent OAuth preflight.
- Disposition: active

## G-DOCS-GITHUB-WORKTREE: worktree cleanup guidance after PR merge
- Commits: cfe5eb78002c55a2a5dabd17578537a5f0aff1c4
- Owned-Files:
  - skills/software-development/github/references/pr-workflow.md
  - website/docs/user-guide/skills/bundled/github/github-github-pr-workflow.md
- Intent: `gh pr merge --squash --delete-branch` can merge remotely yet exit nonzero when the branch is checked out in a local worktree; the skill documents read-back (`gh pr view --json state,...`) before any retry. Originally landed in `skills/github/github-pr-workflow/SKILL.md`; upstream's skill-tree restructure relocated the content to the current path.
- Protected-Invariant: The documented flow never retries a merge that GitHub already reports as MERGED.
- Tests: none (documentation-only; exercised by the github skill's workflow)
- Retirement-Condition: Upstream documentation covers the ambiguous-exit read-back flow.
- Disposition: active

## G-DESKTOP-TEST-ISOLATION: isolate desktop test fixtures and mutex paths
- Commits: e5180f641f630f79e9f9d91105d68a7404be0b0f
- Owned-Files:
  - apps/desktop/electron/git-review-ops.test.ts
  - apps/desktop/electron/git-worktree-ops.test.ts
  - apps/desktop/electron/git-worktree-ops.ts
  - apps/desktop/electron/managed-ssh-update.test.ts
  - apps/desktop/electron/remote-lifecycle.test.ts
  - apps/desktop/electron/remote-lifecycle.ts
  - apps/desktop/'/var/folders/5h/qzgt02rn619fttp2d7zdxj600000gn/T/hermes-update-mutex-LMF9y5/home/.hermes-update-in-progress.mutex'
  - apps/desktop/'/var/folders/5h/qzgt02rn619fttp2d7zdxj600000gn/T/hermes-update-mutex-us8HZu/home/.hermes-update-in-progress.mutex'
- Intent: Desktop electron tests write fixtures and update-mutex files under isolated temp paths instead of the repository tree. The two quoted `/var/folders/...` paths are retained only as historical commit-ownership records; they are not current upstream deletions or current fork deltas.
- Protected-Invariant: No test writes mutex/fixture state into the repository working tree.
- Tests: apps/desktop/electron/git-worktree-ops.test.ts, apps/desktop/electron/remote-lifecycle.test.ts
- Retirement-Condition: Upstream applies equivalent fixture/mutex isolation.
- Disposition: active

## G-ELECTRON-PATCH: Electron patched-release bump
- Commits: 09230048813f1d42d3d3beb79db2d9f23d75ffc3
- Owned-Files:
  - apps/desktop/package.json
  - package.json
  - package-lock.json
- Intent: Track a patched Electron release ahead of upstream's pin to pick up a security fix.
- Protected-Invariant: The desktop app never regresses below the patched Electron version.
- Tests: `npm ls electron --all`; `cd apps/desktop && npm run test:desktop:all` (builds and packages the Electron 41.10.3 runtime)
- Retirement-Condition: Upstream's Electron pin reaches or passes the patched release.
- Disposition: active

## G-NPM-NANOID: nanoid 3 security override
- Commits: none
- Owned-Files:
  - package.json
  - package-lock.json
- Intent: Override transitive nanoid 3.x to 3.3.18 for its security fix while upstream's lockfile still resolves an older release.
- Protected-Invariant: No transitive nanoid 3.x below 3.3.18 in the lockfile.
- Tests: none (lockfile override; enforced by npm resolution)
- Retirement-Condition: Met - current upstream resolves nanoid 3.x to 3.3.18 without a fork override.
- Disposition: absorbed-upstream

## G-DASHBOARD-LOOPBACK: loopback Desktop backends exempt from public_url gate
- Commits: none
- Owned-Files:
  - hermes_cli/web_server.py
  - tests/hermes_cli/test_web_server.py
- Intent: Cherry-pick of upstream 54ee290bc (#96490): a non-loopback `dashboard.public_url` must not engage the ticket-only auth gate for the private loopback backend the Desktop app spawns (loopback bind + HERMES_DESKTOP=1 + operator-minted credential are all required for the exemption).
- Protected-Invariant: The real public dashboard keeps its auth gate; only the Desktop-owned loopback backend is exempt.
- Tests: tests/hermes_cli/test_web_server.py
- Retirement-Condition: Met - 54ee290bc is in `upstream/main` and the fork carries no residual delta on the owned files at the current merge-base; remove this entry after the next clean upstream sync.
- Disposition: absorbed-upstream

## G-SYNC-RESIDUE: retired trailing-newline residue from upstream sync merges
- Commits: none
- Owned-Files:
  - tests/test_engines_satisfiable.py
  - tests/agent/test_turn_finalizer_final_response_persistence.py
  - tests/run_agent/test_tool_call_incremental_persistence.py
- Intent: Historical record of three trailing-newline-only deltas removed by the clean reset to current upstream before reconstruction.
- Protected-Invariant: The residue never carries a behavioral fork change; a non-newline delta on these paths is out of scope for this entry and must get its own owner.
- Tests: none (newline-only residue; no behavioral assertion)
- Retirement-Condition: Met - the clean upstream reset removed all three residual deltas.
- Disposition: absorbed-upstream

## G-FORK-LEDGER: fork change ledger and post-verify checker
- Commits: self
- Ledger-Revision: 9
- Owned-Files:
  - docs/FORK_CHANGES.md
  - scripts/ci/check_fork_ledger.py
  - tests/ci/test_check_fork_ledger.py
  - tests/ci/test_check_fork_ledger_adversarial.py
- Intent: Record every fork-only change and fail closed if a work commit is unmapped, a current path is unowned or ambiguous, or the checker cannot run. `Commits: self` is component-scoped self-mapping for commits that touch only G-FORK-LEDGER files and change this specific entry. Every ledger maintenance commit bumps Ledger-Revision so the authorization is explicit and entry-scoped.
- Protected-Invariant: `self` stays narrow. A commit is mapped only when it changes the ledger and every changed path is owned by G-FORK-LEDGER. A commit that also changes an unrelated path remains unmapped.
- Tests: tests/ci/test_check_fork_ledger.py, tests/ci/test_check_fork_ledger_adversarial.py
- Retirement-Condition: The fork stops carrying local commits and the ledger is no longer required.
- Disposition: active

## Sync-merge residue

The reconstruction reset to upstream merge-base
`3b45681c25a880477a2a806cdebe91d2f1bfe9ce`; no sync-merge residue remains in
the current delta. G-SYNC-RESIDUE records the retired historical newline-only
paths. Future sync-resolution deltas must have explicit ownership; a current
path listed by no entry is `unknown_fork_change` and blocks the resolver.

## Path-Precedence

A path listed by more than one entry has one declared conflict winner. The
named entry must itself list the path, and claims consult that effective owner
even when the path is absent from the current three-dot delta. Current
`path_owners`, unowned, and ambiguous reporting remains limited to current
changed paths. The checker does not choose. Each path may be declared once. A
repeated row fails closed, whether the winner is the same or conflicting, and
is reported in `invalid_precedence`. The winner is the token after the last
`: <ID>`; the preceding path uses the same plain-or-JSON syntax as Owned-Files.

- hermes_cli/main.py: G-ONESHOT-ISOLATION
- package.json: G-ELECTRON-PATCH
- package-lock.json: G-ELECTRON-PATCH

`hermes_cli/main.py` stays listed by G-CRON-DURABLE and G-ONESHOT-ISOLATION.
G-ONESHOT-ISOLATION wins because zero-tool isolation is the fail-closed
tool-surface invariant on that file. `package.json` and `package-lock.json`
stay listed by G-ELECTRON-PATCH and the absorbed G-NPM-NANOID history.
G-ELECTRON-PATCH now wins because every current delta on those files belongs
to the Electron 41.10.3 dependency graph; nanoid 3.3.18 matches upstream.
