#!/bin/bash
# Install FLA charge service on Venus OS
set -e

INSTALL_DIR="/data/apps/fla-charge"
SERVICE_DIR="${INSTALL_DIR}/service"
LOG_DIR="/data/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FLA charge service to ${INSTALL_DIR}..."

# Stop existing service if running
svc -d /service/fla-charge 2>/dev/null || true
sleep 2

# Create directories
mkdir -p "${INSTALL_DIR}"
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# Copy files
cp "${SCRIPT_DIR}/fla_charge.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_status_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/settings.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/web_server.py" "${INSTALL_DIR}/"

# Install shared modules
SHARED_DIR="/data/apps/fla-shared"
mkdir -p "${SHARED_DIR}"
if [ -d "${SCRIPT_DIR}/../fla-shared" ]; then
    for f in __init__.py relay_control.py voltage_matching.py aggregate_driver.py lock.py dbus_monitor.py alerting.py temp_battery.py; do
        cp "${SCRIPT_DIR}/../fla-shared/${f}" "${SHARED_DIR}/"
    done
    mkdir -p "${SHARED_DIR}/ext"
    ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${SHARED_DIR}/ext/velib_python"
    echo "Shared modules installed to ${SHARED_DIR}"
fi

# Copy service/run
cp "${SCRIPT_DIR}/service/run" "${SERVICE_DIR}/run"

# Make executable
chmod +x "${INSTALL_DIR}/fla_charge.py"
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

# Create daemontools service symlink
ln -sfn "${SERVICE_DIR}" /service/fla-charge
echo "Service symlink created at /service/fla-charge"

# Add to rc.local for persistence across Venus OS firmware upgrades
if ! grep -q "fla-charge" /data/rc.local 2>/dev/null; then
    echo "" >> /data/rc.local
    echo "# FLA Charge service" >> /data/rc.local
    echo "ln -sfn /data/apps/fla-charge/service /service/fla-charge" >> /data/rc.local
    echo "Added to /data/rc.local for auto-start on firmware upgrade"
fi

# Service starts automatically via daemontools
sleep 3
echo ""
echo "Installation complete."
echo "Service status: $(svstat /service/fla-charge 2>/dev/null || echo 'starting...')"
echo "Web UI available at http://venus.local:8089"
