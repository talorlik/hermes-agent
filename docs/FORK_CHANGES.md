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
order and its tree equals `git merge-tree --write-tree`. A conflicted merge
is also exempt when every content conflict keeps the upstream lines and any
extra line comes from the fork. A wholesale fork file is not exempt. A
directory-rename suggestion may retain the original source; other conflicted or manually altered merges
remain mapped work. Every work commit must be
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
- Commits: 074b4b9dcc6c281619e357749397f69e9fe631e9, 08cbe89e17662e79a2290a4e051cb114b64cf072, b9776c3870ee6938ac2806eeb08c3eeccad1bfd1, 2d75df969f923ca86f8360ae51ac86d492825391
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
  - tests/hermes_cli/test_update_existing_branch_baseline.py
  - tests/hermes_cli/test_update_fleet_check_fail_closed.py
- Intent: `updates.fork_sync_strategy: merge` makes `hermes update` merge `upstream/main` into `main` (fork commits preserved, recovery tag before merge, post-merge syntax guard with rollback, candidate test run before push, no-op pull accepted). Default stays `ff_only`; unknown values fall back to `ff_only`.
- Protected-Invariant: An update never force-pushes or rewrites `main`; a conflict or a post-merge syntax/test failure aborts with the pre-merge SHA restored and nothing pushed; upstream code merged by the sync cannot reach origin unchecked.
- Tests: tests/hermes_cli/test_fork_sync_strategy.py, tests/hermes_cli/test_cmd_update.py
- Retirement-Condition: Upstream ships an equivalent fork-sync merge strategy in `hermes update`, or the fork stops carrying local commits.
- Disposition: active

## G-CRON-DURABLE: durable scheduler outcomes, detached runs, fail-closed scripts
- Commits: d7cf45920d38a9823c03562a97494c25ca5fea61, 4a7f53254e4b3b8702e7d0c3225f3897a1bdcee7
- Owned-Files:
  - cron/deferrals.py
  - cron/delivery_queue.py
  - cron/outcomes.py
  - cron/executions.py
  - cron/incidents.py
  - cron/jobs.py
  - cron/monitor.py
  - cron/model_drift_compat.py
  - cron/outbox.py
  - cron/scheduler.py
  - cron/scheduler_delivery.py
  - cron/scheduler_delivery_run.py
  - cron/scheduler_outcomes.py
  - cron/scheduler_script.py
  - cron/scheduler_tick.py
  - cron/unreachable_retry.py
  - hermes_cli/cron.py
  - hermes_cli/subcommands/cron.py
  - hermes_cli/main.py
  - tools/cronjob_tools.py
  - tools/cronjob_job_args.py
  - tests/cron/test_deferred_obligations.py
  - tests/cron/test_delivery_no_unawaited_coroutines.py
  - tests/cron/test_delivery_outbox.py
  - tests/cron/test_cron_live_bot_delivery.py
  - tests/cron/test_cron_live_delivery_confirmation.py
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
  - tests/cron/test_model_drift_compat.py
  - tests/cron/test_script_claim_heartbeat.py
  - tests/cron/test_unreachable_retry.py
  - tests/cron/g01c_managed_store_fixture.py
  - tests/cron/test_bot_chat_timeout_marker.py
  - tests/cron/test_cron_script.py
  - tests/cron/test_deferral_production_contract.py
  - tests/cron/test_delivery_generation_atomicity.py
  - tests/cron/test_delivery_generation_producer.py
  - tests/cron/test_execution_retention_policy.py
  - tests/cron/test_g01c_causal_boundary_regressions.py
  - tests/cron/test_gateway_startup_import_closure.py
  - tests/cron/test_jobs_delivery_projection.py
  - tests/cron/test_jobs_import_contract.py
  - tests/cron/test_jobs_syntax.py
  - tests/cron/test_monitor_kind.py
  - tests/cron/test_warning_execution_outcome.py
  - tests/hermes_cli/test_cron.py
  - tests/hermes_cli/test_cron_exit_code_propagation.py
  - website/docs/user-guide/features/cron.md
- Intent: Typed durable outcomes and an execution ledger for scheduled jobs, detached runs that survive gateway restarts with propagated finalization exit codes, delivery outbox, incident lifecycle, fail-closed handling of script errors (a failing pre/post script fails the run instead of silently passing), and a merge-safe fallback for resolve_cron_model_drift_defaults (cron/model_drift_compat.py) so gateway bootstrap remains operational when upstream removes the function from hermes_cli.config.
- Protected-Invariant: A scheduled job's outcome is always durably recorded; script failure never reports success; detached finalization exit codes reach the caller; upstream schema migrations must not drop the executions ledger.
- Tests: tests/cron/, tests/hermes_cli/test_cron.py, tests/hermes_cli/test_cron_exit_code_propagation.py
- Retirement-Condition: Conductor cutover completes and P6-B retires `cron/deferrals.py` and `cron/outcomes.py` after confirming no remaining caller; the rest retires if upstream ships equivalent durable-outcome semantics.
- Disposition: active

## G-KANBAN-LIFECYCLE: durable Kanban lifecycle contracts and CAS guards
- Commits: 1ab971057377815a36ce2c521dd794d9d1798f4f, e84910466496b942b0a9bada5e455ad16a0a8d68, f45949d4f4f751209c41e29f8623e3f898c916cf
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
  - tests/tools/test_kanban_tools_parse_contract.py
  - website/docs/user-guide/features/kanban.md
- Intent: Protect durable Kanban lifecycle transitions and serve the board dashboard's task deep links and bounded schema-v2 orchestration summaries without coupling board operations to a producer.
- Protected-Invariant: Stale lifecycle transitions fail closed; replayed claims and guarded comments are idempotent; summary paths stay profile-confined and unsafe or inconsistent files remain panel-local; stale asynchronous responses cannot overwrite the selected board.
- Tests: tests/hermes_cli/test_kanban_db.py, tests/hermes_cli/test_kanban_cli_exit_status.py, tests/tools/test_kanban_tools.py, tests/plugins/test_kanban_dashboard_plugin.py, apps/desktop/src/plugins/kanban/dashboard-bundle.test.tsx
- Retirement-Condition: Upstream ships equivalent Kanban lifecycle guards, task deep links, and a profile-safe schema-v2 board-summary endpoint and panel.
- Disposition: active

## G-ONESHOT-ISOLATION: explicit zero-tool isolation for oneshot runs
- Commits: 1c6bc9f3afd9efd69dc3a0a0d1a8fbfeed6036cc, 4b1fe5f7723202224e13d62b91825532b8b84aba, 9a1ce849032259bf36c545e85db96fe86a864db3
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
  - tests/tools/test_model_tools.py
- Intent: A oneshot invocation that requests zero tools gets exactly zero tools: no MCP startup, no builtin tool discovery leaking into the run.
- Protected-Invariant: Explicitly tool-less oneshot runs never load or expose any toolset.
- Tests: tests/hermes_cli/test_oneshot_skills.py, tests/hermes_cli/test_mcp_startup.py, tests/test_model_tools.py
- Retirement-Condition: Upstream enforces explicit zero-tool isolation on the oneshot path.
- Disposition: active

## G-TELEGRAM-MDV2: escape chunk indicators on the standalone Telegram send path
- Commits: 2e8d488bf455889389f9e9f93c3f6f59bacb24e9
- Owned-Files:
  - tools/send_message_senders.py
  - tests/tools/test_send_message_tool.py
- Intent: `truncate_message` appends raw ` (N/M)` chunk suffixes; bare parentheses are reserved in MarkdownV2, so every chunk of a long report was rejected and delivery fell back to plain text. Mirrors the gateway adapter's escaping on the standalone `_send_telegram` path. Originally landed in `tools/send_message_tool.py`; upstream's decomposition relocated the owned logic to `tools/send_message_senders.py`.
- Protected-Invariant: Multi-chunk MarkdownV2 Telegram sends deliver with formatting intact; chunk indicators are escaped identically on gateway and standalone paths.
- Tests: tests/tools/test_send_message_tool.py
- Retirement-Condition: Upstream escapes chunk indicators on the standalone send path.
- Disposition: active

## G-SKILL-CLAUDE-OAUTH: verify Claude OAuth before delegation, current model examples
- Commits: 7092df93ffe4bb3cbc8984494cd27e4bca3ed2b3
- Owned-Files:
  - skills/autonomous-ai-agents/claude-code/SKILL.md
  - tests/skills/test_claude_code_skill.py
- Intent: The claude-code delegation skill verifies Claude CLI OAuth health before dispatching work (an expired login fails fast with a clear remediation) and shows currently configured model examples.
- Protected-Invariant: Delegation to Claude Code never proceeds on an expired/absent OAuth session without surfacing the failure.
- Tests: tests/skills/test_claude_code_skill.py
- Retirement-Condition: Upstream skill gains an equivalent OAuth preflight.
- Disposition: active

## G-DOCS-GITHUB-WORKTREE: worktree cleanup guidance after PR merge
- Commits: 351dfab6e78e200feaf5c838c8fb5ddb0e228c0f
- Owned-Files:
  - skills/software-development/github/references/pr-workflow.md
  - website/docs/user-guide/skills/bundled/github/github-github-pr-workflow.md
- Intent: `gh pr merge --squash --delete-branch` can merge remotely yet exit nonzero when the branch is checked out in a local worktree; the skill documents read-back (`gh pr view --json state,...`) before any retry. Originally landed in `skills/github/github-pr-workflow/SKILL.md`; upstream's skill-tree restructure relocated the content to the current path.
- Protected-Invariant: The documented flow never retries a merge that GitHub already reports as MERGED.
- Tests: none (documentation-only; exercised by the github skill's workflow)
- Retirement-Condition: Upstream documentation covers the ambiguous-exit read-back flow.
- Disposition: active

## G-DESKTOP-TEST-ISOLATION: isolate desktop test fixtures and mutex paths
- Commits: 9f241f683a6aa4e3dd7472fd125dc2d862cf1e8b
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

## G-DESKTOP-SESSION-HISTORY: keep earlier prompts and profile-default new sessions
- Commits: 9c61aea2d1d212f3b7491c62f6c16e51970344c4
- Owned-Files:
  - apps/desktop/src/app/chat/history-window.test.tsx
  - apps/desktop/src/app/chat/index.tsx
  - apps/desktop/src/app/session/hooks/default-new-session.test.tsx
  - apps/desktop/src/app/session/new-session-route.ts
  - apps/desktop/src/plugins/hermes-bots/group-round-members.ts
- Intent: Own the fork-only desktop extras that blocked `cc_resolve_upstream_conflict` prepare as `unknown_fork_change` (INC-HU / 5721ceec). Keep earlier prompts reachable in open history windows, offer Start new session on the stranded-resume dead end, and apply profile defaults to new chats. `group-round-members.ts` is retained as a historical conflict-ownership record; it currently matches upstream.
- Protected-Invariant: Upstream merges must not drop the history-window keep-earlier-prompts behavior or the profile-default new-session route without an explicit ledger retirement.
- Tests: apps/desktop/src/app/chat/history-window.test.tsx, apps/desktop/src/app/session/hooks/default-new-session.test.tsx
- Retirement-Condition: Upstream carries equivalent history-window and profile-default new-session behavior.
- Disposition: absorbed-upstream

## G-DESKTOP-SKILLS-CATALOG: desktop skills catalog, deeplink install, and host confirm
- Commits: 813d849627e0b9c315eba83ed4b0986304b63aac
- Owned-Files:
  - apps/desktop/src/app/capabilities/index.test.tsx
  - apps/desktop/src/app/chat/route-tile.tsx
  - apps/desktop/src/app/contrib/hooks/use-desktop-integrations.test.tsx
  - apps/desktop/src/app/master-detail.tsx
  - apps/desktop/src/app/skills/capability-tabs.tsx
  - apps/desktop/src/app/skills/catalog-browser.test.tsx
  - apps/desktop/src/app/skills/catalog-browser.tsx
  - apps/desktop/src/app/skills/catalog-data.test.ts
  - apps/desktop/src/app/skills/catalog-data.ts
  - apps/desktop/src/app/skills/index.tsx
  - apps/desktop/src/app/skills/plugins-tab.test.tsx
  - apps/desktop/src/app/skills/plugins-tab.tsx
  - apps/desktop/src/app/skills/skill-catalog.tsx
  - apps/desktop/src/app/skills/update-skills-button.tsx
  - apps/desktop/src/components/confirm-host.test.tsx
  - apps/desktop/src/components/ui/segmented-control.tsx
  - apps/desktop/src/i18n/ja.ts
  - apps/desktop/src/lib/catalog-install.test.ts
  - apps/desktop/src/lib/deeplink-routes.test.ts
  - apps/desktop/src/lib/deeplink-routes.ts
  - apps/desktop/src/lib/error-surface.test.ts
  - apps/desktop/src/store/skill-deeplink-install.test.ts
  - apps/desktop/src/store/skill-deeplink-install.ts
- Intent: Own the fork-only desktop extras that blocked the 2026-09-21 live `cc_resolve_upstream_conflict` prepare (`inc-hu-verify-20260921`) as `unknown_fork_change` after G-DESKTOP-SESSION-HISTORY took the Sep 19 conflict set. Keep the skills catalog browser, capability tabs, deeplink install, host-confirm, and Japanese catalog copy during upstream merges.
- Protected-Invariant: Upstream merges must not drop the desktop skills catalog, deeplink install, or host-confirm surfaces without an explicit ledger retirement.
- Tests: apps/desktop/src/app/skills/catalog-browser.test.tsx, apps/desktop/src/lib/deeplink-routes.test.ts, apps/desktop/src/store/skill-deeplink-install.test.ts, apps/desktop/src/components/confirm-host.test.tsx
- Retirement-Condition: Upstream carries equivalent catalog, deeplink-install, and host-confirm behavior.
- Disposition: absorbed-upstream

## G-DESKTOP-PROFILE-SWITCHER: desktop sidebar profile switcher
- Commits: b1446a740800503e909c44ca717a0c238c838a3e
- Owned-Files:
  - apps/desktop/src/app/chat/sidebar/profile-dropdown-switcher.tsx
- Intent: Own the remaining unowned conflict path that blocked the 2026-09-21 live `cc_resolve_upstream_conflict` prepare (`inc-hu-verify-20260921-r27b`, `f4680448-0887-37e0-92f5-b40cc9d4aa24`) as `unknown_fork_change` after G-DESKTOP-SKILLS-CATALOG took the catalog/deeplink set. Keep the desktop sidebar profile switcher during upstream merges.
- Protected-Invariant: Upstream merges must not drop the desktop sidebar profile switcher without an explicit ledger retirement.
- Tests: apps/desktop/src/app/chat/sidebar/profile-dropdown-switcher.test.tsx
- Retirement-Condition: Upstream carries equivalent sidebar profile-switcher behavior.
- Disposition: absorbed-upstream

## G-DESKTOP-CONNECTORS: desktop connector and MCP setup extras
- Commits: 1f319e8451c6691e2111940e302b7f3149683344
- Owned-Files:
  - apps/desktop/electron/main.ts
  - apps/desktop/src/components/assistant-ui/connector-tool.tsx
  - apps/desktop/src/components/assistant-ui/mcp-setup-tool.tsx
  - apps/desktop/src/components/onboarding-chat/onboarding-recommendations-runbook.test.ts
  - apps/desktop/src/store/connection-request.ts
- Intent: Own the remaining unowned conflict paths that blocked the 2026-09-21 live `cc_resolve_upstream_conflict` prepare (`bee437ed-75c5-3676-ab04-019f937b3606`) as `unknown_fork_change` after G-DESKTOP-PROFILE-SWITCHER. Keep fork desktop connector/MCP setup extras, Electron main-process wiring, connection-request store, and the onboarding recommendations runbook test during upstream merges.
- Protected-Invariant: Upstream merges must not drop those desktop connector/MCP setup extras without an explicit ledger retirement.
- Tests: apps/desktop/src/components/onboarding-chat/onboarding-recommendations-runbook.test.ts
- Retirement-Condition: Upstream carries equivalent connector/MCP setup behavior and the runbook test, or the extras are explicitly retired.
- Disposition: absorbed-upstream

## G-ELECTRON-PATCH: Electron patched-release bump
- Commits: a511d303943b14105f53e31f5c134fee3b45ad0a, c5b7c978d0d500a243e616df8ff5c8f14904ce9a, a2e39e2bc738c38641611b758acfdc7f8f877bf8, 484b22f607136ed4b0b5acc5372d73a2fb5b40b0, 14c7cf5339a24990f7577997dcb49e512a3695b2, 767ae035f5d60915d1ecc10bbd28ab9caf9a0230
- Owned-Files:
  - apps/desktop/package.json
  - apps/desktop/scripts/batch-sign-binaries.mjs
  - apps/desktop/scripts/builder-arch-targets.mjs
  - apps/desktop/scripts/builder-arch-targets.test.mjs
  - apps/desktop/scripts/builder-config-26.mjs
  - apps/desktop/scripts/builder-config-26.test.mjs
  - apps/desktop/scripts/patch-electron-builder-mac-binary.mjs
  - apps/desktop/scripts/prepare-packaging-tools.mjs
  - apps/desktop/scripts/prepare-packaging-tools.test.mjs
  - apps/desktop/scripts/run-electron-builder.mjs
  - apps/desktop/scripts/signing-resources.test.mjs
  - package.json
  - package-lock.json
- Intent: Track a patched Electron release ahead of upstream's pin to pick up a security fix. The mac signing adapter must follow electron-builder 26.15.3's supplier, osx-sign 1.3.3 and isbinaryfile 4.0.10, not the 2.4.0/5.0.7 pins that ship with the 27 alpha. Packaging preparation must load app-builder-lib from the directory in its installed package.json main (`out/` on 26.15.3), not the 27 alpha's `dist/`. The runner must present a 26.15.3-valid config: no top-level `msix`, `asar.unpack` flattened onto `asarUnpack`, and no `toolsets` file URLs. The Windows signer must load `computeArchToTargetNamesMap` from the installed compiled root (`out/targets/targetFactory.js` on 26.15.3), not `app-builder-lib/internal`. That load must reject a mismatched package name or version, and must reject a `main` that leaves the package root, before `require`.
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
- Commits: da5beef7a1b61077bb1a476da3064bf0735f405b
- Owned-Files:
  - hermes_cli/web_server.py
  - tests/hermes_cli/test_web_server.py
- Intent: Cherry-pick of upstream 54ee290bc (#96490): a non-loopback `dashboard.public_url` must not engage the ticket-only auth gate for the private loopback backend the Desktop app spawns (loopback bind + HERMES_DESKTOP=1 + operator-minted credential are all required for the exemption).
- Protected-Invariant: The real public dashboard keeps its auth gate; only the Desktop-owned loopback backend is exempt.
- Tests: tests/hermes_cli/test_web_server.py
- Retirement-Condition: Met - 54ee290bc is in `upstream/main` and the fork carries no residual delta on the owned files at the current merge-base; remove this entry after the next clean upstream sync.
- Disposition: absorbed-upstream

## G-SYNC-RESIDUE: retired trailing-newline residue from upstream sync merges
- Commits: bde5333c178ddf8bb1868ee72f770e7a14e5726b
- Owned-Files:
  - tests/test_engines_satisfiable.py
  - tests/agent/test_turn_finalizer_final_response_persistence.py
  - tests/run_agent/test_tool_call_incremental_persistence.py
- Intent: Historical record of three trailing-newline-only deltas removed by the clean reset to current upstream before reconstruction.
- Protected-Invariant: The residue never carries a behavioral fork change; a non-newline delta on these paths is out of scope for this entry and must get its own owner.
- Tests: none (newline-only residue; no behavioral assertion)
- Retirement-Condition: Met - the clean upstream reset removed all three residual deltas.
- Disposition: absorbed-upstream

## G-UPSTREAM-HYGIENE: repair integration residue during upstream sync
- Commits: cb993066b647344bf0552e0d1c32a5e2f78b8208
- Reconciliations: 68f8ab91e3a3b0aa5cb3c8d114fa60b253f227bd
- Owned-Files:
  - tests/cron/test_estop.py
  - tests/cron/test_file_permissions.py
  - website/docs/reference/optional-skills-catalog.md
- Intent: Adapt inherited tests to the upstream parser and macOS symlink-boundary contracts while removing an accidentally published conflict marker from generated documentation.
- Protected-Invariant: Integrated upstream tests exercise the live APIs and host path semantics, and published documentation contains no unresolved conflict marker.
- Tests: tests/cron/test_estop.py, tests/cron/test_file_permissions.py; `git diff --check`
- Retirement-Condition: Upstream carries equivalent test updates and removes the documentation marker.
- Disposition: active

## G-DESKTOP-UPDATE-20260921: desktop and TUI test/component extras
- Commits: 5c5e59401bbc76f289d1f12c8dcdf03b2220aa5c
- Owned-Files:
  - apps/desktop/electron/command-screenshot-monitor.test.ts
  - apps/desktop/electron/command-screenshot-monitor.ts
  - apps/desktop/electron/command-screenshot.test.ts
  - apps/desktop/electron/command-screenshot.ts
  - apps/desktop/electron/github-api-auth.test.ts
  - apps/desktop/electron/pool-retirement-live-fixture/main.ts
  - apps/desktop/electron/portal-session-live-fixture/main.ts
  - apps/desktop/electron/preload.ts
  - apps/desktop/electron/update-api-check.ts
  - apps/desktop/src/api/config.ts
  - apps/desktop/src/app/chat/history-window.ts
  - apps/desktop/src/app/chat/hooks/use-composer-actions.test.ts
  - apps/desktop/src/app/contrib/hooks/restore-late-plugin-route.test.tsx
  - apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/server-requests.test.ts
  - apps/desktop/src/app/settings/toolset-config-panel.test.tsx
  - apps/desktop/src/components/assistant-ui/thread/response-group.test.tsx
  - apps/desktop/src/components/assistant-ui/thread/timeline-rail.test.tsx
  - apps/desktop/src/components/assistant-ui/thread/timeline.tsx
  - apps/desktop/src/components/assistant-ui/thread/use-timeline-reveal.ts
  - apps/desktop/src/components/boot-failure-cause.test.ts
  - apps/desktop/src/components/desktop-install-overlay.tsx
  - apps/desktop/src/components/onboarding/free-tier-setup-notice.test.tsx
  - apps/desktop/src/lib/onboarding-recommendations.ts
  - apps/desktop/src/plugins/hermes-bots/bot-row.test.tsx
  - apps/desktop/src/plugins/hermes-bots/create-dialog.tsx
  - apps/desktop/src/plugins/hermes-bots/group-activity.test.ts
  - apps/desktop/src/plugins/hermes-bots/group-chat-view-members.test.ts
  - apps/desktop/src/plugins/hermes-bots/group-chat-view.inline-code.test.tsx
  - apps/desktop/src/plugins/hermes-bots/group-chat-view.tsx
  - apps/desktop/src/plugins/hermes-bots/group-chat.test.ts
  - apps/desktop/src/plugins/hermes-bots/group-rounds.test.ts
  - apps/desktop/src/plugins/hermes-bots/relay.test.ts
  - apps/desktop/src/store/gateway-connection-lifecycle.test.ts
  - apps/desktop/src/store/gateway.test.ts
  - apps/desktop/src/store/hub-actions.blocked.test.ts
  - apps/desktop/src/store/suggestion-providers/mcp.test.ts
  - ui-tui/src/__tests__/createGatewayEventHandler.test.ts
  - ui-tui/src/hooks/useCompletion.ts
- Intent: Own commit `5c5e59401bbc76f289d1f12c8dcdf03b2220aa5c`, which still changes these paths. The desktop paths were restored to upstream pin `547248908bf07e22dc21eec20fe416684f55a596` because they were stale pre-move copies and broke `npm run build`. The remaining fork delta is `ui-tui/src/__tests__/createGatewayEventHandler.test.ts`: fork-only cases for goal status prefixes, spinner filtering, `moa.aggregating`, the `/agents` nudge, and ttl self-expiry. Those cases stay until upstream ships them or they are explicitly retired. The other owned paths are listed because the commit claim must cover every path that commit changes, not because they still differ from the pin.
- Protected-Invariant: Upstream merges must not drop the remaining TUI gateway-event cases in `ui-tui/src/__tests__/createGatewayEventHandler.test.ts` without an explicit ledger retirement. Restored desktop paths in this entry match the pin and are not a second delta.
- Tests: apps/desktop/electron/command-screenshot-monitor.test.ts, apps/desktop/electron/command-screenshot.test.ts, apps/desktop/electron/github-api-auth.test.ts, apps/desktop/src/app/chat/hooks/use-composer-actions.test.ts, apps/desktop/src/app/session/hooks/use-message-stream/gateway-event/server-requests.test.ts, apps/desktop/src/app/settings/toolset-config-panel.test.tsx, apps/desktop/src/components/assistant-ui/thread/response-group.test.tsx, apps/desktop/src/components/assistant-ui/thread/timeline-rail.test.tsx, apps/desktop/src/components/boot-failure-cause.test.ts, apps/desktop/src/components/onboarding/free-tier-setup-notice.test.tsx, apps/desktop/src/plugins/hermes-bots/bot-row.test.tsx, apps/desktop/src/plugins/hermes-bots/group-activity.test.ts, apps/desktop/src/plugins/hermes-bots/group-chat-view-members.test.ts, apps/desktop/src/plugins/hermes-bots/group-chat.test.ts, apps/desktop/src/plugins/hermes-bots/group-rounds.test.ts, apps/desktop/src/plugins/hermes-bots/relay.test.ts, apps/desktop/src/store/gateway-connection-lifecycle.test.ts, apps/desktop/src/store/gateway.test.ts, apps/desktop/src/store/hub-actions.blocked.test.ts, apps/desktop/src/store/suggestion-providers/mcp.test.ts, ui-tui/src/__tests__/createGatewayEventHandler.test.ts
- Retirement-Condition: Upstream carries the remaining TUI gateway-event cases, or those cases are explicitly retired.
- Disposition: active

## G-CLI-STARTUP: process entrypoint helpers shared by desktop and CLI launch
- Commits: 4287d10b96b09546d2af49e673fde06f5f5cc47f, 572db173f7c57b2890a8af6b77eccf5f565bcfa1
- Owned-Files:
  - hermes_cli/_startup_fast.py
  - hermes_cli/main_desktop.py
  - tests/hermes_cli/test_cli_entrypoint_imports.py
  - tests/hermes_cli/test_cli_module_imports.py
- Intent: Own the CLI startup helpers the integrate replay left unowned, plus the desktop launch module and entrypoint import test that call the same process entrypoint. This is not the oneshot zero-tool invariant.
- Protected-Invariant: Desktop launch and the CLI import contract must resolve these helpers from the checkout under test, not from an unrelated live install, when the checkout is not the installed app.
- Tests: tests/hermes_cli/test_cli_module_imports.py, tests/hermes_cli/test_desktop_profile_launch.py
- Retirement-Condition: Upstream owns this startup surface and the fork no longer patches it.
- Disposition: active

## G-GATEWAY-RESTART-ROUTE: non-supervisor restart drain stays hermetic under launchd
- Commits: d432bf87ada09ff8f074a8cdfcac928e88f48304
- Owned-Files:
  - tests/gateway/test_restart_drain.py
- Intent: The published drain contract asserts a detached relaunch when supervisor markers are absent. This host re-exports HERMES_LAUNCHD_LABEL to the gateway grandchild, and upstream's test does not clear that marker, so the assertion selected the service route.
- Protected-Invariant: A /restart with the supervisor markers removed must drain without interrupting the running agent and must request a detached relaunch, not the exit-75 service route.
- Tests: tests/gateway/test_restart_drain.py
- Retirement-Condition: Upstream clears HERMES_LAUNCHD_LABEL in this test, or this host no longer exports a launchd job label into the test process.
- Disposition: active

## G-CONFIG-SCOPE: last-good config recovery and managed-scope layer shapes
- Commits: none
- Owned-Files:
  - hermes_cli/config.py
  - hermes_cli/config_effective.py
  - hermes_cli/managed_scope.py
  - tests/hermes_cli/test_config_effective.py
  - tests/hermes_cli/test_config_scope_expansion.py
  - tests/hermes_cli/test_managed_scope_config.py
  - tests/hermes_cli/test_managed_scope_loaders.py
- Intent: Config loading keeps the last successfully parsed user layer separately from the merged policy value, so recovery after a mid-edit parse failure re-expands only user-authored templates. The effective-config cache keys on the per-layer shape each read produced, so a managed overlay that fails to parse is ignored loudly and never re-resolved through a profile's secret scope. The scheduler's exact thread admission reads config through this surface, which is why the cron reconciliation commit carried it.
- Protected-Invariant: A broken user YAML never silently drops overrides; a managed `${VAR}` resolves against the process environment only; a merged policy value is never fed back into template expansion.
- Tests: tests/hermes_cli/test_config_effective.py, tests/hermes_cli/test_config_scope_expansion.py, tests/hermes_cli/test_managed_scope_config.py, tests/hermes_cli/test_managed_scope_loaders.py
- Retirement-Condition: Upstream separates the last-good user layer from the expanded value and keys the effective cache on layer shape.
- Disposition: active

## G-PM-TEMP-HOME: temporary homes boot from the owner's committed environment
- Commits: 0dfe32746ba664217fd74d32a7234f30e8a406b1
- Owned-Files:
  - pm/environments.py
  - tests/hermes_cli/test_borrowed_home_launch.py
- Intent: A launch under a temporary HERMES_HOME has no committed generation of its own. Refusing it forced every test harness to run `hermes pm repair` or wait out a full sync. The activation state dir borrows the owning home's committed record for the read; writes stay on the install state dir, and a borrower with its own facts.json keeps it.
- Protected-Invariant: A borrowed read never writes into the owner's state dir; a home with its own committed generation is never overridden by the owner's.
- Tests: tests/hermes_cli/test_borrowed_home_launch.py
- Retirement-Condition: Upstream lets a temporary home activate from the owning install's committed generation.
- Disposition: active

## G-FORK-LEDGER: fork change ledger and post-verify checker
- Commits: self, 377bed46629a263ffaf541a624d013c00926fb9e
- Ledger-Revision: 48
- History-Reconciliations: 288f24682a67cdbca1ff00e011144cb961f65d5b
- Cross-Owner-Commits: 291e8f48c6801453e4f0710c00336513ec8a6dc7, 43ed7d97c2fc3006e2de8ed25fb0494de1244465, 88e129f239bb8a1f610ad9ff25c63d2656f035e7, 19d7fe26b0efef91f031260eb52f0569458b2c58, 45ede46503b32dc3de89e154ffd7e1d2e6ebeabb, 96460687e993b30041ab1cee4b2104cd7f5c5a94
- Repaired-Conflict-Merges: ba7235102d001f2d616be8f28df7400f0ae0c39a, ab70ac98d6f31d18217cdb2511fb681083157c62
- Upstream-Line-Deletions: cron/scheduler.py removed-upstream-blobs 6276f9477279568c1dfb163e5dba4606b110ec6d+8a1f5827553dbbe509a362dee701024b70683014, cron/executions.py removed-upstream-blobs f64945e56081545a639079c36361f817c34646e7+4f40500bfc3aa322b28c3126f8712bbca4a2f5d2, hermes_cli/cron.py removed-upstream-blobs c73465fab94d9353470a476feeb3b6a378156722
- Owned-Files:
  - docs/FORK_CHANGES.md
  - website/docs/developer-guide/FORK_CHANGES.md
  - scripts/ci/check_fork_ledger.py
  - tests/ci/test_check_fork_ledger.py
  - tests/ci/test_check_fork_ledger_adversarial.py
- Intent: Record every fork-only change and fail closed if a work commit is unmapped, a current path is unowned or ambiguous, or the checker cannot run. `Commits: self` is component-scoped self-mapping for commits that touch only G-FORK-LEDGER files and change this specific entry. Every ledger maintenance commit bumps Ledger-Revision so the authorization is explicit and entry-scoped. `History-Reconciliations` authorizes only audited zero-tree ancestry links needed for non-force publication after a history reconstruction. Revision 32: extend G-DESKTOP-UPDATE-20260921 to own `apps/desktop/electron/command-screenshot-monitor.ts` and `apps/desktop/electron/preload.ts` after prepare d4bc63cd (spawned by update ce08cc8d) failed with `unknown_fork_change`. Revision 33 links pre-integrate tip `1d8887eedad60deaa9d9d5f48b98fa5730de1ae4` through zero-tree reconciliation `288f24682a67cdbca1ff00e011144cb961f65d5b`, maps the replayed first-parent commits onto their existing owners, and records the two published cross-owner commits that cannot be split without a non-fast-forward rewrite. Revision 34 claims the desktop-launch restore and records the launchd-hermetic drain test. Revision 35 removes the retired update-workflow name from the fork-sync retirement condition. Revision 36 rejects a conflicted sync that keeps the wholesale fork file and accepts one that keeps upstream lines with fork insertions on top. Revision 37 retires the desktop entries whose current trees match upstream pin `547248908bf07e22dc21eec20fe416684f55a596`, because the stale fork copies broke `npm run build`. G-DESKTOP-UPDATE-20260921 stays active: `ui-tui/src/__tests__/createGatewayEventHandler.test.ts` still differs from that pin. Revision 38 claims the signing adapter that follows electron-builder 26.15.3's osx-sign 1.3.3 supplier, because the upstream 2.4.0 pin made `npm run builder` die before packaging. Revision 39 claims the packager load of app-builder-lib from its installed `main` (`out/` on 26.15.3), because the hardcoded `dist/` import died with `ERR_MODULE_NOT_FOUND` after the signing adapter passed. Revision 40 claims the runner's 26.15.3 config adapter, because the 27 alpha config (`msix`, `asar.unpack`, toolset file URLs) failed schema validation after the import path was fixed. Revision 41 claims the arch-target load from the installed compiled root, because `app-builder-lib/internal` does not exist on 26.15.3 and afterPack imports that signer on macOS. Revision 42 requires that load to reject a mismatched pin and a `main` that leaves the package root before `require`. Revision 43 claims `2d75df969f923ca86f8360ae51ac86d492825391` on G-UPDATE-FORKSYNC and owns the two update tests that commit introduced. Revision 44 maps `ba7235102d001f2d616be8f28df7400f0ae0c39a` and `ab70ac98d6f31d18217cdb2511fb681083157c62` only while the checked tip keeps every current upstream line of each conflict path. Listing those SHAs is not a waiver. Revision 45 maps `19d7fe26b0efef91f031260eb52f0569458b2c58` as a cross-owner commit. It restores the oneshot guard without dropping upstream lines. Listing that SHA is not a waiver either. Revision 46 maps `9a1ce849032259bf36c545e85db96fe86a864db3` under G-ONESHOT-ISOLATION. It deletes the rebinding copies and keeps every upstream line. Listing that SHA is not a waiver either. Revision 47 maps the four gateway-restore commits the cron reconciliation left unmapped: `4a7f53254e4b3b8702e7d0c3225f3897a1bdcee7` under G-CRON-DURABLE, `0dfe32746ba664217fd74d32a7234f30e8a406b1` under the new G-PM-TEMP-HOME, and `45ede46503b32dc3de89e154ffd7e1d2e6ebeabb` and `96460687e993b30041ab1cee4b2104cd7f5c5a94` as cross-owner commits. It owns the scheduler tick, outcome and delivery-run modules and their tests under G-CRON-DURABLE, the parse-contract test under G-KANBAN-LIFECYCLE, and the config-scope surface under the new G-CONFIG-SCOPE, because the weekly update's safety gate stopped on `unowned_extra: hermes_cli/config.py` and the checker named 27 unowned current paths. The same revision records the cron reconciliation's removal of upstream lines in `cron/scheduler.py`, `cron/executions.py` and `hermes_cli/cron.py` as `Upstream-Line-Deletions`, each bound to the blob of the upstream version whose lines were removed. Those lines are the pre-reconciliation scheduler, execution-store and cron-cli text that the fork replaced with its own modules; the declaration names that text instead of inferring the deletion from a later commit.
- Protected-Invariant: `self` stays narrow. A commit is mapped only when it changes the ledger and every changed path is owned by G-FORK-LEDGER. A commit that also changes an unrelated path remains unmapped. History reconciliation cannot hide first-parent work, upstream work, current-tree changes, replacement-forged objects, unrelated roots, overlapping retired sets, inherited activation state, full-reachable revision high-water marks, commit-time Path-Precedence, oversized revisions, or sticky entry and History-Reconciliations removal. `Cross-Owner-Commits` maps a work commit only when every changed path already has one effective owner, at least two owners are involved, the commit does not change the ledger, and no entry has already claimed it. `Repaired-Conflict-Merges` maps a published conflict merge only when the checked tip still keeps every current upstream line of each conflict path, in upstream order. A tip that drops a line leaves that SHA unmapped and names the path. A later edit of the file is not a waiver. An intentional removal is declared in `Upstream-Line-Deletions` as the path plus the blobs of the upstream versions whose lines were removed, so the deletion is audited text. Every current upstream line the tip lacks must appear in one of those blobs; a line upstream added later is unaudited and fails. Each blob must be one upstream actually committed. Only G-FORK-LEDGER may declare it.
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
