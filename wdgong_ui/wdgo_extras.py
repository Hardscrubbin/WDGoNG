"""WatchDogsGo EXTRAS — Phase 1 platform (creator SDK, in-process).

Third-party mini-apps dropped into ~/.wdgo/extras/<id>/ are discovered here,
listed under the EXTRAS menu, and driven through a small, documented `ctx`
facade. See docs/EXTRAS_spec.md (design) and docs/EXTRAS_authoring.md (author
guide).

PHASE 1 IS IN-PROCESS: an extra runs with the game's privileges (root). It is
intended for code the USER wrote or personally vetted. The real security
boundary — a non-root subprocess + capability broker — is Phase 2. The `ctx`
facade is deliberately transport-agnostic (verbs only, no raw managers, no
`import pyray`) so the SAME extra code will run unchanged once Phase 2 lands.

This module is pure stdlib — it never imports raylib. The host (main.py) injects
a small `bridge` of drawing/input/sensor callables; everything raylib-specific
stays in main.py.
"""
import importlib.util
import json
import os
import re
import shutil
import sys
import time
import traceback

EXTRAS_API = 2                       # bump on any breaking change to ctx / hook contract

_ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")
# capabilities an extra may declare in its manifest "permissions" (v1 grantable).
# draw / input / log / clock / storage are always available and need no permission.
_KNOWN_PERMS = {"gps", "loot_read", "loot_write"}
PALETTE = ("txt", "hi", "dim", "teal", "danger", "warn",
           "success", "magenta", "ice", "void", "edge", "purple")


def extras_root() -> str:
    """User drop-in dir for third-party extras (survives game OTA)."""
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras")


def examples_dir() -> str:
    """Bundled example extras, in the repo. Scanned directly so they always
    match the installed game version (they update with the game's OTA)."""
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "extras", "examples")


def data_root() -> str:
    """Persistent per-extra writable data, separate from the extra's code so it
    survives game updates and works for in-repo examples too (<data_root>/<id>/)."""
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-data")


# ======================================================================
# Manifest
# ======================================================================
class ExtraMeta:
    def __init__(self, d):
        self.dir = d
        self.id = os.path.basename(d.rstrip(os.sep))
        self.name = self.id
        self.author = ""
        self.version = "0.0.0"
        self.api = 0
        self.entry = "main:Extra"
        self.entry_symbol = "Extra"
        self.entry_file = "main.py"
        self.permissions = []
        self.hardware = []
        self.icon = None
        self.description = ""      # one-line (menu subtitle + hub card)
        self.about = ""            # long "what it does / how it works" (hub detail)
        self.category = "misc"
        self.tags = []
        self.homepage = ""
        self.error = None          # str -> invalid: shown greyed, cannot launch
        self.update_note = None    # set when a newer version superseded an installed copy


def _load_meta(d) -> ExtraMeta:
    m = ExtraMeta(d)
    mf = os.path.join(d, "extra.json")
    if not os.path.isfile(mf):
        m.error = "no extra.json"
        return m
    try:
        with open(mf, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as exc:
        m.error = "bad extra.json: %s" % exc
        return m
    e = raw.get("extra", raw) if isinstance(raw, dict) else {}
    try:
        m.name = str(e.get("name", m.id))[:32]
        m.author = str(e.get("author", ""))[:40]
        m.version = str(e.get("version", "0.0.0"))[:16]
        try:
            m.api = int(e.get("api", 0))
        except (TypeError, ValueError):
            m.api = 0
        m.entry = str(e.get("entry", "main:Extra"))
        m.entry_symbol = m.entry.partition(":")[2] or "Extra"
        _mf = m.entry.partition(":")[0] or "main"
        m.entry_file = _mf if _mf.endswith(".py") else _mf + ".py"
        m.permissions = [str(x) for x in (e.get("permissions") or [])]
        m.hardware = [str(x) for x in (e.get("hardware") or [])]
        m.icon = e.get("icon")
        m.description = str(e.get("description", ""))[:120]
        m.about = str(e.get("about", ""))[:2000]
        m.category = str(e.get("category", "misc"))[:24]
        m.tags = [str(x)[:20] for x in (e.get("tags") or [])][:8]
        m.homepage = str(e.get("homepage", ""))[:200]
    except Exception as exc:
        m.error = "manifest error: %s" % exc
        return m
    # ---- validation (reject -> greyed, with a reason the author can read) ----
    if not _ID_RE.match(m.id):
        m.error = "bad id '%s' (need ^[a-z0-9_]{3,32}$ and == folder name)" % m.id
        return m
    if m.api <= 0:
        m.error = "manifest missing a valid integer 'api'"
        return m
    if m.api > EXTRAS_API:
        m.error = "needs api %d (game has %d) — update the game [U]" % (m.api, EXTRAS_API)
        return m
    if not os.path.isfile(os.path.join(d, m.entry_file)):
        m.error = "entry file '%s' missing" % m.entry_file
        return m
    bad = [p for p in m.permissions if p not in _KNOWN_PERMS]
    if bad:
        m.error = "unknown permission(s): %s (allowed: %s)" % (
            ", ".join(bad), ", ".join(sorted(_KNOWN_PERMS)))
        return m
    return m


def _vkey(v):
    """Parse a semver-ish string into a comparable int tuple ('1.2.0'->(1,2,0))."""
    out = []
    for part in str(v).split("."):
        num = ""
        for ch in part:
            if ch.isdigit():
                num += ch
            else:
                break
        out.append(int(num) if num else 0)
    return tuple(out) or (0,)


def scan() -> list:
    """Discover the extras the player has: the user dir (~/.wdgo/extras), which is
    where the Hub installs and where hand-copied extras go. Reads manifests only —
    never imports an extra's code (that happens lazily on launch).

    The examples shipped in the repository are reference code for authors; they are
    published through the hub like any other extra, so they are NOT listed here by
    default (the list shows what is installed, nothing else). A developer can list
    them too with WDGO_EXTRAS_EXAMPLES=1.

    When the same id exists in both places, the HIGHER manifest 'version' wins
    (ties -> the shipped example, which is canonical and updates with the game).
    So a freshly-updated example supersedes an older copy left in the user dir —
    no stale shadow, and we note the update. Returns ExtraMeta list, valid
    first, by name.
    """
    cands = {}      # id -> list of (origin, meta); origin 0=user dir, 1=shipped example
    roots = [(0, extras_root())]
    if os.environ.get("WDGO_EXTRAS_EXAMPLES") == "1":
        roots.append((1, examples_dir()))
    for origin, root in roots:
        try:
            os.makedirs(root, exist_ok=True)
        except Exception:
            pass
        try:
            entries = sorted(os.listdir(root))
        except Exception:
            entries = []
        for nm in entries:
            d = os.path.join(root, nm)
            if nm.startswith(("_", ".")) or not os.path.isdir(d):
                continue
            m = _load_meta(d)
            cands.setdefault(m.id, []).append((origin, m))
    out = []
    for lst in cands.values():
        # highest version wins; on a tie the shipped example (origin 1) wins
        best = sorted(lst, key=lambda om: (_vkey(om[1].version), om[0]))[-1][1]
        superseded = [m for o, m in lst if m is not best and _vkey(best.version) > _vkey(m.version)]
        if superseded:
            best.update_note = "updated to v%s" % best.version
        out.append(best)
    out.sort(key=lambda m: (m.error is not None, m.name.lower()))
    return out


# ======================================================================
# Source collection + content hash (shared by the host and the sandbox child)
# ======================================================================
SRC_MAX_FILES = 64
SRC_MAX_FILE_BYTES = 512 * 1024
SRC_MAX_TOTAL_BYTES = 2 * 1024 * 1024
from wdgo_extras_png import IMG_MAX_BYTES, IMG_MAX_COUNT, IMG_MAX_PX, IMG_MAX_TOTAL    # noqa: E402


def collect_sources(d) -> dict:
    """Read an extra's content into memory: {relpath: bytes} for extra.json, every
    *.py and every *.png (small images, see wdgo_extras_png) up to 3 directories deep. Symlinks are never followed (a hostile package
    must not make the root-owned host read files outside its own dir), and the
    counts/sizes are capped. Raises ValueError when a cap is exceeded."""
    import stat as _stat
    out = {}
    total = 0
    images = [0, 0]                 # count, bytes
    root = os.path.abspath(d)

    def _walk(rel, depth):
        nonlocal total
        base = os.path.join(root, rel) if rel else root
        for nm in sorted(os.listdir(base)):
            if nm.startswith(".") or nm == "__pycache__":
                continue
            p = os.path.join(base, nm)
            r = (rel + "/" + nm) if rel else nm
            st = os.lstat(p)
            if _stat.S_ISLNK(st.st_mode):
                continue
            if _stat.S_ISDIR(st.st_mode):
                if depth < 3:
                    _walk(r, depth + 1)
                continue
            if not _stat.S_ISREG(st.st_mode):
                continue
            is_img = nm.endswith(".png")
            if not (nm.endswith(".py") or r == "extra.json" or is_img):
                continue
            if is_img:
                images[0] += 1
                if st.st_size > IMG_MAX_BYTES:
                    raise ValueError("image too large: %s (max %d bytes)" % (r, IMG_MAX_BYTES))
                images[1] += st.st_size
                if images[0] > IMG_MAX_COUNT:
                    raise ValueError("too many images (max %d)" % IMG_MAX_COUNT)
                if images[1] > IMG_MAX_TOTAL:
                    raise ValueError("images too large in total (max %d bytes)" % IMG_MAX_TOTAL)
            if st.st_size > SRC_MAX_FILE_BYTES:
                raise ValueError("source file too large: %s" % r)
            fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as f:
                data = f.read(SRC_MAX_FILE_BYTES + 1)
            total += len(data)
            if len(out) >= SRC_MAX_FILES or total > SRC_MAX_TOTAL_BYTES:
                raise ValueError("extra has too much code (max %d files / %d bytes)"
                                 % (SRC_MAX_FILES, SRC_MAX_TOTAL_BYTES))
            out[r] = data

    _walk("", 0)
    return out


def sources_hash(src: dict) -> str:
    """Stable sha256 over (relpath, content) pairs — the extra's content identity."""
    import hashlib
    h = hashlib.sha256()
    for r in sorted(src):
        rb = r.encode("utf-8")
        h.update(len(rb).to_bytes(4, "big")); h.update(rb)
        h.update(len(src[r]).to_bytes(8, "big")); h.update(src[r])
    return h.hexdigest()


def trust_file() -> str:
    """User-maintained allowlist of extras allowed to run IN-PROCESS (as root):
    {"<id>": "<sha256 from sources_hash>"}. Pinned to the content hash, so an edited
    extra drops back to the sandbox until re-pinned. Bundled examples need no entry."""
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-trusted.json")


def is_trusted(meta) -> bool:
    """True when this exact code may run in-process: a bundled example (ships with
    the game) or an id+hash the user pinned in trust_file()."""
    try:
        if os.path.dirname(os.path.abspath(meta.dir)) == os.path.abspath(examples_dir()):
            return True
        with open(trust_file(), "r", encoding="utf-8") as f:
            pins = json.load(f)
        want = pins.get(meta.id) if isinstance(pins, dict) else None
        return bool(want) and want == sources_hash(collect_sources(meta.dir))
    except Exception:
        return False


def make_host(meta, bridge, data_dir, require_isolation=True):
    """Pick the runtime for an extra.

    Default is the SANDBOX (non-root, jailed subprocess + capability broker; see
    wdgo_extras_sandbox). Only code the user explicitly trusts (bundled examples or
    a hash pinned in trust_file()) may fall back to the in-process host, and only
    when the sandbox cannot run on this machine. Foreign code never runs in-process.
    """
    try:
        import wdgo_extras_sandbox as _sb
    except Exception:
        _sb = None
    if _sb is not None and _sb.available(require_isolation):
        return _sb.SandboxHost(meta, bridge, data_dir, require_isolation=require_isolation)
    if is_trusted(meta):
        try:
            bridge.log("sandbox unavailable here - trusted extra runs in-process", "warn")
        except Exception:
            pass
        return ExtraHost(meta, bridge, data_dir)
    return _RefusedHost(meta, "sandbox unavailable on this system - untrusted extras "
                              "cannot run (see docs/EXTRAS_authoring.md)")


# ======================================================================
# Provenance + consent (who vouches for this code, and did the user agree to it)
# ======================================================================
def _wdgo_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".wdgo")


def _read_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, type(default)) else default
    except Exception:
        return default


def _write_json_file(path, obj) -> bool:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, sort_keys=True)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def installed_file() -> str:
    """Registry of extras installed from the hub: {id: {version, sha, source}}."""
    return os.path.join(_wdgo_dir(), "extras-installed.json")


def consent_file() -> str:
    """Per-extra consent, pinned to the content hash: {id: {sha, perms, t}}."""
    return os.path.join(_wdgo_dir(), "extras-consent.json")


def is_bundled(meta) -> bool:
    try:
        return os.path.dirname(os.path.abspath(meta.dir)) == os.path.abspath(examples_dir())
    except Exception:
        return False


def provenance(meta, sha) -> str:
    """'bundled' (ships with the game) | 'hub' (installed from the signed hub and
    byte-identical to what was verified) | 'local' (anything else - unverified)."""
    if is_bundled(meta):
        return "bundled"
    ent = _read_json_file(installed_file(), {}).get(meta.id)
    if isinstance(ent, dict) and ent.get("source") == "hub" and ent.get("sha") == sha:
        return "hub"
    return "local"


def is_official(meta, sha) -> bool:
    """True only for an extra installed from the signed hub, byte-identical to what was
    verified, whose catalogue entry carried official=true (the maintainer's own list)."""
    ent = _read_json_file(installed_file(), {}).get(meta.id)
    return (isinstance(ent, dict) and ent.get("source") == "hub" and ent.get("sha") == sha
            and ent.get("official") is True)


def consent_ok(meta, sha) -> bool:
    ent = _read_json_file(consent_file(), {}).get(meta.id)
    return (isinstance(ent, dict) and ent.get("sha") == sha
            and sorted(ent.get("perms") or []) == sorted(meta.permissions or []))


def consent_grant(meta, sha) -> bool:
    allc = _read_json_file(consent_file(), {})
    allc[meta.id] = {"sha": sha, "perms": sorted(meta.permissions or []), "t": int(time.time())}
    return _write_json_file(consent_file(), allc)


_PERM_TEXT = {
    "gps": "read your GPS position",
    "loot_read": "read the list of scanned networks",
    "loot_write": "save files into loot/extras/%s/",
}


class ConsentScreen:
    """Shown instead of an extra the user has not approved yet (or whose code or
    permissions changed since). Drawn through the bridge like an extra; on [Y] it
    records the grant (id + content hash) and hands over `next_host`."""
    builtin = True
    sandboxed = True

    def __init__(self, meta, bridge, sha, source, make_next):
        self.meta = meta
        self._b = bridge
        self._sha = sha
        self._source = source
        self._make_next = make_next
        self.state = "running"
        self.error = None
        self.faults = 0
        self.next_host = None
        self._t0 = time.monotonic()

    def open(self):
        pass

    def event(self, key):
        pass

    def close(self):
        self.state = "closed"

    def frame(self, dt):
        b, m = self._b, self.meta
        y = 6
        b.draw_text("ALLOW THIS EXTRA TO RUN?", 0, y, 24, "hi", True); y += 40
        b.draw_text("%s  v%s  by %s" % (m.name, m.version, m.author or "?"), 0, y, 18, "txt", True); y += 30
        if self._source == "hub":
            b.draw_text("SOURCE: WDGo extras hub - reviewed, signature + checksum verified", 0, y, 15, "success", False)
        else:
            b.draw_text("SOURCE: UNVERIFIED LOCAL CODE - nobody reviewed this copy", 0, y, 15, "warn", True)
        y += 26
        b.draw_text("code fingerprint  %s" % self._sha[:32], 0, y, 13, "dim", False); y += 30
        # who made it / who answers for it - said plainly, every time
        if self._source == "hub" and is_official(m, self._sha):
            b.draw_text("Official extra by LOCOSP - marked official in the signed hub catalogue.", 0, y, 15, "success", True); y += 22
            b.draw_text("It still runs sandboxed and gets only what it asks for below.", 0, y, 15, "success", False); y += 32
        elif self._source == "hub":
            b.draw_text("Third-party extra: written by its author, NOT by LOCOSP. A hub review", 0, y, 15, "warn", True); y += 22
            b.draw_text("is not a guarantee. You install and use it at your own risk.", 0, y, 15, "warn", True); y += 32
        else:
            b.draw_text("Third-party extra: NOT made, reviewed or supported by LOCOSP.", 0, y, 15, "warn", True); y += 22
            b.draw_text("You install and use it entirely at your own risk.", 0, y, 15, "warn", True); y += 32
        b.draw_text("It runs SANDBOXED: no files, no network, no other programs.", 0, y, 15, "txt", False); y += 24
        b.draw_text("It can draw on this screen, read the keyboard, keep its own small data,", 0, y, 15, "txt", False); y += 22
        perms = [p for p in (m.permissions or []) if p in _PERM_TEXT]
        if perms:
            b.draw_text("and it asks for:", 0, y, 15, "txt", False); y += 26
            for p in perms:
                txt = _PERM_TEXT[p] % m.id if "%s" in _PERM_TEXT[p] else _PERM_TEXT[p]
                b.draw_text("  + %s   (%s)" % (txt, p), 0, y, 16, "ice", True); y += 24
        else:
            b.draw_text("and it asks for nothing else.", 0, y, 15, "txt", False); y += 24
        y += 16
        b.draw_text("[Y] allow and run        [ESC] cancel", 0, y, 18, "teal", True); y += 30
        b.draw_text("Asked once per version: any change to its code or permissions asks again.", 0, y, 13, "dim", False)
        if time.monotonic() - self._t0 > 0.4 and self.next_host is None:
            try:
                if b.key_pressed("y"):
                    consent_grant(m, self._sha)
                    self.next_host = self._make_next()
            except Exception:
                pass


def launch(meta, bridge, data_dir, require_isolation=True):
    """What the game calls when the user picks an extra. Returns something with the
    host surface: the runtime itself, or a ConsentScreen in front of it when this
    exact code + permission set has not been approved yet. Bundled examples and
    hand-pinned trusted extras (trust_file) skip the question."""
    def _mk():
        return make_host(meta, bridge, data_dir, require_isolation=require_isolation)
    if is_trusted(meta):
        return _mk()
    try:
        sha = sources_hash(collect_sources(meta.dir))
    except Exception as exc:
        return _RefusedHost(meta, "cannot read extra: %s" % exc)
    if consent_ok(meta, sha):
        return _mk()
    return ConsentScreen(meta, bridge, sha, provenance(meta, sha), _mk)


class _RefusedHost:
    """Stand-in host for an extra we will not run. Same surface as ExtraHost."""
    def __init__(self, meta, reason):
        self.meta = meta
        self.state = "faulted"
        self.error = reason
        self.faults = 1
        self.sandboxed = False

    def open(self):
        pass

    def frame(self, dt):
        pass

    def event(self, key):
        pass

    def close(self):
        self.state = "closed"


def extras_tab_items(metas) -> list:
    """Build the menu item 5-tuples for the EXTRAS category. Caller wraps with
    (name, color, items). First row is a live Rescan."""
    items = [("r", "Rescan", "_extra_rescan", "_extra_rescan", None),
             ("h", "Hub", "_extra_hub", "_extra_hub", None)]
    pool = list("123456789abcdefghjklmnopqstuvwxyz")
    pi = 0
    for m in metas:
        hk = pool[pi] if pi < len(pool) else ""
        pi += 1
        label = m.name[:18] + ("" if not m.error else "  X")
        items.append((hk, label, "_extra:%s" % m.id, "_extra_%s" % m.id, None))
    return items


# ======================================================================
# ctx facade
# ======================================================================
class _Storage:
    """Per-extra persistent storage, confined to the extra's own data/ dir."""
    def __init__(self, d):
        self.path = d
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass

    def _p(self, name):
        return os.path.join(self.path, os.path.basename(str(name)))  # no traversal

    def read(self, name, default=""):
        try:
            with open(self._p(name), "r", encoding="utf-8") as f:
                return f.read()
        except Exception:
            return default

    def write(self, name, text):
        try:
            with open(self._p(name), "w", encoding="utf-8") as f:
                f.write(str(text))
            return True
        except Exception:
            return False

    def read_json(self, name, default=None):
        raw = self.read(name, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except Exception:
            return default

    def write_json(self, name, obj):
        try:
            return self.write(name, json.dumps(obj))
        except Exception:
            return False


class _GpsCap:
    def __init__(self, fn):
        self._fn = fn

    def fix(self):
        """-> dict(valid, lat, lon, sats, speed_kmh, heading) or None."""
        try:
            return self._fn()
        except Exception:
            return None


class _LootCap:
    def __init__(self, nets, counts):
        self._nets, self._counts = nets, counts

    def networks(self):
        try:
            return list(self._nets())
        except Exception:
            return []

    def counts(self):
        try:
            return dict(self._counts())
        except Exception:
            return {}


class ExtraContext:
    """The only object an extra talks to. Verbs only — never the raw game."""
    def __init__(self, bridge, meta, data_dir, storage=None, images=None):
        self._b = bridge
        self._meta = meta
        # {relpath: png bytes}; None = read them from the extra's folder on first use.
        # Decoded lazily, once, by the extra's own process (the sandbox for third parties).
        self._img_src = images
        self._img = {}
        self.api_version = EXTRAS_API
        # In the sandbox the child has no filesystem: storage is a broker proxy.
        self.storage = storage if storage is not None else _Storage(data_dir)
        perms = set(meta.permissions or [])
        self.gps = _GpsCap(bridge.gps) if "gps" in perms else None
        self.loot = (_LootCap(bridge.loot_networks, bridge.loot_counts)
                     if "loot_read" in perms else None)
        # loot_save(name, text) -> absolute path | None. Writes into the game's loot
        # tree under loot/extras/<id>/ ONLY (own namespace; never core loot files).
        self.loot_save = bridge.loot_save if "loot_write" in perms else None
        self.mesh = None     # Phase 2 (sandbox + arbiter lease) — not yet granted

    # ---- geometry (read-only) ----
    @property
    def w(self):
        return self._b.view()[0]

    @property
    def h(self):
        return self._b.view()[1]

    def require(self, cap):
        v = getattr(self, cap, None)
        if v is None:
            raise PermissionError("capability '%s' not granted (declare it in permissions)" % cap)
        return v

    # ---- drawing (local coords 0,0..w,h; clamped to the extra's viewport) ----
    def text(self, s, x, y, size=16, color="txt", bold=False):
        self._b.draw_text(s, x, y, size, color, bold)

    def rect(self, x, y, w, h, color="txt", fill=True, alpha=255):
        self._b.draw_rect(x, y, w, h, color, fill, alpha)

    def line(self, x1, y1, x2, y2, color="txt", thick=1):
        self._b.draw_line(x1, y1, x2, y2, color, thick)

    def circle(self, x, y, r, color="txt"):
        self._b.draw_circle(x, y, r, color)

    def measure(self, s, size=16):
        return self._b.measure(s, size)

    # ---- images (api 2): small PNGs shipped in the extra's own folder ----
    def _image(self, name):
        name = str(name)
        got = self._img.get(name)
        if got is None:
            if self._img_src is None:
                self._img_src = {r: b for r, b in collect_sources(self._meta.dir).items()
                                 if r.endswith(".png")}
            if name not in self._img_src:
                raise KeyError("no image '%s' in this extra" % name)
            import wdgo_extras_png
            got = self._img[name] = wdgo_extras_png.decode(self._img_src[name])
        return got

    def image_size(self, name):
        """(width, height) in pixels of one of the extra's own .png files."""
        w, h, _ = self._image(name)
        return (w, h)

    def image(self, name, x, y, scale=1, alpha=255):
        """Draw one of the extra's own .png files (max 512x512 px) at x, y.
        scale is a whole number 1..8 (pixels are kept crisp)."""
        w, h, rgba = self._image(name)
        self._b.draw_image(str(name), w, h, rgba, x, y,
                           max(1, min(8, int(scale))), max(0, min(255, int(alpha))))

    # ---- input (only while the extra is active; ESC is the host's) ----
    def key_down(self, name):
        return self._b.key_down(name)

    def key_pressed(self, name):
        return self._b.key_pressed(name)

    def chars(self):
        """Printable characters typed this frame, as a list of str (for text entry)."""
        try:
            return self._b.chars()
        except Exception:
            return []

    # ---- misc ----
    def log(self, msg, level="info"):
        self._b.log(msg, level)

    def now(self):
        return time.time()


# ======================================================================
# Host — loads + drives one extra with full error isolation
# ======================================================================
def _import_entry(meta):
    path = os.path.join(meta.dir, meta.entry_file)
    name = "wdgo_extra_%s" % meta.id
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    added = meta.dir not in sys.path
    if added:
        sys.path.insert(0, meta.dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        if added:
            try:
                sys.path.remove(meta.dir)
            except ValueError:
                pass
    return mod


class ExtraHost:
    """One running extra. Every hook call is wrapped: a fault disables the extra
    and surfaces the reason, but never propagates into the game loop."""
    def __init__(self, meta, bridge, data_dir):
        self.meta = meta
        self._bridge = bridge
        self.ctx = ExtraContext(bridge, meta, data_dir)
        self.state = "loading"       # loading | running | faulted | closed
        self.error = None
        self.instance = None
        self.faults = 0
        self.sandboxed = False       # in-process tier (trusted code only)

    def open(self):
        try:
            mod = _import_entry(self.meta)
            cls = getattr(mod, self.meta.entry_symbol, None)
            if cls is None:
                raise AttributeError("entry symbol '%s' not found" % self.meta.entry_symbol)
            self.instance = cls()
            self._call("on_load")
            self._call("on_enter")
            if self.state == "loading":
                self.state = "running"
        except Exception as exc:
            self._fault(exc)

    def frame(self, dt):
        if self.state == "running":
            self._call("on_frame", dt)

    def event(self, key):
        if self.state == "running":
            self._call("on_event", key)

    def close(self):
        try:
            if self.instance and self.state in ("running", "faulted"):
                fn = getattr(self.instance, "on_exit", None)
                if fn:
                    try:
                        fn(self.ctx)
                    except Exception:
                        pass
        finally:
            try:
                self._bridge.release_all()
            except Exception:
                pass
            self.state = "closed"

    def _call(self, hook, *a):
        fn = getattr(self.instance, hook, None)
        if fn is None:
            return
        try:
            fn(self.ctx, *a)
        except Exception as exc:
            self._fault(exc)

    def _fault(self, exc):
        self.faults += 1
        self.error = "%s: %s" % (type(exc).__name__, exc)
        try:
            tb = traceback.format_exc().strip().splitlines()
            self._bridge.log("faulted: %s" % (tb[-1] if tb else self.error), "error")
        except Exception:
            pass
        self.state = "faulted"
