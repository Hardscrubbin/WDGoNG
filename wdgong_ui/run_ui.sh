#!/usr/bin/env bash
# Launch the new WatchDogsGo UI (raylib) using the GAME repo's venv, which has
# every dependency (pyxel, scapy, bleak, esptool, rns, lxmf, LoRaRF, pyserial,
# requests + raylib). Runs under sudo for serial / GPIO / scapy — mirrors run.sh.
#
#   ./run_ui.sh              # normal
#   ./run_ui.sh /dev/ttyACM0 # force ESP32 port (sets WDGO_PORT)

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # wdgong_ui/
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"                          # game repo (has watchdogs/, loot/, .venv)
cd "$REPO"

VENV_PYTHON="$REPO/.venv/bin/python3"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "[run_ui] game venv not found at $VENV_PYTHON — run ./setup.sh once first" >&2
    exit 1
fi
# Dependency self-heal: if any core module is missing (fresh or partial venv),
# run the full setup once — it installs everything from requirements.txt (raylib,
# pyserial, Pillow, scapy, rns, lxmf, LoRaRF, ...) plus the system/apt packages.
if ! "$VENV_PYTHON" -c "import pyray, serial, PIL" >/dev/null 2>&1; then
    echo "[run_ui] core dependencies missing — running setup.sh to install them..." >&2
    if [ -f "$REPO/setup.sh" ]; then
        sudo bash "$REPO/setup.sh" || { echo "[run_ui] setup.sh failed — see errors above" >&2; exit 1; }
    else
        echo "[run_ui] setup.sh not found — cannot auto-install dependencies" >&2
        exit 1
    fi
fi

export PYTHONDONTWRITEBYTECODE=1
RUN_UID="${SUDO_UID:-$(id -u)}"
export XDG_RUNTIME_DIR="/run/user/${RUN_UID}"
export DISPLAY="${DISPLAY:-:0}"
export XAUTHORITY="${XAUTHORITY:-/home/$(id -un)/.Xauthority}"
# the game repo is the loot/firmware/import root for the backend
export WDGO_APP_DIR="$REPO"
# forward an explicit ESP32 port argument, if given
if [ "${1:-}" != "" ]; then
    export WDGO_PORT="$1"
fi

# meshtasticd holds the SPI bus LoRa needs — stop it, like run.sh
if command -v meshtasticd >/dev/null 2>&1; then
    sudo pkill -9 meshtasticd 2>/dev/null || true
    sudo systemctl stop meshtasticd 2>/dev/null || true
fi
# root-owned __pycache__ can block updates
sudo find "$SCRIPT_DIR" -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

exec sudo -E "$VENV_PYTHON" "$SCRIPT_DIR/main.py"
