"""EXTRAS Phase 3 — hub client (browse / verify / install reviewed extras).

The hub is a git repository:

    index.json        signed catalogue: {schema:1, extras:{<id>:{version, sha, ...}}}
    index.json.sig    OpenSSH signature of index.json (namespace "wdgo-extras-hub")
    extras/<id>/      the reviewed source of every extra

Trust chain, checked on THIS device before anything is installed:

  1. index.json must carry a valid signature from a key in `extras_hub_signers`
     (shipped inside the game). Verification is done by `ssh-keygen -Y verify` —
     audited OpenSSH crypto, no home-made primitives, no extra dependency.
  2. the extra's files in the checkout must hash (wdgo_extras.sources_hash) to the
     `sha` the signed index lists for it.
  3. what gets copied to ~/.wdgo/extras/<id>/ is exactly the bytes that were hashed.

So a tampered mirror, a hijacked transport or a modified checkout cannot get code
installed: whoever does not hold the signing key cannot produce an index that
verifies, and cannot change an extra without breaking its checksum.

Installed hub extras still run in the Phase-2 sandbox and still go through the
consent card. Being in the hub means "reviewed", not "trusted with root".

Pure stdlib; never imports raylib. The network step (`git`) runs as the login
user, not root (same pattern as the game's own OTA).
"""
import json
import os
import re
import shutil
import subprocess
import threading
import time
import types

import wdgo_extras as wx

NAMESPACE = "wdgo-extras-hub"
SIGNER_IDENTITY = "wdgo-extras-hub"
DEFAULT_HUB_URL = "https://github.com/LOCOSP/WDGoNG-HUB.git"
INDEX_MAX = 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[a-z0-9_]{3,32}$")


def browse_url(url, eid) -> str:
    """A browsable link to an extra's reviewed source INSIDE the hub repo — the
    code the PR added and that the game checksums. Derived from the hub git URL so
    it is always available and always matches what runs, independent of any author
    link. Handles github/gitlab-style https and git@host:owner/repo; '' if it can't."""
    u = str(url or "").strip()
    if u.startswith("git@") and ":" in u:
        host, _, path = u[4:].partition(":")
        u = "https://%s/%s" % (host, path)
    if u.endswith(".git"):
        u = u[:-4]
    if not u.startswith(("https://", "http://")) or not _ID_RE.match(str(eid)):
        return ""
    return "%s/tree/HEAD/extras/%s" % (u.rstrip("/"), eid)


def clean_repo_url(u) -> str:
    """A displayable https/http source-repo URL, or '' if it isn't plainly one.
    Shown as text only (the game has no browser), so the user reads it and opens
    it themselves — we just make sure it can't carry control characters or spaces."""
    u = str(u or "").strip()
    if not (u.startswith("https://") or u.startswith("http://")):
        return ""
    if len(u) > 200 or " " in u or any(ch < " " or ch == "\x7f" for ch in u):
        return ""
    return u


class HubError(Exception):
    pass


def signers_file() -> str:
    """allowed_signers file with the hub's public key(s); ships with the game."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras_hub_signers")


def config_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-hub.json")


def cache_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-hub-cache")


def hub_url() -> str:
    """Hub repository URL: ~/.wdgo/extras-hub.json {"url": ...} overrides the default."""
    u = wx._read_json_file(config_file(), {}).get("url")
    return str(u).strip() if u else DEFAULT_HUB_URL


def _login_user():
    """(name, uid, gid) of the human behind sudo, or None when not elevated."""
    try:
        if os.geteuid() == 0 and os.environ.get("SUDO_USER"):
            return (os.environ["SUDO_USER"], int(os.environ.get("SUDO_UID", "-1")),
                    int(os.environ.get("SUDO_GID", "-1")))
    except Exception:
        pass
    return None


def _git(*args):
    lu = _login_user()
    base = ["git", *args]
    return ["sudo", "-n", "-u", lu[0], "-H", *base] if lu else base


def _run(argv, timeout=90, **kw):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    return subprocess.run(argv, capture_output=True, timeout=timeout, env=env, **kw)


def sync(url=None) -> str:
    """Clone or refresh the hub checkout. Returns the cache path. Network + git run
    as the login user; nothing fetched here is trusted until verify_index()."""
    url = url or hub_url()
    if not url:
        raise HubError("hub not configured")
    if url.startswith("-"):
        raise HubError("bad hub url")
    cache = cache_dir()
    lu = _login_user()
    os.makedirs(os.path.dirname(cache), exist_ok=True)
    if os.path.isdir(os.path.join(cache, ".git")):
        r = _run(_git("-C", cache, "remote", "get-url", "origin"), timeout=10)
        if r.returncode != 0 or r.stdout.decode("utf-8", "replace").strip() != url:
            shutil.rmtree(cache, ignore_errors=True)
    if not os.path.isdir(os.path.join(cache, ".git")):
        shutil.rmtree(cache, ignore_errors=True)
        os.makedirs(cache, exist_ok=True)
        if lu and lu[1] >= 0:
            os.chown(cache, lu[1], lu[2])
        r = _run(_git("clone", "--quiet", "--depth", "1", "--", url, cache))
        if r.returncode != 0:
            raise HubError("hub download failed: %s"
                           % r.stderr.decode("utf-8", "replace").strip()[-160:])
    else:
        r = _run(_git("-C", cache, "fetch", "--quiet", "--depth", "1", "origin"))
        if r.returncode != 0:
            raise HubError("hub refresh failed: %s"
                           % r.stderr.decode("utf-8", "replace").strip()[-160:])
        r = _run(_git("-C", cache, "reset", "--quiet", "--hard", "FETCH_HEAD"), timeout=30)
        if r.returncode != 0:
            raise HubError("hub refresh failed (reset)")
    return cache


def _read_regular(path, limit) -> bytes:
    import stat
    try:
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise HubError("%s is not a small regular file" % os.path.basename(path))
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as f:
            return f.read(limit + 1)
    except OSError:
        raise HubError("hub is missing %s - refusing" % os.path.basename(path))


def verify_index(cache=None, signers=None) -> dict:
    """Check index.json's signature and return the sanitised catalogue
    {id: entry}. Raises HubError when the signature or the content is not valid."""
    cache = cache or cache_dir()
    signers = signers or signers_file()
    if not os.path.isfile(signers):
        raise HubError("no hub signing key shipped with this game build")
    data = _read_regular(os.path.join(cache, "index.json"), INDEX_MAX)
    sig = _read_regular(os.path.join(cache, "index.json.sig"), 16 * 1024)
    # verify from a private copy of the signature so the checked bytes cannot change
    import tempfile
    with tempfile.NamedTemporaryFile(prefix="wdgo-hub-", suffix=".sig") as tf:
        tf.write(sig)
        tf.flush()
        try:
            r = subprocess.run(
                ["ssh-keygen", "-Y", "verify", "-f", signers, "-I", SIGNER_IDENTITY,
                 "-n", NAMESPACE, "-s", tf.name],
                input=data, capture_output=True, timeout=20)
        except FileNotFoundError:
            raise HubError("ssh-keygen not available - cannot verify the hub")
    if r.returncode != 0:
        raise HubError("hub index signature is NOT valid - refusing")
    try:
        idx = json.loads(data.decode("utf-8"))
    except Exception:
        raise HubError("hub index is not valid JSON")
    if not isinstance(idx, dict) or idx.get("schema") != 1 or not isinstance(idx.get("extras"), dict):
        raise HubError("unsupported hub index format")
    out = {}
    for eid, e in idx["extras"].items():
        if not (isinstance(eid, str) and _ID_RE.match(eid) and isinstance(e, dict)):
            continue
        sha = str(e.get("sha", ""))
        if not _SHA_RE.match(sha):
            continue
        try:
            api = int(e.get("api", 0))
        except (TypeError, ValueError):
            continue
        perms = [str(p) for p in (e.get("permissions") or []) if str(p) in wx._KNOWN_PERMS]
        out[eid] = {
            "id": eid, "sha": sha, "api": api, "permissions": perms,
            "name": str(e.get("name", eid))[:32], "version": str(e.get("version", "0.0.0"))[:16],
            "author": str(e.get("author", ""))[:40], "description": str(e.get("description", ""))[:120],
            "category": str(e.get("category", "misc"))[:24],
            "tags": [str(t)[:20] for t in (e.get("tags") or []) if isinstance(t, str)][:8],
            "icon": e.get("icon") is True,
            # set by the hub's index builder from its maintainer-only list, and covered
            # by the signature: this, not the author string, says "from the maintainer"
            "official": e.get("official") is True,
            "repo": clean_repo_url(e.get("homepage", "")),
        }
    return out


def load_ratings(cache=None) -> dict:
    """Banana (yes) / peel (no) counts from the hub's ratings.json:
    {id: {banana, peel, url}}. DISPLAY data only - not signed and not part of the
    install trust chain - so every value is re-validated and anything odd dropped.
    Source-agnostic on purpose: today GitHub reactions produce the file, later the
    WDGWars portal can; `url` is wherever that vote happens (any https link)."""
    try:
        raw = json.loads(_read_regular(os.path.join(cache or cache_dir(), "ratings.json"),
                                       256 * 1024).decode("utf-8"))
    except Exception:
        return {}
    ex = raw.get("extras") if isinstance(raw, dict) else None
    if not isinstance(ex, dict):
        return {}

    def _n(v):
        return v if isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 10 ** 7 else 0

    out = {}
    for eid, r in list(ex.items())[:2000]:
        if not (isinstance(eid, str) and _ID_RE.match(eid) and isinstance(r, dict)):
            continue
        url = clean_repo_url(r.get("url", ""))
        out[eid] = {"banana": _n(r.get("banana")), "peel": _n(r.get("peel")),
                    "url": url if url.startswith("https://") else ""}
    return out


def installed() -> dict:
    return wx._read_json_file(wx.installed_file(), {})


def status(entry) -> str:
    """'' (not installed) | 'installed' | 'update' | 'modified' | 'needs-game-update'."""
    if entry["api"] > wx.EXTRAS_API:
        return "needs-game-update"
    d = os.path.join(wx.extras_root(), entry["id"])
    if not os.path.isdir(d):
        return ""
    try:
        cur = wx.sources_hash(wx.collect_sources(d))
    except Exception:
        return "modified"
    if cur == entry["sha"]:
        return "installed"
    reg = installed().get(entry["id"])
    if isinstance(reg, dict) and reg.get("sha") == cur:
        return "update"
    return "modified"


def install(eid, catalogue, cache=None) -> str:
    """Install (or update) one extra from the verified catalogue. Returns its dir."""
    cache = cache or cache_dir()
    entry = catalogue.get(eid)
    if entry is None:
        raise HubError("'%s' is not in the hub" % eid)
    if entry["api"] > wx.EXTRAS_API:
        raise HubError("needs a newer game (api %d) - update the game first" % entry["api"])
    try:
        src = wx.collect_sources(os.path.join(cache, "extras", eid))
    except Exception as exc:
        raise HubError("cannot read '%s' from the hub: %s" % (eid, exc))
    if "extra.json" not in src or wx.sources_hash(src) != entry["sha"]:
        raise HubError("checksum mismatch for '%s' - refusing to install" % eid)
    root = wx.extras_root()
    os.makedirs(root, exist_ok=True)
    dst, tmp = os.path.join(root, eid), os.path.join(root, ".%s.installing" % eid)
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, mode=0o755)
    try:
        for rel, data in src.items():
            p = os.path.join(tmp, *rel.split("/"))
            os.makedirs(os.path.dirname(p), mode=0o755, exist_ok=True)
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
        if wx.sources_hash(wx.collect_sources(tmp)) != entry["sha"]:
            raise HubError("installed copy does not match the verified checksum")
        if os.path.lexists(dst):
            if os.path.islink(dst):
                os.unlink(dst)
            else:
                shutil.rmtree(dst)
        os.rename(tmp, dst)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    reg = installed()
    reg[eid] = {"version": entry["version"], "sha": entry["sha"], "source": "hub",
                "official": bool(entry.get("official")), "t": int(time.time())}
    wx._write_json_file(wx.installed_file(), reg)
    return dst


def uninstall(eid) -> bool:
    """Remove a hub-installed extra (its saved data in extras-data/ is kept)."""
    reg = installed()
    if eid not in reg or not _ID_RE.match(eid):
        return False
    d = os.path.join(wx.extras_root(), eid)
    if os.path.islink(d):
        os.unlink(d)
    else:
        shutil.rmtree(d, ignore_errors=True)
    reg.pop(eid, None)
    wx._write_json_file(wx.installed_file(), reg)
    return True


# ======================================================================
# In-game hub browser — a built-in screen drawn through the extras bridge
# ======================================================================
class HubScreen:
    """Browse / install / update / remove hub extras. Same surface as an extra
    host so the game drives it through the existing EXTRAS screen."""
    builtin = True
    sandboxed = True

    def __init__(self, bridge):
        self._b = bridge
        self.meta = types.SimpleNamespace(id="hub", name="Extras Hub", author="WDGo",
                                          version="", permissions=[])
        self.state = "running"
        self.error = None
        self.faults = 0
        self.next_host = None
        self.changed = False            # something was installed/removed -> game rescans
        self._cat = {}
        self._ratings = {}
        self._rows = []
        self._sel = 0
        self._msg = ""
        self._msg_col = "dim"
        self._busy = False
        self._t0 = time.monotonic()
        self._detail = None             # the entry whose details card is open
        self._scroll = 0                # first visible line of the long text in the card
        self._query = ""                # search box text
        self._searching = False         # the search box has the keyboard
        self._cat_filter = None         # show one category only
        self._about = {}                # sha -> long description, from the verified copy
        self._vdir = {}                 # sha -> hub checkout dir if it hashes to sha, else None
        self._icons = {}                # sha -> (w, h, rgba) | None

    def open(self):
        self._refresh()

    def event(self, key):
        pass

    def close(self):
        self.state = "closed"

    def _say(self, text, col="dim"):
        self._msg, self._msg_col = text, col

    def _bg(self, fn):
        if self._busy:
            return
        self._busy = True

        def run():
            try:
                fn()
            except HubError as exc:
                self._say(str(exc), "danger")
            except Exception as exc:
                self._say("%s: %s" % (type(exc).__name__, exc), "danger")
            finally:
                self._busy = False
        threading.Thread(target=run, daemon=True).start()

    def _refresh(self):
        def work():
            self._say("contacting the hub...", "dim")
            cache = sync()
            self._cat = verify_index(cache)
            # keep the "official" mark of installed extras in step with the signed catalogue
            reg, dirty = installed(), False
            for eid, ent in reg.items():
                e = self._cat.get(eid)
                if isinstance(ent, dict) and e and ent.get("sha") == e["sha"] \
                        and ent.get("official") is not bool(e.get("official")):
                    ent["official"], dirty = bool(e.get("official")), True
            if dirty:
                wx._write_json_file(wx.installed_file(), reg)
            self._ratings = load_ratings(cache)
            self._rows = sorted(self._cat.values(), key=lambda e: e["name"].lower())
            self._sel = min(self._sel, max(0, len(self._rows) - 1))
            self._say("signature OK - %d reviewed extra(s)" % len(self._rows), "success")
        if not hub_url():
            self._say("The hub is not configured yet in this build.", "warn")
            return
        self._bg(work)

    def _install(self, entry):
        def work():
            self._say("installing %s..." % entry["name"], "dim")
            install(entry["id"], self._cat)
            self.changed = True
            self._say("%s v%s installed - find it in EXTRAS (it will ask before the first run)"
                      % (entry["name"], entry["version"]), "success")
        self._bg(work)

    def _remove(self, entry):
        def work():
            if uninstall(entry["id"]):
                self.changed = True
                self._say("%s removed (its saved data was kept)" % entry["name"], "success")
            else:
                self._say("%s was not installed from the hub" % entry["name"], "warn")
        self._bg(work)

    _PERM_TEXT = {"gps": "reads your GPS position",
                  "loot_read": "reads the list of networks you have scanned",
                  "loot_write": "saves its own files into the session's loot folder"}

    def _about_text(self, entry) -> str:
        """The extra's long description, read from the hub checkout ONLY when those
        files hash to the signed checksum - text nobody reviewed is never shown."""
        sha = entry["sha"]
        if sha not in self._about:
            text = ""
            try:
                d = self._verified_dir(entry)
                if d:
                    text = str(getattr(wx._load_meta(d), "about", "") or "")
            except Exception:
                text = ""
            self._about[sha] = " ".join(text.split())[:2000]
        return self._about[sha]

    def _verified_dir(self, entry):
        """The extra's folder in the hub checkout, but only if its content hashes to the
        signed checksum - i.e. it is byte-for-byte what was reviewed. Else None."""
        sha = entry["sha"]
        if sha not in self._vdir:
            d = os.path.join(cache_dir(), "extras", entry["id"])
            try:
                ok = wx.sources_hash(wx.collect_sources(d)) == sha
            except Exception:
                ok = False
            self._vdir[sha] = d if ok else None
        return self._vdir[sha]

    def _icon(self, entry):
        """(w, h, rgba) of the extra's icon, or None. This is the one place the game
        itself decodes an image that came from outside, so it is fenced in: only from
        a folder that matches the signed checksum (reviewed content), only icon.png,
        only within the icon limits (64x64, 16 KB), and only with the pure-Python
        decoder - never a native image library."""
        sha = entry["sha"]
        if sha not in self._icons:
            img = None
            try:
                d = self._verified_dir(entry) if entry.get("icon") else None
                if d:
                    import wdgo_extras_png as png
                    p = os.path.join(d, png.ICON_NAME)
                    fd = os.open(p, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    with os.fdopen(fd, "rb") as f:
                        data = f.read(png.ICON_MAX_BYTES + 1)
                    png.check_icon(data)
                    img = png.decode(data)
            except Exception:
                img = None
            self._icons[sha] = img
        return self._icons[sha]

    @staticmethod
    def _wrap(text, cols):
        out, line = [], ""
        for word in text.split():
            while len(word) > cols:
                if line:
                    out.append(line)
                    line = ""
                out.append(word[:cols])
                word = word[cols:]
            if line and len(line) + 1 + len(word) > cols:
                out.append(line)
                line = word
            else:
                line = (line + " " + word) if line else word
        if line:
            out.append(line)
        return out

    def _draw_detail(self, b, w, h, e):
        """A card over the list: everything worth knowing before installing."""
        mw, mh = min(w - 40, 980), min(h - 24, 560)
        x0, y0 = (w - mw) // 2, (h - mh) // 2
        b.draw_rect(-8, -8, w + 16, h + 16, "void", True, 215)
        b.draw_rect(x0, y0, mw, mh, "void", True, 255)
        b.draw_rect(x0, y0, mw, mh, "ice", False, 255)
        x, y, right = x0 + 22, y0 + 16, x0 + mw - 22
        cols = max(20, int((mw - 44) / max(1.0, b.measure("M" * 20, 15) / 20.0)))
        st = status(e)
        tag, col = {"installed": ("INSTALLED", "success"), "update": ("UPDATE AVAILABLE", "warn"),
                    "modified": ("LOCAL COPY DIFFERS", "warn"),
                    "needs-game-update": ("NEEDS GAME UPDATE", "danger")}.get(st, ("NOT INSTALLED", "dim"))
        tx, icon = x, self._icon(e)
        if icon and hasattr(b, "draw_image"):
            b.draw_image("hub-icon-" + e["sha"][:16] + ".png", icon[0], icon[1], icon[2], x, y, 1, 255)
            tx = x + icon[0] + 16
        b.draw_text(e["name"][:40], tx, y, 24, "hi", True)
        b.draw_text(tag, right - b.measure(tag, 15), y + 6, 15, col, True)
        pre = "v%s   by " % e["version"]
        b.draw_text(pre, tx, y + 34, 15, "dim", False)
        ax = tx + b.measure(pre, 15)
        who = e["author"][:28] + ("  (official)" if e["official"] else "")
        b.draw_text(who, ax, y + 34, 15, "success" if e["official"] else "dim", e["official"])
        b.draw_text("   " + e["category"], ax + b.measure(who, 15), y + 34, 15, "dim", False)
        if e.get("tags"):
            b.draw_text("  ".join("#" + t for t in e["tags"][:8])[:cols], tx, y + 56, 14, "dim", False)
        y += max(78, (icon[1] + 10) if icon else 0)
        b.draw_line(x, y + 4, right, y + 4, "edge", 1)
        y += 16
        # the description: larger type, and scrollable when it does not fit
        big = max(20, int((mw - 44 - 18) / max(1.0, b.measure("M" * 20, 18) / 20.0)))
        text = [(ln, True) for ln in self._wrap(e["description"] or "(no description)", big)]
        about = self._wrap(self._about_text(e), big)
        if about:
            text += [("", False)] + [(ln, False) for ln in about]
        foot = y0 + mh - 180                              # the fixed block below starts here
        step = 24
        fit = max(1, (foot - 6 - y) // step)
        self._scroll_max = max(0, len(text) - fit)
        self._scroll = max(0, min(self._scroll, self._scroll_max))
        for ln, bold in text[self._scroll:self._scroll + fit]:
            if ln:
                b.draw_text(ln, x, y, 18, "hi" if bold else "txt", bold)
            y += step
        if self._scroll_max:                              # scrollbar on the right edge
            track_y, track_h = foot - 6 - fit * step, fit * step - 4
            b.draw_rect(right - 4, track_y, 4, track_h, "edge", True, 255)
            th = max(16, track_h * fit // len(text))
            b.draw_rect(right - 4, track_y + (track_h - th) * self._scroll // self._scroll_max, 4, th,
                        "ice", True, 255)
        y = foot
        b.draw_line(x, y, right, y, "edge", 1)
        y += 10
        if e["permissions"]:
            b.draw_text("ASKS FOR", x, y, 14, "warn", True)
            for p in e["permissions"]:
                b.draw_text("%s - %s" % (p, self._PERM_TEXT.get(p, "")), x + 110, y, 14, "warn", False)
                y += 19
        else:
            b.draw_text("ASKS FOR   nothing - no permissions", x, y, 14, "success", True)
            y += 19
        y = max(y + 4, foot + 54)
        rt = self._ratings.get(e["id"])
        if rt:
            b.draw_text("%d bananas / %d peels" % (rt["banana"], rt["peel"]), x, y, 14, "warn", True)
            if rt["url"]:
                b.draw_text(("vote: " + rt["url"])[:cols - 26], x + 230, y, 13, "ice", False)
        y += 21
        src = browse_url(hub_url(), e["id"]) or "the hub repo, folder extras/%s/" % e["id"]
        b.draw_text(("source: " + src)[:cols], x, y, 13, "teal", False)
        y += 19
        b.draw_text("checksum %s   %s" % (e["sha"][:16], "official extra from the hub's maintainer" if e["official"]
                                         else "made by its author, not by LOCOSP - at your own risk"),
                    x, y, 13, "dim", False)
        keys = "[I] install / update   [X] remove   [ENTER] close"
        if self._scroll_max:
            keys = "[UP/DOWN] scroll   " + keys
        b.draw_text(self._msg[:cols], x, y0 + mh - 46, 14, self._msg_col, True)
        b.draw_text(keys, x, y0 + mh - 24, 14, "teal", False)

    def _visible(self):
        """Rows after the category filter and the search box (all words must match)."""
        rows = self._rows
        if self._cat_filter:
            rows = [e for e in rows if e["category"] == self._cat_filter]
        words = self._query.lower().split()
        if words:
            def hay(e):
                return " ".join([e["id"], e["name"], e["author"], e["category"], e["description"]]
                                + e.get("tags", [])).lower()
            rows = [e for e in rows if all(wd in hay(e) for wd in words)]
        return rows

    def frame(self, dt):
        b = self._b
        try:
            w, h = b.view()
        except Exception:
            w, h = 800, 480
        ready = time.monotonic() - self._t0 > 0.3 and not self._busy
        try:
            typed = list(b.chars()) if hasattr(b, "chars") else []
        except Exception:
            typed = []
        per_page = max(3, (h - 52 - 196) // 26)

        if ready and self._searching and self._detail is None:
            # the search box owns the keyboard: letters are text, not commands
            for ch in typed:
                if len(self._query) < 32 and (ch.isalnum() or ch in " _-."):
                    self._query += ch
                    self._sel = 0
            if b.key_pressed("backspace"):
                if self._query:
                    self._query = self._query[:-1]
                    self._sel = 0
                else:
                    self._searching = False
            if b.key_pressed("enter"):
                self._searching = False
        elif ready:
            rows = self._visible()
            if self._detail is None:
                if b.key_pressed("slash"):
                    self._searching = True
                elif b.key_pressed("tab"):
                    cats = [None] + sorted({e["category"] for e in self._rows})
                    k = cats.index(self._cat_filter) if self._cat_filter in cats else 0
                    self._cat_filter = cats[(k + 1) % len(cats)]
                    self._sel = 0
                    rows = self._visible()
                elif b.key_pressed("backspace") and (self._query or self._cat_filter):
                    self._query, self._cat_filter, self._sel = "", None, 0
                    rows = self._visible()
                if rows:
                    if b.key_pressed("down"):
                        self._sel = (self._sel + 1) % len(rows)
                    if b.key_pressed("up"):
                        self._sel = (self._sel - 1) % len(rows)
                    if b.key_pressed("right"):
                        self._sel = min(len(rows) - 1, self._sel + per_page)
                    if b.key_pressed("left"):
                        self._sel = max(0, self._sel - per_page)
                if b.key_pressed("r"):
                    self._refresh()
            self._sel = max(0, min(self._sel, len(rows) - 1))
            cur = self._detail if self._detail is not None else (rows[self._sel] if rows else None)
            if cur is not None:
                if b.key_pressed("enter") or b.key_pressed("space") or (
                        self._detail is not None and b.key_pressed("backspace")):
                    self._detail = None if self._detail is not None else cur
                    self._scroll = 0
                if self._detail is not None:              # the card is open: arrows scroll its text
                    if b.key_pressed("down") or b.key_pressed("right"):
                        self._scroll = min(getattr(self, "_scroll_max", 0), self._scroll + 3)
                    if b.key_pressed("up") or b.key_pressed("left"):
                        self._scroll = max(0, self._scroll - 3)
                if b.key_pressed("i") or b.key_pressed("u"):
                    st = status(cur)
                    if st == "needs-game-update":
                        self._say("needs a newer game - update the game first", "warn")
                    elif st == "installed":
                        self._say("already installed and up to date", "dim")
                    else:
                        self._install(cur)
                if b.key_pressed("x"):
                    self._remove(cur)

        rows = self._visible()
        self._sel = max(0, min(self._sel, len(rows) - 1))
        b.draw_text("Reviewed extras - signed index, checksum verified, always sandboxed", 0, 0, 15, "dim", False)
        b.draw_text("Green author = official extra by LOCOSP. All others are by their own authors -",
                    0, 22, 14, "warn", False)
        b.draw_text("install and run them at your own risk.", b.measure(
            "Green author = official extra by LOCOSP. All others are by their own authors - ", 14), 22, 14, "warn", False)
        # search / filter line
        y = 46
        cursor = "_" if self._searching and int(time.monotonic() * 2) % 2 == 0 else " "
        if self._searching or self._query:
            b.draw_text("search: %s%s" % (self._query, cursor), 0, y, 15, "hi" if self._searching else "ice", True)
        else:
            b.draw_text("[/] search", 0, y, 14, "dim", False)
        info = "%d of %d" % (len(rows), len(self._rows))
        if self._cat_filter:
            info = "category: %s    %s" % (self._cat_filter, info)
        b.draw_text(info, w - b.measure(info, 14) - 8, y, 14, "ice" if self._cat_filter else "dim", False)
        y = 74
        if not rows:
            b.draw_text("(nothing listed)" if not self._rows else "(no extra matches - [BACKSPACE] clears the filter)",
                        0, y, 16, "dim", False)
        top = max(0, min(self._sel - per_page // 2, max(0, len(rows) - per_page)))
        for i, e in enumerate(rows[top:top + per_page]):
            idx = top + i
            st = status(e)
            tag, col = {"installed": ("INSTALLED", "success"), "update": ("UPDATE", "warn"),
                        "modified": ("LOCAL COPY DIFFERS", "warn"),
                        "needs-game-update": ("NEEDS GAME UPDATE", "danger")}.get(st, ("", "dim"))
            if idx == self._sel:
                b.draw_rect(-6, y - 3, w + 6, 24, "edge", True, 150)
            rowc, bold = ("hi" if idx == self._sel else "txt"), idx == self._sel
            cw = b.measure("M" * 20, 16) / 20.0               # monospace: columns by character
            b.draw_text("%-20s v%-8s" % (e["name"][:20], e["version"]), 0, y, 16, rowc, bold)
            # the maintainer's own extras (per the SIGNED catalogue) are the only green ones
            b.draw_text(e["author"][:14], 31 * cw, y, 16, "success" if e["official"] else rowc,
                        bold or e["official"])
            b.draw_text(e["category"][:10], 46 * cw, y, 16, rowc, bold)
            rt = self._ratings.get(e["id"])
            if rt:
                b.draw_text("%d bananas" % rt["banana"], 620, y + 1, 14, "warn", True)
                b.draw_text("%d peels" % rt["peel"], 740, y + 1, 14, "dim", False)
            if tag:
                b.draw_text(tag, w - b.measure(tag, 14) - 8, y + 1, 14, col, True)
            y += 26
        if len(rows) > per_page:
            b.draw_text("%d-%d  [LEFT/RIGHT] page" % (top + 1, min(len(rows), top + per_page)),
                        0, y + 2, 13, "dim", False)
        if rows:
            e = rows[self._sel]
            y = h - 132
            b.draw_line(0, y - 8, w, y - 8, "edge", 1)
            b.draw_text(e["description"] or "(no description)", 0, y, 15, "txt", False)
            perms = ", ".join(e["permissions"]) or "none"
            b.draw_text("asks for: %s" % perms, 0, y + 24, 14, "warn" if e["permissions"] else "success", False)
            b.draw_text("[ENTER] full details, source and checksum", 0, y + 46, 14, "dim", False)
        b.draw_text(self._msg, 0, h - 52, 15, self._msg_col, True)
        if self._searching:
            keys = "type to search   [ENTER] done   [BACKSPACE] delete / leave search"
        else:
            keys = "[UP/DOWN] select  [ENTER] details  [I] install  [X] remove  [/] search  [TAB] category  [R] refresh"
        b.draw_text(keys, 0, h - 26, 14, "teal", False)
        if self._detail is not None:
            if self._detail["id"] not in self._cat:
                self._detail = None                       # gone after a refresh
            else:
                self._detail = self._cat[self._detail["id"]]
                self._draw_detail(b, w, h, self._detail)
