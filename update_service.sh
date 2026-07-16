#!/bin/bash
# update_service.sh: (Re)create and restart the Digital Frame systemd service,
# pointing it at a specific project folder.
#
# Called by install.sh after setup, but also standalone — handy for testing
# a checkout without running the full installer:
#   sudo ./update_service.sh                              # use this checkout, frame.service
#   sudo ./update_service.sh /path/to/checkout             # point frame.service at another folder
#   sudo ./update_service.sh /path/to/checkout frame-test.service   # separate service, won't clobber prod
#
# Usage: sudo ./update_service.sh [PROJECT_DIR] [SERVICE_NAME]

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-.}" )" >/dev/null 2>&1 && pwd )"
PROJECT_DIR="${1:-$SCRIPT_DIR}"
SERVICE_NAME="${2:-frame.service}"

if [ ! -f "$PROJECT_DIR/run_frame.sh" ]; then
    echo "Error: $PROJECT_DIR does not look like a Digital Frame checkout (no run_frame.sh found)."
    exit 1
fi

echo "Pointing $SERVICE_NAME at $PROJECT_DIR..."

chmod +x "$PROJECT_DIR/run_frame.sh"

cat <<EOF > /etc/systemd/system/$SERVICE_NAME
[Unit]
Description=Digital Frame Display Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$PROJECT_DIR
ExecStart=$PROJECT_DIR/run_frame.sh
Restart=always
# Ensure output to the first console
StandardOutput=tty
TTYPath=/dev/tty1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

echo "------------------------------------------------"
echo "Service '$SERVICE_NAME' now points at $PROJECT_DIR and is running."
echo "Use 'systemctl status $SERVICE_NAME' to check status."
echo "Use 'journalctl -u $SERVICE_NAME -f' to see live logs."
echo "------------------------------------------------"
