"""Native Meshtastic support for WatchDogsGo-NG.

Wraps the official ``meshtasticd`` daemon (driving the uConsole's SX1262 over
SPI) and the ``meshtastic`` Python lib (TCP API on localhost:4403). Mirrors the
MeshCore role: collect nodes (with GPS) for loot + map, send/receive text.

Radio is the shared SX1262, so Meshtastic and MeshCore are mutually exclusive —
the backend arbiter powers the LoRa rail (GPIO16) and stops the other stack
before calling start(). meshtasticd.service is masked on purpose; we run the
binary as a subprocess.

Hardware recipe proven 2026-11-14: rail ON -> ``sudo meshtasticd`` -> 4403 ->
TCPInterface. Without the rail the SX1262 init aborts (SIGABRT).
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

MT_API_HOST = "localhost"
MT_API_PORT = 4403

# Curated region list (band-relevant); label carries the 868/915 band the user
# cares about. Enum names must match meshtastic.config_pb2 RegionCode.
MT_REGIONS = [
    ("EU_868", "EU 868 MHz"),
    ("US", "US 915 MHz"),
    ("ANZ", "AU/NZ 915 MHz"),
    ("EU_433", "EU 433 MHz"),
    ("IN", "India 865 MHz"),
]
# Curated modem presets (speed/range trade-off).
MT_PRESETS = [
    ("LONG_FAST", "Long Fast (default)"),
    ("LONG_MODERATE", "Long Moderate"),
    ("LONG_SLOW", "Long Slow"),
    ("MEDIUM_FAST", "Medium Fast"),
    ("MEDIUM_SLOW", "Medium Slow"),
    ("SHORT_FAST", "Short Fast"),
    ("SHORT_SLOW", "Short Slow"),
    ("SHORT_TURBO", "Short Turbo"),
]


def region_label(key: str) -> str:
    for k, lab in MT_REGIONS:
        if k == key:
            return lab
    return key


def preset_label(key: str) -> str:
    for k, lab in MT_PRESETS:
        if k == key:
            return lab
    return key


def derive_short(name: str) -> str:
    """A <=4 char Meshtastic short name from a long name (alnum, uppercase)."""
    s = "".join(c for c in (name or "") if c.isalnum())
    return (s[:4].upper() or "WDG")


def _is_generic_name(name: str) -> bool:
    """meshtasticd ships a generic owner like 'Meshtastic 1a2b' — treat that (or
    empty) as unset so we can stamp the player's real identity instead."""
    n = (name or "").strip()
    return (not n) or n.lower().startswith("meshtastic")


class MeshtasticManager:
    """meshtasticd + meshtastic-lib wrapper. UI-agnostic: driven by callables."""

    def __init__(self, emit: Optional[Callable[[str, str], None]] = None,
                 on_node: Optional[Callable[[dict], None]] = None,
                 on_message: Optional[Callable[[str, str], None]] = None) -> None:
        self._emit = emit or (lambda m, k="dim": None)
        self._on_node = on_node
        self._on_message = on_message
        self.running = False
        self.connected = False
        self.my_name = ""
        self.my_short = ""
        self.my_num = None
        self.region = ""
        self.preset = ""
        self._iface = None
        self._proc = None               # the `sudo meshtasticd` WE launched (or None)
        self._logf = None
        self._nodes: dict = {}          # node_id -> {id,name,lat,lon,snr,rssi,ts}
        self.messages: list = []        # [(sender, text, ts)]
        self._emitted_nodes: set = set()
        self._stop = threading.Event()
        self._poll_thread = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    @staticmethod
    def available() -> bool:
        """meshtasticd binary + meshtastic lib both present."""
        if not shutil.which("meshtasticd"):
            return False
        try:
            import meshtastic  # noqa: F401
            return True
        except Exception:
            return False

    @staticmethod
    def _port_open(host: str = MT_API_HOST, port: int = MT_API_PORT) -> bool:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def start(self, region: str = "EU_868", preset: str = "LONG_FAST",
              owner_long: str = "", owner_short: str = "") -> bool:
        """Launch meshtasticd (if needed), connect, apply region/preset + owner
        name. The LoRa rail must already be powered and MeshCore stopped
        (backend arbiter). owner_long defaults the node away from the generic
        'Meshtastic xxxx' to the player's identity."""
        if self.running:
            return True
        self._stop.clear()
        if not self.available():
            self._emit("Meshtastic: meshtasticd or python lib missing", "error")
            return False
        if not self._launch_daemon():
            self._stop_own_daemon()         # roll back a half-started launch
            return False
        if not self._connect():
            self._emit("Meshtastic: could not reach API on :%d" % MT_API_PORT, "error")
            self.stop()
            return False
        self._apply_config(region, preset)
        self._apply_owner(owner_long, owner_short)
        self.running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        self._emit("Meshtastic up: %s / %s as %s"
                   % (region_label(self.region or region),
                      preset_label(self.preset or preset), self.my_name or "?"), "ok")
        return True

    def _launch_daemon(self) -> bool:
        if self._proc is not None and self._proc.poll() is not None:
            self._stop_own_daemon()              # reap a daemon of ours that died
        if self._port_open():
            self._emit("Meshtastic: meshtasticd already running", "dim")
            return True
        try:
            _out = self._open_log()              # keep the daemon's own log for debugging
            _mtd = shutil.which("meshtasticd") or "/usr/bin/meshtasticd"
            self._proc = subprocess.Popen(
                ["sudo", _mtd],                  # ABSOLUTE path: a relative argv[0]
                stdout=_out, stderr=_out,        # breaks meshtasticd's execv() self-reboot
                stdin=subprocess.DEVNULL, start_new_session=True)
        except Exception as exc:
            self._emit("Meshtastic: cannot start meshtasticd (%s)" % exc, "error")
            return False
        self._emit("Meshtastic: starting meshtasticd...", "tx")
        for _ in range(30):                     # up to ~15s for the API to bind
            if self._stop.is_set():
                return False
            if self._port_open():
                return True
            if self._proc.poll() is not None:   # daemon died (e.g. radio not powered)
                self._emit("Meshtastic: meshtasticd exited early "
                           "(LoRa rail powered?)", "error")
                return False
            time.sleep(0.5)
        self._emit("Meshtastic: meshtasticd did not open the API in time", "error")
        return False

    def _open_log(self):
        """Daemon log in /tmp, opened as root WITHOUT following a symlink."""
        self._close_log()
        try:
            fd = os.open("/tmp/wdg_meshtasticd.log",
                         os.O_WRONLY | os.O_CREAT | os.O_APPEND
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                         0o600)
            self._logf = os.fdopen(fd, "ab", buffering=0)
            return self._logf
        except Exception:
            self._logf = None
            return subprocess.DEVNULL

    def _close_log(self) -> None:
        f, self._logf = self._logf, None
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    @staticmethod
    def _descendants(pid: int) -> list:
        """PIDs below `pid` in the process tree (from /proc), deepest last. Lets us
        stop exactly the daemon WE started (sudo may run it under a pty monitor in
        its own session, out of reach of a process-group signal)."""
        children: dict = {}
        try:
            for d in os.listdir("/proc"):
                if not d.isdigit():
                    continue
                try:
                    with open("/proc/%s/stat" % d) as f:
                        st = f.read()
                    ppid = int(st.rsplit(")", 1)[1].split()[1])
                except Exception:
                    continue
                children.setdefault(ppid, []).append(int(d))
        except OSError:
            return []
        out, todo = [], [pid]
        while todo:
            for c in children.get(todo.pop(), []):
                if c not in out:
                    out.append(c)
                    todo.append(c)
        return out

    def _stop_own_daemon(self, timeout: float = 8.0) -> None:
        """Stop ONLY the meshtasticd this manager launched (a daemon that was
        already running when we attached is left alone), reap it, close its log."""
        p, self._proc = self._proc, None
        if p is not None and p.poll() is None:
            tree = self._descendants(p.pid)
            try:
                p.terminate()                # sudo relays SIGTERM to meshtasticd
            except Exception:
                pass
            for pid in tree:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                p.wait(timeout=timeout)
            except Exception:
                for pid in [p.pid] + self._descendants(p.pid) + tree:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except OSError:
                        pass
                try:
                    p.wait(timeout=3)
                except Exception:
                    pass
            # wait for the tree we signalled to actually leave (frees the SX1262)
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and any(
                    os.path.exists("/proc/%d" % pid) for pid in tree):
                time.sleep(0.1)
        elif p is not None:
            try:
                p.wait(timeout=0.1)          # already exited: reap
            except Exception:
                pass
        self._close_log()

    def _connect(self) -> bool:
        import meshtastic.tcp_interface
        for _ in range(5):
            if self._stop.is_set():
                return False
            try:
                self._iface = meshtastic.tcp_interface.TCPInterface(hostname=MT_API_HOST)
                break
            except Exception as exc:
                log.debug("meshtastic connect retry: %s", exc)
                time.sleep(2)
        if not self._iface:
            return False
        time.sleep(2)
        try:
            from pubsub import pub
            pub.subscribe(self._on_receive, "meshtastic.receive.text")
        except Exception as exc:
            log.debug("pubsub subscribe failed: %s", exc)
        try:
            my = self._iface.getMyNodeInfo() or {}
            self.my_num = my.get("num")
            self.my_name = (my.get("user", {}) or {}).get("longName", "") or ""
        except Exception:
            pass
        self.connected = True
        return True

    # ------------------------------------------------------------------
    # region / preset
    # ------------------------------------------------------------------
    def _config_enums(self):
        from meshtastic import config_pb2
        return (config_pb2.Config.LoRaConfig.RegionCode,
                config_pb2.Config.LoRaConfig.ModemPreset)

    def _read_config(self):
        """(region_name, preset_name) currently on the node, or ("","")."""
        try:
            R, P = self._config_enums()
            lc = self._iface.localNode.localConfig
            return R.Name(lc.lora.region), P.Name(lc.lora.modem_preset)
        except Exception as exc:
            log.debug("read config: %s", exc)
            return "", ""

    def _apply_config(self, region: str, preset: str) -> None:
        """Set region+preset ONLY if they differ (writeConfig reboots the node,
        which drops the connection — so avoid churn on every start)."""
        cur_r, cur_p = self._read_config()
        self.region, self.preset = cur_r, cur_p
        want_r = region if region in dict(MT_REGIONS) else cur_r
        want_p = preset if preset in dict(MT_PRESETS) else cur_p
        if cur_r == want_r and cur_p == want_p:
            return                               # already correct, no reboot
        try:
            R, P = self._config_enums()
            node = self._iface.localNode
            node.localConfig.lora.region = R.Value(want_r)
            node.localConfig.lora.modem_preset = P.Value(want_p)
            node.localConfig.lora.use_preset = True
            self._emit("Meshtastic: applying %s / %s (node reboots)..."
                       % (region_label(want_r), preset_label(want_p)), "tx")
            node.writeConfig("lora")
            self.region, self.preset = want_r, want_p
            self._reconnect_after_reboot()
        except Exception as exc:
            self._emit("Meshtastic: config set failed (%s)" % exc, "warn")

    def _reconnect_after_reboot(self) -> None:
        """A region/preset write makes the node save config and self-reboot. As a
        subprocess its execv() self-restart fails ('execv() returned -1' ->
        'free(): invalid pointer' -> SIGABRT), so the daemon DIES rather than
        comes back. Tear it down and launch a fresh meshtasticd — it boots with
        the newly-saved config — then reconnect. (With the absolute-path launch
        the self-reboot may now succeed in place; the kill+relaunch is the robust
        fallback either way.)"""
        try:
            self._iface.close()
        except Exception:
            pass
        self._iface = None
        self.connected = False
        time.sleep(2)                            # let the node finish saving config
        if not self._port_open():                # daemon died on its failed reboot
            self._stop_own_daemon()              # only ever our own process tree
            time.sleep(2)
            if not self._launch_daemon():
                self._stop_own_daemon()
                self._emit("Meshtastic: daemon did not come back after config change",
                           "error")
                return
        if self._connect():
            self._emit("Meshtastic: reconnected after %s / %s"
                       % (region_label(self.region), preset_label(self.preset)), "ok")
        else:
            self._emit("Meshtastic: could not reconnect after config change", "warn")

    def set_region(self, region: str) -> None:
        if self.running and self._iface:
            self._apply_config(region, self.preset or "LONG_FAST")

    def set_preset(self, preset: str) -> None:
        if self.running and self._iface:
            self._apply_config(self.region or "EU_868", preset)

    def _apply_owner(self, owner_long: str, owner_short: str = "") -> None:
        """Stamp the player's identity as the node owner if one was given AND the
        node still carries a generic/empty name, OR the given name differs. Keeps
        us off the generic 'Meshtastic xxxx' every Heltec ships with."""
        owner_long = (owner_long or "").strip()
        if not owner_long:
            return
        if not (_is_generic_name(self.my_name) or self.my_name != owner_long):
            return
        self.set_owner(owner_long, owner_short)

    def set_owner(self, long_name: str, short_name: str = "") -> None:
        """Set the Meshtastic owner long/short name (what peers see). short_name
        is <=4 chars (derived from long_name when blank). No node reboot."""
        if not self._iface:
            return
        long_name = (long_name or "").strip()[:39]
        if not long_name:
            return
        short_name = (short_name or "").strip()[:4] or derive_short(long_name)
        try:
            self._iface.localNode.setOwner(long_name=long_name, short_name=short_name)
            self.my_name = long_name
            self.my_short = short_name
            self._emit("Meshtastic name: %s (%s)" % (long_name, short_name), "ok")
        except Exception as exc:
            self._emit("Meshtastic setOwner failed: %s" % exc, "warn")

    # ------------------------------------------------------------------
    # messaging + nodes
    # ------------------------------------------------------------------
    def send(self, text: str) -> None:
        text = (text or "").strip()
        if not text or not self._iface:
            return
        try:
            self._iface.sendText(text)
            self.messages.append((self.my_name or "me", text, time.time()))
            self.messages = self.messages[-300:]   # cap: appended from two threads, never trimmed
        except Exception as exc:
            self._emit("Meshtastic send failed: %s" % exc, "warn")

    def _on_receive(self, packet=None, interface=None) -> None:
        try:
            dec = (packet or {}).get("decoded", {}) or {}
            text = dec.get("text") or dec.get("payload")
            if isinstance(text, bytes):
                text = text.decode("utf-8", "replace")
            if not text:
                return
            frm = str((packet or {}).get("fromId") or (packet or {}).get("from") or "?")
            name = self._nodes.get(frm, {}).get("name") or frm
            self.messages.append((name, text, time.time()))
            self.messages = self.messages[-300:]   # cap: appended from two threads, never trimmed
            if self._on_message:
                self._on_message(name, text)
        except Exception as exc:
            log.debug("on_receive: %s", exc)

    def _poll_loop(self) -> None:
        """Poll the node DB for fresh nodes + positions, emit to backend (loot/map).
        Also watch daemon liveness: if meshtasticd dies underneath us, flip running
        off so the UI stops showing a false 'ON' and the arbiter can recover."""
        misses = 0
        while not self._stop.is_set():
            if self._port_open():
                misses = 0
                self._scan_nodes()
            else:
                misses += 1
                if misses >= 2:                  # ~10s of a dead API = daemon gone
                    self._emit("Meshtastic: meshtasticd stopped - radio lost", "warn")
                    self.running = False
                    self.connected = False
                    return
            self._stop.wait(5.0)

    def _scan_nodes(self) -> None:
        if not self._iface:
            return
        try:
            nodes = self._iface.nodes or {}
        except Exception:
            return
        for nid, n in list(nodes.items()):
            try:
                if self.my_num is not None and n.get("num") == self.my_num:
                    continue                     # skip self
                u = n.get("user", {}) or {}
                pos = n.get("position", {}) or {}
                lat = pos.get("latitude")
                lon = pos.get("longitude")
                rec = {
                    "id": str(nid),
                    "name": u.get("longName") or u.get("shortName") or str(nid),
                    "lat": float(lat) if lat is not None else 0.0,
                    "lon": float(lon) if lon is not None else 0.0,
                    "snr": n.get("snr") or 0.0,
                    "rssi": n.get("rssi") or 0.0,
                    "ts": time.time(),
                }
                with self._lock:
                    self._nodes[str(nid)] = rec
                # emit each node once (for loot), and again if it gains a GPS fix
                key = str(nid) + ("+gps" if (rec["lat"] or rec["lon"]) else "")
                if key not in self._emitted_nodes:
                    self._emitted_nodes.add(key)
                    if self._on_node:
                        self._on_node(rec)
            except Exception as exc:
                log.debug("scan node %s: %s", nid, exc)

    def nodes(self) -> list:
        with self._lock:
            return list(self._nodes.values())

    # ------------------------------------------------------------------
    @property
    def owns_daemon(self) -> bool:
        """True when the meshtasticd in use was started by the game (and so may be
        stopped by it); False for a daemon started outside the game."""
        return self._proc is not None

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        self.connected = False
        try:
            if self._iface:
                self._iface.close()
        except Exception:
            pass
        self._iface = None
        try:
            from pubsub import pub
            pub.unsubscribe(self._on_receive, "meshtastic.receive.text")
        except Exception:
            pass
        # stop the daemon WE started (frees the SX1262 for MeshCore); one that was
        # already running before the game attached is not ours to kill
        owned = self._proc is not None
        self._stop_own_daemon()
        t = self._poll_thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=6)
        self._poll_thread = None
        self._emit("Meshtastic stopped (radio released)" if owned else
                   "Meshtastic disconnected (external meshtasticd left running)", "dim")
