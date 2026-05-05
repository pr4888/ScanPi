#!/usr/bin/env bash
# ScanPi LITE install — Pi 5 / Pi 4 / ARM SBC.
#
# Sourced by install.sh. Don't run this directly unless you know why.
#
# Footprint:
#   - apt deps: ~600 MB
#   - python venv: ~250 MB
#   - whisper.cpp tiny.en model: ~75 MB
#   - mosquitto + paho-mqtt
#   - bge-small-en model OPT-IN, ~33 MB if enabled
#
# Total disk: ~1 GB. Designed to fit on an 8 GB SD with room for ~2 weeks of
# squelched audio.

set -euo pipefail

REPO_ROOT="${SCANPI_REPO_ROOT}"
USER_NAME="${SCANPI_INSTALL_USER}"
DRY_RUN="${SCANPI_DRY_RUN:-0}"
SKIP_DEPS="${SCANPI_SKIP_DEPS:-0}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"
[ -d "$USER_HOME" ] || { echo "user $USER_NAME has no home"; exit 1; }

run() {
  echo "+ $*"
  [ "$DRY_RUN" = "1" ] || eval "$@"
}

echo "=== installing ScanPi LITE for $USER_NAME (home: $USER_HOME) ==="

# ---- apt deps ----------------------------------------------------------
if [ "$SKIP_DEPS" != "1" ]; then
  echo "--- apt deps ---"
  run "apt-get update -qq"
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip python3-dev \
        sqlite3 \
        rtl-sdr librtlsdr-dev \
        gnuradio gr-osmosdr \
        hackrf libhackrf-dev libhackrf0 soapysdr-tools soapysdr-module-hackrf \
        ffmpeg \
        mosquitto mosquitto-clients \
        avahi-daemon \
        git curl jq \
        libsndfile1 \
        ca-certificates"
fi

# ---- mosquitto: open localhost listener (lite default) ----------------
if [ ! -f /etc/mosquitto/conf.d/scanpi.conf ]; then
  cat > /etc/mosquitto/conf.d/scanpi.conf <<'EOF'
# ScanPi mosquitto config — localhost-only listener.
# To allow LAN/Tailscale subscribers, change "localhost" to the bind IP
# and add `allow_anonymous true` plus a password file for auth.
listener 1883 localhost
allow_anonymous true
EOF
fi
run "systemctl enable --now mosquitto"

# ---- python venv -------------------------------------------------------
VENV="$USER_HOME/scanpi-venv"
echo "--- python venv at $VENV ---"
run "sudo -u $USER_NAME python3 -m venv '$VENV'"
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q --upgrade pip"
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q \
        fastapi uvicorn[standard] pydantic \
        paho-mqtt \
        pyyaml tomli tomli-w \
        requests \
        numpy"
# whisper.cpp wrapper — tiny model, CPU-only on lite
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q faster-whisper || true"

# ---- install ScanPi package -------------------------------------------
echo "--- pip install scanpi ---"
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q -e '$REPO_ROOT'"

# ---- bundle profile + watchlist + dirs --------------------------------
echo "--- seeding $USER_HOME/scanpi/ ---"
run "sudo -u $USER_NAME mkdir -p \
        '$USER_HOME/scanpi' \
        '$USER_HOME/scanpi/profiles/sdrs' \
        '$USER_HOME/scanpi/models' \
        '$USER_HOME/scanpi/audio' \
        '$USER_HOME/scanpi/logs'"

if [ ! -f "$USER_HOME/scanpi/profile.toml" ]; then
  run "cp '$REPO_ROOT/profiles/lite.toml' '$USER_HOME/scanpi/profile.toml'"
  run "chown $USER_NAME:$USER_NAME '$USER_HOME/scanpi/profile.toml'"
fi
if [ ! -f "$USER_HOME/scanpi/watchlist.yaml" ] && [ -f "$REPO_ROOT/install/watchlist.default.yaml" ]; then
  run "cp '$REPO_ROOT/install/watchlist.default.yaml' '$USER_HOME/scanpi/watchlist.yaml'"
  run "chown $USER_NAME:$USER_NAME '$USER_HOME/scanpi/watchlist.yaml'"
fi
# preset SDR profiles
run "cp -n '$REPO_ROOT'/profiles/sdrs/presets/*.toml '$USER_HOME/scanpi/profiles/sdrs/' 2>/dev/null || true"
run "chown -R $USER_NAME:$USER_NAME '$USER_HOME/scanpi'"

# ---- systemd unit ------------------------------------------------------
echo "--- systemd unit ---"
SVC="/etc/systemd/system/scanpi-v3.service"
cat > "$SVC" <<EOF
[Unit]
Description=ScanPi v3 (lite) — neighborhood SDR scanner
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=SCANPI_PROFILE=$USER_HOME/scanpi/profile.toml
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python -m scanpi.cli_v3
Restart=on-failure
RestartSec=5
StandardOutput=append:$USER_HOME/scanpi/logs/scanpi-v3.log
StandardError=append:$USER_HOME/scanpi/logs/scanpi-v3.log

[Install]
WantedBy=multi-user.target
EOF
run "systemctl daemon-reload"
run "systemctl enable scanpi-v3"

# ---- udev rules so non-root can talk to RTL-SDR / HackRF --------------
if ! getent group plugdev > /dev/null; then
  run "groupadd plugdev"
fi
run "usermod -aG plugdev,dialout $USER_NAME"
if [ ! -f /etc/udev/rules.d/53-rtl-sdr.rules ]; then
  cat > /etc/udev/rules.d/53-rtl-sdr.rules <<'EOF'
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2832", GROUP="plugdev", MODE="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0666"
EOF
fi
if [ ! -f /etc/udev/rules.d/53-hackrf.rules ]; then
  cat > /etc/udev/rules.d/53-hackrf.rules <<'EOF'
SUBSYSTEMS=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="6089", GROUP="plugdev", MODE="0666"
SUBSYSTEMS=="usb", ATTRS{idVendor}=="1d50", ATTRS{idProduct}=="cc15", GROUP="plugdev", MODE="0666"
EOF
fi
run "udevadm control --reload-rules"
run "udevadm trigger"

# ---- start ScanPi ------------------------------------------------------
run "systemctl start scanpi-v3 || true"
sleep 3
if systemctl is-active --quiet scanpi-v3; then
  echo "[lite] scanpi-v3 is running"
else
  echo "[lite] WARNING — scanpi-v3 didn't start. Check: sudo journalctl -u scanpi-v3 -n 50"
fi
