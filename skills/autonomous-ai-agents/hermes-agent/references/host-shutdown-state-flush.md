# Host shutdown state flush

Use when the user is about to power off, shut the computer, or otherwise cut the host, and wants the current agent state saved. This is a flush, not maintenance. Do not pause cron, stop the gateway, VACUUM, or run `hermes sessions optimize`. Those change live operations or rewrite the store. Compact and prune belong to `hermes-state-db-maintenance`.

## Order

1. Checkpoint live SQLite only. From a second connection, `PRAGMA wal_checkpoint(TRUNCATE)` with `busy_timeout` set. Targets:
   - `$HERMES_HOME/state.db`
   - `$HERMES_HOME/profiles/*/state.db` when a WAL exists
   - `$HERMES_HOME/state/recovery/recovery.sqlite3`
   - `$HERMES_HOME/kanban/boards/*/kanban.db` when a WAL exists
   - OpenViking's live sqlite under `~/.openviking/data/_system/` (`usage_audit.sqlite3`, `queue.db`) when that layout is present
   Skip historical fixture copies and old snapshot trees. A non-zero `-wal` size does not mean uncheckpointed frames: SQLite keeps the file until `TRUNCATE` or `RESTART`. Success is `busy=0` and `-wal` size 0 afterward. A return of `(0, 0, 0)` with a zero WAL is success, not a no-op failure. `TRUNCATE` from another connection is safe while holders are open. Deleting `-wal` or `-shm`, and `cp` of `state.db` alone, are not.
2. Read `PRAGMA synchronous` on `state.db` before you promise durability. `2` is `FULL` (each commit fsyncs the WAL). `1` is `NORMAL` (the last commit can be lost until a checkpoint). Say which one you observed.
3. Take the quick snapshot, not a full zip. A full `hermes backup` walks the whole home and is the offline-maintenance path.
   ```bash
   hermes backup -q -l shutdown
   ```
   Give it several minutes. A multi-gigabyte `state.db` will not finish inside a short timeout. The snapshot lands at `$HERMES_HOME/state-snapshots/<UTC-timestamp>-<label>/` and uses the SQLite backup API, so it is consistent even if a writer appends to the WAL during the copy. It covers the active home only. Other profiles are not in that tree; the step-1 checkpoint is what makes their committed frames durable.
4. Verify the snapshot before claiming it saved. Read `manifest.json`: `failed_dbs` empty, `state.db` listed, on-disk size matches the manifest byte count, and the first 16 bytes are `SQLite format 3`. A missing `memory_store.db` or `response_store.db` is expected when sessions live in `state.db`.
5. Write `$HERMES_HOME/state/recovery/last-clean-shutdown.json` with the snapshot id and `restore` set to `/snapshot restore <id>`. Then `sync`. Do not put a one-time shutdown in persistent memory.
6. Tell the user they can cut power after the confirmation is on screen. The final reply commits after delivery, so a checkpoint taken during the turn does not include it. If synchronous is `FULL`, WAL replay recovers that commit. If it is not, schedule a delayed `TRUNCATE` plus `sync` a few seconds after the reply with `terminal(background=true)`. A foreground command that ends in `&` is rejected; do not use that form.

Do not kill OpenViking as part of the save. The host stop tears it down. Checkpoint its sqlite and sync.
