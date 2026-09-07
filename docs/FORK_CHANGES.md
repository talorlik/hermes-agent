# Fork Change Ledger

Auditable record of every fork-only change `talorlik/hermes-agent` carries on
top of `NousResearch/hermes-agent` (`upstream`, pull-only). Required by
ARD-010 (Central Command orchestration plan): the autonomous upstream
conflict resolver (`cc_resolve_upstream_conflict`) resolves conflicted files
by ledger ownership and never guesses; a conflicted file with no owning entry
is `unknown_fork_change` and blocks the merge.

Verification: `python scripts/ci/check_fork_ledger.py` (the weekly
`post_verify` gate). It classifies `upstream/main..main`: merge commits with
a parent reachable from `upstream/main` are upstream sync merges (kept
intact by `updates.fork_sync_strategy: merge`; exempt from mapping), and
every remaining work commit must be claimed by exactly one entry. A Commits
field may list SHAs, `<sha>..<sha>` ranges, `none` (path ownership with no
commit claim), or `self`. `self` is component-scoped self-mapping, not a
singular delivery-commit special case. On G-FORK-LEDGER it maps any future
work commit that touches this ledger and only files that entry owns. It
stays narrow because the commit must change the ledger and every changed
path must already be listed under that entry. A commit that also changes
an unrelated path remains unmapped. Do not put a branch-only SHA in
G-FORK-LEDGER; that SHA changes on merge or squash.

Current-path ownership is the three-dot `upstream/main...fork-ref` changed
path set, including deletions. Quoted or unusual paths are parsed from
`git diff -z` and are never guessed. Every current path needs exactly one
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
- Commits: b912e24a2b0530377af9ef264acbe0370f779888, 2a1bd2afd876723742409bf4f3fe47041c37bb88, 5a40fd383afaacdabfbfb69ceff95004c97d8cad, f63a96490d2b9e5f2465c0a4574bb17dab061433, 9185cf31fae8d3510faf336ac90c0b88bafcd913, 00c40874ef5e2b9623f74823ff043358af96a112
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
- Commits: fac305f63d28858a8902c2c49ef24f0ae339a81f, 7b68d1a7e947308c96efb6cc180794a9b5093454, f62bbe1598b585117582f272ef5a59fcd97df29c
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
- Commits: d00ab89f5377792d6f1e2765b1ee4f07a71cbe73, 18291e16e48a97f24c05bba1feb262d7f5551e63, 697d6a9bf197bcf2e2c0b0d87cbd108cd4a294a8, e8d5209e9f9559736c9bffbc9b090b2571b7f7e9, 1d5432e9da846bd82808be16a6ba0917e9672823
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
- Commits: 387b81b8bc1a748aee5d4c07c780aec89b031428, a3d643f82e489d72a41aa2846fd217f5f38a14a3
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
- Commits: 9a2578acbce2fb5c7c98bc18561cd749616110fe
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
- Commits: 52d377930d988dcd7271131b18ba2c41813070c3
- Owned-Files:
  - tools/send_message_senders.py
  - tests/tools/test_send_message_tool.py
- Intent: `truncate_message` appends raw ` (N/M)` chunk suffixes; bare parentheses are reserved in MarkdownV2, so every chunk of a long report was rejected and delivery fell back to plain text. Mirrors the gateway adapter's escaping on the standalone `_send_telegram` path. Originally landed in `tools/send_message_tool.py`; upstream's decomposition relocated the owned logic to `tools/send_message_senders.py`.
- Protected-Invariant: Multi-chunk MarkdownV2 Telegram sends deliver with formatting intact; chunk indicators are escaped identically on gateway and standalone paths.
- Tests: tests/tools/test_send_message_tool.py
- Retirement-Condition: Upstream escapes chunk indicators on the standalone send path.
- Disposition: active

## G-SKILL-CLAUDE-OAUTH: verify Claude OAuth before delegation, current model examples
- Commits: 7247098db8298fcc4c439ae790a7fe8c80b06d4b, 34126507c176de35a5263dcbd4a2a1e01bfe174b
- Owned-Files:
  - skills/autonomous-ai-agents/claude-code/SKILL.md
  - tests/skills/test_claude_code_skill.py
- Intent: The claude-code delegation skill verifies Claude CLI OAuth health before dispatching work (an expired login fails fast with a clear remediation) and shows currently configured model examples.
- Protected-Invariant: Delegation to Claude Code never proceeds on an expired/absent OAuth session without surfacing the failure.
- Tests: tests/skills/test_claude_code_skill.py
- Retirement-Condition: Upstream skill gains an equivalent OAuth preflight.
- Disposition: active

## G-DOCS-GITHUB-WORKTREE: worktree cleanup guidance after PR merge
- Commits: ab0f436f78f7697df4ad1b1825f0fa4e15f03b2c
- Owned-Files:
  - skills/software-development/github/references/pr-workflow.md
  - website/docs/user-guide/skills/bundled/github/github-github-pr-workflow.md
- Intent: `gh pr merge --squash --delete-branch` can merge remotely yet exit nonzero when the branch is checked out in a local worktree; the skill documents read-back (`gh pr view --json state,...`) before any retry. Originally landed in `skills/github/github-pr-workflow/SKILL.md`; upstream's skill-tree restructure relocated the content to the current path.
- Protected-Invariant: The documented flow never retries a merge that GitHub already reports as MERGED.
- Tests: none (documentation-only; exercised by the github skill's workflow)
- Retirement-Condition: Upstream documentation covers the ambiguous-exit read-back flow.
- Disposition: active

## G-DESKTOP-TEST-ISOLATION: isolate desktop test fixtures and mutex paths
- Commits: c6261bf2ca60fc5d13ed4480a5bb6124480f4833
- Owned-Files:
  - apps/desktop/electron/git-review-ops.test.ts
  - apps/desktop/electron/git-worktree-ops.test.ts
  - apps/desktop/electron/git-worktree-ops.ts
  - apps/desktop/electron/managed-ssh-update.test.ts
  - apps/desktop/electron/remote-lifecycle.test.ts
  - apps/desktop/electron/remote-lifecycle.ts
  - apps/desktop/'/var/folders/5h/qzgt02rn619fttp2d7zdxj600000gn/T/hermes-update-mutex-LMF9y5/home/.hermes-update-in-progress.mutex'
  - apps/desktop/'/var/folders/5h/qzgt02rn619fttp2d7zdxj600000gn/T/hermes-update-mutex-us8HZu/home/.hermes-update-in-progress.mutex'
- Intent: Desktop electron tests write their fixtures and update-mutex files under isolated temp paths instead of the repository tree (quoted `/var/folders/...` literals had previously been committed as junk files). This entry also owns the two deleted quoted mutex-junk paths still present on `upstream/main`, so the deletion is not an unowned current path.
- Protected-Invariant: No test writes mutex/fixture state into the repository working tree.
- Tests: apps/desktop/electron/git-worktree-ops.test.ts, apps/desktop/electron/remote-lifecycle.test.ts
- Retirement-Condition: Upstream applies equivalent fixture/mutex isolation.
- Disposition: active

## G-ELECTRON-PATCH: Electron patched-release bump
- Commits: 14f336638db90a57535b6359ca358c436fde66a4
- Owned-Files:
  - apps/desktop/package.json
  - package.json
  - package-lock.json
- Intent: Track a patched Electron release ahead of upstream's pin to pick up a security fix.
- Protected-Invariant: The desktop app never regresses below the patched Electron version.
- Tests: apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx (bundle still builds/runs under the bumped Electron)
- Retirement-Condition: Upstream's Electron pin reaches or passes the patched release.
- Disposition: active

## G-NPM-NANOID: nanoid 3 security override
- Commits: 5fc6f78d22bf89e7e8587903c2738696e5674b01
- Owned-Files:
  - package.json
  - package-lock.json
- Intent: Override transitive nanoid 3.x to 3.3.18 for its security fix while upstream's lockfile still resolves an older release.
- Protected-Invariant: No transitive nanoid 3.x below 3.3.18 in the lockfile.
- Tests: none (lockfile override; enforced by npm resolution)
- Retirement-Condition: Upstream lockfile resolves nanoid >= 3.3.18 without the override.
- Disposition: active

## G-DASHBOARD-LOOPBACK: loopback Desktop backends exempt from public_url gate
- Commits: b227b6cef9d11f3f7b41b8ff947facf5a117e2f1
- Owned-Files:
  - hermes_cli/web_server.py
  - tests/hermes_cli/test_web_server.py
- Intent: Cherry-pick of upstream 54ee290bc (#96490): a non-loopback `dashboard.public_url` must not engage the ticket-only auth gate for the private loopback backend the Desktop app spawns (loopback bind + HERMES_DESKTOP=1 + operator-minted credential are all required for the exemption).
- Protected-Invariant: The real public dashboard keeps its auth gate; only the Desktop-owned loopback backend is exempt.
- Tests: tests/hermes_cli/test_web_server.py
- Retirement-Condition: Met - 54ee290bc is in `upstream/main` and the fork carries no residual delta on the owned files at the current merge-base; remove this entry after the next clean upstream sync.
- Disposition: absorbed-upstream

## G-SYNC-RESIDUE: trailing-newline residue from upstream sync merges
- Commits: none
- Owned-Files:
  - tests/test_engines_satisfiable.py
  - tests/agent/test_turn_finalizer_final_response_persistence.py
  - tests/run_agent/test_tool_call_incremental_persistence.py
- Intent: These three files differ from `upstream/main` only by trailing-newline residue left by an upstream sync merge. They belong to no work commit. Ownership is explicit so the resolver does not guess: on conflict, take upstream's side and drop the newline-only fork residue.
- Protected-Invariant: The residue never carries a behavioral fork change; a non-newline delta on these paths is out of scope for this entry and must get its own owner.
- Tests: none (newline-only residue; no behavioral assertion)
- Retirement-Condition: The next clean upstream sync shows no residual delta on these three paths.
- Disposition: retiring

## G-FORK-LEDGER: fork change ledger and post-verify checker
- Commits: self
- Owned-Files:
  - docs/FORK_CHANGES.md
  - scripts/ci/check_fork_ledger.py
  - tests/ci/test_check_fork_ledger.py
- Intent: Record every fork-only change and fail closed if a work commit is unmapped, a current path is unowned or ambiguous, or the checker cannot run. `Commits: self` is component-scoped self-mapping for any future commit that touches the ledger and only files owned by G-FORK-LEDGER, not a singular delivery-commit special case.
- Protected-Invariant: `self` stays narrow. A commit is mapped only when it changes the ledger and every changed path is owned by G-FORK-LEDGER. A commit that also changes an unrelated path remains unmapped.
- Tests: tests/ci/test_check_fork_ledger.py
- Retirement-Condition: The fork stops carrying local commits and the ledger is no longer required.
- Disposition: active

## Sync-merge residue

Upstream sync merges (exempt merge commits) can leave small fork-side
resolution deltas that belong to no work commit. Current known residue at
merge-base 9dd6634c56: trailing-newline-only diffs in the three paths owned
by G-SYNC-RESIDUE, and fleet-restart monkeypatch additions in
`tests/hermes_cli/test_update_fleet_restart_pending.py` (owned by
G-UPDATE-FORKSYNC). The two deleted quoted mutex-junk paths are owned by
G-DESKTOP-TEST-ISOLATION. A current path listed by no entry is
`unknown_fork_change` and fails this checker; the conflict resolver still
blocks rather than guessing.

## Path-Precedence

A current path listed by more than one entry has exactly one conflict winner.
The named entry must itself list the path. The checker does not choose.
Each path may be declared once. A repeated row fails closed, whether the
winner is the same or conflicting, and is reported in `invalid_precedence`.
The winner is the token after the last `: `, so a colon or quoted path is
not truncated.

- hermes_cli/main.py: G-ONESHOT-ISOLATION
- package.json: G-NPM-NANOID
- package-lock.json: G-NPM-NANOID

`hermes_cli/main.py` stays listed by G-CRON-DURABLE and G-ONESHOT-ISOLATION.
G-ONESHOT-ISOLATION wins because zero-tool isolation is the fail-closed
tool-surface invariant on that file. `package.json` and `package-lock.json`
stay listed by G-ELECTRON-PATCH and G-NPM-NANOID. G-NPM-NANOID wins so a pin
reconciliation cannot drop the nanoid security floor.
