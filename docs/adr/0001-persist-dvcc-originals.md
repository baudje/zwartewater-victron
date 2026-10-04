# Restore DVCC from persisted originals, not hardcoded normals

When a Takeover hands DVCC control back to the aggregate, it must restore
`/Settings/SystemSetup/{BatteryService, BmsInstance, MaxChargeVoltage}`. We
snapshot these **DVCC originals** at hand-off in and **persist them to disk**, so
every restore path — the happy path, the `finally` safety net, and a
crashed-then-resumed teardown — puts back the values that were actually live
before the operation, read from one source of truth.

We explicitly reject restoring to hardcoded "known-normal" constants
(`com.victronenergy.battery.aggregate` / `BmsInstance -1` / `LFP_SAFE_CVL`). A
live VRM read on 2026-06-29 (normal operation, relay 2 closed) showed this
install's real normals are **install-specific** and differ from any constant:
`BatteryService = com.victronenergy.battery/277` (the SmartShunt LFP, which
deliberately drives SoC per CLAUDE.md — not the aggregate) and
`MaxChargeVoltage = 32 V` (not 28.4). The aggregate driver provably never
rewrites these settings on restart, so nothing else puts them back. Restoring to
constants silently switches the system's battery monitor and lowers the DVCC
ceiling — not a free-fall, but a real correctness deviation. Persisted originals
are both deeper (one restore path) and correct.

## Consequences

- The resume path no longer needs hardcoded fallbacks; it loads the persisted
  snapshot the interrupted operation wrote. This fixes a latent bug in the
  resume teardown shipped in PR #13.
- A Takeover must write the snapshot durably before it opens relay 2, and delete
  it only after a confirmed-closed teardown.
- If the snapshot is ever missing on resume (e.g. wiped `/tmp`), the system must
  refuse to guess — hold and alarm rather than restore to a constant.

## Amendment (2026-10-04): snapshot moved to `/data`

The snapshot lived in `/tmp`, on the assumption that a full reboot ends the
takeover cleanly. It doesn't: DVCC's `BmsInstance`/`BatteryService` are
persistent settings and survive the reboot still pointing at the temp battery.
On 2026-10-03 a reboot mid-EQ wiped the `/tmp` snapshot, the next run snapshotted
`BmsInstance=100` as an "original", and its hand-back selected a dead service
("BMS lost", 1.5h). Now:

- the snapshot lives at `/data/apps/fla-shared/dvcc_originals.json`;
- a snapshot found with no operation holding the lock and relay 2 closed is a
  dead takeover: `recover_stale_takeover` takes the lock and runs the real
  guarded `teardown()` (aggregate start + rediscovery, restore, confirm), from
  each service's idle tick. There is deliberately no second, lighter restore;
- `teardown()` keeps the snapshot when the restored selection is not confirmed,
  so the next idle tick retries instead of losing the originals;
- the snapshot is written atomically (tmp + fsync + replace);
- `hand_off_in` refuses to start while DVCC still selects the temp battery, so
  takeover values can never be snapshotted as originals.
