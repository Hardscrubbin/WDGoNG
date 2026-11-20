#!/usr/bin/env python3
"""EXTRAS sandbox test-suite — friendly AND hostile extras against the real host.

    python3 extras/sim/test_sandbox.py            # desktop: protocol/broker/watchdog
    sudo python3 extras/sim/test_sandbox.py       # Linux root: + the kernel jail

No game, no raylib. Each case writes a tiny extra into a temp dir, runs it through
wdgo_extras_sandbox.SandboxHost with a fake bridge, and checks what the HOST saw.
As Linux root the suite also proves the jail: no filesystem, no network, no fork,
no way back to root, bounded memory. Exit code 0 = all passed.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(_REPO, "wdgong_ui"))
import wdgo_extras as wx              # noqa: E402
import wdgo_extras_ipc as ipc         # noqa: E402
import wdgo_extras_sandbox as sb      # noqa: E402

FULL = sb.full_isolation_possible()
_passed = _failed = 0
_tmp = tempfile.mkdtemp(prefix="wdgo_sbtest_")
os.chmod(_tmp, 0o755)


def check(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   %s" % what)
    else:
        _failed += 1
        print("  FAIL %s" % what)


class Rec:
    """Fake bridge that records what the host painted / logged / saved."""
    def __init__(self):
        self.texts, self.rects, self.logs, self.saved = [], 0, [], {}
        self.down, self.pressed = set(), set()
        b = types.SimpleNamespace()
        b.draw_text = lambda s, x, y, size, color, bold: self.texts.append(s)
        b.draw_rect = lambda *a: setattr(self, "rects", self.rects + 1)
        b.draw_line = lambda *a: None
        b.draw_circle = lambda *a: None
        self.images = []
        b.draw_image = lambda name, w, h, rgba, x, y, scale, alpha: self.images.append(
            (name, w, h, bytes(rgba), scale, alpha))
        b.measure = lambda s, size=16: len(str(s)) * size * 0.6
        b.key_down = lambda n: n in self.down
        b.key_pressed = lambda n: n in self.pressed
        b.chars = lambda: []
        b.view = lambda: (1232, 616)
        b.gps = lambda: {"valid": True, "lat": 50.06, "lon": 19.93, "sats": 9,
                         "speed_kmh": 12.5, "heading": 90.0}
        b.loot_networks = lambda: [{"ssid": "SIM-AP", "bssid": "AA:BB:CC:DD:EE:FF", "channel": 6}]
        b.loot_counts = lambda: {"networks": 1}
        b.log = lambda msg, level="info": self.logs.append((level, msg))
        b.loot_save = lambda name, text: self.saved.__setitem__(name, text) or "/loot/" + name
        b.release_all = lambda: None
        self.b = b


def make_extra(eid, code, perms=()):
    d = os.path.join(_tmp, eid)
    os.makedirs(d, exist_ok=True)
    os.chmod(d, 0o755)
    with open(os.path.join(d, "extra.json"), "w") as f:
        json.dump({"extra": {"id": eid, "name": eid, "version": "1.0.0", "api": 1,
                             "entry": "main:Extra", "permissions": list(perms)}}, f)
    with open(os.path.join(d, "main.py"), "w") as f:
        f.write(code)
    m = wx._load_meta(d)
    assert not m.error, m.error
    return m


def run(meta, frames=25, until=None, rec=None, timeout=8.0):
    rec = rec or Rec()
    data = os.path.join(_tmp, "data", meta.id)
    h = sb.SandboxHost(meta, rec.b, data, require_isolation=FULL)
    h.open()
    end = time.time() + timeout
    n = 0
    while time.time() < end and h.state in ("loading", "running"):
        rec.texts = []
        h.frame(1 / 30.0)
        if h.state == "running" and h._frame_n >= 1 and not h._awaiting:
            n += 1
        if until and until(h, rec):
            break
        if not until and n >= frames:
            break
        time.sleep(0.01)
    return h, rec


# ----------------------------------------------------------------------
print("isolation available: %s" % ("FULL (Linux root)" if FULL else "none (desktop dev mode)"))

print("\n[1] bundled examples run unchanged in the sandbox")
for eid in ("gps_speedo", "key_copier"):
    m = wx._load_meta(os.path.join(wx.examples_dir(), eid))
    h, rec = run(m, frames=20)
    check(h.state == "running", "%s is running (state=%s err=%s)" % (eid, h.state, h.error))
    check(len(h._ops) > 3, "%s produced draw ops (%d)" % (eid, len(h._ops)))
    check(h.isolation == ("full" if FULL else "none"), "%s isolation=%s" % (eid, h.isolation))
    h.close()
    check(h.state == "closed" and h._proc is None, "%s closed cleanly" % eid)

print("\n[2] ctx parity: input, gps, storage, loot, log, print")
m = make_extra("parity", '''
import json, math
class Extra:
    def on_enter(self, ctx):
        self.n = ctx.storage.read_json("n.json", {"n": 0})["n"]
        ctx.storage.write_json("n.json", {"n": self.n + 1})
        self.seen = ""
    def on_frame(self, ctx, dt):
        if ctx.key_pressed("a"): self.seen += "a"
        if ctx.key_down("up"): self.seen += "U"
        f = ctx.gps.fix()
        ctx.text("n=%d seen=%s lat=%.2f w=%d" % (self.n, self.seen, f["lat"], ctx.w), 0, 0)
        ctx.text("nets=%d" % len(ctx.loot.networks()), 0, 20)
        ctx.text("m=%d" % ctx.measure("abcd", 20), 0, 40)
        if ctx.key_pressed("s"):
            ctx.text("saved=%s" % ctx.loot_save("rep", "hello"), 0, 60)
            ctx.log("log-line", "ok"); print("print-line")
''', perms=("gps", "loot_read", "loot_write"))
rec = Rec()
rec.pressed = {"a", "s"}; rec.down = {"up"}
h, rec = run(m, rec=rec, until=lambda h, r: any("saved=" in t for t in r.texts))
txt = " | ".join(rec.texts)
check("n=0" in txt and "seen=aU" in txt and "lat=50.06" in txt and "w=1232" in txt, "input+gps+view: " + txt[:90])
check("nets=1" in txt, "loot.networks via broker")
check("m=48" in txt, "measure matches host font fit (%s)" % txt[txt.find("m="):][:6])
check(rec.saved.get("rep") == "hello" and "saved=/loot/rep" in txt, "loot_save via broker")
check(("ok", "log-line") in rec.logs and ("info", "print-line") in rec.logs, "ctx.log + print() reach the host log")
h.close()
h, rec = run(m, frames=3)
check(any("n=1" in t for t in rec.texts), "storage persisted across runs")
h.close()

print("\n[3] permissions are enforced by the HOST, not by the child's ctx")
m = make_extra("noperm", '''
class Extra:
    def on_frame(self, ctx, dt):
        ctx.text("gps=%s loot=%s save=%s" % (ctx.gps, ctx.loot, ctx.loot_save), 0, 0)
        b = ctx._b      # a hostile extra reaches past the facade to the raw broker call
        for m, a in (("loot.networks", ()), ("loot.save", ("x", "y")), ("os.system", ("id",))):
            try:
                r = b.loot_networks() if m == "loot.networks" else (
                    b.loot_save(*a) if m == "loot.save" else None)
                ctx.text("%s -> %r" % (m, r), 0, 20)
            except Exception as e:
                ctx.text("%s DENIED %s" % (m, type(e).__name__), 0, 20)
''')
h, rec = run(m, frames=5)
txt = " | ".join(rec.texts)
check("gps=None loot=None save=None" in txt, "ungranted capabilities are None")
check("loot.networks DENIED PermissionError" in txt and "loot.save DENIED PermissionError" in txt,
      "raw broker calls without permission are refused")
check(rec.saved == {}, "nothing was written")
h.close()

print("\n[4] an exception in the extra -> clean fault, game unaffected")
m = make_extra("crasher", "class Extra:\n    def on_frame(self, ctx, dt):\n        return 1 / 0\n")
h, rec = run(m)
check(h.state == "faulted" and "ZeroDivisionError" in (h.error or "") and "main.py:3" in h.error,
      "fault reported with location: %s" % h.error)
check(h._proc is None, "process reaped")
h.close()

print("\n[5] watchdog: an extra that never returns is killed")
m = make_extra("hang", "class Extra:\n    def on_frame(self, ctx, dt):\n        while True: pass\n")
t0 = time.time()
h, rec = run(m, timeout=10)
check(h.state == "faulted" and "watchdog" in (h.error or ""), "killed by the watchdog: %s" % h.error)
check(time.time() - t0 < sb.FRAME_KILL_AFTER + 3, "within the kill window (%.1fs)" % (time.time() - t0))
h.close()

print("\n[6] the game loop never blocks on a slow extra")
m = make_extra("slow", "import time\nclass Extra:\n    def on_frame(self, ctx, dt):\n"
                       "        t=time.time()\n        while time.time()-t < 0.4: pass\n        ctx.text('x',0,0)\n")
h, rec = run(m, frames=1)
worst = 0.0
for _ in range(30):
    t0 = time.time(); h.frame(1 / 30.0); worst = max(worst, time.time() - t0); time.sleep(0.005)
check(worst < 0.08, "worst host frame() call %.0f ms while the extra burns 400 ms/frame" % (worst * 1000))
check(h.state == "running", "slow-but-alive extra keeps running")
h.close()

print("\n[7] flood + garbage: op cap, oversized frame, raw bytes on the socket")
m = make_extra("flood", "class Extra:\n    def on_frame(self, ctx, dt):\n"
                        "        for i in range(20000): ctx.rect(i, 0, 1, 1)\n")
h, rec = run(m, frames=3)
check(h.state == "running" and len(h._ops) <= ipc.MAX_OPS, "ops capped at %d (got %d)" % (ipc.MAX_OPS, len(h._ops)))
h.close()
m = make_extra("bigtext", "class Extra:\n    def on_frame(self, ctx, dt):\n"
                          "        for i in range(4000): ctx.text('Z'*5000, float('nan'), 1e300, 9999, 'nocolor')\n")
h, rec = run(m, frames=3)
check(h.state == "running", "NaN/huge/oversized drawing does not hurt the host (state=%s err=%s)" % (h.state, h.error))
h.close()
m = make_extra("rawsock", '''
import gc
class Extra:
    def on_frame(self, ctx, dt):
        for o in gc.get_objects():
            if type(o).__name__ == "socket":
                o.sendall(b"\\xff\\xff\\xff\\xff" + b"A" * 64)
''')
h, rec = run(m)
check(h.state == "faulted" and "protocol" in (h.error or ""), "raw garbage on the socket -> %s" % h.error)
h.close()
m = make_extra("callflood", "class Extra:\n    def on_frame(self, ctx, dt):\n"
                            "        for i in range(500): ctx.storage.read('a')\n")
h, rec = run(m)
check(h.state == "faulted" and "flood" in (h.error or ""), "broker call flood -> %s" % h.error)
h.close()

print("\n[8] storage quotas + no path traversal")
m = make_extra("quota", '''
class Extra:
    def on_enter(self, ctx):
        self.big = ctx.storage.write("big", "x" * (300 * 1024))
        self.trav = ctx.storage.write("../../escape.txt", "boo")
        self.n = sum(1 for i in range(80) if ctx.storage.write("f%d" % i, "1"))
    def on_frame(self, ctx, dt):
        ctx.text("big=%s trav=%s n=%d" % (self.big, self.trav, self.n), 0, 0)
''')
h, rec = run(m, frames=2)
txt = " | ".join(rec.texts)
check("big=False" in txt, "oversized storage write refused")
check(not os.path.exists(os.path.join(_tmp, "escape.txt")) and not os.path.exists(os.path.join(_tmp, "data", "escape.txt")),
      "'../../escape.txt' stayed inside the extra's data dir")
_qd = os.path.join(_tmp, "data", "quota")
_nq = len(os.listdir(_qd)) if os.path.isdir(_qd) else -1
check(0 <= _nq <= sb.STORAGE_MAX_FILES, "file-count quota held (%d files): %s" % (_nq, txt[:40]))
h.close()

print("\n[9] symlinked source is never followed by the (root) host")
d = os.path.join(_tmp, "symlinky"); os.makedirs(d, exist_ok=True)
secret = os.path.join(_tmp, "secret.py"); open(secret, "w").write("SECRET = 1\n")
os.symlink(secret, os.path.join(d, "stolen.py"))
open(os.path.join(d, "main.py"), "w").write("x = 1\n")
check("stolen.py" not in wx.collect_sources(d), "collect_sources skips symlinks")

print("\n[9b] images: decoded in the child, raw pixels re-checked by the host")
import base64, struct, zlib                                       # noqa: E402


def png(w, h, rgba):
    def ch(k, b):
        return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw, 6)) + ch(b"IEND", b""))


small_px = b"".join(bytes((x * 5 % 256, y * 7 % 256, (x + y) % 256, 255)) for y in range(30) for x in range(40))
big_px = os.urandom(300 * 200 * 4)                               # 240 KB raw: several strips
m = make_extra("pics", '''
class Extra:
    def on_frame(self, ctx, dt):
        ctx.image("small.png", 10, 20, scale=3, alpha=200)
        ctx.image("art/big.png", 0, 0)
        ctx.text("size %dx%d" % ctx.image_size("small.png"), 0, 0)
''')
os.makedirs(os.path.join(m.dir, "art"))
open(os.path.join(m.dir, "small.png"), "wb").write(png(40, 30, small_px))
open(os.path.join(m.dir, "art", "big.png"), "wb").write(png(300, 200, big_px))
h0 = wx.sources_hash(wx.collect_sources(m.dir))
m = wx._load_meta(m.dir)
h, rec = run(m, until=lambda h, r: len(h._imgs) == 2, timeout=10)
rec.images = []
h.frame(1 / 30.0)
got = {i[0]: i for i in rec.images}
check(h.state == "running" and set(got) == {"small.png", "art/big.png"}, "both images reach the renderer (%s)" % sorted(got))
check(got.get("small.png", (0,) * 6)[1:] == (40, 30, small_px, 3, 200), "small image: exact pixels, scale and alpha")
check(got.get("art/big.png", (0,) * 6)[1:4] == (300, 200, big_px), "large image survives the strip transfer bit-exact")
check("size 40x30" in rec.texts, "ctx.image_size reports the real size")
h.close()
open(os.path.join(m.dir, "small.png"), "wb").write(png(40, 30, bytes(4800)))
check(wx.sources_hash(wx.collect_sources(m.dir)) != h0, "changing an image changes the extra's content hash")

m = make_extra("nopic", '''
class Extra:
    def on_frame(self, ctx, dt):
        ctx.image("missing.png", 0, 0)
''')
h, rec = run(m, until=lambda h, r: h.state == "faulted")
check(h.state == "faulted" and "no image" in (h.error or ""), "unknown image name -> clean fault (%s)" % h.error)
h.close()

m = make_extra("hugepic", '''
class Extra:
    def on_frame(self, ctx, dt):
        ctx.image("huge.png", 0, 0)
''')
open(os.path.join(m.dir, "huge.png"), "wb").write(png(513, 4, bytes(513 * 4 * 4)))
h, rec = run(m, until=lambda h, r: h.state == "faulted")
check(h.state == "faulted" and "limit" in (h.error or ""), "image over the pixel limit -> clean fault (%s)" % h.error)
h.close()

d = os.path.join(_tmp, "manypics"); os.makedirs(d, exist_ok=True)
for i in range(wx.IMG_MAX_COUNT + 1):
    open(os.path.join(d, "p%d.png" % i), "wb").write(png(2, 2, bytes(16)))
try:
    wx.collect_sources(d)
    check(False, "too many images are refused")
except ValueError as exc:
    check("too many images" in str(exc), "too many images are refused")

b64 = lambda b: base64.b64encode(b).decode()                      # noqa: E731
have, parts = {}, {}
sb.sanitize_images({"ok.png": [2, 2, 0, b64(bytes(16))]}, have, parts)
check(have.get("ok.png") == (2, 2, bytes(16)), "host accepts a well-formed image")
for label, msg in (
        ("pixel buffer of the wrong length", {"a.png": [2, 2, 0, b64(bytes(15))]}),
        ("more pixels than w*h", {"a.png": [2, 2, 0, b64(bytes(24))]}),
        ("oversized dimensions", {"a.png": [600, 1, 0, b64(bytes(2400))]}),
        ("zero dimension", {"a.png": [0, 2, 0, ""]}),
        ("first strip not at row 0", {"a.png": [2, 4, 2, b64(bytes(16))]}),
        ("path traversal in the name", {"../a.png": [2, 2, 0, b64(bytes(16))]}),
        ("non-image name", {"a.py": [2, 2, 0, b64(bytes(16))]}),
        ("not base64", {"a.png": [2, 2, 0, "!!!!"]}),
        ("wrong types", {"a.png": ["2", 2, 0, b64(bytes(16))]}),
        ("not a list", {"a.png": "x"})):
    sb.sanitize_images(msg, have, parts)
    check(set(have) == {"ok.png"} and not parts, "host drops: %s" % label)
sb.sanitize_images({"s.png": [2, 4, 0, b64(bytes(16))]}, have, parts)
sb.sanitize_images({"s.png": [2, 4, 3, b64(bytes(8))]}, have, parts)          # skips row 2
check("s.png" not in have and "s.png" not in parts, "host drops an image whose strips arrive out of order")
have2, parts2 = {}, {}
for i in range(wx.IMG_MAX_COUNT + 3):
    sb.sanitize_images({"n%d.png" % i: [1, 1, 0, b64(bytes(4))]}, have2, parts2)
check(len(have2) == wx.IMG_MAX_COUNT, "host keeps at most %d images per extra" % wx.IMG_MAX_COUNT)
ops = sb.sanitize_ops([["i", "ok.png", 1, 2, 99, 999], ["i", "ghost.png", 0, 0, 1, 255], ["i", "ok.png", 0, 0]], have)
check(ops == [("i", "ok.png", 1.0, 2.0, 8, 255)], "image ops: undeclared names dropped, scale/alpha clamped (%s)" % ops)

if FULL:
    print("\n[10] KERNEL JAIL (running as Linux root)")
    victim = os.path.join(_tmp, "victim.key"); open(victim, "w").write("TOPSECRET"); os.chmod(victim, 0o644)
    m = make_extra("escape", ('''
import os
class Extra:
    def on_enter(self, ctx):
        r = []
        def t(name, fn):
            try: r.append("%s=%r" % (name, fn()))
            except BaseException as e: r.append("%s!%s" % (name, type(e).__name__))
        t("uid", lambda: (os.getuid(), os.geteuid(), os.getgid(), os.getgroups()))
        t("root", lambda: os.listdir("/"))
        t("victim", lambda: open(__VICTIM__).read())
        t("passwd", lambda: open("/etc/passwd").read()[:10])
        t("write", lambda: open("/x", "w").write("1"))
        t("setuid", lambda: os.setuid(0))
        t("fork", lambda: os.fork())
        t("chroot", lambda: os.chroot("."))
        def net():
            import socket
            return socket.socket()
        t("import_socket", net)
        def sub():
            import subprocess
            return subprocess.run(["id"])
        t("import_subprocess", sub)
        def ct():
            import ctypes
            return 1
        t("import_ctypes", ct)
        t("kill1", lambda: os.kill(1, 9))
        t("mem", lambda: len(bytearray(900 * 1024 * 1024)))
        self.r = r
    def on_frame(self, ctx, dt):
        for i, s in enumerate(self.r): ctx.text(s, 0, i * 12)
''').replace("__VICTIM__", repr(victim)))
    h, rec = run(m, frames=2)
    txt = " | ".join(rec.texts)
    print("     child saw: " + txt)
    check(h.isolation == "full", "host confirmed isolation=full")
    check("uid=(%d, %d, %d, [])" % (sb.SANDBOX_UID, sb.SANDBOX_UID, sb.SANDBOX_GID) in txt, "runs as the dedicated uid, no groups")
    check("root=[]" in txt, "filesystem is empty")
    check("victim!" in txt and "TOPSECRET" not in txt, "cannot read a world-readable secret")
    check("passwd!" in txt and "write!" in txt, "no /etc, no writing")
    check("setuid!PermissionError" in txt and "chroot!PermissionError" in txt, "cannot regain root / re-chroot")
    check("fork!" in txt, "cannot fork")
    check("import_socket!" in txt and "import_subprocess!" in txt and "import_ctypes!" in txt, "socket/subprocess/ctypes not importable")
    check("kill1!" in txt, "cannot signal other processes")
    check("mem!MemoryError" in txt, "memory limit enforced")
    h.close()
    m = make_extra("nonet", '''
import gc
class Extra:
    def on_enter(self, ctx):
        self.r = "no-socket-class"
        for o in gc.get_objects():
            if type(o).__name__ == "socket":
                S = type(o)
                try:
                    s = S(2, 1); s.settimeout(2); s.connect(("1.1.1.1", 53)); self.r = "CONNECTED"
                except BaseException as e:
                    self.r = "blocked:%s" % type(e).__name__
    def on_frame(self, ctx, dt):
        ctx.text(self.r, 0, 0)
''')
    h, rec = run(m, frames=2)
    txt = " | ".join(rec.texts)
    check("blocked:" in txt, "even with a recovered socket class there is no network (%s)" % txt)
    h.close()
else:
    print("\n[10] kernel-jail checks SKIPPED (need Linux + root) - run with sudo on the device")

shutil.rmtree(_tmp, ignore_errors=True)
print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
