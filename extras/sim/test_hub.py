#!/usr/bin/env python3
"""EXTRAS hub test-suite — the whole trust chain against a throw-away local hub.

    python3 extras/sim/test_hub.py

Builds a real git repository with an extra in it, a real signing key, a signed
index, then drives wdgo_extras_hub exactly as the game does: sync -> verify the
signature -> install by checksum -> consent -> run. Then it attacks the chain:
edited index, edited code, wrong key, missing signature. No network, no game.
Everything happens under a temp HOME, so the real ~/.wdgo is never touched.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_TMP = tempfile.mkdtemp(prefix="wdgo_hubtest_")
os.environ["HOME"] = os.path.join(_TMP, "home")
os.makedirs(os.environ["HOME"])
os.environ.pop("SUDO_USER", None)
sys.path.insert(0, os.path.join(_REPO, "wdgong_ui"))
import wdgo_extras as wx              # noqa: E402
import wdgo_extras_hub as hub         # noqa: E402

_passed = _failed = 0


def check(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   %s" % what)
    else:
        _failed += 1
        print("  FAIL %s" % what)


def sh(*argv, cwd=None, inp=None):
    r = subprocess.run(argv, cwd=cwd, input=inp, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError("%s -> %s" % (argv, r.stderr.decode()[-300:]))
    return r.stdout


def refuses(fn, needle):
    try:
        fn()
    except hub.HubError as exc:
        return needle in str(exc), str(exc)
    return False, "no error raised"


HUB = os.path.join(_TMP, "hub")
KEY = os.path.join(_TMP, "hubkey")
SIGNERS = os.path.join(_TMP, "signers")
TOOLS = os.path.join(_REPO, "extras", "hub")


def git(*a):
    return sh("git", "-c", "user.name=t", "-c", "user.email=t@t", *a, cwd=HUB)


def publish(key=KEY, sign=True):
    """What the hub's CI does on merge: rebuild + sign the index, commit."""
    sh(sys.executable, os.path.join(TOOLS, "hub_build_index.py"), HUB)
    sig = os.path.join(HUB, "index.json.sig")
    if os.path.exists(sig):
        os.unlink(sig)
    if sign:
        sh("ssh-keygen", "-Y", "sign", "-f", key, "-n", hub.NAMESPACE, os.path.join(HUB, "index.json"))
    git("add", "-A")
    git("commit", "-q", "-m", "publish", "--allow-empty")


# ---- a hub with one reviewed extra ------------------------------------------
os.makedirs(os.path.join(HUB, "extras"))
shutil.copytree(os.path.join(_REPO, "extras", "examples", "gps_speedo"),
                os.path.join(HUB, "extras", "hub_speedo"))
mf = os.path.join(HUB, "extras", "hub_speedo", "extra.json")
m = json.load(open(mf)); m["extra"]["id"] = "hub_speedo"; m["extra"]["name"] = "Hub Speedo"
m["extra"]["version"] = "1.0.0"
m["extra"]["author"] = "tester"          # the maintainer's name is reserved for official extras
m["extra"]["homepage"] = "https://github.com/example/hub_speedo"
m["extra"].setdefault("about", "A hub test speedometer extra.")
json.dump(m, open(mf, "w"))
sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "hub-test", "-f", KEY)
open(SIGNERS, "w").write("%s %s" % (hub.SIGNER_IDENTITY, open(KEY + ".pub").read()))
hub.signers_file = lambda: SIGNERS
sh("git", "init", "-q", "-b", "main", HUB)
publish()
os.makedirs(os.path.dirname(hub.config_file()), exist_ok=True)
json.dump({"url": HUB}, open(hub.config_file(), "w"))

print("[1] CI gate accepts a clean extra, rejects hostile ones")
r = subprocess.run([sys.executable, os.path.join(TOOLS, "hub_check.py"),
                    os.path.join(HUB, "extras", "hub_speedo")], capture_output=True, text=True)
check(r.returncode == 0 and "PASS" in r.stdout, "hub_check passes the reviewed extra")
bad = os.path.join(_TMP, "evil_extra"); os.makedirs(bad)
json.dump({"extra": {"id": "evil_extra", "name": "Evil", "author": "x", "version": "1.0.0", "api": 1,
                     "description": "d", "about": "a", "permissions": []}}, open(os.path.join(bad, "extra.json"), "w"))
open(os.path.join(bad, "main.py"), "w").write(
    "import os, socket\nclass Extra:\n    def on_frame(self, ctx, dt):\n"
    "        open('/etc/passwd'); eval('1'); ctx._b.x; ctx.gps.fix(); ().__class__.__subclasses__()\n")
r = subprocess.run([sys.executable, os.path.join(TOOLS, "hub_check.py"), bad], capture_output=True, text=True)
out = r.stdout
check(r.returncode == 1, "hub_check fails the hostile extra")
check("forbidden module 'os'" in out and "forbidden module 'socket'" in out, "flags forbidden imports")
check("calls open()" in out and "calls eval()" in out, "flags open()/eval()")
check("ctx internals" in out and "__subclasses__" in out, "flags ctx internals + dunder tricks")
check("uses 'gps' but the manifest does not declare it" in out, "flags undeclared permission")
nohp = os.path.join(_TMP, "nohp"); os.makedirs(nohp)
json.dump({"extra": {"id": "nohp", "name": "No HP", "author": "x", "version": "1.0.0", "api": 1,
                     "description": "d", "about": "a", "permissions": []}}, open(os.path.join(nohp, "extra.json"), "w"))
open(os.path.join(nohp, "main.py"), "w").write("class Extra:\n    def on_frame(self, ctx, dt):\n        ctx.text('x', 0, 0)\n")
r = subprocess.run([sys.executable, os.path.join(TOOLS, "hub_check.py"), nohp], capture_output=True, text=True)
check(r.returncode == 0 and "PASS" in r.stdout, "hub_check does NOT force a separate repo link (the hub folder is the source)")
check(hub.browse_url("https://github.com/x/wdgo-hub.git", "hub_speedo")
      == "https://github.com/x/wdgo-hub/tree/HEAD/extras/hub_speedo", "browse_url -> the extra's folder in the hub repo")
check(hub.browse_url("git@github.com:x/wdgo-hub.git", "hub_speedo")
      == "https://github.com/x/wdgo-hub/tree/HEAD/extras/hub_speedo", "browse_url handles git@ ssh urls")

print("\n[2] sync + signature verification + install by checksum")
cache = hub.sync()
cat = hub.verify_index(cache)
check("hub_speedo" in cat and cat["hub_speedo"]["version"] == "1.0.0", "signed index verified, extra listed")
check(cat["hub_speedo"]["repo"] == "https://github.com/example/hub_speedo", "catalogue carries the author's optional repo link")
check(hub.status(cat["hub_speedo"]) == "", "not installed yet")
dst = hub.install("hub_speedo", cat)
check(os.path.isfile(os.path.join(dst, "main.py")), "installed into ~/.wdgo/extras/")
check(hub.status(cat["hub_speedo"]) == "installed", "status = installed")
meta = next(x for x in wx.scan() if x.id == "hub_speedo")
sha = wx.sources_hash(wx.collect_sources(meta.dir))
check(not meta.error and sha == cat["hub_speedo"]["sha"], "installed bytes == the signed checksum")
check(wx.provenance(meta, sha) == "hub", "provenance = hub")
check(not wx.is_trusted(meta), "a hub extra is NOT trusted for in-process (always sandboxed)")

print("\n[3] consent card: asked once per code+permissions, then remembered")
import types
keys = set()
b = types.SimpleNamespace(draw_text=lambda *a: None, draw_rect=lambda *a: None, draw_line=lambda *a: None,
                          draw_circle=lambda *a: None, measure=lambda s, size=16: 1, chars=lambda: [],
                          key_down=lambda n: False, key_pressed=lambda n: n in keys, view=lambda: (800, 480),
                          gps=lambda: None, loot_networks=lambda: [], loot_counts=lambda: {},
                          loot_save=lambda n, t: None, log=lambda *a: None, release_all=lambda: None)
h = wx.launch(meta, b, os.path.join(_TMP, "data"), require_isolation=False)
check(isinstance(h, wx.ConsentScreen), "first launch shows the consent card")
import time
h.frame(0.1); time.sleep(0.5); h.frame(0.1)
check(h.next_host is None, "no key -> not accepted")
keys.add("y"); h.frame(0.1); keys.clear()
check(h.next_host is not None and getattr(h.next_host, "sandboxed", False), "[Y] -> hands over a SANDBOXED host")
h2 = wx.launch(meta, b, os.path.join(_TMP, "data"), require_isolation=False)
check(not isinstance(h2, wx.ConsentScreen), "second launch does not ask again")
open(os.path.join(meta.dir, "main.py"), "a").write("\n# edited locally\n")
h3 = wx.launch(meta, b, os.path.join(_TMP, "data"), require_isolation=False)
sha2 = wx.sources_hash(wx.collect_sources(meta.dir))
check(isinstance(h3, wx.ConsentScreen), "edited code -> asks again")
check(wx.provenance(meta, sha2) == "local", "edited code is no longer 'hub' (shown as UNVERIFIED)")
check(hub.status(cat["hub_speedo"]) == "modified", "hub shows LOCAL COPY DIFFERS")
hub.install("hub_speedo", cat)
check(hub.status(cat["hub_speedo"]) == "installed", "reinstall restores the verified copy")

print("\n[4] update flow")
m["extra"]["version"] = "1.1.0"; json.dump(m, open(mf, "w")); publish()
cat = hub.verify_index(hub.sync())
check(cat["hub_speedo"]["version"] == "1.1.0" and hub.status(cat["hub_speedo"]) == "update", "new version -> status = update")
hub.install("hub_speedo", cat)
check(hub.status(cat["hub_speedo"]) == "installed" and hub.installed()["hub_speedo"]["version"] == "1.1.0", "updated to 1.1.0")

print("\n[5] attacks on the chain")
# (a) code changed in the repo without re-signing the index
open(os.path.join(HUB, "extras", "hub_speedo", "main.py"), "a").write("\nimport os\n")
git("add", "-A"); git("commit", "-q", "-m", "sneak")
cat = hub.verify_index(hub.sync())
ok, why = refuses(lambda: hub.install("hub_speedo", cat), "checksum mismatch")
check(ok, "code changed after signing -> install refused (%s)" % why)
# (b) index edited (points at the modified code) but signature not redone
sh(sys.executable, os.path.join(TOOLS, "hub_build_index.py"), HUB)
git("add", "-A"); git("commit", "-q", "-m", "forged index")
ok, why = refuses(lambda: hub.verify_index(hub.sync()), "signature is NOT valid")
check(ok, "forged index with the old signature -> rejected (%s)" % why)
# (c) index re-signed with an attacker's key
EVIL = os.path.join(_TMP, "evilkey")
sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "evil", "-f", EVIL)
publish(key=EVIL)
ok, why = refuses(lambda: hub.verify_index(hub.sync()), "signature is NOT valid")
check(ok, "index signed by an unknown key -> rejected")
# (d) signature removed
publish(sign=False)
ok, why = refuses(lambda: hub.verify_index(hub.sync()), "missing index.json.sig")
check(ok, "missing signature -> rejected (%s)" % why)
# (e) right key, wrong namespace (a signature made for something else)
sh(sys.executable, os.path.join(TOOLS, "hub_build_index.py"), HUB)
sh("ssh-keygen", "-Y", "sign", "-f", KEY, "-n", "something-else", os.path.join(HUB, "index.json"))
git("add", "-A"); git("commit", "-q", "-m", "wrong ns")
ok, why = refuses(lambda: hub.verify_index(hub.sync()), "signature is NOT valid")
check(ok, "valid key but wrong namespace -> rejected")
check(hub.status({"id": "hub_speedo", "sha": hub.installed()["hub_speedo"]["sha"], "api": 1}) == "installed",
      "the installed copy was never touched by any failed attempt")

print("\n[6] uninstall")
check(hub.uninstall("hub_speedo") and not os.path.exists(os.path.join(wx.extras_root(), "hub_speedo")), "removed")
check(not hub.uninstall("../../etc"), "uninstall ignores ids that are not hub-installed")

print("\n[7] ratings: banana / peel")
sys.path.insert(0, TOOLS)
import hub_ratings as hr
calls = []
ISSUES = [
    {"number": 3, "title": "rating: hub_speedo", "html_url": "https://github.com/x/hub/issues/3",
     "reactions": {"+1": 12, "-1": 3}},
    {"number": 9, "title": "rating: hub_speedo", "html_url": "https://github.com/x/hub/issues/9",
     "reactions": {"+1": 999, "-1": 0}},                       # later duplicate: ignored
    {"number": 4, "title": "rating: hub_speedo", "pull_request": {}, "reactions": {"+1": 500}},
]
def fake(method, path, body=None):
    calls.append((method, path, body))
    if method == "GET" and "/labels/" in path:
        return 404, None
    if method == "GET" and "/issues?" in path:
        return 200, ISSUES
    if method == "POST" and path.endswith("/issues"):
        return 201, {"number": 10, "title": body["title"], "html_url": "https://github.com/x/hub/issues/10",
                     "reactions": {"+1": 0, "-1": 0}}
    return 201, {}
rh = os.path.join(_TMP, "rhub"); os.makedirs(rh)
json.dump({"schema": 1, "extras": {"hub_speedo": {"name": "Hub Speedo"}, "new_one": {"name": "New One"}}},
          open(os.path.join(rh, "index.json"), "w"))
res = hr.run(rh, "x/hub", fake)
check(res["hub_speedo"] == {"banana": 12, "peel": 3, "url": "https://github.com/x/hub/issues/3"},
      "counts from the oldest labelled issue; duplicates and PRs ignored")
created = [c for c in calls if c[0] == "POST" and c[1].endswith("/issues")]
check(len(created) == 1 and created[0][2]["title"] == "rating: new_one" and created[0][2]["labels"] == ["rating"],
      "opens a vote issue only for the extra that had none")
check(any(c[0] == "POST" and c[1].endswith("/labels") for c in calls), "creates the 'rating' label when missing")
p1 = open(hr.write_ratings(rh, res), "rb").read(); p2 = open(hr.write_ratings(rh, res), "rb").read()
check(p1 == p2, "ratings.json is deterministic (no needless commits)")
cache = hub.cache_dir()
json.dump({"schema": 1, "extras": {
    "good_one": {"banana": 7, "peel": 1, "url": "https://wdgwars.pl/vote/good_one"},
    "neg": {"banana": -5, "peel": True, "url": "javascript:alert(1)"},
    "../evil": {"banana": 1, "peel": 1, "url": "https://x"},
    "huge": {"banana": 10 ** 12, "peel": "9", "url": "https://a b"}}}, open(os.path.join(cache, "ratings.json"), "w"))
lr = hub.load_ratings(cache)
check(lr.get("good_one") == {"banana": 7, "peel": 1, "url": "https://wdgwars.pl/vote/good_one"},
      "game accepts any https vote link (ready for the portal later)")
check(lr.get("neg") == {"banana": 0, "peel": 0, "url": ""}, "negative / bool counts and non-https links dropped")
check("../evil" not in lr and lr.get("huge") == {"banana": 0, "peel": 0, "url": ""}, "bad ids, huge numbers, spaced urls dropped")
open(os.path.join(cache, "ratings.json"), "w").write("{not json")
check(hub.load_ratings(cache) == {}, "broken ratings.json -> no ratings, no crash")

shutil.rmtree(_TMP, ignore_errors=True)
print("\n%d passed, %d failed" % (_passed, _failed))
sys.exit(1 if _failed else 0)
