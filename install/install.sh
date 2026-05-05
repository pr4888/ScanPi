#!/usr/bin/env bash
# ScanPi installer — autodetects target, dispatches to install-lite.sh or install-full.sh.
#
# Quick-start (any debian-based system):
#
#   curl -fsSL https://raw.githubusercontent.com/pr4888/ScanPi/main/install/install.sh | sudo bash
#
# Or after cloning:
#
#   sudo bash install/install.sh           # autodetect
#   sudo bash install/install.sh --lite    # force lite (Pi 5)
#   sudo bash install/install.sh --full    # force full (Ubuntu x86_64)
#   sudo bash install/install.sh --dry-run # show what would happen
#
# Tested on:
#   - Raspberry Pi OS Bookworm (64-bit) on Pi 4 / Pi 5
#   - Ubuntu 22.04 / 24.04 (x86_64)
#   - Debian 12 (x86_64)
#
# Not supported (yet):
#   - Fedora / RHEL family
#   - Alpine
#   - macOS / Windows (ScanPi runs over Tailscale; install on a Linux box and
#     reach it from there)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="/tmp/scanpi-install-$(date +%Y%m%d-%H%M%S).log"

# ---- ANSI helpers (degrade gracefully if no tty) -----------------------
if [ -t 1 ]; then
  C_GREEN='\033[0;32m'; C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'
  C_BOLD='\033[1m'; C_RESET='\033[0m'
else
  C_GREEN=''; C_YELLOW=''; C_RED=''; C_BOLD=''; C_RESET=''
fi
say()  { printf "${C_BOLD}[scanpi]${C_RESET} %s\n" "$*"; }
ok()   { printf "${C_GREEN}[ok]${C_RESET} %s\n" "$*"; }
warn() { printf "${C_YELLOW}[warn]${C_RESET} %s\n" "$*" >&2; }
die()  { printf "${C_RED}[err]${C_RESET} %s\n" "$*" >&2; exit 1; }

# ---- Args --------------------------------------------------------------
MODE=""
DRY_RUN=0
SKIP_DEPS=0
INSTALL_USER="${SUDO_USER:-${USER}}"
while [ $# -gt 0 ]; do
  case "$1" in
    --lite)     MODE="lite"; shift ;;
    --full)     MODE="full"; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    --skip-deps) SKIP_DEPS=1; shift ;;
    --user=*)   INSTALL_USER="${1#--user=}"; shift ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    *)          die "unknown arg: $1" ;;
  esac
done

# ---- Pre-flight --------------------------------------------------------
[ "$(id -u)" -eq 0 ] || die "run with sudo (apt + systemd require root)"

if [ ! -d "$REPO_ROOT/src/scanpi" ]; then
  die "couldn't find ScanPi source at $REPO_ROOT/src/scanpi — are you running from a clone?"
fi

# ---- Autodetect target -------------------------------------------------
detect_target() {
  local arch cpu_count ram_gb
  arch="$(uname -m)"
  cpu_count="$(nproc)"
  ram_gb=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  if [[ "$arch" == "x86_64" || "$arch" == "amd64" ]] && [ "$cpu_count" -ge 8 ] && [ "$ram_gb" -ge 16 ]; then
    echo "full"
  else
    echo "lite"
  fi
}

if [ -z "$MODE" ]; then
  MODE="$(detect_target)"
  say "autodetected target: ${C_BOLD}${MODE}${C_RESET}  (use --lite or --full to override)"
fi

[ "$MODE" = "lite" ] || [ "$MODE" = "full" ] || die "MODE must be lite or full, got: $MODE"

say "user:    ${INSTALL_USER}"
say "mode:    ${MODE}"
say "repo:    ${REPO_ROOT}"
say "log:     ${LOG_FILE}"
say "arch:    $(uname -m)  cores: $(nproc)  ram: $(free -h | awk '/Mem:/ {print $2}')"
[ $DRY_RUN -eq 1 ] && warn "DRY RUN — nothing will be changed"
echo

# ---- Dispatch ----------------------------------------------------------
SUB="$REPO_ROOT/install/install-${MODE}.sh"
[ -f "$SUB" ] || die "missing $SUB"
chmod +x "$SUB"

export SCANPI_REPO_ROOT="$REPO_ROOT"
export SCANPI_INSTALL_USER="$INSTALL_USER"
export SCANPI_DRY_RUN="$DRY_RUN"
export SCANPI_SKIP_DEPS="$SKIP_DEPS"

bash "$SUB" 2>&1 | tee -a "$LOG_FILE"

ok "ScanPi ${MODE} install complete."
echo
say "Next steps:"
say "  1. Plug in your SDR(s)"
say "  2. systemctl status scanpi-v3"
say "  3. Open http://$(hostname -I | awk '{print $1}'):8080/  (or scanpi.local:8080 over mDNS)"
say "  4. Tailscale: 'tailscale up' on this box if not already, then expose with"
say "     'sudo tailscale serve --bg --https=443 http://localhost:8080'"
say ""
say "Profile is at ~${INSTALL_USER}/scanpi/profile.toml — edit to enable opt-in features."
say "Watchlist for keyword alerts: ~${INSTALL_USER}/scanpi/watchlist.yaml"
