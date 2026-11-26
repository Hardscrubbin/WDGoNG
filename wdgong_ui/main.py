#!/usr/bin/env python3
"""WatchDogsGo — new-UI prototype (raylib).

Default view: MAP/RADAR fullscreen + live TERMINAL below; tab menu hidden,
opens on TAB. CRT scanlines + intermittent glitch, drawn (no GLSL, works on any
GL/GLES). Real data via backend.py — reuses the OG watchdogs managers for ESP32
serial, GPS, network parsing and loot (imports the existing loot database).
Falls back to a demo view when the backend / ESP32 is unavailable.

Keys: TAB menu | <-/-> tab, UP/DOWN item (menu) | C cycle loot marker |
      G FIELD mode | F fullscreen | ESC = quit prompt (Y/N)
"""
import math
import os
import random
import sys
import time
from pyray import *

HERE = os.path.dirname(os.path.abspath(__file__))
A = os.path.join(HERE, "assets")
if HERE not in sys.path:
    sys.path.insert(0, HERE)
_ROOT = os.path.dirname(HERE)          # repo root, so the `watchdogs` package imports
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
# NOTE: `backend` (as `be`) is imported INSIDE main(), right after the instant
# pre-splash frame — its heavy managers import (serial/gps/network/loot + numpy)
# is a big chunk of cold-start time, so we draw "INITIALIZING" first, then import.
try:
    from watchdogs.basemap import Basemap, lonlat_to_tile, tile_corner
except Exception:
    Basemap = None
import wdgo_extras   # EXTRAS platform — pure stdlib, no raylib (see docs/EXTRAS_spec.md)
import wdgo_extras_hub   # EXTRAS hub client (signed index, checksum-verified installs)

W, H = 1280, 720


def C(r, g, b, a=255): return Color(r, g, b, a)
VOID = C(5, 7, 10); BG0 = C(13, 20, 32); BG1 = C(5, 8, 13)
SURF = C(16, 21, 30); EDGE = C(42, 53, 69)
TEAL = C(34, 211, 219); MAGENTA = C(255, 46, 136); PURPLE = C(178, 59, 214)
DANGER = C(255, 59, 48); SUCCESS = C(57, 255, 136); WARN = C(255, 176, 32); ICE = C(130, 220, 255)
HI = C(232, 240, 248); TXT = C(159, 176, 195); DIM = C(92, 107, 125); GHOST = C(47, 59, 73)

# Faithful mirror of watchdogs/app.py MENU_CATS. Each item is the OG 5-tuple
# (hotkey, label, cmd, state_key, input_type). cmd starting with "_" = host-side
# Python action in the OG app; real ESP32 serial commands otherwise.
TABS = [
    ("SCAN", TEAL, [
        ("1", "WiFi Scan",        "scan_networks",          "wifi_scan",    None),
        ("2", "BLE Scan",         "scan_bt",                "ble_scan",     None),
        ("t", "BT Tracker",       "scan_bt",                "bt_tracking",  "mac"),
        ("a", "AirTag Scan",      "scan_airtag",            "bt_airtag",    None),
    ]),
    ("SNIFF", ICE, [
        ("1", "WiFi Wardrive",    "_wardrive_wifi",         "wardriving",   None),
        ("2", "BT Wardrive",      "_wardrive_bt",           "bt_scanning",  None),
        ("c", "Cluster Wardrive", "_cluster",               "_cluster_scr", None),
        ("3", "Pkt Sniffer",      "start_sniffer",          "sniffer",      None),
        ("4", "HS Capture",       "start_handshake",        "handshake",    None),
        ("5", "HS Capture no SD", "start_handshake_serial", "handshake",    None),
    ]),
    ("ATTACK", DANGER, [
        ("1", "Deauth",           "start_deauth",           "deauth",       "bssid_ch"),
        ("2", "Blackout",         "start_blackout",         "blackout",     None),
        ("3", "HS Capture",       "start_handshake",        "handshake",    None),
        ("4", "Evil Twin",        "_evil_twin",             "evil_twin",    None),
        ("5", "SAE Flood",        "sae_overflow",           "sae_flood",    "bssid_ch"),
        ("6", "HS Capture no SD", "start_handshake_serial", "handshake",    None),
        ("7", "Evil Portal",      "_evil_portal",           "portal",       None),
        ("d", "Dragon Drain",     "_dragon_drain",          "dragon_drain", None),
        ("m", "MITM",             "_mitm",                  "mitm",         None),
        ("k", "BlueDucky (WIP)",  "_bd_wip",                "_bd_wip",      None),
        ("j", "RACE Attack (WIP)","_race_wip",              "_race_wip",    None),
    ]),
    ("ADDONS", WARN, [
        ("h", "BLE HID (WIP)",    "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("i", "HID Type (WIP)",   "_bt_hid_wip",            "_bt_hid_wip",  None),
        ("9", "LoRa Mesh",        "_lora_mesh",             "_lora_mesh",   None),
        ("w", "WDGWars Chat",     "_wdgwars",               "wdgwars",      None),
        ("u", "WDGWars Sync",     "_wdgwars_sync",          "_wdg_sync",    None),
        ("k", "WDGWars Pair",     "_wdgwars_pair",          "_wdg_pair",    None),
        ("f", "Flipper Zero",     "_flipper",               "_flipper",     None),
        ("a", "ADS-B Radar",      "_sdr_adsb",              "_sdr_adsb",    None),
        ("4", "433 MHz Scanner",  "_sdr_433",               "_sdr_433",     None),
        ("g", "433 Capture",      "_sdr_grab433",           "_sdr_grab433", None),
        ("e", "Emergency Radio",  "_sdr_emergency",         "_sdr_emergency", None),
        ("h", "433 Hunter",       "_sdr_keyhunt",           "_sdr_keyhunt", None),
        ("b", "433 Beep",         "_keyhunt_beep",          "_keyhunt_beep", None),
        ("s", "Monster RF SubGHz","_subghz",                "_subghz",      None),
        ("m", "Mute Sounds",      "_mute_toggle",           "_mute_toggle", None),
        ("p", "PipBoy Watch",     "_watch_connect",         "_watch",       None),
        ("y", "Proximity",        "_prox_toggle",           "_prox_toggle", None),
        ("z", "Prox Radar",       "_prox_view",             "_prox_view",   None),
    ]),
    ("SYSTEM", SUCCESS, [
        ("x", "STOP ALL",         "stop",                   "_stop_all",    None),
        ("r", "Reboot ESP32",     "_reboot_esp32",          "_reboot",      None),
        ("g", "GPS",              "_toggle_gps",            "_gps_toggle",  None),
        ("l", "LoRa Mesh",        "_lora_mesh",             "_lora_mesh",   None),
        ("d", "SDR",              "_toggle_sdr",            "_sdr_toggle",  None),
        ("b", "USB",              "_toggle_usb",            "_usb_toggle",  None),
        ("w", "Whitelist",        "_whitelist",             "_wl_screen",   None),
        ("y", "Radio (WiFi card)","_radio_matrix",          "_radio_mx",    None),
        ("u", "Upload WPA-SEC",   "_wpasec_upload",         "_wpasec_up",   None),
        ("p", "Download WPA-SEC", "_wpasec_download",       "_wpasec_dl",   None),
        ("v", "Upload WiGLE",     "_wigle_upload",          "_wigle_up",    None),
        ("f", "Flash ESP32",      "_flash_esp32",           "_flash_esp",   None),
        ("i", "Update Game",      "_update_game",           "_gameupd",     None),
        ("k", "Backup Data",      "_backup_export",         "_backup_exp",  None),
        ("o", "Restore Data",     "_backup_import",         "_backup_imp",  None),
        ("s", "Session Data",     "_session_data",          "_session_data", None),
        ("c", "Captured Data",    "_captured_data",         "_captured_data", None),
    ]),
    ("CRISIS", MAGENTA, [
        ("w", "Wikipedia (Kiwix)", "_srv_wiki",             "_srv_wiki",    None),
        ("c", "Offline Nav System","_srv_ctos",             "_srv_ctos",    None),
        ("n", "Organic Maps",      "_srv_nav",              "_srv_nav",     None),
        ("s", "Status",            "_srv_status",           "_srv_status",  None),
    ]),
]
# short PL descriptions shown as a dwell-tooltip on each menu item (keyed by label;
# duplicate labels across tabs mean the same thing, so one entry covers them)
MENU_DESC = {
    # SCAN
    "WiFi Scan": "Scan nearby WiFi networks (SSID, channel, signal, security).",
    "BLE Scan": "Scan for nearby Bluetooth LE devices.",
    "BT Tracker": "Track one BT device by MAC address (live signal strength).",
    "AirTag Scan": "Detect Apple AirTags and BLE trackers nearby.",
    # SNIFF
    "WiFi Wardrive": "Log WiFi networks with GPS position to the loot DB.",
    "BT Wardrive": "Log Bluetooth devices with GPS position to the loot DB.",
    "Pkt Sniffer": "Capture raw WiFi packets in monitor mode.",
    "HS Capture": "Capture a WPA handshake (for cracking), saved to SD card.",
    "HS Capture no SD": "Capture a WPA handshake, streamed over serial (no SD card).",
    # ATTACK
    "Deauth": "Disconnect clients of a target network (deauth on BSSID+ch).",
    "Blackout": "Mass deauth across all channels (WiFi jamming).",
    "Evil Twin": "Clone an AP + login portal to capture credentials.",
    "SAE Flood": "Flood WPA3 with SAE Commit frames (WPA3 attack).",
    "Evil Portal": "Standalone rogue AP with a captive portal (no cloning).",
    "Dragon Drain": "Host-side WPA3 SAE flood (scapy) - exhausts the AP.",
    "MITM": "Man-in-the-middle: ARP spoofing + traffic capture.",
    "BlueDucky (WIP)": "HID injection over Bluetooth - work in progress.",
    "RACE Attack (WIP)": "Airoha BT exploit (bleak GATT) - work in progress.",
    # ADDONS
    "BLE HID (WIP)": "Emulate a keyboard/mouse over BLE - work in progress.",
    "HID Type (WIP)": "Type text as a BLE HID device - work in progress.",
    "LoRa Mesh": "Pick MeshCore or Meshtastic, set region/band + preset, then chat (off-grid).",
    "WDGWars Chat": "WDGWars chat over Reticulum/LXMF (game).",
    "WDGWars Sync": "Upload wardriving/ADS-B/MeshCore loot to the WDGWars portal.",
    "Cluster Wardrive": "XIAO acts as ESP-NOW core for a node fleet; streams fleet hits over serial.",
    "WDGWars Pair": "Link this device to your WDGWars account with a short code (no 64-hex key typing).",
    "Flipper Zero": "Drive a Flipper Zero (SubGHz, NFC, Device Info).",
    "ADS-B Radar": "Track aircraft via RTL-SDR (dump1090, 1090 MHz).",
    "433 MHz Scanner": "Listen for 433 MHz sensors (rtl_433).",
    "433 Capture": "Record 433 MHz signals: raw .cu8 (URH) + decoded log + Flipper .sub.",
    "Emergency Radio": "Detect emergency RF (fire VHF, police TETRA).",
    "433 Hunter": "Fox-hunt a 433.92 MHz signal (RSSI + beep). Trigger the remote.",
    "433 Beep": "Cycle the 433 Hunter beep volume: loud / quiet / off.",
    "Monster RF SubGHz": "Lab5 Monster RF (CC1101) - sub-GHz RX/record.",
    "Mute Sounds": "Mute game sounds.",
    "PipBoy Watch": "Connect the PipBoy watch (BLE NUS, PIN).",
    "Proximity": "Proximity radar for other WDGWars players (BLE adv).",
    "Prox Radar": "Open the proximity radar screen.",
    # SYSTEM
    "STOP ALL": "Stop all running attacks and scans.",
    "Reboot ESP32": "Reboot the ESP32 firmware.",
    "GPS": "Toggle the GPS receiver (position, fix).",
    "LoRa": "Toggle the LoRa power rail (SX1262).",
    "SDR": "Toggle power to the RTL-SDR dongle.",
    "USB": "Toggle the USB port power (AIO GPIO).",
    "Whitelist": "Edit the ignored-MAC whitelist.",
    "Radio (WiFi card)": "Pick ESP32 or a host AC1200 card per WiFi function (both connected).",
    "Upload WPA-SEC": "Upload handshakes to WPA-sec (cloud cracking).",
    "Download WPA-SEC": "Download cracked passwords from WPA-sec.",
    "Upload WiGLE": "Upload wardriving CSV to wigle.net (needs WDG_WIGLE_NAME + WDG_WIGLE_TOKEN).",
    "Flash ESP32": "Flash firmware to the ESP32 (board picker).",
    "Update Game": "Update the game (git pull).",
    "Backup Data": "Export ALL your data (loot, XP, badges, chat ID) to one ~/wdgo-backup file.",
    "Restore Data": "Restore the newest ~/wdgo-backup file, then relaunch.",
    "Session Data": "Browse captured loot sessions; preview files or delete a session ([X]).",
    "Captured Data": "Captured credentials: Evil Twin / portal logins + WPA-sec cracked WiFi ([TAB] switch).",
    # CRISIS
    "Wikipedia (Kiwix)": "Offline Wikipedia (Kiwix) in a kiosk browser.",
    "Offline Nav System": "WDGWorldMap - offline navigation (map + routing).",
    "Organic Maps": "Organic Maps - offline vector maps + navigation.",
    "Status": "Check whether crisis services are up (Kiwix/map).",
}
TIP_DELAY = 3.5     # seconds of dwell on a menu item before its tooltip appears

# commands that are host-side Python actions but map cleanly to a serial command
_PY_TO_SERIAL = {"_reboot_esp32": "reboot"}
QUIPS = ["stay dark", "ghost mode", "ctrl+alt+hack", "connection is power"]
MARKERS = [  # angle_deg, radius_px, color, type, label, info
    (-120, 70,  TEAL,    "WiFi",  "HomeNet_5G",  "WPA2  -42dBm  ch6"),
    (60,   150, MAGENTA, "BLE",   "AirPods-9c",  "BLE  -61dBm"),
    (25,   100, PURPLE,  "MESH",  "WDG_koko",    "MeshCore  -55dBm"),
    (150,  175, ICE,     "ADS-B", "RYR4T",       "FL310  412kt"),
    (-50,  120, TEAL,    "WiFi",  "Linksys_2A",  "WPA2  -67dBm  ch1"),
    (-100, 210, MAGENTA, "BLE",   "Tile_ff01",   "BLE  -73dBm"),
]
TERM = [
    ("18:42:07", "> boot ok - serial /dev/ttyUSB0 115200", DIM),
    ("18:42:07", "ESP32 ready - firmware projectZero", TEAL),
    ("18:42:09", "GPS fix 3D - 7 sats - 44.26N 70.53W", SUCCESS),
    ("18:42:10", "idle - press TAB for menu", TXT),
]

# Addons that need a hardware module powered first. cmd -> (module_key, label, addon_name).
# When launched with the module OFF, the game asks before enabling it.
ADDON_DEPS = {
    "_meshcore":      ("lora", "LoRa", "MeshCore Messenger"),
    "_sdr_adsb":      ("sdr",  "SDR",  "ADS-B Radar"),
    "_sdr_433":       ("sdr",  "SDR",  "433 MHz Scanner"),
    "_sdr_grab433":   ("sdr",  "SDR",  "433 Capture"),
    "_sdr_emergency": ("sdr",  "SDR",  "Emergency Radio"),
    "_sdr_keyhunt":   ("sdr",  "SDR",  "433 Hunter"),
}


def _detect_crisis():
    """Presence detection for the CRISIS offline services. Returns the set of
    available keys among {'wiki','ctos','nav'}, or None on failure (=> show all;
    never hide functionality just because a probe errored). Env WDGO_CRISIS
    overrides for testing: a comma list, empty/'none' = nothing present."""
    ov = os.environ.get("WDGO_CRISIS")
    if ov is not None:
        return {t.strip() for t in ov.replace("none", "").split(",") if t.strip()}
    try:
        import shutil, socket, subprocess
        home = os.path.expanduser("~")               # survival data is the login user's,
        su = os.environ.get("SUDO_USER")             # not /root even when run under sudo
        if su:
            try:
                import pwd
                home = pwd.getpwnam(su).pw_dir
            except Exception:
                home = "/home/" + su

        def _port(p):
            try:
                with socket.create_connection(("localhost", p), 0.4):
                    return True
            except Exception:
                return False

        def _zim():
            try:
                return any(f.lower().endswith(".zim")
                           for f in os.listdir(os.path.join(home, "survival", "zim")))
            except Exception:
                return False

        avail = set()
        try:    # Organic Maps = user flatpak
            if subprocess.run(["flatpak", "info", "app.organicmaps.desktop"],
                              capture_output=True, timeout=4).returncode == 0:
                avail.add("nav")
        except Exception:
            pass
        if shutil.which("kiwix-serve") or _zim() or _port(8080):   # Wikipedia/Kiwix
            avail.add("wiki")
        if (os.path.exists(os.path.join(home, "survival", "pmtiles", "planet.pmtiles"))
                or _port(8082)                                     # Offline Nav / WDGWorldMap
                or os.path.exists(os.path.join(home, ".local", "share",
                                               "applications", "ctos-map.desktop"))):
            avail.add("ctos")
        return avail
    except Exception:
        return None


def main():
    shot = os.environ.get("WDGO_SHOT")
    set_config_flags(FLAG_VSYNC_HINT)
    init_window(W, H, "WatchDogsGo")
    set_target_fps(60)
    hide_cursor()            # keyboard-driven game; no mouse pointer on screen
    set_exit_key(KEY_NULL)   # ESC must not auto-close the window; we show a Y/N modal
    # NOTE: fullscreen is left to the compositor (labwc) — see the main loop. In-game
    # borderless sizes wrong on the rotated DSI panel (720x720 instead of 1280x720).

    # ---- animated pre-splash + threaded backend load --------------------------
    # A cold launch spends ~4-5s importing the backend managers and probing
    # serial/GPS/loot (connect() alone sleeps ~6s waiting for the XIAO over the USB
    # rail) BEFORE the real boot splash can render — the window would sit blank.
    # The backend is pure logic (no raylib, no signal handlers), so run its heavy
    # import + Backend() + connect() on a worker thread and animate a light loader
    # here on the GL thread meanwhile: the gorilla badge, a pulsing glow and a big
    # "INITIALIZING" with cycling dots. Deliberately simpler than the real splash
    # (no scramble, no progress bar, no system check) so the two never clash. Bonus:
    # the serial/USB wait now overlaps the loader instead of adding to it.
    _pcx = W // 2
    _ply = H // 2 - 70                           # logo centre (room for the label below)
    import threading
    _ld = {"bk": None, "connected": False, "err": None, "done": False}
    def _bootload():
        try:
            import backend as _be
            _b = _be.Backend(); _ld["bk"] = _b
            _b.connect(); _ld["connected"] = True
        except Exception as _e:
            _ld["err"] = _e
        finally:
            _ld["done"] = True
    _bt = threading.Thread(target=_bootload, name="bootload", daemon=True); _bt.start()

    _plogo = None
    _plp = os.path.join(A, "boot", "logo.png")
    if os.path.isfile(_plp):
        _plogo = load_texture(_plp); set_texture_filter(_plogo, TEXTURE_FILTER_BILINEAR)
    _pfp = os.path.join(A, "ChakraPetch-Bold.ttf")
    _pf = load_font_ex(_pfp, 46, None, 0) if os.path.isfile(_pfp) else get_font_default()
    set_texture_filter(_pf.texture, TEXTURE_FILTER_BILINEAR)
    _plw = measure_text_ex(_pf, "INITIALIZING...", 46, 3).x   # widest form -> stable left edge
    _pf0 = 0
    while (not _ld["done"] or _pf0 < 40) and not window_should_close():
        _st = get_time()
        _pulse = 0.5 + 0.5 * math.sin(_st * 2.2)
        _ez = min(1.0, _pf0 / 22.0); _ez = 1 - (1 - _ez) ** 3   # gentle scale-in
        _pd = int(320 * (0.90 + 0.10 * _ez))
        _dots = "." * (1 + (_pf0 // 12) % 3)                    # . .. ... cycling
        begin_drawing()
        clear_background(C(0, 0, 0, 255))
        draw_rectangle_gradient_v(0, 0, W, H, C(10, 17, 25, 255), C(4, 7, 11, 255))
        begin_blend_mode(BLEND_ADDITIVE)                       # soft cyan/magenta glow
        draw_circle(_pcx, _ply, _pd * 0.60, C(53, 230, 255, int(16 + 26 * _pulse)))
        draw_circle(_pcx, _ply, _pd * 0.50, C(255, 61, 240, int(10 + 16 * _pulse)))
        end_blend_mode()
        if _plogo:
            draw_texture_pro(_plogo, Rectangle(0, 0, _plogo.width, _plogo.height),
                             Rectangle(_pcx - _pd / 2, _ply - _pd / 2, _pd, _pd),
                             Vector2(0, 0), 0.0, C(255, 255, 255, 255))
        draw_ring(Vector2(_pcx, _ply), _pd / 2 + 3, _pd / 2 + 5, 0, 360, 72,
                  C(53, 230, 255, int(120 + 60 * _pulse)))
        draw_text_ex(_pf, "INITIALIZING" + _dots,
                     Vector2(_pcx - _plw / 2, _ply + _pd / 2 + 12), 46, 3, C(234, 255, 251, 255))
        end_drawing()
        _pf0 += 1
    if _plogo is not None: unload_texture(_plogo)
    if os.path.isfile(_pfp): unload_font(_pf)

    # if the loader loop was cut short (window closed mid-boot), wait for the worker
    # to finish so we never build a second Backend() racing it on the serial/USB rail.
    if not _ld["done"]:
        _bt.join(timeout=30)
    # collect the backend the worker built (fall back to a synchronous build if the
    # thread errored before finishing), then make sure it's connected.
    bk = _ld["bk"]
    if bk is None:
        import backend as be
        bk = be.Backend()
    if not _ld["connected"]:
        try: bk.connect()
        except Exception: pass

    def font(name, size):
        p = os.path.join(A, name)
        f = load_font_ex(p, size, None, 0) if os.path.isfile(p) else get_font_default()
        set_texture_filter(f.texture, TEXTURE_FILTER_BILINEAR)
        return f
    disp = font("ChakraPetch-SemiBold.ttf", 48); dispB = font("ChakraPetch-Bold.ttf", 48)
    mono = font("JetBrainsMono-Regular.ttf", 44); monoB = font("JetBrainsMono-Bold.ttf", 44)

    char = None
    cp = os.path.join(A, "hacker.png")
    if os.path.isfile(cp):
        char = load_texture(cp); set_texture_filter(char, TEXTURE_FILTER_BILINEAR)

    # player avatar for the map center (WDGWars operator) — replaces the skull
    player_tex = None
    pp = os.path.join(A, "map", "player.png")
    if os.path.isfile(pp):
        player_tex = load_texture(pp); set_texture_filter(player_tex, TEXTURE_FILTER_BILINEAR)

    # CRISIS menu is conditional: show only the offline services actually present
    # on this system (Kiwix / Offline Nav / Organic Maps). If none are found, the
    # whole CRISIS tab is hidden. Done once at startup -> zero per-frame cost.
    _crisis_avail = _detect_crisis()
    if _crisis_avail is not None:
        _CK = {"_srv_wiki": "wiki", "_srv_ctos": "ctos", "_srv_nav": "nav"}
        for _cat in list(TABS):
            if _cat[0] != "CRISIS":
                continue
            _items = _cat[2]
            if not any(_CK.get(it[2]) in _crisis_avail for it in _items):
                TABS.remove(_cat)          # nothing present -> drop CRISIS entirely
            else:                          # keep present services + non-service rows (Status)
                _items[:] = [it for it in _items
                             if it[2] not in _CK or _CK[it[2]] in _crisis_avail]
            break

    # ---- EXTRAS: discover user mini-apps and synthesise the EXTRAS tab ----
    # Scan reads manifests only (no extra code imported); the tab is appended
    # like CRISIS, only when >=1 extra is present. See wdgo_extras / docs.
    _extras = wdgo_extras.scan()
    # Always show EXTRAS (even empty) so the live [Rescan] is always reachable —
    # adding an extra never requires restarting the game.
    TABS.append(("EXTRAS", PURPLE, wdgo_extras.extras_tab_items(_extras)))
    MENU_DESC["Hub"] = "Browse and install reviewed extras (signed, checksum-verified, always sandboxed)."
    for _m in _extras:
        _d = ("DISABLED: " + _m.error) if _m.error else (_m.description or _m.about[:90])
        if _m.update_note:
            _d = "(%s) %s" % (_m.update_note, _d)
        MENU_DESC[_m.name[:18]] = _d

    # animated gorilla mascot, one looping clip per tab — keyed BY CATEGORY NAME
    # (assets/characters/<name>/) so CRISIS/EXTRAS (and any future tab) get their own
    # clip instead of falling back to SYSTEM. A tab with no folder shows the static char.
    GNAMES = ["scan", "sniff", "attack", "addons", "system", "extras", "crisis"]
    gchar_by_name = {}  # "<tabname>" -> [frame_texture, ...]
    for gn in GNAMES:
        folder = os.path.join(A, "characters", gn)
        frames = []
        if os.path.isdir(folder):
            for fn in sorted(os.listdir(folder)):
                if fn.lower().endswith(".png"):
                    tx = load_texture(os.path.join(folder, fn))
                    set_texture_filter(tx, TEXTURE_FILTER_BILINEAR)
                    frames.append(tx)
        if frames:
            gchar_by_name[gn] = frames
    GFPS = 5.0  # gorilla animation playback rate (slower = calmer loop)

    target = load_render_texture(W, H)
    set_texture_filter(target.texture, TEXTURE_FILTER_BILINEAR)

    tab, sel, field, menu_open, msel = 2, 0, False, False, -1
    if os.environ.get("WDGO_MENU"): menu_open = True
    if os.environ.get("WDGO_TAB"): tab = int(os.environ["WDGO_TAB"]) % len(TABS)
    scanning = bool(os.environ.get("WDGO_SCAN"))
    confirm_quit = bool(os.environ.get("WDGO_QUIT"))
    quit_modules = []           # active modules (GPS/LoRa/SDR) shown on the quit prompt
    quit_can_restore = False    # True when current rails differ from the launch snapshot
    quit_mode = "off"           # how to exit: 'off' | 'leave' | 'restore'
    confirm_restore = False     # Y/N guard before overwriting data on restore
    confirm_del = None          # session name pending delete in LOOT DB (Y/N), or None
    dep_prompt = None           # (item, module_key, module_label, addon_name) when an addon needs a module OFF
    next_glitch, glitch_until = 2.0, 0.0
    frame = 0
    worms = [{"x": 0.0, "y": 0.0, "dir": (1, 0), "seg": 0.0, "trail": [], "life": 0, "init": False} for _ in MARKERS]

    # ---- real backend (ESP32 serial + GPS + loot DB import) ----
    # NOTE: `bk` was built + connected on a worker thread during the animated
    # pre-splash above (overlapping the ~6s serial/USB wait). Nothing to do here.

    # ---- notification sounds (bundled in wdgong_ui/assets/sounds, respect Mute Sounds) ----
    sounds = {}
    try:
        init_audio_device()
        _sdir = os.path.join(A, "sounds")
        for _k, _f in (("mesh", "meshcore_notify.wav"), ("emergency", "warning.wav"),
                       ("fire", "fire.wav"), ("police", "police.wav"), ("ems", "ambulance.wav"),
                       ("pip", "keyhunt_pip.wav")):
            _p = os.path.join(_sdir, _f)
            if os.path.isfile(_p):
                sounds[_k] = load_sound(_p)
    except Exception:
        sounds = {}

    running_keys = set()     # state_keys currently toggled on
    input_mode = None        # pending target-input dialog: "bssid_ch" | "mac"
    input_item = None        # the (hk,label,cmd,skey,inp) awaiting input
    input_buf = ""
    # Evil Twin / Evil Portal pickers
    et_screen = None         # None | "net" | "portal"
    et_mode = "twin"         # "twin" | "portal"
    et_sel = 0               # cursor in network picker
    et_selected = set()      # multi-selected network rows
    portal_sel = 0           # cursor in portal picker
    portal_list = []         # [(name, html|None), ...]
    et_pending = False       # waiting for a scan to open the net picker
    # messenger screens (MeshCore / WDGWars chat)
    msg_screen = None        # None | "meshcore" | "wdgwars"
    msg_buf = ""             # compose input
    msg_scroll = 0           # chat scroll offset (lines from bottom)
    loot_screen = False      # LOOT DATABASE stats screen
    loot_log_scroll = 0      # scroll offset in the session-log journal
    loot_log_sel = 0         # selected session row in the journal
    loot_detail = None       # drilled-in session dict (None = journal list)
    loot_detail_files = []   # cached file list of the drilled-in session
    loot_detail_scroll = 0   # file-list scroll in the drill-in view
    loot_detail_sel = 0      # selected file in the drill-in view
    loot_adsb = None         # ADS-B tracks of the drilled-in session (adsb_tracks.load) or None
    loot_adsb_sel = -1       # highlighted aircraft (-1 = all equal)
    loot_adsb_cov = True     # show the receiver coverage outline
    loot_adsb_msg = ""       # last action (KML saved ...)
    loot_preview = None      # opened file preview dict (None = file list)
    loot_preview_scroll = 0  # scroll in the file preview
    loot_log_refresh_t = 0.0 # last journal auto-refresh time
    prox_screen = False      # WDGWARS proximity radar screen
    cluster_screen = False   # Cluster-core (node fleet) screen
    fleet_sel = 0            # selected row in the fleet health table (for manual channel pins)
    adsb_trails = {}         # icao -> recent [(lat,lon)] breadcrumb trail for the ADS-B radar
    pair_screen = False      # WDGWars device-pairing screen
    scan_modal = None        # scan-results modal: None | "wifi" | "ble" | "airtag"
    scan_modal_scroll = 0    # scroll offset in the scan-results modal
    pending_scan = None      # scan type launched from SCAN tab, awaiting completion
    pending_scan_t = 0.0     # when that scan was launched (safety-net timer)
    zoom = 1.0               # map zoom factor
    map_pan = [0.0, 0.0]     # D-pad pan offset (dlat, dlon) added to your position; 0 recenters
    _last_coslat = 1.0       # last frame's cos(lat), so pan speed matches E-W on screen
    ZOOM_LABELS = [(0.6, "REGION"), (0.85, "CITY"), (1.0, "DISTRICT"),
                   (1.4, "STREET"), (1.9, "BLOCK"), (2.5, "CLOSE")]
    cluster_sel = -1         # selected cluster on the map (-1 = none / nav off)
    cluster_drill = False    # drill-down popup (individual nodes of a cluster) open
    cluster_scroll = 0       # scroll offset inside the drill-down popup
    clusters = []            # current geographic clusters (recomputed on zoom/move)
    cl_cache_key = None      # (zoom, center) the clusters were built for
    cl_cache_t = 0.0
    bm_cache = {}            # (z,x,y) -> loaded basemap GPU texture (None = missing)
    basemap = Basemap(os.path.join(A, "map", "basemap.mbtiles")) if Basemap else None
    wl_screen = False        # whitelist editor
    radio_screen = False     # radio matrix (ESP32 vs host WiFi card)
    radio_sel = 0
    wl_sel = 0               # selected whitelist entry
    wl_scan = False          # scan-&-select sub-mode
    wl_scan_sel = 0
    flash_screen = False     # ESP32 firmware flash board picker
    flash_sel = 0
    data_screen = False      # captured credentials viewer (Evil Twin + WPA-sec)
    data_scroll = 0
    data_tab = 0             # 0 = Evil Twin / portal, 1 = WPA-sec cracked WiFi
    mc_region_screen = False  # MeshCore region picker
    mc_region_sel = 0
    mc_contacts_panel = False # mesh nodes/contacts side panel (in messenger, [H])
    mesh_screen = False       # LoRa Mesh picker (stack -> region/band -> preset -> name)
    mesh_sel = 0              # selected row in the LoRa Mesh picker
    mesh_pick = ""            # stack being configured ("meshcore" | "meshtastic")
    mesh_name_edit = False    # editing the Meshtastic owner name in the picker
    mesh_name_buf = ""        # owner-name edit buffer
    # companion screens
    comp_screen = None        # None | flipper | subghz | mitm | watch | gameupd | extra
    active_extra = None       # running EXTRA (wdgo_extras.ExtraHost) or None
    _extra_origin = [0, 0]    # viewport origin fed to the active extra's draw calls
    _extra_view = [0, 0]      # viewport (w, h) the extra draws into
    comp_sel = 0
    comp_scroll = 0
    watch_pin = ""            # PipBoy PIN entry buffer
    hack_prog = 0.0           # SPACE-to-hack progress 0..1
    hacked = set()            # labels of pwned nodes
    if os.environ.get("WDGO_ET"):            # test hook: open a picker directly
        et_screen = os.environ["WDGO_ET"]
        if et_screen == "portal": portal_list = bk.get_portals()
    if os.environ.get("WDGO_LOOT"):          # test hook: open loot/stats screen
        loot_screen = True
    if os.environ.get("WDGO_PROX"):          # test hook: open proximity radar
        prox_screen = True
    if os.environ.get("WDGO_CLUSTER"):       # test hook: select first cluster
        cluster_sel = 0
    if os.environ.get("WDGO_CLUSTER_SIM"):   # demo/test hook: open fleet screen + sim
        bk.start_cluster(); cluster_screen = True
    elif os.environ.get("WDGO_CLUSTER_BRIDGE_AUTO"):  # real bridge: start the fleet on launch
        bk.start_cluster(); cluster_screen = True
    if not os.environ.get("WDGO_NO_MESH_RESUME"):   # auto-listen: resume last configured LoRa stack
        bk.auto_resume_mesh()
    if os.environ.get("WDGO_WL"):            # test hook: whitelist editor
        wl_screen = True; wl_scan = (os.environ["WDGO_WL"] == "scan")
    if os.environ.get("WDGO_FLASH"):         # test hook: flash board picker
        flash_screen = True
    if os.environ.get("WDGO_REGION"):        # test hook: MeshCore region picker
        mc_region_screen = True
    if os.environ.get("WDGO_COMP"):          # test hook: companion screen
        comp_screen = os.environ["WDGO_COMP"]
    if os.environ.get("WDGO_DATA"):          # test hook: captured data viewer
        data_screen = True
        bk.captures.extend(["Email: bob@gmail.com | Password: hunter2",
                            "User: admin | Password: r00t",
                            "email: test@wp.pl | password: qwerty123"])
    if os.environ.get("WDGO_MSG"):           # test hook: open messenger w/ demo msgs
        msg_screen = os.environ["WDGO_MSG"]
        demo = [("sys", "connecting to #general...", "sys"), ("in", "koko: siema z terenu", "in"),
                ("out", "uConsole: elo, mam fix GPS", "out"), ("in", "[DM] r4ptor: widzisz mnie?", "dm"),
                ("in", "koko: 5 nodeow w zasiegu", "in")]
        (bk.mc_log if msg_screen == "meshcore" else bk.wd_log).extend(demo)

    def new_worms(n):
        return [{"x": 0.0, "y": 0.0, "dir": (1, 0), "seg": 0.0, "trail": [], "life": 0, "init": False} for _ in range(n)]

    def live_markers():
        """Build map markers from real scanned WiFi + BLE; None -> demo."""
        nets = bk.networks(); bles = bk.ble_devices
        if not nets and not bles:
            return None
        out = []
        for i, n in enumerate(nets[:12]):
            h = sum(ord(c) for c in (n.bssid or n.ssid or str(i)))
            ang = (h * 47 + i * 23) % 360
            try:
                rssi = int(str(n.rssi).replace("dBm", "").strip())
            except (ValueError, AttributeError):
                rssi = -75
            strength = max(0.0, min(1.0, (rssi + 90) / 50.0))
            rr = int(258 - strength * 188)
            auth = (n.auth or "")
            col = MAGENTA if "WPA3" in auth or "SAE" in auth else (PURPLE if "WPA2" in auth else TEAL)
            info = "%s  %sdBm  ch%s" % (auth or "open", n.rssi or "?", n.channel or "?")
            out.append((ang, rr, col, "WiFi", n.ssid or "<hidden>", info))
        for j, (mac, rssi_s, name, tag) in enumerate(bles[:8]):
            h = sum(ord(c) for c in (mac or str(j)))
            ang = (h * 59 + j * 31) % 360
            try: rssi = int(str(rssi_s).strip())
            except (ValueError, AttributeError): rssi = -78
            strength = max(0.0, min(1.0, (rssi + 90) / 50.0))
            rr = int(258 - strength * 188)
            col = WARN if tag else MAGENTA
            lab = (name or mac[-8:]) + (" [%s]" % tag if tag else "")
            out.append((ang, rr, col, "BLE", lab, "%s  %sdBm" % (mac, rssi_s)))
        return out

    def dispatch(item):
        """Run a menu item like the OG _execute_item: toggle/stop/send."""
        nonlocal scanning, input_mode, input_item
        nonlocal et_screen, et_mode, et_sel, et_selected, portal_sel, portal_list, et_pending
        nonlocal msg_screen, msg_buf, msg_scroll, prox_screen, wl_screen, wl_sel, wl_scan
        nonlocal radio_screen, radio_sel
        nonlocal flash_screen, flash_sel, mc_region_screen, mc_region_sel
        nonlocal comp_screen, comp_sel, comp_scroll, cluster_screen, pair_screen
        nonlocal active_extra, _extras, sel
        nonlocal loot_screen, loot_detail, loot_preview, loot_log_sel, data_screen
        nonlocal mesh_screen, mesh_sel, mesh_pick
        hk, label, cmd, skey, inp = item
        # LoRa Mesh = unified picker (MeshCore/Meshtastic -> region/band -> preset -> chat)
        if cmd == "_lora_mesh":
            mesh_screen = True; mesh_sel = 0
            mesh_pick = bk.mesh_stack() or "meshcore"
            return True
        # Session Data manager = the LOOT DATABASE screen (browse / preview / delete)
        if cmd == "_session_data":
            loot_screen = True; loot_detail = None; loot_preview = None
            loot_log_sel = 0; bk.sessions_refresh(); return True
        # Captured Data = the captured-credentials viewer (also [P] on the map)
        if cmd == "_captured_data":
            data_screen = True; return True
        # companion screens (Flipper / SubGHz / MITM / Watch / Update Game)
        _COMP = {"_flipper": "flipper", "_subghz": "subghz", "_mitm": "mitm",
                 "_watch_connect": "watch", "_update_game": "gameupd"}
        if cmd in _COMP:
            comp_screen = _COMP[cmd]; comp_sel = 0; comp_scroll = 0
            if comp_screen == "flipper": bk._mgr("flipper"); bk.host_action("_flipper", None)
            elif comp_screen == "subghz": bk._mgr("subghz"); bk.host_action("_subghz", None)
            elif comp_screen == "mitm": bk._mgr("mitm")
            elif comp_screen == "watch": bk.prox_pause(); bk._mgr("watch"); bk.watch_scan()
            elif comp_screen == "gameupd": bk.check_game_update()
            return True
        # EXTRAS: rescan the folder, or launch a third-party mini-app
        if cmd == "_extra_rescan":
            _extras = wdgo_extras.scan()
            for _i2 in range(len(TABS)):
                if TABS[_i2][0] == "EXTRAS":
                    TABS[_i2] = ("EXTRAS", PURPLE, wdgo_extras.extras_tab_items(_extras))
                    break
            for _m2 in _extras:
                _d2 = ("DISABLED: " + _m2.error) if _m2.error else (_m2.description or _m2.about[:90])
                if _m2.update_note:
                    _d2 = "(%s) %s" % (_m2.update_note, _d2)
                    bk._term("[EXTRAS] %s %s" % (_m2.name, _m2.update_note), "ok")
                MENU_DESC[_m2.name[:18]] = _d2
            sel = 0   # list may have changed length — park the cursor on Rescan
            bk.set_status("extras rescanned (%d found)" % len(_extras))
            return True
        if cmd == "_extra_hub":
            # built-in hub browser, drawn through the same extras viewport
            active_extra = wdgo_extras_hub.HubScreen(_make_extra_bridge(wdgo_extras.ExtraMeta("hub")))
            active_extra.open()
            comp_screen = "extra"; comp_sel = 0; comp_scroll = 0
            return True
        if cmd.startswith("_extra:"):
            _eid = cmd.split(":", 1)[1]
            _meta = next((m for m in _extras if m.id == _eid), None)
            if _meta is None:
                bk.set_status("extra not found"); return True
            if _meta.error:
                bk.set_status("extra disabled: " + _meta.error); return True
            # Sandboxed by default (jailed non-root subprocess + capability broker);
            # only code the user trusts may ever run in-process. See wdgo_extras.make_host.
            # launch() puts a consent card in front of code the user has not approved yet.
            active_extra = wdgo_extras.launch(_meta, _make_extra_bridge(_meta),
                                              os.path.join(wdgo_extras.data_root(), _meta.id))
            active_extra.open()
            comp_screen = "extra"; comp_sel = 0; comp_scroll = 0
            return True
        # MeshCore region picker
        if cmd == "_meshcore_region":
            regs = bk.mc_regions(); mc_region_sel = 0
            for i, (k, _, _) in enumerate(regs):
                if k == bk.mc_region_current(): mc_region_sel = i
            mc_region_screen = True; return True
        # whitelist editor
        if cmd == "_whitelist":
            wl_screen = True; wl_sel = 0; wl_scan = False; return True
        # radio matrix (which radio serves which WiFi function)
        if cmd == "_radio_matrix":
            bk.radio_detect_refresh(); radio_screen = True; radio_sel = 0; return True
        # firmware flash board picker
        if cmd == "_flash_esp32":
            flash_screen = True; flash_sel = 0
            boards = bk.flash_boards()
            for i, (k, _) in enumerate(boards):
                if k == bk.flash_default_board(): flash_sel = i
            return True
        # proximity radar screen
        if cmd == "_prox_view":
            bk.prox_ensure(); prox_screen = True; return True
        if cmd == "_prox_toggle":
            bk.prox_toggle(); return
        # messenger screens
        if cmd == "_meshcore":
            bk.host_action("_meshcore", None)       # start LoRa + MeshCore
            msg_screen = "meshcore"; msg_buf = ""; msg_scroll = 0; return True
        if cmd == "_wdgwars":
            bk.start_wdgwars()
            msg_screen = "wdgwars"; msg_buf = ""; msg_scroll = 0; return True
        # WDGWars portal upload — push pending loot sessions (background)
        if cmd == "_wdgwars_sync":
            bk.wdgwars_sync(); bk.set_status("WDGWars Sync"); return True
        # WDGWars device pairing — request a code, poll, save the key
        if cmd == "_wdgwars_pair":
            bk.pair_start(); pair_screen = True; return True
        # STOP ALL
        if cmd == "stop" or skey == "_stop_all":
            bk.stop_attacks(); running_keys.clear(); scanning = False
            bk.set_wardrive(None)
            bk.set_mode("idle"); bk.set_status("stopped all"); return
        # Evil Twin: pick target network(s) -> portal -> serial sequence
        if cmd == "_evil_twin":
            et_mode = "twin"
            if bk.networks():
                et_screen = "net"; et_sel = 0; et_selected = set()
            else:
                bk.set_mode("scan"); bk.send("scan_networks")
                et_pending = True; scanning = True
                bk.set_status("Evil Twin: scanning for targets...")
            return True
        # Evil Portal: standalone rogue AP, portal picker only
        if cmd == "_evil_portal":
            et_mode = "portal"; portal_list = bk.get_portals(); portal_sel = 0
            et_screen = "portal"; bk.set_status("Evil Portal: pick a template")
            return True
        # Firmware serial-wardrive (node streams WiGLE, host tags GPS) — toggle on/off
        if cmd in ("_wardrive_wifi", "_wardrive_bt"):
            if skey in running_keys:
                bk.stop_wardrive_serial(); running_keys.discard(skey); scanning = False
            else:
                bk.start_wardrive_serial("wifi" if cmd == "_wardrive_wifi" else "bt")
                running_keys.add(skey); scanning = True
            return
        # Cluster-core (node fleet): start / re-open / stop the fleet screen.
        # The menu only STARTS or RE-OPENS the fleet: selecting it never stops a
        # running fleet (that was confusing). Escape hides the screen while the
        # fleet keeps collecting; stopping is explicit — [X] on the fleet screen,
        # STOP ALL (S), or starting any other radio op (one radio, mutually
        # exclusive — the backend yields the cluster automatically).
        if cmd == "_cluster":
            if not bk.cluster_active():
                bk.start_cluster()
            cluster_screen = True
            fleet_sel = 0
            return True
        # host-side python action -> reuse the OG manager via backend.host_action
        if cmd.startswith("_"):
            nets = bk.networks()
            tgt = nets[msel] if (0 <= msel < len(nets)) else None
            if bk.host_action(cmd, tgt):
                bk.set_status(label)
            else:
                bk._term(label + " - not implemented", "warn")
            return
        # needs a target first (deauth/sae = bssid+ch, tracker = mac)
        if inp in ("bssid_ch", "mac") and skey not in running_keys:
            nets = bk.networks()
            if msel >= 0 and nets and msel < len(nets):
                n = nets[msel]
                param = ("%s %s" % (n.bssid, n.channel)) if inp == "bssid_ch" else n.bssid
                _fire(cmd, skey, label, param)
            else:
                input_mode = inp; input_item = item
                bk.set_status("enter %s (or scan+select a target)" % inp)
            return
        _fire(cmd, skey, label, "")

    def _fire(cmd, skey, label, param):
        nonlocal scanning
        if skey in running_keys:                 # toggle off
            bk.send("stop"); running_keys.discard(skey); scanning = False
            bk.set_wardrive(None)
            bk.set_mode("idle"); bk.set_status("stopped " + label); return
        full = (cmd + " " + param).strip()
        if cmd in ("scan_networks", "scan_bt", "scan_airtag"):
            try: bk.net and bk.net.clear()
            except Exception: pass
            if cmd in ("scan_bt", "scan_airtag"): bk.ble_devices.clear()
            # only the WiFi scan streams CSV rows that need scan-mode routing; BLE/AirTag
            # are parsed by their own regexes, so keep idle (airtag counts must not hit the
            # CSV parser)
            bk.set_mode("scan" if cmd == "scan_networks" else "idle")
        elif cmd.startswith("start_sniffer"):
            bk.set_mode("sniffer")
        else:
            bk.set_mode("idle")
        bk.send(full)
        running_keys.add(skey)
        if skey in ("wardriving", "bt_scanning"):   # continuous wardrive sweep (auto-repeat)
            bk.set_wardrive(cmd)
        if any(k in label.lower() for k in ("scan", "wardrive", "sniff", "deauth", "blackout", "flood", "handshake", "hs ")):
            scanning = True
        bk.set_status("running " + label)

    # global UI font scale — enlarges all on-screen text EXCEPT the terminal (raw=True).
    # Applied to both text() and tw() so measurement-based layout stays consistent.
    UISCALE = float(os.environ.get("WDGO_UISCALE", "1.16"))
    def text(f, s, x, y, sz, col, sp=1.0, raw=False):
        draw_text_ex(f, s, Vector2(float(x), float(y)), float(sz if raw else sz * UISCALE), sp, col)
    def tw(f, s, sz, sp=1.0, raw=False):
        return measure_text_ex(f, s, float(sz if raw else sz * UISCALE), sp).x
    def halo(x, y, col, layers=((20, 26), (12, 34))):
        begin_blend_mode(BLEND_ADDITIVE)
        for rad, al in layers:
            draw_circle(int(x), int(y), rad, C(col.r, col.g, col.b, al))
        end_blend_mode()
    def gtext(f, s, x, y, sz, col, sp=1.0, g=55):
        sz = sz * UISCALE
        begin_blend_mode(BLEND_ADDITIVE)
        for ox, oy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            draw_text_ex(f, s, Vector2(float(x + ox), float(y + oy)), float(sz), sp, C(col.r, col.g, col.b, g))
        end_blend_mode()
        draw_text_ex(f, s, Vector2(float(x), float(y)), float(sz), sp, col)

    def cyber_panel(x, y, w, h, col):
        """Cyberpunk overlay frame: dark fill + scanlines + glowing neon corner brackets."""
        x, y, w, h = int(x), int(y), int(w), int(h)
        draw_rectangle(x, y, w, h, C(8, 12, 19, 250))
        for sy in range(y + 4, y + h - 3, 4):            # faint scanlines
            draw_rectangle(x + 2, sy, w - 4, 1, C(col.r, col.g, col.b, 7))
        draw_rectangle_lines_ex(Rectangle(x, y, w, h), 1, C(col.r, col.g, col.b, 60))
        # top + bottom accent hairlines between the brackets
        L, th = 40, 3
        draw_rectangle(x + L, y, w - 2 * L, 2, C(col.r, col.g, col.b, 130))
        draw_rectangle(x + L, y + h - 2, w - 2 * L, 2, C(col.r, col.g, col.b, 130))
        corners = ((x, y, 1, 1), (x + w - 1, y, -1, 1),
                   (x, y + h - 1, 1, -1), (x + w - 1, y + h - 1, -1, -1))
        begin_blend_mode(BLEND_ADDITIVE)                 # soft glow under each corner
        for cx, cy, dx, dy in corners:
            draw_circle(cx, cy, 11, C(col.r, col.g, col.b, 38))
            draw_circle(cx, cy, 6, C(col.r, col.g, col.b, 60))
        end_blend_mode()
        for cx, cy, dx, dy in corners:                   # the neon L brackets
            draw_line_ex(Vector2(cx, cy), Vector2(cx + dx * L, cy), th, col)
            draw_line_ex(Vector2(cx, cy), Vector2(cx, cy + dy * L), th, col)
            draw_rectangle(cx - (0 if dx > 0 else 2), cy - (0 if dy > 0 else 2), 2, 2, HI)

    # ===== BOOT SPLASH + SYSTEM CHECK (WDGWorldMap boot style) =====
    # Lifted 1:1 from the offline WDGWorldMap boot splash (#wdgboot): radial bg,
    # corner brackets, scanlines + masked grid, a glowing circular logo that
    # scales/glitches/pulses in, a character-SCRAMBLE brand + tag, and a
    # cyan->magenta progress bar. Renamed to WatchDogsGo + real system checks.
    def run_splash():
        checks = bk.system_check()
        tot = len(checks)
        CY = C(53, 230, 255); MG = C(255, 61, 240); WHT = C(234, 255, 251)
        TAGC = C(159, 232, 255); DOMC = C(127, 179, 191); GRN = C(49, 224, 122); RED = C(255, 77, 94)
        led = {"ok": GRN, "warn": WARN, "fail": RED, "info": DOMC}
        CH = "ABCDEFGHJKLMNPQRSTUVWXYZ0123456789#%&/()=?+*<>[]{}$@!"
        logo_tex = None
        lp = os.path.join(A, "boot", "logo.png")
        if os.path.isfile(lp):
            logo_tex = load_texture(lp); set_texture_filter(logo_tex, TEXTURE_FILTER_BILINEAR)

        def scramble(final, prog):
            # WDGWorldMap-style name DECODE: the full-length string shows as
            # flickering random glyphs from the start, resolving left->right.
            n = len(final)
            k = prog * n
            o = []
            for i, ch in enumerate(final):
                if ch == " ":
                    o.append(" ")
                elif i < k:
                    o.append(ch)                                   # resolved
                else:
                    o.append(CH[random.randint(0, len(CH) - 1)])   # still decoding (flickers)
            return "".join(o)

        sf = 0
        splashshot = os.environ.get("WDGO_SPLASHSHOT")
        reveal_start = 24; reveal_every = 4
        while not window_should_close():
            st = get_time()
            shown = max(0, min(tot, (sf - reveal_start) // reveal_every + 1)) if sf >= reveal_start else 0
            done = shown >= tot and sf > reveal_start + tot * reveal_every + 24
            blink = int(st * 1.5) % 2 == 0
            if (get_key_pressed() != 0 or is_mouse_button_pressed(0)) and sf > 6:
                break
            if done and sf > reveal_start + tot * reveal_every + 90 and not splashshot:
                break

            begin_texture_mode(target)
            clear_background(C(0, 0, 0, 255))
            # radial-ish background
            draw_rectangle_gradient_v(0, 0, W, H, C(10, 17, 25, 255), C(4, 7, 11, 255))
            # grid (masked to center-ish by fading edges)
            for x in range(0, W, 64): draw_line(x, 0, x, H, C(53, 230, 255, 9))
            for y in range(0, H, 64): draw_line(0, y, W, y, C(53, 230, 255, 9))
            cxc = W // 2

            # ---- logo (scale-in + pulse + brief glitch) ----
            intro = min(1.0, sf / 46.0)
            ease = 1 - (1 - intro) ** 3
            d = int(176 * (0.86 + 0.14 * ease))
            ly = 150
            pulse = 0.5 + 0.5 * math.sin(st * 1.8)
            gx = random.randint(-3, 3) if 30 <= sf <= 48 and sf % 2 == 0 else 0
            begin_blend_mode(BLEND_ADDITIVE)
            draw_circle(cxc + gx, ly, d * 0.62, C(53, 230, 255, int(26 + 30 * pulse)))
            draw_circle(cxc + gx, ly, d * 0.52, C(255, 61, 240, int(14 + 18 * pulse)))
            end_blend_mode()
            if logo_tex:
                draw_texture_pro(logo_tex, Rectangle(0, 0, logo_tex.width, logo_tex.height),
                                 Rectangle(cxc + gx - d / 2, ly - d / 2, d, d), Vector2(0, 0), 0.0,
                                 C(255, 255, 255, int(255 * ease)))
            draw_ring(Vector2(cxc + gx, ly), d / 2 + 3, d / 2 + 5, 0, 360, 72, C(53, 230, 255, int(150 * ease)))

            # ---- brand (scramble) ----
            bp = max(0.0, min(1.0, (sf - 18) / 40.0))
            brand = scramble("WatchDogsGo", bp) if bp < 1 else "WatchDogsGo"
            bw = tw(dispB, brand, 46, 3)
            gtext(dispB, brand, cxc - bw / 2, ly + d / 2 + 18, 46, WHT, 3, 60)
            # tag (scramble)
            tp = max(0.0, min(1.0, (sf - 34) / 46.0))
            tag = scramble("FIELD PENTEST SYSTEM", tp)
            text(mono, tag, cxc - tw(mono, tag, 22, 2) / 2, ly + d / 2 + 76, 22, TAGC, 2)
            dp = max(0.0, min(1.0, (sf - 60) / 36.0))
            dom = scramble("WDGWars.pl", dp)
            text(mono, dom, cxc - tw(mono, dom, 15, 4) / 2, ly + d / 2 + 108, 15, DOMC, 4)

            # ---- system checks (2-column grid, revealed) ----
            okn = sum(1 for _, _, s in checks[:shown] if s == "ok")
            gy = ly + d / 2 + 142
            cols = 2; per = (tot + cols - 1) // cols
            colw = 420; gx0 = cxc - colw - 18
            crowh = min(24, int((H - 150 - gy) // max(1, per)))
            cfs = 17 if crowh >= 22 else 15
            for i in range(shown):
                col = i // per; row = i % per
                name, val, state = checks[i]
                rx = gx0 + col * (colw + 36); ry = int(gy + row * crowh)
                lc = led.get(state, DOMC)
                begin_blend_mode(BLEND_ADDITIVE)
                draw_circle(rx + 6, ry + crowh // 2, 6, C(lc.r, lc.g, lc.b, 80))
                end_blend_mode()
                draw_circle(rx + 6, ry + crowh // 2, 3.5, lc)
                text(mono, name.upper(), rx + 20, ry, cfs, WHT, 1)
                sv = val[:14]
                text(mono, sv, rx + colw - tw(mono, sv, cfs, 1), ry, cfs, lc, 1)

            # ---- progress bar (cyan -> magenta, glow, white edge) ----
            frac = shown / max(1, tot)
            bw2 = 560; bx = cxc - bw2 // 2; by = H - 92; bh = 8
            draw_rectangle(bx, by, bw2, bh, C(53, 230, 255, 30))
            draw_rectangle_lines_ex(Rectangle(bx, by, bw2, bh), 1, C(53, 230, 255, 90))
            fw = int((bw2 - 2) * frac)
            for xx in range(fw):
                f = xx / max(1, bw2 - 2)
                col = C(int(53 + (255 - 53) * f), int(230 + (61 - 230) * f), int(255 + (240 - 255) * f))
                draw_line(bx + 1 + xx, by + 1, bx + 1 + xx, by + bh - 1, col)
            begin_blend_mode(BLEND_ADDITIVE)
            if fw > 1: draw_rectangle(bx + fw - 2, by - 1, 3, bh + 2, WHT)
            end_blend_mode()
            num = ("SYSTEM READY" if shown >= tot else "INITIALIZING %d%%" % int(frac * 100))
            text(mono, num, cxc - tw(mono, num, 14, 4) / 2, by + 16, 14, GRN if shown >= tot else DOMC, 4)
            # OK/WARN/FAIL tally
            warn = sum(1 for _, _, s in checks[:shown] if s == "warn")
            fail = sum(1 for _, _, s in checks[:shown] if s == "fail")
            tally = "OK %d   WARN %d   FAIL %d" % (okn, warn, fail)
            text(mono, tally, cxc - tw(mono, tally, 13, 2) / 2, by - 24, 13, DOMC, 2)

            # ---- corner brackets ----
            for (ox, oy, sx, sy) in ((26, 26, 1, 1), (W - 26, 26, -1, 1), (26, H - 26, 1, -1), (W - 26, H - 26, -1, -1)):
                draw_line_ex(Vector2(ox, oy), Vector2(ox + sx * 36, oy), 2, C(53, 230, 255, 140))
                draw_line_ex(Vector2(ox, oy), Vector2(ox, oy + sy * 36), 2, C(53, 230, 255, 140))
            end_texture_mode()

            begin_drawing()
            clear_background(BLACK)
            draw_texture_rec(target.texture, Rectangle(0, 0, float(W), float(-H)), Vector2(0, 0), WHITE)
            begin_blend_mode(BLEND_ADDITIVE)
            for y in range(0, H, 3): draw_line(0, y, W, y, C(53, 230, 255, 6))
            end_blend_mode()
            for y in range(0, H, 3): draw_line(0, y, W, y, C(0, 0, 0, 30))
            # vignette
            draw_rectangle_gradient_v(0, 0, W, 90, C(0, 0, 0, 150), C(0, 0, 0, 0))
            draw_rectangle_gradient_v(0, H - 90, W, 90, C(0, 0, 0, 0), C(0, 0, 0, 150))
            draw_rectangle_gradient_h(0, 0, 120, H, C(0, 0, 0, 130), C(0, 0, 0, 0))
            draw_rectangle_gradient_h(W - 120, 0, 120, H, C(0, 0, 0, 0), C(0, 0, 0, 130))
            end_drawing()
            sf += 1
            if splashshot and sf > int(os.environ.get("WDGO_SPLASHFRAME", "120")):
                take_screenshot(splashshot); break
        if logo_tex:
            unload_texture(logo_tex)

    if os.environ.get("WDGO_SPLASHSHOT") or not shot:
        run_splash()
        if os.environ.get("WDGO_SPLASHSHOT"):
            bk.close(); close_window(); return

    # Fullscreen is handled by the compositor (labwc windowRule ToggleFullscreen on the
    # "WatchDogsGo" window) because the uConsole DSI panel is 720x1280 + Transform 270;
    # GLFW ignores the rotation and sizes its own borderless window to 720x720, leaving
    # the desktop visible on both sides. labwc is transform-aware and fills 1280x720.
    fs_dbg = bool(os.environ.get("WDGO_FSDEBUG"))
    fs_dbg_t = 0.0
    tip_last = (-2, -2)         # (tab, sel) the dwell timer is tracking
    tip_since = 0.0             # when the current item was first hovered
    emerg_was = False           # edge-detect emergency detections for the siren sound
    keyhunt_next_beep = 0.0     # fox-hunt pip scheduler (rate rises as you get hotter)
    hunt_full = False           # 433 Hunter / Capture panel: expanded full-screen view ([H])

    # ---- EXTRAS bridge: the raylib-backed services an extra's ctx facade calls.
    # All extra draw coords are local to its viewport; we offset by _extra_origin
    # (set each frame) and the host scissor clips them. Colours are by name only.
    _EPAL = {"txt": TXT, "hi": HI, "dim": DIM, "teal": TEAL, "danger": DANGER,
             "warn": WARN, "success": SUCCESS, "magenta": MAGENTA, "ice": ICE,
             "void": VOID, "edge": EDGE, "purple": PURPLE}
    _EKEYS = {"up": KEY_UP, "down": KEY_DOWN, "left": KEY_LEFT, "right": KEY_RIGHT,
              "enter": KEY_ENTER, "space": KEY_SPACE, "tab": KEY_TAB, "backspace": KEY_BACKSPACE,
              "minus": KEY_MINUS, "equal": KEY_EQUAL, "comma": KEY_COMMA, "period": KEY_PERIOD,
              "slash": KEY_SLASH, "bracket_left": KEY_LEFT_BRACKET, "bracket_right": KEY_RIGHT_BRACKET}

    def _ecol(name):
        return _EPAL.get(name, TXT)

    def _ekey(name):
        name = str(name).lower()
        if name in _EKEYS:
            return _EKEYS[name]
        if len(name) == 1 and (name.isalpha() or name.isdigit()):
            return ord(name.upper())    # raylib letter/digit keycodes == ASCII
        return None

    _extra_tex = {}      # (extra id, image name) -> (pixel buffer it was made from, Texture)

    def _make_extra_bridge(meta):
        import types as _types
        b = _types.SimpleNamespace()

        def _bi(name, w, h, rgba, x, y, scale, alpha):
            # Raw RGBA only, w*h*4 bytes, already size-checked by the caller: nothing
            # here parses a file. One small texture per image, rebuilt if the pixels change.
            key = (meta.id, name)
            ent = _extra_tex.get(key)
            if ent is None or ent[0] is not rgba:
                if len(rgba) != w * h * 4 or not (0 < w <= 512 and 0 < h <= 512):
                    return
                if ent is not None:
                    unload_texture(ent[1])
                if len(_extra_tex) >= 32:                 # bound GPU use across many extras
                    for _k in list(_extra_tex):
                        unload_texture(_extra_tex.pop(_k)[1])
                import pyray as _pr
                _buf = _pr.ffi.new("unsigned char[]", bytes(rgba))
                _tex = load_texture_from_image(Image(_buf, w, h, 1, 7))    # 7 = UNCOMPRESSED_R8G8B8A8
                set_texture_filter(_tex, TEXTURE_FILTER_POINT)
                ent = _extra_tex[key] = (rgba, _tex)
            draw_texture_ex(ent[1], Vector2(_extra_origin[0] + x, _extra_origin[1] + y), 0.0,
                            float(max(1, min(8, int(scale)))), C(255, 255, 255, max(0, min(255, int(alpha)))))

        def _bt(s, x, y, size, color, bold):
            text(monoB if bold else mono, str(s), _extra_origin[0] + x, _extra_origin[1] + y,
                 max(6, min(80, size)), _ecol(color))

        def _br(x, y, w, h, color, fill, alpha):
            cc = _ecol(color); c = C(cc.r, cc.g, cc.b, max(0, min(255, int(alpha))))
            if fill:
                draw_rectangle(int(_extra_origin[0] + x), int(_extra_origin[1] + y), int(w), int(h), c)
            else:
                draw_rectangle_lines_ex(Rectangle(_extra_origin[0] + x, _extra_origin[1] + y, w, h), 1, c)

        def _bl(x1, y1, x2, y2, color, thick):
            draw_line_ex(Vector2(_extra_origin[0] + x1, _extra_origin[1] + y1),
                         Vector2(_extra_origin[0] + x2, _extra_origin[1] + y2), max(1.0, float(thick)), _ecol(color))

        def _bc(x, y, r, color):
            draw_circle(int(_extra_origin[0] + x), int(_extra_origin[1] + y), float(r), _ecol(color))

        def _bgps():
            st = bk.state
            if not (st and getattr(st, "gps_fix_valid", False)):
                return None
            return {"valid": True,
                    "lat": float(getattr(st, "gps_latitude", 0.0) or 0.0),
                    "lon": float(getattr(st, "gps_longitude", 0.0) or 0.0),
                    "sats": int(getattr(st, "gps_satellites", 0) or 0),
                    "speed_kmh": float(getattr(st, "gps_speed_kmh", 0.0) or 0.0),
                    "heading": float(getattr(st, "gps_heading", 0.0) or 0.0)}

        b.draw_text = _bt
        b.draw_rect = _br
        b.draw_line = _bl
        b.draw_circle = _bc
        b.draw_image = _bi
        b.measure = lambda s, size=16: tw(mono, str(s), max(6, min(80, size)))
        b.key_down = lambda name: (lambda k: bool(k is not None and is_key_down(k)))(_ekey(name))
        b.key_pressed = lambda name: (lambda k: bool(k is not None and is_key_pressed(k)))(_ekey(name))
        b.view = lambda: (_extra_view[0], _extra_view[1])
        # key names an extra may query — shipped to the sandbox as a per-frame snapshot
        b.key_names = (list(_EKEYS.keys()) + [chr(_c) for _c in range(ord("a"), ord("z") + 1)]
                       + [str(_d) for _d in range(10)])
        b.gps = _bgps
        b.loot_networks = lambda: [{"ssid": getattr(n, "ssid", ""), "bssid": getattr(n, "bssid", ""),
                                    "channel": getattr(n, "channel", 0)} for n in (bk.networks() or [])]
        b.loot_counts = lambda: {"networks": len(bk.networks() or [])}
        b.log = lambda msg, level="info": bk._term(
            "[%s] %s" % (meta.id, msg),
            {"info": "raw", "ok": "ok", "warn": "warn", "error": "err"}.get(level, "raw"))

        def _bchars():
            out = []
            c = get_char_pressed()
            while c > 0:
                if 32 <= c < 127:
                    out.append(chr(c))
                c = get_char_pressed()
            return out
        b.chars = _bchars

        def _bloot_save(name, text):
            # permission "loot_write": write ONLY into loot/extras/<id>/ — a per-extra
            # namespace inside the loot tree, never a core loot file.
            import re as _re
            try:
                base = os.path.join(bk._app_dir, "loot", "extras", meta.id)
                os.makedirs(base, exist_ok=True)
                safe = _re.sub(r"[^A-Za-z0-9._ -]", "_", str(name)).strip(" ._") or "untitled"
                safe = os.path.basename(safe)
                if "." not in safe:
                    safe += ".txt"
                p = os.path.join(base, safe)
                with open(p, "w", encoding="utf-8") as f:
                    f.write(str(text))
                return p
            except Exception as _exc:
                bk._term("[%s] loot save failed: %s" % (meta.id, _exc), "warn")
                return None
        b.loot_save = _bloot_save

        b.release_all = lambda: None
        return b

    while not window_should_close():
        t = get_time()
        # menu-item dwell timer: reset while the menu is closed so the tooltip only
        # appears after the user actually settles on an item (TIP_DELAY seconds)
        if not menu_open:
            tip_last = (-2, -2); tip_since = t
        if fs_dbg and t - fs_dbg_t > 1.0:
            fs_dbg_t = t
            print("[FSDBG] bl=%s fs=%s w=%d h=%d" % (
                is_window_state(FLAG_BORDERLESS_WINDOWED_MODE), is_window_fullscreen(),
                get_screen_width(), get_screen_height()), flush=True)
        bk.poll()
        _cue = bk.consume_notify()        # play a notification sound (e.g. MeshCore msg)
        if _cue and not bk.is_muted():
            _snd = sounds.get(_cue)
            if _snd is not None:
                try: play_sound(_snd)
                except Exception: pass
        if bk.consume_restart():          # OTA install finished -> relaunch to apply
            bk.close(); close_window()
            os.execv(sys.executable, [sys.executable] + sys.argv)
        # a scan truly FINISHED (WiFi "Scan results printed" / BLE "Summary:") -> the
        # backend has the full list by now, so pop the ordered summary modal immediately.
        if bk.consume_scan_done():
            if not bk.is_wardriving():        # continuous wardrive: keep sweeping, no modal
                scanning = False
                running_keys.discard("wifi_scan"); running_keys.discard("wardriving")
                if pending_scan:
                    scan_modal = pending_scan; scan_modal_scroll = 0
                    pending_scan = None; menu_open = False
        # safety net for a firmware that never signals completion: open late, only if
        # results actually arrived (never an empty modal)
        if pending_scan and t - pending_scan_t > 14.0:
            _have = bool(bk.networks()) if pending_scan == "wifi" else bool(bk.ble_devices or bk.airtags)
            if _have:
                scan_modal = pending_scan; scan_modal_scroll = 0; menu_open = False
            pending_scan = None
        typing = bool(msg_screen or input_mode)   # suppress single-key shortcuts while typing
        if not typing and not menu_open and not wl_screen and not flash_screen and not mc_region_screen and not comp_screen and not scan_modal and not confirm_quit:
            if is_key_pressed(KEY_G): field = not field
            if is_key_pressed(KEY_F): toggle_borderless_windowed()   # manual (dev/non-labwc)
            if is_key_pressed(KEY_H) and bk.sdr_on() and \
               ("keyhunt" in bk.sdr_mode().lower() or "grab433" in bk.sdr_mode().lower()):
                hunt_full = not hunt_full   # expand/shrink the 433 Hunter / Capture panel
            if bk.sdr_on() and "keyhunt" in bk.sdr_mode().lower() and not loot_screen:
                if is_key_pressed(KEY_K): bk.keyhunt_learn()          # learn the next remote pressed
                if is_key_pressed(KEY_J): bk.keyhunt_reset_target()   # back to the car key
            # [C] cluster nav is handled in the map render block (needs cluster geometry)
            if is_key_pressed(KEY_S):          # S = definitive STOP (firmware + wardrive + attacks + cluster)
                bk.stop_attacks(); bk.set_wardrive(None); bk.set_mode("idle")
                bk.stop_cluster()              # tears down the ENOW bridge/brain too
                running_keys.clear(); scanning = False
                bk.set_status("STOP - all scanning/attacks/cluster halted")
            if is_key_pressed(KEY_L) or is_key_pressed(KEY_GRAVE): loot_screen = not loot_screen
            if is_key_pressed(KEY_P): data_screen = not data_screen
            if is_key_pressed(KEY_U):    # open the game-update (OTA) screen
                comp_screen = "gameupd"; comp_sel = 0; comp_scroll = 0; bk.check_game_update()
            # map zoom
            # multiplicative zoom: fine steps up close, but can pull all the way out
            # to a whole-world view where the z0-6 offline basemap shows real detail
            if is_key_pressed(KEY_EQUAL) or is_key_pressed(KEY_KP_ADD): zoom = min(1.9, zoom * 1.5)   # cap zoom-in at Block
            if is_key_pressed(KEY_MINUS) or is_key_pressed(KEY_KP_SUBTRACT): zoom = max(0.0004, zoom / 1.5)  # zoom-out floor; basemap clamp stops it at the map edge
            if is_key_pressed(KEY_ZERO) or is_key_pressed(KEY_KP_0):
                zoom = 1.0; map_pan[0] = map_pan[1] = 0.0          # recenter on YOU + reset zoom
            # D-pad pans the map to explore away from your position (0 recenters).
            # Only on the bare map — loot/cluster/data screens use the arrows themselves.
            if not loot_screen and not cluster_screen and not data_screen and not prox_screen:
                _ps = 12.0 / (9000.0 * max(0.0004, zoom))          # ~12 px/frame, in degrees (screen-constant)
                if is_key_down(KEY_UP):    map_pan[0] += _ps
                if is_key_down(KEY_DOWN):  map_pan[0] -= _ps
                if is_key_down(KEY_LEFT):  map_pan[1] -= _ps / max(0.2, _last_coslat)
                if is_key_down(KEY_RIGHT): map_pan[1] += _ps / max(0.2, _last_coslat)
                map_pan[0] = max(-85.0, min(85.0, map_pan[0]))
                map_pan[1] = max(-180.0, min(180.0, map_pan[1]))
        if prox_screen and is_key_pressed(KEY_O): bk.prox_toggle()
        if cluster_screen and not confirm_quit:    # cluster fleet screen
            if is_key_pressed(KEY_X):               # stop the fleet core (and close)
                bk.stop_cluster(); cluster_screen = False
            if is_key_pressed(KEY_A):               # adopt the first pending joiner
                _cj = bk.cluster_joins()
                if _cj: bk.cluster_accept(_cj[0])
            _fn = bk.cluster_nodes()                # select a node + pin its scan band
            if _fn:
                fleet_sel = max(0, min(fleet_sel, len(_fn) - 1))
                if is_key_pressed(KEY_DOWN): fleet_sel = min(len(_fn) - 1, fleet_sel + 1)
                if is_key_pressed(KEY_UP):   fleet_sel = max(0, fleet_sel - 1)
                # LEFT/RIGHT cycle the selected node's manual channel band; AUTO hands
                # it back to the brain's auto split.
                if is_key_pressed(KEY_RIGHT): bk.cluster_cycle_band(_fn[fleet_sel].get("mac"), +1)
                if is_key_pressed(KEY_LEFT):  bk.cluster_cycle_band(_fn[fleet_sel].get("mac"), -1)
        if (loot_screen and is_key_pressed(KEY_D) and loot_detail is None
                and loot_preview is None and confirm_del is None):
            data_screen = True   # password history
        if loot_screen and not confirm_quit and confirm_del is None:  # loot nav (3 levels)
            if loot_adsb is not None:                                 # ADS-B tracks map
                _ids = sorted(loot_adsb["tracks"], key=lambda k: -len(loot_adsb["tracks"][k]["pts"]))
                if _ids:
                    if is_key_pressed(KEY_DOWN): loot_adsb_sel = min(len(_ids) - 1, loot_adsb_sel + 1)
                    if is_key_pressed(KEY_UP):   loot_adsb_sel = max(-1, loot_adsb_sel - 1)
                if is_key_pressed(KEY_C): loot_adsb_cov = not loot_adsb_cov
                if is_key_pressed(KEY_K) and loot_detail is not None:
                    _kp = bk.adsb_export_kml(loot_detail["name"], loot_adsb)
                    loot_adsb_msg = ("KML saved: " + _kp) if _kp else "KML export failed - see the terminal"
                    loot_detail_files = bk.session_files(loot_detail["name"])
            elif loot_preview is not None:                            # L3: file preview
                _plen = len(loot_preview.get("lines", []))
                if is_key_pressed(KEY_DOWN): loot_preview_scroll += 1
                if is_key_pressed(KEY_UP):   loot_preview_scroll = max(0, loot_preview_scroll - 1)
                if is_key_pressed(KEY_PAGE_DOWN): loot_preview_scroll += 20
                if is_key_pressed(KEY_PAGE_UP):   loot_preview_scroll = max(0, loot_preview_scroll - 20)
            elif loot_detail is not None:                            # L2: session file list
                if is_key_pressed(KEY_X):        # delete THIS session (asks first)
                    confirm_del = loot_detail["name"]
                if is_key_pressed(KEY_A) and loot_detail["counts"].get("adsb"):
                    loot_adsb = bk.adsb_tracks(loot_detail["name"])
                    loot_adsb_sel, loot_adsb_msg = -1, ""
                if loot_detail_files:
                    if is_key_pressed(KEY_DOWN): loot_detail_sel = min(len(loot_detail_files) - 1, loot_detail_sel + 1)
                    if is_key_pressed(KEY_UP):   loot_detail_sel = max(0, loot_detail_sel - 1)
                    if is_key_pressed(KEY_ENTER) and 0 <= loot_detail_sel < len(loot_detail_files):
                        loot_preview = bk.file_preview(loot_detail["name"],
                                                       loot_detail_files[loot_detail_sel]["name"])
                        loot_preview_scroll = 0
            else:                                                    # L1: session journal
                _slist = bk.sessions() or []
                if _slist:
                    if is_key_pressed(KEY_DOWN): loot_log_sel = min(len(_slist) - 1, loot_log_sel + 1)
                    if is_key_pressed(KEY_UP):   loot_log_sel = max(0, loot_log_sel - 1)
                    if is_key_pressed(KEY_X) and 0 <= loot_log_sel < len(_slist):
                        confirm_del = _slist[loot_log_sel]["name"]    # delete selected (asks first)
                    if is_key_pressed(KEY_ENTER) and 0 <= loot_log_sel < len(_slist):
                        loot_detail = _slist[loot_log_sel]
                        loot_detail_files = bk.session_files(loot_detail["name"])
                        loot_detail_sel = 0; loot_detail_scroll = 0
        if radio_screen and not confirm_quit:      # radio matrix keys
            _rr = bk.radio_rows()
            if _rr:
                radio_sel = max(0, min(radio_sel, len(_rr) - 1))
                if is_key_pressed(KEY_DOWN): radio_sel = (radio_sel + 1) % len(_rr)
                if is_key_pressed(KEY_UP):   radio_sel = (radio_sel - 1) % len(_rr)
                if (is_key_pressed(KEY_LEFT) or is_key_pressed(KEY_RIGHT)
                        or is_key_pressed(KEY_ENTER) or is_key_pressed(KEY_SPACE)):
                    bk.radio_cycle(_rr[radio_sel][0])
            if is_key_pressed(KEY_R): bk.radio_detect_refresh()
        if wl_screen and not confirm_quit:         # whitelist editor keys
            wl_e = bk.wl_entries()
            if wl_scan:
                wl_n = bk.networks()
                if is_key_pressed(KEY_DOWN): wl_scan_sel = min(max(0, len(wl_n) - 1), wl_scan_sel + 1)
                if is_key_pressed(KEY_UP):   wl_scan_sel = max(0, wl_scan_sel - 1)
                if is_key_pressed(KEY_ENTER) and 0 <= wl_scan_sel < len(wl_n):
                    nn = wl_n[wl_scan_sel]; bk.wl_add("wifi", nn.bssid, nn.ssid or "<hidden>")
            else:
                if is_key_pressed(KEY_DOWN) and wl_e: wl_sel = min(len(wl_e) - 1, wl_sel + 1)
                if is_key_pressed(KEY_UP):   wl_sel = max(0, wl_sel - 1)
                if is_key_pressed(KEY_D) and wl_e:
                    bk.wl_remove(wl_sel); wl_sel = max(0, min(wl_sel, len(bk.wl_entries()) - 1))
                if is_key_pressed(KEY_S) or is_key_pressed(KEY_A):
                    wl_scan = True; wl_scan_sel = 0
        if mc_region_screen and not confirm_quit:  # MeshCore region picker keys
            regs = bk.mc_regions()
            if is_key_pressed(KEY_DOWN): mc_region_sel = min(len(regs) - 1, mc_region_sel + 1)
            if is_key_pressed(KEY_UP):   mc_region_sel = max(0, mc_region_sel - 1)
            if is_key_pressed(KEY_ENTER) and 0 <= mc_region_sel < len(regs):
                bk.mc_set_region(regs[mc_region_sel][0]); mc_region_screen = False
        if mesh_screen and not confirm_quit and mesh_name_edit:   # editing owner name
            _ch = get_char_pressed()
            while _ch > 0:
                if 32 <= _ch < 127 and len(mesh_name_buf) < 39: mesh_name_buf += chr(_ch)
                _ch = get_char_pressed()
            if is_key_pressed(KEY_BACKSPACE): mesh_name_buf = mesh_name_buf[:-1]
            if is_key_pressed(KEY_ENTER):
                bk.mt_set_name(mesh_name_buf.strip()); mesh_name_edit = False
        elif mesh_screen and not confirm_quit:      # LoRa Mesh picker keys
            _mt = (mesh_pick == "meshtastic")
            _rows = ["stack", "region"] + (["preset", "name"] if _mt else []) + ["start"]
            mesh_sel = max(0, min(mesh_sel, len(_rows) - 1))
            if is_key_pressed(KEY_DOWN): mesh_sel = min(len(_rows) - 1, mesh_sel + 1)
            if is_key_pressed(KEY_UP):   mesh_sel = max(0, mesh_sel - 1)
            _cur = _rows[mesh_sel]
            _step = 1 if is_key_pressed(KEY_RIGHT) else (-1 if is_key_pressed(KEY_LEFT) else 0)
            if _cur == "stack" and _step:
                mesh_pick = "meshcore" if mesh_pick == "meshtastic" else "meshtastic"
            elif _cur == "region" and _step:
                _ks = [r[0] for r in (bk.mt_regions() if _mt else bk.mc_regions())]
                _ck = bk.mt_region_current() if _mt else bk.mc_region_current()
                _i = (_ks.index(_ck) if _ck in _ks else 0)
                _nk = _ks[(_i + _step) % len(_ks)]
                (bk.mt_set_region if _mt else bk.mc_set_region)(_nk)
            elif _cur == "preset" and _step:
                _ks = [r[0] for r in bk.mt_presets()]; _ck = bk.mt_preset_current()
                _i = (_ks.index(_ck) if _ck in _ks else 0)
                bk.mt_set_preset(_ks[(_i + _step) % len(_ks)])
            elif _cur == "name" and is_key_pressed(KEY_ENTER):
                mesh_name_edit = True; mesh_name_buf = bk.mt_name() or ""
            elif _cur == "start" and is_key_pressed(KEY_ENTER):
                bk.start_mesh(mesh_pick)
                mesh_screen = False
                msg_screen = "mesh"; msg_buf = ""; msg_scroll = 0; mc_contacts_panel = False
        if comp_screen and not confirm_quit:       # companion screen keys
            fmenu = [("SubGHz Scan 433 MHz", lambda: bk.flipper_subghz(433920000)),
                     ("SubGHz Scan 868 MHz", lambda: bk.flipper_subghz(868350000)),
                     ("SubGHz RAW 433",      lambda: bk.flipper_subghz(433920000, True)),
                     ("Device Info",         lambda: bk.flipper_send("info device")),
                     ("NFC Detect",          lambda: bk.flipper_send("nfc detect"))]
            smenu = [("Read / Scan",     lambda: bk.subghz_cmd("scan")),
                     ("Record RAW",      lambda: bk.subghz_cmd("rx")),
                     ("Freq Analyzer",   lambda: bk.subghz_cmd("analyzer")),
                     ("Save to SD",      lambda: bk.subghz_cmd("save")),
                     ("Status",          lambda: bk.subghz_cmd("status")),
                     ("Stop",            lambda: bk.subghz_cmd("stop"))]
            wmenu = [("Status", "status"), ("Compass", "compass"), ("Haptic", "haptic"),
                     ("WiFi Recon", "wifi_scan"), ("BLE Recon", "ble_scan"),
                     ("NFC Scan", "nfc_scan"), ("LoRa Start", "lora_start"), ("Disconnect", "_disc")]
            if comp_screen == "flipper":
                if is_key_pressed(KEY_DOWN): comp_sel = min(len(fmenu) - 1, comp_sel + 1)
                if is_key_pressed(KEY_UP):   comp_sel = max(0, comp_sel - 1)
                if is_key_pressed(KEY_ENTER): fmenu[comp_sel][1]()
                if is_key_pressed(KEY_X): bk.flipper_stop()
            elif comp_screen == "subghz":
                if is_key_pressed(KEY_DOWN): comp_sel = min(len(smenu) - 1, comp_sel + 1)
                if is_key_pressed(KEY_UP):   comp_sel = max(0, comp_sel - 1)
                if is_key_pressed(KEY_ENTER): smenu[comp_sel][1]()
                if is_key_pressed(KEY_X): bk.subghz_cmd("stop")
            elif comp_screen == "mitm":
                ifs = bk.mitm_ifaces()
                if is_key_pressed(KEY_DOWN): comp_sel = min(max(0, len(ifs) - 1), comp_sel + 1)
                if is_key_pressed(KEY_UP):   comp_sel = max(0, comp_sel - 1)
                if is_key_pressed(KEY_S) and ifs and not bk.mitm_running():
                    bk.mitm_start(ifs[comp_sel][0])
                if is_key_pressed(KEY_X): bk.mitm_stop()
            elif comp_screen == "watch":
                if bk.watch_pin_requested():
                    c = get_char_pressed()
                    while c > 0:
                        if 48 <= c <= 57 and len(watch_pin) < 6: watch_pin += chr(c)
                        c = get_char_pressed()
                    if is_key_pressed(KEY_BACKSPACE): watch_pin = watch_pin[:-1]
                    if is_key_pressed(KEY_ENTER) and watch_pin:
                        bk.watch_provide_pin(int(watch_pin)); watch_pin = ""
                elif bk.watch_connected():
                    if is_key_pressed(KEY_DOWN): comp_sel = min(len(wmenu) - 1, comp_sel + 1)
                    if is_key_pressed(KEY_UP):   comp_sel = max(0, comp_sel - 1)
                    if is_key_pressed(KEY_ENTER):
                        wc = wmenu[comp_sel][1]
                        if wc == "_disc": bk._mgrs.get("watch") and bk._mgrs["watch"].disconnect()
                        else: bk.watch_send(wc)
                else:
                    res = bk.watch_results()
                    if is_key_pressed(KEY_R): bk.watch_scan()
                    if is_key_pressed(KEY_DOWN): comp_sel = min(max(0, len(res) - 1), comp_sel + 1)
                    if is_key_pressed(KEY_UP):   comp_sel = max(0, comp_sel - 1)
                    if is_key_pressed(KEY_ENTER) and 0 <= comp_sel < len(res):
                        addr = res[comp_sel][0] if isinstance(res[comp_sel], (list, tuple)) else res[comp_sel].get("address", "")
                        bk.watch_connect(addr)
            elif comp_screen == "gameupd":
                gl = bk.game_update_info()
                if is_key_pressed(KEY_ENTER) and not gl[1]:
                    if bk.game_update_available(): bk.game_update_run()
                    else: bk.check_game_update()
        if flash_screen and not confirm_quit:      # flash board picker keys
            fboards = bk.flash_boards()
            if not bk.flash_busy():
                if is_key_pressed(KEY_DOWN): flash_sel = min(len(fboards) - 1, flash_sel + 1)
                if is_key_pressed(KEY_UP):   flash_sel = max(0, flash_sel - 1)
                if is_key_pressed(KEY_ENTER) and 0 <= flash_sel < len(fboards):
                    bk.flash_start(fboards[flash_sel][0])
        if is_key_pressed(KEY_TAB) and not et_screen and not msg_screen and not data_screen: menu_open = not menu_open
        if cluster_drill and not confirm_quit and not menu_open:
            _dp = clusters[cluster_sel]["points"] if 0 <= cluster_sel < len(clusters) else []
            if is_key_pressed(KEY_DOWN): cluster_scroll = min(max(0, len(_dp) - 1), cluster_scroll + 1)
            if is_key_pressed(KEY_UP):   cluster_scroll = max(0, cluster_scroll - 1)
        if data_screen and not confirm_quit:
            if is_key_pressed(KEY_TAB) or is_key_pressed(KEY_LEFT) or is_key_pressed(KEY_RIGHT):
                data_tab = 1 - data_tab; data_scroll = 0      # switch Evil Twin <-> WPA-sec
            if is_key_pressed(KEY_DOWN): data_scroll += 1
            if is_key_pressed(KEY_UP):   data_scroll = max(0, data_scroll - 1)
        if scan_modal and not confirm_quit:
            if is_key_pressed(KEY_DOWN): scan_modal_scroll += 1
            if is_key_pressed(KEY_UP):   scan_modal_scroll = max(0, scan_modal_scroll - 1)
        if is_key_pressed(KEY_ESCAPE):
            if confirm_del is not None:       # cancel a pending session delete first
                confirm_del = None
            elif scan_modal:
                if scan_modal == "airtag":   # fully end the continuous AirTag scan
                    bk.send("stop"); bk.set_mode("idle")
                    scanning = False; running_keys.discard("bt_airtag")
                    bk.set_status("AirTag scan stopped")
                scan_modal = None
            elif comp_screen:
                if comp_screen == "watch":   # host-BT done -> give hci0 back to proximity
                    bk.prox_resume()
                if comp_screen == "extra" and active_extra is not None:
                    _hub_changed = bool(getattr(active_extra, "changed", False))
                    try:
                        active_extra.close()
                    except Exception:
                        pass
                    active_extra = None
                    if _hub_changed:      # the hub installed/removed something -> refresh EXTRAS
                        dispatch(("r", "Rescan", "_extra_rescan", "_extra_rescan", None))
                comp_screen = None
            elif mesh_screen:
                if mesh_name_edit: mesh_name_edit = False
                else: mesh_screen = False
            elif mc_region_screen:
                mc_region_screen = False
            elif data_screen:
                data_screen = False
            elif flash_screen and not bk.flash_busy():
                flash_screen = False
            elif radio_screen:
                radio_screen = False
            elif wl_screen:
                if wl_scan: wl_scan = False
                else: wl_screen = False
            elif cluster_drill:
                cluster_drill = False
            elif cluster_sel >= 0:
                cluster_sel = -1
            elif prox_screen:
                prox_screen = False
            elif cluster_screen:
                cluster_screen = False
                if bk.cluster_active():
                    bk.set_status("fleet still running in background - SNIFF > Cluster Wardrive to reopen, [X] or S to stop")
            elif pair_screen:
                pair_screen = False; bk.pair_cancel()
            elif loot_adsb is not None:
                loot_adsb = None
            elif loot_preview is not None:
                loot_preview = None
            elif loot_detail is not None:
                loot_detail = None
            elif loot_screen:
                loot_screen = False
            elif msg_screen:
                msg_screen = None; bk.set_status("")
            elif et_screen == "portal" and et_mode == "twin":
                et_screen = "net"                       # back to the net picker
            elif et_screen:
                et_screen = None; et_pending = False; bk.set_status("cancelled")
            elif confirm_restore: confirm_restore = False
            elif confirm_quit: confirm_quit = False
            elif input_mode: input_mode = None; input_item = None; input_buf = ""
            elif menu_open: menu_open = False
            else:
                confirm_quit = True
                quit_modules = bk.active_modules()          # snapshot for the prompt
                _qe = bk.entry_rails(); _qc = bk.rail_status()
                quit_can_restore = bool(_qe and _qc and
                    any(_qc.get(k) != _qe.get(k) for k in ("gps", "lora", "sdr")))
        if confirm_quit:
            if bk.flash_busy():
                if is_key_pressed(KEY_Y) or is_key_pressed(KEY_L) or is_key_pressed(KEY_R):
                    bk.set_status("flashing ESP32 - cannot quit until it finishes")
                if is_key_pressed(KEY_N): confirm_quit = False
            else:
                if is_key_pressed(KEY_Y):                    # turn modules off, then quit
                    quit_mode = "off"; break
                if quit_modules and is_key_pressed(KEY_L):   # leave modules running, quit
                    quit_mode = "leave"; break
                if quit_can_restore and is_key_pressed(KEY_R):  # restore pre-launch, quit
                    quit_mode = "restore"; break
                if is_key_pressed(KEY_N): confirm_quit = False
        if confirm_restore:
            if is_key_pressed(KEY_Y): confirm_restore = False; bk.import_userdata()
            if is_key_pressed(KEY_N): confirm_restore = False
        if confirm_del is not None:
            if is_key_pressed(KEY_Y):
                ok, msg = bk.delete_session(confirm_del)
                bk.set_status(msg)
                if ok and loot_detail is not None and loot_detail.get("name") == confirm_del:
                    loot_detail = None; loot_detail_files = []
                confirm_del = None
                _ss = bk.sessions() or []
                loot_log_sel = max(0, min(loot_log_sel, len(_ss) - 1))
            if is_key_pressed(KEY_N): confirm_del = None
        if dep_prompt:
            if is_key_pressed(KEY_Y):
                _dp_item, _dp_kind = dep_prompt[0], dep_prompt[1]
                dep_prompt = None
                bk.enable_module(_dp_kind)
                dispatch(_dp_item)          # module now powered -> open the addon
            if is_key_pressed(KEY_N):
                dep_prompt = None
        items = TABS[tab][2]

        # Evil Twin: auto-open the network picker once a scan returns targets
        if et_pending and bk.networks():
            et_pending = False; et_screen = "net"; et_sel = 0; et_selected = set()
            scanning = False; bk.set_status("pick target network(s)")

        if msg_screen and not confirm_quit:
            ch = get_char_pressed()
            while ch > 0:
                if 32 <= ch < 127 and len(msg_buf) < 120:
                    msg_buf += chr(ch)
                ch = get_char_pressed()
            if is_key_pressed(KEY_BACKSPACE): msg_buf = msg_buf[:-1]
            if is_key_pressed(KEY_UP):   msg_scroll += 1
            if is_key_pressed(KEY_DOWN): msg_scroll = max(0, msg_scroll - 1)
            if msg_screen in ("meshcore", "mesh") and is_key_pressed(KEY_TAB):
                mc_contacts_panel = not mc_contacts_panel   # nodes/contacts panel
            if msg_screen == "mesh" and (is_key_pressed(KEY_LEFT) or is_key_pressed(KEY_RIGHT)):
                _mstep = 1 if is_key_pressed(KEY_RIGHT) else -1   # quick band switch
                _mmt = bk.mesh_stack() == "meshtastic"
                _mks = [r[0] for r in (bk.mt_regions() if _mmt else bk.mc_regions())]
                _mck = bk.mt_region_current() if _mmt else bk.mc_region_current()
                if _mks:
                    _mi = (_mks.index(_mck) if _mck in _mks else 0)
                    (bk.mt_set_region if _mmt else bk.mc_set_region)(
                        _mks[(_mi + _mstep) % len(_mks)])
            if (msg_screen == "mesh"
                    and (is_key_down(KEY_LEFT_CONTROL) or is_key_down(KEY_RIGHT_CONTROL))
                    and is_key_pressed(KEY_T)):        # quick stack toggle (shared SX1262)
                _other = "meshcore" if bk.mesh_stack() == "meshtastic" else "meshtastic"
                if _other == "meshtastic" and not bk.mt_available():
                    bk.set_status("Meshtastic niedostepny (brak meshtasticd)")
                else:
                    bk.start_mesh(_other)              # arbiter releases SPI + restarts
                    mc_contacts_panel = False; msg_scroll = 0
            if is_key_pressed(KEY_ENTER) and msg_buf.strip():
                if msg_screen == "mesh":
                    (bk.mt_send if bk.mesh_stack() == "meshtastic"
                     else bk.mc_send)(msg_buf.strip())
                elif msg_screen == "meshcore": bk.mc_send(msg_buf.strip())
                else: bk.wd_send(msg_buf.strip())
                msg_buf = ""; msg_scroll = 0
        elif et_screen == "net" and not confirm_quit:
            enets = bk.networks()
            if is_key_pressed(KEY_UP):   et_sel = max(0, et_sel - 1)
            if is_key_pressed(KEY_DOWN): et_sel = min(max(0, len(enets) - 1), et_sel + 1)
            if is_key_pressed(KEY_SPACE) and enets:
                if et_sel in et_selected: et_selected.discard(et_sel)
                else: et_selected.add(et_sel)
            if is_key_pressed(KEY_ENTER) and enets:
                portal_list = bk.get_portals(); portal_sel = 0; et_screen = "portal"
        elif et_screen == "portal" and not confirm_quit:
            if is_key_pressed(KEY_UP):   portal_sel = max(0, portal_sel - 1)
            if is_key_pressed(KEY_DOWN): portal_sel = min(max(0, len(portal_list) - 1), portal_sel + 1)
            if is_key_pressed(KEY_ENTER) and portal_list:
                pname, phtml = portal_list[portal_sel]
                if et_mode == "twin":
                    enets = bk.networks()
                    selrows = sorted(et_selected) if et_selected else [et_sel]
                    selrows = [i for i in selrows if i < len(enets)]
                    args = " ".join((getattr(enets[i], "index", "") or str(i)) for i in selrows)
                    essid = enets[selrows[0]].ssid if selrows else "<hidden>"
                    bk.run_evil_twin(args, phtml, essid or "<hidden>")
                    running_keys.add("evil_twin")
                else:
                    bk.run_evil_portal("FreeWiFi", phtml)
                    running_keys.add("portal")
                et_screen = None; menu_open = False; scanning = True
        elif input_mode and not confirm_quit:      # capturing a target (bssid/ch or mac)
            ch = get_char_pressed()
            while ch > 0:
                if 32 <= ch < 127 and len(input_buf) < 40:
                    input_buf += chr(ch)
                ch = get_char_pressed()
            if is_key_pressed(KEY_BACKSPACE): input_buf = input_buf[:-1]
            if is_key_pressed(KEY_ESCAPE): input_mode = None; input_item = None; input_buf = ""
            if is_key_pressed(KEY_ENTER) and input_buf.strip() and input_item:
                _fire(input_item[2], input_item[3], input_item[1], input_buf.strip())
                input_mode = None; input_item = None; input_buf = ""; menu_open = False
        elif menu_open and not confirm_quit:
            if is_key_pressed(KEY_RIGHT): tab = (tab + 1) % len(TABS); sel = 0
            if is_key_pressed(KEY_LEFT):  tab = (tab - 1) % len(TABS); sel = 0
            items = TABS[tab][2]
            if is_key_pressed(KEY_DOWN): sel = min(sel + 1, len(items) - 1)
            if is_key_pressed(KEY_UP):   sel = max(sel - 1, 0)
            # run an item: ENTER on the selected row, OR typing its hotkey letter/number
            run_idx = -1
            _hk = get_char_pressed()
            while _hk > 0:
                _c = chr(_hk).lower() if 32 <= _hk < 127 else ""
                for _i, _it in enumerate(items):
                    if _c and _it[0].lower() == _c:
                        run_idx = _i; break
                _hk = get_char_pressed()
            if is_key_pressed(KEY_ENTER):
                run_idx = sel
            if run_idx >= 0:
                sel = run_idx
                if items[sel][2] == "_backup_import":   # destructive -> confirm first
                    confirm_restore = True; menu_open = False
                else:
                    _icmd = items[sel][2]
                    _dep = ADDON_DEPS.get(_icmd)
                    if _dep and not bk.module_ready(_dep[0]):
                        # addon needs a hardware module that's OFF -> ask before enabling
                        dep_prompt = (items[sel], _dep[0], _dep[1], _dep[2]); menu_open = False
                    else:
                        # WiFi/BLE scans finish -> pop the results modal when done;
                        if tab == 0 and _icmd in ("scan_networks", "scan_bt"):
                            pending_scan = {"scan_networks": "wifi", "scan_bt": "ble"}[_icmd]
                            pending_scan_t = t
                        opened = dispatch(items[sel])
                        # AirTag is a CONTINUOUS scan (live counts, no end) -> open a live panel now
                        if tab == 0 and _icmd == "scan_airtag":
                            scan_modal = "airtag"; scan_modal_scroll = 0; menu_open = False
                        # keep the menu open only in SYSTEM (multi-toggle GPS/LoRa/SDR/USB…);
                        # every other tab closes the menu after running an item
                        elif ((opened or input_mode is not None or TABS[tab][0] != "SYSTEM")
                              and _icmd != "_extra_rescan"):   # Rescan keeps the menu open
                            menu_open = False
        tname, tcol, _ = TABS[tab]

        no_overlay = not (menu_open or confirm_quit or msg_screen or input_mode or et_screen
                          or loot_screen or prox_screen or wl_screen or flash_screen
                          or cluster_screen or pair_screen
                          or data_screen or mc_region_screen or comp_screen or cluster_drill
                          or mesh_screen or confirm_restore or scan_modal or dep_prompt)

        HUD, STA, TRM = 64, 38, 168
        by0, by1 = HUD, H - STA - TRM
        bh = by1 - by0
        mw = int(W * 0.63)
        mapx0 = mw if menu_open else 0
        mcx = mapx0 + int((W - mapx0) * (0.52 if menu_open else 0.5))
        mcy = by0 + int(bh * 0.5)

        # ---------- render scene into texture ----------
        begin_texture_mode(target)
        clear_background(VOID)
        draw_rectangle_gradient_v(0, 0, W, H, BG0, BG1)

        # ---------- GEOGRAPHIC MAP: loot clusters placed by GPS ----------
        lootpts = bk.loot_points()
        # drop junk GPS reads (lat -94, null island, out-of-range)
        valid = [p for p in lootpts
                 if -90 <= p.get("lat", 0) <= 90 and -180 <= p.get("lon", 0) <= 180
                 and not (abs(p.get("lat", 0)) < 1e-6 and abs(p.get("lon", 0)) < 1e-6)]
        # center on the live GPS fix, else the median of captured points (robust)
        if bk.state and bk.state.gps_fix_valid:
            clat, clon = bk.state.gps_latitude, bk.state.gps_longitude
        elif valid:
            slat = sorted(p["lat"] for p in valid); slon = sorted(p["lon"] for p in valid)
            clat = slat[len(slat) // 2]; clon = slon[len(slon) // 2]
        else:
            clat = clon = 0.0
        _base_lat, _base_lon = clat, clon                 # your true position (avatar marker)
        clat = max(-85.0, min(85.0, clat + map_pan[0]))   # D-pad pan shifts the VIEW center
        clon = max(-180.0, min(180.0, clon + map_pan[1]))
        coslat = math.cos(math.radians(clat)) or 1.0
        _last_coslat = coslat
        # clamp zoom-OUT so the world basemap always fills the map pane: stop the
        # moment a map edge (lon +-180 / lat +-85) reaches the matching pane edge,
        # so you can't pull out into empty void past the end of the map.
        if basemap is not None and basemap.ok:
            _cla = max(-84.9, min(84.9, clat)); _clo = max(-179.9, min(179.9, clon))
            _ms_min = max(
                (mcx - mapx0) / max(0.1, 180.0 + _clo) / coslat,   # west edge
                (W - mcx) / max(0.1, 180.0 - _clo) / coslat,       # east edge
                (mcy - by0) / max(0.1, 85.05 - _cla),              # north edge
                (by1 - mcy) / max(0.1, 85.05 + _cla),              # south edge
            )
            if zoom < _ms_min / 9000.0:
                zoom = _ms_min / 9000.0
        mscale = 9000.0 * zoom                               # screen px per degree

        def geo2scr(lat, lon):
            return (mcx + (lon - clon) * mscale * coslat, mcy - (lat - clat) * mscale)

        # rebuild clusters only when the view changes (zoom/center) or every 0.75s
        ckey = (round(zoom, 3), round(clat, 4), round(clon, 4), mapx0)
        if ckey != cl_cache_key or t - cl_cache_t > 0.75:
            cl_cache_key = ckey; cl_cache_t = t
            CPX = 34; _grid = {}; _cls = []
            for p in valid:
                sx, sy = geo2scr(p["lat"], p["lon"])
                if not (mapx0 - 40 < sx < W + 40 and by0 - 40 < sy < by1 + 40):
                    continue
                gx, gy = int(sx) // CPX, int(sy) // CPX
                merged = False
                for dx in (-1, 0, 1):
                    for dy in (-1, 0, 1):
                        for ci in _grid.get((gx + dx, gy + dy), ()):
                            cc = _cls[ci]
                            if (sx - cc["x"]) ** 2 + (sy - cc["y"]) ** 2 <= CPX * CPX:
                                n = cc["count"]
                                cc["x"] = (cc["x"] * n + sx) / (n + 1)
                                cc["y"] = (cc["y"] * n + sy) / (n + 1)
                                cc["points"].append(p); cc["count"] = n + 1
                                merged = True; break
                        if merged: break
                    if merged: break
                if not merged:
                    _cls.append({"x": sx, "y": sy, "points": [p], "count": 1})
                    _grid.setdefault((gx, gy), []).append(len(_cls) - 1)
            for cc in _cls:
                wf = sum(1 for p in cc["points"] if p.get("type") == "wifi")
                cc["color"] = SUCCESS if wf >= (cc["count"] - wf) else ICE   # wifi=green bt=cyan
                cc["radius"] = min(7 + cc["count"] // 4, 18) if cc["count"] > 1 else 3
            clusters = _cls
            if cluster_sel >= len(clusters):
                cluster_sel = -1

        # ---- ctOS offline basemap (whole-world z0-6 raster tiles from MBTiles).
        #      Ground layer UNDER the grid/territory/markers. Each tile decodes to a
        #      GPU texture ONCE and is cached + blitted every frame; new uploads are
        #      capped per frame so panning/zooming never hitches. Tiles are drawn to
        #      their own geo-corners via geo2scr, so adjacent tiles meet seamlessly. ----
        if basemap is not None and basemap.ok and not field and mscale * coslat > 0.0:
            _bz = int(round(math.log2(360.0 * mscale * coslat / 340.0)))
            _bz = max(basemap.minzoom, min(basemap.maxzoom, _bz))
            _bn = 1 << _bz
            _lonw = clon + (mapx0 - mcx) / (mscale * coslat)
            _lone = clon + (W - mcx) / (mscale * coslat)
            _latn = clat - (by0 - mcy) / mscale
            _lats = clat - (by1 - mcy) / mscale
            _tx0, _ty0 = lonlat_to_tile(_lonw, _latn, _bz)
            _tx1, _ty1 = lonlat_to_tile(_lone, _lats, _bz)
            if _tx1 < _tx0: _tx0, _tx1 = _tx1, _tx0
            if _ty1 < _ty0: _ty0, _ty1 = _ty1, _ty0
            _tx0 = max(0, _tx0); _ty0 = max(0, _ty0)
            _tx1 = min(_bn - 1, _tx1); _ty1 = min(_bn - 1, _ty1)
            if (_tx1 - _tx0 + 1) * (_ty1 - _ty0 + 1) <= 80:          # sanity cap
                begin_scissor_mode(mapx0, by0, W - mapx0, by1 - by0)
                _ups = 0; _vis = set()
                for _tx in range(_tx0, _tx1 + 1):
                    for _ty in range(_ty0, _ty1 + 1):
                        _key = (_bz, _tx, _ty); _vis.add(_key)
                        if _key not in bm_cache:
                            if _ups >= 4:                           # cap uploads/frame
                                continue
                            _data = basemap.tile(_bz, _tx, _ty)
                            if not _data:
                                bm_cache[_key] = None; continue
                            try:
                                _img = load_image_from_memory(".png", _data, len(_data))
                                _t = load_texture_from_image(_img); unload_image(_img)
                                set_texture_filter(_t, TEXTURE_FILTER_BILINEAR)
                                bm_cache[_key] = _t; _ups += 1
                            except Exception:
                                bm_cache[_key] = None; continue
                        _tex = bm_cache[_key]
                        if _tex is None:
                            continue
                        _lo0, _la0 = tile_corner(_tx, _ty, _bz)          # NW corner
                        _lo1, _la1 = tile_corner(_tx + 1, _ty + 1, _bz)  # SE corner
                        _ax, _ay = geo2scr(_la0, _lo0)
                        _bx, _by = geo2scr(_la1, _lo1)
                        _dw = int(_bx - _ax) + 1; _dh = int(_by - _ay) + 1
                        if _dw <= 0 or _dh <= 0:
                            continue
                        draw_texture_pro(_tex, Rectangle(0, 0, _tex.width, _tex.height),
                                         Rectangle(int(_ax), int(_ay), _dw, _dh),
                                         Vector2(0, 0), 0.0, WHITE)
                end_scissor_mode()
                if len(bm_cache) > 64:                              # evict off-screen
                    for _k in list(bm_cache.keys()):
                        if _k in _vis:
                            continue
                        _old = bm_cache.pop(_k)
                        if _old is not None:
                            try:
                                unload_texture(_old)
                            except Exception:
                                pass
                        if len(bm_cache) <= 64:
                            break

        # map grid backdrop
        gcol = C(27, 39, 54, 140 if not field else 80)
        for x in range(0, W, 92): draw_line(x, by0, x, by1, gcol)
        for y in range(by0, by1, 92): draw_line(0, y, W, y, gcol)

        # WDGWars territory: your team's held tiles (game-state decay_nearby) as
        # real map cells like the portal — a 0.01deg grid, ~20% team-colour fill +
        # a brighter border, drawn as a BACKDROP (under clusters/markers) and
        # clipped to the map. critical -> amber, vulnerable -> red.
        if not field:
            _tiles = bk.gs_decay()
            if _tiles:
                _tm = (bk.gs_me().get("team") or {}).get("color") or ""
                try:
                    _teamcol = (C(int(_tm[1:3], 16), int(_tm[3:5], 16), int(_tm[5:7], 16))
                                if _tm.startswith("#") and len(_tm) >= 7 else TEAL)
                except Exception:
                    _teamcol = TEAL
                begin_scissor_mode(mapx0, by0, W - mapx0, by1 - by0)
                for _dt in _tiles:
                    la = _dt.get("lat", 0.0); lo = _dt.get("lng", _dt.get("lon", 0.0))
                    ax, ay = geo2scr(la, lo)                      # SW corner
                    nx, ny = geo2scr(la + 0.01, lo + 0.01)        # NE corner
                    rx, ry = int(min(ax, nx)), int(min(ay, ny))
                    rw, rh = int(abs(nx - ax)), int(abs(ny - ay))
                    if rw < 1 or rh < 1 or rx + rw < mapx0 or rx > W or ry + rh < by0 or ry > by1:
                        continue
                    _dc = DANGER if _dt.get("vulnerable") else (WARN if _dt.get("critical") else _teamcol)
                    draw_rectangle(rx, ry, rw, rh, C(_dc.r, _dc.g, _dc.b, 48))        # ~20% fill
                    draw_rectangle_lines_ex(Rectangle(rx, ry, rw, rh), 1, C(_dc.r, _dc.g, _dc.b, 170))
                end_scissor_mode()

        # ---- cluster navigation: [C] select, arrows move, ENTER drill ----
        def nearest_cluster(dx, dy, frm):
            fx = clusters[frm]["x"] if 0 <= frm < len(clusters) else mcx
            fy = clusters[frm]["y"] if 0 <= frm < len(clusters) else mcy
            best, bd = -1, 1e18
            for i, cc in enumerate(clusters):
                if i == frm:
                    continue
                vx, vy = cc["x"] - fx, cc["y"] - fy
                if (dx or dy) and (vx * dx + vy * dy) <= 0:
                    continue                                 # wrong direction
                d = vx * vx + vy * vy
                if d < bd:
                    bd, best = d, i
            return best
        if no_overlay and clusters:
            if is_key_pressed(KEY_C):
                cluster_sel = -1 if cluster_sel >= 0 else nearest_cluster(0, 0, -1)
                cluster_drill = False
            if cluster_sel >= 0 and not cluster_drill:
                if is_key_pressed(KEY_ENTER):
                    cluster_drill = True; cluster_scroll = 0
                for _k, _dx, _dy in ((KEY_LEFT, -1, 0), (KEY_RIGHT, 1, 0),
                                     (KEY_UP, 0, -1), (KEY_DOWN, 0, 1)):
                    if is_key_pressed(_k):
                        _nn = nearest_cluster(_dx, _dy, cluster_sel)
                        if _nn >= 0:
                            cluster_sel = _nn
        # SPACE = hack the selected cluster (pwn its nodes)
        hack_tgt = -1
        if no_overlay and clusters and is_key_down(KEY_SPACE):
            hack_tgt = cluster_sel if 0 <= cluster_sel < len(clusters) else nearest_cluster(0, 0, -1)
            if hack_tgt >= 0:
                hack_prog = min(1.0, hack_prog + 1.0 / 42.0)
                if hack_prog >= 1.0:
                    for p in clusters[hack_tgt]["points"]:
                        hacked.add(p.get("label", ""))
                    bk.gain_xp(50)
                    bk.set_status("PWNED cluster (%d nodes)" % clusters[hack_tgt]["count"])
                    hack_prog = 0.0
        else:
            hack_prog = 0.0

        # ---- draw clusters (circle + count, color by type) ----
        for idx, cc in enumerate(clusters):
            x, y = int(cc["x"]), int(cc["y"]); c = cc["color"]; cl_on = (idx == cluster_sel)
            rr = cc["radius"] if cc["count"] > 1 else 5
            if cc["count"] == 1:
                if not field: halo(x, y, c, ((10, 20), (6, 28)))
                draw_circle(x, y, 3, c)
            else:
                r = cc["radius"]
                if not field: halo(x, y, c, ((r + 10, 20), (r + 4, 28)))
                draw_circle(x, y, r, C(c.r, c.g, c.b, 210))
                draw_circle_lines(x, y, r, c)
                lbl = str(cc["count"])
                text(monoB, lbl, x - tw(monoB, lbl, 15) / 2, y - 9, 15, VOID)
                pp = (t * 1.6 + idx * 0.4) % 2.0             # breathing pulse
                if pp < 0.7:
                    draw_circle_lines(x, y, int(r + 2 + pp * 10), C(c.r, c.g, c.b, int(120 * (1 - pp / 0.7))))
            if cl_on:
                draw_circle_lines(x, y, rr + 5, HI)
                if int(t * 3) % 2 == 0:
                    draw_circle_lines(x, y, rr + 8, ICE)
            if idx == hack_tgt and hack_prog > 0:
                draw_ring(Vector2(x, y), rr + 4, rr + 7, -90, -90 + 360 * hack_prog, 24, DANGER)

        # ---- live module overlays on the map (ADS-B aircraft, MeshCore nodes) ----
        def _onmap(sx, sy):
            return mapx0 - 10 < sx < W + 10 and by0 - 10 < sy < by1 + 10
        # ADS-B: when the 1090 MHz scanner is live, the map doubles as a flight-radar
        # scope centred on you — range rings, compass, a rotating sweep, and each
        # aircraft a blip with a breadcrumb trail + heading/speed vector + tag.
        _adsb_live = bk.sdr_on() and "adsb" in bk.sdr_mode().lower()
        _acs = bk.aircraft()
        if _adsb_live and not field and mapx0 < W:
            begin_scissor_mode(mapx0, by0, W - mapx0, by1 - by0)
            _rc = TEAL
            _ppk = mscale / 111.32                               # screen px per km
            _edge = max(W - mcx, mcx - mapx0, mcy - by0, by1 - mcy)
            _mkm = _edge / max(0.001, _ppk)
            _rkm = next((s for s in (1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000)
                         if _mkm / s <= 5), 2000)
            _nr = int(_mkm / _rkm) + 1
            _maxr = _nr * _rkm * _ppk
            for _ang, _lab in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):   # compass radials
                _a = math.radians(_ang)
                draw_line_ex(Vector2(mcx, mcy),
                             Vector2(mcx + math.sin(_a) * _maxr, mcy - math.cos(_a) * _maxr),
                             1, C(_rc.r, _rc.g, _rc.b, 22))
                text(mono, _lab, int(mcx + math.sin(_a) * (_maxr - 18)) - 4,
                     int(mcy - math.cos(_a) * (_maxr - 18)) - 7, 13, C(_rc.r, _rc.g, _rc.b, 120))
            for _ri in range(1, _nr + 1):                        # range rings
                _rpx = _ri * _rkm * _ppk
                draw_circle_lines(int(mcx), int(mcy), _rpx, C(_rc.r, _rc.g, _rc.b, 38))
                text(mono, "%dkm" % (_ri * _rkm), int(mcx) + 5, int(mcy - _rpx) - 7, 12,
                     C(_rc.r, _rc.g, _rc.b, 110))
            _sw = (t * 55) % 360                                 # rotating sweep + fade trail
            for _i in range(20):
                _a = math.radians(_sw - _i * 2.2)
                draw_line_ex(Vector2(mcx, mcy),
                             Vector2(mcx + math.sin(_a) * _maxr, mcy - math.cos(_a) * _maxr),
                             2, C(_rc.r, _rc.g, _rc.b, int(95 * (1 - _i / 20))))
            draw_circle(int(mcx), int(mcy), 3, _rc)              # the receiver (you)
            end_scissor_mode()
        # breadcrumb trails: remember where each aircraft has been
        _seen_ic = set()
        for ac in _acs:
            _ic = ac.get("icao") or ac.get("cs")
            _seen_ic.add(_ic)
            _tl = adsb_trails.setdefault(_ic, [])
            if not _tl or _tl[-1] != (ac["lat"], ac["lon"]):
                _tl.append((ac["lat"], ac["lon"]))
                if len(_tl) > 40:
                    _tl.pop(0)
        for _ic in list(adsb_trails):
            if _ic not in _seen_ic:
                adsb_trails.pop(_ic, None)
        # aircraft blips: altitude colour + trail + heading/speed vector + tag
        for ac in _acs:
            sx, sy = geo2scr(ac["lat"], ac["lon"])
            if not _onmap(sx, sy):
                continue
            x, y = int(sx), int(sy)
            alt = ac["alt"]
            ac_c = SUCCESS if alt < 10000 else (WARN if alt < 30000 else ICE)
            _tl = adsb_trails.get(ac.get("icao") or ac.get("cs"), [])
            for _j in range(1, len(_tl)):                        # fading breadcrumb trail
                _p0 = geo2scr(*_tl[_j - 1]); _p1 = geo2scr(*_tl[_j])
                draw_line_ex(Vector2(*_p0), Vector2(*_p1), 1,
                             C(ac_c.r, ac_c.g, ac_c.b, int(110 * _j / len(_tl))))
            if _adsb_live:                                       # heading/speed vector
                _tr = math.radians(ac.get("track", 0))
                _vl = 10 + min(46, ac.get("spd", 0) * 0.07)
                draw_line_ex(Vector2(x, y),
                             Vector2(x + math.sin(_tr) * _vl, y - math.cos(_tr) * _vl), 2, ac_c)
            draw_circle(x, y, 3, ac_c)
            draw_circle_lines(x, y, int(7 + 2 * math.sin(t * 4)), C(ac_c.r, ac_c.g, ac_c.b, 150))
            text(mono, str(ac["cs"])[:8], x + 10, y - 15, 13, ac_c)
            if _adsb_live or zoom >= 1.4:
                text(mono, "FL%03d %dkt" % (max(0, alt) // 100, ac.get("spd", 0)),
                     x + 10, y - 1, 11, C(ac_c.r, ac_c.g, ac_c.b, 210))
        # radar header readout (top-left of the map pane)
        if _adsb_live and not field and mapx0 < W:
            _hx = mapx0 + 14
            draw_circle(_hx + 4, by0 + 16, 4, C(TEAL.r, TEAL.g, TEAL.b, 150 + int(90 * math.sin(t * 3))))
            text(mono, "ADS-B RADAR", _hx + 16, by0 + 9, 16, TEAL)
            text(mono, "%d on scope / %d tracked   BRG %03d"
                 % (len(_acs), bk.aircraft_total(), int((t * 55) % 360)),
                 _hx + 16, by0 + 28, 13, C(TEAL.r, TEAL.g, TEAL.b, 180))
        # MeshCore nodes that advertised a position: node glyph + name
        for nd in bk.mc_nodes():
            sx, sy = geo2scr(nd["lat"], nd["lon"])
            if not _onmap(sx, sy):
                continue
            x, y = int(sx), int(sy)
            if not field:
                halo(x, y, PURPLE, ((8, 20), (5, 28)))
            draw_poly(Vector2(x, y), 4, 6, t * 40, PURPLE)      # diamond node
            draw_poly_lines_ex(Vector2(x, y), 4, 9, t * 40, 2, C(178, 59, 214, 150))
            text(mono, str(nd["name"])[:14], x + 11, y - 7, 13, PURPLE)
        # Meshtastic nodes: green hexagon (distinct from the MeshCore diamond)
        _mtcol = C(45, 220, 130)
        for nd in bk.mt_nodes():
            sx, sy = geo2scr(nd["lat"], nd["lon"])
            if not _onmap(sx, sy):
                continue
            x, y = int(sx), int(sy)
            if not field:
                halo(x, y, _mtcol, ((8, 20), (5, 28)))
            draw_poly(Vector2(x, y), 6, 5, t * 30, _mtcol)       # hexagon node
            draw_poly_lines_ex(Vector2(x, y), 6, 9, t * 30, 2, C(45, 220, 130, 150))
            text(mono, str(nd["name"])[:14], x + 11, y - 7, 13, _mtcol)
        # 433 MHz sensors (weather / TPMS / remotes): small blinking square + model
        for sn in bk.sensors_433():
            sx, sy = geo2scr(sn["lat"], sn["lon"])
            if not _onmap(sx, sy):
                continue
            x, y = int(sx), int(sy)
            blink = math.sin(t * 5 + (hash(str(sn["sid"])) % 100)) > 0
            sc = C(120, 220, 120) if blink else C(60, 120, 60)
            draw_rectangle(x - 2, y - 2, 4, 4, sc)
            draw_line_ex(Vector2(x, y - 2), Vector2(x, y - 6), 1, sc)     # antenna
            if zoom >= 1.9 and sn["model"]:
                text(mono, str(sn["model"])[:14], x + 7, y - 6, 12, C(120, 220, 120))

        # WDGWars "Chase the White Rabbit": portal targets as objectives on the
        # map. White = open, dim = already caught. Distance/compass are from OUR
        # GPS (backend recomputes them, not the server's area).
        for rb in bk.rabbits():
            sx, sy = geo2scr(rb["lat"], rb["lon"])
            if not _onmap(sx, sy):
                continue
            x, y = int(sx), int(sy)
            caught = rb.get("state") not in (None, "open")
            rc = C(120, 130, 140) if caught else C(240, 240, 245)
            if not caught:                                   # pulsing objective beacon
                pr = (t * 1.2) % 1.0
                draw_circle_lines(x, y, int(10 + pr * 22),
                                  C(rc.r, rc.g, rc.b, int(150 * (1 - pr))))
            draw_circle(x, y, 4, rc)                          # head
            draw_line_ex(Vector2(x - 2, y - 3), Vector2(x - 4, y - 11), 2, rc)   # ear L
            draw_line_ex(Vector2(x + 2, y - 3), Vector2(x + 4, y - 11), 2, rc)   # ear R
            lbl = "RABBIT"
            if rb.get("need"):
                lbl += " +%d" % rb["need"]
            dk = rb.get("dist_km")
            if dk is not None:
                lbl += "  %.1fkm %s" % (dk, rb.get("compass") or "")
            text(mono, lbl, x + 9, y - 7, 13, rc)

        # WPA-sec "pwned" APs: wardrive points whose BSSID is in our cracked list.
        # Gold pulsing key = we already have this network's password. Only at BLOCK
        # zoom (>=1.65) so they don't pile up / overlap on the wider views.
        if zoom >= 1.65:
            for pp in bk.pwned_points():
                sx, sy = geo2scr(pp["lat"], pp["lon"])
                if not _onmap(sx, sy):
                    continue
                x, y = int(sx), int(sy)
                gold = C(255, 196, 0)
                pr = (t * 1.1) % 1.0
                draw_circle_lines(x, y, int(8 + pr * 18),
                                  C(gold.r, gold.g, gold.b, int(170 * (1 - pr))))
                draw_circle_lines(x - 4, y, 4, gold)                              # key head
                draw_line_ex(Vector2(x, y), Vector2(x + 9, y), 2, gold)          # shaft
                draw_line_ex(Vector2(x + 9, y), Vector2(x + 9, y + 4), 2, gold)  # tooth
                draw_line_ex(Vector2(x + 6, y), Vector2(x + 6, y + 3), 2, gold)  # tooth

        # Edge pointer to the NEAREST off-screen rabbit, so a far target stays
        # navigable (bk.rabbits() is nearest-first; the first off-map open one is
        # the nearest you can't see). Clamp the ray center->target to the map
        # border and draw a pulsing arrow + distance there.
        if not field:
            for rb in bk.rabbits():
                if rb.get("state") not in (None, "open"):
                    continue
                sx, sy = geo2scr(rb["lat"], rb["lon"])
                if _onmap(sx, sy):
                    continue                        # already drawn on the map
                dx, dy = sx - mcx, sy - mcy
                if dx == 0 and dy == 0:
                    break
                m = 30
                edges = []
                if dx > 0:   edges.append((W - m - mcx) / dx)
                elif dx < 0: edges.append((mapx0 + m - mcx) / dx)
                if dy > 0:   edges.append((by1 - m - mcy) / dy)
                elif dy < 0: edges.append((by0 + m - mcy) / dy)
                f = min([c for c in edges if c > 0] or [1.0])
                ex, ey = mcx + dx * f, mcy + dy * f
                ang = math.degrees(math.atan2(dy, dx))
                pr = (t * 1.5) % 1.0
                rc = C(240, 240, 245)
                draw_circle_lines(int(ex), int(ey), int(7 + pr * 9),
                                  C(rc.r, rc.g, rc.b, int(150 * (1 - pr))))
                draw_poly(Vector2(ex, ey), 3, 12, ang, rc)           # arrow head
                draw_poly_lines_ex(Vector2(ex, ey), 3, 13, ang, 2, C(rc.r, rc.g, rc.b, 150))
                dk = rb.get("dist_km")
                if dk is not None:
                    d_lbl = ("%.0fkm %s" % (dk, rb.get("compass") or "")
                             if dk >= 1 else "%.0fm %s" % (dk * 1000, rb.get("compass") or ""))
                    lw = tw(mono, d_lbl, 13)
                    # Step the label back along the ray far enough that its box clears the
                    # arrow and its pulse ring (radius up to 16). A fixed 26 px was too little
                    # for a wide label pointing W or E: the arrow covered its first or last
                    # digits ("410km W" read as "10km W").
                    ux, uy = math.cos(math.radians(ang)), math.sin(math.radians(ang))
                    back = 22 + abs(ux) * lw / 2 + abs(uy) * 7
                    lx = min(W - lw - 6, max(mapx0 + 6, ex - ux * back - lw / 2))
                    ly = min(by1 - 16, max(by0 + 4, ey - uy * back - 7))
                    text(mono, d_lbl, lx, ly, 13, rc)
                break

        # WDGWars game-state overlays: contracts (objectives). The territory tiles
        # (decay_nearby) are drawn earlier as a map backdrop. Server uses 'lng'.
        if not field:
            for _ct in bk.gs_contracts():
                sx, sy = geo2scr(_ct.get("lat", 0), _ct.get("lng", _ct.get("lon", 0)))
                if not _onmap(sx, sy):
                    continue
                x, y = int(sx), int(sy)
                _cc = HI if _ct.get("mine") else ICE
                _rm = _ct.get("radius_m") or 0
                _rpx = int((_rm / 111000.0) * mscale) if _rm else 0
                if 4 < _rpx < 400:
                    draw_circle_lines(x, y, _rpx, C(_cc.r, _cc.g, _cc.b, 70))
                draw_poly(Vector2(x, y), 6, 5, t * 30, _cc)
                draw_poly_lines_ex(Vector2(x, y), 6, 8, t * 30, 2, C(_cc.r, _cc.g, _cc.b, 150))
                _lbl = "%s %s/%s" % (str(_ct.get("title", "CONTRACT"))[:16],
                                     _ct.get("progress", 0), _ct.get("need", 0))
                text(mono, _lbl, x + 10, y - 7, 13, _cc)

        # radar sweep while a scan is running
        if scanning and not field:
            a = (t * 110) % 360
            begin_blend_mode(BLEND_ADDITIVE)
            for i in range(14):
                al = int(42 * (1 - i / 14))
                draw_circle_sector(Vector2(mcx, mcy), 300, a - i * 3.5, a - i * 3.5 + 5, 10, C(34, 211, 219, al))
            end_blend_mode()

        # player avatar — at YOUR true position. With no D-pad pan that IS the screen
        # centre; once you pan to explore, the avatar stays put on the world and the
        # view slides, clamped to the pane edge so "you are here" never leaves the map.
        _pax, _pay = geo2scr(_base_lat, _base_lon)
        pax = int(max(mapx0 + 12, min(W - 12, _pax)))
        pay = int(max(by0 + 12, min(by1 - 12, _pay)))
        if not field: halo(pax, pay, TEAL, ((22, 24), (14, 34)))
        draw_ring(Vector2(pax, pay), 15, 17, 0, 360, 40, C(34, 211, 219, 150))  # ground locus
        if player_tex is not None:
            ph = max(42.0, min(205.0, 70.0 * zoom))        # grows as you zoom in (~+20% at CLOSE)
            pw = ph * player_tex.width / player_tex.height
            draw_texture_pro(player_tex, Rectangle(0, 0, player_tex.width, player_tex.height),
                             Rectangle(pax - pw / 2, pay - ph * 0.66, pw, ph), Vector2(0, 0), 0.0,
                             WHITE if not field else C(210, 225, 235, 255))
        else:
            draw_ring(Vector2(pax, pay), 22, 25, 0, 360, 48, C(220, 247, 255, 230))
            draw_circle(pax - 7, pay - 2, 4, TEAL); draw_circle(pax + 7, pay - 2, 4, TEAL)
            draw_rectangle(pax - 1, pay + 3, 2, 5, C(220, 247, 255, 200))
            for dx in (-6, -2, 2, 6): draw_rectangle(pax + dx - 1, pay + 9, 2, 4, C(220, 247, 255, 200))

        # ---- police lightbar (Emergency RF): left edge, between [TAB]MENU and the
        #      proximity pager. Grey while scanning; red/blue strobe on a detection. ----
        if bk.sdr_on() and "emerg" in bk.sdr_mode().lower():
            eactive, eband, erssi = bk.emergency_active()
            if eactive and not emerg_was and not bk.is_muted():      # siren on a fresh detection
                _es = sounds.get({"STRAZ-VHF": "fire", "EMS-VHF": "ems",
                                  "POL-VHF": "police"}.get(eband, "emergency"))
                if _es is not None:
                    try: play_sound(_es)
                    except Exception: pass
            emerg_was = eactive
            lx, ly = mapx0 + 12, by0 + 70
            WB, HB, LAMP = 42, 16, 18
            _flip = int(t / 0.12) % 2 == 0
            draw_rectangle(lx, ly, WB, HB, C(12, 16, 24, 255))
            if eactive:
                _lc = C(60, 120, 255, 255) if _flip else C(16, 22, 42, 255)       # blue lamp
                _rc = DANGER if not _flip else C(52, 18, 22, 255)                 # red lamp
            else:
                _lc = _rc = C(46, 52, 60, 255)                                    # grey (idle scan)
            draw_rectangle(lx + 2, ly + 2, LAMP, HB - 4, _lc)
            draw_rectangle(lx + WB - 2 - LAMP, ly + 2, LAMP, HB - 4, _rc)
            draw_rectangle_lines_ex(Rectangle(lx, ly, WB, HB), 1,
                                    (DANGER if _flip else C(60, 120, 255, 255)) if eactive else DIM)
            if eactive:
                svc = {"STRAZ-VHF": "STRAZ", "EMS-VHF": "EMS", "POL-VHF": "POLICJA",
                       "SM-VHF": "SM", "TETRA-UL": "TETRA"}.get(eband, eband or "SVC")
                text(monoB, "%s! %ddB" % (svc, erssi), lx + WB + 10, ly, 16,
                     C(60, 120, 255, 255) if _flip else DANGER)
                if int(t * 4) % 2 == 0:                               # screen-edge flash
                    draw_rectangle_lines_ex(Rectangle(mapx0 + 2, by0 + 2, (W - mapx0) - 4, bh - 4),
                                            3, DANGER if _flip else C(60, 120, 255, 255))
            else:
                text(mono, "EMERGENCY RF", lx + WB + 10, ly + 1, 14, C(120, 130, 140, 255))

        # ---- 433 Hunter / 433 Capture panels. Small = corner overlay (drawn here);
        #      full-screen (hunt_full, toggled by [H]) is drawn LAST in the frame so
        #      it sits on top of all map HUD (no overlap chaos). ----
        def _draw_hunt_panel(big):
            kh = bk.keyhunt_state()
            if big:
                px0, py0 = mapx0, by0                 # cover the whole map pane (no bleed)
                pw = W - mapx0; ph = by1 - by0
            else:
                px0, py0 = mapx0 + 12, by0 + 64
                pw = int(min(360, (W - mapx0) - 24)); ph = 150
            pad = 16 if big else 10
            draw_rectangle(px0, py0, pw, ph, C(8, 12, 20, 255 if big else 225))
            hot = float(kh.get("hot", 0.0) or 0.0)
            burst = bool(kh.get("burst"))
            jump = float(kh.get("jump", 0.0) or 0.0)
            sweeps = int(kh.get("sweeps", 0) or 0)

            def _hotcol(h):   # cold blue -> cyan -> green -> amber -> red-hot
                h = max(0.0, min(1.0, h))
                stops = [(0.0, (60, 120, 255)), (0.35, (40, 210, 220)),
                         (0.6, (120, 220, 120)), (0.8, (240, 190, 70)),
                         (1.0, (255, 70, 60))]
                for i in range(len(stops) - 1):
                    a, ca = stops[i]; b, cb = stops[i + 1]
                    if h <= b:
                        f = 0 if b == a else (h - a) / (b - a)
                        return C(int(ca[0] + (cb[0] - ca[0]) * f),
                                 int(ca[1] + (cb[1] - ca[1]) * f),
                                 int(ca[2] + (cb[2] - ca[2]) * f), 255)
                return C(255, 70, 60, 255)
            hc = _hotcol(hot)

            ts = 40 if big else 18
            text(monoB, "433 HUNTER", px0 + pad, py0 + (14 if big else 8), ts, hc)
            _tgt = float(kh.get("target_mhz", 0.0) or 0.0)
            _tl = ("LEARNING - press the remote" if kh.get("learning") else "%.3f MHz" % _tgt)
            text(mono, _tl, px0 + pad + int(tw(monoB, "433 HUNTER", ts)) + 16,
                 py0 + (26 if big else 11), 22 if big else 14,
                 WARN if kh.get("learning") else C(150, 165, 180, 255))
            _alive = int(t * 2) % 2 == 0 if sweeps else False
            draw_circle(px0 + pw - (26 if big else 16), py0 + (24 if big else 16),
                        6 if big else 4, SUCCESS if _alive else C(40, 60, 50, 255))
            text(mono, "hits %d" % int(kh.get("hits", 0) or 0),
                 px0 + pw - (160 if big else 86), py0 + (18 if big else 9),
                 20 if big else 13, C(150, 165, 180, 255))
            _blvl = bk.keyhunt_beep_level()
            _bw = ("beep off" if _blvl == 0 else "beep low" if _blvl == 1 else "beep loud")
            text(mono, _bw, px0 + pw - (130 if big else 78), py0 + ph - (24 if big else 15),
                 16 if big else 12, DIM if _blvl == 0 else C(120, 160, 140, 255))
            text(mono, "[H] " + ("okno" if big else "pelny ekran") + "  [K] learn remote  [J] car key",
                 px0 + pad, py0 + ph - (24 if big else 15), 16 if big else 12,
                 C(90, 110, 130, 255))
            if big:                                   # what else is on the air (not beeping)
                _om, _od = float(kh.get("other_mhz", 0) or 0), float(kh.get("other_db", 0) or 0)
                _ov = "RIGHT NEXT TO YOU (receiver overloaded)" if kh.get("overload") else ""
                if _ov:
                    text(monoB, _ov, px0 + pad, py0 + ph - 52, 18, DANGER)
                elif _od > 15:
                    text(mono, "other signal: %.3f MHz +%.0f dB (not your remote)" % (_om, _od),
                         px0 + pad, py0 + ph - 52, 16, DIM)

            bx, by_ = px0 + pad, py0 + ts + (24 if big else 18)
            bw, bh2 = pw - 2 * pad, (48 if big else 20)
            draw_rectangle(bx, by_, bw, bh2, C(16, 22, 32, 255))
            fillw = int(bw * max(0.0, min(1.0, hot)))
            if fillw > 0:
                draw_rectangle(bx, by_, fillw, bh2, hc)
            draw_rectangle_lines_ex(Rectangle(bx, by_, bw, bh2), 1, C(60, 72, 86, 255))
            word = ("RED HOT" if hot >= 0.8 else "HOT" if hot >= 0.6 else
                    "WARM" if hot >= 0.35 else "COLD" if hot > 0.02 else
                    ("SEARCHING" if sweeps else "WARMING UP"))
            text(monoB, word, bx + (12 if big else 6), by_ + (10 if big else 2),
                 34 if big else 16, C(10, 14, 20, 255) if hot >= 0.35 else C(180, 195, 210, 255))
            text(mono, "+%.0f dB" % jump if burst else "-- dB",
                 bx + bw - (150 if big else 64), by_ + (12 if big else 3),
                 26 if big else 14, C(10, 14, 20, 255) if hot >= 0.6 else C(170, 185, 200, 255))

            rows, axis = bk.keyhunt_waterfall()
            sx, sy = px0 + pad, by_ + bh2 + (24 if big else 10)
            sw = pw - 2 * pad; sh = (py0 + ph) - sy - (34 if big else 14)
            draw_rectangle(sx, sy, sw, sh, C(5, 8, 14, 255))
            lthick = 2 if big else 1
            if rows and axis and len(axis) >= 2 and sh > 4:
                row = rows[-1]
                fl = float(kh.get("floor", min(row)) or min(row))
                span = 42.0
                n = min(len(row), len(axis))
                prevx = prevy = None
                pkx = None; pkval = -1e9
                for i in range(n):
                    nx = sx + int(sw * i / (n - 1))
                    nv = max(0.0, min(1.0, (row[i] - fl) / span))
                    ny = sy + sh - 1 - int((sh - 2) * nv)
                    if prevx is not None:
                        draw_line_ex(Vector2(prevx, prevy), Vector2(nx, ny), lthick, C(40, 210, 220, 200))
                    prevx, prevy = nx, ny
                    if row[i] > pkval:
                        pkval = row[i]; pkx = nx
                if pkx is not None and burst:
                    draw_line_ex(Vector2(pkx, sy), Vector2(pkx, sy + sh), lthick, C(255, 70, 60, 180))
                    text(mono, "%.3f" % float(kh.get("peak_freq", 0.0) or 0.0),
                         min(pkx + 4, sx + sw - (70 if big else 42)), sy + 2,
                         22 if big else 13, DANGER)
            else:
                text(mono, "waiting for sweep...", sx + 8, sy + sh // 2 - 7,
                     20 if big else 14, DIM)
            draw_rectangle_lines_ex(Rectangle(sx, sy, sw, sh), 1, C(40, 50, 62, 255))
            draw_rectangle_lines_ex(Rectangle(px0, py0, pw, ph), 2 if big else 1,
                                    hc if burst else C(50, 62, 76, 255))

        def _draw_grab_panel(gbig):
            gs = bk.grab_state()
            if gbig:
                gx, gy = mapx0, by0                   # cover the whole map pane (no bleed)
                gw = W - mapx0; gh = by1 - by0
            else:
                gx, gy = mapx0 + 12, by0 + 64
                gw = int(min(360, (W - mapx0) - 24)); gh = 58
            gpad = 16 if gbig else 12
            draw_rectangle(gx, gy, gw, gh, C(8, 12, 20, 255 if gbig else 225))
            _rec = int(t * 2) % 2 == 0
            draw_circle(gx + (26 if gbig else 18), gy + (26 if gbig else 18),
                        8 if gbig else 5, DANGER if _rec else C(60, 24, 28, 255))  # REC blink
            text(monoB, "433 CAPTURE", gx + (46 if gbig else 32), gy + (14 if gbig else 9),
                 34 if gbig else 18, C(230, 120, 110, 255))
            text(mono, "%d sig" % int(gs.get("count", 0) or 0),
                 gx + gw - (150 if gbig else 70), gy + (18 if gbig else 11),
                 24 if gbig else 15, C(150, 165, 180, 255))
            _last = str(gs.get("last", "") or "listening... (.cu8 + .sub + decoded log)")
            text(mono, _last[:90 if gbig else 52], gx + gpad, gy + (64 if gbig else 34),
                 20 if gbig else 13, C(120, 200, 150, 255))
            if gbig:
                text(mono, "saving: raw .cu8 (URH)  +  decoded log  +  Flipper RAW .sub",
                     gx + gpad, gy + 100, 18, C(120, 140, 160, 255))
                text(mono, "with a Flipper connected -> replay the saved .sub (TX fixed-code)",
                     gx + gpad, gy + 128, 18, C(120, 200, 150, 255))
                _gd = str(gs.get("dir", "") or "")
                if _gd:
                    text(mono, "-> " + _gd[-80:], gx + gpad, gy + 156, 16, DIM)
                text(mono, "[H] okno", gx + gpad, gy + gh - 28, 16, C(90, 110, 130, 255))
            draw_rectangle_lines_ex(Rectangle(gx, gy, gw, gh), 2 if gbig else 1,
                                    C(120, 50, 50, 200) if _rec else C(50, 62, 76, 255))

        if bk.sdr_on() and "keyhunt" in bk.sdr_mode().lower() and not field:
            if not hunt_full:
                _draw_hunt_panel(False)
            # fox-hunt beep — rate rises with 'hot'; silent when cold (fob not pressed).
            # Volume: _blvl 0=off / 1=low / 2=loud (433 Beep menu), gated by global Mute.
            _khs = bk.keyhunt_state()
            _hot = float(_khs.get("hot", 0.0) or 0.0)
            _blvl = bk.keyhunt_beep_level()
            if _hot > 0.03 and not bk.is_muted() and _blvl > 0 and sounds.get("pip") is not None:
                if t >= keyhunt_next_beep:
                    try:
                        set_sound_volume(sounds["pip"], 0.3 if _blvl == 1 else 1.0)
                        set_sound_pitch(sounds["pip"], 0.85 + _hot * 0.9)
                    except Exception:
                        pass
                    try:
                        play_sound(sounds["pip"])
                    except Exception:
                        pass
                    keyhunt_next_beep = t + (1.1 - _hot * 0.98)   # 1.1s cold .. ~0.12s red-hot
            else:
                keyhunt_next_beep = t

        if bk.sdr_on() and "grab433" in bk.sdr_mode().lower() and not field:
            if not hunt_full:
                _draw_grab_panel(False)

        # ---- WDGWars proximity pager (left edge): colour + info of the nearest player ----
        if bk.prox_on() and not field:
            prows = bk.prox_rows()
            unk = bk.prox_unknown()
            inrange = bool(prows) or unk > 0
            mutual = any(r.get("mutual") for r in prows)
            close = any((r.get("state") or 0) >= 4 for r in prows)
            meeting = bk.prox_meeting() is not None
            # colour by relationship: green met/mutual, cyan resolved, amber seen-but-unknown
            if meeting:   pcol, rate, ring = SUCCESS, 0.09, True
            elif mutual:  pcol, rate, ring = SUCCESS, 0.18, close
            elif prows:   pcol, rate, ring = ICE, (0.2 if close else 0.4), False
            elif unk > 0: pcol, rate, ring = WARN, 0.35, False
            else:         pcol, rate, ring = DIM, 0.0, False
            lit = True if rate == 0 else (int(t / rate) % 2 == 0)
            pxp, pyp = mapx0 + 12, by0 + 104
            scr = pcol if (inrange and lit) else C(20, 30, 40, 255)
            draw_line_ex(Vector2(pxp + 4, pyp), Vector2(pxp + 4, pyp - 5), 1, pcol)
            draw_rectangle_lines_ex(Rectangle(pxp, pyp, 18, 22), 1, pcol)
            draw_rectangle(pxp + 3, pyp + 3, 12, 7, scr)
            draw_rectangle(pxp + 3, pyp + 13, 12, 1, pcol)
            if ring and lit:
                draw_circle_lines(pxp + 9, pyp + 7, int(11 + (t * 6) % 4), pcol)
            # info panel: the strongest device found
            if inrange:
                _STN = ("UNKNOWN", "WEAK", "NEAR", "CLOSE", "VERY CLOSE", "LOST")
                ix2, iy2 = pxp + 28, pyp - 7
                if prows:
                    top = max(prows, key=lambda r: ((r.get("state") or 0), (r.get("ema") or -999)))
                    nm = str(top.get("label") or "WDG")[:16]
                    st = _STN[max(0, min(5, top.get("state") or 0))]
                    tr = top.get("trend") or 0
                    arrow = "vv" if tr <= -2 else ("v" if tr == -1 else ("^^" if tr >= 2 else ("^" if tr == 1 else "=")))
                    bar = int(top.get("bar") or 0); ema = top.get("ema")
                    text(monoB, nm, ix2, iy2, 15, pcol)
                    text(mono, "%s  %s" % (st, arrow), ix2, iy2 + 17, 13, pcol)
                    draw_rectangle(ix2, iy2 + 34, 72, 5, C(30, 40, 55, 255))          # signal bar
                    draw_rectangle(ix2, iy2 + 34, int(72 * bar / 20.0), 5, pcol)
                    if ema is not None:
                        text(mono, "%d dBm" % int(ema), ix2 + 80, iy2 + 31, 12, DIM)
                    extra = (len(prows) - 1) + unk
                    if extra > 0:
                        text(mono, "+%d more" % extra, ix2, iy2 + 46, 12, DIM)
                else:
                    text(monoB, "WDG x%d" % unk, ix2, iy2, 15, WARN)
                    text(mono, "nearby - UNKNOWN", ix2, iy2 + 17, 13, WARN)
                    text(mono, "(no resolution key)", ix2, iy2 + 33, 12, DIM)

        zlab = min(ZOOM_LABELS, key=lambda zl: abs(zl[0] - zoom))[1]
        text(mono, "Z:" + zlab, W - 100, by0 + 12, 16, TEAL)
        text(mono, "[+/-] zoom", W - 100, by1 - 44, 13, DIM)
        text(mono, "NODES:%d" % len(valid), W - 118, by0 + 32, 16, DIM)
        text(mono, "CL:%d" % len(clusters), W - 118, by0 + 52, 16, DIM)
        # cluster-nav state hint
        if cluster_sel >= 0 and cluster_sel < len(clusters):
            _hc = clusters[cluster_sel]
            chint = "CLUSTER %d/%d  %d nodes  [ENTER] open  [<>^v] move  [C] off" % (
                cluster_sel + 1, len(clusters), _hc["count"])
            text(mono, chint, W - 18 - tw(mono, chint, 15), by1 - 26, 15, ICE)
        else:
            chint = "[C] Cluster Select  (%d)" % len(clusters)
            text(mono, chint, W - 18 - tw(mono, chint, 16), by1 - 26, 16, TEAL)
        if not menu_open:
            text(mono, "[TAB] MENU", 20, by0 + 12, 17, DIM)
        # last-action readout (top-left of map, so the bottom is free for badges)
        stx = bk.status()
        if stx:
            scol = SUCCESS if any(k in stx.lower() for k in ("found", "ready", "started", "live", "done")) else ICE
            text(monoB, "> " + stx[:56], mapx0 + 20, by0 + 34, 16, scol)
        if not field and bk.scan_count() > 0:
            nc = "NETS: %d" % bk.scan_count()
            text(monoB, nc, mapx0 + (W - mapx0) // 2 - tw(monoB, nc, 17) / 2, by0 + 12, 17, TEAL)

        # earned badge chips — bottom of the map, just above the console line,
        # each coloured by its loot category (bigger so they're readable)
        _badges = bk.badges()
        if _badges and not field:
            BLAB = {"flipper": "FLP", "wardriver": "WDR", "meshcore": "MSH",
                    "handshake_hunter": "HS", "wpasec_uploader": "WPA", "evil_twin": "ET",
                    "skywatch": "ADB", "iot_hunter": "433", "emergency": "SVC"}
            BCOL = {"wardriver": TEAL, "meshcore": PURPLE, "handshake_hunter": MAGENTA,
                    "evil_twin": DANGER, "wpasec_uploader": SUCCESS, "flipper": WARN,
                    "skywatch": ICE, "iot_hunter": C(120, 220, 120), "emergency": C(255, 80, 80)}
            bch = 26
            bx = mapx0 + 16; byb = by1 - bch - 4
            for bid in sorted(_badges):
                lab = BLAB.get(bid, bid[:3].upper()); col = BCOL.get(bid, WARN)
                bwc = int(tw(monoB, lab, 18)) + 18
                draw_rectangle(bx, byb, bwc, bch, C(col.r, col.g, col.b, 34))
                draw_rectangle(bx, byb, 3, bch, col)
                draw_rectangle_lines_ex(Rectangle(bx, byb, bwc, bch), 1, col)
                text(monoB, lab, bx + 9, byb + 5, 18, col)
                bx += bwc + 8

        # radio icon on the map when LoRa / SDR radio is live (animated waves)
        if (bk.lora_on() or bk.sdr_on()) and not field:
            rix, riy = mapx0 + 34, by1 - 70
            halo(rix, riy, PURPLE, ((16, 30), (10, 40)))
            draw_rectangle(rix - 7, riy, 14, 11, PURPLE)              # radio body
            draw_rectangle(rix - 5, riy + 2, 10, 4, C(10, 16, 24, 255))
            draw_line_ex(Vector2(rix + 5, riy), Vector2(rix + 11, riy - 11), 2, PURPLE)  # antenna
            draw_circle(rix + 11, riy - 11, 2, ICE)
            waves = 1 + int(t * 3) % 3
            for wv in range(waves):                                   # signal waves
                rr = 7 + wv * 5
                draw_ring(Vector2(rix + 11, riy - 11), rr, rr + 1.5, -60, 60, 16,
                          C(130, 220, 255, int(160 * (1 - wv / 3.0))))
            text(mono, "LoRa" if bk.lora_on() else "RADIO", rix - 14, riy + 15, 12, PURPLE)

        # selected-cluster info chip (near the cluster, before drilling in)
        if 0 <= cluster_sel < len(clusters) and not cluster_drill:
            cc = clusters[cluster_sel]
            x, y = int(cc["x"]), int(cc["y"])
            wf = sum(1 for p in cc["points"] if p.get("type") == "wifi")
            bt = cc["count"] - wf
            bw, bhh = 214, 48
            bx = min(max(x + 16, 8), W - bw - 8); byv = min(max(y - 24, by0 + 4), by1 - bhh - 4)
            draw_rectangle(bx, byv, bw, bhh, C(7, 11, 17, 242))
            draw_rectangle_lines_ex(Rectangle(bx, byv, bw, bhh), 1, cc["color"])
            draw_rectangle(bx, byv, 4, bhh, cc["color"])
            text(monoB, "%d nodes here" % cc["count"], bx + 12, byv + 6, 16, HI)
            text(mono, "WiFi:%d  BLE:%d   [ENTER]" % (wf, bt), bx + 12, byv + 27, 14, TXT)

        # ---- DRILL-DOWN popup: individual nodes (APs / BLE) of the cluster ----
        if cluster_drill and 0 <= cluster_sel < len(clusters) and not menu_open:
            cc = clusters[cluster_sel]; pts = cc["points"]
            maxrows = 11; pw = 720
            rows_shown = min(len(pts), maxrows) if pts else 1
            ph = 96 + rows_shown * 28 + 16
            px0 = mapx0 + (W - mapx0) // 2 - pw // 2; pyy = by0 + 16
            cyber_panel(px0, pyy, pw, ph, cc["color"])
            nbt = sum(1 for p in pts if p.get("type") == "bt")
            nsec = sum(1 for p in pts if p.get("type") == "wifi"
                       and any(s in (p.get("auth", "") or "").upper() for s in ("WPA", "WEP", "PSK", "SAE")))
            nopen = (len(pts) - nbt) - nsec
            gtext(dispB, "CLUSTER NODES  //  %d" % len(pts), px0 + 18, pyy + 12, 22, HI, 1)
            text(mono, "Open:%d  Secured:%d  BLE:%d" % (nopen, nsec, nbt), px0 + 18, pyy + 44, 15, DIM)
            closehint = "[UP/DOWN] scroll   [C/ESC] close"
            text(mono, closehint, px0 + pw - 18 - tw(mono, closehint, 13), pyy + 18, 13, GHOST)
            hy = pyy + 70
            for cx, lab2 in ((px0 + 44, "SSID / NAME"), (px0 + 392, "TYPE"), (px0 + 500, "RSSI"), (px0 + 596, "CH")):
                text(mono, lab2, cx, hy, 14, DIM)
            draw_line(px0 + 16, hy + 20, px0 + pw - 16, hy + 20, EDGE)
            cluster_scroll = max(0, min(cluster_scroll, max(0, len(pts) - maxrows)))
            for i in range(cluster_scroll, min(len(pts), cluster_scroll + maxrows)):
                p = pts[i]
                ry = hy + 30 + (i - cluster_scroll) * 28
                is_bt = p.get("type") == "bt"
                tcol2 = ICE if is_bt else SUCCESS
                auth = (p.get("auth", "") or "").upper()
                secured = (not is_bt) and any(s in auth for s in ("WPA", "WEP", "PSK", "SAE"))
                draw_circle(px0 + 30, ry + 8, 4, tcol2)
                text(monoB, "L" if secured else "o", px0 + 16, ry, 15, DANGER if secured else WARN)
                text(monoB, str(p.get("label", "?"))[:42], px0 + 44, ry, 16, TXT)
                text(mono, "BLE" if is_bt else "WiFi", px0 + 392, ry, 15, tcol2)
                text(mono, str(p.get("rssi", ""))[:5], px0 + 500, ry, 15, DIM)
                text(mono, str(p.get("channel", ""))[:4], px0 + 596, ry, 15, DIM)
            if len(pts) > maxrows:
                text(mono, "%d-%d / %d" % (cluster_scroll + 1, min(len(pts), cluster_scroll + maxrows), len(pts)),
                     px0 + pw - 96, pyy + ph - 24, 13, DIM)

        if menu_open:
            # dwell timer: show the item's description tooltip after TIP_DELAY seconds
            if (tab, sel) != tip_last:
                tip_last = (tab, sel); tip_since = t
            show_tip = (t - tip_since) >= TIP_DELAY
            draw_rectangle(0, by0, mw, bh, C(5, 8, 13, 238))
            draw_rectangle_gradient_h(mw - 120, by0, 120, bh, C(5, 8, 13, 238), C(5, 8, 13, 0))
            _rw, _rh, _step, _cham = 116, 46, 56, 10   # flat tech buttons: width, height, pitch, corner cut
            railw = _rw
            _y0 = by0 + 18
            _bgcut = C(5, 8, 13, 255)                   # panel-bg colour, used to chamfer a corner
            for i, (nm, cc, _) in enumerate(TABS):
                # Technical flat-HUD tab style (sci-fi UI ref): low dark bars with white
                # labels, a chamfered top-right corner, and category-colour accents —
                # left spine, ▸ arrow, index number, a little hatch block and an under-
                # tick. Selected = tinted fill + bright frame/spine; unselected = dark
                # with dim colour accents. Triangle winding matches the tooltip arrow.
                ty = _y0 + i * _step; on = i == tab
                _acc = cc if on else C(cc.r, cc.g, cc.b, 150)
                draw_rectangle(0, ty, _rw, _rh, C(17, 23, 33, 255) if on else C(12, 16, 24, 255))
                if on:                                  # active bar: category tint + bright frame
                    draw_rectangle(0, ty, _rw, _rh, C(cc.r, cc.g, cc.b, 40))
                    draw_rectangle_lines_ex(Rectangle(0, ty, _rw, _rh), 1, cc)
                draw_triangle(Vector2(float(_rw - _cham), float(ty)),      # chamfer the top-right corner
                              Vector2(float(_rw), float(ty + _cham)), Vector2(float(_rw), float(ty)), _bgcut)
                draw_rectangle(0, ty, 3, _rh, cc if on else C(cc.r, cc.g, cc.b, 170))   # left spine
                text(dispB, nm, 14, ty + (_rh - 20) / 2, 17, HI if on else C(190, 205, 220, 255), 1)
                text(mono, "%03d" % ((i + 1) * 2), _rw - 34, ty + 3, 11, C(cc.r, cc.g, cc.b, 150), 1)
                _ax, _ay = float(_rw - 17), ty + _rh / 2 + 1    # right-pointing arrow
                draw_triangle(Vector2(_ax, _ay - 5), Vector2(_ax, _ay + 5), Vector2(_ax + 7, _ay), _acc)
                for _h in range(0, 18, 5):              # hatch block to the right of the bar
                    draw_line_ex(Vector2(float(_rw + 8 + _h), float(ty + _rh - 6)),
                                 Vector2(float(_rw + 16 + _h), float(ty + 6)), 1.5,
                                 C(cc.r, cc.g, cc.b, 150 if on else 90))
                draw_rectangle(6, ty + _rh + 2, 22, 2, C(cc.r, cc.g, cc.b, 120))   # under-tick
            ix = railw + 48
            text(dispB, "// " + tname, ix, by0 + 20, 34, tcol if tname == "ATTACK" else HI, 2)
            draw_rectangle(ix, by0 + 58, 3, 24, tcol)
            _nets = bk.networks()
            if 0 <= msel < len(_nets):
                _tn = _nets[msel]; tgt = "target > %s - ch%s" % (_tn.ssid or "<hidden>", _tn.channel or "?")
            elif _nets:
                tgt = "%d networks - [C] select target" % len(_nets)
            else:
                tgt = "no target - run a scan first"
            text(mono, tgt, ix + 10, by0 + 60, 16, DIM)
            iy = by0 + 96
            rowh = max(22, min(30, (by1 - iy - 10) // max(1, len(items))))
            fsz = 19 if rowh >= 28 else (17 if rowh >= 25 else 15)
            # Scroll window: when the list is taller than the panel even at the
            # minimum row height (e.g. ADDONS), show only a window of rows that
            # follows the cursor, so the selected item is never off-screen.
            avail = by1 - iy - 10
            max_vis = max(1, avail // rowh)
            first = (max(0, min(sel - max_vis // 2, len(items) - max_vis))
                     if len(items) > max_vis else 0)
            last = min(len(items), first + max_vis)
            for i in range(first, last):
                it = items[i]
                k, label, icmd, iskey = it[0], it[1], it[2], it[3]
                on = i == sel; rowy = iy + (i - first) * rowh
                run = iskey in running_keys
                if on:
                    draw_rectangle(ix, rowy - 3, mw - ix - 300, rowh - 2, C(tcol.r, tcol.g, tcol.b, 36))
                    draw_rectangle(ix, rowy - 3, 3, rowh - 2, tcol)
                kc = tcol if on else C(54, 80, 106)
                kbh = min(20, rowh - 4)
                draw_rectangle(ix + 8, rowy, 30, kbh, kc)
                text(monoB, k, ix + 8 + (30 - tw(monoB, k, 16)) / 2, rowy + 1, 16, VOID if on else HI)
                lcol = (DANGER if tname == "ATTACK" else SUCCESS) if run else (HI if on else TXT)
                text(mono, label, ix + 50, rowy, fsz, lcol)
                if run:
                    draw_circle(ix + 50 + int(tw(mono, label, fsz)) + 12, rowy + rowh // 2, 4, SUCCESS)
            # scrollbar on the right of the list when it overflows the panel
            if len(items) > max_vis:
                trk_x = mw - 306; trk_y = iy - 2; trk_h = max_vis * rowh
                draw_rectangle(trk_x, trk_y, 3, trk_h, C(40, 56, 74, 170))
                thm_h = max(16, int(trk_h * max_vis / len(items)))
                thm_y = trk_y + int((trk_h - thm_h) * first / max(1, len(items) - max_vis))
                draw_rectangle(trk_x, thm_y, 3, thm_h, tcol)
            # dwell tooltip bubble (right of the panel, over the map, pointing left)
            if show_tip and 0 <= sel < len(items):
                desc = MENU_DESC.get(items[sel][1], "")
                if desc:
                    tfs = 16; maxw = 360; pad = 12
                    lines = []; cur = ""
                    for wrd in desc.split(" "):
                        test = (cur + " " + wrd).strip()
                        if cur and tw(mono, test, tfs) > maxw:
                            lines.append(cur); cur = wrd
                        else:
                            cur = test
                    if cur:
                        lines.append(cur)
                    lh = int(tfs * UISCALE) + 7
                    bwid = int(min(maxw, max(tw(mono, ln, tfs) for ln in lines)) + pad * 2)
                    bhei = lh * len(lines) + pad * 2 - 6
                    rowy_sel = iy + (sel - first) * rowh
                    bxx = mw + 18
                    byy = int(min(max(rowy_sel - 8, by0 + 8), by1 - bhei - 8))
                    ay = int(min(byy + bhei - 10, max(byy + 10, rowy_sel + rowh // 2)))
                    draw_triangle(Vector2(bxx - 11, ay), Vector2(bxx, ay + 8),
                                  Vector2(bxx, ay - 8), C(12, 18, 27, 248))
                    draw_rectangle(bxx, byy, bwid, bhei, C(12, 18, 27, 248))
                    draw_rectangle_lines_ex(Rectangle(bxx, byy, bwid, bhei), 1, tcol)
                    draw_rectangle(bxx, byy, 3, bhei, tcol)
                    for li, ln in enumerate(lines):
                        text(mono, ln, bxx + pad, byy + pad + li * lh, tfs, ICE)
            # own clip if present, else fall back to the SYSTEM gorilla (not the human
            # char) so EXTRAS/CRISIS keep a gorilla until their own clips are added
            frames = (gchar_by_name.get(TABS[tab][0].lower())
                      or gchar_by_name.get("system") or [])
            spr = frames[int(t * GFPS) % len(frames)] if frames else char
            if spr:
                chh = bh * 0.74; scale = chh / spr.height; cww = spr.width * scale
                maxw = (mw - ix - 30) * 0.9
                if cww > maxw: scale = maxw / spr.width; cww = maxw; chh = spr.height * scale
                cxc = mw - 150; cx = cxc - cww / 2; cy = by1 - chh
                draw_texture_pro(spr, Rectangle(0, 0, spr.width, spr.height),
                                 Rectangle(cx, cy, cww, chh), Vector2(0, 0), 0.0,
                                 WHITE if not field else C(210, 225, 235, 255))
                lblw = tw(monoB, "OPERATOR", 17)
                text(monoB, "OPERATOR", cxc - lblw / 2, by0 + 36, 17, TEAL)
                q = "// " + QUIPS[int(t / 3) % len(QUIPS)]
                text(mono, q, cxc - tw(mono, q, 15) / 2, by0 + 58, 15, DIM)

        # HUD
        draw_rectangle(0, 0, W, HUD, C(10, 16, 24, 230)); draw_line(0, HUD, W, HUD, EDGE)
        lx = 20
        for part, pc in (("Watch", MAGENTA), ("Dogs", TEAL), ("Go", MAGENTA)):
            (gtext if not field else text)(dispB, part, lx, 15, 26, pc, 1)
            lx += tw(dispB, part, 26, 1)
        cx0 = int(lx) + 28
        def chip(x, label, on, warn=False):
            col = SUCCESS if on else (WARN if warn else DIM)
            draw_circle(x, 28, 5, col); text(mono, label, x + 12, 20, 16, TXT)
            return x + 26 + tw(mono, label, 16)
        esp_lbl = "ESP32" if bk.connected else ("ESP32 off" if bk.ok else "DEMO")
        gps_lbl = ("GPS %dsat" % bk.gps_sats()) if (bk.state and bk.state.gps_fix_valid) else ("GPS ..." if bk.gps_on else "GPS off")
        lora_run = bk.lora_on(); sdr_run = bk.sdr_on(); sdr_rail = bk.sdr_enabled()
        hx = chip(cx0, esp_lbl, bk.connected)
        hx = chip(int(hx) + 16, gps_lbl, bool(bk.state and bk.state.gps_fix_valid), warn=bk.gps_on)
        hx = chip(int(hx) + 16, "LoRa" + (" ON" if lora_run else " off"), lora_run)
        # RADIO: green when a mode is live (ADSB/433/SVC), amber when the rail is
        # powered but idle, grey when off — mirrors the OG ('SDR', _sdr_enabled) light
        radio_lbl = ("RADIO " + (bk.sdr_mode().upper().replace("EMERGENCY", "SVC").replace("KEYHUNT", "433").replace("GRAB433", "CAP"))) if sdr_run \
            else ("RADIO rdy" if sdr_rail else "RADIO off")
        chip(int(hx) + 16, radio_lbl, sdr_run, warn=sdr_rail)
        # Update banners — centered just BELOW the top bar so they never overlap the
        # HUD widgets (LV/XP/scan all live inside the bar). If both are present they
        # stack one under the other instead of colliding.
        _bny = HUD + 10
        if bk.game_update_available():
            umsg = "GAME UPDATE AVAILABLE  [U]"
            uw = int(tw(monoB, umsg, 16)) + 26
            ux = (W - uw) // 2
            pulse = 120 + int(100 * abs(math.sin(t * 3)))
            draw_rectangle(ux, _bny, uw, 32, C(255, 46, 136, 46))
            draw_rectangle_lines_ex(Rectangle(ux, _bny, uw, 32), 2, C(255, 46, 136, pulse))
            text(monoB, umsg, ux + 13, _bny + 6, 16, MAGENTA)
            _bny += 38
        if bk.fw_update_available():
            fmsg = "FW UPDATE v%s  -  SYSTEM > Flash ESP32" % bk.fw_latest()
            fww = int(tw(monoB, fmsg, 16)) + 30
            fx = (W - fww) // 2
            fpulse = 120 + int(100 * abs(math.sin(t * 3)))
            draw_rectangle(fx, _bny, fww, 32, C(255, 176, 32, 54))
            draw_rectangle_lines_ex(Rectangle(fx, _bny, fww, 32), 2, C(255, 176, 32, fpulse))
            text(monoB, fmsg, fx + 15, _bny + 6, 16, WARN)
        sp_txt = "SCANNING" if scanning else "IDLE"
        spw = tw(dispB, sp_txt, 22, 1) + 54; spx = W - 360 - spw
        draw_rectangle_rounded(Rectangle(spx - 2, 10, spw + 4, 36), 0.35, 8, C(34, 211, 219, 120))
        draw_rectangle_rounded(Rectangle(spx, 12, spw, 32), 0.3, 8, C(12, 24, 30, 255))
        pr = 5 + (2 * abs(math.sin(t * 4)) if (scanning and not field) else 0)
        draw_circle(int(spx + 20), 28, pr, TEAL)
        (gtext if not field else text)(dispB, sp_txt, spx + 34, 17, 22, TEAL, 1)
        # WDGWars account name — matching pill just left of IDLE/SCANNING, with a
        # magenta->cyan gradient; fixed width so varying names don't shift the bar,
        # marquee-scrolls like an LED ticker when it's too long.
        _un = bk.wdg_username()
        if _un:
            _ut = "@" + _un
            unw = 152
            unx = int(spx - 16 - unw)   # int: scissor + gradient_h require int coords
            draw_rectangle_rounded(Rectangle(unx - 2, 10, unw + 4, 36), 0.35, 8, C(255, 46, 136, 110))
            draw_rectangle_rounded(Rectangle(unx, 12, unw, 32), 0.3, 8, C(10, 16, 24, 255))
            draw_rectangle_gradient_h(unx + 3, 14, unw - 6, 28, C(255, 46, 136, 55), C(34, 211, 219, 55))
            begin_blend_mode(BLEND_ADDITIVE)
            draw_circle(int(unx + 18), 28, 5 + abs(math.sin(t * 2.5)), MAGENTA)
            end_blend_mode()
            draw_circle(int(unx + 18), 28, 4, TEAL)
            _utw = tw(dispB, _ut, 18, 1)
            begin_scissor_mode(unx + 30, 12, unw - 36, 32)
            if _utw <= unw - 38:
                (gtext if not field else text)(dispB, _ut, unx + 30, 16, 18, HI, 1)
            else:
                _per = _utw + 36
                _o = (t * 42) % _per
                (gtext if not field else text)(dispB, _ut, int(unx + 30 - _o), 16, 18, HI, 1)
                (gtext if not field else text)(dispB, _ut, int(unx + 30 - _o + _per), 16, 18, HI, 1)
            end_scissor_mode()
        # level / rank title / XP bar (top-right)
        _xp = bk.xp(); _lvl = bk.level(); _title = bk.level_title()
        xp_txt = ("%.1fk" % (_xp / 1000.0)) if _xp >= 1000 else str(_xp)
        hdr = "LV %d  %s" % (_lvl, _title)
        hx2 = W - 20 - tw(monoB, hdr, 16)
        text(monoB, hdr, hx2, 9, 16, SUCCESS)
        cur, span = bk.xp_bar()
        xbw = 150; xbx = W - 20 - xbw; xby = 32
        draw_rectangle(xbx, xby, xbw, 7, C(20, 30, 44, 255))
        draw_rectangle(xbx, xby, int(xbw * max(0.0, min(1.0, cur / span))), 7, TEAL)
        text(mono, xp_txt + " XP", xbx - 10 - tw(mono, xp_txt + " XP", 13), xby - 3, 13, DIM)
        # hacker-hat identity chip (left of the LV header)
        hname, hrgb = bk.hat_profile(); hcol = C(hrgb[0], hrgb[1], hrgb[2])
        hcx = int(hx2 - 20 - tw(mono, hname, 14))
        begin_blend_mode(BLEND_ADDITIVE); draw_circle(hcx - 12, 17, 6 + abs(math.sin(t * 2)) * 2, C(hcol.r, hcol.g, hcol.b, 70)); end_blend_mode()
        draw_circle(hcx - 12, 17, 6, HI); draw_circle(hcx - 12, 17, 4, hcol)
        text(mono, hname, hcx, 10, 14, hcol if sum(hrgb) > 120 else HI)

        # TERMINAL (live ESP32 serial + backend events; demo when no backend)
        ty0 = H - STA - TRM
        draw_rectangle(0, ty0, W, TRM, C(8, 12, 18, 255)); draw_line(0, ty0, W, ty0, EDGE)
        text(mono, "TERMINAL", 20, ty0 + 8, 16, GHOST)
        _kcol = {"tx": TEAL, "ok": SUCCESS, "warn": WARN, "err": DANGER, "raw": TXT}
        tlines = [(ts, msg, _kcol.get(k, TXT)) for (ts, msg, k) in bk.term[-4:]] if (bk.ok and bk.term) else TERM
        for i, (ts, msg, col) in enumerate(tlines):
            ly = ty0 + 32 + i * 29
            text(mono, ts, 20, ly, 20, GHOST, raw=True); text(mono, msg[:92], 136, ly, 20, col, raw=True)
        cary = ty0 + 32 + len(tlines) * 29
        text(mono, "_", 20, cary, 20, TXT, raw=True)
        if int(t * 2) % 2 == 0: draw_rectangle(42, cary + 3, 10, 17, TEAL)

        # STATUS (real loot counters: all-time totals + live session)
        tot = bk.totals()
        fi = tot.get("wardriving", 0) + len(bk.networks())
        hs = tot.get("hc22000", 0) or tot.get("hccapx", 0) or tot.get("pcap", 0)
        pwd = tot.get("passwords", 0) + tot.get("et_captures", 0)
        pwn = tot.get("cracked", 0) + len(hacked)
        adsb = tot.get("adsb", 0)
        sy = H - STA
        draw_rectangle(0, sy, W, STA, VOID); draw_line(0, sy, W, sy, C(14, 20, 32, 255))
        text(mono, "Fi:", 20, sy + 10, 18, DIM); text(monoB, str(fi), 50, sy + 10, 18, TEAL)
        fx = 50 + tw(monoB, str(fi), 18) + 18
        pre = "HS:%d PWD:%d PWN:%d  ADS-B:%d/0  LoRa:" % (hs, pwd, pwn, adsb)
        text(mono, pre, fx, sy + 10, 18, DIM)
        text(monoB, "IDLE", fx + tw(mono, pre, 18), sy + 10, 18, TEAL)
        gps = bk.gps_text()
        gpsx = W - 20 - tw(monoB, gps, 18)
        text(monoB, gps, gpsx, sy + 10, 18, SUCCESS if (bk.state and bk.state.gps_fix_valid) else DIM)
        # battery indicator (left of the GPS coords)
        bpct = bk.read_battery()
        if bpct >= 0:
            bmv = bk.battery_mv()
            low_v = bool(bmv) and bmv < 3450          # AXP223 % lies under load; trust voltage
            bcol = DANGER if low_v else (SUCCESS if bpct > 25 else (WARN if bpct > 10 else DANGER))
            blink_b = (bpct > 10 and not low_v) or int(t * 3) % 2 == 0
            bxx = int(gpsx) - 90
            if blink_b:
                draw_rectangle_lines_ex(Rectangle(bxx, sy + 12, 26, 14), 1, bcol)
                draw_rectangle(bxx + 26, sy + 15, 2, 8, bcol)
                draw_rectangle(bxx + 2, sy + 14, int(22 * bpct / 100), 10, bcol)
            text(mono, ("%.2fV" % (bmv / 1000.0)) if low_v else ("%d%%" % bpct), bxx + 34, sy + 11, 16, bcol)

        # key hints — tucked UNDER the brand + module chips (not crammed above
        # them), in TXT so they actually read against the bar
        hint = ("MENU   <-/-> tab   UP/DOWN item   ENTER run   TAB/ESC close"
                if menu_open else "MAP  TAB=menu  C=cluster  ENTER=open  SPACE=hack  `=loot  P=creds  +/-=zoom  arrows=pan  0=home")
        text(mono, hint, 20, 44, 15, TXT)

        # target-input dialog (Deauth/SAE bssid+ch, BT Tracker mac)
        if input_mode:
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 170))
            bw, bhh = 540, 150; bx = (W - bw) // 2; byv = (H - bhh) // 2
            cyber_panel(bx, byv, bw, bhh, tcol)
            ttl = "ENTER BSSID + CHANNEL" if input_mode == "bssid_ch" else "ENTER TARGET MAC"
            gtext(dispB, ttl, bx + 28, byv + 22, 24, HI, 1)
            ex = "e.g.  AA:BB:CC:DD:EE:FF 6" if input_mode == "bssid_ch" else "e.g.  AA:BB:CC:DD:EE:FF"
            text(mono, ex, bx + 28, byv + 56, 15, DIM)
            draw_rectangle(bx + 28, byv + 82, bw - 56, 34, C(5, 8, 13, 255))
            draw_rectangle_lines_ex(Rectangle(bx + 28, byv + 82, bw - 56, 34), 1, tcol)
            cur = input_buf + ("_" if int(t * 2) % 2 == 0 else " ")
            text(monoB, cur, bx + 38, byv + 89, 20, HI)
            text(mono, "[ENTER] fire   [ESC] cancel", bx + 28, byv + 122, 14, GHOST)

        # LOOT DATABASE / STATS screen
        if loot_screen:
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, TEAL)
            gtext(dispB, "LOOT DATABASE", 24, 10, 26, HI, 1)
            text(mono, "// WatchDogsGo", 24 + tw(dispB, "LOOT DATABASE", 26, 1) + 16, 18, 16, DIM)
            text(mono, "[L] / [ESC] close", W - 20 - tw(mono, "[L] / [ESC] close", 14), 18, 14, DIM)
            tot = bk.totals()

            def col_header(cx, label):
                text(monoB, label, cx, 72, 15, TEAL, 3)
                draw_line(cx, 92, cx + 160, 92, C(53, 230, 255, 120))

            def stat(cx, yy, label, val, col):
                text(mono, label, cx, yy, 18, DIM)
                text(monoB, str(val), cx + 180 - tw(monoB, str(val), 18), yy, 18, col)

            def led(cx, yy, label, on, txt, col):
                text(mono, label, cx, yy, 18, DIM)
                c = col if on else C(90, 100, 110, 255)
                begin_blend_mode(BLEND_ADDITIVE); draw_circle(cx + 150, yy + 9, 6, C(c.r, c.g, c.b, 90)); end_blend_mode()
                draw_circle(cx + 150, yy + 9, 4, c)
                text(monoB, txt, cx + 162, yy, 16, c)

            # --- col 1: ALL-TIME TOTALS ---
            c1 = 60; col_header(c1, "ALL-TIME TOTALS"); yy = 108
            rows1 = [("Sessions", tot.get("sessions", 0), HI),
                     ("WiFi nets", tot.get("wardriving", 0), SUCCESS),
                     ("BT devices", tot.get("bt_devices", 0), TEAL),
                     ("Handshakes", tot.get("pcap", 0), DANGER),
                     ("HC22000", tot.get("hc22000", 0), DANGER),
                     ("Passwords", tot.get("passwords", 0) + tot.get("et_captures", 0), ICE),
                     ("Cracked", tot.get("cracked", 0), SUCCESS),
                     ("MC Nodes", tot.get("mc_nodes", 0), WARN),
                     ("MC Msgs", tot.get("mc_messages", 0), WARN),
                     ("ADS-B", tot.get("adsb", 0), ICE),
                     ("SVC alerts", tot.get("emergency", 0), DANGER),
                     ("Contacts", bk.contacts_count(), TEAL)]
            for lab, v, cc in rows1:
                stat(c1, yy, lab, v, cc); yy += 30

            # --- col 2: THIS SESSION + XP + badges ---
            c2 = 470; col_header(c2, "THIS SESSION"); yy = 108
            rows2 = [("WiFi", bk.scan_count(), SUCCESS),
                     ("Captures", len(bk.captures), ICE),
                     ("MC msgs", len(bk.mc_log), WARN)]
            for lab, v, cc in rows2:
                stat(c2, yy, lab, v, cc); yy += 30
            yy += 10
            text(monoB, "LV %d  %s" % (bk.level(), bk.level_title()), c2, yy, 20, SUCCESS); yy += 30
            cur, span = bk.xp_bar()
            draw_rectangle(c2, yy, 200, 10, C(20, 30, 44, 255))
            draw_rectangle(c2, yy, int(200 * max(0.0, min(1.0, cur / span))), 10, TEAL)
            text(mono, "%d XP" % bk.xp(), c2 + 210, yy - 3, 14, DIM); yy += 28
            text(monoB, "BADGES", c2, yy, 14, TEAL, 3); yy += 22
            BLAB = {"flipper": "FLP", "wardriver": "WDR", "meshcore": "MSH", "handshake_hunter": "HS",
                    "wpasec_uploader": "WPA", "evil_twin": "ET", "skywatch": "ADB",
                    "iot_hunter": "433", "emergency": "SVC"}
            BCOL = {"wardriver": TEAL, "meshcore": PURPLE, "handshake_hunter": MAGENTA,
                    "evil_twin": DANGER, "wpasec_uploader": SUCCESS, "flipper": WARN,
                    "skywatch": ICE, "iot_hunter": C(120, 220, 120), "emergency": C(255, 80, 80)}
            bx = c2; brow = 0
            for bid in sorted(bk.badges()):
                lab = BLAB.get(bid, bid[:3].upper()); col = BCOL.get(bid, WARN)
                bwc = int(tw(monoB, lab, 17)) + 16
                if bx + bwc > c2 + 360: bx = c2; brow += 1
                ry = yy + brow * 30
                draw_rectangle(bx, ry, bwc, 24, C(col.r, col.g, col.b, 34))
                draw_rectangle_lines_ex(Rectangle(bx, ry, bwc, 24), 1, col)
                text(monoB, lab, bx + 8, ry + 4, 17, col); bx += bwc + 8

            # --- col 3: STATUS ---
            c3 = 900; col_header(c3, "STATUS"); yy = 108
            led(c3, yy, "ESP32", bk.connected, "ONLINE" if bk.connected else "OFFLINE",
                SUCCESS if bk.connected else DANGER); yy += 30
            gfix = bool(bk.state and bk.state.gps_fix_valid)
            led(c3, yy, "GPS", gfix, ("FIX %dsat" % bk.gps_sats()) if gfix else ("SEARCH" if bk.gps_on else "N/A"),
                SUCCESS if gfix else (WARN if bk.gps_on else DANGER)); yy += 30
            led(c3, yy, "LoRa", bk.lora_on(), "ON" if bk.lora_on() else "OFF", SUCCESS if bk.lora_on() else DANGER); yy += 30
            led(c3, yy, "RADIO", bk.sdr_on(), (bk.sdr_mode().upper() or "ON") if bk.sdr_on() else "OFF",
                SUCCESS if bk.sdr_on() else DANGER); yy += 30
            _bmv = bk.battery_mv(); _bpct = bk.read_battery(); _blow = bool(_bmv) and _bmv < 3450
            led(c3, yy, "BATT", not _blow,
                ("%d%% %.2fV" % (_bpct, _bmv / 1000.0)) if _bmv else ("%d%%" % _bpct),
                DANGER if _blow else (SUCCESS if _bpct > 25 else WARN)); yy += 30
            ops = []
            if scanning: ops.append("SCAN")
            if bk.state and bk.state.evil_twin_running: ops.append("ET")
            if bk.state and bk.state.portal_running: ops.append("EP")
            if bk.lora_on(): ops.append("MESH")
            led(c3, yy, "Active", bool(ops), " ".join(ops) if ops else "idle", TEAL if ops else DIM); yy += 40
            text(mono, "REPO: %s" % (bk.app_dir.split("/")[-1] if bk.app_dir else "?"), c3, yy, 14, GHOST)

            # --- SESSION LOG (journal): per-session archive, like a case drawer ---
            jy = 474
            draw_line(40, jy, W - 40, jy, C(53, 230, 255, 70))
            if t - loot_log_refresh_t > 15.0:         # auto-refresh (background, no flicker)
                loot_log_refresh_t = t
                bk.sessions_refresh()
            _sess = bk.sessions()
            text(monoB, "SESSION LOG", 60, jy + 12, 15, TEAL, 3)
            if _sess is not None:
                _emp = bk.sessions_empty()
                _note = "%d events" % len(_sess) + ("   %d empty hidden" % _emp if _emp else "")
                text(mono, _note, 62 + tw(monoB, "SESSION LOG", 15, 3) + 16, jy + 14, 14, DIM)
            text(mono, "[UP/DOWN] select  [ENTER] open  [X] delete",
                 W - 60 - tw(mono, "[UP/DOWN] select  [ENTER] open  [X] delete", 13), jy + 14, 13, GHOST)
            _ly0 = jy + 40
            _rowh = 30
            _rows = max(1, (H - 28 - _ly0) // _rowh)
            if _sess is None:
                text(mono, "indexing session archive...", 60, _ly0 + 6, 16, DIM)
            elif not _sess:
                text(mono, "no sessions recorded yet", 60, _ly0 + 6, 16, DIM)
            else:
                loot_log_sel = max(0, min(loot_log_sel, len(_sess) - 1))
                if loot_log_sel < loot_log_scroll:
                    loot_log_scroll = loot_log_sel
                elif loot_log_sel >= loot_log_scroll + _rows:
                    loot_log_scroll = loot_log_sel - _rows + 1
                loot_log_scroll = max(0, min(loot_log_scroll, max(0, len(_sess) - _rows)))
                _upl = bk.uploaded_sessions()     # cached set of fully-uploaded names
                for _i, _s in enumerate(_sess[loot_log_scroll:loot_log_scroll + _rows]):
                    _ry = _ly0 + _i * _rowh
                    _abs = loot_log_scroll + _i
                    if _abs == loot_log_sel:                 # selection highlight
                        draw_rectangle(44, _ry - 2, W - 88, _rowh - 2, C(34, 211, 219, 30))
                        draw_rectangle(44, _ry - 2, 3, _rowh - 2, TEAL)
                    _fc = SUCCESS if _s["n"] else C(90, 100, 110, 255)
                    draw_rectangle(62, _ry + 1, 10, 4, _fc)                 # folder tab
                    draw_rectangle(60, _ry + 4, 24, 16, C(_fc.r, _fc.g, _fc.b, 45))
                    draw_rectangle_lines_ex(Rectangle(60, _ry + 4, 24, 16), 1, _fc)
                    _nm = _s["name"]
                    _disp = _nm[:10] + "   " + _nm[11:].replace("-", ":")
                    text(monoB, _disp, 98, _ry + 2, 17, HI if _abs == loot_log_sel else (TXT if _s["n"] else DIM))
                    _c = _s["counts"]
                    _parts = []
                    for _k, _lab in (("wifi", "wifi"), ("bt", "bt"), ("hs", "hs"),
                                     ("mc", "mc"), ("adsb", "adsb")):
                        if _c[_k]:
                            _parts.append("%d %s" % (_c[_k], _lab))
                    _summ = "   ".join(_parts) if _parts else "empty"
                    text(mono, _summ, 360, _ry + 3, 15,
                         ICE if _parts else C(90, 100, 110, 255))
                    # WDGWars upload state (only meaningful for sessions with data)
                    if _s["n"]:
                        _sent = _s["name"] in _upl
                        _bt = "SENT" if _sent else "PENDING"
                        _bc = SUCCESS if _sent else C(120, 150, 170, 255)
                        text(mono, _bt, W - 96 - tw(mono, _bt, 13), _ry + 3, 13, _bc)
                if len(_sess) > _rows:
                    _pg = "%d / %d" % (loot_log_sel + 1, len(_sess))
                    text(mono, _pg, W - 60 - tw(mono, _pg, 13), H - 22, 13, DIM)

        # SESSION drill-in: open a session folder like a case file
        if loot_detail is not None:
            _s = loot_detail
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 220))
            pw, ph = 880, 540; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, TEAL)
            _nm = _s["name"]
            gtext(dispB, "SESSION  " + _nm[:10] + "   " + _nm[11:].replace("-", ":"),
                  px0 + 28, py0 + 20, 24, HI, 1)
            _c = _s["counts"]; _cx = px0 + 28
            for _lab, _k, _col in (("WiFi", "wifi", SUCCESS), ("BT", "bt", TEAL),
                                   ("Handshakes", "hs", DANGER), ("MeshCore", "mc", PURPLE),
                                   ("ADS-B", "adsb", ICE)):
                _v = _c.get(_k, 0)
                text(mono, _lab, _cx, py0 + 60, 14, DIM)
                text(monoB, str(_v), _cx, py0 + 78, 20, _col if _v else C(90, 100, 110, 255))
                _cx += 158
            draw_line(px0 + 24, py0 + 112, px0 + pw - 24, py0 + 112, C(53, 230, 255, 120))
            text(mono, "FILES (%d)" % len(loot_detail_files), px0 + 28, py0 + 120, 14, TEAL)
            _fly = py0 + 146; _flh = 24; _fmax = max(1, (ph - 196) // _flh)
            if not loot_detail_files:
                text(mono, "no files in this session folder", px0 + 28, _fly + 6, 16, DIM)
            else:
                loot_detail_sel = max(0, min(loot_detail_sel, len(loot_detail_files) - 1))
                if loot_detail_sel < loot_detail_scroll:
                    loot_detail_scroll = loot_detail_sel
                elif loot_detail_sel >= loot_detail_scroll + _fmax:
                    loot_detail_scroll = loot_detail_sel - _fmax + 1
                loot_detail_scroll = max(0, min(loot_detail_scroll, max(0, len(loot_detail_files) - _fmax)))
                for _i, _f in enumerate(loot_detail_files[loot_detail_scroll:loot_detail_scroll + _fmax]):
                    _ry = _fly + _i * _flh
                    _fabs = loot_detail_scroll + _i
                    if _fabs == loot_detail_sel:
                        draw_rectangle(px0 + 28, _ry - 2, pw - 56, _flh - 2, C(34, 211, 219, 30))
                        draw_rectangle(px0 + 28, _ry - 2, 3, _flh - 2, TEAL)
                    _sz = _f["size"]
                    _szs = ("%d B" % _sz if _sz < 1024 else
                            ("%.1f KB" % (_sz / 1024) if _sz < 1048576 else "%.1f MB" % (_sz / 1048576)))
                    draw_circle(px0 + 40, _ry + 9, 2, TEAL)
                    text(mono, _f["name"][:78], px0 + 50, _ry, 16,
                         HI if _fabs == loot_detail_sel else TXT)
                    text(mono, _szs, px0 + pw - 36 - tw(mono, _szs, 15), _ry, 15, DIM)
                if len(loot_detail_files) > _fmax:
                    _fpg = "%d / %d" % (loot_detail_sel + 1, len(loot_detail_files))
                    text(mono, _fpg, px0 + pw - 28 - tw(mono, _fpg, 13), py0 + ph - 52, 13, DIM)
            _dh = "[UP/DOWN] select   [ENTER] preview   [X] delete session   [ESC] back"
            if _c.get("adsb"):
                _dh = "[A] aircraft tracks   " + _dh
            text(mono, _dh, px0 + 28, py0 + ph - 30, 14, TEAL if _c.get("adsb") else GHOST)

        # FILE preview inside a session (text lines or hex dump)
        if loot_preview is not None:
            _pv = loot_preview
            draw_rectangle(0, 0, W, H, C(2, 4, 7, 236))
            pw, ph = 1060, 604; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, ICE)
            _fn = (loot_detail_files[loot_detail_sel]["name"]
                   if loot_detail_files and 0 <= loot_detail_sel < len(loot_detail_files) else "")
            gtext(dispB, _fn[-52:] if len(_fn) > 52 else _fn, px0 + 28, py0 + 20, 22, HI, 1)
            _kind = _pv.get("kind", "text")
            _meta = "%s   %d B%s" % (_kind.upper(), _pv.get("size", 0),
                                     "   truncated" if _pv.get("truncated") else "")
            text(mono, _meta, px0 + pw - 28 - tw(mono, _meta, 14), py0 + 26, 14, DIM)
            draw_line(px0 + 24, py0 + 56, px0 + pw - 24, py0 + 56, C(130, 220, 255, 120))
            _lines = _pv.get("lines", [])
            _lh = 18; _ly = py0 + 70; _lmax = max(1, (ph - 108) // _lh)
            loot_preview_scroll = max(0, min(loot_preview_scroll, max(0, len(_lines) - _lmax)))
            if not _lines:
                text(mono, "(empty file)", px0 + 28, _ly, 16, DIM)
            for _i, _ln in enumerate(_lines[loot_preview_scroll:loot_preview_scroll + _lmax]):
                text(mono, _ln[:130], px0 + 28, _ly + _i * _lh, 15,
                     ICE if _kind == "binary" else TXT)
            if len(_lines) > _lmax:
                _ppg = "line %d-%d / %d" % (loot_preview_scroll + 1,
                                            min(loot_preview_scroll + _lmax, len(_lines)), len(_lines))
                text(mono, _ppg, px0 + pw - 28 - tw(mono, _ppg, 13), py0 + ph - 30, 13, DIM)
            text(mono, "[UP/DOWN] scroll   [PgUp/PgDn] page   [ESC] back",
                 px0 + 28, py0 + ph - 30, 14, GHOST)

        # ADS-B TRACKS of one session: tracks coloured by altitude + receiver coverage
        if loot_adsb is not None:
            import adsb_tracks as _at
            _ad = loot_adsb
            draw_rectangle(0, 0, W, H, C(2, 4, 7, 240))
            pw, ph = W - 60, H - 40; px0, py0 = 30, 20
            cyber_panel(px0, py0, pw, ph, ICE)
            _nm = loot_detail["name"] if loot_detail else ""
            gtext(dispB, "ADS-B TRACKS  " + _nm[:10] + "  " + _nm[11:].replace("-", ":"), px0 + 24, py0 + 16, 22, HI, 1)
            _ids = sorted(_ad["tracks"], key=lambda k: -len(_ad["tracks"][k]["pts"]))
            # map area (square, left) and list (right)
            ms = min(ph - 110, pw - 360); mx0, my0 = px0 + 24, py0 + 58
            mcx, mcy = mx0 + ms / 2.0, my0 + ms / 2.0
            draw_rectangle(int(mx0), int(my0), int(ms), int(ms), C(6, 10, 16, 255))
            draw_rectangle_lines_ex(Rectangle(mx0, my0, ms, ms), 1, C(53, 230, 255, 70))
            _rx = _ad["receiver"]
            if _ad["bounds"] and _rx:
                _lat0, _lon0 = _rx
                _kx = 111.32 * max(0.05, math.cos(math.radians(_lat0)))
                _far = 1.0
                for _tr in _ad["tracks"].values():
                    for _p in _tr["pts"]:
                        _far = max(_far, abs((_p[2] - _lon0) * _kx), abs((_p[1] - _lat0) * 111.32))
                _ppk = (ms / 2.0 - 14) / (_far * 1.05)             # pixels per km
                def _xy(lat, lon):
                    return (mcx + (lon - _lon0) * _kx * _ppk, mcy - (lat - _lat0) * 111.32 * _ppk)
                begin_scissor_mode(int(mx0), int(my0), int(ms), int(ms))
                if loot_adsb_cov and _ad["max_km"] > 0:
                    # coverage: one shaded wedge per 10 degrees out to the farthest aircraft
                    # heard in that direction (empty directions stay empty) - drawn UNDER
                    # everything, so it never reads as a track
                    _cv = _ad["coverage"]; _n = len(_cv); _sw = 360.0 / _n
                    for _si in range(_n):
                        if _cv[_si] > 0:
                            draw_circle_sector(Vector2(mcx, mcy), _cv[_si] * _ppk, _si * _sw - 90.0,
                                               (_si + 1) * _sw - 90.0, 4, C(130, 220, 255, 26))
                for _rk in (5, 10, 25, 50, 100, 200, 400):          # range rings, labelled on the
                    if _rk * _ppk < ms / 2.0 - 4 and _rk * _ppk > 22:   # right edge, off the tracks
                        draw_circle_lines(int(mcx), int(mcy), _rk * _ppk, C(53, 230, 255, 45))
                        text(mono, "%d km" % _rk, int(mcx + _rk * _ppk + 3), int(mcy - 6), 11, C(53, 230, 255, 120))
                _hl = _ids[loot_adsb_sel] if 0 <= loot_adsb_sel < len(_ids) else None
                for _ic in sorted(_ids, key=lambda k: k == _hl):    # highlighted one drawn last
                    _pp = _ad["tracks"][_ic]["pts"]
                    _al = 255 if (_hl is None or _ic == _hl) else 60
                    _th = 3 if _ic == _hl else 2
                    for _a, _b2 in zip(_pp, _pp[1:]):
                        if _b2[0] - _a[0] > 600:                    # gap > 10 min: do not join
                            continue
                        _r, _g, _bb = _at.alt_color(_b2[3])
                        draw_line_ex(Vector2(*_xy(_a[1], _a[2])), Vector2(*_xy(_b2[1], _b2[2])), _th, C(_r, _g, _bb, _al))
                    if _pp:
                        _ex, _ey = _xy(_pp[-1][1], _pp[-1][2])
                        _r, _g, _bb = _at.alt_color(_pp[-1][3])
                        draw_circle(int(_ex), int(_ey), 3 if _ic != _hl else 5, C(_r, _g, _bb, _al))
                        if _hl is None or _ic == _hl:                # keep the label inside the map
                            _lb = _ad["tracks"][_ic]["callsign"] or _ic
                            _lw = tw(mono, _lb, 12)
                            _lx = _ex + 6 if _ex + 6 + _lw < mx0 + ms - 4 else _ex - 6 - _lw
                            _ly = min(max(_ey - 7, my0 + 18), my0 + ms - 16)
                            text(mono, _lb, int(_lx), int(_ly), 12, C(232, 240, 248, _al))
                        if len(_pp) > 1:                              # start of the track: a small ring
                            _sx, _sy = _xy(_pp[0][1], _pp[0][2])
                            _r0, _g0, _b0 = _at.alt_color(_pp[0][3])
                            draw_circle_lines(int(_sx), int(_sy), 3, C(_r0, _g0, _b0, _al))
                draw_circle(int(mcx), int(mcy), 5, SUCCESS)                 # receiver
                draw_circle_lines(int(mcx), int(mcy), 9, SUCCESS)
                end_scissor_mode()
                text(monoB, "N", int(mcx - 4), int(my0 + 4), 14, ICE)
            else:
                text(mono, "no positions in this session", int(mx0 + 20), int(my0 + 20), 16, DIM)
            # right column: stats, legend, aircraft list
            lx = int(mx0 + ms + 24); ly = int(my0)
            _src = {"gps-log": "GPS during the session", "wardriving": "session wardriving fixes",
                    "gps-now": "current GPS (no fix saved in the session)",
                    "estimate": "ESTIMATED from the aircraft heard"}.get(_ad["rx_source"], "unknown")
            text(monoB, "%d aircraft   %d positions" % (_ad["n_aircraft"], _ad["n_points"]), lx, ly, 16, HI); ly += 24
            _mc = _ad["tracks"].get(_ad["max_icao"], {}).get("callsign") or _ad["max_icao"]
            text(mono, "max range  %.0f km  (%s)" % (_ad["max_km"], _mc), lx, ly, 15, ICE); ly += 22
            text(mono, "receiver: " + _src, lx, ly, 13, WARN if _ad["rx_source"] in ("estimate", "gps-now") else DIM); ly += 20
            text(mono, "o start  * last position   shaded = farthest heard per 10 deg", lx, ly, 12, DIM); ly += 22
            for _ai, (_alab, _aft) in enumerate((("0", 0), ("10k", 10000), ("20k", 20000), ("30k", 30000), ("40k ft", 40000))):
                _r, _g, _bb = _at.alt_color(_aft)
                draw_rectangle(lx + _ai * 52, ly, 46, 6, C(_r, _g, _bb, 255))
                text(mono, _alab, lx + _ai * 52, ly + 9, 12, DIM)
            ly += 34
            _lrows = max(1, (py0 + ph - 60 - ly) // 22)
            _top = max(0, min(loot_adsb_sel - _lrows // 2, max(0, len(_ids) - _lrows)))
            for _li, _ic in enumerate(_ids[_top:_top + _lrows]):
                _tr = _ad["tracks"][_ic]; _pp = _tr["pts"]
                _sel = (_top + _li) == loot_adsb_sel
                if _sel:
                    draw_rectangle(lx - 6, ly - 2, pw - (lx - px0) - 18, 21, C(34, 211, 219, 35))
                _r, _g, _bb = _at.alt_color(max(p[3] for p in _pp))
                draw_rectangle(lx, ly + 6, 8, 8, C(_r, _g, _bb, 255))
                _dur = (_pp[-1][0] - _pp[0][0]) // 60 if _pp else 0
                text(mono, "%-8s %s  %5d ft  %3d pts  %2d min" % ((_tr["callsign"] or "-")[:8], _ic,
                     max(p[3] for p in _pp), len(_pp), _dur), lx + 16, ly, 14, HI if _sel else TXT)
                ly += 22
            if loot_adsb_msg:
                text(mono, loot_adsb_msg[:110], px0 + 24, py0 + ph - 50, 13, SUCCESS)
            text(mono, "[UP/DOWN] highlight aircraft   [C] coverage %s   [K] export KML   [ESC] back"
                 % ("on" if loot_adsb_cov else "off"), px0 + 24, py0 + ph - 28, 14, TEAL)

        # MeshCore REGION picker
        if mesh_screen:
            _mt = (mesh_pick == "meshtastic")
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 215))
            pw, ph = 720, 400; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            _acc = C(45, 220, 130) if _mt else PURPLE
            cyber_panel(px0, py0, pw, ph, _acc)
            gtext(dispB, "LORA MESH", px0 + 28, py0 + 20, 26, HI, 1)
            _act = bk.mesh_stack()
            _am = ("active: " + _act) if _act else "active: none"
            text(mono, _am, px0 + pw - 28 - tw(mono, _am, 14), py0 + 28, 14, DIM)
            draw_line(px0 + 24, py0 + 58, px0 + pw - 24, py0 + 58,
                      C(_acc.r, _acc.g, _acc.b, 120))
            _rows = ["stack", "region"] + (["preset", "name"] if _mt else []) + ["start"]
            mesh_sel = max(0, min(mesh_sel, len(_rows) - 1))
            if _mt:
                _reg = next((l for k, l in bk.mt_regions() if k == bk.mt_region_current()),
                            bk.mt_region_current())
                _pre = next((l for k, l in bk.mt_presets() if k == bk.mt_preset_current()),
                            bk.mt_preset_current())
            else:
                _reg = next((r[1] for r in bk.mc_regions() if r[0] == bk.mc_region_current()),
                            bk.mc_region_current())
                _pre = ""
            if mesh_name_edit:
                _nm = mesh_name_buf + ("_" if int(t * 2) % 2 == 0 else " ")
            else:
                _nm = bk.mt_name() or "(auto: your identity)"
            _vals = {"stack": "Meshtastic" if _mt else "MeshCore",
                     "region": _reg, "preset": _pre, "name": _nm}
            _rlab = {"stack": "STACK", "region": "REGION / BAND",
                     "preset": "PRESET", "name": "NAME"}
            ry = py0 + 80
            for ri, rk in enumerate(_rows):
                sel = (ri == mesh_sel)
                if sel:
                    draw_rectangle(px0 + 20, ry - 4, pw - 40, 34, C(34, 211, 219, 30))
                    draw_rectangle(px0 + 20, ry - 4, 3, 34, TEAL)
                if rk == "start":
                    gtext(dispB, "[ENTER]  START & CHAT", px0 + 36, ry,
                          22, SUCCESS if sel else TEAL, 1)
                else:
                    text(monoB, _rlab[rk], px0 + 36, ry, 16, TEAL)
                    if rk == "name":
                        _vt = _vals[rk]            # arrows don't apply; ENTER edits
                    else:
                        _vt = ("< %s >" % _vals[rk]) if sel else _vals[rk]
                    text(mono, str(_vt)[:40], px0 + 270, ry, 18, HI if sel else TXT)
                ry += 42
            _hint = ("[TYPE] name   [ENTER] save   [ESC] cancel" if mesh_name_edit
                     else "[UP/DOWN] row   [LEFT/RIGHT] change   [ENTER] edit/start   [ESC] back")
            text(mono, _hint, px0 + 28, py0 + ph - 30, 14, GHOST)

        if mc_region_screen:
            regs = bk.mc_regions()
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 210))
            pw, ph = 620, min(560, 140 + len(regs) * 34); px0 = (W - pw) // 2; py0 = (H - int(ph)) // 2
            cyber_panel(px0, py0, pw, int(ph), PURPLE)
            gtext(dispB, "MESHCORE  //  SELECT REGION", px0 + 28, py0 + 20, 24, HI, 1)
            curk = bk.mc_region_current()
            curl = next((l for k, l, f in regs if k == curk), curk)
            text(mono, "current: " + curl, px0 + 28, py0 + 54, 15, TEAL)
            draw_line(px0 + 24, py0 + 78, px0 + pw - 24, py0 + 78, C(178, 59, 214, 120))
            yy = py0 + 92
            for i, (k, lab, freq) in enumerate(regs):
                on = i == mc_region_sel; ry = yy + i * 34
                if on:
                    draw_rectangle(px0 + 24, ry - 4, pw - 48, 30, C(178, 59, 214, 44)); draw_rectangle(px0 + 24, ry - 4, 3, 30, PURPLE)
                text(monoB, lab[:34], px0 + 36, ry, 18, HI if on else TXT)
                text(mono, "%.3f MHz" % (freq / 1e6) if freq else "", px0 + pw - 170, ry, 15, DIM)
            text(mono, "[UP/DOWN] select    [ENTER] set & retune    [ESC] cancel", px0 + 28, py0 + int(ph) - 30, 15, GHOST)

        # CAPTURED DATA viewer — two tabs: Evil Twin / portal creds + WPA-sec cracks
        if data_screen:
            caps = bk.captures_all()
            crk = bk.cracked_wifi()
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 215))
            pw, ph = 960, 560; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, ICE)
            gtext(dispB, "CAPTURED DATA", px0 + 28, py0 + 20, 24, HI, 1)
            # tab pills (TAB switches)
            _tabs = [("EVIL TWIN  %d" % len(caps), ICE), ("WPA-SEC  %d" % len(crk), SUCCESS)]
            _tx = px0 + 28
            for _ti, (_tlab, _tcol) in enumerate(_tabs):
                _twd = tw(mono, _tlab, 15) + 26
                _on = _ti == data_tab
                draw_rectangle(_tx, py0 + 52, int(_twd), 26,
                               C(_tcol.r, _tcol.g, _tcol.b, 48 if _on else 16))
                if _on:
                    draw_rectangle(_tx, py0 + 52, int(_twd), 3, _tcol)
                text(mono, _tlab, _tx + 13, py0 + 56, 15, _tcol if _on else DIM)
                _tx += int(_twd) + 12
            text(mono, "[TAB] switch", px0 + pw - 28 - tw(mono, "[TAB] switch", 14), py0 + 58, 14, DIM)
            draw_line(px0 + 24, py0 + 88, px0 + pw - 24, py0 + 88, C(130, 220, 255, 120))
            ly = py0 + 102; lh = 26; maxrows = (ph - 150) // lh

            if data_tab == 0:                           # Evil Twin / portal credentials
                rows = caps
                if not rows:
                    text(mono, "no credentials captured yet - run Evil Twin / Portal",
                         px0 + 28, ly + 10, 18, DIM)
                data_scroll = max(0, min(data_scroll, max(0, len(rows) - maxrows)))
                for i, c in enumerate(rows[data_scroll:data_scroll + maxrows]):
                    ry = ly + i * lh
                    draw_circle(px0 + 34, ry + 9, 3, ICE)
                    text(mono, c[:108], px0 + 48, ry, 18, ICE)
            else:                                       # WPA-sec cracked WiFi
                rows = crk
                if not rows:
                    text(mono, "no cracked passwords yet - SYSTEM > Download WPA-SEC",
                         px0 + 28, ly + 10, 18, DIM)
                data_scroll = max(0, min(data_scroll, max(0, len(rows) - maxrows)))
                for i, e in enumerate(rows[data_scroll:data_scroll + maxrows]):
                    ry = ly + i * lh
                    draw_circle(px0 + 34, ry + 9, 3, SUCCESS)
                    text(monoB, e["ssid"][:28], px0 + 48, ry, 18, HI)
                    if e.get("n", 1) > 1:
                        text(mono, "x%d" % e["n"], px0 + 310, ry + 2, 13, DIM)
                    text(mono, e["password"][:40], px0 + 360, ry, 18, SUCCESS)
                    if e["ap_mac"]:
                        text(mono, e["ap_mac"], px0 + pw - 28 - tw(mono, e["ap_mac"], 13),
                             ry + 2, 13, DIM)
                if rows:
                    text(mono, "SSID", px0 + 48, py0 + 92, 12, DIM)
                    text(mono, "PASSWORD", px0 + 360, py0 + 92, 12, DIM)

            _n = len(caps) if data_tab == 0 else len(crk)
            if _n > maxrows:
                text(mono, "%d-%d / %d" % (data_scroll + 1, min(_n, data_scroll + maxrows), _n),
                     px0 + pw - 120, py0 + ph - 54, 13, DIM)
            text(mono, "[TAB] switch   [UP/DOWN] scroll   [P] / [ESC] close",
                 px0 + 28, py0 + ph - 30, 15, GHOST)

        # SCAN RESULTS modal — ordered, scrollable; pops after a SCAN-tab scan finishes
        if scan_modal:
            def sigcol(s):
                try:
                    v = int(str(s).replace("dBm", "").strip())
                except Exception:
                    return DIM
                return SUCCESS if v > -60 else (WARN if v > -75 else DANGER)
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 215))
            pw, ph = 960, 540; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            accent = TEAL if scan_modal == "wifi" else (MAGENTA if scan_modal == "airtag" else ICE)
            cyber_panel(px0, py0, pw, ph, accent)
            if scan_modal == "airtag":
                # CONTINUOUS scan -> live counters (no device list from the firmware)
                atg, stg = bk.airtags, bk.smarttags
                gtext(dispB, "AIRTAG SCAN", px0 + 28, py0 + 20, 26, MAGENTA, 1)
                text(mono, "continuous - Apple AirTags & Samsung SmartTags in range", px0 + 28, py0 + 56, 15, DIM)
                if int(t * 2) % 2 == 0:
                    draw_circle(px0 + pw - 150, py0 + 30, 6, SUCCESS)
                text(mono, "SCANNING", px0 + pw - 134, py0 + 22, 15, SUCCESS)
                draw_line(px0 + 24, py0 + 58, px0 + pw - 24, py0 + 58, C(accent.r, accent.g, accent.b, 120))
                cy = py0 + 150
                gtext(dispB, str(atg), px0 + 230 - tw(dispB, str(atg), 120) / 2, cy, 120, MAGENTA if atg else DIM, 1)
                text(monoB, "AirTags", px0 + 230 - tw(monoB, "AirTags", 22) / 2, cy + 150, 22, HI)
                gtext(dispB, str(stg), px0 + 650 - tw(dispB, str(stg), 120) / 2, cy, 120, ICE if stg else DIM, 1)
                text(monoB, "SmartTags", px0 + 650 - tw(monoB, "SmartTags", 22) / 2, cy + 150, 22, HI)
                if atg == 0 and stg == 0:
                    text(mono, "none detected yet - move around to sweep the area", px0 + 28, py0 + ph - 66, 18, WARN)
                else:
                    text(monoB, "%d tracker(s) in range" % (atg + stg), px0 + 28, py0 + ph - 66, 18, SUCCESS)
                text(mono, "[ESC] stop scan & close", px0 + 28, py0 + ph - 30, 15, GHOST)
            else:
                if scan_modal == "wifi":
                    def _rssi(n):
                        try:
                            return int(str(getattr(n, "rssi", "") or "0").replace("dBm", "").strip() or "0")
                        except Exception:
                            return -999
                    rows = sorted(bk.networks(), key=_rssi, reverse=True)
                    title = "WIFI SCAN  //  %d networks" % len(rows)
                    cols = (("SSID", px0 + 48), ("BSSID", px0 + 372), ("CH", px0 + 580),
                            ("SIGNAL", px0 + 650), ("SECURITY", px0 + 760))
                else:
                    def _r2(d):
                        try:
                            return int(d[1])
                        except Exception:
                            return -999
                    rows = sorted(bk.ble_devices, key=_r2, reverse=True)
                    title = "BLE SCAN  //  %d devices" % len(rows)
                    cols = (("NAME", px0 + 48), ("MAC", px0 + 430), ("SIGNAL", px0 + 660), ("TAG", px0 + 800))
                gtext(dispB, title, px0 + 28, py0 + 20, 24, HI, 1)
                draw_line(px0 + 24, py0 + 58, px0 + pw - 24, py0 + 58, C(accent.r, accent.g, accent.b, 120))
                hy = py0 + 66
                for lab2, cx in cols:
                    text(mono, lab2, cx, hy, 14, DIM)
                draw_line(px0 + 24, hy + 20, px0 + pw - 24, hy + 20, EDGE)
                ly = hy + 30; lh = 26; maxrows = (ph - 150) // lh
                if not rows:
                    text(mono, "no results yet - is the ESP32 connected / scan still running?", px0 + 48, ly + 10, 18, WARN)
                scan_modal_scroll = max(0, min(scan_modal_scroll, max(0, len(rows) - maxrows)))
                for i, r in enumerate(rows[scan_modal_scroll:scan_modal_scroll + maxrows]):
                    ry = ly + i * lh
                    if scan_modal == "wifi":
                        ssid = str(getattr(r, "ssid", "") or "<hidden>")
                        auth = str(getattr(r, "auth", "") or "")
                        secured = any(s in auth.upper() for s in ("WPA", "WEP", "PSK", "SAE"))
                        draw_circle(px0 + 34, ry + 9, 3, DANGER if secured else SUCCESS)
                        text(monoB, ssid[:36], px0 + 48, ry, 16, HI)
                        text(mono, str(getattr(r, "bssid", ""))[:20], px0 + 372, ry, 14, DIM)
                        text(mono, str(getattr(r, "channel", ""))[:4], px0 + 580, ry, 15, TXT)
                        text(mono, str(getattr(r, "rssi", ""))[:6], px0 + 650, ry, 15, sigcol(getattr(r, "rssi", "")))
                        text(mono, ("OPEN" if not secured else auth[:18]), px0 + 760, ry, 14, WARN if not secured else TXT)
                    else:
                        mac, rssi, name, tag = (tuple(r) + ("", "", "", ""))[:4]
                        draw_circle(px0 + 34, ry + 9, 3, MAGENTA if tag else ICE)
                        text(monoB, (str(name)[:42] or "?"), px0 + 48, ry, 16, HI)
                        text(mono, str(mac)[:20], px0 + 430, ry, 14, DIM)
                        text(mono, str(rssi)[:6], px0 + 660, ry, 15, sigcol(rssi))
                        if tag:
                            text(monoB, str(tag)[:16], px0 + 800, ry, 14, MAGENTA)
                if len(rows) > maxrows:
                    text(mono, "%d-%d / %d" % (scan_modal_scroll + 1, min(len(rows), scan_modal_scroll + maxrows), len(rows)),
                         px0 + pw - 130, py0 + ph - 54, 13, DIM)
                text(mono, "[UP/DOWN] scroll    [ESC] close", px0 + 28, py0 + ph - 30, 15, GHOST)

        # EXTRAS: a third-party mini-app owns a viewport; the host paints the chrome,
        # scissors the viewport, drives on_frame, and owns ESC. A faulted extra shows
        # its reason instead of drawing.
        if comp_screen == "extra" and active_extra is not None:
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64):
                draw_line(gx, 46, gx, H, C(178, 59, 214, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, PURPLE)
            gtext(dispB, active_extra.meta.name.upper(), 24, 10, 24, HI, 1)
            text(mono, "by " + (active_extra.meta.author or "?"), W - 340, 16, 15, DIM)
            if getattr(active_extra.meta, "id", "") == "hub":
                _hw = "INSTALL & RUN ONLY WHAT YOU KNOW AND TRUST"
                text(monoB, _hw, (W - tw(monoB, _hw, 15)) // 2, 16, 15, WARN)
            if getattr(active_extra, "builtin", False):
                pass                       # hub browser / consent card: the game's own screens
            elif getattr(active_extra, "sandboxed", False):
                text(monoB, "SANDBOXED", W - 520, 16, 15, SUCCESS)
            else:
                text(monoB, "TRUSTED (in-process)", W - 660, 16, 15, WARN)
            _vx, _vy, _vw, _vh = 24, 64, W - 48, H - 64 - 38
            if active_extra.state == "faulted":
                text(monoB, "EXTRA CRASHED", _vx, _vy + 24, 22, DANGER)
                text(mono, (active_extra.error or "")[:120], _vx, _vy + 58, 15, TXT)
                text(mono, "disabled for this session - fix it and relaunch", _vx, _vy + 84, 14, DIM)
            else:
                _extra_origin[0] = _vx; _extra_origin[1] = _vy
                _extra_view[0] = _vw; _extra_view[1] = _vh
                begin_scissor_mode(_vx, _vy, _vw, _vh)
                active_extra.frame(get_frame_time())
                end_scissor_mode()
                _nxt = getattr(active_extra, "next_host", None)
                if _nxt is not None:       # consent given -> swap in the real (sandboxed) host
                    active_extra = _nxt
                    active_extra.open()
            text(mono, "[ESC] back to EXTRAS", 24, H - 30, 14, GHOST)

        # COMPANION screens (Flipper / SubGHz / MITM / Watch / Update Game)
        if comp_screen and comp_screen != "extra":
            CT = {"flipper": ("FLIPPER ZERO", WARN), "subghz": ("MONSTER RF  //  SUB-GHZ", C(120, 220, 120)),
                  "mitm": ("MITM  //  ARP SPOOF", DANGER), "watch": ("PIPBOY WATCH", TEAL),
                  "gameupd": ("UPDATE GAME  //  OTA", MAGENTA)}
            ttl, tcol = CT[comp_screen]
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, tcol)
            gtext(dispB, ttl, 24, 10, 24, HI, 1)
            log = []
            mrows = []        # (label,) selectable menu rows
            selmax = 0
            if comp_screen == "flipper":
                conn = bk.flipper_connected(); nm, fw = bk.flipper_info()
                text(monoB, ("ONLINE %s %s" % (nm, fw)) if conn else "OFFLINE - connect a Flipper", W - 420, 14, 16, SUCCESS if conn else DANGER)
                mrows = ["SubGHz Scan 433 MHz", "SubGHz Scan 868 MHz", "SubGHz RAW 433", "Device Info", "NFC Detect"]
                log = bk.flipper_log
            elif comp_screen == "subghz":
                conn = bk.subghz_connected(); rssi = bk.subghz_rssi()
                text(monoB, "CONNECTED" if conn else "no CC1101 board", W - 300, 14, 16, SUCCESS if conn else DANGER)
                # RSSI meter
                text(mono, "RSSI", 24, 64, 15, DIM)
                segs = 28; lvl = int(max(0, min(segs, (rssi + 120) * segs / 90)))
                for s in range(segs):
                    col = (SUCCESS if s < segs * 0.5 else WARN if s < segs * 0.8 else DANGER) if s < lvl else C(30, 40, 52, 255)
                    draw_rectangle(80 + s * 18, 62, 14, 18, col)
                text(monoB, "%d dBm" % rssi, 80 + segs * 18 + 16, 62, 16, HI)
                text(mono, "catches: %d" % bk.subghz_catches(), W - 200, 64, 15, TEAL)
                mrows = ["Read / Scan", "Record RAW", "Freq Analyzer", "Save to SD", "Status", "Stop"]
                log = bk.subghz_log
            elif comp_screen == "mitm":
                run = bk.mitm_running()
                text(monoB, ("RUNNING  packets:%d" % bk.mitm_packets()) if run else "idle", W - 320, 14, 16, DANGER if run else DIM)
                text(mono, "ARP-spoof the LAN gateway, capture DNS/HTTP/cleartext creds -> loot/mitm/", 24, 62, 15, DIM)
                text(mono, "interfaces ([S] start on selected, [X] stop):", 24, 88, 15, TXT)
                ifs = bk.mitm_ifaces()
                for i, (nm, desc) in enumerate(ifs[:6]):
                    on = i == comp_sel; ry = 112 + i * 28
                    if on: draw_rectangle(24, ry - 3, 600, 24, C(255, 59, 48, 36))
                    text(monoB, nm, 32, ry, 17, HI if on else TXT); text(mono, str(desc)[:40], 200, ry, 15, DIM)
                log = [m for (ts, m, kd) in bk.term if "mitm" in m.lower()]
            elif comp_screen == "watch":
                log = bk.watch_log
                if bk.watch_pin_requested():
                    text(monoB, "ENTER PIN FROM WATCH DISPLAY", W // 2 - 180, 120, 20, WARN)
                    pin = watch_pin + ("_" if int(t * 2) % 2 == 0 else " ")
                    text(monoB, pin, W // 2 - tw(monoB, pin, 34) / 2, 160, 34, HI)
                    text(mono, "[0-9] type   [ENTER] confirm   [ESC] back", W // 2 - 160, 210, 14, GHOST)
                elif bk.watch_connected():
                    text(monoB, "CONNECTED  batt:%d%%" % bk.watch_battery(), W - 320, 14, 16, SUCCESS)
                    mrows = ["Status", "Compass", "Haptic", "WiFi Recon", "BLE Recon", "NFC Scan", "LoRa Start", "Disconnect"]
                else:
                    text(monoB, ("scanning..." if bk.watch_scanning() else "not connected"), W - 260, 14, 16, WARN)
                    text(mono, "[R] rescan   [UP/DOWN] select   [ENTER] connect", 24, 62, 15, GHOST)
                    res = bk.watch_results()
                    for i, r in enumerate(res[:10]):
                        on = i == comp_sel; ry = 92 + i * 28
                        nm = (r[1] if isinstance(r, (list, tuple)) and len(r) > 1 else r.get("name", "?")) if not isinstance(r, str) else r
                        ad = (r[0] if isinstance(r, (list, tuple)) else r.get("address", "")) if not isinstance(r, str) else r
                        if on: draw_rectangle(24, ry - 3, 700, 24, C(34, 211, 219, 40))
                        text(monoB, str(nm)[:22], 32, ry, 17, HI if on else TXT); text(mono, str(ad), 360, ry, 15, DIM)
            elif comp_screen == "gameupd":
                remote, busy, glog = bk.game_update_info()
                avail = bk.game_update_available()
                text(mono, "installed: v%s" % bk.game_version(), 24, 64, 16, TEAL)
                text(mono, "remote:  %s" % (remote or "checking..."), 24, 90, 16,
                     MAGENTA if avail else (SUCCESS if "up to date" in remote else DIM))
                lat = bk.game_update_latest()
                if avail and lat:
                    text(mono, "new:  %s" % lat, 24, 114, 15, ICE)
                if busy:
                    text(monoB, "installing... do not power off", 24, 140, 16, ICE)
                elif avail:
                    text(monoB, "[ENTER] download + install + restart", 24, 140, 16, SUCCESS)
                else:
                    text(mono, "[ENTER] re-check for updates", 24, 140, 16, DIM)
                # changelog: every new remote commit, newest first (fits above the log panel)
                _clog = bk.game_changelog()
                if avail and _clog:
                    text(monoB, "CHANGELOG  (%d new)" % len(_clog), 24, 172, 14, MAGENTA)
                    _cy = 194
                    for _cl in _clog:
                        if _cy > H - 252:
                            text(mono, "...", 32, _cy, 15, DIM); break
                        text(mono, "- " + _cl[:112], 32, _cy, 15, TXT)
                        _cy += 20
                log = glog
            # menu rows (flipper/subghz/watch-connected)
            if mrows:
                for i, mr in enumerate(mrows):
                    on = i == comp_sel; ry = 100 + i * 32
                    if on: draw_rectangle(24, ry - 4, 420, 28, C(tcol.r, tcol.g, tcol.b, 40)); draw_rectangle(24, ry - 4, 3, 28, tcol)
                    text(monoB, mr, 40, ry, 18, HI if on else TXT)
            # log panel (bottom)
            ly0 = H - 230; log = log[-11:]
            draw_line(24, ly0 - 8, W - 24, ly0 - 8, EDGE)
            text(mono, "OUTPUT", 24, ly0 - 26, 13, GHOST)
            for i, m in enumerate(log):
                text(mono, str(m)[:120], 24, ly0 + i * 18, 15, TXT)
            text(mono, "[UP/DOWN] select   [ENTER] run   [ESC] close", 24, H - 30, 14, GHOST)

        # FLASH ESP32 board picker
        if flash_screen:
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 210))
            boards = bk.flash_boards()
            busy = bk.flash_busy()
            pw, ph = 1060, 580; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, WARN)
            gtext(dispB, "FLASH ESP32  //  SELECT BOARD", px0 + 28, py0 + 20, 24, HI, 1)
            text(mono, "firmware: LOCOSP/projectZero latest - DO NOT UNPLUG while flashing",
                 px0 + 28, py0 + 54, 14, DANGER)
            if not busy:
                yy = py0 + 90
                for i, (k, lab) in enumerate(boards):
                    on = i == flash_sel; ry = yy + i * 36
                    if on:
                        draw_rectangle(px0 + 24, ry - 4, pw - 48, 32, C(255, 176, 32, 40)); draw_rectangle(px0 + 24, ry - 4, 3, 32, WARN)
                    draw_rectangle(px0 + 32, ry, 30, 22, WARN if on else C(54, 80, 106, 255))
                    text(monoB, str(i + 1), px0 + 32 + 10, ry + 2, 16, VOID if on else HI)
                    text(monoB, lab, px0 + 74, ry + 1, 18, HI if on else TXT)
                dft = bk.flash_default_board()
                lvl, lmsg = bk.flash_result()
                if lmsg:      # outcome of the last flash attempt (ok / warning / failure)
                    lcol = SUCCESS if lvl == "ok" else (WARN if lvl == "warn" else DANGER)
                    mm = "LAST FLASH: " + lmsg
                    while mm and tw(mono, mm, 15, 0) > pw - 56:
                        mm = mm[:-4]
                    text(mono, mm, px0 + 28, py0 + ph - 80, 15, lcol)
                text(mono, "detected board (by port): %s" % dft, px0 + 28, py0 + ph - 54, 14, DIM)
                text(mono, "[UP/DOWN] select    [ENTER] flash    [ESC] cancel", px0 + 28, py0 + ph - 30, 15, GHOST)
            else:
                fstg, fpct = bk.flash_progress()
                flbl = {"preflight": "PREFLIGHT CHECKS", "download": "DOWNLOAD + VERIFY",
                        "connect": "CONNECTING TO ESP32", "write": "WRITING FIRMWARE",
                        "boot": "REBOOT + CONFIRM VERSION"}.get(fstg, "WORKING")
                text(monoB, "FLASHING  //  " + flbl, px0 + 28, py0 + 92, 20, TEAL)
                fbx, fby, fbw, fbh = px0 + 28, py0 + 124, pw - 56, 14
                draw_rectangle(fbx, fby, fbw, fbh, C(16, 22, 32, 255))
                if fpct >= 0:
                    draw_rectangle(fbx, fby, int(fbw * min(100, fpct) / 100), fbh, TEAL)
                    text(monoB, "%d%%" % fpct, fbx + fbw - 52, py0 + 96, 16, HI)
                else:          # indeterminate: a block sweeping across the bar
                    fseg = int(fbw * 0.18)
                    foff = int((t * 260) % (fbw + fseg)) - fseg
                    fx0 = max(fbx, fbx + foff); fx1 = min(fbx + fbw, fbx + foff + fseg)
                    if fx1 > fx0:
                        draw_rectangle(int(fx0), fby, int(fx1 - fx0), fbh, TEAL)
                draw_rectangle_lines_ex(Rectangle(fbx, fby, fbw, fbh), 1, EDGE)
                flog = [m for (ts, m, kd) in bk.term if "FLASH" in m or "esptool" in m.lower()][-14:]
                avail = pw - 56        # clip each line to the panel width (fonts are UISCALE-scaled)
                for i, m in enumerate(flog):
                    mm = m
                    while mm and tw(mono, mm, 15, 0) > avail:
                        mm = mm[:-4]
                    text(mono, mm, px0 + 28, py0 + 152 + i * 24, 15, SUCCESS if "complete" in m.lower() else TXT)
                text(mono, "ESC to close when done", px0 + 28, py0 + ph - 30, 14, DIM)

        # WHITELIST editor
        if radio_screen:
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, TEAL)
            gtext(dispB, "RADIO", 24, 10, 24, HI, 1)
            text(mono, "// " + (bk.radio_summary() or "no radio"),
                 24 + tw(dispB, "RADIO", 24, 1) + 16, 18, 16, DIM)
            _rr = bk.radio_rows()
            if not bk.radio_both():
                text(mono, "One radio present - everything uses it automatically. The matrix",
                     40, 76, 18, DIM)
                text(mono, "matters when both an ESP32 and a host WiFi card are connected.",
                     40, 100, 18, DIM)
            y0 = 140
            text(mono, "FUNCTION", 60, y0, 16, C(120, 140, 160, 255))
            for ci, cname in enumerate(("AUTO", "ESP32", "AC1200")):
                text(mono, cname, 360 + ci * 150, y0, 16, C(120, 140, 160, 255))
            text(mono, "USES", 360 + 3 * 150 + 30, y0, 16, C(120, 140, 160, 255))
            for i, (cap, lab, choice, resolved, valid) in enumerate(_rr):
                ry = y0 + 34 + i * 40
                if i == radio_sel:
                    draw_rectangle(44, ry - 4, W - 88, 34, C(34, 211, 219, 28))
                    draw_rectangle(44, ry - 4, 3, 34, TEAL)
                text(monoB, lab, 60, ry, 20, HI if i == radio_sel else TXT)
                for ci, cval in enumerate(("auto", "esp32", "host")):
                    cx = 360 + ci * 150
                    avail = (cval == "auto" or (cval == "esp32" and "esp32" in valid) or (cval == "host" and "host" in valid))
                    on = (choice == cval)
                    col = (SUCCESS if on else (C(90, 110, 130, 255) if avail else C(50, 60, 72, 255)))
                    if on:
                        draw_rectangle(cx - 8, ry - 2, 128, 28, C(col.r, col.g, col.b, 40))
                        draw_rectangle_lines_ex(Rectangle(cx - 8, ry - 2, 128, 28), 1, col)
                    text(mono, "o" if on else ("-" if avail else "x"), cx, ry, 20, col)
                    text(mono, {"auto": "auto", "esp32": "ESP32", "host": "AC1200"}[cval], cx + 20, ry + 2, 16, col)
                rc = SUCCESS if resolved == "esp32" else WARN if resolved == "host" else DANGER
                text(monoB, resolved.upper() or "NONE", 360 + 3 * 150 + 30, ry, 18, rc)
            text(mono, "[UP/DOWN] function   [LEFT/RIGHT/ENTER] change   [R] re-detect   [ESC] back",
                 40, H - 34, 16, TEAL)
            
        if wl_screen:
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            entries = bk.wl_entries()
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, WARN)
            gtext(dispB, "WHITELIST  //  %d entries" % len(entries), 24, 10, 24, HI, 1)
            text(mono, "MACs here are IGNORED by attacks/deauth", 24 + tw(dispB, "WHITELIST  //  %d entries" % len(entries), 24, 1) + 20, 18, 15, DIM)
            if not wl_scan:
                for cx, lb in ((24, "TYPE"), (140, "MAC"), (420, "NAME")):
                    text(mono, lb, cx, 64, 14, DIM)
                draw_line(24, 84, W - 24, 84, EDGE)
                if not entries:
                    text(mono, "whitelist is empty - press [S] to scan & add", 24, 100, 18, WARN)
                for i, (et, mac, nm) in enumerate(entries[:16]):
                    ry = 94 + i * 28; on = i == wl_sel
                    if on:
                        draw_rectangle(24, ry - 3, W - 48, 24, C(255, 176, 32, 36)); draw_rectangle(24, ry - 3, 3, 24, WARN)
                    text(monoB, (et or "?").upper(), 24 + 8, ry, 17, TEAL if et == "wifi" else MAGENTA)
                    text(mono, mac or "?", 140, ry, 17, HI if on else TXT)
                    text(mono, (nm or "")[:40], 420, ry, 17, HI if on else TXT)
                text(mono, "[UP/DOWN] select    [D] delete    [S] scan & add    [ESC] close",
                     24, H - 32, 15, GHOST)
            else:
                wln = bk.networks()
                text(monoB, "SCAN & ADD  -  pick a network to whitelist", 24, 64, 18, WARN)
                draw_line(24, 90, W - 24, 90, EDGE)
                if not wln:
                    text(mono, "no networks - run a WiFi scan first (ESC back)", 24, 104, 18, WARN)
                for i, n in enumerate(wln[:16]):
                    ry = 100 + i * 28; on = i == wl_scan_sel
                    if on:
                        draw_rectangle(24, ry - 3, W - 48, 24, C(34, 211, 219, 40)); draw_rectangle(24, ry - 3, 3, 24, TEAL)
                    text(monoB, (n.ssid or "<hidden>")[:30], 32, ry, 17, HI if on else TXT)
                    text(mono, n.bssid or "?", 420, ry, 16, DIM)
                    text(mono, "%sdBm ch%s" % (n.rssi or "?", n.channel or "?"), 720, ry, 16, DIM)
                text(mono, "[UP/DOWN] select    [ENTER] add to whitelist    [ESC] back",
                     24, H - 32, 15, GHOST)

        # WDGWARS PROXIMITY RADAR screen
        if pair_screen:
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 245))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, TEAL)
            gtext(dispB, "WDGWARS  //  PAIR DEVICE", 24, 10, 24, HI, 1)
            _ps = bk.pair_status(); _cx = W // 2
            if _ps in ("", "requesting"):
                _t1 = "REQUESTING CODE..."
                text(monoB, _t1, _cx - tw(monoB, _t1, 22) / 2, 300, 22, ICE)
            elif _ps == "waiting":
                _t1 = "On another device open:"
                text(mono, _t1, _cx - tw(mono, _t1, 18) / 2, 150, 18, DIM)
                _t2 = "wdgwars.pl/profile  ->  API keys  ->  enter this code"
                text(monoB, _t2, _cx - tw(monoB, _t2, 18) / 2, 178, 18, TEAL)
                _code = bk.pair_code()
                text(dispB, _code, _cx - tw(dispB, _code, 64) / 2, 250, 64, SUCCESS)
                _left = bk.pair_seconds_left()
                _t3 = "expires in %d:%02d   waiting for confirmation..." % (_left // 60, _left % 60)
                text(mono, _t3, _cx - tw(mono, _t3, 16) / 2, 362, 16, WARN)
            elif _ps == "done":
                text(monoB, "LINKED", _cx - tw(monoB, "LINKED", 28) / 2, 280, 28, SUCCESS)
                _u = bk.pair_user() or "your account"
                _t4 = "connected as %s — key saved to secrets.conf" % _u
                text(mono, _t4, _cx - tw(mono, _t4, 18) / 2, 322, 18, ICE)
            elif _ps == "expired":
                text(monoB, "CODE EXPIRED", _cx - tw(monoB, "CODE EXPIRED", 24) / 2, 290, 24, WARN)
                _t5 = "select ADDONS > WDGWars Pair to try again"
                text(mono, _t5, _cx - tw(mono, _t5, 16) / 2, 326, 16, DIM)
            else:
                text(monoB, "PAIRING FAILED", _cx - tw(monoB, "PAIRING FAILED", 24) / 2, 290, 24, DANGER)
                _t6 = "check network / server, then retry"
                text(mono, _t6, _cx - tw(mono, _t6, 16) / 2, 326, 16, DIM)
            text(mono, "[ESC] close", 24, H - 32, 14, GHOST)

        if cluster_screen:
            _cn = bk.cluster_nodes(); _sm = bk.cluster_summary(); _cjoin = bk.cluster_joins()
            _active = _sm["active"]
            # ---------- backdrop + header ----------
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, TEAL)
            gtext(dispB, "CLUSTER  //  NODE FLEET CORE", 24, 10, 24, HI, 1)
            _cstat = "CORE ACTIVE" if _active else "CORE OFF"
            _spulse = 150 + int(90 * (0.5 + 0.5 * math.sin(t * 3))) if _active else 90
            draw_circle(W - 210, 23, 5, C(57, 255, 136, _spulse) if _active else DIM)
            text(monoB, _cstat, W - 20 - tw(monoB, _cstat, 16), 14, 16,
                 SUCCESS if _active else DIM)

            # ---------- summary stat tiles ----------
            def _tile(x, w, big, lab, col):
                draw_rectangle_rounded(Rectangle(x, 56, w, 60), 0.18, 6, C(12, 18, 27, 255))
                draw_rectangle(x, 56, 3, 60, col)
                gtext(dispB, big, x + 16, 60, 30, col, 1)
                text(mono, lab, x + 16, 97, 13, DIM)
            _tiles = [
                ("%d/%d" % (_sm["online"], _sm["total"]), "NODES ONLINE",
                 SUCCESS if _sm["online"] else DIM),
                ("%d" % _sm["host_rate"], "FLEET REC / MIN", TEAL),
                ("%d" % _sm["recs"], "RECORDS THIS RUN", ICE),
                ("%d" % _sm.get("wifi_nets", 0), "WIFI NETS", HI),
                ("%d" % _sm.get("ble_public", 0),
                 "BLE PUBLIC  (+%d rnd)" % _sm.get("ble_random", 0), PURPLE),
            ]
            _tx, _twd = 24, 232
            for _b, _l, _c in _tiles:
                _tile(_tx, _twd, _b, _l, _c); _tx += _twd + 12

            # ---------- join request banner ----------
            _top = 132
            if _cjoin:
                _jp = 120 + int(110 * (0.5 + 0.5 * math.sin(t * 5)))
                draw_rectangle_rounded(Rectangle(24, _top, W - 48, 40), 0.3, 8, C(46, 38, 8, 255))
                draw_rectangle(24, _top, 4, 40, WARN)
                _jt = ("NEW NODE REQUESTS TO JOIN   %s   —  press [A] to adopt into the fleet"
                       % _cjoin[0])
                text(monoB, _jt, 44, _top + 11, 17, C(WARN.r, WARN.g, WARN.b, _jp))
                _top += 50

            # ---------- helpers ----------
            def _ncol(n):
                if n.get("stale"): return C(120, 134, 150, 255)
                if n.get("full"): return DANGER
                if (n.get("role") or "").lower() == "ble": return PURPLE
                if not n.get("by_core", True): return WARN
                return SUCCESS
            def _upfmt(s):
                try: s = int(s)
                except (TypeError, ValueError): return "--"
                if s < 60: return "%ds" % s
                if s < 3600: return "%dm%02d" % (s // 60, s % 60)
                return "%dh%02d" % (s // 3600, (s % 3600) // 60)
            def _ival(v):
                try: return int(v)
                except (TypeError, ValueError): return 0

            # ---------- fleet radar (left) ----------
            _rcx, _rcy, _rr = 310, _top + 232, 148
            _NCH = 40
            draw_rectangle_rounded(Rectangle(24, _top, 548, 468), 0.04, 6, C(8, 12, 19, 255))
            text(mono, "SPECTRUM COVERAGE", 40, _top + 12, 14, DIM)
            # faint full-ring baseline (the whole spectrum) + uncovered gaps show through
            draw_ring(Vector2(_rcx, _rcy), _rr - 30, _rr - 18, 0, 360, 80, C(30, 40, 52, 255))
            # rotating sweep
            _sw = (t * 55) % 360
            draw_ring(Vector2(_rcx, _rcy), _rr - 30, _rr - 18, _sw, _sw + 26, 12, C(53, 230, 255, 60))
            # band orientation labels (2.4 GHz = ch idx 0-13, 5 GHz = 14-39)
            for _blab, _bctr in (("2.4G", 7), ("5G", 26)):
                _ba = math.radians(-90 + _bctr / _NCH * 360)
                _blx = _rcx + (_rr - 48) * math.cos(_ba)
                _bly = _rcy + (_rr - 48) * math.sin(_ba)
                text(mono, _blab, _blx - tw(mono, _blab, 11) / 2, _bly - 6, 11, C(96, 110, 126, 255))
            if not _cn and not _cjoin:
                _m = "WAITING FOR CLUSTER CORE" if _active else "CLUSTER CORE OFF"
                text(monoB, _m, _rcx - tw(monoB, _m, 20) / 2, _rcy - 10, 20, TEAL)
                _sub = ("power the XIAO node fleet" if _active
                        else "SNIFF > Cluster Wardrive")
                text(mono, _sub, _rcx - tw(mono, _sub, 14) / 2, _rcy + 16, 14, DIM)
            # per-node coverage arcs + nodes. WiFi nodes sit at the centre of the
            # spectrum slice they cover (so the ring reads as real coverage, gaps and
            # all); BLE/VITA nodes carry no wifi slice, so they park in the widest free
            # arc at the bottom-left, clear of the coverage spokes.
            _bi = 0
            for _n in _cn:
                _c = _ncol(_n)
                _role = (_n.get("role") or "").lower()
                if _role in ("ble", "vita"):
                    _ang = 198.0 + _bi * 34; _bi += 1
                else:
                    _lo, _hi = _n.get("lo", 0), _n.get("hi", 0)
                    a0 = -90 + _lo / _NCH * 360; a1 = -90 + (_hi + 1) / _NCH * 360
                    _af = 90 if _n.get("stale") else 220
                    draw_ring(Vector2(_rcx, _rcy), _rr - 30, _rr - 18, a0, a1, 24,
                              C(_c.r, _c.g, _c.b, _af))
                    _ang = (a0 + a1) / 2
                _rad = math.radians(_ang)
                _nx = _rcx + _rr * math.cos(_rad); _ny = _rcy + _rr * math.sin(_rad)
                _fade = 150 if _n.get("stale") else 255
                draw_line_ex(Vector2(_rcx, _rcy), Vector2(_nx, _ny), 1.5,
                             C(_c.r, _c.g, _c.b, 40 if _n.get("stale") else 90))
                # activity pulse sized by rec/min
                _rm = _ival(_n.get("rec_min"))
                if _rm and not _n.get("stale"):
                    _pr = 14 + 8 * (0.5 + 0.5 * math.sin(t * 4 + _n.get("idx", 0)))
                    draw_ring(Vector2(_nx, _ny), _pr, _pr + 1.5, 0, 360, 24,
                              C(_c.r, _c.g, _c.b, min(120, 20 + _rm)))
                draw_poly(Vector2(_nx, _ny), 6, 12, t * 12 + _n.get("idx", 0) * 20,
                          C(_c.r, _c.g, _c.b, _fade))
                draw_poly_lines_ex(Vector2(_nx, _ny), 6, 15, t * 12 + _n.get("idx", 0) * 20,
                                   2, C(_c.r, _c.g, _c.b, 150))
                text(monoB, _n.get("mac", "")[-4:],
                     _nx - tw(monoB, _n.get("mac", "")[-4:], 13) / 2, _ny - 7, 13, HI)
                _tag = "BLE" if _role == "ble" else "#%s" % _n.get("idx", "?")
                text(mono, _tag, _nx - tw(mono, _tag, 11) / 2, _ny + 18, 11, _c)
                if _n.get("stale"):
                    text(mono, "STALE", _nx - tw(mono, "STALE", 10) / 2, _ny + 30, 10, WARN)
            # pending joiner on the ring's edge
            for _ji, _jm in enumerate(_cjoin):
                _ja = -90 + _ji * 40
                _jrad = math.radians(_ja); _jR = _rr + 6
                _jx = _rcx + _jR * math.cos(_jrad); _jy = _rcy + _jR * math.sin(_jrad)
                _jp2 = 120 + int(120 * (0.5 + 0.5 * math.sin(t * 5)))
                draw_poly_lines_ex(Vector2(_jx, _jy), 6, 14, t * 40, 2,
                                   C(WARN.r, WARN.g, WARN.b, _jp2))
                text(monoB, "?", _jx - 4, _jy - 8, 18, WARN)
            # core glyph
            _corp = 150 + int(80 * (0.5 + 0.5 * math.sin(t * 2.5)))
            draw_ring(Vector2(_rcx, _rcy), 40, 42, 0, 360, 48, C(53, 230, 255, _corp))
            draw_circle(_rcx, _rcy, 30, C(10, 26, 34, 255))
            draw_poly_lines_ex(Vector2(_rcx, _rcy), 6, 26, t * 6, 2, TEAL)
            gtext(dispB, "CORE", _rcx - tw(dispB, "CORE", 17) / 2, _rcy - 16, 17, HI, 1)
            _hg = "GPS" if _sm["gps"] else "NOGPS"
            text(mono, _hg, _rcx - tw(mono, _hg, 11) / 2, _rcy + 4, 11,
                 SUCCESS if _sm["gps"] else WARN)

            # ---------- node health table (right) ----------
            _htx = 508
            _tw = W - (_htx - 8) - 16             # table width (right edge ~1264)
            draw_rectangle_rounded(Rectangle(_htx - 8, _top, _tw, 468), 0.04, 6, C(8, 12, 19, 255))
            _cols = [(_htx, "#"), (_htx + 40, "NODE"), (_htx + 116, "ROLE"),
                     (_htx + 188, "SLICE"), (_htx + 290, "REC/MIN"),
                     (_htx + 410, "DEDUP"), (_htx + 536, "TEMP"),
                     (_htx + 598, "UP"), (_htx + 676, "DRP"), (_htx + 708, "STATE")]
            for _cx, _lab in _cols: text(mono, _lab, _cx, _top + 12, 15, DIM)
            draw_line(_htx, _top + 38, _htx - 8 + _tw - 8, _top + 38, EDGE)
            if not _cn:
                _wm = "no nodes checked in yet" if _active else "core is off"
                text(mono, _wm, _htx, _top + 66, 18, DIM)
            # scrolling window: keep the selected row in view, handle any node count
            _rowh = 40
            _vis = max(1, (468 - 52) // _rowh)
            fleet_sel = max(0, min(fleet_sel, max(0, len(_cn) - 1)))
            _first = (max(0, min(fleet_sel - _vis // 2, len(_cn) - _vis))
                      if len(_cn) > _vis else 0)
            _rowx, _roww = _htx - 4, _tw - 8
            for _vi, _n in enumerate(_cn[_first:_first + _vis]):
                _i = _first + _vi
                _ry = _top + 48 + _vi * _rowh
                _c = _ncol(_n)
                if _vi % 2 == 0:
                    draw_rectangle(_rowx, _ry - 5, _roww, _rowh - 4, C(255, 255, 255, 5))
                if _i == fleet_sel:             # operator's selection (channel-pin cursor)
                    draw_rectangle(_rowx, _ry - 5, _roww, _rowh - 4, C(HI.r, HI.g, HI.b, 34))
                text(monoB, str(_n.get("idx", "?")), _htx, _ry, 18, HI)
                text(monoB, _n.get("mac", "")[-4:], _htx + 40, _ry, 18, _c)
                text(mono, (_n.get("role") or "").upper()[:4], _htx + 116, _ry, 16, _c)
                _pin = _n.get("pin")            # manual channel band set by the operator
                text(mono, (("*%s" % _n.get("ch", "")) if _pin else str(_n.get("ch", ""))),
                     _htx + 188, _ry, 16, WARN if _pin else TXT)
                # rec/min + bar
                _rm = _ival(_n.get("rec_min"))
                text(mono, str(_rm), _htx + 290, _ry, 16, TXT)
                _bw = int(min(1.0, _rm / 100.0) * 60)
                draw_rectangle(_htx + 342, _ry + 8, 60, 7, C(30, 40, 52, 255))
                if _bw: draw_rectangle(_htx + 342, _ry + 8, _bw, 7, C(_c.r, _c.g, _c.b, 230))
                # dedup bar
                _dd = _ival(_n.get("dedup"))
                _dc = DANGER if _dd >= 70 else (WARN if _dd >= 45 else SUCCESS)
                draw_rectangle(_htx + 410, _ry + 7, 54, 8, C(30, 40, 52, 255))
                draw_rectangle(_htx + 410, _ry + 7, int(_dd / 100.0 * 54), 8, _dc)
                text(mono, "%d%%" % _dd, _htx + 470, _ry, 14, _dc)
                # temp
                _tp = _n.get("temp", "--")
                _tc = DANGER if (_tp not in ("--", "") and _ival(_tp) >= 58) else TXT
                text(mono, _tp if _tp == "--" else "%sC" % _tp, _htx + 536, _ry, 16, _tc)
                text(mono, _upfmt(_n.get("uptime")), _htx + 598, _ry, 16, DIM)
                _drp = _ival(_n.get("dropouts"))
                text(mono, str(_drp), _htx + 676, _ry, 16, WARN if _drp else TXT)
                # state badge
                if _n.get("stale"): _bd, _bc = "STALE", WARN
                elif _n.get("full"): _bd, _bc = "FULL", DANGER
                elif not _n.get("by_core", True): _bd, _bc = "SELF", WARN
                else: _bd, _bc = "OK", SUCCESS
                text(monoB, _bd, _htx + 708, _ry, 15, _bc)
            # scrollbar when the fleet is taller than the window
            if len(_cn) > _vis:
                _sbx, _trky, _trkh = _htx - 8 + _tw - 4, _top + 48, _vis * _rowh
                draw_rectangle(_sbx, _trky, 3, _trkh, C(40, 50, 62, 255))
                _thh = max(20, int(_trkh * _vis / len(_cn)))
                _thy = _trky + int((_trkh - _thh) * _first / max(1, len(_cn) - _vis))
                draw_rectangle(_sbx, _thy, 3, _thh, TEAL)

            # ---------- footer ----------
            _gfix = _sm["gps"]
            text(mono, ("GPS: FIX — fleet hits stamped with position"
                        if _gfix else
                        "GPS: no fix (indoors?) — fleet hits still logged, mapped once a fix lands"),
                 24, H - 52, 14, SUCCESS if _gfix else WARN)
            text(mono, "[UP/DN] pick node   [L/R] task (AUTO/BLE/2.4G/5G)   [A] adopt   [X] stop core   [ESC] hide (keeps running)",
                 24, H - 32, 15, TXT)

        if prox_screen:
            PST = ("UNKNOWN", "WEAK", "NEAR", "CLOSE", "VERY CLOSE", "LOST")
            PTR = {2: "CLOSING FAST", 1: "GETTING CLOSER", 0: "HOLDING",
                   -1: "MOVING AWAY", -2: "MOVING AWAY FAST"}
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 46, gx, H, C(53, 230, 255, 7))
            draw_rectangle(0, 0, W, 46, C(9, 13, 20, 255)); draw_line(0, 46, W, 46, MAGENTA)
            gtext(dispB, "WDGWARS  //  PROXIMITY RADAR", 24, 10, 24, HI, 1)
            st_txt, st_c, ident = bk.prox_info()
            sc = {"ok": SUCCESS, "warn": WARN, "off": DIM}.get(st_c, DIM)
            text(monoB, "YOU WDG-%s" % ident, W - 420, 14, 16, SUCCESS)
            text(monoB, st_txt, W - 20 - tw(monoB, st_txt, 16), 14, 16, sc)
            if not bk.prox_on():
                text(monoB, "PROXIMITY RADAR IS OFF", W // 2 - tw(monoB, "PROXIMITY RADAR IS OFF", 24) / 2, 300, 24, DIM)
                text(mono, "press [O] to turn it ON", W // 2 - tw(mono, "press [O] to turn it ON", 18) / 2, 340, 18, ICE)
                text(mono, "[O] on/off    [ESC] back", 24, H - 32, 14, GHOST)
            else:
                hy = 70
                cols = [(24, "PLAYER"), (260, "STATE"), (440, "TREND"), (680, "SIGNAL"), (980, "dBm"), (1090, "MUTUAL")]
                for cxx, lab in cols:
                    text(mono, lab, cxx, hy, 15, DIM)
                draw_line(24, hy + 22, W - 24, hy + 22, EDGE)
                rows = bk.prox_rows()
                if not rows:
                    text(monoB, "LISTENING", W // 2 - tw(monoB, "LISTENING", 22) / 2, 240, 22, TEAL)
                    text(mono, "no WDGWars nodes in range yet", W // 2 - tw(mono, "no WDGWars nodes in range yet", 18) / 2, 274, 18, DIM)
                else:
                    seen = set(); i = 0
                    for r in rows:
                        lbl = str(r.get("label", "?"))
                        if lbl in seen: continue
                        seen.add(lbl)
                        yy = hy + 40 + i * 40; i += 1
                        if i > 11: break
                        mut = r.get("mutual"); stt = r.get("state") or 0
                        ncol = SUCCESS if mut else TEAL
                        text(monoB, lbl[:18], 24, yy, 18, ncol)
                        text(mono, PST[stt] if stt < len(PST) else "?", 260, yy, 18, HI)
                        text(mono, PTR.get(r.get("trend") or 0, ""), 440, yy, 18, ICE)
                        draw_rectangle_lines_ex(Rectangle(680, yy, 260, 16), 1, DIM)
                        bar = max(0, min(20, r.get("bar") or 0))
                        draw_rectangle(682, yy + 2, int(256 * bar / 20), 12, ncol)
                        ema = r.get("ema")
                        text(monoB, ("%d" % int(ema)) if ema is not None else "--", 980, yy, 18, HI)
                        if mut:
                            draw_rectangle(1090, yy, 48, 18, SUCCESS); text(monoB, "YES", 1098, yy + 2, 16, VOID)
                        else:
                            text(mono, "--", 1098, yy, 18, DIM)
                text(mono, "unknown nodes nearby: %d" % bk.prox_unknown(), 24, H - 58, 16, DIM)
                meet = bk.prox_meeting()
                if meet:
                    draw_rectangle(0, H - 48, W, 24, SUCCESS)
                    rssi = meet.get("rssi")
                    mtxt = "*** SPOTKANIE z %s   %s dBm ***" % (meet.get("label", "?"), int(rssi) if rssi is not None else "?")
                    text(monoB, mtxt, W // 2 - tw(monoB, mtxt, 18) / 2, H - 46, 18, VOID)
                text(mono, "[O] on/off    [ESC] back", 24, H - 28, 14, GHOST)

        # Messenger screen (MeshCore / WDGWars chat)
        if msg_screen:
            mcol = {"in": SUCCESS, "dm": ICE, "out": TEAL, "sys": DIM}
            mpre = {"in": "> ", "dm": "> ", "out": "< ", "sys": "- "}
            draw_rectangle(0, 0, W, H, C(4, 7, 12, 255))
            for gx in range(0, W, 64): draw_line(gx, 44, gx, H, C(53, 230, 255, 7))
            # title bar
            if msg_screen == "mesh" and bk.mesh_stack() == "meshtastic":
                ttl = "MESHTASTIC  //  %s  node:%s" % ("ON" if bk.mt_running() else "OFF", bk.mt_name() or "?")
                mlog = [("", "%s: %s" % (s, x), "in") for s, x, _ts in bk.mt_messages()]
                tcolr = C(45, 220, 130)
            elif msg_screen in ("meshcore", "mesh"):
                ttl = "MESHCORE  //  %s  node:%s" % ("ON" if bk.lora_on() else "OFF", bk.mc_node_name())
                mlog = bk.mc_log; tcolr = PURPLE
            else:
                ttl = "WDGWARS  #general  //  you:%s" % bk.wd_nick()
                mlog = bk.wd_log; tcolr = MAGENTA
            draw_rectangle(0, 0, W, 44, C(9, 13, 20, 255)); draw_line(0, 44, W, 44, tcolr)
            gtext(dispB, ttl, 24, 10, 24, HI, 1)
            if msg_screen == "mesh":                 # current band, cyclable with [<>]
                _mmt = bk.mesh_stack() == "meshtastic"
                _bl = (next((l for k, l in bk.mt_regions() if k == bk.mt_region_current()), "")
                       if _mmt else
                       next((r[1] for r in bk.mc_regions() if r[0] == bk.mc_region_current()), ""))
                _bt = "band: %s  [<>]" % _bl
                text(mono, _bt, W - 24 - tw(mono, _bt, 15), 15, 15, tcolr)
            # message log (scrollable, newest at bottom)
            lx0 = 24; ltop = 58; lbot = H - 68; lh = 22
            maxlines = (lbot - ltop) // lh
            total = len(mlog)
            end = max(0, total - msg_scroll)
            start = max(0, end - maxlines)
            if total == 0:
                text(mono, "no messages yet - type below and press ENTER", lx0, ltop + 6, 18, GHOST)
            for i, (d, txt, kind) in enumerate(mlog[start:end]):
                ry = ltop + i * lh
                col = mcol.get(kind, TXT)
                text(mono, (mpre.get(kind, "  ") + txt)[:118], lx0, ry, 18, col)
            if msg_scroll > 0:
                text(mono, "SCROLL +%d (DOWN to catch up)" % msg_scroll, W - 320, ltop, 14, WARN)
            # input bar
            iby = H - 52
            draw_rectangle(0, iby, W, 52, C(7, 11, 17, 255)); draw_line(0, iby, W, iby, EDGE)
            prompt = "> "
            cur = msg_buf + ("_" if int(t * 2) % 2 == 0 else " ")
            text(monoB, prompt, 24, iby + 15, 20, tcolr)
            text(mono, cur, 24 + tw(monoB, prompt, 20), iby + 15, 20, HI)
            cc = "%d/120" % len(msg_buf)
            text(mono, cc, W - 20 - tw(mono, cc, 14), iby + 6, 14, DIM)
            if msg_screen == "mesh":
                hint2 = "[TAB] nodes   [<>] band   [^T] stack   [ENTER] send   [ESC] close"
            elif msg_screen == "meshcore":
                hint2 = "[TAB] nodes   [ENTER] send   [ESC] close"
            else:
                hint2 = "[ENTER] send   [UP/DOWN] scroll   [ESC] close"
            text(mono, hint2, W - 20 - tw(mono, hint2, 13), iby + 32, 13, GHOST)
            # nodes side panel ([TAB]) — active stack's nodes (MeshCore or Meshtastic)
            if msg_screen in ("meshcore", "mesh") and mc_contacts_panel:
                _mtk = (msg_screen == "mesh" and bk.mesh_stack() == "meshtastic")
                if _mtk:
                    contacts = [{"name": n.get("name", "?"), "type": 0} for n in bk.mt_nodes()]
                    _pcol = C(45, 220, 130); _ptitle = "MESHTASTIC NODES"
                else:
                    contacts = bk.mc_contacts(); _pcol = PURPLE; _ptitle = "CONTACTS"
                cpw = 360; cpx = W - cpw; cpt = 44; cph = H - 52 - cpt
                draw_rectangle(cpx, cpt, cpw, cph, C(7, 11, 18, 255)); draw_line(cpx, cpt, cpx, cpt + cph, _pcol)
                text(monoB, "%s (%d)" % (_ptitle, len(contacts)), cpx + 14, cpt + 10, 17, _pcol)
                draw_line(cpx + 10, cpt + 34, cpx + cpw - 10, cpt + 34, EDGE)
                TICO = {0: "C", 1: "C", 2: "R", 3: "M", 4: "S"}
                if not contacts:
                    text(mono, "no nodes seen yet", cpx + 14, cpt + 46, 15, DIM)
                for i, nd in enumerate(contacts[:16]):
                    ry = cpt + 44 + i * 30
                    try: ico = TICO.get(int(nd.get("type", 0)), "?")
                    except (ValueError, TypeError): ico = "?"
                    draw_rectangle(cpx + 12, ry, 20, 20, C(_pcol.r, _pcol.g, _pcol.b, 60))
                    text(monoB, ico, cpx + 18, ry + 2, 16, _pcol)
                    text(monoB, str(nd.get("name", "?"))[:18], cpx + 40, ry, 16, HI)
                    rssi = nd.get("rssi", 0); snr = nd.get("snr", 0)
                    text(mono, "%sdBm  snr%s" % (rssi, snr), cpx + 40, ry + 16, 12, DIM)

        # Evil Twin / Portal pickers
        if et_screen == "net":
            enets = bk.networks()
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 180))
            pw, ph = 760, 456; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, DANGER)
            gtext(dispB, "EVIL TWIN  //  SELECT TARGET(S)", px0 + 28, py0 + 20, 26, HI, 1)
            text(mono, "%d networks  -  first selected = cloned SSID" % len(enets), px0 + 28, py0 + 54, 15, DIM)
            ly = py0 + 86; vis = enets[:11]
            for i, n in enumerate(vis):
                rowy = ly + i * 30; on = i == et_sel; chk = i in et_selected
                if on:
                    draw_rectangle(px0 + 20, rowy - 3, pw - 40, 28, C(DANGER.r, DANGER.g, DANGER.b, 40))
                    draw_rectangle(px0 + 20, rowy - 3, 3, 28, DANGER)
                bx = px0 + 30
                draw_rectangle_lines_ex(Rectangle(bx, rowy, 18, 18), 1, SUCCESS if chk else DIM)
                if chk: draw_rectangle(bx + 4, rowy + 4, 10, 10, SUCCESS)
                try: rssi = int(str(n.rssi).replace("dBm", "").strip())
                except (ValueError, AttributeError): rssi = -99
                nm = (n.ssid or "<hidden>")[:26]
                text(monoB, nm, bx + 30, rowy, 18, HI if on else TXT)
                meta = "%s  %sdBm  ch%s" % ((n.auth or "open")[:10], n.rssi or "?", n.channel or "?")
                text(mono, meta, px0 + pw - 30 - tw(mono, meta, 15), rowy + 1, 15, DIM)
            if not vis:
                text(mono, "no networks - ESC and run a WiFi scan first", px0 + 30, ly, 16, WARN)
            text(mono, "[UP/DOWN] move   [SPACE] toggle   [ENTER] portal   [ESC] cancel",
                 px0 + 28, py0 + ph - 28, 14, GHOST)
        elif et_screen == "portal":
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 180))
            pw, ph = 620, 320; px0 = (W - pw) // 2; py0 = (H - ph) // 2
            cyber_panel(px0, py0, pw, ph, DANGER)
            ttl = "EVIL TWIN  //  PORTAL" if et_mode == "twin" else "EVIL PORTAL  //  TEMPLATE"
            gtext(dispB, ttl, px0 + 28, py0 + 20, 26, HI, 1)
            ly = py0 + 72
            for i, (pn, ph_html) in enumerate(portal_list[:7]):
                rowy = ly + i * 32; on = i == portal_sel
                if on:
                    draw_rectangle(px0 + 20, rowy - 4, pw - 40, 30, C(DANGER.r, DANGER.g, DANGER.b, 40))
                    draw_rectangle(px0 + 20, rowy - 4, 3, 30, DANGER)
                tagc = TEAL if ph_html is None else MAGENTA
                text(monoB, "o", px0 + 32, rowy, 18, tagc)
                text(mono, pn[:46], px0 + 54, rowy + 1, 19, HI if on else TXT)
            text(mono, "[UP/DOWN] move   [ENTER] launch   [ESC] back",
                 px0 + 28, py0 + ph - 28, 14, GHOST)

        # MeshCore radio toast (incoming mesh message)
        _toast = bk.mc_bubble_active()
        if _toast:
            tw0 = int(min(560, tw(mono, _toast, 18) + 120)); tx0 = (W - tw0) // 2; ty0 = by0 + 20
            draw_rectangle(tx0, ty0, tw0, 56, C(10, 16, 24, 245))
            draw_rectangle_lines_ex(Rectangle(tx0, ty0, tw0, 56), 2, PURPLE)
            blinkr = int(t * 4) % 2 == 0
            draw_circle(tx0 + tw0 - 22, ty0 + 20, 5, PURPLE if blinkr else C(40, 20, 50, 255))
            draw_ring(Vector2(tx0 + tw0 - 22, ty0 + 20), 8, 9, -50, 50, 12, C(178, 59, 214, 140 if blinkr else 40))
            text(monoB, "MESHCORE", tx0 + 16, ty0 + 8, 15, PURPLE)
            text(mono, _toast[:56], tx0 + 16, ty0 + 30, 18, HI)

        # QUIT modal — module-aware (GPS/LoRa/SDR); USB stays powered for the XIAO
        if confirm_quit:
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 205))
            _busy = bk.flash_busy()
            _mods = quit_modules if not _busy else []
            bw = 560          # wide enough for "loot stays on the SD card" either way
            bhh = 158 + (58 if _mods else 0) + (26 if (_mods and quit_can_restore) else 0)
            bx = (W - bw) // 2; byv = (H - bhh) // 2
            cyber_panel(bx, byv, bw, bhh, MAGENTA)
            gtext(dispB, "QUIT WatchDogsGo?", bx + 32, byv + 28, 30, HI, 1)
            if _busy:
                text(mono, "FLASH IN PROGRESS - cannot quit, wait for it to finish",
                     bx + 32, byv + 74, 16, DANGER)
                gtext(dispB, "[N] NO", bx + bw - 150, byv + 106, 26, TEAL, 1)
            elif _mods:
                text(mono, "ACTIVE MODULES:  " + "   ".join(_mods), bx + 32, byv + 68, 17, WARN)
                text(mono, "Power them down before leaving?", bx + 32, byv + 92, 15, DIM)
                _ry = byv + 122
                gtext(dispB, "[Y] OFF & QUIT", bx + 32, _ry, 22, MAGENTA, 1)
                gtext(dispB, "[L] LEAVE ON", bx + 300, _ry, 22, TEAL, 1)
                if quit_can_restore:
                    gtext(dispB, "[R] RESTORE PRE-LAUNCH", bx + 32, _ry + 34, 20, ICE, 1)
                _fy = byv + bhh - 26
                text(mono, "[N] cancel   ·   loot stays on the SD card", bx + 32, _fy, 13, GHOST)
            else:
                text(mono, "no active modules   ·   loot stays on the SD card",
                     bx + 32, byv + 74, 16, DIM)
                gtext(dispB, "[Y] YES", bx + 32, byv + 106, 26, MAGENTA, 1)
                gtext(dispB, "[N] NO", bx + bw - 150, byv + 106, 26, TEAL, 1)

        # RESTORE-DATA confirm modal (Y/N)
        if confirm_restore:
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 205))
            bw, bhh = 560, 176; bx = (W - bw) // 2; byv = (H - bhh) // 2
            cyber_panel(bx, byv, bw, bhh, DANGER)
            gtext(dispB, "RESTORE DATA?", bx + 32, byv + 26, 30, HI, 1)
            text(mono, "Replaces loot/XP/badges/chat with the newest backup.", bx + 32, byv + 70, 15, WARN)
            text(mono, "Current data is kept at loot.pre-restore. App restarts.", bx + 32, byv + 92, 15, DIM)
            gtext(dispB, "[Y] YES", bx + 32, byv + 124, 26, DANGER, 1)
            gtext(dispB, "[N] NO", bx + bw - 150, byv + 124, 26, TEAL, 1)

        # DELETE-SESSION confirm modal (Y/N) — permanent, so it spells out cost
        if confirm_del is not None:
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 205))
            bw, bhh = 620, 212; bx = (W - bw) // 2; byv = (H - bhh) // 2
            cyber_panel(bx, byv, bw, bhh, DANGER)
            gtext(dispB, "DELETE SESSION?", bx + 32, byv + 24, 30, HI, 1)
            text(mono, confirm_del, bx + 32, byv + 66, 16, WARN)
            _sz = bk.session_size(confirm_del)
            _szt = ("%.1f MB" % (_sz / 1048576.0)) if _sz >= 1048576 else ("%d KB" % (_sz // 1024))
            _cnt = ""
            for _s in (bk.sessions() or []):
                if _s.get("name") == confirm_del:
                    _c = _s.get("counts", {})
                    _cnt = "  %d wifi / %d bt / %d hs / %d mesh / %d adsb" % (
                        _c.get("wifi", 0), _c.get("bt", 0), _c.get("hs", 0),
                        _c.get("mc", 0), _c.get("adsb", 0))
                    break
            text(mono, "%s%s" % (_szt, _cnt), bx + 32, byv + 90, 14, DIM)
            if bk.session_uploaded(confirm_del):
                text(mono, "uploaded to WDGWars - a copy is on the portal", bx + 32, byv + 116, 14, SUCCESS)
            else:
                text(mono, "NOT uploaded - this is the only copy", bx + 32, byv + 116, 14, DANGER)
            text(mono, "Permanent. Capture data cannot be recovered.", bx + 32, byv + 138, 15, WARN)
            gtext(dispB, "[Y] DELETE", bx + 32, byv + 166, 26, DANGER, 1)
            gtext(dispB, "[N] KEEP", bx + bw - 176, byv + 166, 26, TEAL, 1)

        # addon hardware-dependency prompt (module OFF -> enable?)
        if dep_prompt:
            _dp_kind, _dp_lbl, _dp_addon = dep_prompt[1], dep_prompt[2], dep_prompt[3]
            draw_rectangle(0, 0, W, H, C(3, 5, 9, 205))
            dbw, dbh = 640, 184; dbx = (W - dbw) // 2; dby = (H - dbh) // 2
            cyber_panel(dbx, dby, dbw, dbh, WARN)
            gtext(dispB, "%s NEEDS %s" % (_dp_addon.upper(), _dp_lbl.upper()), dbx + 32, dby + 24, 26, HI, 1)
            text(mono, "The %s module is currently OFF." % _dp_lbl, dbx + 32, dby + 68, 16, WARN)
            text(mono, "Enable %s and open %s?" % (_dp_lbl, _dp_addon), dbx + 32, dby + 92, 15, DIM)
            gtext(dispB, "[Y] ENABLE", dbx + 32, dby + 128, 24, SUCCESS, 1)
            gtext(dispB, "[N] CANCEL", dbx + dbw - 196, dby + 128, 24, TEAL, 1)

        end_texture_mode()

        # ---------- composite: texture + glitch + scanlines + vignette ----------
        begin_drawing()
        clear_background(BLACK)
        src = Rectangle(0, 0, float(W), float(-H))
        draw_texture_rec(target.texture, src, Vector2(0, 0), WHITE)
        if not field:
            if t >= next_glitch:
                glitch_until = t + 0.12
                next_glitch = t + random.uniform(2.5, 5.5)
            if t < glitch_until:
                begin_blend_mode(BLEND_ADDITIVE)
                for _ in range(random.randint(2, 4)):
                    yb = random.randint(by0, by1 - 24); hb = random.randint(6, 26)
                    dxb = random.randint(-34, 34)
                    tnt = random.choice([C(0, 255, 255, 150), C(255, 0, 160, 150)])
                    begin_scissor_mode(0, yb, W, hb)
                    draw_texture_rec(target.texture, src, Vector2(dxb, 0), tnt)
                    end_scissor_mode()
                end_blend_mode()
            for y in range(0, H, 3): draw_line(0, y, W, y, C(0, 0, 0, 38))
            draw_rectangle_gradient_v(0, 0, W, 72, C(0, 0, 0, 120), C(0, 0, 0, 0))
            draw_rectangle_gradient_v(0, H - 72, W, 72, C(0, 0, 0, 0), C(0, 0, 0, 120))
            draw_rectangle_gradient_h(0, 0, 84, H, C(0, 0, 0, 110), C(0, 0, 0, 0))
            draw_rectangle_gradient_h(W - 84, 0, 84, H, C(0, 0, 0, 0), C(0, 0, 0, 110))

        # full-screen 433 Hunter / Capture panel: drawn LAST so it sits on top of
        # all map HUD (no overlap). Suppressed while a menu/overlay screen is up.
        if hunt_full and bk.sdr_on() and not field and not (
                menu_open or loot_screen or data_screen or msg_screen or comp_screen
                or flash_screen or wl_screen or cluster_screen or scan_modal
                or prox_screen or mc_region_screen or mesh_screen or et_screen or confirm_quit):
            _hm = bk.sdr_mode().lower()
            if "keyhunt" in _hm:
                _draw_hunt_panel(True)
            elif "grab433" in _hm:
                _draw_grab_panel(True)
        end_drawing()

        frame += 1
        if shot and frame > 110:
            take_screenshot(shot); break

    for _t in bm_cache.values():
        if _t is not None:
            try:
                unload_texture(_t)
            except Exception:
                pass
    if basemap is not None:
        basemap.close()
    bk.close(modules=quit_mode)     # 'off' | 'leave' | 'restore' per the quit prompt
    close_window()


if __name__ == "__main__":
    main()
