"""Host-side WDGWars proximity for WatchDogsGo.

Detects other WDGWars nodes (badges, other consoles) over BLE using the
uConsole's OWN hci0 controller — btmgmt to advertise a rotating identity, bleak
to scan — and resolves the ones we hold a key for. The maths lives in
watchdogs/proximity/prox.py, the SAME engine the Pimoroni badge runs, so the two
land in the same rotation slot and see each other.

WHY A SUBPROCESS, NOT A THREAD: the radio needs its own asyncio loop and root;
pyxel needs the main thread. Running both in one process fought the controller
and crashed the render loop. So the radio runs as a child process (peer.py) that
writes a JSON snapshot every window, and the game just reads and draws it. The
window can never take the radio down and vice versa.

CONTROLLER SHARING: the ESP32 BT features (scan_bt, sniffer, evil twin) use the
ESP32's radio over serial and do NOT touch hci0 — but the HOST BT features
(bt_ducky, race_attack, the watch link) do. Call pause() before those and
resume() after, so proximity yields hci0 for the duration.
"""

import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
PEER_SCRIPT = os.path.join(_HERE, "proximity", "peer.py")
SNAPSHOT_PATH = "/tmp/wdgprox_snap.json"

_CFG_DIR = os.path.expanduser("~/.watchdogs")
SECRET_FILE = os.path.join(_CFG_DIR, "prox_secret")
KEYS_FILE = os.path.join(_CFG_DIR, "prox_keys.json")
ENABLED_FILE = os.path.join(_CFG_DIR, "prox_enabled")

# Defaults pair with the badge smoke-test setup so it works out of the box; the
# WDGWars portal would drop the real secret + resolution keys into ~/.watchdogs.
DEFAULT_SECRET = "peer-jeden"
DEFAULT_KNOWN = {"marauder": "badger-test", "zero": "wdgong-zero"}

ADV_SECS = "3"
SCAN_SECS = "6"
STALE_S = 20.0            # snapshot older than this = radio not really reporting
POLL_MIN_S = 0.4         # re-read the snapshot at most this often


class ProximityManager:
    def __init__(self, snapshot=SNAPSHOT_PATH):
        self.snapshot = snapshot
        self.proc = None            # scanner (scan-only) process
        self._adv_proc = None       # BlueZ dbus advertiser process
        self.enabled = False
        self.paused = False
        self._snap = {}
        self._snap_mtime = 0.0
        self._last_poll = 0.0
        self._last_spawn = 0.0
        self._respawn_backoff = 5.0     # don't relaunch a dying child faster than this

    # ── config ───────────────────────────────────────────────────────────
    def _secret(self):
        try:
            s = open(SECRET_FILE).read().strip()
            if s:
                return s
        except Exception:
            pass
        return DEFAULT_SECRET

    def _known(self):
        try:
            with open(KEYS_FILE) as f:
                d = json.load(f)
            if isinstance(d, dict) and d:
                return {str(k): str(v) for k, v in d.items()}
        except Exception:
            pass
        return dict(DEFAULT_KNOWN)

    def wanted(self):
        """Persisted on/off preference (default ON)."""
        try:
            return open(ENABLED_FILE).read().strip() != "0"
        except Exception:
            return True

    def _remember(self, on):
        try:
            os.makedirs(_CFG_DIR, exist_ok=True)
            with open(ENABLED_FILE, "w") as f:
                f.write("1" if on else "0")
        except Exception:
            pass

    # ── process control ──────────────────────────────────────────────────
    def _spawn(self):
        if self.running():
            return
        self._last_spawn = time.time()
        # SCANNER: scan-only (continuous, reliable) -> sees peers + writes the snapshot.
        # We no longer advertise from the peer: btmgmt add-adv is unusable on this UART
        # controller (never registers an instance). Advertising runs in a SECOND process
        # via the BlueZ D-Bus API (dbus_adv), which BlueZ can do alongside the scan.
        args = [sys.executable, PEER_SCRIPT, "--scan-only",
                "--secret", self._secret(),
                "--snapshot", self.snapshot]
        for label, secret in self._known().items():
            args += ["--know", "%s=%s" % (label, secret)]
        # Sanitize the env: the game runs under `sudo -E` so the child would inherit
        # XDG_RUNTIME_DIR=/run/user/1000 (the login user's) while running as root, which
        # makes dbus/bleak hang on controller setup — the scan never starts and no
        # snapshot is ever written. Point the child at root's runtime dir and a sane
        # PATH (btmgmt lives in /usr/bin), and drop the Wayland/display vars it never needs.
        env = dict(os.environ)
        for _k in ("XDG_RUNTIME_DIR", "WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY",
                   "DBUS_SESSION_BUS_ADDRESS"):
            env.pop(_k, None)
        env["PYTHONUNBUFFERED"] = "1"            # so the peer log flushes (diagnosis)
        _path = env.get("PATH", "")
        if "/usr/bin" not in _path:
            env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin" + ((":" + _path) if _path else "")
        _repo = os.path.dirname(os.path.dirname(os.path.dirname(PEER_SCRIPT)))
        adv_args = [sys.executable, "-m", "watchdogs.proximity.dbus_adv",
                    "--secret", self._secret()]
        try:
            _log = open("/tmp/wdgprox_peer.log", "a")
            self.proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=_log, stderr=_log,
                close_fds=True, env=env)
            self._adv_proc = subprocess.Popen(
                adv_args, stdin=subprocess.DEVNULL, stdout=_log, stderr=_log,
                close_fds=True, env=env, cwd=_repo)
        except Exception:
            self.proc = None

    @staticmethod
    def _end(p):
        if p is not None:
            try:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except Exception:
                    p.kill()
            except Exception:
                pass

    def _kill(self):
        self._end(self.proc)
        self._end(getattr(self, "_adv_proc", None))
        self.proc = None
        self._adv_proc = None

    def running(self):
        scan_ok = self.proc is not None and self.proc.poll() is None
        adv = getattr(self, "_adv_proc", None)
        adv_ok = adv is not None and adv.poll() is None
        return scan_ok and adv_ok

    # ── public API ───────────────────────────────────────────────────────
    def start(self):
        """Turn proximity on (and remember it)."""
        self.enabled = True
        self.paused = False
        self._remember(True)
        self._spawn()

    def stop(self):
        """Turn proximity off (and remember it)."""
        self.enabled = False
        self.paused = False
        self._remember(False)
        self._kill()

    def toggle(self):
        if self.enabled:
            self.stop()
        else:
            self.start()
        return self.enabled

    def pause(self):
        """Yield hci0 to a host-BT operation; stays 'enabled' so resume() knows."""
        if self.enabled and not self.paused:
            self.paused = True
            self._kill()

    def resume(self):
        if self.enabled and self.paused:
            self.paused = False
            self._spawn()

    def shutdown(self):
        """Kill the child on app exit WITHOUT changing the on/off preference."""
        self._kill()

    def tick(self, now=None):
        """Call once per frame. Keeps the child alive when it should be, and
        refreshes the snapshot at a modest rate. Cheap; safe to call at 30 fps."""
        now = now or time.time()
        if (self.enabled and not self.paused and not self.running()
                and now - self._last_spawn >= self._respawn_backoff):
            self._spawn()                       # respawn a died child, throttled
        if now - self._last_poll >= POLL_MIN_S:
            self._last_poll = now
            try:
                st = os.stat(self.snapshot)
                if st.st_mtime != self._snap_mtime:
                    with open(self.snapshot) as f:
                        self._snap = json.load(f)
                    self._snap_mtime = st.st_mtime
            except Exception:
                pass

    # ── view for drawing ─────────────────────────────────────────────────
    def fresh(self):
        return bool(self._snap) and (time.time() - self._snap_mtime) < STALE_S

    def rows(self):
        return self._snap.get("rows", []) if self.fresh() else []

    def my_ident(self):
        return self._snap.get("my_ident", "") if self.fresh() else ""

    def meeting(self):
        return self._snap.get("meet") if self.fresh() else None

    def unknown(self):
        return self._snap.get("unknown", 0) if self.fresh() else 0

    def status_text(self):
        if not self.enabled:
            return "PROX OFF"
        if self.paused:
            return "PROX HOLD"
        if not self.fresh():
            return "PROX ..."
        rows = self.rows()
        near = sum(1 for r in rows if r.get("mutual"))
        if not rows:
            return "PROX SCAN"
        return "PROX %d/%d" % (near, len(rows))
