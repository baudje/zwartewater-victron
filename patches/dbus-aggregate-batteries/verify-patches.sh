#!/bin/sh
# Verify that the Zwartewater behaviour is present in the deployed
# dbus-aggregate-batteries driver. A Venus OS firmware update or driver
# self-update can replace the upstream files and silently drop our patches.
#
# Checks for behaviour rather than a marker comment: the same identifiers
# would be present whether the patches are applied locally OR have been
# merged upstream and pulled in via a fresh download. So this stays green
# after our PRs (Dr-Gigavolt/dbus-aggregate-batteries#152 / #154) land
# upstream — only flips red when the behaviour is genuinely missing.
#
# Run on the Cerbo (manually, from cron, or from /data/rc.local). Writes a
# warning to syslog and to /data/var/zwartewater-patches.status. Exit codes:
#   0 — all expected identifiers present
#   1 — one or more files are missing required identifiers
#   2 — driver directory not found

DRIVER_DIR=/data/apps/dbus-aggregate-batteries
STATUS_FILE=/data/var/zwartewater-patches.status

mkdir -p "$(dirname "$STATUS_FILE")"

if [ ! -d "$DRIVER_DIR" ]; then
    msg="ERROR: $DRIVER_DIR not found"
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $msg" > "$STATUS_FILE"
    logger -t zwartewater-patches -p user.err "$msg"
    exit 2
fi

# Each file must contain ALL of its listed identifiers.
# Add new behaviour checks here as patches grow.
check_file() {
    file=$1; shift
    for needle in "$@"; do
        if ! grep -q -- "$needle" "$DRIVER_DIR/$file" 2>/dev/null; then
            return 1
        fi
    done
    return 0
}

missing=""
check_file settings.py get_smartshunts_from_config SMARTSHUNT_AS_BATTERY_CURRENT \
    || missing="$missing settings.py"
check_file dbus-aggregate-batteries.py SMARTSHUNT_AS_BATTERY_CURRENT \
    || missing="$missing dbus-aggregate-batteries.py"
check_file config.default.ini SMARTSHUNT_AS_BATTERY_CURRENT \
    || missing="$missing config.default.ini"

if [ -n "$missing" ]; then
    msg="WARNING: dbus-aggregate-batteries behaviour missing in:$missing — re-deploy from patches/dbus-aggregate-batteries/"
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $msg" > "$STATUS_FILE"
    logger -t zwartewater-patches -p user.warn "$msg"
    exit 1
fi

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') OK: aggregate behaviour intact (settings.py, dbus-aggregate-batteries.py, config.default.ini)" > "$STATUS_FILE"
exit 0
