"""EXTRAS Phase 2 — sandbox HOST side (runs inside the root game process).

An extra runs in a separate, freshly exec'd Python interpreter that locks itself
down before any extra code executes (see wdgo_extras_child): empty chroot, a
dedicated unprivileged uid, no network namespace, no fork, bounded memory. It is
a headless logic process: it cannot draw or touch hardware. This module is the
other end of its only file descriptor:

  * draw-ops   the child returns a list of primitives per frame; every op is
               re-validated and clamped here, then painted through the bridge
  * broker     the child asks for storage / loot by name; each request is checked
               against the permissions in the extra's manifest and bounded
  * watchdog   a child that stops answering is SIGKILLed; the game never blocks
               on it (a late frame just re-paints the previous one)

Nothing received from the child is trusted. A protocol violation kills it.

SandboxHost mirrors ExtraHost's surface (meta / state / error / open / frame /
close) so the game drives both the same way. Pure stdlib — never imports raylib.
"""
import base64
import math
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time

import wdgo_extras as wx
import wdgo_extras_ipc as ipc

SANDBOX_UID = 61184           # dedicated, otherwise-unused numeric uid/gid
SANDBOX_GID = 61184
JAIL_DIR = "/run/wdgo-extras-jail"
MEM_LIMIT = 512 * 1024 * 1024

START_TIMEOUT = 8.0           # hello -> ready
FRAME_KILL_AFTER = 3.0        # no frame reply for this long -> SIGKILL
FRAME_WAIT_BUDGET = 0.010     # max time the game loop waits for a frame reply
EXIT_GRACE = 0.30             # on_exit window before the kill
MAX_OUT_BUF = 2 * 1024 * 1024
CALL_BURST = 128              # broker calls: token bucket (burst / refill per second)
CALL_RATE = 256.0
STORAGE_MAX_FILE = 256 * 1024
STORAGE_MAX_FILES = 64
LOOT_SAVE_MAX = 1024 * 1024
LOOT_LIST_MAX = 500
LOG_BURST = 30                # log lines accepted per LOG_WINDOW seconds
LOG_WINDOW = 10.0

_CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wdgo_extras_child.py")


def full_isolation_possible() -> bool:
    return sys.platform.startswith("linux") and hasattr(os, "geteuid") and os.geteuid() == 0


def available(require_isolation=True) -> bool:
    """Can the sandbox run here? With require_isolation the kernel jail must be
    achievable (Linux + root); without it (desktop simulator) any POSIX host works,
    with the child reporting isolation level 'none'."""
    if not os.path.isfile(_CHILD) or not hasattr(socket, "socketpair"):
        return False
    return full_isolation_possible() or not require_isolation


# ----------------------------------------------------------------------
# validation helpers — everything from the child goes through these
# ----------------------------------------------------------------------
def _num(v, lo, hi, default=0.0):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return default
    if v != v or v in (float("inf"), float("-inf")):
        return default
    return lo if v < lo else hi if v > hi else v


def _color(v):
    return v if isinstance(v, str) and v in wx.PALETTE else "txt"


def _clean_text(v, limit):
    if not isinstance(v, str):
        v = str(v)
    if len(v) > limit:
        v = v[:limit]
    return "".join(ch if (ch >= " " or ch == "\t") and ch != "\x7f" else " " for ch in v)


_C = 20000.0      # coordinate clamp (the host scissor does the real clipping)


_IMG_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,63}\.png$")


def sanitize_images(raw, have: dict, parts: dict) -> None:
    """Accept image strips an untrusted child sends: {name: [w, h, y0, base64 RGBA rows]}.
    Rows must arrive in order from row 0; a finished image moves to have[name] =
    (w, h, pixels). The child decoded the file - all the host checks is that the
    numbers describe a small image and that each strip is whole rows of it. Anything
    inconsistent drops that image's partial data."""
    if not isinstance(raw, dict):
        return
    for name, v in list(raw.items())[:2]:
        try:
            if not (isinstance(name, str) and _IMG_NAME.match(name) and ".." not in name):
                continue
            if name in have:
                continue
            w, h, y0, b64 = v
            if not all(isinstance(n, int) and not isinstance(n, bool) for n in (w, h, y0)) \
                    or not isinstance(b64, str):
                continue
            if not (1 <= w <= wx.IMG_MAX_PX and 1 <= h <= wx.IMG_MAX_PX):
                continue
            if len(b64) > (ipc.IMG_STRIP_BYTES + w * 4) * 4 // 3 + 8:
                continue
            part = parts.get(name)
            if part is None:
                if y0 != 0 or len(have) + len(parts) >= wx.IMG_MAX_COUNT:
                    continue
                part = parts[name] = [w, h, bytearray()]
            if (part[0], part[1]) != (w, h) or y0 * w * 4 != len(part[2]):
                parts.pop(name, None)
                continue
            px = base64.b64decode(b64, validate=True)
            if not px or len(px) % (w * 4) or len(part[2]) + len(px) > w * h * 4:
                parts.pop(name, None)
                continue
            part[2] += px
            if len(part[2]) == w * h * 4:
                have[name] = (w, h, bytes(part[2]))
                del parts[name]
        except Exception:
            parts.pop(name, None) if isinstance(name, str) else None
            continue


def sanitize_ops(raw, images=None) -> list:
    """Turn an untrusted op list into a list of safe tuples; bad ops are dropped."""
    out = []
    if not isinstance(raw, list):
        return out
    for op in raw:
        if len(out) >= ipc.MAX_OPS:
            break
        if not isinstance(op, list) or not op:
            continue
        k = op[0]
        try:
            if k == "t" and len(op) == 7:
                out.append(("t", _clean_text(op[1], ipc.MAX_TEXT),
                            _num(op[2], -_C, _C), _num(op[3], -_C, _C),
                            _num(op[4], 6, 80, 16), _color(op[5]), bool(op[6])))
            elif k == "r" and len(op) == 8:
                out.append(("r", _num(op[1], -_C, _C), _num(op[2], -_C, _C),
                            _num(op[3], 0, _C), _num(op[4], 0, _C), _color(op[5]),
                            bool(op[6]), int(_num(op[7], 0, 255, 255))))
            elif k == "l" and len(op) == 7:
                out.append(("l", _num(op[1], -_C, _C), _num(op[2], -_C, _C),
                            _num(op[3], -_C, _C), _num(op[4], -_C, _C),
                            _color(op[5]), _num(op[6], 1, 64, 1)))
            elif k == "c" and len(op) == 5:
                out.append(("c", _num(op[1], -_C, _C), _num(op[2], -_C, _C),
                            _num(op[3], 0, 4000), _color(op[4])))
            elif k == "i" and len(op) == 6 and images and op[1] in images:
                out.append(("i", op[1], _num(op[2], -_C, _C), _num(op[3], -_C, _C),
                            int(_num(op[4], 1, 8, 1)), int(_num(op[5], 0, 255, 255))))
        except Exception:
            continue
    return out


class _BrokerStorage:
    """Per-extra storage executed by the host on the child's behalf, with quotas.
    Confined to <data_dir>/<basename>; never follows a symlink."""
    def __init__(self, d):
        self.dir = d

    def _p(self, name):
        nm = os.path.basename(str(name))
        if not nm or nm in (".", "..") or len(nm) > 64:
            raise ValueError("bad storage name")
        return os.path.join(self.dir, nm)

    def read(self, name):
        try:
            fd = os.open(self._p(name), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except Exception:
            return None
        try:
            with os.fdopen(fd, "rb") as f:
                return f.read(STORAGE_MAX_FILE).decode("utf-8", "replace")
        except Exception:
            return None

    def write(self, name, text):
        if not isinstance(text, str):
            return False
        data = text.encode("utf-8", "replace")
        if len(data) > STORAGE_MAX_FILE:
            return False
        try:
            os.makedirs(self.dir, exist_ok=True)
            p = self._p(name)
            if not os.path.lexists(p) and len(os.listdir(self.dir)) >= STORAGE_MAX_FILES:
                return False
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                         | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return True
        except Exception:
            return False


class SandboxHost:
    """One sandboxed extra. Same surface as wdgo_extras.ExtraHost."""
    def __init__(self, meta, bridge, data_dir, require_isolation=True):
        self.meta = meta
        self._bridge = bridge
        self._data_dir = data_dir
        self._require_iso = bool(require_isolation)
        self.state = "loading"       # loading | running | faulted | closed
        self.error = None
        self.faults = 0
        self.sandboxed = True
        self.isolation = "?"         # "full" | "none" (dev simulator only)
        self.content_hash = ""
        self._perms = set(meta.permissions or [])
        self._store = _BrokerStorage(data_dir)
        self._proc = None
        self._sock = None
        self._framer = ipc.Framer()
        self._out = bytearray()
        self._ops = []
        self._imgs = {}              # name -> (w, h, rgba) declared by the child, validated
        self._img_parts = {}         # name -> [w, h, bytearray] still arriving in strips
        self._awaiting = False
        self._frame_n = 0
        self._t_sent = 0.0
        self._t_spawn = 0.0
        self._acc_dt = 0.0
        self._pressed = set()
        self._chars = []
        self._tokens = float(CALL_BURST)
        self._tok_t = time.monotonic()
        self._log_t0 = 0.0
        self._log_n = 0
        self._keys = list(getattr(bridge, "key_names", None) or ipc.KEY_NAMES)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def open(self):
        try:
            src = wx.collect_sources(self.meta.dir)
            if self.meta.entry_file not in src:
                raise ValueError("entry file '%s' is not a regular file" % self.meta.entry_file)
            self.content_hash = wx.sources_hash(src)
            host_s, child_s = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                host_s.setblocking(False)
                # A fresh interpreter, never a fork: the game's memory (API keys,
                # radio state, GL context) must not exist in the extra's address space.
                self._proc = subprocess.Popen(
                    [sys.executable, "-I", "-B", _CHILD, str(child_s.fileno())],
                    pass_fds=[child_s.fileno()], close_fds=True,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, cwd="/", start_new_session=True,
                    env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
            except Exception:
                host_s.close()
                raise
            finally:
                child_s.close()
            self._sock = host_s
            self._t_spawn = time.monotonic()
            vw, vh = self._view()
            self._send({
                "t": "hello", "proto": ipc.PROTO, "api": wx.EXTRAS_API,
                "id": self.meta.id, "dir": os.path.abspath(self.meta.dir),
                "entry_file": self.meta.entry_file, "entry_symbol": self.meta.entry_symbol,
                "sha": self.content_hash, "perms": sorted(self._perms & wx._KNOWN_PERMS),
                "view": [vw, vh], "mw": self._measure_fit(),
                "iso": self._require_iso, "uid": SANDBOX_UID, "gid": SANDBOX_GID,
                "jail": JAIL_DIR, "mem": MEM_LIMIT,
            })
        except Exception as exc:
            self._fault("sandbox start failed: %s: %s" % (type(exc).__name__, exc))

    def frame(self, dt):
        if self.state in ("faulted", "closed"):
            return
        try:
            self._acc_dt += max(0.0, float(dt))
            self._collect_input()
            self._pump()
            if self.state == "running" and not self._awaiting:
                self._send_frame()
                self._wait_frame(FRAME_WAIT_BUDGET)
            self._watchdog()
        except ipc.ProtocolError as exc:
            self._fault("protocol violation: %s" % exc)
        except Exception as exc:
            self._fault("sandbox host error: %s: %s" % (type(exc).__name__, exc))
        self._render()

    def event(self, key):
        pass          # input travels in the frame snapshot

    def close(self):
        try:
            if self._proc is not None and self._proc.poll() is None and self.state == "running":
                try:
                    self._send({"t": "exit"})
                    end = time.monotonic() + EXIT_GRACE
                    self.state = "closing"
                    while time.monotonic() < end and self._proc.poll() is None:
                        if self._pump(wait=0.02) == "exited":
                            break
                except Exception:
                    pass
        finally:
            self._kill()
            try:
                self._bridge.release_all()
            except Exception:
                pass
            self.state = "closed"

    # ------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------
    def _view(self):
        try:
            w, h = self._bridge.view()
            return int(_num(w, 1, 16384, 800)), int(_num(h, 1, 16384, 480))
        except Exception:
            return 800, 480

    def _measure_fit(self):
        """Monospace text width is linear in the character count; fit it once on the
        host font so the child can answer ctx.measure() without a round trip:
        width(s, size) ~= size * (a * len(s) + c)."""
        try:
            m10 = float(self._bridge.measure("M" * 10, 40))
            m20 = float(self._bridge.measure("M" * 20, 40))
            a = (m20 - m10) / 400.0
            c = m10 / 40.0 - 10.0 * a
            if not (0.05 < a < 3.0) or not math.isfinite(c):
                raise ValueError
            return [a, c]
        except Exception:
            return [0.55, 0.0]

    def _send(self, obj):
        self._out += ipc.pack(obj)
        if len(self._out) > MAX_OUT_BUF:
            raise ipc.ProtocolError("child is not reading")
        self._flush()

    def _flush(self):
        while self._out and self._sock is not None:
            try:
                n = self._sock.send(self._out[:65536])
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                self._out.clear()
                return
            if n <= 0:
                return
            del self._out[:n]

    def _pump(self, wait=0.0):
        """Flush pending output, read whatever the child sent, dispatch it.
        Returns "frame"/"exited" when that message arrived, else None."""
        if self._sock is None:
            return None
        self._flush()
        got = None
        if wait > 0:
            try:
                select.select([self._sock], [], [], wait)
            except Exception:
                pass
        for _ in range(64):
            try:
                data = self._sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                data = b""
            if not data:
                if self.state in ("loading", "running"):
                    code = self._proc.poll() if self._proc is not None else None
                    self._fault("sandbox process ended unexpectedly (exit %s)" % code)
                return got
            for msg in self._framer.feed(data):
                r = self._handle(msg)
                if r:
                    got = r
                if self.state in ("faulted", "closed"):
                    return got
        return got

    def _wait_frame(self, budget):
        end = time.monotonic() + budget
        while self._awaiting and self.state == "running":
            left = end - time.monotonic()
            if left <= 0:
                break
            self._pump(wait=left)

    def _collect_input(self):
        b = self._bridge
        for nm in self._keys:
            try:
                if b.key_pressed(nm):
                    self._pressed.add(nm)
            except Exception:
                pass
        try:
            for ch in (b.chars() or []):
                if isinstance(ch, str) and len(ch) == 1 and len(self._chars) < ipc.MAX_CHARS:
                    self._chars.append(ch)
        except Exception:
            pass

    def _gps(self):
        if "gps" not in self._perms:
            return None
        try:
            f = self._bridge.gps()
        except Exception:
            f = None
        if not isinstance(f, dict):
            return None
        return {"valid": bool(f.get("valid")),
                "lat": float(_num(f.get("lat"), -90, 90)),
                "lon": float(_num(f.get("lon"), -180, 180)),
                "sats": int(_num(f.get("sats"), 0, 255)),
                "speed_kmh": float(_num(f.get("speed_kmh"), 0, 2000)),
                "heading": float(_num(f.get("heading"), 0, 360))}

    def _send_frame(self):
        b = self._bridge
        down = []
        for nm in self._keys:
            try:
                if b.key_down(nm):
                    down.append(nm)
            except Exception:
                pass
        vw, vh = self._view()
        self._frame_n += 1
        self._send({"t": "frame", "n": self._frame_n, "dt": round(min(self._acc_dt, 1.0), 5),
                    "view": [vw, vh], "down": down, "pressed": sorted(self._pressed),
                    "chars": self._chars, "gps": self._gps(), "now": time.time()})
        self._acc_dt = 0.0
        self._pressed = set()
        self._chars = []
        self._awaiting = True
        self._t_sent = time.monotonic()

    def _watchdog(self):
        now = time.monotonic()
        if self.state == "loading" and now - self._t_spawn > START_TIMEOUT:
            self._fault("sandbox did not start within %ds" % START_TIMEOUT)
        elif self.state == "running" and self._awaiting and now - self._t_sent > FRAME_KILL_AFTER:
            self._fault("unresponsive for %ds - killed by the watchdog" % FRAME_KILL_AFTER)
        elif (self.state in ("loading", "running") and self._proc is not None
              and self._proc.poll() is not None):
            self._fault("sandbox process ended unexpectedly (exit %s)" % self._proc.returncode)

    # ------------------------------------------------------------------
    # message handling
    # ------------------------------------------------------------------
    def _handle(self, msg):
        t = msg.get("t")
        if t == "frame":
            if not self._awaiting or msg.get("n") != self._frame_n:
                raise ipc.ProtocolError("unexpected frame reply")
            sanitize_images(msg.get("imgs"), self._imgs, self._img_parts)
            self._ops = sanitize_ops(msg.get("ops"), self._imgs)
            self._take_logs(msg.get("logs"))
            self._awaiting = False
            return "frame"
        if t == "call":
            self._broker(msg)
            return None
        if t == "ready":
            if self.state != "loading":
                raise ipc.ProtocolError("duplicate ready")
            iso = msg.get("iso") if isinstance(msg.get("iso"), dict) else {}
            level = iso.get("level")
            if level != "full" and (self._require_iso or level != "none"):
                self._fault("isolation not established (%s) - refused to run"
                            % _clean_text(iso.get("why", "?"), 120))
                return None
            self.isolation = level
            self.state = "running"
            return None
        if t == "fault":
            self._fault(_clean_text(msg.get("err", "extra faulted"), 300), from_child=True)
            return None
        if t == "exited":
            return "exited"
        raise ipc.ProtocolError("unknown message type")

    def _take_logs(self, logs):
        if not isinstance(logs, list):
            return
        now = time.monotonic()
        if now - self._log_t0 > LOG_WINDOW:
            self._log_t0, self._log_n = now, 0
        for item in logs[:ipc.MAX_LOGS]:
            if self._log_n >= LOG_BURST:
                break
            if isinstance(item, list) and len(item) == 2:
                lvl = item[0] if item[0] in ("info", "ok", "warn", "error") else "info"
                self._log_n += 1
                try:
                    self._bridge.log(_clean_text(item[1], ipc.MAX_LOG_LEN), lvl)
                except Exception:
                    pass

    def _broker(self, msg):
        """Capability broker: the ONLY way the child reaches anything outside its
        process. Method names are a fixed allowlist; permission and size checks are
        done here, on the trusted side, regardless of what the child's ctx thinks."""
        cid = msg.get("id")
        if isinstance(cid, bool) or not isinstance(cid, int):
            raise ipc.ProtocolError("bad call id")
        now = time.monotonic()
        self._tokens = min(float(CALL_BURST), self._tokens + (now - self._tok_t) * CALL_RATE)
        self._tok_t = now
        self._tokens -= 1.0
        if self._tokens < 0:
            raise ipc.ProtocolError("broker call flood")
        m = msg.get("m")
        a = msg.get("a") if isinstance(msg.get("a"), list) else []
        try:
            if m == "storage.read" and len(a) == 1:
                v = self._store.read(a[0])
            elif m == "storage.write" and len(a) == 2:
                v = self._store.write(a[0], a[1])
            elif m in ("loot.networks", "loot.counts"):
                if "loot_read" not in self._perms:
                    raise PermissionError("permission 'loot_read' not granted")
                if m == "loot.networks":
                    v = [{"ssid": _clean_text(n.get("ssid", ""), 64),
                          "bssid": _clean_text(n.get("bssid", ""), 32),
                          "channel": int(_num(n.get("channel", 0), 0, 255))}
                         for n in list(self._bridge.loot_networks() or [])[:LOOT_LIST_MAX]
                         if isinstance(n, dict)]
                else:
                    v = {str(k)[:32]: int(_num(c, 0, 10 ** 9))
                         for k, c in list(dict(self._bridge.loot_counts() or {}).items())[:32]}
            elif m == "loot.save" and len(a) == 2:
                if "loot_write" not in self._perms:
                    raise PermissionError("permission 'loot_write' not granted")
                if not isinstance(a[0], str) or not isinstance(a[1], str) \
                        or len(a[1].encode("utf-8", "replace")) > LOOT_SAVE_MAX:
                    raise ValueError("bad loot.save arguments")
                v = self._bridge.loot_save(a[0][:80], a[1])
                v = str(v) if v else None
            else:
                raise ValueError("unknown broker method")
            self._send({"t": "reply", "id": cid, "ok": True, "v": v})
        except ipc.ProtocolError:
            raise
        except Exception as exc:
            self._send({"t": "reply", "id": cid, "ok": False,
                        "err": "%s: %s" % (type(exc).__name__, _clean_text(exc, 160))})

    # ------------------------------------------------------------------
    # failure + teardown
    # ------------------------------------------------------------------
    def _fault(self, reason, from_child=False):
        if self.state in ("faulted", "closed"):
            return
        self.faults += 1
        self.error = reason
        self.state = "faulted"
        self._ops = []
        self._kill()
        try:
            self._bridge.log("faulted: %s" % reason, "error")
        except Exception:
            pass
        try:
            self._bridge.release_all()
        except Exception:
            pass

    def _kill(self):
        p, self._proc = self._proc, None
        if p is not None:
            try:
                if p.poll() is None:
                    try:
                        os.killpg(p.pid, signal.SIGKILL)   # own session -> own group
                    except Exception:
                        p.kill()
                p.wait(timeout=1.0)
            except Exception:
                pass
        s, self._sock = self._sock, None
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
        self._out.clear()
        self._awaiting = False

    def _render(self):
        b = self._bridge
        if self.state == "loading":
            try:
                b.draw_text("starting sandbox...", 0, 0, 16, "dim", False)
            except Exception:
                pass
            return
        if self.state not in ("running", "closing"):
            return
        for op in self._ops:
            try:
                k = op[0]
                if k == "t":
                    b.draw_text(op[1], op[2], op[3], op[4], op[5], op[6])
                elif k == "r":
                    b.draw_rect(op[1], op[2], op[3], op[4], op[5], op[6], op[7])
                elif k == "l":
                    b.draw_line(op[1], op[2], op[3], op[4], op[5], op[6])
                elif k == "c":
                    b.draw_circle(op[1], op[2], op[3], op[4])
                elif k == "i":
                    w, h, px = self._imgs[op[1]]
                    b.draw_image(op[1], w, h, px, op[2], op[3], op[4], op[5])
            except Exception:
                pass
