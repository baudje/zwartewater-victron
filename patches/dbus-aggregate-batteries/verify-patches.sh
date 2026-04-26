#!/bin/sh
# Verify that the Zwartewater patches are still present in the deployed
# dbus-aggregate-batteries driver. A Venus OS firmware update can replace
# the upstream files and silently revert our patches.
#
# Run on the Cerbo (manually, from cron, or from /data/rc.local). Writes a
# warning to syslog and to /data/var/zwartewater-patches.status. Exit codes:
#   0 — all patched files contain the [Zwartewater patch] marker
#   1 — one or more files are missing the marker (probably reverted)
#   2 — driver directory not found

DRIVER_DIR=/data/apps/dbus-aggregate-batteries
STATUS_FILE=/data/var/zwartewater-patches.status
MARKER='[Zwartewater patch]'
FILES='settings.py dbus-aggregate-batteries.py config.default.ini'

mkdir -p "$(dirname "$STATUS_FILE")"

if [ ! -d "$DRIVER_DIR" ]; then
    msg="ERROR: $DRIVER_DIR not found"
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $msg" > "$STATUS_FILE"
    logger -t zwartewater-patches -p user.err "$msg"
    exit 2
fi

missing=""
for f in $FILES; do
    if ! grep -q "$MARKER" "$DRIVER_DIR/$f" 2>/dev/null; then
        missing="$missing $f"
    fi
done

if [ -n "$missing" ]; then
    msg="WARNING: dbus-aggregate-batteries patches missing in:$missing — re-deploy from patches/dbus-aggregate-batteries/"
    echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') $msg" > "$STATUS_FILE"
    logger -t zwartewater-patches -p user.warn "$msg"
    exit 1
fi

echo "$(date -u +'%Y-%m-%dT%H:%M:%SZ') OK: patches present in$( for f in $FILES; do printf ' %s' "$f"; done )" > "$STATUS_FILE"
exit 0
