#!/usr/bin/env python3
"""Hub CI gate for ONE submitted extra.

    python3 hub_check.py <path-to-extras/<id>> [--sdk <dir with wdgo_extras*.py>]

Run on every pull request to the hub, before a human looks at it. It is a
REVIEW AID, not the security boundary (that is the runtime sandbox): it rejects
what is plainly not allowed and gives the reviewer a short list of things to read.

Checks, in order:
  1. manifest      valid extra.json (same loader the game uses), id == folder name
  2. files         only source/doc/image files, no symlinks, size caps, no binaries
  3. imports       every import is the sandbox stdlib allowlist or a local module
  4. forbidden     eval/exec/compile/__import__/open, dunder-attribute tricks,
                   reaching into ctx internals (ctx._b ...)
  5. permissions   capabilities used in code are declared in the manifest
  6. smoke run     the extra runs N frames in the real sandbox runtime without faulting

Exit code 0 = pass. Any FAIL line = exit 1. WARN lines are for the reviewer.
"""
import ast
import os
import stat
import sys
import time
import types

ALLOWED_EXT = {".py", ".json", ".md", ".txt", ".png"}
MAX_FILE = 512 * 1024
MAX_TOTAL = 2 * 1024 * 1024
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "open", "input",
                   "globals", "locals", "vars", "breakpoint", "memoryview"}
FORBIDDEN_MODULES = {"os", "sys", "gc", "inspect", "importlib", "builtins", "ctypes",
                     "socket", "subprocess", "threading", "multiprocessing", "pathlib",
                     "shutil", "tempfile", "signal", "pickle", "marshal", "code", "pty"}
CAP_ATTRS = {"gps": "gps", "loot": "loot_read", "loot_save": "loot_write"}

_fail = 0


def FAIL(msg):
    global _fail
    _fail += 1
    print("FAIL  " + msg)


def WARN(msg):
    print("WARN  " + msg)


def OK(msg):
    print("ok    " + msg)


def _find_sdk(here):
    """Directory holding wdgo_extras.py: a 'sdk' dir next to this tool (the hub
    repository layout), else the game package found near the repository root."""
    cand = [os.path.join(here, "sdk")]
    root = here
    for _ in range(3):
        root = os.path.dirname(root)
        try:
            cand += [os.path.join(root, d) for d in sorted(os.listdir(root))]
        except OSError:
            pass
    for c in cand:
        if os.path.isfile(os.path.join(c, "wdgo_extras.py")):
            return c
    return os.path.join(here, "sdk")


def main():
    args = [a for a in sys.argv[1:]]
    sdk = None
    if "--sdk" in args:
        i = args.index("--sdk")
        sdk = args[i + 1]
        del args[i:i + 2]
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    d = os.path.abspath(args[0])
    here = os.path.dirname(os.path.abspath(__file__))
    sdk = sdk or _find_sdk(here)
    sys.path.insert(0, sdk)
    import wdgo_extras as wx
    import wdgo_extras_child as child
    import wdgo_extras_sandbox as sb

    eid = os.path.basename(d.rstrip(os.sep))
    print("== hub check: %s ==" % eid)
    import hub_official
    official = hub_official.load(os.path.join(os.path.dirname(os.path.dirname(d)), "official.json"),
                                 os.path.join(os.path.dirname(here), "official.json"),
                                 os.path.join(here, "template", "official.json"))

    # 1. manifest ------------------------------------------------------------
    meta = wx._load_meta(d)
    if meta.error:
        FAIL("manifest: %s" % meta.error)
        sys.exit(1)
    OK("manifest valid: %s v%s by %s, api %d, permissions %s"
       % (meta.name, meta.version, meta.author or "?", meta.api, meta.permissions or "none"))
    if hub_official.reserved_name_misuse(official, meta.id, meta.author):
        FAIL("manifest: the author name '%s' is reserved for the hub's own extras - use your own name"
             % official["author"])
    elif hub_official.is_official(official, meta.id, meta.author):
        OK("official extra (listed in official.json)")
    for field in ("author", "description", "about"):
        if not getattr(meta, field):
            FAIL("manifest: '%s' is required for the hub" % field)
    # The verifiable source is the extra's folder in THIS hub repo (that is what the
    # PR added and what the game checksums), so a separate repo link is optional.
    hp = meta.homepage
    if hp and not (hp.startswith("https://") or hp.startswith("http://")):
        FAIL("manifest: 'homepage', if set, must be an http(s) link")

    # 2. files ---------------------------------------------------------------
    total = 0
    n_png = 0
    pyfiles = []
    for base, dirs, files in os.walk(d):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for nm in dirs + files:
            p = os.path.join(base, nm)
            rel = os.path.relpath(p, d)
            st = os.lstat(p)
            if stat.S_ISLNK(st.st_mode):
                FAIL("files: symlink not allowed: %s" % rel)
                continue
            if nm.startswith("."):
                FAIL("files: hidden file/dir not allowed: %s" % rel)
            if stat.S_ISDIR(st.st_mode):
                continue
            ext = os.path.splitext(nm)[1].lower()
            if ext not in ALLOWED_EXT:
                FAIL("files: type not allowed: %s" % rel)
            if st.st_size > MAX_FILE:
                FAIL("files: too large (%d bytes): %s" % (st.st_size, rel))
            total += st.st_size
            if ext == ".py":
                pyfiles.append((rel, p))
            if ext == ".png":
                n_png += 1
                try:
                    import wdgo_extras_png
                    with open(p, "rb") as f:
                        raw = f.read(wdgo_extras_png.IMG_MAX_BYTES + 1)
                    if rel == wdgo_extras_png.ICON_NAME:
                        iw, ih = wdgo_extras_png.check_icon(raw)
                        OK("icon:  %s  %dx%d  %d bytes" % (rel, iw, ih, st.st_size))
                    else:
                        iw, ih, _ = wdgo_extras_png.decode(raw)
                        OK("image: %s  %dx%d  %d bytes" % (rel, iw, ih, st.st_size))
                except ValueError as exc:
                    FAIL("image: %s - %s" % (rel, exc))
    if n_png > wx.IMG_MAX_COUNT:
        FAIL("files: too many images (%d, max %d)" % (n_png, wx.IMG_MAX_COUNT))
    if n_png and meta.api < 2:
        FAIL('manifest: an extra that ships images (an icon counts) must declare "api": 2 - '
             "older games do not know images and would refuse it with a checksum error")
    if total > MAX_TOTAL:
        FAIL("files: extra is too large in total (%d bytes)" % total)
    try:
        src = wx.collect_sources(d)
        OK("files: %d source file(s), content hash %s" % (len(src), wx.sources_hash(src)[:16]))
    except Exception as exc:
        FAIL("files: %s" % exc)

    # 3-5. static scan ---------------------------------------------------------
    allowed = set(m.split(".")[0] for m in child.ALLOWED_STDLIB)
    local = set()
    for rel, _p in pyfiles:
        parts = rel[:-3].replace(os.sep, "/").split("/")
        local.add(parts[0])
    used_caps = set()
    for rel, p in pyfiles:
        try:
            tree = ast.parse(open(p, "r", encoding="utf-8").read(), filename=rel)
        except SyntaxError as exc:
            FAIL("%s: syntax error line %s" % (rel, exc.lineno))
            continue
        for node in ast.walk(tree):
            ln = getattr(node, "lineno", 0)
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                         else [node.module or ""])
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue                                   # relative = local
                for full in names:
                    top = full.split(".")[0]
                    if top in FORBIDDEN_MODULES:
                        FAIL("%s:%d imports forbidden module '%s'" % (rel, ln, full))
                    elif top not in allowed and top not in local:
                        FAIL("%s:%d imports '%s' - not available in the sandbox" % (rel, ln, full))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in FORBIDDEN_CALLS:
                FAIL("%s:%d calls %s()" % (rel, ln, node.func.id))
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("getattr", "setattr", "delattr"):
                WARN("%s:%d uses %s() - reviewer: check it is not used to dodge this scan"
                     % (rel, ln, node.func.id))
            elif isinstance(node, ast.Attribute):
                a = node.attr
                if a.startswith("__") and a.endswith("__") and a not in ("__init__", "__name__", "__class__", "__dict__", "__doc__"):
                    FAIL("%s:%d touches dunder attribute .%s" % (rel, ln, a))
                elif a in ("__class__", "__dict__"):
                    WARN("%s:%d touches .%s" % (rel, ln, a))
                elif a.startswith("_") and not a.startswith("__") \
                        and isinstance(node.value, ast.Name) and node.value.id == "ctx":
                    FAIL("%s:%d reaches into ctx internals (ctx.%s)" % (rel, ln, a))
                if isinstance(node.value, ast.Name) and node.value.id == "ctx" and a in CAP_ATTRS:
                    used_caps.add(CAP_ATTRS[a])
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "require" and node.args \
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value in CAP_ATTRS:
                used_caps.add(CAP_ATTRS[node.args[0].value])
    declared = set(meta.permissions or [])
    for cap in sorted(used_caps - declared):
        FAIL("permissions: code uses '%s' but the manifest does not declare it" % cap)
    for cap in sorted(declared - used_caps):
        WARN("permissions: '%s' is declared but no use was found - drop it if unneeded" % cap)
    if not _fail:
        OK("static scan clean (imports, forbidden calls, permissions)")

    # 6. smoke run in the real sandbox runtime -------------------------------
    if _fail:
        print("\n%d problem(s) - not running the extra" % _fail)
        sys.exit(1)
    logs = []
    b = types.SimpleNamespace()
    b.draw_text = b.draw_rect = b.draw_line = b.draw_circle = b.draw_image = lambda *a: None
    b.measure = lambda s, size=16: len(str(s)) * size * 0.6
    b.key_down = b.key_pressed = lambda n: False
    b.chars = lambda: []
    b.view = lambda: (1232, 616)
    b.gps = lambda: {"valid": True, "lat": 50.06, "lon": 19.93, "sats": 9,
                     "speed_kmh": 10.0, "heading": 45.0}
    b.loot_networks = lambda: [{"ssid": "CI-AP", "bssid": "AA:BB:CC:DD:EE:FF", "channel": 6}]
    b.loot_counts = lambda: {"networks": 1}
    b.loot_save = lambda name, text: "/ci/" + str(name)
    b.log = lambda msg, level="info": logs.append((level, msg))
    b.release_all = lambda: None
    import tempfile
    full = sb.full_isolation_possible()
    h = sb.SandboxHost(meta, b, tempfile.mkdtemp(prefix="hubci_"), require_isolation=full)
    h.open()
    frames, end = 0, time.time() + 20
    while time.time() < end and h.state in ("loading", "running") and frames < 60:
        h.frame(1 / 30.0)
        if h.state == "running" and not h._awaiting:
            frames += 1
        time.sleep(0.01)
    state, err, iso, nops = h.state, h.error, h.isolation, len(h._ops)
    h.close()
    if state != "running":
        FAIL("smoke run: extra faulted in the sandbox: %s" % err)
    elif nops == 0:
        FAIL("smoke run: extra drew nothing in 60 frames")
    else:
        OK("smoke run: 60 frames in the sandbox (isolation=%s), %d draw ops/frame" % (iso, nops))
        if iso != "full":
            WARN("smoke run was NOT kernel-jailed (run the check as root on Linux for the full jail)")

    print("\n%s" % ("PASS" if not _fail else "%d problem(s)" % _fail))
    sys.exit(1 if _fail else 0)


if __name__ == "__main__":
    main()
