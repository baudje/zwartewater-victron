#!/bin/bash
# Install FLA equalisation service on Venus OS
set -e

INSTALL_DIR="/data/apps/fla-equalisation"
SERVICE_DIR="${INSTALL_DIR}/service"
LOG_DIR="/data/log"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FLA equalisation service to ${INSTALL_DIR}..."

# Stop existing service if running
svc -d /service/fla-equalisation 2>/dev/null || true
sleep 2

# Create directories
mkdir -p "${INSTALL_DIR}"
mkdir -p "${SERVICE_DIR}"
mkdir -p "${LOG_DIR}"

# Copy files
cp "${SCRIPT_DIR}/fla_equalisation.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_battery_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_monitor.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_status_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/settings.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/alerting.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/web_server.py" "${INSTALL_DIR}/"

# Copy service/run
cp "${SCRIPT_DIR}/service/run" "${SERVICE_DIR}/run"

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
echo "Service symlink created at /service/fla-equalisation"

# Add to rc.local for persistence across Venus OS firmware upgrades
if ! grep -q "fla-equalisation" /data/rc.local 2>/dev/null; then
    echo "" >> /data/rc.local
    echo "# FLA Equalisation service" >> /data/rc.local
    echo "ln -sfn /data/apps/fla-equalisation/service /service/fla-equalisation" >> /data/rc.local
    echo "Added to /data/rc.local for auto-start on firmware upgrade"
fi

# Service starts automatically via daemontools
sleep 3
echo ""
echo "Installation complete."
echo "Service status: $(svstat /service/fla-equalisation 2>/dev/null || echo 'starting...')"
echo "Configure settings via Cerbo GX Device List or VRM."
