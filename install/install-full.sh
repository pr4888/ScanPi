#!/usr/bin/env bash
# ScanPi FULL install — Ubuntu 22.04+ / x86_64.
#
# Sourced by install.sh.
#
# Footprint:
#   - apt deps: ~1.5 GB
#   - python venv with full feature set: ~600 MB
#   - whisper small.en model: ~480 MB (medium.en if GPU: ~1.5 GB)
#   - bge-small-en-v1.5: ~33 MB
#   - mosquitto, postgres-client (optional), nginx (optional)
#   - trunk-recorder + rdio-scanner (optional, can be Docker)
#
# Total disk: ~3-4 GB. Designed for a NUC / server / workstation with
# a USB SSD or NVMe.

set -euo pipefail

REPO_ROOT="${SCANPI_REPO_ROOT}"
USER_NAME="${SCANPI_INSTALL_USER}"
DRY_RUN="${SCANPI_DRY_RUN:-0}"
SKIP_DEPS="${SCANPI_SKIP_DEPS:-0}"
USER_HOME="$(getent passwd "$USER_NAME" | cut -d: -f6)"

run() {
  echo "+ $*"
  [ "$DRY_RUN" = "1" ] || eval "$@"
}

echo "=== installing ScanPi FULL for $USER_NAME (home: $USER_HOME) ==="

# ---- apt deps ----------------------------------------------------------
if [ "$SKIP_DEPS" != "1" ]; then
  echo "--- apt deps ---"
  run "apt-get update -qq"
  run "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip python3-dev \
        sqlite3 \
        rtl-sdr librtlsdr-dev rtl-433 \
        gnuradio gr-osmosdr gr-iio \
        hackrf libhackrf-dev libhackrf0 soapysdr-tools \
        soapysdr-module-hackrf soapysdr-module-rtlsdr soapysdr-module-airspy \
        soapysdr-module-bladerf soapysdr-module-uhd \
        ffmpeg sox libsndfile1 \
        mosquitto mosquitto-clients \
        avahi-daemon \
        nginx \
        git curl jq \
        ca-certificates build-essential \
        libopenblas-dev libomp-dev"
  # GPU detection — install CUDA whisper later in venv if visible
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi >/dev/null 2>&1; then
    echo "[full] NVIDIA GPU detected"
    HAVE_GPU=1
  else
    HAVE_GPU=0
  fi
fi

# ---- mosquitto: open localhost+local-LAN listener ---------------------
if [ ! -f /etc/mosquitto/conf.d/scanpi.conf ]; then
  cat > /etc/mosquitto/conf.d/scanpi.conf <<'EOF'
# ScanPi mosquitto config — open on all interfaces.
# WARNING: anonymous + open. If exposing beyond your LAN, add password_file
# and TLS. Safe over Tailscale because tailnet is auth'd at the network layer.
listener 1883
allow_anonymous true
EOF
fi
run "systemctl enable --now mosquitto"

# ---- python venv -------------------------------------------------------
VENV="$USER_HOME/scanpi-venv"
echo "--- python venv at $VENV ---"
run "sudo -u $USER_NAME python3 -m venv '$VENV'"
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q --upgrade pip wheel"
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q \
        fastapi uvicorn[standard] pydantic \
        paho-mqtt \
        pyyaml tomli tomli-w \
        requests \
        numpy scipy \
        onnxruntime"
# whisper — try GPU first if NVIDIA visible, else CPU
if [ "${HAVE_GPU:-0}" = "1" ]; then
  echo "[full] installing whisper with CUDA support"
  run "sudo -u $USER_NAME '$VENV/bin/pip' install -q \
        faster-whisper \
        nvidia-cublas-cu12 nvidia-cudnn-cu12 || true"
else
  run "sudo -u $USER_NAME '$VENV/bin/pip' install -q faster-whisper"
fi
# Optional speaker correlation deps (cross_channel_correlation flag)
run "sudo -u $USER_NAME '$VENV/bin/pip' install -q --no-deps speechbrain || true"

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
        '$USER_HOME/scanpi/iq_archive' \
        '$USER_HOME/scanpi/logs'"

if [ ! -f "$USER_HOME/scanpi/profile.toml" ]; then
  run "cp '$REPO_ROOT/profiles/full.toml' '$USER_HOME/scanpi/profile.toml'"
  run "chown $USER_NAME:$USER_NAME '$USER_HOME/scanpi/profile.toml'"
fi
if [ ! -f "$USER_HOME/scanpi/watchlist.yaml" ] && [ -f "$REPO_ROOT/install/watchlist.default.yaml" ]; then
  run "cp '$REPO_ROOT/install/watchlist.default.yaml' '$USER_HOME/scanpi/watchlist.yaml'"
  run "chown $USER_NAME:$USER_NAME '$USER_HOME/scanpi/watchlist.yaml'"
fi
run "cp -n '$REPO_ROOT'/profiles/sdrs/presets/*.toml '$USER_HOME/scanpi/profiles/sdrs/' 2>/dev/null || true"
run "chown -R $USER_NAME:$USER_NAME '$USER_HOME/scanpi'"

# ---- optional: trunk-recorder + rdio-scanner via Docker compose ------
TR_COMPOSE="$REPO_ROOT/install/optional/docker-compose.yml"
if [ -f "$TR_COMPOSE" ]; then
  echo "[full] optional trunk-recorder + rdio-scanner stack: see $TR_COMPOSE"
  echo "[full] not auto-started — run 'docker compose -f $TR_COMPOSE up -d' when ready"
fi

# ---- systemd unit ------------------------------------------------------
echo "--- systemd unit ---"
SVC="/etc/systemd/system/scanpi-v3.service"
cat > "$SVC" <<EOF
[Unit]
Description=ScanPi v3 (full) — neighborhood SDR scanner
After=network-online.target sound.target mosquitto.service
Wants=network-online.target mosquitto.service

[Service]
Type=simple
User=$USER_NAME
Group=$USER_NAME
Environment=SCANPI_PROFILE=$USER_HOME/scanpi/profile.toml
Environment=PATH=$VENV/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$VENV/bin/python -m scanpi.cli_v3
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
StandardOutput=append:$USER_HOME/scanpi/logs/scanpi-v3.log
StandardError=append:$USER_HOME/scanpi/logs/scanpi-v3.log

[Install]
WantedBy=multi-user.target
EOF
run "systemctl daemon-reload"
run "systemctl enable scanpi-v3"

# ---- udev rules --------------------------------------------------------
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

# ---- nginx reverse proxy (optional, full only) ------------------------
NGINX_CONF="/etc/nginx/sites-available/scanpi"
if [ ! -f "$NGINX_CONF" ]; then
  cat > "$NGINX_CONF" <<'EOF'
# ScanPi reverse proxy. Adds gzip + sane timeouts. Pair with Tailscale's
# `tailscale serve --bg --https=443 http://localhost:80` for HTTPS.
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 100m;
    proxy_read_timeout 600s;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
EOF
  run "ln -sf '$NGINX_CONF' /etc/nginx/sites-enabled/scanpi"
  run "rm -f /etc/nginx/sites-enabled/default"
  run "nginx -t && systemctl reload nginx || true"
fi

# ---- start ScanPi ------------------------------------------------------
run "systemctl start scanpi-v3 || true"
sleep 3
if systemctl is-active --quiet scanpi-v3; then
  echo "[full] scanpi-v3 is running"
else
  echo "[full] WARNING — scanpi-v3 didn't start. Check: sudo journalctl -u scanpi-v3 -n 50"
fi
