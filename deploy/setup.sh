#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/jlcpcb-parts-db"

echo "=== Setting up jlcpcb-parts-db ==="

# System deps
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends python3 python3-venv p7zip-full

# Create venv
if [ ! -d "$INSTALL_DIR/.venv" ]; then
    python3 -m venv "$INSTALL_DIR/.venv"
fi
source "$INSTALL_DIR/.venv/bin/activate"
pip install --quiet --upgrade pip
pip install --quiet -r "$INSTALL_DIR/requirements.txt"

# Data directory
mkdir -p "$INSTALL_DIR/data/upstream"

# Cron
CRON_FILE="/etc/cron.d/jlcpcb-parts-db"
if [ ! -f "$CRON_FILE" ]; then
    echo "0 6 * * * ubuntu /opt/jlcpcb-parts-db/deploy/cron-update.sh >> /opt/jlcpcb-parts-db/data/cron.log 2>&1" | sudo tee "$CRON_FILE"
    sudo chmod 644 "$CRON_FILE"
    echo "Cron installed: daily at 06:00 UTC"
fi

echo "=== Setup complete ==="
