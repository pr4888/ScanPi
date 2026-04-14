#!/usr/bin/env bash
# ScanPi installer — one-shot setup on any fresh Linux (Debian/Ubuntu/Raspberry Pi OS).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pr4888/ScanPi/master/install.sh | sudo bash
#
# Or, after cloning:
#   sudo bash install.sh
#
# Idempotent — safe to re-run.
#
# Installs: GNU Radio, RTL-SDR, faster-whisper, ScanPi, systemd unit.
# Web UI on http://<host>:8080/ once complete.

set -euo pipefail

SCANPI_USER="${SCANPI_USER:-scanpi}"
SCANPI_REPO="${SCANPI_REPO:-https://github.com/pr4888/ScanPi}"
SCANPI_BRANCH="${SCANPI_BRANCH:-master}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/scanpi}"

log()  { printf "\033[32m[scanpi]\033[0m %s\n" "$*"; }
warn() { printf "\033[33m[scanpi]\033[0m %s\n" "$*" >&2; }
die()  { printf "\033[31m[scanpi]\033[0m %s\n" "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Please run as root (sudo)."

log "== ScanPi installer =="
log "repo=$SCANPI_REPO branch=$SCANPI_BRANCH user=$SCANPI_USER root=$INSTALL_ROOT"

# 1. Packages -----------------------------------------------------------------
log "apt dependencies (this may take a few minutes)…"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends \
    git curl ca-certificates \
    gnuradio gr-osmosdr rtl-sdr libusb-1.0-0 \
    python3-venv python3-pip python3-numpy \
    libsndfile1 ffmpeg \
    >/dev/null

# 2. User ---------------------------------------------------------------------
if ! id "$SCANPI_USER" &>/dev/null; then
    log "creating system user '$SCANPI_USER'"
    useradd --system --create-home --shell /bin/bash --groups plugdev,dialout "$SCANPI_USER"
fi

# 3. Repo ---------------------------------------------------------------------
if [[ ! -d "$INSTALL_ROOT/.git" ]]; then
    log "cloning $SCANPI_REPO into $INSTALL_ROOT"
    git clone --depth=1 --branch "$SCANPI_BRANCH" "$SCANPI_REPO" "$INSTALL_ROOT"
else
    log "updating existing clone"
    git -C "$INSTALL_ROOT" fetch --depth=1 origin "$SCANPI_BRANCH"
    git -C "$INSTALL_ROOT" reset --hard "origin/$SCANPI_BRANCH"
fi
chown -R "$SCANPI_USER:$SCANPI_USER" "$INSTALL_ROOT"

# 4. Python deps --------------------------------------------------------------
log "installing Python dependencies (ScanPi + faster-whisper)"
sudo -u "$SCANPI_USER" bash -c "
    cd '$INSTALL_ROOT' && \
    pip install --break-system-packages --user --upgrade pip setuptools wheel >/dev/null && \
    pip install --break-system-packages --user 'numpy<2' >/dev/null && \
    pip install --break-system-packages --user -e . >/dev/null && \
    pip install --break-system-packages --user faster-whisper >/dev/null
"

# 5. RTL-SDR udev rules (plug-and-play access without root) -------------------
UDEV_RULES=/etc/udev/rules.d/20-rtlsdr.rules
if [[ ! -f "$UDEV_RULES" ]]; then
    log "installing RTL-SDR udev rules"
    cat > "$UDEV_RULES" <<'UDEV'
# RTL2832U-based DVB-T sticks (NESDR, generic RTL-SDR, etc.)
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
UDEV
    udevadm control --reload-rules && udevadm trigger
fi

# 6. Blacklist DVB-T kernel module (steals the SDR otherwise) ----------------
if ! grep -q dvb_usb_rtl28xxu /etc/modprobe.d/blacklist-rtlsdr.conf 2>/dev/null; then
    log "blacklisting DVB-T kernel drivers so the SDR is free for ScanPi"
    cat > /etc/modprobe.d/blacklist-rtlsdr.conf <<'BLACKLIST'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
BLACKLIST
fi

# 7. systemd unit -------------------------------------------------------------
UNIT=/etc/systemd/system/scanpi.service
log "writing systemd unit $UNIT"
cat > "$UNIT" <<SYSTEMD
[Unit]
Description=ScanPi — modular home radio scanner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SCANPI_USER
WorkingDirectory=$INSTALL_ROOT
ExecStart=/home/$SCANPI_USER/.local/bin/scanpi-v3
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=multi-user.target
SYSTEMD

systemctl daemon-reload
systemctl enable --now scanpi.service

sleep 3
if systemctl is-active --quiet scanpi.service; then
    host="$(hostname -I | awk '{print $1}')"
    log ""
    log "=========================================="
    log "  ScanPi is running."
    log "  Open: http://${host}:8080/"
    log "  Logs: journalctl -u scanpi -f"
    log "=========================================="
else
    warn "scanpi.service did not start — check: journalctl -u scanpi -n 40"
    exit 1
fi
