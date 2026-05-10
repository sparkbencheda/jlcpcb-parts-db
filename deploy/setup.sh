#!/bin/bash
set -euo pipefail

INSTALL_DIR="/opt/jlcpcb-parts-db"
REPO_URL="https://github.com/sparkbencheda/jlcpcb-parts-db.git"

echo "=== Setting up jlcpcb-parts-db ==="

# System deps
sudo apt-get update -qq
sudo apt-get install -y --no-install-recommends python3 python3-venv p7zip-full git

# Clone or update repo
if [ -d "$INSTALL_DIR/.git" ]; then
    echo "Updating existing repo..."
    git -C "$INSTALL_DIR" pull --ff-only
else
    if [ -d "$INSTALL_DIR" ]; then
        echo "Existing non-git install found, backing up data..."
        mkdir -p /tmp/jlcpcb-backup
        cp -a "$INSTALL_DIR/data" /tmp/jlcpcb-backup/data 2>/dev/null || true
        cp "$INSTALL_DIR/proxies.txt" /tmp/jlcpcb-backup/ 2>/dev/null || true
        sudo rm -rf "$INSTALL_DIR"
    fi

    sudo git clone "$REPO_URL" "$INSTALL_DIR"
    sudo chown -R ubuntu:ubuntu "$INSTALL_DIR"

    # Restore data and proxies if backed up
    if [ -d /tmp/jlcpcb-backup/data ]; then
        cp -a /tmp/jlcpcb-backup/data "$INSTALL_DIR/"
        echo "Restored data directory from backup"
    fi
    if [ -f /tmp/jlcpcb-backup/proxies.txt ]; then
        cp /tmp/jlcpcb-backup/proxies.txt "$INSTALL_DIR/"
        echo "Restored proxies.txt from backup"
    fi
    rm -rf /tmp/jlcpcb-backup
fi

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
echo "0 6 * * * ubuntu $INSTALL_DIR/deploy/cron-update.sh >> $INSTALL_DIR/data/cron.log 2>&1" | sudo tee "$CRON_FILE" > /dev/null
sudo chmod 644 "$CRON_FILE"
echo "Cron installed: daily at 06:00 UTC"

echo "=== Setup complete ==="
