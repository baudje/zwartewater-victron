#!/bin/bash
# Remote installer for FLA equalisation service on Venus OS
# Usage: wget -qO- https://raw.githubusercontent.com/baudje/zwartewater-victron/main/fla-equalisation/install-remote.sh | bash
set -e

REPO="baudje/zwartewater-victron"
BRANCH="main"
BASE_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/fla-equalisation"
INSTALL_DIR="/data/apps/fla-equalisation"
SERVICE_DIR="${INSTALL_DIR}/service"
LOG_DIR="/data/log"

echo "Installing FLA equalisation service from GitHub..."

# Stop existing service if running
svc -d /service/fla-equalisation 2>/dev/null || true
sleep 2

# Create directories
mkdir -p "${INSTALL_DIR}"
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# Download all files
echo "Downloading files..."
for f in \
    fla_equalisation.py \
    dbus_monitor.py \
    dbus_status_service.py \
    settings.py \
    alerting.py \
    web_server.py \
; do
    wget -qO "${INSTALL_DIR}/${f}" "${BASE_URL}/${f}"
    echo "  ${f}"
done

# Download service/run
wget -qO "${SERVICE_DIR}/run" "${BASE_URL}/service/run"
echo "  service/run"

# Download shared modules
SHARED_DIR="/data/apps/fla-shared"
SHARED_URL="https://raw.githubusercontent.com/${REPO}/${BRANCH}/fla-shared"
mkdir -p "${SHARED_DIR}"
echo "Downloading shared modules..."
for f in \
    __init__.py \
    relay_control.py \
    voltage_matching.py \
    aggregate_driver.py \
    lock.py \
    dbus_monitor.py \
    alerting.py \
    temp_battery.py \
    temp_battery_process.py \
    temp_compensation.py \
; do
    wget -qO "${SHARED_DIR}/${f}" "${SHARED_URL}/${f}"
    echo "  ${f}"
done
mkdir -p "${SHARED_DIR}/ext"
ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${SHARED_DIR}/ext/velib_python"
echo "Shared modules installed to ${SHARED_DIR}"

# Make executable
chmod +x "${INSTALL_DIR}/fla_equalisation.py"
chmod +x "${SERVICE_DIR}/run"

# Symlink velib_python from aggregate batteries
if [ -d "/data/apps/dbus-aggregate-batteries/ext/velib_python" ]; then
    mkdir -p "${INSTALL_DIR}/ext"
    ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${INSTALL_DIR}/ext/velib_python"
    echo "Linked velib_python from dbus-aggregate-batteries"
else
    echo "ERROR: velib_python not found at /data/apps/dbus-aggregate-batteries/ext/velib_python"
    echo "Please install dbus-aggregate-batteries first."
    exit 1
fi

# Remove old cron job if present
rm -f /etc/cron.d/fla-equalisation

# Create daemontools service symlink
ln -sfn "${SERVICE_DIR}" /service/fla-equalisation
echo "Service symlink created"

# Add to rc.local for persistence across Venus OS firmware upgrades
if ! grep -q "fla-equalisation" /data/rc.local 2>/dev/null; then
    echo "" >> /data/rc.local
    echo "# FLA Equalisation service" >> /data/rc.local
    echo "ln -sfn /data/apps/fla-equalisation/service /service/fla-equalisation" >> /data/rc.local
    echo "Added to /data/rc.local for auto-start on firmware upgrade"
fi

# Wait for service to start
sleep 3

echo ""
echo "Installation complete."
echo "Service status: $(svstat /service/fla-equalisation 2>/dev/null || echo 'starting...')"
echo "Web UI: http://venus.local:8088"
echo ""
echo "To install configs (optional):"
echo "  wget -qO /data/apps/dbus-aggregate-batteries/config.ini ${BASE_URL}/../config/config.ini"
echo "  wget -qO /data/apps/dbus-serialbattery/config.ini ${BASE_URL}/../config/sb-config.ini"
