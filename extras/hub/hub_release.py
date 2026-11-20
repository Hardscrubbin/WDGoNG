#!/usr/bin/env python3
"""Publish a hub release: sign the candidate catalogue OFFLINE and push it.

    hub_release.py <hub-clone> [--key <private-key-file>] [--signers <file>] [--yes] [--no-pull] [--no-push]

CI never signs. After a reviewed merge it only writes pending/index.json. This tool,
run by the maintainer, turns that candidate into the release players trust:

  1. pulls the hub clone and REBUILDS the catalogue from the checked-out extras/ with
     this repository's own builder - the candidate must be byte-identical, so what
     gets signed is exactly the reviewed files, not whatever a runner produced;
  2. prints what changes for players (new / updated / NEW PERMISSION / removed) and
     the files touched since the last release;
  3. asks the signing dongle to sign it. The dongle downloads the candidate from the
     hub by itself, shows the same summary on its own screen and wants its PIN, so a
     compromised computer cannot get something else signed;
  4. checks the signature the way the game does, writes index.json + index.json.sig,
     commits and pushes.

--key signs with an OpenSSH private key file instead of the dongle (the encrypted
backup, if the dongle is lost). ssh-keygen asks for its passphrase on the terminal.
--signers names the allowed_signers file the result must satisfy (default: the one
shipped with the game, when this runs from the game repository).
Stop the key-manager dashboard first: only one program can talk to the dongle.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import hub_dongle as hd                                   # noqa: E402

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"   # git's well-known empty tree


class ReleaseError(Exception):
    """A release step refused or failed; the message is for the person (Polish)."""


def die(msg):
    raise ReleaseError(msg)


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", root] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        die("git %s: %s" % (" ".join(args), (r.stderr or r.stdout).strip()))
    return r.stdout.strip()


def load(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def entries(raw):
    if raw is None:
        return {}
    idx = json.loads(raw.decode("utf-8"))
    if not isinstance(idx, dict) or idx.get("schema") != 1 or not isinstance(idx.get("extras"), dict):
        die("katalog ma nieznany format")
    return idx["extras"]


def diff(cur, pen):
    """[(mark, id, text)] - the same classes the dongle shows: + ~ ! -"""
    out = []
    for eid in sorted(pen):
        e = pen[eid]
        if eid not in cur:
            out.append(("+", eid, "NOWY  v%s  autor: %s  uprawnienia: %s"
                        % (e.get("version"), e.get("author"), ", ".join(e.get("permissions", [])) or "brak")))
        elif cur[eid].get("sha") != e.get("sha"):
            added = sorted(set(e.get("permissions", [])) - set(cur[eid].get("permissions", [])))
            if added:
                out.append(("!", eid, "AKTUALIZACJA v%s -> v%s  NOWE UPRAWNIENIA: %s"
                            % (cur[eid].get("version"), e.get("version"), ", ".join(added))))
            else:
                out.append(("~", eid, "aktualizacja v%s -> v%s" % (cur[eid].get("version"), e.get("version"))))
        elif cur[eid] != e:
            out.append(("~", eid, "zmiana opisu (kod bez zmian)"))
    for eid in sorted(cur):
        if eid not in pen:
            out.append(("-", eid, "USUNIĘTY"))
    return out


def rebuild(root):
    """The catalogue as THIS machine builds it from the checked-out extras/."""
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "index.json")
        r = subprocess.run([sys.executable, os.path.join(HERE, "hub_build_index.py"), root, "--out", out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die("nie udało się zbudować katalogu lokalnie:\n" + r.stdout + r.stderr)
        return load(out)


def verify(data, sig_text, signers_line):
    with tempfile.TemporaryDirectory() as td:
        sp, ap = os.path.join(td, "s.sig"), os.path.join(td, "signers")
        open(sp, "w").write(sig_text)
        open(ap, "w").write(signers_line.strip() + "\n")
        r = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", ap, "-I", hd.IDENTITY, "-n", hd.NAMESPACE,
                            "-s", sp], input=data, capture_output=True)
    return r.returncode == 0


def game_signers():
    """The allowed_signers file shipped with the game, if this is run from the game repo."""
    root = os.path.dirname(os.path.dirname(HERE))
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d, "extras_hub_signers")
        if os.path.isfile(p):
            return p
    return None


def wait_for_hub(base, digest, limit=600, log=print):
    """The dongle downloads the candidate itself. Right after a push the file server
    still serves the previous copy for a few minutes - wait here, before the person is
    asked for a PIN, until what the hub serves is what we are about to sign."""
    if not base.startswith("http"):
        die("klucz nie ma ustawionego adresu huba")
    url, start, told = base.rstrip("/") + "/pending/index.json", time.time(), False
    while True:
        try:
            with urllib.request.urlopen(url, timeout=20) as r:
                if hashlib.sha512(r.read(4 * 1024 * 1024)).hexdigest() == digest:
                    return
        except OSError:
            pass
        if time.time() - start > limit:
            die("hub nadal podaje starą wersję kandydata (%s). Spróbuj ponownie za kilka minut." % url)
        if not told:
            log("Czekam, aż serwer plików huba odświeży kandydata (zwykle do 5 minut)...")
            told = True
        time.sleep(15)


def sign_with_dongle(data, dongle=None, log=print):
    """Sign with the dongle. `dongle`: an open hub_dongle.Dongle (the dashboard passes
    its own, it holds the port); None = open one here."""
    digest = hashlib.sha512(data).hexdigest()
    try:
        d = dongle or hd.Dongle()
        info = d.info()
    except SystemExit as exc:
        die("klucz nie odpowiada (%s). Zamknij dashboard klucza i spróbuj ponownie." % exc)
    q = bytes.fromhex(info["pub"])
    wait_for_hub(info.get("hub", "-"), digest, log=log)
    log("Spójrz na klucz: porównaj odcisk %s i potwierdź PIN-em." % digest[:16])
    r = d.cmd("sign " + digest, timeout=600)
    if not r.startswith("OK"):
        if "does not match" in r:
            die("klucz odmówił: katalog pobrany przez klucz z huba różni się od lokalnego.\n"
                "       Jeśli przed chwilą był push, serwer plików mógł jeszcze nie odświeżyć kopii -\n"
                "       odczekaj kilka minut i uruchom ponownie.")
        die("klucz: " + r)
    p = r.split()
    if p[4] != "verified":
        die("klucz nie mógł sam porównać katalogu z hubem (brak Wi-Fi?). Nie publikuję.")
    return hd.sshsig(q, bytes.fromhex(p[2]), bytes.fromhex(p[3])), "%s %s" % (hd.IDENTITY, hd.pubkey_line(q))


def sign_with_key(data, key):
    with tempfile.TemporaryDirectory() as td:
        f = os.path.join(td, "index.json")
        open(f, "wb").write(data)
        # the passphrase prompt goes to the terminal itself, not to these pipes
        r = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", key, "-n", hd.NAMESPACE, f], capture_output=True)
        if r.returncode != 0:
            die("ssh-keygen nie podpisał katalogu")
        sig = open(f + ".sig").read()
    pub = subprocess.run(["ssh-keygen", "-y", "-f", key], capture_output=True, text=True)
    if pub.returncode != 0:
        die("nie udało się odczytać klucza publicznego z pliku")
    return sig, "%s %s" % (hd.IDENTITY, " ".join(pub.stdout.split()[:2]))


def prepare(root, pull=True) -> dict:
    """Check the hub clone and the candidate. Returns
    {"ready": bool, "changes": [(mark, id, text, [file lines])], "pending": bytes,
     "fp": first 16 hex of SHA-512, "new_perms": bool}. Raises ReleaseError."""
    root = os.path.abspath(root)
    if not os.path.isdir(os.path.join(root, ".git")):
        die("%s nie jest klonem repozytorium huba" % root)
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        die("klon huba ma niezatwierdzone zmiany - wydanie robi się z czystego stanu")
    if pull:
        git(root, "pull", "--ff-only", "-q")
    pending = load(os.path.join(root, "pending", "index.json"))
    if pending is None:
        die("brak pending/index.json - CI jeszcze nie zbudował kandydata")
    current = load(os.path.join(root, "index.json"))
    cur_sig = load(os.path.join(root, "index.json.sig"))
    if rebuild(root) != pending:
        die("pending/index.json NIE zgadza się z katalogiem zbudowanym lokalnie z extras/. "
            "Albo CI jeszcze nie skończył po ostatnim merge'u, albo ktoś zmienił kandydata. Nie podpisuję.")
    fp = hashlib.sha512(pending).hexdigest()[:16]
    if current == pending and cur_sig:
        return {"ready": False, "changes": [], "pending": pending, "fp": fp, "new_perms": False}
    last = git(root, "log", "-1", "--format=%H", "--", "index.json", check=False)
    changes = []
    for mark, eid, text in diff(entries(current), entries(pending)):
        files = []
        if mark != "-":
            stat = git(root, "diff", "--stat", last or EMPTY_TREE, "HEAD", "--", "extras/" + eid, check=False)
            files = [ln.strip() for ln in stat.splitlines()[:12]]
        changes.append((mark, eid, text, files))
    return {"ready": True, "changes": changes, "pending": pending, "fp": fp,
            "new_perms": any(c[0] == "!" for c in changes)}


def publish(root, prep, key=None, dongle=None, signers=None, push=True, log=print) -> str:
    """Sign the prepared candidate, check it against the game's signers, commit and
    push. Returns the commit id. Raises ReleaseError (nothing is written then)."""
    root = os.path.abspath(root)
    pending = prep["pending"]
    if load(os.path.join(root, "pending", "index.json")) != pending:
        die("kandydat zmienił się od sprawdzenia - sprawdź jeszcze raz")
    sig, signer = sign_with_key(pending, key) if key else sign_with_dongle(pending, dongle=dongle, log=log)
    if not verify(pending, sig, signer):
        die("podpis nie przeszedł weryfikacji - nic nie zapisuję")
    gs = signers or game_signers()
    if gs:
        lines = [ln for ln in open(gs).read().splitlines() if ln.strip() and not ln.startswith("#")]
        if not any(verify(pending, sig, ln) for ln in lines):
            die("podpis jest poprawny, ale NIE pochodzi od klucza wpisanego do gry (%s) - "
                "gracze by go odrzucili. Nie publikuję." % gs)
        log("Podpis zgodny z kluczem wpisanym do gry.")
    else:
        log("Uwaga: gra nie ma jeszcze pliku extras_hub_signers - sprawdziłem podpis tylko kluczem, który podpisał.")
    n = len(prep["changes"])
    with open(os.path.join(root, "index.json"), "wb") as f:
        f.write(pending)
    with open(os.path.join(root, "index.json.sig"), "w") as f:
        f.write(sig)
    git(root, "add", "index.json", "index.json.sig")
    git(root, "commit", "-q", "-m", "release: sign catalogue (%d change%s)" % (n, "" if n == 1 else "s"))
    commit = git(root, "rev-parse", "--short", "HEAD")
    log("Zapisano index.json + index.json.sig, commit %s." % commit)
    if not push:
        log("Bez pusha (--no-push).")
        return commit
    git(root, "pull", "--rebase", "-q")
    git(root, "push", "-q")
    log("Opublikowano. Gracze zobaczą wydanie przy następnym odświeżeniu huba.")
    return commit


def main():
    a = sys.argv[1:]
    yes, no_pull, no_push = "--yes" in a, "--no-pull" in a, "--no-push" in a
    a = [x for x in a if x not in ("--yes", "--no-pull", "--no-push")]
    key = None
    if "--key" in a:
        i = a.index("--key")
        key = a[i + 1]
        del a[i:i + 2]
    signers = None
    if "--signers" in a:
        i = a.index("--signers")
        signers = a[i + 1]
        del a[i:i + 2]
    if len(a) != 1:
        print(__doc__)
        sys.exit(2)
    root = os.path.abspath(a[0])
    try:
        prep = prepare(root, pull=not no_pull)
        if not prep["ready"]:
            print("Nic do podpisania: opublikowany katalog jest aktualny.")
            return
        changes = prep["changes"]
        print("Wydanie huba: %d zmian(y), odcisk %s\n" % (len(changes), prep["fp"]))
        for mark, eid, text, files in changes:
            print("  %s %-20s %s" % (mark, eid, text))
            for ln in files:
                print("        " + ln)
        if prep["new_perms"]:
            print("\n  UWAGA: są aktualizacje z NOWYMI UPRAWNIENIAMI - gracze dostaną ponownie kartę zgody.")
        if not changes:
            print("  (brak zmian w treści - publikacja pierwszego podpisu albo samego podpisu)")
        if not yes and input("\nPodpisać i opublikować? [tak/N] ").strip().lower() != "tak":
            die("przerwano - nic nie zostało podpisane")
        print()
        publish(root, prep, key=key, signers=signers, push=not no_push)
    except ReleaseError as exc:
        print("BŁĄD: %s" % exc)
        sys.exit(1)


if __name__ == "__main__":
    main()
