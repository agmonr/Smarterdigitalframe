#!/bin/bash
# install.sh: Install the Digital Frame as a systemd service
#
# Run from an existing checkout:
#   sudo ./install.sh
# Or bootstrap directly into an empty folder (clones the repo first):
#   curl -fsSL https://raw.githubusercontent.com/agmonr/Smarterdigitalframe/main/install.sh | sudo bash

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

REPO_URL="https://github.com/agmonr/Smarterdigitalframe.git"
INSTALL_USER="${SUDO_USER:-$(whoami)}"
INSTALL_USER_HOME="$(getent passwd "$INSTALL_USER" | cut -d: -f6)"
DEFAULT_INSTALL_DIR="${INSTALL_USER_HOME:-/root}/Smarterdigitalframe"

# When this script is piped in (e.g. `curl ... | sudo bash`), there is no
# real file on disk, so $BASH_SOURCE doesn't point at a checkout. Detect that
# case by checking whether a sibling common.py exists next to this script;
# if not, clone the repo first and operate on that copy instead.
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]:-.}" )" >/dev/null 2>&1 && pwd )"
if [ -f "$SCRIPT_DIR/common.py" ]; then
    PROJECT_DIR="$SCRIPT_DIR"
else
    PROJECT_DIR="$DEFAULT_INSTALL_DIR"
    echo "No existing checkout found next to this script; bootstrapping into $PROJECT_DIR..."
    command -v git &> /dev/null || { apt-get update && apt-get install -y git; }
    if [ -d "$PROJECT_DIR/.git" ]; then
        git -C "$PROJECT_DIR" pull
    else
        git clone "$REPO_URL" "$PROJECT_DIR"
    fi
fi

# frame.service runs as root (see below), and the auto-update thread in
# api.py runs `git` commands as root too — mark the repo as safe for root to
# operate on regardless of who owns it (e.g. a manual `git clone` done as a
# regular user before running this script), avoiding git's "dubious
# ownership" refusal.
git config --global --add safe.directory "$PROJECT_DIR"

SERVICE_NAME="frame.service"

echo "Setting up Digital Frame service in $PROJECT_DIR..."

# 0. Install system dependencies
echo "Installing system dependencies..."
apt-get update
apt-get install -y bluez python3-venv python3-pip nginx \
	logrotate libopenjp2-7 libtiff6 libcamera-apps-lite\
       	dnsmasq network-manager apt-listchanges cloud-init libglx-mesa0 rfkill

# Unblock Wi-Fi and Bluetooth in case they were soft-blocked (e.g. by the
# OS default or a prior config) — BLE proximity and Wi-Fi setup need both.
echo "Unblocking Wi-Fi and Bluetooth via rfkill..."
rfkill unblock wifi
rfkill unblock bluetooth

# Mount the boot partition read-only to further reduce SD card wear (this
# repo already keeps frequently-written runtime state off the card via
# /dev/shm; see common.py). Raspberry Pi OS Bookworm+ mounts it at
# /boot/firmware, older releases at /boot. Takes effect on next reboot;
# `mount -o remount,rw /boot...` before editing config.txt etc.
BOOT_MOUNT=""
if mountpoint -q /boot/firmware 2>/dev/null; then
    BOOT_MOUNT="/boot/firmware"
elif mountpoint -q /boot 2>/dev/null; then
    BOOT_MOUNT="/boot"
fi

if [ -n "$BOOT_MOUNT" ]; then
    FSTAB_LINE=$(grep -E "^\S+[[:space:]]+${BOOT_MOUNT//\//\\/}[[:space:]]" /etc/fstab || true)
    if [ -z "$FSTAB_LINE" ]; then
        echo "No /etc/fstab entry found for $BOOT_MOUNT; skipping read-only change."
    elif echo "$FSTAB_LINE" | awk '{print $4}' | grep -qw "ro"; then
        echo "$BOOT_MOUNT is already read-only in /etc/fstab."
    else
        echo "Setting $BOOT_MOUNT to read-only in /etc/fstab (takes effect after reboot)..."
        cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%d%H%M%S)"
        sed -i -E "s|^(\S+[[:space:]]+${BOOT_MOUNT//\//\\/}[[:space:]]+\S+[[:space:]]+)([^[:space:]]+)|\1\2,ro|" /etc/fstab
    fi
else
    echo "Could not detect boot partition mount point; skipping fstab read-only change."
fi

# 1. Setup Virtual Environment with uv
echo "Setting up Python virtual environment and installing requirements using uv..."

# Ensure uv is available
if ! command -v uv &> /dev/null; then
    if [ -f "$HOME/.local/bin/uv" ]; then
        export PATH="$HOME/.local/bin:$PATH"
    elif [ -f "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    else
        echo "uv not found, installing..."
        curl -LsSf https://astral.sh/uv/install.sh | sh
        # The installer usually puts it in $HOME/.local/bin
        export PATH="$HOME/.local/bin:$PATH"
        # Also try to source the env file if it was created
        [ -f "$HOME/.local/bin/env" ] && source "$HOME/.local/bin/env"
    fi
fi

# Final check
if ! command -v uv &> /dev/null; then
    echo "Error: uv could not be installed or found in PATH."
    exit 1
fi

if [ ! -d "$PROJECT_DIR/venv" ]; then
    echo "Virtual environment not found. Creating new one with uv..."
    uv venv "$PROJECT_DIR/venv"
fi

# Always update requirements
echo "Installing/Updating dependencies from requirements.txt using uv..."
uv pip install --python "$PROJECT_DIR/venv/bin/python" -r "$PROJECT_DIR/requirements.txt"

# 2. Configure Nginx
echo "Configuring Nginx..."
# Update paths in nginx config to match current installation directory
sed -i "s|/home/ram/photos/digitalframe|$PROJECT_DIR|g" "$PROJECT_DIR/digitalframe.nginx"
cp "$PROJECT_DIR/digitalframe.nginx" /etc/nginx/sites-available/digitalframe
ln -sf /etc/nginx/sites-available/digitalframe /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Ensure static directory and loading image exist for 502 page
mkdir -p "$PROJECT_DIR/static"
if [ ! -f "$PROJECT_DIR/static/loading.png" ] && [ -f "$PROJECT_DIR/images/IMG-20260521-WA0001.jpg" ]; then
    cp "$PROJECT_DIR/images/IMG-20260521-WA0001.jpg" "$PROJECT_DIR/static/loading.png"
fi

nginx -t && systemctl restart nginx

# 3. Setup Logging and Directories
echo "Setting up logging and directories..."
mkdir -p "$PROJECT_DIR/logs"
chmod 777 "$PROJECT_DIR/logs"

# Create directories for images and captures if they don't exist
# We'll use the current user's home or project dir as appropriate
mkdir -p "$PROJECT_DIR/images"
mkdir -p "$PROJECT_DIR/google_photos"
mkdir -p "$PROJECT_DIR/captures"
mkdir -p "$PROJECT_DIR/removed"
chmod -R 777 "$PROJECT_DIR/images" "$PROJECT_DIR/google_photos" "$PROJECT_DIR/captures" "$PROJECT_DIR/removed"

# Create logrotate config
cat <<EOF > /etc/logrotate.d/digitalframe
$PROJECT_DIR/logs/*.log {
    daily
    rotate 7
    compress
    missingok
    copytruncate
}
EOF
chmod 644 /etc/logrotate.d/digitalframe

# Ensure config.ini exists
if [ ! -f "$PROJECT_DIR/config.ini" ]; then
    echo "Creating config.ini from example..."
    cp "$PROJECT_DIR/config.ini.example" "$PROJECT_DIR/config.ini"
fi

# 4. Create/refresh the systemd service and (re)start it
chmod +x "$PROJECT_DIR/update_service.sh"
"$PROJECT_DIR/update_service.sh" "$PROJECT_DIR" "$SERVICE_NAME"

echo "------------------------------------------------"
echo "Installation complete!"
echo "------------------------------------------------"
