# Lessons

## 2026-07-03 — grep -v "#" hid a live code line during a symbol-removal audit

- **Failure mode**: while replacing `web_server` with the shared engine, the
  usage audit ran `grep -n "_cache" ... | grep -v "update_cache\|#"` to drop
  comment noise. The EQ line `_cache["run_now_requested"] = False  # CRIT-5...`
  has a trailing comment, so `-v "#"` filtered out real executable code. The
  stale reference shipped: first `should_run()==True` raised a swallowed
  NameError after `_running = True`, wedging fla-equalisation permanently.
- **Detection signal**: multi-agent review; 4 independent finder angles
  converged on the same line. All 275 tests were green because `_check()`'s
  should-run branch had no coverage.
- **Prevention rules**:
  1. Never filter usage-audit greps with `-v "#"` — trailing comments make it
     drop live code. Filter on `^\s*#` (whole-line comments) or don't filter.
  2. After deleting a module/symbol, verify zero references mechanically:
     `python3 -c "import ast..."` parse + grep for the bare symbol with no
     exclusions, or import the module and let NameError surface.
  3. When a change converts a pattern in one service, grep the OTHER service
     for the same pattern before declaring the conversion done (repo rule:
     always apply the same change to both services).
  4. State-machine branches guarded by `except Exception` need at least one
     test driving the happy path — a swallowed exception there is invisible
     to the whole suite.

## 2026-10-04 — EQ drained the Trojans and restored the wrong BMS (incident 2026-10-03)

- **Failure mode**: a Cerbo reboot mid-EQ wiped the `/tmp` DVCC snapshot; the next
  run snapshotted `BmsInstance=100` as an original and restored it to a dead
  service. Separately the EQ loop read `abs(current)`, so 150 min at -50A (ESS
  "Optimized" feeding the boat from the isolated bank) looked like charging, and
  the timed-out run was recorded as a success.
- **Detection signal**: Trojan SmartShunt voltage falling while the service logs
  "Equalising"; "BMS lost" + MPPT #67 right after a hand-back.
- **Prevention rules**:
  1. State that protects persistent settings must itself be persistent.
  2. Never take the magnitude of a signed measurement in a safety check; assert
     the direction the operation depends on.
  3. "The loop ended" is not "the operation succeeded" — success needs its own
     condition (target voltage reached).
  4. An automatic restore must reuse the one real teardown, not a lighter copy
     (review of PR #36).
