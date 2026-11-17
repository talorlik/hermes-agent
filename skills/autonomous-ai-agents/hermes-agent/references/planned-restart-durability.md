# Planned restart durability and recovery verification

Use this before a planned host restart and again after boot. It is not the power-off flush in `host-shutdown-state-flush.md`: that flush does not pause schedules and does not verify recovery after boot. It is not an unplanned service-failure response. Do not create a parallel skill.

Do not infer schedule pause or resume from host shutdown or from service recovery. Record those decisions explicitly. Do not record an unproven claim as established fact. Startup health is provisional until required services pass live checks after the boot storm. Omit snapshot names, database sizes, hostnames, private addresses, exact filesystem locations, recovery identifiers, and current service state. The evidence below does not prove that a later schedule resume completed.

DDA-2026-11-12-01 — Planned restart durability and recovery verification**
  - Classification: skill
  - Proposal: add a planned-restart procedure that:
    1. Checkpoints live SQLite databases and records whether any remained busy or failed.
    2. Creates a verified recovery snapshot or equivalent checkpoint with a readable manifest.
    3. Writes and syncs a clean-shutdown marker.
    4. After boot, verifies session-state recovery, database durability settings, checkpoint readability, and service health.
    5. Treats startup health as provisional until required services pass live checks after the boot storm.
    6. Records schedule pause or resume decisions explicitly rather than inferring them from host shutdown or service recovery.
  - Evidence: the restart used WAL checkpoints, a snapshot manifest, and a clean-shutdown marker; post-boot checks verified persisted state and services. Initial liveness checks nevertheless found unavailable services and a missing runtime configuration value before later recovery.
  - Rationale: this is a repeatable operational ceremony applicable beyond the specific restart.
  - Promotion constraint: omit snapshot names, database sizes, hostnames, private addresses, exact filesystem locations, recovery identifiers, and current service state.

## Procedure

Follow the six steps above, in order.

1. Checkpoint live SQLite databases and record whether any remained busy or failed. A busy or failed checkpoint is not success.
2. Create a verified recovery snapshot or an equivalent checkpoint. It counts only when the manifest is readable and the snapshot or checkpoint was verified.
3. Write a clean-shutdown marker and sync it before boot.
4. After boot, verify session-state recovery, database durability settings, checkpoint readability, and service health.
5. Treat startup health as provisional until required services pass live checks after the boot storm. An early unavailable service or a missing runtime configuration value is not recovery. A later recovery does not erase that earlier miss.
6. Record schedule pause or resume decisions explicitly. Do not infer them from host shutdown or from service recovery. Service recovery does not prove a schedule was resumed.

Do not store snapshot names, database sizes, hostnames, private addresses, exact filesystem locations, recovery identifiers, or current service state in this procedure.
