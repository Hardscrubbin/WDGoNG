#!/usr/bin/env python3
"""Host side of the WDGo hub signing dongle (needs pyserial).

    hub_dongle.py info                  device state + public key
    hub_dongle.py pubkey                the allowed_signers line for the game
    hub_dongle.py wifi-sync [file]      load networks from dongle/wifi.conf (SSID:password per line)
    hub_dongle.py hub <base-url>        where the dongle looks for index.json / pending/index.json
    hub_dongle.py check                 make the dongle fetch the hub and report what is pending
    hub_dongle.py sign <file>           ask the dongle to sign <file> -> <file>.sig (press its button)

The dongle signs only the hub namespace and only a digest that matches the pending
catalogue it downloaded itself. This tool merely carries bytes: it wraps the (r, s)
the dongle returns into a standard OpenSSH signature, which `ssh-keygen -Y verify`
(and therefore the game) checks like any other.
"""
import base64
import glob
import hashlib
import os
import struct
import subprocess
import sys
import tempfile
import time

NAMESPACE = "wdgo-extras-hub"
IDENTITY = "wdgo-extras-hub"
KEYTYPE = b"ecdsa-sha2-nistp256"


def _s(b: bytes) -> bytes:
    return struct.pack(">I", len(b)) + b


def _mpint(b: bytes) -> bytes:
    b = b.lstrip(b"\x00") or b"\x00"
    if b[0] & 0x80:
        b = b"\x00" + b
    return _s(b)


def pubkey_blob(q: bytes) -> bytes:
    return _s(KEYTYPE) + _s(b"nistp256") + _s(q)


def pubkey_line(q: bytes) -> str:
    return "%s %s" % (KEYTYPE.decode(), base64.b64encode(pubkey_blob(q)).decode())


def sshsig(q: bytes, r: bytes, s: bytes) -> str:
    sig = _s(KEYTYPE) + _s(_mpint(r) + _mpint(s))
    blob = (b"SSHSIG" + struct.pack(">I", 1) + _s(pubkey_blob(q)) + _s(NAMESPACE.encode())
            + _s(b"") + _s(b"sha512") + _s(sig))
    b64 = base64.b64encode(blob).decode()
    lines = [b64[i:i + 70] for i in range(0, len(b64), 70)]
    return "-----BEGIN SSH SIGNATURE-----\n%s\n-----END SSH SIGNATURE-----\n" % "\n".join(lines)


def _open_port(port):
    import serial
    ser = serial.Serial()
    ser.port, ser.baudrate, ser.timeout = port, 115200, 0.2
    ser.exclusive = True        # one program per port: a busy port fails fast instead of mixing replies
    ser.open()
    # The OS raises DTR+RTS on open (idle). Dropping DTR first would pass through
    # DTR=0/RTS=1, which is the chip's reset line - so release RTS first, then DTR.
    ser.rts = False
    ser.dtr = False
    time.sleep(0.3)
    ser.reset_input_buffer()
    return ser


def _is_lsp_key(ser, timeout=2.0) -> bool:
    """True when the device on `ser` answers `info` the way the LSP KEY firmware does.
    Other boards (e.g. a cluster bridge on the same Mac) are left alone: they only see
    one unknown `info` line."""
    ser.write(b"info\n")
    end, buf = time.time() + timeout, b""
    while time.time() < end:
        buf += ser.read(256)
        while b"\n" in buf:
            ln, buf = buf.split(b"\n", 1)
            ln = ln.strip().decode("utf-8", "replace")
            if ln.startswith("@OK info ") and " pub=" in ln:
                return True
    return False


def find_dongle():
    """(port, open serial) of the first device that answers as an LSP KEY, else (None, None).
    Busy ports (e.g. held by the dashboard) are skipped."""
    for port in sorted(glob.glob("/dev/cu.usbmodem*") + glob.glob("/dev/ttyACM*")):
        try:
            ser = _open_port(port)
        except Exception:
            continue
        try:
            if _is_lsp_key(ser):
                ser.reset_input_buffer()
                return port, ser
        except Exception:
            pass
        ser.close()
    return None, None


class Dongle:
    def __init__(self, port=None):
        port = port or os.environ.get("HUB_DONGLE_PORT")
        if port:
            self.port, self.ser = port, _open_port(port)
            return
        self.port, self.ser = find_dongle()
        if not self.ser:
            raise SystemExit("no LSP KEY found on any free serial port (plug it in, close the "
                             "LSP KEY dashboard if it is running, or set HUB_DONGLE_PORT)")

    def cmd(self, line, timeout=20.0) -> str:
        self.ser.write((line + "\n").encode())
        end, buf = time.time() + timeout, b""
        while time.time() < end:
            buf += self.ser.read(256)
            while b"\n" in buf:
                ln, buf = buf.split(b"\n", 1)
                ln = ln.strip().decode("utf-8", "replace")
                if ln.startswith("@"):
                    return ln[1:]
        raise SystemExit("dongle did not answer '%s'" % line.split()[0])

    def ok(self, line, timeout=20.0) -> str:
        r = self.cmd(line, timeout)
        if not r.startswith("OK"):
            raise SystemExit("dongle: " + r)
        return r

    def info(self) -> dict:
        r = self.ok("info")
        return dict(kv.split("=", 1) for kv in r.split()[2:] if "=" in kv)


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        sys.exit(2)
    d = Dongle()
    if a[0] == "info":
        i = d.info()
        unh = lambda v: "-" if v == "-" else bytes.fromhex(v).decode("utf-8", "replace")
        print("%s   firmware %s   key mode: %s%s" % (unh(i["name"]), i["fw"], i["mode"],
              "   (DEV: key is in flash, NOT protected)" if i["mode"] == "DEV" else ""))
        print("wifi: %s   stored networks: %s   hub: %s   signs: %s"
              % (unh(i["wifi"]), i["nets"], i["hub"], i["ns"]))
        print("public key: %s" % pubkey_line(bytes.fromhex(i["pub"])))
    elif a[0] == "pubkey":
        print("%s %s" % (IDENTITY, pubkey_line(bytes.fromhex(d.info()["pub"]))))
    elif a[0] == "wifi-sync":
        path = a[1] if len(a) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                     "dongle", "wifi.conf")
        nets = []
        for ln in open(path, encoding="utf-8"):
            ln = ln.rstrip("\n")
            if ln.strip() and not ln.lstrip().startswith("#") and ":" in ln:
                nets.append(ln.split(":", 1))
        d.ok("wifi clear")
        for ssid, pw in nets:
            d.ok("wifi add %s %s" % (ssid.encode().hex(), pw.encode().hex() or "00"[:0]))
        print("loaded %d network(s): %s" % (len(nets), ", ".join(s for s, _ in nets)))
    elif a[0] == "hub" and len(a) == 2:
        print(d.ok("hub " + a[1]))
    elif a[0] == "check":
        print(d.cmd("check", timeout=60))
    elif a[0] == "sign" and len(a) == 2:
        data = open(a[1], "rb").read()
        digest = hashlib.sha512(data).hexdigest()
        print("digest %s...  -> confirm on the dongle (button)" % digest[:16])
        r = d.ok("sign " + digest, timeout=100).split()
        q = bytes.fromhex(d.info()["pub"])
        out = a[1] + ".sig"
        open(out, "w").write(sshsig(q, bytes.fromhex(r[2]), bytes.fromhex(r[3])))
        with tempfile.NamedTemporaryFile("w", suffix=".signers") as tf:
            tf.write("%s %s\n" % (IDENTITY, pubkey_line(q)))
            tf.flush()
            v = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", tf.name, "-I", IDENTITY,
                                "-n", NAMESPACE, "-s", out], input=data, capture_output=True)
        if v.returncode != 0:
            os.unlink(out)
            raise SystemExit("signature did NOT verify with ssh-keygen: %s" % v.stderr.decode().strip())
        print("signed (%s by the dongle), verified with ssh-keygen -> %s" % (r[4], out))
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
