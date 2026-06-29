# Zwartewater FLA Operations

The language of the automated Trojan FLA equalisation and charge operations on Venus OS — specifically the temporary transfer of bus control away from the normal aggregate driver while the LFP bank is isolated, and back again. This is the vocabulary for the most safety-critical sequence in the system.

## Language

**Takeover**:
The stateful operation that temporarily transfers control of the main bus from the aggregate driver to the temp battery so the LFP bank can be isolated and the Trojan bank charged high, then transfers it back. Owns four entangled lifecycles for its duration: the operation lock, the aggregate driver, the DVCC selection, and the temp battery.
_Avoid_: handoff (that names a phase, not the whole operation), session.

**Hand-off in**:
The ordered opening phase of a **Takeover**: register temp battery at a safe voltage → stop aggregate → restart systemcalc → wait for discovery → snapshot the **DVCC originals** → switch DVCC to the temp battery → confirm the BMS selection → open relay 2 → raise CVL to the target. The ordering is safety-critical: the relay must not open before the BMS selection is confirmed.
_Avoid_: setup, init.

**Hand-back**:
The closing phase of a **Takeover**: hold the bus at float until the Trojan↔LFP delta converges (the **safe-hold**), close relay 2, then **restore** the **DVCC originals** and release the four lifecycles — but only once the relay is confirmed closed.
_Avoid_: teardown (reserved for the guarded restore step inside hand-back), reconnect.

**DVCC originals**:
The snapshot of `/Settings/SystemSetup/{BatteryService, BmsInstance, MaxChargeVoltage}` captured at **hand-off in**, before the Takeover overwrites them, and **persisted** to disk so a crashed-and-resumed teardown restores the true values rather than guessed constants. On Zwartewater the real normals are install-specific (`BatteryService = com.victronenergy.battery/277`, the SmartShunt LFP; `MaxChargeVoltage = 32 V`) and are NOT the same as any hardcodeable default — which is why they must be snapshotted, not assumed.
_Avoid_: defaults, known-normals (those were the wrong model — see ADR-0001).

**Safe-hold**:
The fail-safe state inside **hand-back**: relay open, bus actively pinned at float by the temp battery every cycle, alarm asserted, lock held — entered on sustained non-convergence or unreadable shunts, and never exited into teardown. The firm invariant is that the relay is never auto-closed while the Trojan↔LFP delta exceeds the safe threshold, and DVCC control is never handed back while the relay is open.
_Avoid_: hold, wait.

**Temp battery**:
The service-neutral D-Bus battery service (`com.victronenergy.battery.fla_temp`, instance 100) that holds the main bus during a **Takeover**. A live temp battery with relay 2 open is a hold in progress and must never be killed; with relay 2 closed it is a true orphan.
_Avoid_: fake battery, dummy.

**Resume**:
Adopting an interrupted **Takeover** on service startup: when relay 2 is open and a temp battery is running, the winning service attaches to the existing temp battery (no respawn, no hold gap), loads the persisted **DVCC originals**, and runs **hand-back** to completion.

**Operation profile**:
The single declarative card that distinguishes one FLA operation (equalisation vs charge) from the other: its name, web port, D-Bus identity (service name, device instance, product id, alarm path), settings schema, state list, cache fields, and HTML page. The shared settings/status/web engines are closed and configured by the profile — so the two operations differ only in their profile, never in the plumbing.
_Avoid_: config, descriptor (too generic).

## Relationships

- A **Takeover** has exactly one **hand-off in** and one **hand-back**.
- A **Takeover** snapshots **DVCC originals** during **hand-off in** and restores them during **hand-back**.
- **Hand-back** enters **safe-hold** on non-convergence and only restores **DVCC originals** once relay 2 is confirmed closed.
- A **Resume** reconstructs a **Takeover** mid-**hand-back** from a running **temp battery** plus the persisted **DVCC originals**.
- Both the equalisation and charge operations are **Takeovers** that differ only in the charging loop between **hand-off in** and **hand-back**.

## Example dialogue

> **Dev:** "On a **resume**, what do we restore `BatteryService` to?"
> **Domain expert:** "The **DVCC original** we snapshotted at **hand-off in** — `battery/277` on this boat. Don't restore it to the aggregate; that's a guess, and it's wrong here."
> **Dev:** "And if the relay won't close during **hand-back**?"
> **Domain expert:** "Then you never restore anything. You stay in **safe-hold** — relay open, bus pinned at float, alarm on, lock held — until the operator acts. Handing DVCC back with the relay open is the free-fall."

## Flagged ambiguities

- "handoff" was used for both the whole operation and its opening phase — resolved: the operation is a **Takeover**; its phases are **hand-off in** and **hand-back**.
- "known-normals" / "defaults" was used for the values restored on resume — resolved: there are no safe constants here; the restored values are the persisted **DVCC originals** (see ADR-0001).
