#!/bin/bash
# Install FLA equalisation script on Venus OS
set -e

INSTALL_DIR="/data/apps/fla-equalisation"
LOG_DIR="/data/log"
CRON_FILE="/etc/cron.d/fla-equalisation"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing FLA equalisation script to ${INSTALL_DIR}..."

# Create directories
mkdir -p "${INSTALL_DIR}"
mkdir -p "${LOG_DIR}"

# Copy files
cp "${SCRIPT_DIR}/fla_equalisation.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_battery_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_monitor.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/dbus_status_service.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/settings.py" "${INSTALL_DIR}/"
cp "${SCRIPT_DIR}/alerting.py" "${INSTALL_DIR}/"

# Make executable
chmod +x "${INSTALL_DIR}/fla_equalisation.py"

# Symlink velib_python from aggregate batteries if available, else copy
if [ -d "/data/apps/dbus-aggregate-batteries/ext/velib_python" ]; then
    ln -sfn /data/apps/dbus-aggregate-batteries/ext/velib_python "${INSTALL_DIR}/ext"
    echo "Linked velib_python from dbus-aggregate-batteries"
else
    echo "ERROR: velib_python not found at /data/apps/dbus-aggregate-batteries/ext/velib_python"
    echo "Please install dbus-aggregate-batteries first."
    exit 1
fi

# Install cron job (runs every hour)
cat > "${CRON_FILE}" << 'EOF'
0 * * * * root /usr/bin/python3 /data/apps/fla-equalisation/fla_equalisation.py >> /data/log/fla-equalisation.log 2>&1
EOF

echo "Installation complete."
echo "Configure settings via Cerbo GX GUI or VRM remote console."
