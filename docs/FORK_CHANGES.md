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
- Commits: d1066f3439f937acc68b6190aa9724ef0b66e130, 4e7e4488dafd2e43a8a96b27d0460a41be6d12bc, 76c7a422884a48b84a6c9fea5e8f3408cf8f6bd5, 239e3f5bf5892cd3cb14225394db2765d74b7db0, f95f8b4a76b74ee78a4a81e0d6f92fd0cc83140e, 244763a48d0a57be66d53bdd2831a898251af67a
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
- Commits: 558528c06d72382e0f22eefc397429762d7b0ac6, 859599f4ffaea4c86a8a335de2f2ed4de0b65d04, 6b6f4d414e12557801e3057f12b687cef7a615c7, 1c0291cce81a81248fb7c5727b2a10dec0a19b9a, 1e0e3e2fbba930bae94ae225ec7fde5e27cfb45e, 0cd04128b2560cd428e5ba67089ab9ad2039da1e, 0861fa470a76a4bb7070c9d034eb60e288f6df71, d63999668f8e45dc24c81c9abddbed99594cc9dd
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
- Commits: dcacabd74f2677ba7bcef2384a8ad5b70a87a523, 062f119e2907f40142f6ef5f549e1ebea8b1fe2d, 6c4985b8283306ed75cbfe7ee8e25a2d603ccdd1, 67e926c3b1972609f89acb3455645f287b2a5f34, 9a2fb822aa4032947675c27bd9a0fa9b55d8ab27, 973611b856b02fb0f1b2c5901d4d883e8629af12, 22f76698544c5260b3f9087df55da81b5ad1bd8e, 1d7b3834d9bb58a13d20d7f724a01e6c7b4d3bfc, a3589d26cb6ab71cecd8168a3e34963611b62c09, 70f70d6313b8b01b94c88b481ddf8ddb87504cf8, 3051b46850fa33ef5f837cdeef92a47fc2c08434
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
- Commits: 71c5d069176de74357ad6d181a134be645a73d2d, 94ce0f54c853810416de945b1b898f5295135300
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
- Commits: a4915b3e095ce7eac3236dad84c844fa8bcb210f, ed1289aefd1f2f86ea904d4a5facc1b606306893, 6ad540c3bb9586064c4afdce2893a1423f380a86, 0f8f1120af7548bb52339dcefa5582636fd83d78, 6277c0e77aaf0eb9636c046e004fffcb0e4e5d9e, 13cc5c9f5419994f334404a7b4b4370b510c16b1
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
- Commits: 34971a47930f614d8614f4e759430a9946a7240c
- Owned-Files:
  - tools/send_message_senders.py
  - tests/tools/test_send_message_tool.py
- Intent: `truncate_message` appends raw ` (N/M)` chunk suffixes; bare parentheses are reserved in MarkdownV2, so every chunk of a long report was rejected and delivery fell back to plain text. Mirrors the gateway adapter's escaping on the standalone `_send_telegram` path. Originally landed in `tools/send_message_tool.py`; upstream's decomposition relocated the owned logic to `tools/send_message_senders.py`.
- Protected-Invariant: Multi-chunk MarkdownV2 Telegram sends deliver with formatting intact; chunk indicators are escaped identically on gateway and standalone paths.
- Tests: tests/tools/test_send_message_tool.py
- Retirement-Condition: Upstream escapes chunk indicators on the standalone send path.
- Disposition: active

## G-SKILL-CLAUDE-OAUTH: verify Claude OAuth before delegation, current model examples
- Commits: cbd09bc001c3075269f887328bc05717d33e3d6a, 73a82ca13ede374ada35428e5a5e2e54ec7b29fc
- Owned-Files:
  - skills/autonomous-ai-agents/claude-code/SKILL.md
  - tests/skills/test_claude_code_skill.py
- Intent: The claude-code delegation skill verifies Claude CLI OAuth health before dispatching work (an expired login fails fast with a clear remediation) and shows currently configured model examples.
- Protected-Invariant: Delegation to Claude Code never proceeds on an expired/absent OAuth session without surfacing the failure.
- Tests: tests/skills/test_claude_code_skill.py
- Retirement-Condition: Upstream skill gains an equivalent OAuth preflight.
- Disposition: active

## G-DOCS-GITHUB-WORKTREE: worktree cleanup guidance after PR merge
- Commits: ebc1472900d691109707686e9c5b65e3f6beb320
- Owned-Files:
  - skills/software-development/github/references/pr-workflow.md
  - website/docs/user-guide/skills/bundled/github/github-github-pr-workflow.md
- Intent: `gh pr merge --squash --delete-branch` can merge remotely yet exit nonzero when the branch is checked out in a local worktree; the skill documents read-back (`gh pr view --json state,...`) before any retry. Originally landed in `skills/github/github-pr-workflow/SKILL.md`; upstream's skill-tree restructure relocated the content to the current path.
- Protected-Invariant: The documented flow never retries a merge that GitHub already reports as MERGED.
- Tests: none (documentation-only; exercised by the github skill's workflow)
- Retirement-Condition: Upstream documentation covers the ambiguous-exit read-back flow.
- Disposition: active

## G-DESKTOP-TEST-ISOLATION: isolate desktop test fixtures and mutex paths
- Commits: af99756b24f6676aeb4b7535b3f524b922b64c09
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
- Commits: dcd8af7fda4696e2585bcdc9c2286f1220ec890e
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
- Ledger-Revision: 5
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
