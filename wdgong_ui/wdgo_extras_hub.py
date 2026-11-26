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

Hardening (the game runs as root):

  * git runs as the dedicated unprivileged account HUB_USER (same uid however the
    game was started) with a scrubbed environment (no user/system git config, no
    hooks, no credential helpers, https only), into a fresh staging directory of
    a private cache, bounded in bytes, inodes and time; the staging copy replaces
    the last good checkout only after its signed index verified.
  * ssh-keygen -Y verify runs as a second unprivileged account on private,
    root-owned copies of the signature and of the shipped allowed-signers list.
  * the signed index may carry `catalog_seq`; a catalogue older than (or
    different from, at the same number) the newest one this device accepted is
    refused, and once a numbered catalogue was seen unnumbered ones are refused.
  * installs are atomic and keep the previous folder (wdgo_extras.activate_package);
    a hand-installed extra with the same id is only replaced after confirmation.
  * one job queue for the whole game; statuses, descriptions and icons are
    computed in the background and shown from memory.

Pure stdlib; never imports raylib.
"""
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
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


class HubCollision(HubError):
    """Installing would replace a hand-installed or locally edited extra."""


# ---- accounts, locations, limits ---------------------------------------------
HUB_USER = "wdgo-hub"            # runs git (network + checkout), never root
VERIFY_USER = "wdgo-verify"      # runs ssh-keygen -Y verify, never root, != HUB_USER
VERIFY_FALLBACK = "nobody"
ROOT_CACHE_BASE = "/var/cache/wdgo-hub"   # private (0700, owned by HUB_USER)
SIG_MAX = 16 * 1024
SIGNERS_MAX = 64 * 1024
SYNC_TIMEOUT = 90                # whole clone, then the process group is killed
SYNC_MAX_BYTES = 64 * 1024 * 1024
SYNC_MAX_INODES = 4096
SYNC_POLL = 0.25
MIN_CATALOG_SEQ = 0              # a game release may raise this floor
SEQ_MAX = 2 ** 64 - 1
last_catalog = {"seq": None, "sha256": None}


def _is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def signers_file() -> str:
    """allowed_signers file with the hub's public key(s); ships with the game."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "extras_hub_signers")


def config_file() -> str:
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-hub.json")


def cache_dir() -> str:
    """The verified hub checkout. As root it lives in a private cache owned by the
    unprivileged hub account (reachable without crossing root's home)."""
    env = os.environ.get("WDGO_HUB_CACHE")
    if env:
        return os.path.join(os.path.abspath(env), "checkout")
    if _is_root():
        return os.path.join(ROOT_CACHE_BASE, "checkout")
    return os.path.join(os.path.expanduser("~"), ".wdgo", "extras-hub-cache")


def hub_state_file() -> str:
    """Highest catalogue release number this device accepted (protected state)."""
    return os.path.join(wx.state_dir(), "extras-hub-state.json")


def hub_url() -> str:
    """Hub repository URL: ~/.wdgo/extras-hub.json {"url": ...} overrides the default."""
    u = wx._read_json_file(config_file(), {}).get("url")
    return str(u).strip() if u else DEFAULT_HUB_URL


def _check_url(url):
    """https only for a root game; a local path / file:// is accepted for the
    non-root desktop simulator and tests."""
    if (not isinstance(url, str) or not url or len(url) > 300 or url.startswith("-")
            or any(ch <= " " or ch == "\x7f" for ch in url)):
        raise HubError("bad hub url")
    if url.startswith("https://"):
        return
    if not _is_root() and (url.startswith("file://") or url.startswith("/")):
        return
    raise HubError("bad hub url (only https:// is allowed)")


def _account(name):
    if not name:
        return None
    try:
        import pwd
        pw = pwd.getpwnam(name)
    except (KeyError, ImportError):
        return None
    if pw.pw_uid <= 0 or pw.pw_gid <= 0:
        return None
    return (pw.pw_name, pw.pw_uid, pw.pw_gid)


def _forbidden_uids():
    try:
        import wdgo_extras_sandbox as _sb
        return {0, _sb.SANDBOX_UID}
    except Exception:
        return {0, 61184}


def _fetch_account():
    """None when the game is not root (git runs as us), else (name, uid, gid) of the
    dedicated unprivileged hub account - the same one however the game was started."""
    if not _is_root():
        return None
    name = os.environ.get("WDGO_HUB_USER") or HUB_USER
    a = _account(name)
    if a is None or a[1] in _forbidden_uids():
        raise HubError("the hub needs the system account '%s' - create it once with: "
                       "useradd --system --no-create-home --shell /usr/sbin/nologin %s"
                       % (name, name))
    return a


def _verify_account():
    """None when not root, else an unprivileged account other than the hub's."""
    if not _is_root():
        return None
    try:
        fetch = _fetch_account()
    except HubError:
        fetch = None
    bad = _forbidden_uids() | ({fetch[1]} if fetch else set())
    for name in (os.environ.get("WDGO_HUB_VERIFY_USER"), VERIFY_USER, VERIFY_FALLBACK):
        a = _account(name)
        if a is not None and a[1] not in bad:
            return a
    raise HubError("no unprivileged account to verify the hub signature (create '%s')"
                   % VERIFY_USER)


def _tool(name) -> str:
    """Absolute path of a trusted system binary (as root: /usr/bin or /bin only)."""
    p = shutil.which(name, path="/usr/bin:/bin")
    if p is None and not _is_root():
        p = shutil.which(name)
    if p is None:
        raise HubError("%s is not installed" % name)
    return p


def _drop_kw(acct) -> dict:
    if acct is None:
        return {}
    return {"user": acct[1], "group": acct[2], "extra_groups": [], "umask": 0o077}


def _tree_usage(path, max_inodes):
    """(bytes on disk, inodes) under path, never following symlinks; stops counting
    once max_inodes is exceeded."""
    used, n, todo = 0, 0, [path]
    while todo and n <= max_inodes:
        d = todo.pop()
        try:
            with os.scandir(d) as it:
                for ent in it:
                    n += 1
                    if n > max_inodes:
                        break
                    try:
                        st = ent.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    used += getattr(st, "st_blocks", 0) * 512 or st.st_size
                    if ent.is_dir(follow_symlinks=False):
                        todo.append(ent.path)
        except OSError:
            continue
    return used, n


def _run_watched(argv, acct, cwd, env, timeout, watch=None):
    """Run a tool in its own process group as `acct`; kill the whole group on
    timeout, or when `watch` grows past the size / inode limit or the disk runs into
    the game's reserve. Returns (returncode, stderr tail, reason or None)."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, cwd=cwd, env=env, close_fds=True,
                             start_new_session=True, **_drop_kw(acct))
    except FileNotFoundError:
        raise HubError("%s is not installed" % os.path.basename(argv[0]))
    except OSError as exc:
        raise HubError("cannot start %s: %s" % (os.path.basename(argv[0]), exc))
    tail = bytearray()

    def _reader():
        try:
            while True:
                b = p.stderr.read1(65536)
                if not b:
                    break
                tail.extend(b)
                del tail[:-4096]
        except Exception:
            pass
    rt = threading.Thread(target=_reader, daemon=True)
    rt.start()
    why = None
    t0 = time.monotonic()
    while p.poll() is None:
        if time.monotonic() - t0 > timeout:
            why = "timed out after %ds" % timeout
            break
        if watch:
            used, n = _tree_usage(watch, SYNC_MAX_INODES)
            if used > SYNC_MAX_BYTES or n > SYNC_MAX_INODES:
                why = "the hub repository is too large (limit %d MiB / %d files)" % (
                    SYNC_MAX_BYTES >> 20, SYNC_MAX_INODES)
                break
            try:
                sv = os.statvfs(cwd)
                if sv.f_bavail * sv.f_frsize < wx.FS_RESERVE_BYTES:
                    why = "the disk is full (the game keeps a reserve)"
                    break
            except OSError:
                pass
        time.sleep(SYNC_POLL)
    if why:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()
    rt.join(1.0)
    return p.returncode, bytes(tail).decode("utf-8", "replace"), why


def _git_env(home) -> dict:
    return {"PATH": "/usr/bin:/bin", "LC_ALL": "C", "LANG": "C", "HOME": home,
            "XDG_CONFIG_HOME": os.path.join(home, ".no-config"),
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_ASKPASS": "/bin/false",
            "SSH_ASKPASS": "/bin/false", "GIT_SSH_COMMAND": "/bin/false",
            "GIT_ALLOW_PROTOCOL": "https" if _is_root() else "https:file",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_PROTOCOL_FROM_USER": "0"}


_GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false",
             "-c", "core.fsmonitor=false", "-c", "credential.helper=",
             "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
             "-c", "submodule.recurse=false", "-c", "transfer.fsckObjects=true",
             "-c", "http.followRedirects=initial", "-c", "core.askPass=/bin/false"]


def _open_cache_parent(parent, acct) -> int:
    """The directory holding the checkout: ours (0700) when not root; as root a
    private directory owned by the hub account, created once."""
    if acct is None:
        return wx.open_dir(parent, create=True, mode=0o700)
    gfd = wx.open_dir(os.path.dirname(parent), create=True, mode=0o755)
    try:
        fd = wx._subdir(gfd, os.path.basename(parent), create=True, mode=0o700)
    finally:
        os.close(gfd)
    try:
        st = os.fstat(fd)
        if st.st_uid not in (0, acct[1]):
            raise HubError("hub cache %s belongs to another account - refusing" % parent)
        if st.st_uid != acct[1] or st.st_gid != acct[2]:
            os.fchown(fd, acct[1], acct[2])
        if stat.S_IMODE(st.st_mode) != 0o700:
            os.fchmod(fd, 0o700)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _recover_checkout(pfd, base):
    """Drop half-finished clones; if a swap was interrupted, bring the last good
    checkout back."""
    stale, olds = [], []
    with os.scandir(pfd) as it:
        for n, ent in enumerate(it):
            if n > 256:
                break
            if ent.name.startswith(base + ".stage-"):
                stale.append(ent.name)
            elif ent.name.startswith(base + ".old-"):
                olds.append(ent.name)
    if olds and not wx._lexists_at(pfd, base):
        os.rename(sorted(olds)[-1], base, src_dir_fd=pfd, dst_dir_fd=pfd)
        olds = sorted(olds)[:-1]
    for nm in stale + olds:
        wx._rmtree_at(pfd, nm)


def sync(url=None) -> str:
    """Download the hub into a fresh staging directory (as the unprivileged hub
    account, bounded in size, inodes and time), verify its signed index, and only
    then swap it in for the last good checkout - an interrupted or refused download
    leaves the previous checkout untouched. Returns the checkout path."""
    url = url or hub_url()
    if not url:
        raise HubError("hub not configured")
    _check_url(url)
    acct = _fetch_account()
    cache = cache_dir()
    parent, base = os.path.dirname(cache), os.path.basename(cache)
    try:
        pfd = _open_cache_parent(parent, acct)
    except HubError:
        raise
    except Exception as exc:
        raise HubError("cannot prepare the hub cache: %s" % exc)
    try:
        try:
            lock = wx.dir_lock(pfd, timeout=SYNC_TIMEOUT + 10)
            lock.__enter__()
        except TimeoutError:
            raise HubError("another hub download is running")
        try:
            _recover_checkout(pfd, base)
            if not wx.fs_has_room(pfd, SYNC_MAX_BYTES, SYNC_MAX_INODES):
                raise HubError("not enough free space for the hub (needs %d MiB plus the "
                               "game's reserve)" % (SYNC_MAX_BYTES >> 20))
            stage_name = "%s.stage-%s" % (base, os.urandom(6).hex())
            stage = os.path.join(parent, stage_name)
            argv = [_tool("git"), *_GIT_SAFE]
            if not _is_root():
                argv += ["-c", "protocol.file.allow=always"]
            argv += ["clone", "--quiet", "--depth", "1", "--single-branch", "--no-tags",
                     "--no-recurse-submodules", "--", url, stage]
            rc, err, why = _run_watched(argv, acct, parent, _git_env(parent), SYNC_TIMEOUT,
                                        watch=stage)
            if why or rc != 0:
                wx._rmtree_at(pfd, stage_name)
                raise HubError("hub download failed: %s" % (why or err.strip()[-160:] or rc))
            try:
                verify_index(stage, record=False)
            except BaseException:
                wx._rmtree_at(pfd, stage_name)
                raise
            old = None
            if wx._lexists_at(pfd, base):
                old = "%s.old-%s" % (base, os.urandom(6).hex())
                os.rename(base, old, src_dir_fd=pfd, dst_dir_fd=pfd)
            os.rename(stage_name, base, src_dir_fd=pfd, dst_dir_fd=pfd)
            try:
                os.fsync(pfd)
            except OSError:
                pass
            if old:
                wx._rmtree_at(pfd, old)
        finally:
            lock.__exit__(None, None, None)
    finally:
        os.close(pfd)
    return cache


def _read_regular(path, limit) -> bytes:
    try:
        dfd = wx.open_dir(os.path.dirname(path))
    except (FileNotFoundError, NotADirectoryError):
        raise HubError("hub is missing %s - refusing" % os.path.basename(path))
    except OSError:
        raise HubError("%s is not a small regular file" % os.path.basename(path))
    try:
        return wx.read_at(dfd, os.path.basename(path), limit)
    except FileNotFoundError:
        raise HubError("hub is missing %s - refusing" % os.path.basename(path))
    except (ValueError, OSError):
        raise HubError("%s is not a small regular file" % os.path.basename(path))
    finally:
        os.close(dfd)


def _ssh_verify(data, sig, signers_bytes) -> bool:
    """`ssh-keygen -Y verify` as an unprivileged account, on private root-owned
    copies of the signature and the allowed-signers list; the message is exactly
    `data` (stdin)."""
    acct = _verify_account()
    keygen = _tool("ssh-keygen")
    tmp = tempfile.mkdtemp(prefix="wdgo-hub-verify-")
    try:
        if acct is not None:
            os.chmod(tmp, 0o711)
        tfd = wx.open_dir(tmp)
        try:
            for nm, blob in (("index.json.sig", sig), ("allowed_signers", signers_bytes)):
                fd = os.open(nm, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644,
                             dir_fd=tfd)
                with os.fdopen(fd, "wb") as f:
                    f.write(blob)
        finally:
            os.close(tfd)
        try:
            r = subprocess.run(
                [keygen, "-Y", "verify", "-f", os.path.join(tmp, "allowed_signers"),
                 "-I", SIGNER_IDENTITY, "-n", NAMESPACE, "-s", os.path.join(tmp, "index.json.sig")],
                input=data, capture_output=True, timeout=20, cwd="/", close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}, start_new_session=True,
                **_drop_kw(acct))
        except FileNotFoundError:
            raise HubError("ssh-keygen not available - cannot verify the hub")
        except subprocess.TimeoutExpired:
            raise HubError("hub signature check timed out")
        except OSError as exc:
            raise HubError("cannot run the signature check: %s" % exc)
        return r.returncode == 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _check_seq(idx, data, record):
    """Anti-rollback: a catalogue carrying catalog_seq must not be older than (or a
    different catalogue with the same number as) the newest one this device
    accepted. Once a numbered catalogue was seen, unnumbered ones are refused."""
    import hashlib
    digest = hashlib.sha256(data).hexdigest()
    seq = idx.get("catalog_seq")
    if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool)
                            or not 0 <= seq <= SEQ_MAX):
        raise HubError("hub catalogue has a bad release number - refusing")
    st = wx._read_json_file(hub_state_file(), {})
    have = st.get("seq")
    if not isinstance(have, int) or isinstance(have, bool):
        have = None
    if seq is None:
        if have is not None or MIN_CATALOG_SEQ > 0:
            raise HubError("hub catalogue carries no release number, this device already "
                           "accepted release %d - refusing" % (have or MIN_CATALOG_SEQ))
    else:
        if seq < MIN_CATALOG_SEQ:
            raise HubError("hub catalogue release %d is older than this game accepts" % seq)
        if have is not None and seq < have:
            raise HubError("hub catalogue release %d is OLDER than release %d this device "
                           "already accepted - refusing (rollback)" % (seq, have))
        if have is not None and seq == have and st.get("sha256") != digest:
            raise HubError("hub catalogue release %d differs from the one accepted before "
                           "- refusing" % seq)
        if record and (have is None or seq > have):
            def put(cur):
                c = cur.get("seq")
                if isinstance(c, int) and not isinstance(c, bool) and (
                        c > seq or (c == seq and cur.get("sha256") != digest)):
                    raise HubError("hub catalogue release changed meanwhile - refusing")
                return {"seq": seq, "sha256": digest, "t": int(time.time())}
            try:
                wx.update_json(hub_state_file(), put, {})
            except HubError:
                raise
            except Exception as exc:
                raise HubError("cannot record the hub release number (%s)" % exc)
    if record:
        last_catalog.update(seq=seq, sha256=digest)


def verify_index(cache=None, signers=None, record=True) -> dict:
    """Check index.json's signature and return the sanitised catalogue
    {id: entry}. Raises HubError when the signature or the content is not valid, or
    when the catalogue is older than one already accepted (record=True stores a
    newer release number)."""
    cache = cache or cache_dir()
    signers = signers or signers_file()
    try:
        signers_bytes = _read_regular(signers, SIGNERS_MAX)
    except HubError:
        raise HubError("no hub signing key shipped with this game build")
    data = _read_regular(os.path.join(cache, "index.json"), INDEX_MAX)
    sig = _read_regular(os.path.join(cache, "index.json.sig"), SIG_MAX)
    if not _ssh_verify(data, sig, signers_bytes):
        raise HubError("hub index signature is NOT valid - refusing")
    try:
        idx = json.loads(data.decode("utf-8"))
    except Exception:
        raise HubError("hub index is not valid JSON")
    if not isinstance(idx, dict) or idx.get("schema") != 1 or not isinstance(idx.get("extras"), dict):
        raise HubError("unsupported hub index format")
    _check_seq(idx, data, record)
    out = {}
    for eid, e in idx["extras"].items():
        if not (isinstance(eid, str) and _ID_RE.match(eid) and isinstance(e, dict)):
            continue
        sha = str(e.get("sha", ""))
        if not _SHA_RE.match(sha):
            continue
        api = e.get("api", 0)
        if not isinstance(api, int) or isinstance(api, bool):
            try:
                api = int(str(api)[:4])
            except (TypeError, ValueError):
                continue
        perms = e.get("permissions") if isinstance(e.get("permissions"), list) else []
        perms = sorted({p for p in perms if isinstance(p, str) and p in wx._KNOWN_PERMS})
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
    """'' (not installed) | 'installed' | 'update' | 'modified' (a hub copy edited
    locally) | 'local' (a hand-installed extra with the same id) |
    'needs-game-update'. Reads the installed folder: call it off the UI thread."""
    if entry["api"] > wx.EXTRAS_API:
        return "needs-game-update"
    try:
        kind, cur = wx.package_state(entry["id"])
    except Exception:
        return "modified"
    if kind == "none":
        return ""
    if cur is not None and cur == entry["sha"]:
        return "installed"
    if kind == "hub":
        return "update"
    return "modified" if kind == "hub-modified" else "local"


def install(eid, catalogue, cache=None, replace_local=None) -> str:
    """Install (or update) one extra from the verified catalogue. Returns its dir.
    The new version is activated atomically and the previous folder kept (see
    wdgo_extras.activate_package). A hand-installed extra with the same id is only
    replaced with replace_local=True (HubCollision otherwise)."""
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
    reg_entry = {"version": entry["version"], "sha": entry["sha"], "source": "hub",
                 "official": bool(entry.get("official")), "t": int(time.time())}
    try:
        return wx.activate_package(eid, src, entry["sha"], reg_entry, replace_local=replace_local)
    except wx.PackageCollision as exc:
        raise HubCollision("%s - confirm to replace it (the old folder is kept in %s)"
                           % (exc, os.path.join(wx.extras_root(), wx.BACKUP_DIR)))
    except HubError:
        raise
    except TimeoutError:
        raise HubError("another install is running - try again")
    except Exception as exc:
        raise HubError("install failed: %s" % exc)


def uninstall(eid) -> bool:
    """Remove a hub-installed extra (its saved data in extras-data/ is kept; a copy
    edited locally is moved to the backup folder, not deleted)."""
    if not isinstance(eid, str) or not _ID_RE.match(eid):
        return False
    try:
        return wx.remove_package(eid)
    except TimeoutError:
        raise HubError("another install is running - try again")
    except Exception as exc:
        raise HubError("remove failed: %s" % exc)


# ======================================================================
# One hub job queue for the whole game (screens come and go, jobs do not)
# ======================================================================
class _HubState:
    def __init__(self):
        self.lock = threading.Lock()
        self.busy = None            # label of the running job, or None
        self.msg, self.msg_col = "", "dim"
        self.cat = {}
        self.rows = []
        self.ratings = {}
        self.status = {}            # id -> status() result, computed in the background
        self.about = {}             # (id, sha) -> long description from the VERIFIED bytes
        self.icons = {}             # (id, sha) -> (w, h, rgba) decoded once, or None
        self.confirm = None         # (id, sha) waiting for a second [I] to replace a local copy
        self.changes = 0            # installs/removes done
        self.taken = 0              # ... of which the menu has already rescanned


_HUB = _HubState()
PREVIEW_MAX = 1000                  # catalogue entries whose about/icon are cached


def _say(text, col="dim"):
    with _HUB.lock:
        _HUB.msg, _HUB.msg_col = text, col


def busy():
    return _HUB.busy


def submit(label, fn) -> bool:
    """Run fn() on the hub worker. One job at a time for the whole game: returns
    False (nothing started) while another job runs."""
    with _HUB.lock:
        if _HUB.busy:
            return False
        _HUB.busy = label

    def run():
        try:
            fn()
        except HubCollision as exc:
            _say(str(exc), "warn")
        except HubError as exc:
            _say(str(exc), "danger")
        except Exception as exc:
            _say("%s: %s" % (type(exc).__name__, exc), "danger")
        finally:
            with _HUB.lock:
                _HUB.busy = None
    threading.Thread(target=run, daemon=True, name="wdgo-hub").start()
    return True


def take_changes() -> bool:
    """True once after each hub install/remove the EXTRAS menu has not rescanned for
    yet - also when the hub screen was closed while the job was still running."""
    with _HUB.lock:
        if _HUB.changes != _HUB.taken:
            _HUB.taken = _HUB.changes
            return True
        return False


def _statuses(cat) -> dict:
    return {eid: status(e) for eid, e in list(cat.items())[:PREVIEW_MAX]}


def _previews(cat, cache):
    """About text + icon of every catalogue entry, taken from the checkout ONLY when
    its bytes hash to the signed checksum, and kept in memory under (id, sha): what
    is shown never changes behind a verified checksum."""
    about, icons = {}, {}
    import wdgo_extras_png as png
    for eid, e in list(cat.items())[:PREVIEW_MAX]:
        key = (eid, e["sha"])
        if key in _HUB.about:
            about[key], icons[key] = _HUB.about[key], _HUB.icons.get(key)
            continue
        text, img = "", None
        d = os.path.join(cache, "extras", eid)
        try:
            src = wx.collect_sources(d)
            if wx.sources_hash(src) == e["sha"]:
                m = wx._meta_from_manifest(d, src.get("extra.json"), lambda r: r in src)
                text = " ".join(str(m.about or "").split())[:2000]
                if e.get("icon") and png.ICON_NAME in src:
                    try:
                        img = png.decode_icon(src[png.ICON_NAME])
                    except ValueError:
                        img = None
        except Exception:
            pass
        about[key], icons[key] = text, img
    return about, icons


def _job_refresh():
    _say("contacting the hub...", "dim")
    cache = sync()
    cat = verify_index(cache)
    # keep the registry in step with the signed catalogue: the "official" mark, and
    # re-adopt copies that are byte-identical to a signed entry but unregistered
    # (e.g. after decisions in an unprotected location were not imported)
    adopt = {}
    for eid, e in cat.items():
        try:
            kind, cur = wx.package_state(eid)
        except Exception:
            continue
        if kind == "local" and cur == e["sha"]:
            adopt[eid] = {"version": e["version"], "sha": e["sha"], "source": "hub",
                          "official": bool(e.get("official")), "t": int(time.time())}

    def put(reg):
        for eid, ent in reg.items():
            e = cat.get(eid)
            if isinstance(ent, dict) and e and ent.get("sha") == e["sha"] \
                    and ent.get("official") is not bool(e.get("official")):
                ent["official"] = bool(e.get("official"))
        reg.update(adopt)
        return reg
    try:
        wx.update_json(wx.installed_file(), put, {})
    except Exception:
        pass
    ratings = load_ratings(cache)
    about, icons = _previews(cat, cache)
    st = _statuses(cat)
    rows = sorted(cat.values(), key=lambda e: e["name"].lower())
    with _HUB.lock:
        _HUB.cat, _HUB.rows, _HUB.ratings = cat, rows, ratings
        _HUB.about, _HUB.icons, _HUB.status = about, icons, st
    _say("signature OK - %d reviewed extra(s)" % len(rows), "success")


def _job_statuses():
    st = _statuses(_HUB.cat)
    with _HUB.lock:
        _HUB.status = st


def _job_install(entry, replace):
    _say("installing %s..." % entry["name"], "dim")
    try:
        install(entry["id"], _HUB.cat, replace_local=True if replace else None)
    except HubCollision:
        with _HUB.lock:
            _HUB.confirm = (entry["id"], entry["sha"])
        raise
    finally:
        _job_statuses()
    with _HUB.lock:
        _HUB.changes += 1
        _HUB.confirm = None
    _say("%s v%s installed - find it in EXTRAS (it will ask before the first run)"
         % (entry["name"], entry["version"]), "success")


def _job_remove(entry):
    try:
        ok = uninstall(entry["id"])
    finally:
        _job_statuses()
    if ok:
        with _HUB.lock:
            _HUB.changes += 1
        _say("%s removed (its saved data was kept)" % entry["name"], "success")
    else:
        _say("%s was not installed from the hub" % entry["name"], "warn")


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
        self._sel = 0
        self._t0 = time.monotonic()
        self._detail = None             # the entry whose details card is open
        self._scroll = 0                # first visible line of the long text in the card
        self._query = ""                # search box text
        self._searching = False         # the search box has the keyboard
        self._cat_filter = None         # show one category only
        self._confirm = None            # (id, sha) armed by a first [I] on a local copy

    # The job queue, the catalogue, statuses and previews live in _HUB (module level),
    # so closing and reopening this screen never starts a second job and a result
    # that arrives after the screen closed still reaches the menu (take_changes()).
    @property
    def changed(self):
        """Something was installed/removed since the menu last rescanned (consumed)."""
        return take_changes()

    @changed.setter
    def changed(self, v):
        if v:
            with _HUB.lock:
                _HUB.changes += 1

    @property
    def _cat(self):
        return _HUB.cat

    @property
    def _rows(self):
        return _HUB.rows

    @property
    def _ratings(self):
        return _HUB.ratings

    @property
    def _busy(self):
        return _HUB.busy is not None

    @property
    def _msg(self):
        return _HUB.msg

    @property
    def _msg_col(self):
        return _HUB.msg_col

    def open(self):
        if _HUB.busy:
            return                      # a job from an earlier screen is still running
        self._refresh()

    def event(self, key):
        pass

    def close(self):
        self.state = "closed"

    def _say(self, text, col="dim"):
        _say(text, col)

    def _bg(self, label, fn):
        if not submit(label, fn):
            _say("busy: %s - wait for it to finish" % _HUB.busy, "warn")

    def _refresh(self):
        if not hub_url():
            _say("The hub is not configured yet in this build.", "warn")
            return
        self._bg("refreshing the hub", _job_refresh)

    def _install(self, entry, replace=False):
        self._bg("installing %s" % entry["name"], lambda: _job_install(entry, replace))

    def _remove(self, entry):
        self._bg("removing %s" % entry["name"], lambda: _job_remove(entry))

    _PERM_TEXT = {"gps": "reads your GPS position",
                  "loot_read": "reads the list of networks you have scanned",
                  "loot_write": "saves its own files into the session's loot folder"}

    def _about_text(self, entry) -> str:
        """The extra's long description, taken (in the background) from hub files that
        hashed to the signed checksum - text nobody reviewed is never shown."""
        return _HUB.about.get((entry["id"], entry["sha"]), "")

    def _icon(self, entry):
        """(w, h, rgba) of the extra's icon, or None: decoded once in the background,
        only from bytes that matched the signed checksum, only icon.png, within the
        icon limits (16 KB, 64x64 checked before inflating), pure-Python decoder."""
        return _HUB.icons.get((entry["id"], entry["sha"]))

    def _status(self, entry) -> str:
        return _HUB.status.get(entry["id"], "")

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
        st = self._status(e)
        tag, col = {"installed": ("INSTALLED", "success"), "update": ("UPDATE AVAILABLE", "warn"),
                    "modified": ("LOCAL COPY DIFFERS", "warn"),
                    "local": ("LOCAL EXTRA WITH THIS ID", "warn"),
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
                key = (cur["id"], cur["sha"])
                if self._confirm is not None and self._confirm != key:
                    self._confirm = None              # moved on: the arming is gone
                if b.key_pressed("i") or b.key_pressed("u"):
                    st = self._status(cur)
                    armed = self._confirm == key or _HUB.confirm == key
                    if st == "needs-game-update":
                        self._say("needs a newer game - update the game first", "warn")
                    elif st == "installed":
                        self._say("already installed and up to date", "dim")
                    elif st in ("modified", "local") and not armed:
                        self._confirm = key
                        self._say("'%s' already exists here (%s). Press [I] again to replace it - "
                                  "the old folder is kept in ~/.wdgo/extras/%s/"
                                  % (cur["id"], "edited locally" if st == "modified"
                                     else "installed by hand", wx.BACKUP_DIR), "warn")
                    else:
                        self._confirm = None
                        self._install(cur, replace=armed)
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
            st = self._status(e)
            tag, col = {"installed": ("INSTALLED", "success"), "update": ("UPDATE", "warn"),
                        "modified": ("LOCAL COPY DIFFERS", "warn"),
                        "local": ("LOCAL EXTRA", "warn"),
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
