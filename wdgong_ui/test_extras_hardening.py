"""EXTRAS hardening tests — run: python3 wdgong_ui/test_extras_hardening.py

Everything happens in temp dirs under a throw-away HOME (the real ~/.wdgo and the
installed extras are never touched) and without network: hubs are local git
repositories / directories signed with a throw-away key.

Covers: the approved snapshot is what runs (swapped files after consent), no root
fallback, symlink/FIFO/hard-link safe reads and writes, bounded manifest / icon /
package walks, version parsing, interrupted updates (crash at every step, in a
subprocess), collisions with hand-installed extras, catalogue rollback (N, N, N-1,
different N, after a restart), staged hub sync with a size limit, IPC and drawing
budgets, the child refusing a foreign parent or a wrong fingerprint, the hub
screen's background status cache and single job queue, and loot quotas.
"""
import base64
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = None if "--crash-child" in sys.argv else tempfile.mkdtemp(prefix="wdgo_harden_")
if _TMP is not None:
    import atexit
    atexit.register(shutil.rmtree, _TMP, True)       # also when a check raises
    os.environ["HOME"] = os.path.join(_TMP, "home")
    os.makedirs(os.environ["HOME"])
os.environ.pop("SUDO_USER", None)
os.environ.pop("WDGO_EXTRAS_STATE_DIR", None)
os.environ.pop("WDGO_EXTRAS_EXAMPLES", None)
sys.path.insert(0, _HERE)
import wdgo_extras as wx              # noqa: E402
import wdgo_extras_hub as hub         # noqa: E402
import wdgo_extras_ipc as ipc         # noqa: E402
import wdgo_extras_png as png         # noqa: E402
import wdgo_extras_sandbox as sb      # noqa: E402

_passed = _failed = 0


def check(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   %s" % what)
    else:
        _failed += 1
        print("  FAIL %s" % what)


def raises(fn, exc=Exception, needle=""):
    try:
        fn()
    except exc as e:
        return needle in str(e)
    return False


def make_extra(root, eid, code, perms=(), version="1.0.0", extra_files=None, api=1):
    d = os.path.join(root, eid)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "extra.json"), "w") as f:
        json.dump({"extra": {"id": eid, "name": eid, "version": version, "api": api,
                             "entry": "main:Extra", "permissions": list(perms)}}, f)
    with open(os.path.join(d, "main.py"), "w") as f:
        f.write(code)
    for rel, data in (extra_files or {}).items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb" if isinstance(data, bytes) else "w") as f:
            f.write(data)
    return d


def mkpng(w, h):
    def ch(k, b):
        return struct.pack(">I", len(b)) + k + b + struct.pack(">I", zlib.crc32(k + b) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + bytes(w * 4) for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + ch(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + ch(b"IDAT", zlib.compress(raw, 9)) + ch(b"IEND", b""))


class Rec:
    def __init__(self):
        self.texts, self.logs, self.saved, self.pressed = [], [], {}, set()
        b = types.SimpleNamespace()
        b.draw_text = lambda s, x, y, size, color, bold: self.texts.append(str(s))
        b.draw_rect = b.draw_line = b.draw_circle = lambda *a: None
        b.draw_image = lambda *a: None
        b.measure = lambda s, size=16: len(str(s)) * size * 0.6
        b.key_down = lambda n: False
        b.key_pressed = lambda n: n in self.pressed
        b.chars = lambda: []
        b.view = lambda: (1000, 600)
        b.gps = lambda: None
        b.loot_networks = lambda: []
        b.loot_counts = lambda: {}
        b.log = lambda msg, level="info": self.logs.append((level, msg))
        b.loot_save = lambda n, t: self.saved.__setitem__(n, t) or "/loot/" + n
        b.release_all = lambda: None
        self.b = b


def drive(h, rec, until, timeout=8.0):
    end = time.time() + timeout
    while time.time() < end and h.state in ("loading", "running"):
        rec.texts = []
        h.frame(1 / 30.0)
        if until(h, rec):
            return True
        time.sleep(0.01)
    return False


# ======================================================================
# crash-injection child: install a new version and die at a chosen point
# ======================================================================
if "--crash-child" in sys.argv:
    point = sys.argv[sys.argv.index("--crash-child") + 1]
    spec = json.loads(sys.stdin.read())
    src = {k: base64.b64decode(v) for k, v in spec["src"].items()}

    def die(*a, **k):
        os._exit(9)
    real_move, real_set, real_store = wx._move_to_backup, wx._set_reg, wx._store_json
    if point == "before-journal":
        wx._store_json = die
    elif point == "after-journal":
        wx._move_to_backup = die
    elif point == "after-backup":
        def mv(*a):
            real_move(*a)
            die()
        wx._move_to_backup = mv
    elif point == "after-rename":
        wx._set_reg = die
    elif point == "after-registry":
        def sr(*a):
            real_set(*a)
            die()
        wx._set_reg = sr
    wx.activate_package(spec["id"], src, spec["sha"], spec["entry"])
    os._exit(0)


EXTRA_CODE = '''
import helper
class Extra:
    def on_frame(self, ctx, dt):
        ctx.text(helper.WORD + " perms=%s" % (ctx.loot is not None), 0, 0)
'''

# ----------------------------------------------------------------------
print("[1] the approved snapshot is what runs")
LOCAL = os.path.join(_TMP, "local")
os.makedirs(LOCAL)
d = make_extra(LOCAL, "swapme", EXTRA_CODE, extra_files={"helper.py": "WORD = 'ORIGINAL'\n"})
meta = wx._load_meta(d)
check(meta.error is None, "test extra is valid (%s)" % meta.error)
rec = Rec()
data = os.path.join(_TMP, "data", "swapme")
h = wx.launch(meta, rec.b, data, require_isolation=False)
check(isinstance(h, wx.ConsentScreen), "unapproved extra -> consent card")
sha0 = h._sha
# swap code, an imported module AND the manifest's permissions while the card is shown
open(os.path.join(d, "helper.py"), "w").write("WORD = 'SWAPPED'\n")
open(os.path.join(d, "main.py"), "a").write("\n# swapped\n")
m2 = json.load(open(os.path.join(d, "extra.json")))
m2["extra"]["permissions"] = ["loot_read"]
json.dump(m2, open(os.path.join(d, "extra.json"), "w"))
h.frame(0.1)
time.sleep(0.45)
h.frame(0.1)
rec.pressed = {"y"}
h.frame(0.1)
rec.pressed = set()
nxt = h.next_host
check(isinstance(nxt, sb.SandboxHost), "[Y] hands over a sandbox host")
check(nxt is not None and nxt.meta.permissions == [], "host carries the APPROVED permission set (none)")
nxt.open()
ok = drive(nxt, rec, lambda hh, r: any("perms=" in t for t in r.texts))
txt = " | ".join(rec.texts)
check(ok and "ORIGINAL perms=False" in txt and "SWAPPED" not in txt,
      "approved bytes ran, not the swapped files (%s)" % txt[:60])
check(nxt.content_hash == sha0, "running fingerprint == approved fingerprint")
nxt.close()
check(wx._read_json_file(wx.consent_file(), {}).get("swapme", {}).get("sha") == sha0,
      "consent recorded for the approved fingerprint")
h2 = wx.launch(wx._load_meta(d), rec.b, data, require_isolation=False)
check(isinstance(h2, wx.ConsentScreen) and h2.meta.permissions == ["loot_read"],
      "the swapped version asks again and shows its new permission")
# approved, then swapped between launch() and open()
d2 = make_extra(LOCAL, "swaplate", EXTRA_CODE, extra_files={"helper.py": "WORD = 'ORIGINAL'\n"})
snap = wx.snapshot(d2)
check(wx.consent_grant(snap.meta, snap.sha), "consent stored")
h3 = wx.launch(wx._load_meta(d2), rec.b, data, require_isolation=False)
check(isinstance(h3, sb.SandboxHost), "approved extra launches without a card")
open(os.path.join(d2, "helper.py"), "w").write("WORD = 'SWAPPED'\n")
h3.open()
drive(h3, rec, lambda hh, r: any("perms=" in t for t in r.texts))
check(any("ORIGINAL" in t for t in rec.texts), "a swap after launch() is not picked up by open()")
h3.close()
# consent write failure -> nothing starts
real_update = wx.update_json
wx.update_json = lambda *a, **k: (_ for _ in ()).throw(OSError(28, "No space left on device"))
try:
    c = wx.launch(wx._load_meta(d), rec.b, data, require_isolation=False)
    c.frame(0.1); time.sleep(0.45); rec.pressed = {"y"}; c.frame(0.1); rec.pressed = set()
    check(c.next_host is None and "NOT started" in (c._fail or ""), "consent not saved -> extra not started")
finally:
    wx.update_json = real_update

print("\n[2] the child runs only for the game that started it, only the approved bytes")


def spawn_child(hello, chunks=()):
    hs, cs = socket.socketpair()
    p = subprocess.Popen([sys.executable, "-I", "-B", sb._CHILD, str(cs.fileno())],
                         pass_fds=[cs.fileno()], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    cs.close()
    hs.sendall(ipc.pack(hello))
    for c in chunks:
        hs.sendall(ipc.pack(c))
    try:
        rc = p.wait(timeout=8)
    except subprocess.TimeoutExpired:
        p.kill()
        rc = "hung"
    hs.settimeout(1)
    got = b""
    try:
        while True:
            b = hs.recv(65536)
            if not b:
                break
            got += b
    except Exception:
        pass
    hs.close()
    return rc, ipc.Framer().feed(got) if got else []


src = {"extra.json": b"{}", "main.py": b"class Extra: pass\n"}
base_hello = {"t": "hello", "proto": ipc.PROTO, "api": 2, "id": "probe", "entry_file": "main.py",
              "entry_symbol": "Extra", "perms": [], "view": [10, 10], "mw": [0.5, 0],
              "iso": False, "uid": sb.SANDBOX_UID, "gid": sb.SANDBOX_GID, "jail": sb.JAIL_DIR,
              "mem": sb.MEM_LIMIT, "src": [[k, len(v)] for k, v in sorted(src.items())]}
chunks = [{"t": "src", "i": i, "o": 0, "b": base64.b64encode(v).decode()}
          for i, (k, v) in enumerate(sorted(src.items()))]
rc, msgs = spawn_child(dict(base_hello, host_pid=os.getpid() + 7777, sha=wx.sources_hash(src)), chunks)
check(rc == 2 and not msgs, "hello from a process that is not our parent -> child quits (rc=%s)" % rc)
rc, msgs = spawn_child(dict(base_hello, host_pid=os.getpid(), sha="0" * 64), chunks)
check(rc == 1 and msgs and msgs[0].get("t") == "fault" and "fingerprint" in msgs[0].get("err", ""),
      "sources that do not hash to the approved fingerprint -> fault, nothing runs")
bad = [dict(chunks[0], o=5)] + chunks[1:]
rc, msgs = spawn_child(dict(base_hello, host_pid=os.getpid(), sha=wx.sources_hash(src)), bad)
check(rc == 1 and msgs and msgs[0].get("t") == "fault", "out-of-order source data -> fault")

print("\n[3] no root fallback")
real_euid, real_avail, real_isroot = wx._euid, sb.available, sb._is_root
try:
    ex = wx.snapshot(os.path.join(wx.examples_dir(), "gps_speedo"))
    wx._euid = lambda: 0
    sb.available = lambda require_isolation=True: False
    h = wx.make_host(ex.meta, Rec().b, os.path.join(_TMP, "d"), snapshot=ex)
    check(isinstance(h, wx._RefusedHost) and "root" in h.error,
          "root + no sandbox -> even a bundled example is refused (%s)" % h.error)
    eh = wx.ExtraHost(ex.meta, Rec().b, os.path.join(_TMP, "d"), snapshot=ex)
    eh.open()
    check(eh.state == "faulted" and "root" in (eh.error or ""), "in-process host refuses to run as root")
    sb._is_root = lambda: True
    sh = sb.SandboxHost(ex.meta, Rec().b, os.path.join(_TMP, "d"), require_isolation=False, snapshot=ex)
    check(sh._require_iso is True, "a root host always requires full isolation")
    check(sb.available(False) is False or sb.full_isolation_possible(),
          "sandbox.available() is False for root without the jail")
finally:
    wx._euid, sb.available, sb._is_root = real_euid, real_avail, real_isroot

print("\n[4] symlinks, FIFOs, hard links: never followed, never blocking")
outside = os.path.join(_TMP, "control.txt")
open(outside, "w").write("CONTROL")
ev = make_extra(LOCAL, "evil_links", "class Extra:\n    pass\n")
os.symlink(outside, os.path.join(ev, "stolen.py"))
os.symlink(_TMP, os.path.join(ev, "subdir"))
os.mkfifo(os.path.join(ev, "fifo.py"))
t0 = time.time()
got = wx.collect_sources(ev)
check(set(got) == {"extra.json", "main.py"} and time.time() - t0 < 2,
      "symlinked file/dir and FIFO skipped without blocking (%s)" % sorted(got))
lnk = os.path.join(LOCAL, "linked_pkg")
os.symlink(ev, lnk)
check(raises(lambda: wx.collect_sources(lnk), OSError), "a symlinked package folder is refused")
check("symlink" in (wx._load_meta(lnk).error or ""), "...and shown with a reason")
fm = os.path.join(LOCAL, "fifo_manifest")
os.makedirs(fm)
os.mkfifo(os.path.join(fm, "extra.json"))
signal.alarm(5)
mm = wx._load_meta(fm)
signal.alarm(0)
check(mm.error is not None, "FIFO manifest -> error, no hang (%s)" % mm.error)
stor = os.path.join(_TMP, "store")
os.makedirs(stor)
st = wx.SafeStorage(stor)
os.symlink(outside, os.path.join(stor, "a.txt"))
check(st.read("a.txt") is None, "storage read through a symlink refused")
check(st.write("a.txt", "boom") and open(outside).read() == "CONTROL",
      "storage write replaces the link, the outside file is untouched")
os.link(outside, os.path.join(stor, "hl.txt"))
check(st.read("hl.txt") is None, "hard-linked file is not read")
os.mkfifo(os.path.join(stor, "pipe"))
t0 = time.time()
check(st.read("pipe") is None and time.time() - t0 < 1, "FIFO in storage: no read, no block")
os.mkdir(os.path.join(stor, "dirname"))
check(st.write("dirname", "x") is False, "a directory under the name is not overwritten")
real_write = os.write
calls = [0]


def failing_write(fd, b):
    calls[0] += 1
    raise OSError(28, "No space left on device")
st.write("keep.txt", "OLD")
os.write = failing_write
try:
    r = st.write("keep.txt", "NEW")
finally:
    os.write = real_write
check(r is False and st.read("keep.txt") == "OLD", "ENOSPC during a write keeps the previous file")
check(not [n for n in os.listdir(stor) if n.startswith(".wdgo-tmp-")], "no temp file left behind")
real_reserve = wx.FS_RESERVE_BYTES
wx.FS_RESERVE_BYTES = 1 << 62
try:
    check(st.write("new.txt", "x") is False, "a write into the game's disk reserve is refused")
finally:
    wx.FS_RESERVE_BYTES = real_reserve

print("\n[5] bounded manifest, package walk and folder scan")
big = make_extra(LOCAL, "big_manifest", "class Extra: pass\n")
mj = json.load(open(os.path.join(big, "extra.json")))
mj["extra"]["about"] = "x" * (40 * 1024)
json.dump(mj, open(os.path.join(big, "extra.json"), "w"))
check("too large" in (wx._load_meta(big).error or ""), "40 KiB manifest refused before parsing")
deep = os.path.join(LOCAL, "deep_json")
os.makedirs(deep)
open(os.path.join(deep, "extra.json"), "w").write("[" * 30000)
check(wx._load_meta(deep).error is not None, "deeply nested JSON -> error, no crash")
many = make_extra(LOCAL, "many_files", "class Extra: pass\n")
for i in range(wx.PKG_MAX_ENTRIES + 5):
    open(os.path.join(many, "junk%04d.txt" % i), "w").close()
check(raises(lambda: wx.collect_sources(many), ValueError, "too many"), "package with >1024 entries refused")
dup = make_extra(LOCAL, "dup_perms", "class Extra: pass\n", perms=["loot_read", "gps", "gps", "loot_read"])
mp = wx._load_meta(dup)
check(mp.error is None and mp.permissions == ["gps", "loot_read"], "permissions de-duplicated and sorted")
check(wx._load_meta(make_extra(LOCAL, "unk_perm", "class Extra: pass\n", perms=["gps", "root"])).error,
      "unknown permission invalidates the manifest")
wx.consent_grant(mp, "f" * 64)
mp2 = wx.ExtraMeta(dup)
mp2.id, mp2.permissions = "dup_perms", ["loot_read", "gps"]
check(wx.consent_ok(mp2, "f" * 64), "same set in another order = same consent")

print("\n[6] versions: validated, numeric order, never crash the scan")
check(wx._vkey("1.10") > wx._vkey("1.9") > wx._vkey("1.9-beta") and wx._vkey("1.9+b1") == wx._vkey("1.9"),
      "1.10 > 1.9 > 1.9-beta; +build ignored")
for v in ("1.²", "١.0", "1..0", "v1.0", "1" * 17, None, 7, [1]):
    try:
        k = wx._vkey(v)
        okv = k < wx._vkey("0.0.1")
    except Exception:
        okv = False
    check(okv, "bad version %r sorts below every valid one, no exception" % (v,))
er = wx.extras_root()
os.makedirs(er, exist_ok=True)
make_extra(er, "sup_ver", "class Extra: pass\n", version="1.²")
make_extra(er, "good_one", "class Extra: pass\n", version="1.10")
ms = {m.id: m for m in wx.scan()}
check("bad version" in (ms.get("sup_ver").error or "") and ms["good_one"].error is None,
      "scan lists both; the bad version is disabled with a reason")
exroot = os.path.join(_TMP, "examples")
make_extra(exroot, "sup_ver", "class Extra: pass\n", version="1.0.0")
real_ex = wx.examples_dir
wx.examples_dir = lambda: exroot
os.environ["WDGO_EXTRAS_EXAMPLES"] = "1"
try:
    pick = {m.id: m for m in wx.scan()}["sup_ver"]
    check(pick.error is None and pick.dir.startswith(exroot), "with two candidates the valid one wins")
finally:
    wx.examples_dir = real_ex
    os.environ.pop("WDGO_EXTRAS_EXAMPLES", None)
shutil.rmtree(os.path.join(er, "sup_ver"))
shutil.rmtree(os.path.join(er, "good_one"))

print("\n[7] icons: limits checked before inflating, decoded once")
real_dobj = png.zlib.decompressobj
inflated = [0]


def counting(*a, **k):
    inflated[0] += 1
    return real_dobj(*a, **k)
png.zlib.decompressobj = counting
try:
    check(raises(lambda: png.decode_icon(mkpng(65, 64)), ValueError, "limit") and inflated[0] == 0,
          "65x64 icon refused at the header, nothing inflated")
    w, h_, px = png.decode_icon(mkpng(64, 64))
    check((w, h_) == (64, 64) and inflated[0] == 1, "64x64 icon decoded exactly once")
    check(png.check_icon(mkpng(10, 12)) == (10, 12), "check_icon keeps its (w, h) contract")
    check(raises(lambda: png.decode_icon(mkpng(8, 8) + b"\0" * (17 * 1024)), ValueError, "limit"),
          "icon over 16 KiB refused")
finally:
    png.zlib.decompressobj = real_dobj

print("\n[8] installs: atomic, recoverable, never eat a hand-installed extra")


def srcs(word):
    return {"extra.json": json.dumps({"extra": {"id": "atom", "name": "atom", "version": "1.0.0",
                                                "api": 1}}).encode(),
            "main.py": ("class Extra:\n    W = %r\n" % word).encode()}


old, new = srcs("OLD"), srcs("NEW")
sha_old, sha_new = wx.sources_hash(old), wx.sources_hash(new)
ent = lambda sha: {"version": "1", "sha": sha, "source": "hub", "official": False, "t": 1}   # noqa: E731
wx.activate_package("atom", old, sha_old, ent(sha_old))
adir = os.path.join(wx.extras_root(), "atom")
check(wx.sources_hash(wx.collect_sources(adir)) == sha_old, "first install in place")
for point in ("before-journal", "after-journal", "after-backup", "after-rename", "after-registry"):
    spec = {"id": "atom", "sha": sha_new, "entry": ent(sha_new),
            "src": {k: base64.b64encode(v).decode() for k, v in new.items()}}
    r = subprocess.run([sys.executable, os.path.abspath(__file__), "--crash-child", point],
                       input=json.dumps(spec).encode(), capture_output=True, timeout=60)
    crashed = r.returncode == 9
    wx.scan()                                                    # = next game start
    cur = wx.sources_hash(wx.collect_sources(adir)) if os.path.isdir(adir) else None
    reg = hub.installed().get("atom", {}).get("sha")
    left = [n for n in os.listdir(wx.extras_root()) if ".stage-" in n]
    check(crashed and cur in (sha_old, sha_new) and reg == cur and not left and not os.path.exists(wx.txn_file()),
          "crash %-15s -> whole %s version, registry agrees, no leftovers"
          % (point, "new" if cur == sha_new else "old" if cur == sha_old else "NO"))
    if cur == sha_new:                                           # back to old for the next round
        wx.activate_package("atom", old, sha_old, ent(sha_old))
real_set = wx._set_reg
wx._set_reg = lambda *a: (_ for _ in ()).throw(OSError(28, "No space left on device"))
try:
    okr = raises(lambda: wx.activate_package("atom", new, sha_new, ent(sha_new)), OSError, "previous version kept")
finally:
    wx._set_reg = real_set
check(okr and wx.sources_hash(wx.collect_sources(adir)) == sha_old and hub.installed()["atom"]["sha"] == sha_old,
      "registry write failure -> error reported, old version still active")
real_rename = os.rename


def failing_rename(a, b, **kw):
    if ".stage-" in str(a):
        raise OSError(5, "I/O error")
    return real_rename(a, b, **kw)
os.rename = failing_rename
try:
    okf = raises(lambda: wx.activate_package("atom", new, sha_new, ent(sha_new)), OSError, "I/O")
finally:
    os.rename = real_rename
check(okf and wx.sources_hash(wx.collect_sources(adir)) == sha_old and not os.path.exists(wx.txn_file()),
      "failure after the old folder moved away -> old version put back, journal cleared")
bk = os.path.join(wx.extras_root(), wx.BACKUP_DIR)
check(os.path.isdir(bk) and 0 < len([n for n in os.listdir(bk) if n.startswith("atom-")]) <= wx.KEEP_BACKUPS,
      "previous versions kept in the backup area (bounded)")
# hand-installed extra with the same id as a hub entry
hand = make_extra(wx.extras_root(), "handmade", "class Extra:\n    MINE = 1\n")
open(os.path.join(hand, "notes.txt"), "w").write("my notes")
hs = {"extra.json": json.dumps({"extra": {"id": "handmade", "name": "h", "version": "2.0.0", "api": 1}}).encode(),
      "main.py": b"class Extra: pass\n"}
hsha = wx.sources_hash(hs)
check(raises(lambda: wx.activate_package("handmade", hs, hsha, ent(hsha)), wx.PackageCollision),
      "installing over a hand-installed extra needs confirmation")
check(open(os.path.join(hand, "notes.txt")).read() == "my notes", "...and the hand-installed folder is untouched")
wx.activate_package("handmade", hs, hsha, ent(hsha), replace_local=True)
kept = [n for n in os.listdir(bk) if n.startswith("handmade-")]
check(kept and open(os.path.join(bk, kept[0], "notes.txt")).read() == "my notes",
      "confirmed replace keeps the whole original (incl. extra files) for restore")
check(hub.uninstall("handmade") and not os.path.exists(hand), "uninstall removes the hub copy")
check(not hub.uninstall("../../etc"), "uninstall ignores ids that are not hub-installed")

print("\n[9] signed catalogue: rollback refused (N, N, N-1, other N, restart)")
KEY = os.path.join(_TMP, "hubkey")
SIGNERS = os.path.join(_TMP, "signers")
subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", KEY], check=True)
open(SIGNERS, "w").write("%s %s" % (hub.SIGNER_IDENTITY, open(KEY + ".pub").read()))


def catalogue(name, seq, note=""):
    cdir = os.path.join(_TMP, "cats", name)
    os.makedirs(cdir, exist_ok=True)
    idx = {"schema": 1, "extras": {}, "note": note}
    if seq is not None:
        idx["catalog_seq"] = seq
    open(os.path.join(cdir, "index.json"), "w").write(json.dumps(idx, sort_keys=True))
    sig = os.path.join(cdir, "index.json.sig")
    if os.path.exists(sig):
        os.unlink(sig)
    subprocess.run(["ssh-keygen", "-Y", "sign", "-f", KEY, "-n", hub.NAMESPACE,
                    os.path.join(cdir, "index.json")], check=True, capture_output=True)
    return cdir


def accepts(cdir):
    try:
        hub.verify_index(cdir, signers=SIGNERS)
        return True
    except hub.HubError:
        return False
c_none = catalogue("none", None)
check(accepts(c_none), "unnumbered catalogue accepted while none was seen")
c5 = catalogue("n5", 5)
check(accepts(c5), "release N accepted")
check(accepts(c5), "the same release N again accepted")
check(not accepts(catalogue("n4", 4)), "release N-1 refused")
check(not accepts(catalogue("n5b", 5, "other")), "a different catalogue with the same N refused")
check(not accepts(c_none), "unnumbered catalogue refused once a number was seen")
check(accepts(catalogue("n6", 6)), "release N+1 accepted")
r = subprocess.run([sys.executable, "-c",
                    "import sys; sys.path.insert(0, %r); import wdgo_extras_hub as h\n"
                    "try:\n    h.verify_index(%r, signers=%r); print('ACCEPTED')\n"
                    "except h.HubError as e: print('REFUSED', e)" % (_HERE, c5, SIGNERS)],
                   capture_output=True, text=True, env=dict(os.environ), timeout=60)
check("REFUSED" in r.stdout and "OLDER" in r.stdout, "after a restart release 5 is still refused (%s)" % r.stdout.strip()[:60])
check(not accepts(catalogue("neg", -1)) and not accepts(catalogue("huge", 2 ** 64)), "bad release numbers refused")
os.unlink(hub.hub_state_file())                  # back to a fresh device for the sync tests

print("\n[10] hub sync: staged, verified before swap, bounded")
HUBREPO = os.path.join(_TMP, "hubrepo")
shutil.copytree(os.path.join(os.path.dirname(_HERE), "extras", "examples", "gps_speedo"),
                os.path.join(HUBREPO, "extras", "gps_speedo"), ignore=shutil.ignore_patterns("__pycache__"))
G = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", HUBREPO]
subprocess.run(["git", "init", "-q", "-b", "main", HUBREPO], check=True)


def publish(sign=True, extra=None):
    subprocess.run([sys.executable, os.path.join(os.path.dirname(_HERE), "extras", "hub",
                    "hub_build_index.py"), HUBREPO], check=True, capture_output=True)
    sig = os.path.join(HUBREPO, "index.json.sig")
    if os.path.exists(sig):
        os.unlink(sig)
    if sign:
        subprocess.run(["ssh-keygen", "-Y", "sign", "-f", KEY, "-n", hub.NAMESPACE,
                        os.path.join(HUBREPO, "index.json")], check=True, capture_output=True)
    if extra:
        extra()
    subprocess.run(G + ["add", "-A"], check=True)
    subprocess.run(G + ["commit", "-q", "-m", "p", "--allow-empty"], check=True)


publish()
hub.signers_file = lambda: SIGNERS
cache = hub.sync(HUBREPO)
check(os.path.isfile(os.path.join(cache, "index.json")) and "gps_speedo" in hub.verify_index(cache),
      "first sync verified and in place")
good = open(os.path.join(cache, "index.json"), "rb").read()
publish(extra=lambda: open(os.path.join(HUBREPO, "index.json"), "a").write("\n"))
check(raises(lambda: hub.sync(HUBREPO), hub.HubError, "NOT valid"), "a badly signed hub refuses to sync")
check(open(os.path.join(cache, "index.json"), "rb").read() == good and hub.verify_index(cache),
      "...and the last good checkout stays in place")
publish(extra=lambda: open(os.path.join(HUBREPO, "blob.bin"), "wb").write(os.urandom(3 * 1024 * 1024)))
real_max = hub.SYNC_MAX_BYTES
hub.SYNC_MAX_BYTES = 256 * 1024
real_poll = hub.SYNC_POLL
hub.SYNC_POLL = 0.01
try:
    okb = raises(lambda: hub.sync(HUBREPO), hub.HubError, "")
finally:
    hub.SYNC_MAX_BYTES, hub.SYNC_POLL = real_max, real_poll
parent = os.path.dirname(cache)
check(okb and not [n for n in os.listdir(parent) if ".stage-" in n or ".old-" in n],
      "oversized download killed and its staging removed")
check(open(os.path.join(cache, "index.json"), "rb").read() == good, "last good checkout kept after the abort")
check(raises(lambda: hub.sync("ext::sh -c id"), hub.HubError, "bad hub url"), "non-https transports refused")

print("\n[11] hub screen: background status, one job queue for the whole game")
cat = {"gps_speedo": dict(hub.verify_index(cache)["gps_speedo"])}
counts = {"collect": 0, "state": 0}
real_cs, real_ps = wx.collect_sources, wx.package_state
wx.collect_sources = lambda d: counts.__setitem__("collect", counts["collect"] + 1) or real_cs(d)
wx.package_state = lambda e: counts.__setitem__("state", counts["state"] + 1) or real_ps(e)
hub._HUB.cat, hub._HUB.rows, hub._HUB.status = cat, list(cat.values()), {"gps_speedo": ""}
rb = Rec()
scr = hub.HubScreen(rb.b)
for i in range(300):
    scr.frame(1 / 30.0)
    if i == 150:
        scr._detail = cat["gps_speedo"]
check(counts == {"collect": 0, "state": 0}, "300 idle frames (list + detail card) read no package files (%s)" % counts)
wx.collect_sources, wx.package_state = real_cs, real_ps
n_inst = [0]
gate = threading.Event()
real_install = hub.install


def slow_install(eid, catalogue, cache=None, replace_local=None):
    n_inst[0] += 1
    gate.wait(5)
    return "/x"
hub.install = slow_install
real_refresh = hub._job_refresh
hub._job_refresh = lambda: None
try:
    s1 = hub.HubScreen(rb.b)
    s1._install(cat["gps_speedo"])
    s1.close()
    s2 = hub.HubScreen(rb.b)
    s2.open()                                    # must not start a refresh while busy
    s2._install(cat["gps_speedo"])               # refused: one job at a time
    gate.set()
    end = time.time() + 5
    while hub.busy() and time.time() < end:
        time.sleep(0.02)
    check(n_inst[0] == 1, "reopening the hub during an install performs ONE install")
    check(hub.take_changes() is True and hub.take_changes() is False,
          "the menu gets exactly one rescan signal, even after the screen closed")
finally:
    hub.install, hub._job_refresh = real_install, real_refresh

print("\n[12] IPC budget per frame and per-state messages")
pm = wx.ExtraMeta(os.path.join(_TMP, "ipcx"))
pm.id = "ipcx"
ph = sb.SandboxHost(pm, Rec().b, os.path.join(_TMP, "data", "ipcx"), require_isolation=False)
host_s, child_s = socket.socketpair()
host_s.setblocking(False)
ph._sock, ph.state = host_s, "running"
child_s.sendall(b"".join(ipc.pack({"t": "call", "id": i, "m": "storage.read", "a": ["x"]}) for i in range(100)))
time.sleep(0.05)
ph._new_budget()
ph._pump()
first = ph._calls_seen
check(0 < first <= sb.PUMP_MAX_MSGS and len(ph._inq) > 0, "one frame handles at most %d messages (%d), the rest wait"
      % (sb.PUMP_MAX_MSGS, first))
ph._awaiting, ph._frame_n = True, 1
big = {"t": "frame", "n": 1, "ops": [["t", "y" * 200, 0, 0, 16, "txt", False]] * 2200, "logs": []}
blob = ipc.pack(big)
check(len(blob) > 3 * sb.PUMP_MAX_BYTES and len(blob) <= ipc.MAX_MSG + 4,
      "largest-class legal message: %d bytes" % len(blob))
threading.Thread(target=lambda: child_s.sendall(blob), daemon=True).start()
frames, worst = 0, 0.0
while ph._awaiting and frames < 200 and ph.state == "running":
    ph._new_budget()
    t0 = time.monotonic()
    ph._pump(wait=0.005)
    worst = max(worst, time.monotonic() - t0 - 0.005)
    frames += 1
check(not ph._awaiting and frames >= 4, "a message bigger than one frame's quota arrives over %d frames" % frames)
check(len(ph._ops) <= sb.HOST_MAX_OPS and sum(len(o[1]) for o in ph._ops if o[0] == "t") <= sb.HOST_MAX_CHARS,
      "accepted frame trimmed to the drawing budget (%d ops)" % len(ph._ops))
child_s.sendall(ipc.pack({"t": "exited"}))
time.sleep(0.05)
ph._new_budget()
check(raises(lambda: ph._pump(), ipc.ProtocolError, "exited"), "'exited' while running is a protocol violation")
host_s.close(); child_s.close()
ops = sb.sanitize_ops([["r", 0, 0, 5000, 5000, "txt", True, 255]] * 10 + [["r", 0, 0, 1, 1, "txt", True, 255]] * 3000,
                      {}, (1000, 600))
check(len(ops) <= sb.HOST_MAX_OPS and sum(1 for o in ops if o[3] == 5000) <= 2,
      "covered-pixel budget: full-screen fills capped at %d screens" % sb.HOST_MAX_AREA_SCREENS)

print("\n[13] loot writes: own namespace, quotas, symlink-safe")
LOOT = os.path.join(_TMP, "loot")
p = wx.loot_save(LOOT, "key_copier", "../../etc/passwd", "hello")
check(p.startswith(os.path.join(LOOT, "extras", "key_copier")) and open(p).read() == "hello",
      "name sanitised into loot/extras/<id>/")
idir = os.path.join(LOOT, "extras", "key_copier")
os.symlink(outside, os.path.join(idir, "victim.txt"))
check(raises(lambda: wx.loot_save(LOOT, "key_copier", "victim.txt", "over"), ValueError, "not a regular file")
      and open(outside).read() == "CONTROL", "a name that is a symlink is refused, never written through")
check(raises(lambda: wx.loot_save(LOOT, "key_copier", "x", "z" * (wx.LOOT_SAVE_MAX + 1)), ValueError),
      "per-file limit")
real_files = wx.LOOT_MAX_FILES
wx.LOOT_MAX_FILES = 3
try:
    wx.loot_save(LOOT, "key_copier", "c.txt", "1")
    check(raises(lambda: wx.loot_save(LOOT, "key_copier", "d.txt", "1"), ValueError, "limit"),
          "per-extra file quota")
    check(wx.loot_save(LOOT, "key_copier", "c.txt", "2"), "rewriting an existing name stays allowed")
finally:
    wx.LOOT_MAX_FILES = real_files

print("\n[14] saved data follows a moved data root (once, link-safe)")
os.environ["WDGO_EXTRAS_STATE_DIR"] = os.path.join(_TMP, "protected")
try:
    legacy = os.path.join(os.environ["HOME"], ".wdgo", "extras-data", "mig_x")
    os.makedirs(legacy)
    open(os.path.join(legacy, "cal.json"), "w").write('{"k": 1}')
    os.symlink(outside, os.path.join(legacy, "leak.txt"))
    newd = os.path.join(wx.data_root(), "mig_x")
    check(wx.legacy_data_dir(newd) == legacy, "old data location detected")
    ms = wx.SafeStorage(newd)
    check(ms.read("cal.json") == '{"k": 1}' and ms.read("leak.txt") is None,
          "regular files copied over, a symlink is not")
    check(sorted(os.listdir(newd)) == ["cal.json"], "nothing else copied (%s)" % os.listdir(newd))
finally:
    os.environ.pop("WDGO_EXTRAS_STATE_DIR", None)

print("\n[15] folder scan overflow is visible")
os.environ["HOME"] = os.path.join(_TMP, "home2")
os.makedirs(wx.extras_root())
for i in range(wx.ROOT_MAX_ENTRIES + 10):
    os.mkdir(os.path.join(wx.extras_root(), "x%04d" % i))
rows = wx.scan()
check(any("more than" in (m.error or "") for m in rows), "an overflowing extras folder shows a disabled row")

shutil.rmtree(_TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
