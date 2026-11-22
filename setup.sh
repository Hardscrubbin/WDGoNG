#!/bin/bash
# =============================================================================
# ESP32 Watch Dogs — Full Setup Script
# Creates venv, installs all dependencies, checks system requirements.
# Usage: ./setup.sh  (or called automatically by run.sh / install)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

ok()   { echo -e "  ${GREEN}[OK]${NC} $1"; }
warn() { echo -e "  ${YELLOW}[!!]${NC} $1"; }
fail() { echo -e "  ${RED}[FAIL]${NC} $1"; }
info() { echo -e "  ${CYAN}[..]${NC} $1"; }

# All output of pip / apt is captured here so failures show real errors,
# not just "[error] setup.sh failed - see errors above" with nothing above.
PIP_LOG="/tmp/watchdogs-pip-$$.log"
APT_LOG="/tmp/watchdogs-apt-$$.log"
: > "$PIP_LOG"; : > "$APT_LOG"

dump_log_on_fail() {
    local what="$1" log="$2"
    fail "$what failed — last 25 lines of $log:"
    echo "----------------------------------------"
    tail -25 "$log"
    echo "----------------------------------------"
    echo "  Full log preserved at: $log"
}

echo ""
echo -e "${CYAN}========================================${NC}"
echo -e "${CYAN}  ESP32 Watch Dogs — Setup${NC}"
echo -e "${CYAN}========================================${NC}"
echo ""

ERRORS=0

# --- 0. Internet connectivity ---
echo "[0/8] Checking internet connectivity..."
if ping -c 1 -W 3 github.com &>/dev/null || ping -c 1 -W 3 1.1.1.1 &>/dev/null; then
    ok "Internet OK"
else
    fail "Cannot reach github.com or 1.1.1.1 — check network/DNS"
    exit 1
fi

# --- 1. Python 3 ---
echo "[1/8] Checking Python..."
if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>&1)
    ok "Found $PY_VER"
else
    fail "python3 not found! Install Python 3.10+ (e.g. apt install python3)"
    exit 1
fi

# --- 2. venv module ---
echo "[2/8] Checking venv module..."
if python3 -c "import venv" 2>/dev/null; then
    ok "venv available"
else
    warn "venv not available — installing python3-venv..."
    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-venv >>"$APT_LOG" 2>&1 \
            && ok "Installed python3-venv" \
            || { dump_log_on_fail "python3-venv install" "$APT_LOG"; ERRORS=$((ERRORS+1)); }
    else
        fail "Install python3-venv manually"
        ERRORS=$((ERRORS + 1))
    fi
fi

# --- 3. System packages (apt) ---
# CRITICAL: must run BEFORE pip install so build deps are available for
# packages that compile from source (dbus-python, PyNaCl, sometimes pyxel
# on bleeding-edge Python on ARM64 where prebuilt wheels aren't available).
echo "[3/8] Installing system packages (build deps + libs + tools)..."
if command -v apt-get &>/dev/null; then
    # Core deps — required on every Debian/Ubuntu/RPi system
    CORE_PKGS=(
        # Build tooling and headers (for pip packages compiling from source)
        build-essential python3-dev python3-venv pkg-config curl ca-certificates
        # Dev headers needed by specific Python wheels:
        libdbus-1-dev libglib2.0-dev   # dbus-python
        libsodium-dev                  # PyNaCl
        libffi-dev libssl-dev          # cryptography, indirect deps
        # (raylib is statically linked via the pyray wheel — no SDL2 needed)
        # Native Python bindings (linked into venv in step 6)
        python3-gi gir1.2-glib-2.0     # BlueZ pairing agent
        # System tools shelled out to by the game
        tcpdump aircrack-ng iw rtl-433
        bluez bluez-tools pulseaudio-utils
        # Build deps for dump1090 (built from source in step 7)
        librtlsdr-dev git
    )

    # RPi-only packages — skipped on non-RPi systems (no fail)
    RPI_PKGS=(
        python3-rpi-lgpio python3-lgpio   # CM5/RPi5 GPIO for LoRa
        raspi-utils                       # provides pinctrl
    )

    SYS_PKGS=("${CORE_PKGS[@]}")
    if [ -f /sys/firmware/devicetree/base/model ] && \
       grep -qi 'raspberry\|clockwork' /sys/firmware/devicetree/base/model 2>/dev/null; then
        SYS_PKGS+=("${RPI_PKGS[@]}")
    fi

    MISSING=()
    for pkg in "${SYS_PKGS[@]}"; do
        dpkg -s "$pkg" &>/dev/null || MISSING+=("$pkg")
    done

    if [ ${#MISSING[@]} -gt 0 ]; then
        info "apt-get update..."
        sudo apt-get update >>"$APT_LOG" 2>&1 \
            || warn "apt-get update had errors (see $APT_LOG) — continuing"

        info "Installing ${#MISSING[@]} packages: ${MISSING[*]}"
        if sudo apt-get install -y "${MISSING[@]}" >>"$APT_LOG" 2>&1 ; then
            ok "System packages installed"
        else
            # Sometimes apt returns non-zero but most packages installed.
            # Re-check what's still missing for a clear error.
            STILL_MISSING=()
            for pkg in "${MISSING[@]}"; do
                dpkg -s "$pkg" &>/dev/null || STILL_MISSING+=("$pkg")
            done
            if [ ${#STILL_MISSING[@]} -gt 0 ]; then
                dump_log_on_fail "apt install" "$APT_LOG"
                fail "Still missing: ${STILL_MISSING[*]}"
                ERRORS=$((ERRORS + 1))
            else
                warn "apt reported errors but all packages present (see $APT_LOG)"
                ok "System packages installed"
            fi
        fi
    else
        ok "All system packages already present"
    fi
else
    warn "Not Debian/Ubuntu — install build deps + SDL2 manually"
fi

# --- 3b. meshtasticd (optional, RPi/uConsole only) — native Meshtastic daemon ---
# Not in Debian; comes from the Meshtastic OBS repo. NON-FATAL: if it can't be
# installed the game simply hides the Meshtastic option (LoRa Mesh keeps MeshCore).
# The game starts/stops it on demand, so its boot auto-start is masked.
if command -v apt-get &>/dev/null && [ -f /sys/firmware/devicetree/base/model ] && \
   grep -qi 'raspberry\|clockwork' /sys/firmware/devicetree/base/model 2>/dev/null; then
    echo "[3b/8] Installing meshtasticd (native Meshtastic daemon, optional)..."
    if command -v meshtasticd &>/dev/null; then
        ok "meshtasticd already installed"
    else
        # The OBS beta channel ships Debian_<ver> + Raspbian_<ver> only (no Ubuntu).
        # Blindly building "Debian_<VERSION_ID>" 404s on Ubuntu (and on any VERSION_ID
        # without a matching repo), which under `set -e` aborted the WHOLE setup. So:
        # build best-match candidates for this distro, PROBE each, use the first that
        # exists, and keep every step non-fatal (meshtasticd is optional; MeshCore
        # works without it). Ubuntu simply has no repo here -> graceful skip.
        # Map THIS distro to the right base via ID/ID_LIKE + the Debian codename, then
        # PROBE candidates and use the first that exists. Covers Debian, Raspberry Pi
        # OS, Raspbian, and Debian derivatives (Kali -> trixie/13, Parrot -> bookworm/12);
        # Ubuntu-based (incl. DragonOS) have no repo here -> graceful skip. Fallbacks to
        # Debian_13/12 guarantee a working repo for any debian-family system.
        _osid="$(. /etc/os-release 2>/dev/null; echo "${ID:-debian}" | tr 'A-Z' 'a-z')"
        _idlike="$(. /etc/os-release 2>/dev/null; echo "${ID_LIKE:-}" | tr 'A-Z' 'a-z')"
        _code="$(. /etc/os-release 2>/dev/null; echo "${VERSION_CODENAME:-}" | tr 'A-Z' 'a-z')"
        _osver="$(. /etc/os-release 2>/dev/null; echo "${VERSION_ID:-}")"
        case "$_code" in
            trixie|sid|kali-rolling) _dn=13 ;;
            bookworm|lory)           _dn=12 ;;
            *)                       _dn="" ;;
        esac
        _pref="${_dn:-${_osver:-13}}"
        if [ "$_osid" = ubuntu ] || printf '%s' "$_idlike" | grep -qw ubuntu; then
            _cands=""                                    # Ubuntu / DragonOS -> no OBS repo
        elif [ "$_osid" = raspbian ]; then
            _cands="Raspbian_${_pref} Debian_${_pref} Raspbian_13 Debian_13 Raspbian_12 Debian_12"
        else                                             # debian, kali, parrot, raspberry pi os, ...
            _cands="Debian_${_pref} Raspbian_${_pref} Debian_13 Debian_12"
        fi
        _OBS_BASE="https://download.opensuse.org/repositories/network:/Meshtastic:/beta"
        OBS=""; OBS_DIST=""
        for _d in $_cands; do
            if curl -fsSL -m 10 --retry 1 -o /dev/null "${_OBS_BASE}/${_d}/Release.key" 2>/dev/null; then
                OBS_DIST="$_d"; OBS="${_OBS_BASE}/${_d}"; break
            fi
        done
        if [ -z "$OBS" ] && [ -n "$_cands" ]; then       # probe inconclusive (throttle/offline)
            OBS_DIST="${_cands%% *}"                      # but debian-family: best-effort try
            OBS="${_OBS_BASE}/${OBS_DIST}"                # (apt step is non-fatal; bad repo cleaned up)
        fi
        if [ -n "$OBS" ]; then
            info "Adding Meshtastic OBS repo (${OBS_DIST}) + installing meshtasticd..."
            {
                sudo mkdir -p /etc/apt/keyrings
                curl -fsSL "${OBS}/Release.key" | gpg --dearmor \
                    | sudo tee /etc/apt/keyrings/meshtastic.gpg >/dev/null
                echo "deb [signed-by=/etc/apt/keyrings/meshtastic.gpg] ${OBS}/ /" \
                    | sudo tee /etc/apt/sources.list.d/meshtastic.list >/dev/null
                sudo apt-get update
                sudo apt-get install -y meshtasticd
            } >>"$APT_LOG" 2>&1 || true
            if command -v meshtasticd &>/dev/null; then
                sudo systemctl mask meshtasticd >>"$APT_LOG" 2>&1 || true
                ok "meshtasticd installed (auto-start masked; the game drives it)"
            else
                sudo rm -f /etc/apt/sources.list.d/meshtastic.list 2>/dev/null || true
                warn "meshtasticd install failed — Meshtastic stays unavailable (MeshCore still works). See $APT_LOG"
            fi
        else
            warn "No Meshtastic repo for ${_osid} ${_osver} (OBS beta = Debian/Raspbian only) — skipping. MeshCore still works."
        fi
    fi
fi

# --- 3c. labwc fullscreen rule (uConsole / labwc) — fill the rotated DSI panel ---
# GLFW/raylib mis-sizes its window to 720x720 on the uConsole's rotated DSI panel
# under Wayland/labwc, so the game leaves fullscreen to the compositor. A fresh
# labwc has no rule for the game window -> tiny square. Add a window rule that
# auto-fullscreens "WatchDogsGo". Idempotent, backs up rc.xml, fully non-fatal.
if command -v labwc &>/dev/null; then
    echo "[3c/8] Adding labwc fullscreen rule for the game window..."
    if python3 - <<'PYLABWC'
import os, re, sys, pathlib
rc = pathlib.Path(os.path.expanduser("~/.config/labwc/rc.xml"))
inner = '<windowRule title="WatchDogsGo"><action name="ToggleFullscreen"/></windowRule>'
block = "  <windowRules>\n    " + inner + "\n  </windowRules>\n"
rc.parent.mkdir(parents=True, exist_ok=True)
txt = rc.read_text() if rc.exists() else '<?xml version="1.0"?>\n<labwc_config>\n</labwc_config>\n'
if 'title="WatchDogsGo"' in txt:
    print("rule already present"); sys.exit(0)
if rc.exists():
    rc.with_name("rc.xml.wdgo.bak").write_text(txt)      # backup before touching
if re.search(r'<windowRules[^>]*>', txt):
    txt = re.sub(r'(<windowRules[^>]*>)', r'\1\n    ' + inner, txt, count=1)
else:
    m = re.search(r'</(labwc_config|openbox_config)>', txt)
    txt = (txt[:m.start()] + block + txt[m.start():]) if m else (txt.rstrip() + "\n" + block)
rc.write_text(txt); print("rule added")
PYLABWC
    then
        ok "labwc rule set in ~/.config/labwc/rc.xml (game auto-fullscreens)"
        labwc --reconfigure >/dev/null 2>&1 || true     # applies now if a session is live
    else
        warn "Could not update labwc rc.xml — set fullscreen manually (press F in-game)."
    fi
fi

# --- 4. Virtual environment ---
echo "[4/8] Setting up virtual environment..."
if [ ! -d ".venv" ]; then
    info "Creating .venv..."
    python3 -m venv .venv 2>/dev/null || python3 -m venv .venv --without-pip
    ok "Created .venv"
else
    ok ".venv exists"
fi

if [ ! -f ".venv/bin/pip" ] && [ ! -f ".venv/bin/pip3" ]; then
    info "Bootstrapping pip..."
    curl -sS https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
    .venv/bin/python3 /tmp/get-pip.py --quiet
    rm -f /tmp/get-pip.py
    ok "pip installed"
fi

# --- 5. Python packages ---
echo "[5/8] Installing Python packages..."

# Run pip silently to keep output clean. On failure, dump the tail of the
# log — never silently swallow errors like the previous '--quiet 2>/dev/null'
# pattern did (that's why nobody could ever see why their install failed).
pip_run() {
    local desc="$1"; shift
    info "$desc"
    if .venv/bin/pip "$@" >>"$PIP_LOG" 2>&1 ; then
        ok "$desc"
        return 0
    else
        dump_log_on_fail "$desc" "$PIP_LOG"
        return 1
    fi
}

if ! pip_run "Upgrading pip + wheel + setuptools" install --upgrade pip wheel setuptools ; then
    ERRORS=$((ERRORS + 1))
fi

if ! pip_run "Installing requirements.txt" install -r requirements.txt ; then
    fail "Common causes:"
    fail "  - missing apt build deps (re-check step [3] errors above)"
    fail "  - Python $PY_VER too new for some wheels"
    fail "  - intermittent network / PyPI timeout (re-run setup.sh)"
    ERRORS=$((ERRORS + 1))
fi

# --- 6. Link system Python modules into venv ---
# rpi-lgpio and python3-gi ship native .so files via apt that pip can't
# easily rebuild. Linking them in lets the venv use them directly.
# pip install LoRaRF pulls old RPi.GPIO 0.7.1 which doesn't know CM5 — we
# rm that copy and link the apt one in.
echo "[6/8] Linking system Python modules into venv..."
if [[ "$(uname)" == "Linux" ]]; then
    if [ -d "/usr/lib/python3/dist-packages/RPi" ] && \
       [ -f "/usr/lib/python3/dist-packages/RPi/GPIO/__init__.py" ]; then
        for sp in .venv/lib/python3.*/site-packages; do
            if [ -d "$sp" ]; then
                rm -rf "$sp/RPi" "$sp/RPi.GPIO"* 2>/dev/null
                ln -sf /usr/lib/python3/dist-packages/RPi "$sp/RPi"
                ln -sf /usr/lib/python3/dist-packages/lgpio.py "$sp/lgpio.py" 2>/dev/null
                for so in /usr/lib/python3/dist-packages/_lgpio*.so; do
                    [ -f "$so" ] && ln -sf "$so" "$sp/$(basename $so)"
                done
                ok "rpi-lgpio linked into venv (LoRa GPIO)"
                break
            fi
        done
    else
        info "rpi-lgpio not present (non-RPi system) — skipping"
    fi

    if [ -d "/usr/lib/python3/dist-packages/gi" ]; then
        for sp in .venv/lib/python3.*/site-packages; do
            if [ -d "$sp" ] && [ ! -e "$sp/gi" ]; then
                ln -sf /usr/lib/python3/dist-packages/gi "$sp/gi"
                for so in /usr/lib/python3/dist-packages/_gi*.so \
                          /usr/lib/python3/dist-packages/_gi_cairo*.so; do
                    [ -f "$so" ] && ln -sf "$so" "$sp/$(basename $so)"
                done
                for extra in pygobject_compat.py; do
                    f="/usr/lib/python3/dist-packages/$extra"
                    [ -f "$f" ] && ln -sf "$f" "$sp/$extra"
                done
                ok "python3-gi linked into venv (BlueZ pairing)"
                break
            fi
        done
    fi
fi

# Verify required imports
MISSING_REQ=""
.venv/bin/python3 -c "import pyray" 2>/dev/null || MISSING_REQ="$MISSING_REQ raylib"
.venv/bin/python3 -c "import serial" 2>/dev/null || MISSING_REQ="$MISSING_REQ pyserial"
.venv/bin/python3 -c "from PIL import Image" 2>/dev/null || MISSING_REQ="$MISSING_REQ Pillow"

if [ -z "$MISSING_REQ" ]; then
    ok "Required Python imports verified"
else
    fail "Missing required packages:$MISSING_REQ"
    fail "Check pip log: $PIP_LOG"
    ERRORS=$((ERRORS + 1))
fi

# Verify optional imports (advanced attacks + LoRa)
MISSING_OPT=""
.venv/bin/python3 -c "import scapy" 2>/dev/null || MISSING_OPT="$MISSING_OPT scapy"
.venv/bin/python3 -c "import netifaces" 2>/dev/null || MISSING_OPT="$MISSING_OPT netifaces"
.venv/bin/python3 -c "import bleak" 2>/dev/null || MISSING_OPT="$MISSING_OPT bleak"
.venv/bin/python3 -c "import dbus" 2>/dev/null || MISSING_OPT="$MISSING_OPT dbus-python"
.venv/bin/python3 -c "from gi.repository import GLib" 2>/dev/null || MISSING_OPT="$MISSING_OPT python3-gi"
.venv/bin/python3 -c "import LoRaRF" 2>/dev/null || MISSING_OPT="$MISSING_OPT LoRaRF"
.venv/bin/python3 -c "import nacl" 2>/dev/null || MISSING_OPT="$MISSING_OPT PyNaCl"

if [ -z "$MISSING_OPT" ]; then
    ok "Optional Python imports verified (all attacks available)"
else
    warn "Optional packages not available:$MISSING_OPT"
    warn "Some attacks may not work (MITM, Dragon Drain, BlueDucky, RACE, LoRa)"
fi

# --- 7. dump1090 from source + aiov2_ctl (uConsole only) ---
echo "[7/8] Building dump1090 + uConsole tools..."

if ! command -v dump1090 &>/dev/null; then
    info "Building FlightAware dump1090 from source..."
    TMP=$(mktemp -d)
    if git clone --depth=1 https://github.com/flightaware/dump1090.git "$TMP/dump1090" >>"$APT_LOG" 2>&1 \
       && (cd "$TMP/dump1090" && make -j"$(nproc)" >>"$APT_LOG" 2>&1) \
       && sudo cp "$TMP/dump1090/dump1090" /usr/local/bin/ ; then
        ok "dump1090 installed (/usr/local/bin/dump1090)"
    else
        warn "dump1090 build failed — ADS-B Radar will not work (see $APT_LOG)"
    fi
    rm -rf "$TMP"
else
    ok "dump1090 present ($(command -v dump1090))"
fi

# AIO v2 control (uConsole only)
if command -v pinctrl &>/dev/null && [ -f /sys/firmware/devicetree/base/model ]; then
    if ! command -v aiov2_ctl &>/dev/null; then
        info "Installing aiov2_ctl from GitHub (uConsole AIO v2 hardware)..."
        sudo apt-get install -y python3-pyqt6 git >>"$APT_LOG" 2>&1 || true
        TMP=$(mktemp -d)
        if git clone --depth=1 https://github.com/hackergadgets/aiov2_ctl.git "$TMP/aiov2_ctl" >>"$APT_LOG" 2>&1 \
           && (cd "$TMP/aiov2_ctl" && sudo python3 ./aiov2_ctl.py --install >>"$APT_LOG" 2>&1) ; then
            ok "aiov2_ctl installed"
        else
            warn "aiov2_ctl install failed — AIO toggles disabled (see $APT_LOG)"
        fi
        rm -rf "$TMP"
    else
        ok "aiov2_ctl present"
    fi
fi

# --- 8. Permissions and data directories ---
echo "[8/8] Permissions and data directories..."
[ -f "setup.sh" ] && chmod +x setup.sh
[ -f "wdgong_ui/run_ui.sh" ] && chmod +x wdgong_ui/run_ui.sh
ok "Scripts executable"

if [[ "$(uname)" == "Linux" ]]; then
    TARGET_USER="${SUDO_USER:-$(whoami)}"
    if id -nG "$TARGET_USER" 2>/dev/null | grep -qE '\b(dialout|tty)\b'; then
        ok "User '$TARGET_USER' in dialout/tty group (serial access)"
    else
        warn "User '$TARGET_USER' not in dialout group"
        warn "  Fix: sudo usermod -aG dialout $TARGET_USER  (then log out + log in)"
    fi
fi

mkdir -p loot maps plugins firmware_cache
ok "Data directories ready (loot, maps, plugins, firmware_cache)"

if [ ! -f "secrets.conf" ] && [ -f "secrets.conf.example" ]; then
    cp secrets.conf.example secrets.conf
    chmod 600 secrets.conf 2>/dev/null || true
    ok "secrets.conf created from template (edit to add API keys)"
fi

# --- Summary ---
echo ""
if [ $ERRORS -eq 0 ]; then
    echo -e "${GREEN}========================================${NC}"
    echo -e "${GREEN}  Setup complete! No errors.${NC}"
    echo -e "${GREEN}========================================${NC}"
    echo ""
    echo "  Run the game:  ./wdgong_ui/run_ui.sh"
    echo ""
    rm -f "$PIP_LOG" "$APT_LOG" 2>/dev/null
else
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  Setup finished with $ERRORS error(s)${NC}"
    echo -e "${RED}========================================${NC}"
    echo ""
    echo "  Logs preserved for debugging:"
    [ -s "$PIP_LOG" ] && echo "    pip:  $PIP_LOG"
    [ -s "$APT_LOG" ] && echo "    apt:  $APT_LOG"
    echo ""
    echo "  Fix the errors above, then re-run: sudo bash setup.sh"
    echo ""
    exit 1
fi
