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
