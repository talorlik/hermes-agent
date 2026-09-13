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
changes on merge or squash. G-FORK-LEDGER may explicitly retire ancestry through
`History-Reconciliations`: each listed canonical commit must be a two-parent
first-parent-chain merge whose raw tree equals its first parent's tree, whose
retired parent shares only official-upstream roots, and whose retired set is
disjoint from every other partition. Every fork-introduced change to that
field is a G-FORK-LEDGER-owned commit whose Ledger-Revision strictly exceeds
the prior full-reachable revision high-water mark seeded at the upstream
boundary. Unchanged inherited authorization creates no new ownership or
in-range obligation. After first activation, the entry and field cannot
disappear; replacement entries and explicit claims cannot reset its identity.

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
- Commits: 08f93e614a62790a6bd29c1397c603c2181f1161, 21a38d4dc2b9301a75ac219da8f19b103813ec6d, 7de663b05eaf30157e51e795d008ed526a52c785, 5f6aeab598ad280d33903e10e4158397220bf264, b63893c1ac74800637b70efd0e74c07fcbe9175d, 2e2a5082f58f01addb00a3b60646da5dd7555e4c, 2f79ad2996d8d6d62736942ce65c88c50cf32a55, 6a00376ed41f2ec30802c30cfa07c9a7858abaf7
- Owned-Files:
  - hermes_cli/update_cmd.py
  - hermes_cli/update_cmd_fleet.py
  - hermes_cli/update_cmd_git.py
  - hermes_cli/update_receipt.py
  - hermes_cli/config_defaults.py
  - tests/hermes_cli/test_cmd_update.py
  - tests/hermes_cli/test_fork_sync_strategy.py
  - tests/hermes_cli/test_update_fleet_restart_pending.py
  - tests/hermes_cli/test_update_head_moved_gate.py
  - tests/hermes_cli/test_update_orphan_backend_reap.py
  - tests/hermes_cli/test_update_skip_unchanged_editable_install.py
  - tests/hermes_cli/test_update_venv_health.py
  - tests/hermes_cli/test_update_yes_flag.py
- Intent: `updates.fork_sync_strategy: merge` makes `hermes update` merge `upstream/main` into `main` (fork commits preserved, recovery tag before merge, post-merge syntax guard with rollback, candidate test run before push, no-op pull accepted). Default stays `ff_only`; unknown values fall back to `ff_only`.
- Protected-Invariant: An update never force-pushes or rewrites `main`; a conflict or a post-merge syntax/test failure aborts with the pre-merge SHA restored and nothing pushed; upstream code merged by the sync cannot reach origin unchecked.
- Tests: tests/hermes_cli/test_fork_sync_strategy.py, tests/hermes_cli/test_cmd_update.py
- Retirement-Condition: Upstream ships an equivalent fork-sync merge strategy in `hermes update`, or the fork stops carrying local commits (P5/P6 cutover replaces the mechanism with `cc_hermes_update` + `cc_resolve_upstream_conflict` and upstream absorbs the remainder).
- Disposition: active

## G-CRON-DURABLE: durable scheduler outcomes, detached runs, fail-closed scripts
- Commits: 82867a2110f6cfa500913f34fa3984f11ab8222b, c25ff2c605ddaf51941a69593e9e11a7abcd9b83, 9d9273e016485af63cad0bb0ebdb8f86469d8e98, 161b03e4d05d955b7246e7536d8e122fcafd1403, ed8cbe13f277229896b7c03945152891a7d35c83, 075205dd17d1c09a43d3332d9cdd3002baafd891, 424f8b423fe798eb9660b606940a322cea718d57, d87a667d7d1c87891f4618a336263cfa383e1bd0, dd36cd7a9d0759be4d36546254f4f85327ccf49a, 2bd5dea2ae19009478e1618cc14043c3e7253d61, 6a19104b72543483923aa619cdfc09f6d246b125, f7ed2fdfc31fba4e1c53068c3d9cc6974c1719e1, fbde468e17a1cc57199d6e03fe69a44eb7b90722, b416b65b79acc4fd8e1143adab428b16d416e96a
- Owned-Files:
  - cron/deferrals.py
  - cron/delivery_queue.py
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
  - tests/cron/test_cron_live_bot_delivery.py
  - tests/cron/test_delivery_crash_boundary.py
  - tests/cron/test_delivery_queue.py
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
- Commits: 97a8adab283e5b3e292d1cca4dd019149c62d544, b14684474269d1e7b32a881fdf387fa6b85ca13f, 690611dd293b2ebea1d98628f4e7d8a3cf7de14f, 4996728aa28bfc3ac1b05b42d6625c6af79b8138, f496346d2ee17a48295de9f1140108bc4ff35942, f8443d665d5deba0177057559d987d0a116a5314, 18e0b13a50e23f70d67aa345ebbc22be79f82d50, e742bfb874138deca7d797d941a1b834ff067f76, 39a6be8ba3339a26b73f637bde3785cf0781cbed, 49019067125ff63a3be2d65aaf37f7469dc630b0, 9a27c914ae3fd0d9700946e806fb038b61ecf288, 66b92833a99cc2e9176809b24ca63b67d0b467b5, 7adbe4ecbfefa5796995149aae49d69e9bb8fb4d, cbd0d6ba095a0ac5064c25c66cdc77d3a4423659, bac93d67aed81f5d36fd0974259539bb86e8efba
- Owned-Files:
  - hermes_cli/kanban.py
  - hermes_cli/kanban_db.py
  - hermes_cli/kanban_db_connect.py
  - hermes_cli/kanban_parser.py
  - apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx
  - plugins/kanban/dashboard/dist/index.js
  - plugins/kanban/dashboard/dist/style.css
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
  - website/docs/user-guide/features/kanban.md
- Intent: Protect durable Kanban lifecycle transitions and serve the board dashboard's task deep links and bounded schema-v2 orchestration summaries without coupling board operations to a producer.
- Protected-Invariant: Stale lifecycle transitions fail closed; replayed claims and guarded comments are idempotent; summary paths stay profile-confined and unsafe or inconsistent files remain panel-local; stale asynchronous responses cannot overwrite the selected board.
- Tests: tests/hermes_cli/test_kanban_db.py, tests/hermes_cli/test_kanban_cli_exit_status.py, tests/tools/test_kanban_tools.py, tests/plugins/test_kanban_dashboard_plugin.py, apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx
- Retirement-Condition: Upstream ships equivalent Kanban lifecycle guards, task deep links, and a profile-safe schema-v2 board-summary endpoint and panel.
- Disposition: active

## G-ONESHOT-ISOLATION: explicit zero-tool isolation for oneshot runs
- Commits: 32c89389f6160452984c9cf8a19b935970489935, d14ebfd1076ed44af75ddd7c70b849bc934b66b9, 5426f0f3125703d2f4275394e0dad549c1875642, deaea591f92190e1317ea0c6d4970f8df5edce78, cd71ff7d1fcd4cb34e9e805b4fba6c56a45a419c, 6b929aebd4a424be5c6a5cac1b30efcc28b1611f, 70e3bdefe385b2f0e9597233c2cff9b0f00f7b41, a19b189a244bcf9f5299e33ba6cc84ebd6fae8e4
- Owned-Files:
  - agent/agent_init.py
  - hermes_cli/_parser.py
  - hermes_cli/main.py
  - hermes_cli/oneshot.py
  - model_tools.py
  - tests/hermes_cli/test_mcp_startup.py
  - tests/hermes_cli/test_oneshot_skills.py
  - tests/hermes_cli/test_startup_plugin_gating.py
  - tests/test_model_tools.py
- Intent: A oneshot invocation that requests zero tools gets exactly zero tools: no MCP startup, no builtin tool discovery leaking into the run.
- Protected-Invariant: Explicitly tool-less oneshot runs never load or expose any toolset.
- Tests: tests/hermes_cli/test_oneshot_skills.py, tests/hermes_cli/test_mcp_startup.py, tests/test_model_tools.py
- Retirement-Condition: Upstream enforces explicit zero-tool isolation on the oneshot path.
- Disposition: active

## G-TELEGRAM-MDV2: escape chunk indicators on the standalone Telegram send path
- Commits: 8b89f7f69a3f10dda431cb1ac177e2f949c2aa60
- Owned-Files:
  - tools/send_message_senders.py
  - tests/tools/test_send_message_tool.py
- Intent: `truncate_message` appends raw ` (N/M)` chunk suffixes; bare parentheses are reserved in MarkdownV2, so every chunk of a long report was rejected and delivery fell back to plain text. Mirrors the gateway adapter's escaping on the standalone `_send_telegram` path. Originally landed in `tools/send_message_tool.py`; upstream's decomposition relocated the owned logic to `tools/send_message_senders.py`.
- Protected-Invariant: Multi-chunk MarkdownV2 Telegram sends deliver with formatting intact; chunk indicators are escaped identically on gateway and standalone paths.
- Tests: tests/tools/test_send_message_tool.py
- Retirement-Condition: Upstream escapes chunk indicators on the standalone send path.
- Disposition: active

## G-SKILL-CLAUDE-OAUTH: verify Claude OAuth before delegation, current model examples
- Commits: 016208fc9758e047e289044ce2594dd0f9750378, b47a5ffa51fd38d793dc19d230691b0c824f721a
- Owned-Files:
  - skills/autonomous-ai-agents/claude-code/SKILL.md
  - tests/skills/test_claude_code_skill.py
- Intent: The claude-code delegation skill verifies Claude CLI OAuth health before dispatching work (an expired login fails fast with a clear remediation) and shows currently configured model examples.
- Protected-Invariant: Delegation to Claude Code never proceeds on an expired/absent OAuth session without surfacing the failure.
- Tests: tests/skills/test_claude_code_skill.py
- Retirement-Condition: Upstream skill gains an equivalent OAuth preflight.
- Disposition: active

## G-DOCS-GITHUB-WORKTREE: worktree cleanup guidance after PR merge
- Commits: 0dc6cfe4bd2c33fc3d1cf5e5ea813f19ad5ccd91
- Owned-Files:
  - skills/software-development/github/references/pr-workflow.md
  - website/docs/user-guide/skills/bundled/github/github-github-pr-workflow.md
- Intent: `gh pr merge --squash --delete-branch` can merge remotely yet exit nonzero when the branch is checked out in a local worktree; the skill documents read-back (`gh pr view --json state,...`) before any retry. Originally landed in `skills/github/github-pr-workflow/SKILL.md`; upstream's skill-tree restructure relocated the content to the current path.
- Protected-Invariant: The documented flow never retries a merge that GitHub already reports as MERGED.
- Tests: none (documentation-only; exercised by the github skill's workflow)
- Retirement-Condition: Upstream documentation covers the ambiguous-exit read-back flow.
- Disposition: active

## G-DESKTOP-TEST-ISOLATION: isolate desktop test fixtures and mutex paths
- Commits: 23435ad9c87c62e313d6bd68abd95a6ff4382b46
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
- Commits: 63a29f4ca9f11bbc25e8cac0bfcf732939a2bcb4
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
- Ledger-Revision: 17
- History-Reconciliations: 66f9ff9229aef76ab980329544294d287ed70ea7
- Owned-Files:
  - docs/FORK_CHANGES.md
  - scripts/ci/check_fork_ledger.py
  - tests/ci/test_check_fork_ledger.py
  - tests/ci/test_check_fork_ledger_adversarial.py
- Intent: Record every fork-only change and fail closed if a work commit is unmapped, a current path is unowned or ambiguous, or the checker cannot run. `Commits: self` is component-scoped self-mapping for commits that touch only G-FORK-LEDGER files and change this specific entry. Every ledger maintenance commit bumps Ledger-Revision so the authorization is explicit and entry-scoped. `History-Reconciliations` authorizes only audited zero-tree ancestry links needed for non-force publication after a history reconstruction.
- Protected-Invariant: `self` stays narrow. A commit is mapped only when it changes the ledger and every changed path is owned by G-FORK-LEDGER. A commit that also changes an unrelated path remains unmapped. History reconciliation cannot hide first-parent work, upstream work, current-tree changes, replacement-forged objects, unrelated roots, overlapping retired sets, inherited activation state, full-reachable revision high-water marks, commit-time Path-Precedence, oversized revisions, or sticky entry and History-Reconciliations removal.
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
