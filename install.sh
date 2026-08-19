#!/bin/bash

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

WHERE=$(dirname "$(readlink -f "$0")")

# Get their tailscale IP address
TAILSCALE_IP=$(tailscale ip -4)
if [ -z "$TAILSCALE_IP" ]; then
    echo "Could not determine Tailscale IP address. Please check your Tailscale configuration."
    echo "We assume you have tailscale because we do no other encryption. Cowardly bailing out."
    exit 1
fi

ARGS="--host=${TAILSCALE_IP} --port=8080"

# Ask what user we should run as, default to minecraft
AS_USER=$(grep "User=" /etc/systemd/system/vibepanel.service | cut -d'=' -f2 || echo "")
if [ -z "${AS_USER}" ]; then
    read -p "Run as user (leave blank for minecraft): " AS_USER
    AS_USER=${AS_USER:-minecraft}
fi

INTO=$(grep "WorkingDirectory=" /etc/systemd/system/vibepanel.service | cut -d'=' -f2 || echo "")
if [ -z "${INTO}" ]; then
    read -p "Install into (leave blank for /home/${AS_USER}/vibepanel): " INTO
    INTO=${INTO:-/home/${AS_USER}/vibepanel}
fi

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
export INTO
export AS_USER
export ARGS
envsubst < vibepanel.service | sudo tee /etc/systemd/system/vibepanel.service > /dev/null
systemctl daemon-reload
systemctl enable vibepanel
systemctl restart vibepanel

echo "Installation complete. You can check the status with 'systemctl status vibepanel' and view logs with 'journalctl -u vibepanel -f'."
echo "If you need to change the user, port, or session name, edit the service file at /etc/systemd/system/vibepanel.service and run 'systemctl daemon-reload' again."
echo "To uninstall, run 'systemctl disable --now vibepanel' and remove the service file."
echo "Remember this service does NOT use SSL. You should NOT open a port for it. Access it through tailscale."
echo
echo
echo "You can access the panel at http://${TAILSCALE_IP}:8080"
echo
echo "Have fun!"