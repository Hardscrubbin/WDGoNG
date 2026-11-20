#!/usr/bin/env python3
"""Offline-release test: CI builds a candidate, the maintainer tool signs and publishes.

    python3 extras/sim/test_hub_release.py

A throw-away hub (bare origin + clone) with a real extra in it. The "CI" step is
the same command the workflow runs (hub_build_index.py --out pending/index.json).
hub_release.py then signs with a throw-away key FILE (the dongle path needs a
person and a device; the signing step is the only part that differs). The result
is checked the way the game checks it, and the tool is attacked: edited candidate,
wrong key, dirty clone. No network. Everything under a temp HOME.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(_HERE))
_TMP = tempfile.mkdtemp(prefix="wdgo_reltest_")
os.environ["HOME"] = os.path.join(_TMP, "home")
os.makedirs(os.environ["HOME"])
os.environ.pop("SUDO_USER", None)
sys.path.insert(0, os.path.join(_REPO, "wdgong_ui"))
import wdgo_extras_hub as hub         # noqa: E402

HUBTOOLS = os.path.join(_REPO, "extras", "hub")
ORIGIN = os.path.join(_TMP, "origin.git")
CLONE = os.path.join(_TMP, "hub")
KEY = os.path.join(_TMP, "key")
OTHER = os.path.join(_TMP, "other")
SIGNERS = os.path.join(_TMP, "signers")
_passed = _failed = 0


def check(cond, what):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  ok   %s" % what)
    else:
        _failed += 1
        print("  FAIL %s" % what)


def sh(*argv, cwd=None):
    r = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def git(*a):
    rc, out = sh("git", "-C", CLONE, *a)
    assert rc == 0, out
    return out.strip()


def ci_build():
    """What the workflow's 'pending' job does after a merge."""
    rc, out = sh(sys.executable, os.path.join(HUBTOOLS, "hub_build_index.py"), CLONE, "--out", "pending/index.json")
    assert rc == 0, out
    git("add", "-A")
    git("commit", "-q", "-m", "pending: rebuild")
    git("push", "-q")


def release(*extra):
    return sh(sys.executable, os.path.join(HUBTOOLS, "hub_release.py"), CLONE, "--yes", *extra)


def read(rel):
    try:
        with open(os.path.join(CLONE, rel), "rb") as f:
            return f.read()
    except OSError:
        return None


def origin_file(rel):
    rc, out = sh("git", "-C", ORIGIN, "show", "main:" + rel)
    return out if rc == 0 else None


def main():
    for k in (KEY, OTHER):
        sh("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", k)
    open(SIGNERS, "w").write("wdgo-extras-hub " + " ".join(open(KEY + ".pub").read().split()[:2]) + "\n")
    sh("git", "init", "-q", "--bare", "-b", "main", ORIGIN)
    sh("git", "clone", "-q", ORIGIN, CLONE)
    git("config", "user.name", "t")
    git("config", "user.email", "t@example.invalid")
    git("checkout", "-q", "-b", "main")
    shutil.copytree(os.path.join(_REPO, "extras", "examples", "gps_speedo"), os.path.join(CLONE, "extras", "gps_speedo"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    git("add", "-A")
    git("commit", "-q", "-m", "add gps_speedo")
    git("push", "-q", "-u", "origin", "main")

    print("candidate only: nothing is signed by 'CI'")
    ci_build()
    check(read("pending/index.json") is not None, "CI wrote pending/index.json")
    check(read("index.json") is None and read("index.json.sig") is None, "CI wrote no index.json and no signature")

    print("official extras come from the hub's own list, not from the author string")
    idx = json.loads(read("pending/index.json"))["extras"]
    check("official" not in idx["gps_speedo"], "an extra signing as the maintainer but not on the list is not official")
    json.dump({"author": "LOCOSP", "extras": ["gps_speedo"]}, open(os.path.join(CLONE, "official.json"), "w"))
    shutil.copytree(os.path.join(_REPO, "extras", "examples", "key_copier"), os.path.join(CLONE, "extras", "key_copier"),
                    ignore=shutil.ignore_patterns("__pycache__"))
    rc, out = sh(sys.executable, os.path.join(HUBTOOLS, "hub_build_index.py"), CLONE, "--out", "pending/index.json")
    check(rc != 0 and "reserved" in out, "builder refuses an unlisted extra that uses the reserved author name")
    rc, out = sh(sys.executable, os.path.join(HUBTOOLS, "hub_check.py"), os.path.join(CLONE, "extras", "key_copier"))
    check(rc != 0 and "reserved" in out, "PR gate refuses it too")
    mk = os.path.join(CLONE, "extras", "key_copier", "extra.json")
    km = json.load(open(mk)); km["extra"]["author"] = "L0C0SP "; json.dump(km, open(mk, "w"))
    rc, out = sh(sys.executable, os.path.join(HUBTOOLS, "hub_check.py"), os.path.join(CLONE, "extras", "key_copier"))
    check(rc != 0 and "reserved" in out, "look-alike spelling (L0C0SP) is refused")
    shutil.rmtree(os.path.join(CLONE, "extras", "key_copier"))
    ci_build()
    idx = json.loads(read("pending/index.json"))["extras"]
    check(idx["gps_speedo"].get("official") is True, "a listed extra is marked official in the candidate")

    print("first release")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc == 0, "release succeeds")
    check("+ gps_speedo" in out and "NOWY" in out, "summary lists the new extra")
    check(read("index.json") == read("pending/index.json"), "index.json is byte-identical to the candidate")
    check(origin_file("index.json.sig") is not None, "index.json + signature were pushed")
    try:
        cat = hub.verify_index(cache=CLONE, signers=SIGNERS)
        check("gps_speedo" in cat, "the game's own verify_index accepts the release")
        check(cat["gps_speedo"]["official"] is True, "the game reads 'official' from the signed catalogue")
    except hub.HubError as exc:
        check(False, "the game's own verify_index accepts the release (%s)" % exc)

    print("nothing to do")
    head = git("rev-parse", "HEAD")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc == 0 and "Nic do podpisania" in out and git("rev-parse", "HEAD") == head, "second run signs nothing")

    print("update that asks for a new permission")
    mp = os.path.join(CLONE, "extras", "gps_speedo", "extra.json")
    m = json.load(open(mp))
    m["extra"]["version"] = "9.9.9"
    m["extra"]["permissions"] = sorted(set(m["extra"].get("permissions", [])) | {"loot_read"})
    json.dump(m, open(mp, "w"), indent=1)
    git("add", "-A")
    git("commit", "-q", "-m", "gps_speedo 9.9.9")
    git("push", "-q")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc != 0 and "NIE zgadza" in out, "refuses while CI has not rebuilt the candidate (stale pending)")
    ci_build()
    check(json.loads(read("index.json"))["extras"]["gps_speedo"]["version"] != "9.9.9",
          "players still have the previous release until it is signed")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc == 0 and "NOWE UPRAWNIENIA" in out and "loot_read" in out, "new permission is called out")
    check(json.loads(read("index.json"))["extras"]["gps_speedo"]["version"] == "9.9.9", "release now carries 9.9.9")
    try:
        hub.verify_index(cache=CLONE, signers=SIGNERS)
        check(True, "game accepts the second release")
    except hub.HubError as exc:
        check(False, "game accepts the second release (%s)" % exc)

    print("attacks")
    good_index, good_sig, head = read("index.json"), read("index.json.sig"), git("rev-parse", "HEAD")
    pp = os.path.join(CLONE, "pending", "index.json")
    idx = json.loads(read("pending/index.json"))
    idx["extras"]["gps_speedo"]["sha"] = "0" * 64                 # a runner swaps the hash
    json.dump(idx, open(pp, "w"), indent=1, sort_keys=True)
    open(pp, "a").write("\n")
    git("add", "-A")
    git("commit", "-q", "-m", "evil pending")
    git("push", "-q")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc != 0 and "NIE zgadza" in out, "edited candidate is refused (does not match the reviewed files)")
    check(read("index.json") == good_index and read("index.json.sig") == good_sig, "release untouched after the refusal")
    ci_build()                                                     # honest candidate again (same as release)

    m["extra"]["version"] = "9.9.10"
    json.dump(m, open(mp, "w"), indent=1)
    git("add", "-A")
    git("commit", "-q", "-m", "gps_speedo 9.9.10")
    git("push", "-q")
    ci_build()
    head = git("rev-parse", "HEAD")
    rc, out = release("--key", OTHER, "--signers", SIGNERS)
    check(rc != 0 and "NIE pochodzi" in out, "a key the game does not know is refused")
    check(git("rev-parse", "HEAD") == head and json.loads(read("index.json"))["extras"]["gps_speedo"]["version"] == "9.9.9",
          "nothing committed, release unchanged")
    open(os.path.join(CLONE, "extras", "gps_speedo", "extra.json"), "a").write(" ")
    rc, out = release("--key", KEY, "--signers", SIGNERS)
    check(rc != 0 and "niezatwierdzone" in out, "a dirty clone is refused")

    print("\n%d passed, %d failed" % (_passed, _failed))
    shutil.rmtree(_TMP, ignore_errors=True)
    sys.exit(1 if _failed else 0)


if __name__ == "__main__":
    main()
