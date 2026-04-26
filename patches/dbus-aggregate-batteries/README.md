# dbus-aggregate-batteries patches

Local patches against upstream `dbus-aggregate-batteries` (Dr-Gigavolt) so the
aggregate's reported battery current reflects what's actually flowing into the
LFP bank — including alternator charge via Orion DC-DC, which the upstream
driver cannot see.

## Files

- `*.orig` — pristine upstream files pulled from `/data/apps/dbus-aggregate-batteries/`
  on the Cerbo at the time the patches were authored. Committed so the patch
  hunks can be reviewed via `diff -u settings.py.orig settings.py` and so future
  upstream updates can be rebased without scping the old version off the Cerbo.
- `settings.py`, `dbus-aggregate-batteries.py`, `config.default.ini` — patched
  versions deployed to the Cerbo.
- `verify-patches.sh` — Cerbo-side script that greps for the *behavioural*
  identifiers introduced by these patches (`get_smartshunts_from_config`,
  `SMARTSHUNT_AS_BATTERY_CURRENT`) and warns to syslog if any are missing.
  Checking for behaviour rather than a marker comment means the script stays
  green if upstream merges our PRs and the same identifiers arrive via a
  fresh download. See "Catching upstream overwrites" below.

## What the patches change

### 1. `settings.py` — list-aware parser for `USE_SMARTSHUNTS`

Upstream uses `get_bool_from_config` for `USE_SMARTSHUNTS`, so a list value
like `[277]` silently coerces to `False` and the SmartShunt is never picked
up. Added `get_smartshunts_from_config` that parses bool **or** list (using
`json.loads` after normalising single quotes — no code execution).

Also exposes the new `SMARTSHUNT_AS_BATTERY_CURRENT` setting.

### 2. `dbus-aggregate-batteries.py` — authoritative-shunt mode

When `SMARTSHUNT_AS_BATTERY_CURRENT = True` and at least one battery-mode
SmartShunt is in the list, the aggregate uses `Current_SHUNTS` directly as
the bank current. This skips the upstream `Current_VE = Quattro + MPPT +
shunts` formula, which double-counts in our setup because the LFP-bank
SmartShunt already includes the contributions from Quattro and MPPT.

### 3. `config.default.ini` — register the new option

Adds `SMARTSHUNT_AS_BATTERY_CURRENT = False` so the upstream config
validator doesn't reject the new option when present in `config/config.ini`.

## Why this is needed

The Zwartewater has an engine alternator that charges the LFP bank via an
Orion DC-DC converter. Orion registers as `com.victronenergy.dcdc`, which
the aggregate driver doesn't know how to sum. Without these patches:

- `USE_SMARTSHUNTS = [277]` is silently ignored (parsed as `False`)
- aggregate current = Quattro + MPPT only
- alternator charge is invisible to ESS/DVCC/VRM

With the patches, aggregate current = SmartShunt LFP (which sees everything
flowing into the LFP bank, alternator included).

## Deploying

```bash
sshpass -p "$CERBO_ROOT_PASSWORD" scp \
  patches/dbus-aggregate-batteries/settings.py \
  patches/dbus-aggregate-batteries/dbus-aggregate-batteries.py \
  patches/dbus-aggregate-batteries/config.default.ini \
  config/config.ini \
  root@venus.local:/data/apps/dbus-aggregate-batteries/
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local \
  '/data/apps/dbus-aggregate-batteries/restart.sh'
```

## Re-applying after upstream updates

**A Venus OS firmware update or a driver self-update will silently revert
these patches** — the upstream files in `/data/apps/dbus-aggregate-batteries/`
get replaced by pristine upstream code, and aggregate's reported current
goes back to `Quattro + MPPT` (alternator invisible again). To re-apply:

1. Pull the new upstream files into this directory as `*.orig`.
2. Diff `*.orig` against the patched versions to see if upstream now does
   what we want, or if there are conflicts.
3. Re-apply the patches (manually, since they're small) or update the
   patched files to match the new upstream baseline.
4. Re-deploy with the snippet under "Deploying" above.

The `.preZwbackup` files on the Cerbo (created on first deploy) preserve
the pre-patch state in case of emergency revert.

## Catching upstream overwrites automatically

`verify-patches.sh` greps each deployed file for the **behavioural identifiers**
introduced by these patches (`get_smartshunts_from_config` in `settings.py`,
`SMARTSHUNT_AS_BATTERY_CURRENT` in all three files). It writes a warning to
syslog + `/data/var/zwartewater-patches.status` if any are missing.

Behaviour-based checking (rather than grepping for a `[Zwartewater patch]`
marker comment) means the script stays green in two cases:

- our local patches are deployed (the identifiers were added by us), and
- upstream eventually merges PRs #152/#154 and the identifiers arrive via
  a pristine driver download.

It only flips red when the behaviour is genuinely missing (e.g. a firmware
update reverted to a pre-merge upstream version).

Install as a boot hook so every Cerbo restart re-checks (Venus OS preserves
`/data/rc.local` across firmware updates):

```bash
sshpass -p "$CERBO_ROOT_PASSWORD" scp \
  patches/dbus-aggregate-batteries/verify-patches.sh \
  root@venus.local:/data/apps/dbus-aggregate-batteries/

sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local '
  grep -q zwartewater-patches /data/rc.local 2>/dev/null || \
    echo "/data/apps/dbus-aggregate-batteries/verify-patches.sh &" >> /data/rc.local
  chmod +x /data/rc.local /data/apps/dbus-aggregate-batteries/verify-patches.sh
'
```

After install, run it manually to baseline the status file:
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local \
  '/data/apps/dbus-aggregate-batteries/verify-patches.sh; cat /data/var/zwartewater-patches.status'
```

Check status after any firmware update or before troubleshooting weird
current readings:
```bash
sshpass -p "$CERBO_ROOT_PASSWORD" ssh root@venus.local \
  'cat /data/var/zwartewater-patches.status'
```
