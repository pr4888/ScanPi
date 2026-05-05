#!/usr/bin/env bash
# ScanPi first-boot installer.
#
# Designed to be run from /boot/firstrun.sh on a fresh Raspberry Pi OS Lite,
# OR via Raspberry Pi Imager's "Run this command on first boot" field as:
#
#   bash -c "curl -fsSL https://raw.githubusercontent.com/pr4888/ScanPi/main/install/firstboot.sh | bash"
#
# What it does:
#   1. Installs git
#   2. Clones the ScanPi repo to /opt/ScanPi
#   3. Runs install.sh with autodetect (lite on a Pi)
#   4. Drops a sentinel so it never runs twice
#
# Safe to run multiple times — the sentinel check no-ops on subsequent boots.

set -euo pipefail

SENTINEL=/var/lib/scanpi/.installed
LOG=/var/log/scanpi-firstboot.log
REPO_URL="https://github.com/pr4888/ScanPi.git"
CLONE_DIR=/opt/ScanPi

mkdir -p /var/lib/scanpi /var/log
exec > >(tee -a "$LOG") 2>&1
echo "[$(date)] firstboot starting"

if [ -f "$SENTINEL" ]; then
  echo "already installed (sentinel: $SENTINEL); skipping"
  exit 0
fi

# ---- Wait for network -------------------------------------------------
echo "waiting for network..."
for i in $(seq 1 60); do
  if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then
    echo "network up"
    break
  fi
  sleep 2
done

# ---- Install git if missing ------------------------------------------
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq git
fi

# ---- Clone repo (or pull if already there) ---------------------------
if [ -d "$CLONE_DIR/.git" ]; then
  cd "$CLONE_DIR" && git pull --ff-only
else
  rm -rf "$CLONE_DIR"
  git clone --depth=1 "$REPO_URL" "$CLONE_DIR"
fi

# ---- Pick install user (default to first regular user) ---------------
INSTALL_USER="${SCANPI_USER:-}"
if [ -z "$INSTALL_USER" ]; then
  # First regular user (uid >= 1000, has a real shell)
  INSTALL_USER=$(awk -F: '$3 >= 1000 && $3 < 65534 && $7 !~ /(false|nologin)/ {print $1; exit}' /etc/passwd)
fi
[ -n "$INSTALL_USER" ] || { echo "couldn't pick an install user"; exit 1; }
echo "installing for user: $INSTALL_USER"

# ---- Run installer ---------------------------------------------------
bash "$CLONE_DIR/install/install.sh" --user="$INSTALL_USER"

# ---- Sentinel --------------------------------------------------------
date > "$SENTINEL"
echo "[$(date)] firstboot complete"

# Clean up Raspberry Pi OS firstrun marker if present
[ -f /boot/firstrun.sh ] && rm -f /boot/firstrun.sh || true
