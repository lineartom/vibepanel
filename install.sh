#!/bin/sh

set -euo pipefail

# check if python3 is available
if ! command -v python3 >/dev/null 2>&1; then
    echo "python3 is required but not found. Please install python3."
    exit 1
fi

# check if sudo is available
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Please run with sudo."
    exit 1
fi

# Ask what user we should run as, default to minecraft
read -p "Run as user (leave blank for minecraft): " AS_USER
AS_USER=${AS_USER:-minecraft}
WHERE=$(dirname "$(readlink -f "$0")")

read -p "Install into (leave blank for /home/${AS_USER}/vibepanel): " INTO
INTO=${INTO:-/home/${AS_USER}/vibepanel}

# If target already exists, ask if we should overwrite (upgrade) it
if [ -d "${INTO}" ]; then
    read -p "Target ${INTO} already exists. Do you want to overwrite (upgrade) it? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborting installation."
        exit 1
    fi
else
    mkdir -p "${INTO}"
fi

echo "Changing to user ${AS_USER} and installing dependencies..."
cp -r ${WHERE}/* ${INTO}/
cd ${INTO}
chown -R ${AS_USER} ./

sudo -u ${AS_USER} python3 -m venv .venv --upgrade-deps
sudo -u ${AS_USER} .venv/bin/pip install -q -r requirements.txt

echo "Creating systemd service file..."
cat <<EOF > /etc/systemd/system/vibepanel.service
# Drop this in /etc/systemd/system/ and run:
#   systemctl daemon-reload
#   systemctl enable --now vibepanel
#
# Adjust User, WorkingDirectory, and --session as needed.

[Unit]
Description=VibePanel — Minecraft web frontend
After=network.target

[Service]
Type=simple
User=${AS_USER}
WorkingDirectory=${INTO}
ExecStart=${INTO}/.venv/bin/python server.py --session minecraft --port 8080
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now vibepanel

echo "Installation complete. You can check the status with 'systemctl status vibepanel' and view logs with 'journalctl -u vibepanel -f'."
echo "If you need to change the user, port, or session name, edit the service file at /etc/systemd/system/vibepanel.service and run 'systemctl daemon-reload' again."
echo "To uninstall, run 'systemctl disable --now vibepanel' and remove the service file."
echo "Remember this service does NOT use SSL. You should NOT open a port for it. Access it through tailscale."
echo
echo
# Get their tailscale IP address
TAILSCALE_IP=$(tailscale ip -4)
if [ -n "$TAILSCALE_IP" ]; then
    echo "You can access the panel at http://${TAILSCALE_IP}:8080"
else
    echo "Could not determine Tailscale IP address. Please check your Tailscale configuration"
fi
echo
echo "Have fun!"