#!/bin/bash
# install.sh: Install the Digital Frame as a systemd service

if [[ $EUID -ne 0 ]]; then
   echo "This script must be run as root (use sudo)"
   exit 1
fi

PROJECT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
SERVICE_NAME="frame.service"

echo "Setting up Digital Frame service in $PROJECT_DIR..."

# 0. Install system dependencies
echo "Installing system dependencies..."
apt-get update
apt-get install -y python3-venv python3-pip nginx logrotate libopenjp2-7 libtiff6 libcamera-apps-lite dnsmasq network-manager

# 1. Setup Virtual Environment
echo "Setting up Python virtual environment and installing requirements..."
if [ ! -f "$PROJECT_DIR/venv/bin/activate" ]; then
    echo "Virtual environment not found or incomplete. Creating new one..."
    # Remove potentially broken/empty venv directory
    rm -rf "$PROJECT_DIR/venv"
    python3 -m venv "$PROJECT_DIR/venv"
fi

# Always update requirements to ensure 'coments' (commands) work after a pull
echo "Installing/Updating dependencies from requirements.txt..."
"$PROJECT_DIR/venv/bin/pip" install --upgrade pip
"$PROJECT_DIR/venv/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

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

# 4. Make scripts executable
chmod +x "$PROJECT_DIR/run_frame.sh"

# 5. Create the systemd service file
echo "Creating systemd service..."
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

# Ensure config.ini exists
if [ ! -f "$PROJECT_DIR/config.ini" ]; then
    echo "Creating config.ini from example..."
    cp "$PROJECT_DIR/config.ini.example" "$PROJECT_DIR/config.ini"
fi 

# 6. Reload systemd, enable and start the service
#
systemctl daemon-reload
systemctl enable $SERVICE_NAME
systemctl restart $SERVICE_NAME

echo "------------------------------------------------"
echo "Installation complete!"
echo "Service '$SERVICE_NAME' is installed and running."
echo "Use 'systemctl status $SERVICE_NAME' to check status."
echo "Use 'journalctl -u $SERVICE_NAME -f' to see live logs."
echo "------------------------------------------------"
