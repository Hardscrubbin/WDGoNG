#!/usr/bin/env python3
"""Local web dashboard for the signing dongle (needs pyserial). UI in Polish.

    hub_dongle_web.py            opens http://127.0.0.1:8790/ in your browser

Runs ONLY on this computer and talks to the dongle over USB - the dongle itself
never serves a web page, so the device holding the key is not exposed on the
network. The page can rename the device, manage its Wi-Fi networks, set the hub
address, ask it to check the hub, start a signature and start a PIN change.
Signing still needs confirmation on the dongle, the dongle still refuses anything
that does not match what it downloaded itself, and PIN digits are typed on the
dongle's own button - they never pass through this page or this computer.

The server binds to 127.0.0.1, accepts only requests carrying a per-run token and
the right Host header (so another site open in the browser cannot drive it).
Stop it before flashing new firmware: it keeps the serial port open.
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hub_dongle as hd                                   # noqa: E402

PORT = 8790


def _token():
    """Per-install token kept in a 0600 file, so an open tab survives a restart."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dongle", ".web_token")
    try:
        t = open(path).read().strip()
        if len(t) >= 24:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(t)
    return t


TOKEN = _token()
LOCK = threading.Lock()
_dongle = None

# device replies (English, stable) -> what the page shows
PL = [
    ("wrong pin", "Zły PIN"),
    ("pin locked", "PIN zablokowany po 5 błędnych próbach — odłącz i podłącz klucz"),
    ("not confirmed on the device", "Nie potwierdzono na urządzeniu (minął czas)"),
    ("timeout", "Minął czas na wpisanie PIN-u na urządzeniu"),
    ("the two entries differ", "Dwa wpisane PIN-y różnią się — nic nie zmieniono"),
    ("digest does not match", "Urządzenie odmówiło: katalog różni się od tego, który samo pobrało z huba"),
    ("no such network", "Nie ma takiej sieci"),
    ("hub <http", "Adres huba musi zaczynać się od http:// lub https://"),
    ("wifi add", "Nie można dodać sieci (maksymalnie 6)"),
    ("signing failed", "Podpisywanie nie powiodło się"),
    ("export already used", "Eksport był już wykonany i jest zablokowany na stałe"),
    ("set a pin first", "Najpierw ustaw PIN — eksport wymaga PIN-u"),
    ("not a valid p-256", "To nie jest poprawny klucz"),
]


def pl(msg):
    low = msg.lower()
    for key, text in PL:
        if key in low:
            tail = msg[msg.find("("):] if "(" in msg and key == "wrong pin" else ""
            return (text + " " + tail).strip()
    return msg


def dongle():
    global _dongle
    if _dongle is None:
        _dongle = hd.Dongle()
    return _dongle


def call(line, timeout=20.0):
    """One command to the dongle; reconnects once if the port went away."""
    global _dongle
    with LOCK:
        for attempt in (0, 1):
            try:
                return dongle().cmd(line, timeout)
            except BaseException as exc:                  # hd raises SystemExit on a silent device
                if isinstance(exc, KeyboardInterrupt):
                    raise
                try:
                    if _dongle is not None:
                        _dongle.ser.close()
                except Exception:
                    pass
                _dongle = None
                if attempt:
                    raise RuntimeError("brak połączenia z kluczem (%s)" % exc)


def need_ok(r):
    if not r.startswith("OK"):
        raise RuntimeError(pl(r[4:] if r.startswith("ERR ") else r))
    return r


def unh(v):
    return "" if v in ("-", "") else bytes.fromhex(v).decode("utf-8", "replace")


def state():
    r = need_ok(call("info"))
    i = dict(kv.split("=", 1) for kv in r.split()[2:] if "=" in kv)
    w = call("wifi list").split(" ", 2)
    nets = [unh(x) for x in w[2].split(";") if x] if len(w) > 2 else []
    q = bytes.fromhex(i["pub"])
    return {"name": unh(i["name"]), "fw": i["fw"], "mode": i["mode"], "wifi": unh(i["wifi"]),
            "networks": nets, "hub": "" if i["hub"] == "-" else i["hub"], "namespaces": [i["ns"]],
            "pin": int(i.get("pin", 0)), "locked": i.get("locked") == "1",
            "exported": i.get("exp") == "1",
            "signer_line": "%s %s" % (hd.IDENTITY, hd.pubkey_line(q))}


def check():
    r = call("check", timeout=60)
    f = dict(kv.split("=", 1) for kv in r.split()[2:] if "=" in kv)
    ok = r.startswith("OK")
    err = "" if ok else {"0": "adres huba nie jest ustawiony", "1": "brak połączenia Wi-Fi"}.get(
        f.get("state"), f.get("err", "błąd"))
    return {"ok": ok, "pending": int(f.get("pending", 0)), "sha": f.get("sha", "-"), "err": err}


def sign_pending():
    st = state()
    if not st["hub"]:
        raise RuntimeError("najpierw ustaw adres huba")
    with urllib.request.urlopen(st["hub"].rstrip("/") + "/pending/index.json", timeout=20) as r:
        data = r.read(2 * 1024 * 1024)
    digest = hashlib.sha512(data).hexdigest()
    p = need_ok(call("sign " + digest, timeout=600)).split()
    q = bytes.fromhex(need_ok(call("info")).split("pub=")[1].split()[0])
    sig = hd.sshsig(q, bytes.fromhex(p[2]), bytes.fromhex(p[3]))
    with tempfile.TemporaryDirectory() as td:
        sp, ap = os.path.join(td, "s.sig"), os.path.join(td, "signers")
        open(sp, "w").write(sig)
        open(ap, "w").write("%s %s\n" % (hd.IDENTITY, hd.pubkey_line(q)))
        v = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", ap, "-I", hd.IDENTITY,
                            "-n", hd.NAMESPACE, "-s", sp], input=data, capture_output=True)
    if v.returncode != 0:
        raise RuntimeError("podpis NIE przeszedł weryfikacji ssh-keygen")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dongle", "last_signed")
    os.makedirs(out, exist_ok=True)                       # gitignored; what was signed + its signature
    open(os.path.join(out, "index.json"), "wb").write(data)
    open(os.path.join(out, "index.json.sig"), "w").write(sig)
    return {"signature": sig, "digest": digest, "checked_by_dongle": p[4] == "verified"}


def _keygen(args, path, passphrase):
    """ssh-keygen with the passphrase fed through an askpass helper (never on argv)."""
    ask = os.path.join(os.path.dirname(path), "ask")
    with open(ask, "w") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$LSP_PASS"\n')
    os.chmod(ask, 0o700)
    env = dict(os.environ, SSH_ASKPASS=ask, SSH_ASKPASS_REQUIRE="force", LSP_PASS=passphrase, DISPLAY=":0")
    return subprocess.run(["ssh-keygen"] + args + ["-f", path], env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True)


def _wipe(path):
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * n)
        os.unlink(path)
    except OSError:
        pass


def encrypt_backup(d: bytes, passphrase: str, expect_pub_line: str) -> str:
    """Raw P-256 scalar -> passphrase-encrypted OpenSSH private key (bcrypt KDF, AES-256-CTR).
    The result works with plain `ssh-keygen -Y sign` if the dongle is ever lost."""
    from cryptography.hazmat.primitives import serialization as ser
    from cryptography.hazmat.primitives.asymmetric import ec
    key = ec.derive_private_key(int.from_bytes(d, "big"), ec.SECP256R1())
    with tempfile.TemporaryDirectory() as td:               # 0700, removed afterwards
        kp = os.path.join(td, "k")
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(key.private_bytes(ser.Encoding.PEM, ser.PrivateFormat.OpenSSH, ser.NoEncryption()))
        try:
            r = _keygen(["-p", "-q", "-a", "64"], kp, passphrase)
            if r.returncode != 0:
                raise RuntimeError("szyfrowanie kopii nie powiodło się")
            text = open(kp).read()
            back = _keygen(["-y"], kp, passphrase)
            if back.returncode != 0 or back.stdout.split()[:2] != expect_pub_line.split()[:2]:
                raise RuntimeError("kopia nie przeszła kontroli (nie zgadza się z kluczem urządzenia)")
        finally:
            _wipe(kp)
    return text


def decrypt_backup(text: str, passphrase: str) -> bytes:
    from cryptography.hazmat.primitives import serialization as ser
    with tempfile.TemporaryDirectory() as td:
        kp = os.path.join(td, "k")
        fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text.strip() + "\n")
        try:
            r = _keygen(["-p", "-q", "-N", ""], kp, passphrase)
            if r.returncode != 0:
                raise RuntimeError("nie udało się odszyfrować kopii (złe hasło albo uszkodzony tekst)")
            key = ser.load_ssh_private_key(open(kp, "rb").read(), None)
        finally:
            _wipe(kp)
    num = key.private_numbers()
    if num.public_numbers.curve.name != "secp256r1":
        raise RuntimeError("kopia zawiera klucz innego typu")
    return num.private_value.to_bytes(32, "big")


def export_backup(body):
    pw, pw2 = str(body.get("password", "")), str(body.get("password2", ""))
    if pw != pw2:
        raise RuntimeError("hasła nie są takie same")
    if len(pw) < 12 or "\n" in pw:
        raise RuntimeError("hasło do kopii: co najmniej 12 znaków")
    st = state()
    if st["exported"]:
        raise RuntimeError("Eksport był już wykonany i jest zablokowany na stałe")
    r = need_ok(call("export", timeout=400)).split()
    d = bytes.fromhex(r[2])
    try:
        text = encrypt_backup(d, pw, st["signer_line"].split(" ", 1)[1])
    finally:
        d = b""
    return {"backup": text}


def import_backup(body):
    d = decrypt_backup(str(body.get("backup", "")), str(body.get("password", "")))
    try:
        need_ok(call("import " + d.hex(), timeout=400))
    finally:
        d = b""
    return {"ok": True}


# ---------------------------------------------------------------- signing queue
# One job at a time: checking or publishing. The page polls /api/job; the job holds
# the dongle (LOCK) only while the dongle is actually needed.
JOB = {"state": "idle", "kind": "", "log": [], "result": "", "error": ""}
CHECKS = {}                          # purpose id -> last check() result
JOB_LOCK = threading.Lock()


def _job_log(msg):
    JOB["log"].append(str(msg))
    del JOB["log"][:-40]


def _run_job(kind, fn):
    with JOB_LOCK:
        if JOB["state"] == "running":
            raise RuntimeError("trwa inne zadanie - poczekaj, aż się skończy")
        JOB.update(state="running", kind=kind, log=[], result="", error="")

    def run():
        try:
            JOB["result"] = fn() or ""
            JOB["state"] = "done"
        except BaseException as exc:                       # hub_dongle raises SystemExit
            JOB["error"] = pl(str(exc))
            JOB["state"] = "error"
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True}


def start_check():
    import signing_purposes as sp

    def fn():
        for p in sp.PURPOSES:
            _job_log("Sprawdzam: %s ..." % p.title)
            CHECKS[p.id] = dict(p.check(), id=p.id, title=p.title, namespace=p.namespace)
        n = sum(1 for c in CHECKS.values() if c.get("ready"))
        return "do podpisu: %d" % n if n else "nic nie czeka na podpis"
    return _run_job("check", fn)


def start_publish(pid):
    import signing_purposes as sp
    targets = ([p for p in sp.PURPOSES if CHECKS.get(p.id, {}).get("ready")] if pid == "all"
               else [p for p in [sp.by_id(pid)] if p and CHECKS.get(p.id, {}).get("ready")])
    if not targets:
        raise RuntimeError("nic do podpisania - najpierw sprawdź")

    def fn():
        done = []
        for p in targets:
            _job_log("== %s" % p.title)
            with LOCK:                                    # the dongle is ours until this one is done
                res = p.publish(dongle(), _job_log)
            _job_log(res)
            done.append("%s: %s" % (p.title, res))
            CHECKS[p.id] = dict(CHECKS.get(p.id, {}), ready=False, summary=[], warn="")
        return "; ".join(done)
    return _run_job("publish", fn)


def do_post(path, body):
    if path == "/api/name":
        name = str(body.get("name", "")).strip()
        if not (1 <= len(name) <= 16) or any(ord(c) < 32 or ord(c) > 126 for c in name):
            raise RuntimeError("nazwa: 1–16 znaków, bez polskich liter (ekran ma tylko ASCII)")
        need_ok(call("name " + name.encode().hex()))
    elif path == "/api/wifi/add":
        ssid, pw = str(body.get("ssid", "")), str(body.get("password", ""))
        if not (1 <= len(ssid.encode()) <= 32) or len(pw.encode()) > 63:
            raise RuntimeError("nazwa sieci 1–32 bajty, hasło do 63")
        need_ok(call("wifi add %s %s" % (ssid.encode().hex(), pw.encode().hex())))
    elif path == "/api/wifi/del":
        need_ok(call("wifi del %d" % int(body.get("index", -1))))
    elif path == "/api/hub":
        need_ok(call("hub " + str(body.get("url", "")).strip()))
    elif path == "/api/flip":
        need_ok(call("flip"))
    elif path == "/api/pin/set":
        n = int(body.get("len", 0))
        if not 4 <= n <= 8:
            raise RuntimeError("PIN: od 4 do 8 cyfr")
        need_ok(call("pin set %d" % n, timeout=1200))      # digits are typed on the device
    elif path == "/api/pin/clear":
        need_ok(call("pin clear", timeout=400))
    elif path == "/api/export":
        return export_backup(body)
    elif path == "/api/export/done":
        need_ok(call("export done"))
    elif path == "/api/import":
        return import_backup(body)
    elif path == "/api/check":
        return check()
    elif path == "/api/sign":
        return sign_pending()
    elif path == "/api/queue/check":
        return start_check()
    elif path == "/api/queue/publish":
        return start_publish(str(body.get("id", "")))
    else:
        raise RuntimeError("nieznana akcja")
    return {"ok": True}


PAGE = r"""<!doctype html><html lang="pl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Klucz</title>
<style>
:root{--bg:#0b0a08;--panel:#14120e;--edge:#2c2618;--txt:#e2dccd;--dim:#948b78;--hi:#fff7e6;
--mango:#ffb02e;--mango2:#ff8a1f;--ok:#4cd67a;--bad:#ff5f56;--ink:#140f04}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{background:var(--mango);color:var(--ink);padding:16px 20px}
header>div{max-width:980px;margin:0 auto}
h1{margin:0;font-size:26px;letter-spacing:1px}h1 small{font-size:13px;font-weight:400;opacity:.75}
.sub{font-size:13px;opacity:.75}
main{max-width:980px;margin:0 auto;padding:18px 20px;display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}
section{background:var(--panel);border:1px solid var(--edge);border-radius:10px;padding:16px}
section.wide{grid-column:1/-1}h2{margin:0 0 10px;font-size:14px;color:var(--mango);letter-spacing:1px;text-transform:uppercase}
input,select{background:#0e0c09;border:1px solid var(--edge);color:var(--txt);padding:8px 10px;border-radius:6px;font:inherit}
input{width:100%}input:focus,select:focus,button:focus-visible{outline:2px solid var(--mango);outline-offset:1px}
button{background:#211c12;border:1px solid var(--edge);color:var(--hi);padding:8px 14px;border-radius:6px;font:inherit;cursor:pointer}
button:hover{border-color:var(--mango)}button.go{background:var(--mango);border-color:var(--mango);color:var(--ink);font-weight:700}
button.go:hover{background:var(--mango2)}button:disabled{opacity:.5;cursor:wait}
.row{display:flex;gap:8px;margin-top:8px;align-items:center;flex-wrap:wrap}.row>input{flex:1;min-width:120px}
.dim{color:var(--dim);font-size:13px}.warn{color:var(--mango)}.ok{color:var(--ok)}.bad{color:var(--bad)}
ul{list-style:none;margin:8px 0 0;padding:0}ul li{display:flex;justify-content:space-between;align-items:center;
padding:6px 0;border-bottom:1px solid var(--edge)}ul li button{padding:3px 10px}
.card{border:1px solid var(--edge);border-radius:8px;padding:12px;margin-top:12px}
.card h3{margin:0 0 6px;font-size:15px;color:var(--hi)}.chg{margin:4px 0;font-size:13px}
.chg b{display:inline-block;width:1.4em}.chg.perm{color:var(--bad);font-weight:700}
pre{background:#0e0c09;border:1px solid var(--edge);border-radius:6px;padding:10px;overflow:auto;font-size:12px;margin:8px 0 0;white-space:pre-wrap;word-break:break-all}
.tag{display:inline-block;border:1px solid var(--mango);color:var(--mango);border-radius:20px;padding:1px 10px;font-size:12px;margin-right:6px}
#msg{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);z-index:9;max-width:min(92vw,900px);background:#211c12;border:2px solid var(--mango);border-radius:10px;padding:12px 18px;font-size:15px;box-shadow:0 6px 24px #000a}#msg:empty{display:none}main{padding-bottom:90px}
ol{margin:6px 0 0;padding-left:20px}
</style></head><body>
<header><div><h1><span id="t-name">…</span> <small>zarządzanie kluczem · tylko ten komputer, po USB</small></h1>
<div class="sub" id="t-sub"></div></div></header>
<div id="msg"></div>
<main>
<section><h2>Urządzenie</h2>
  <div class="dim">Nazwa na ekranie klucza (bez polskich liter)</div>
  <div class="row"><input id="name" maxlength="16"><button onclick="act('/api/name',{name:v('name')})">Zapisz</button></div>
  <div class="row"><button onclick="act('/api/flip',{})">Obróć ekran o 180°</button></div>
  <p class="dim" id="mode"></p>
</section>
<section><h2>PIN</h2>
  <div id="pinstate" class="dim"></div>
  <div class="row"><select id="pinlen"><option>4</option><option>5</option><option selected>6</option><option>7</option><option>8</option></select>
    <span class="dim">cyfr</span><button id="pinset" onclick="pinSet()">Ustaw PIN</button>
    <button id="pinclr" onclick="pinClear()">Usuń PIN</button></div>
  <div class="dim" style="margin-top:10px">Cyfry wpisujesz przyciskiem na kluczu, nigdy tutaj:
    <ol><li>krótkie naciśnięcie: cyfra +1</li>
    <li>długie (ok. 1 s, aż pojawi się zielona kreska): zatwierdź cyfrę</li></ol></div>
</section>
<section><h2>Sieci Wi-Fi</h2>
  <div class="dim">Klucz łączy się z siecią tylko po to, żeby odczytać hub. Hasła zostają w kluczu i nie da się ich odczytać z powrotem.</div>
  <ul id="nets"></ul>
  <div class="row"><input id="ssid" placeholder="nazwa sieci"><input id="pw" type="password" placeholder="hasło"></div>
  <div class="row"><button onclick="addNet()">Dodaj sieć</button><span class="dim" id="wifi"></span></div>
</section>
<section><h2>Co podpisuje ten klucz</h2>
  <div id="ns"></div>
  <p class="dim">Każdy rodzaj podpisu ma własną nazwę, a klucz pokazuje ją przed podpisaniem. Nowe rodzaje dochodzą z aktualizacją firmware'u. Trybu „podpisz cokolwiek” nie ma.</p>
</section>
<section class="wide" id="queue"><h2>Do podpisu</h2>
  <div class="dim">Każdy rodzaj podpisu ma własną kartę. Sprawdź, co czeka, przejrzyj zmiany i opublikuj - klucz poprosi o PIN przy każdym podpisie.</div>
  <div class="row"><button onclick="qCheck()" id="qcheck">Sprawdź, co czeka na podpis</button>
    <button class="go" id="qall" style="display:none" onclick="qPublish('all')">Podpisz i opublikuj wszystkie</button>
    <span class="dim" id="qstate"></span></div>
  <div id="qcards"></div>
  <pre id="qlog" style="display:none"></pre>
</section>
<section class="wide"><h2>Hub z dodatkami</h2>
  <div class="dim">Adres bazowy, pod którym klucz znajdzie <code>index.json</code> i <code>pending/index.json</code></div>
  <div class="row"><input id="hub" placeholder="https://raw.githubusercontent.com/WLASCICIEL/REPO/main"><button onclick="act('/api/hub',{url:v('hub')})">Zapisz</button></div>
  <div class="row"><button onclick="doCheck()">Sprawdź hub teraz</button><span id="chk" class="dim"></span></div>
  <div class="row"><button id="signbtn" onclick="doSign()">Podpis testowy (bez publikacji)…</button>
    <span class="dim">tylko sprawdza klucz i zapisuje podpis lokalnie; prawdziwe wydanie: „Do podpisu” wyżej</span></div>
  <pre id="sig" style="display:none"></pre>
</section>
<section class="wide"><h2>Kopia zapasowa klucza</h2>
  <div id="bkstate" class="dim"></div>
  <div id="bkform">
    <div class="dim" style="margin-top:8px">Klucz opuści urządzenie tylko raz, od razu zaszyfrowany hasłem, które tu podasz (min. 12 znaków). Hasła nie zapisuj razem z kopią. Eksport potwierdzasz PIN-em na kluczu.</div>
    <div class="row"><input id="bp1" type="password" placeholder="hasło do kopii" autocomplete="new-password"><input id="bp2" type="password" placeholder="powtórz hasło" autocomplete="new-password"></div>
    <div class="row"><button id="bkbtn" onclick="doExport()">Wyeksportuj kopię (jednorazowo)</button></div>
  </div>
  <div id="bkout" style="display:none">
    <div class="warn" style="margin-top:8px">To jest Twoja kopia. Wklej ją do bezpiecznej notatki albo pobierz plik. Po zamknięciu strony już jej tu nie zobaczysz.</div>
    <pre id="bktext"></pre>
    <div class="row"><button onclick="navigator.clipboard.writeText($('bktext').textContent).then(()=>say('Skopiowano kopię do schowka.','ok'))">Kopiuj</button>
      <button onclick="dlBackup()">Pobierz plik</button>
      <button class="go" onclick="lockExport()">Zapisałem kopię — zablokuj eksport</button></div>
  </div>
  <details style="margin-top:14px"><summary class="dim" style="cursor:pointer">Odtwórz klucz z kopii (zastępuje klucz w tym urządzeniu)</summary>
    <textarea id="rtext" rows="6" style="width:100%;margin-top:8px;background:#0e0c09;border:1px solid var(--edge);color:var(--txt);border-radius:6px;padding:8px;font:12px ui-monospace,Menlo,monospace" placeholder="-----BEGIN OPENSSH PRIVATE KEY-----"></textarea>
    <div class="row"><input id="rpw" type="password" placeholder="hasło do kopii"><button onclick="doImport()">Odtwórz</button></div>
  </details>
</section>
<section class="wide"><h2>Klucz publiczny</h2>
  <div class="dim">Ta linia trafia do pliku <code>extras_hub_signers</code> w grze. Można ją udostępniać.</div>
  <pre id="pub"></pre>
</section>
</main>
<script>
"use strict";
const TOKEN="__TOKEN__";
const $=id=>document.getElementById(id), v=id=>$(id).value;
let busy=false;
function say(t,c){ const s=document.createElement("span"); s.className=c||"dim"; s.textContent=t; $("msg").replaceChildren(s); }
async function api(path,body){
  const r=await fetch(path,{method:body?"POST":"GET",headers:{"X-Key-Token":TOKEN,"Content-Type":"application/json"},
    body:body?JSON.stringify(body):undefined});
  const j=await r.json(); if(!r.ok) throw new Error(j.error||("HTTP "+r.status)); return j;
}
function span(cls,text){ const s=document.createElement("span"); s.className=cls; s.textContent=text; return s; }
async function load(){
  if(busy) return;
  try{
    const s=await api("/api/state");
    $("t-name").textContent=s.name; document.title=s.name+" · klucz";
    $("t-sub").textContent="firmware "+s.fw; if(document.activeElement!==$("name")) $("name").value=s.name;
    $("mode").replaceChildren(s.mode==="DEV"
      ? span("warn", s.pin ? "Tryb DEV: klucz jest w pamięci urządzenia, zaszyfrowany PIN-em. Do testów."
                           : "Tryb DEV bez PIN-u: klucz leży w pamięci urządzenia niezabezpieczony. Tylko do testów.")
      : span("ok","Klucz sprzętowy: nie da się go odczytać z urządzenia."));
    $("pinstate").replaceChildren(s.locked ? span("bad","PIN zablokowany po 5 błędnych próbach. Odłącz i podłącz klucz.")
      : s.pin ? span("ok","PIN ustawiony ("+s.pin+" cyfr). Każdy podpis wymaga PIN-u.")
              : span("warn","PIN nie jest ustawiony. Podpis potwierdza samo naciśnięcie przycisku."));
    $("pinset").textContent=s.pin?"Zmień PIN":"Ustaw PIN"; $("pinclr").style.display=s.pin?"":"none";
    $("bkstate").replaceChildren(s.exported ? span("ok","Kopia została wykonana, eksport jest zablokowany na stałe.")
      : s.pin ? span("warn","Kopia nie została jeszcze wykonana.") : span("warn","Kopia nie została jeszcze wykonana. Najpierw ustaw PIN."));
    if(s.exported && $("bkout").style.display==="none") $("bkform").style.display="none";
    $("wifi").textContent=s.wifi?("połączono: "+s.wifi):"brak połączenia";
    const ul=$("nets"); ul.replaceChildren();
    s.networks.forEach((n,i)=>{ const li=document.createElement("li"), sp=document.createElement("span"), b=document.createElement("button");
      sp.textContent=n; b.textContent="usuń"; b.onclick=()=>act("/api/wifi/del",{index:i}); li.append(sp,b); ul.append(li); });
    if(!s.networks.length){ const li=document.createElement("li"); li.className="dim"; li.textContent="brak zapisanych sieci"; ul.append(li); }
    $("ns").replaceChildren(...s.namespaces.map(n=>span("tag",n)));
    if(document.activeElement!==$("hub")) $("hub").value=s.hub; $("pub").textContent=s.signer_line;
  }catch(e){ say("Klucz: "+e.message,"bad"); }
}
async function act(path,body){ try{ await api(path,body); say("Zapisano.","ok"); }catch(e){ say(e.message,"bad"); } load(); }
async function longAct(path,body,hint,done){ busy=true; say(hint,"warn");
  try{ await api(path,body); say(done,"ok"); }catch(e){ say(e.message,"bad"); } busy=false; load(); }
function pinSet(){ longAct("/api/pin/set",{len:+v("pinlen")},
  "Spójrz na klucz: wpisz przyciskiem obecny PIN (jeśli jest), potem nowy i jeszcze raz nowy.","PIN ustawiony."); }
function pinClear(){ longAct("/api/pin/clear",{},"Spójrz na klucz: wpisz przyciskiem obecny PIN.","PIN usunięty."); }
async function doExport(){ busy=true; $("bkbtn").disabled=true;
  say("Spójrz na klucz: wpisz PIN, żeby zezwolić na eksport.","warn");
  try{ const r=await api("/api/export",{password:v("bp1"),password2:v("bp2")});
    $("bktext").textContent=r.backup; $("bkout").style.display="block"; $("bkform").style.display="none"; $("bp1").value=""; $("bp2").value="";
    say("Kopia gotowa i sprawdzona. Zapisz ją, potem zablokuj eksport.","ok");
  }catch(e){ say(e.message,"bad"); } $("bkbtn").disabled=false; busy=false; load(); }
function dlBackup(){ const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([$("bktext").textContent],{type:"text/plain"})); a.download="lsp-key-backup.txt"; a.click(); }
async function lockExport(){ try{ await api("/api/export/done",{}); $("bkout").style.display="none"; $("bktext").textContent="";
    say("Eksport zablokowany na stałe.","ok"); }catch(e){ say(e.message,"bad"); } load(); }
function doImport(){ if(!v("rtext")||!v("rpw")) return say("Wklej kopię i podaj hasło.","warn");
  longAct("/api/import",{backup:v("rtext"),password:v("rpw")},"Spójrz na klucz: potwierdź import (PIN albo przycisk).","Klucz odtworzony z kopii.");
  $("rpw").value=""; }
async function addNet(){ if(!v("ssid")) return say("Podaj nazwę sieci.","warn");
  await act("/api/wifi/add",{ssid:v("ssid"),password:v("pw")}); $("ssid").value=""; $("pw").value=""; }
async function doCheck(){ $("chk").textContent="sprawdzam… (klucz pobiera hub)";
  try{ const c=await api("/api/check",{});
    $("chk").replaceChildren(c.ok ? (c.pending ? span("warn","do podpisu: "+c.pending+" · odcisk "+c.sha.slice(0,16))
      : span("ok","nic do podpisania, katalog jest aktualny")) : span("bad",c.err||"sprawdzenie nie powiodło się"));
  }catch(e){ $("chk").textContent=e.message; } load(); }
async function doSign(){ const b=$("signbtn"); b.disabled=true; busy=true; $("sig").style.display="none";
  say("Spójrz na klucz: porównaj odcisk i potwierdź (PIN albo przycisk).","warn");
  try{ const r=await api("/api/sign",{});
    say("Podpisano i zweryfikowano przez ssh-keygen. "+(r.checked_by_dongle?"Klucz sam porównał katalog z hubem.":"Klucz NIE mógł porównać katalogu z hubem."),r.checked_by_dongle?"ok":"warn");
    $("sig").textContent=r.signature; $("sig").style.display="block";
  }catch(e){ say("Nie podpisano: "+e.message,"bad"); } b.disabled=false; busy=false; load(); }
let qPoll=null;
function qRender(j){
  const cards=$("qcards"); cards.replaceChildren();
  const ready=(j.checks||[]).filter(c=>c.ready);
  $("qall").style.display = ready.length>1 && j.state!=="running" ? "" : "none";
  (j.checks||[]).forEach(c=>{
    const d=document.createElement("div"); d.className="card";
    const h=document.createElement("h3"); h.textContent=c.title; d.append(h);
    const ns=span("dim","przestrzeń podpisu: "+c.namespace+(c.where?"   ·   "+c.where:"")); d.append(ns);
    if(c.ready){
      const fp=document.createElement("div"); fp.className="chg"; fp.append(span("warn","odcisk do porównania z ekranem klucza: "+c.fp)); d.append(fp);
      (c.summary||[]).forEach(([m,t])=>{ const r=document.createElement("div"); r.className="chg"+(m==="!"?" perm":"");
        const b=document.createElement("b"); b.textContent=m; r.append(b, document.createTextNode(t)); d.append(r); });
      if(!(c.summary||[]).length) d.append(span("dim","(sam podpis, bez zmian w treści)"));
      if(c.warn){ const w=document.createElement("div"); w.className="chg perm"; w.textContent=c.warn; d.append(w); }
      const row=document.createElement("div"); row.className="row";
      const btn=document.createElement("button"); btn.className="go"; btn.textContent="Podpisz i opublikuj";
      btn.disabled = j.state==="running"; btn.onclick=()=>qPublish(c.id); row.append(btn); d.append(row);
    } else {
      const s=document.createElement("div"); s.className="chg";
      s.append(c.warn ? span("bad",c.warn) : span("ok","nic nie czeka - opublikowane jest aktualne")); d.append(s);
    }
    cards.append(d);
  });
  $("qcheck").disabled = j.state==="running";
  $("qstate").textContent = j.state==="running" ? (j.kind==="publish"?"publikuję…":"sprawdzam…") : (j.result||"");
  if(j.log && j.log.length){ $("qlog").textContent=j.log.join("\n"); $("qlog").style.display="block"; }
  if(j.state==="error") say("Nie udało się: "+j.error,"bad");
}
async function qWatch(){
  try{ const j=await api("/api/job"); qRender(j);
    if(j.state==="running"){ busy=true; qPoll=setTimeout(qWatch,1500); }
    else { busy=false; qPoll=null; if(j.kind==="publish" && j.state==="done") say("Gotowe: "+j.result,"ok"); load(); }
  }catch(e){ busy=false; say(e.message,"bad"); }
}
async function qCheck(){ try{ await api("/api/queue/check",{}); say("Sprawdzam, co czeka na podpis…","dim"); qWatch(); }catch(e){ say(e.message,"bad"); } }
async function qPublish(id){
  if(!confirm(id==="all"?"Podpisać i opublikować wszystkie oczekujące? Klucz poprosi o PIN przy każdym.":"Podpisać i opublikować? Klucz poprosi o PIN.")) return;
  try{ await api("/api/queue/publish",{id:id}); say("Spójrz na klucz: gdy pokaże prośbę o PIN, porównaj odcisk i wpisz PIN (180 s).","warn"); qWatch(); }
  catch(e){ say(e.message,"bad"); } }
load(); setInterval(load,8000); qCheck();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj=None, html=None):
        data = html.encode() if html is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8" if html is not None else "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        return self.headers.get("Host", "") in ("127.0.0.1:%d" % PORT, "localhost:%d" % PORT)

    def do_GET(self):
        if not self._allowed():
            return self._send(403, {"error": "forbidden"})
        if self.path == "/?k=" + TOKEN:
            return self._send(200, html=PAGE.replace("__TOKEN__", TOKEN))
        if self.path == "/api/job" and secrets.compare_digest(self.headers.get("X-Key-Token", ""), TOKEN):
            return self._send(200, dict(JOB, checks=list(CHECKS.values())))   # never touches the dongle
        if self.path == "/api/state" and secrets.compare_digest(self.headers.get("X-Key-Token", ""), TOKEN):
            try:
                return self._send(200, state())
            except Exception as exc:
                return self._send(500, {"error": str(exc)})
        self._send(403, {"error": "otwórz link wypisany przez hub_dongle_web.py"})

    def do_POST(self):
        if not self._allowed() or not secrets.compare_digest(self.headers.get("X-Key-Token", ""), TOKEN):
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b"{}")
            self._send(200, do_post(self.path, body if isinstance(body, dict) else {}))
        except Exception as exc:
            self._send(400, {"error": str(exc)})


def main():
    url = "http://127.0.0.1:%d/?k=%s" % (PORT, TOKEN)
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    except OSError:
        # already running (the token is per-install, so the same link works): just show it
        print("Dashboard klucza już działa - otwieram jego kartę.")
        if "--no-open" not in sys.argv:
            webbrowser.open(url)
        return
    print("key manager: %s\n(Ctrl-C to stop)" % url, flush=True)
    if "--no-open" not in sys.argv:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
