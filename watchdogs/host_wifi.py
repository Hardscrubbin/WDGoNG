"""Drive a host Wi-Fi card (e.g. an AC1200 USB adapter) for the functions the
radio matrix routes to it: Wi-Fi scan / wardrive, WPA handshake capture, deauth.

It produces the SAME WiGLE 1.6 rows the firmware streams, fed to a callback the
backend wires to its existing wardrive ingest - so routing Wi-Fi to the host card
reaches the same loot path, dedup and GPS stamping as the ESP32. Runs as root.

Scan uses `iw dev <iface> scan` in managed mode (no monitor needed, non-intrusive).
Handshake and deauth need monitor mode (airodump-ng / aireplay-ng).
"""
import csv
import io
import os
import re
import shutil
import subprocess
import threading
import time

from watchdogs import wigle

_BSS_RE = re.compile(r"^BSS ([0-9a-fA-F:]{17})")


def have(tool: str) -> bool:
    return shutil.which(tool) is not None


def _freq_to_channel(mhz: int) -> int:
    if 2412 <= mhz <= 2472:
        return (mhz - 2407) // 5
    if mhz == 2484:
        return 14
    if 5000 <= mhz <= 5900:
        return (mhz - 5000) // 5
    if 5955 <= mhz <= 7115:                      # 6 GHz
        return (mhz - 5950) // 5
    return 0


def wigle_row(net: dict, first_seen: str) -> str:
    """One WiGLE row for a parsed scan entry, with the MEASURED frequency.

    wigle.row16() derives the Frequency column from the channel number, which is
    ambiguous across bands (6 GHz channel 1 = 5955 MHz, not 2412). When iw gave
    us the frequency, that value goes in; the channel-derived one is only the
    fallback when the frequency is unknown."""
    row = wigle.row16(net["bssid"], net["ssid"], net["auth"], first_seen,
                      net["channel"], net["rssi"], "", "", "", "", rtype="WIFI")
    freq = int(net.get("freq") or 0)
    if freq <= 0:
        return row
    cols = next(csv.reader(io.StringIO(row)))
    if len(cols) > 5:
        cols[5] = str(freq)
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerow(cols)
    return buf.getvalue()


def parse_iw_scan(text: str) -> list:
    """`iw dev <iface> scan` output -> [{bssid, ssid, freq, channel, rssi, auth}]."""
    nets, cur = [], None
    for raw in text.splitlines():
        m = _BSS_RE.match(raw)
        if m:
            if cur:
                nets.append(cur)
            cur = {"bssid": m.group(1).upper(), "ssid": "", "freq": 0, "channel": 0,
                   "rssi": -100, "wpa": False, "rsn": False, "sae": False, "privacy": False}
            continue
        if cur is None:
            continue
        s = raw.strip()
        if s.startswith("freq:"):
            try:
                cur["freq"] = int(float(s.split(":", 1)[1]))
                cur["channel"] = _freq_to_channel(cur["freq"])
            except ValueError:
                pass
        elif s.startswith("signal:"):
            mm = re.search(r"(-?\d+(?:\.\d+)?)\s*dBm", s)
            if mm:
                cur["rssi"] = int(float(mm.group(1)))
        elif s.startswith("SSID:"):
            cur["ssid"] = s[5:].strip()
        elif s.startswith("DS Parameter set: channel"):
            try:
                cur["channel"] = int(s.rsplit("channel", 1)[1])
            except ValueError:
                pass
        elif s.startswith("RSN:"):
            cur["rsn"] = True
        elif s.startswith("WPA:"):
            cur["wpa"] = True
        elif "Authentication suites" in s and "SAE" in s:
            cur["sae"] = True
        elif s.startswith("capability:") and "Privacy" in s:
            cur["privacy"] = True
    if cur:
        nets.append(cur)
    for n in nets:
        n["auth"] = ("WPA3" if n["sae"] else "WPA2" if n["rsn"] else "WPA" if n["wpa"]
                     else "WEP" if n["privacy"] else "OPEN")
    return nets


class HostWifi:
    """Scan/wardrive on a host card. on_row(wigle_row_str, is_bt) is called per network;
    msg(text, level) logs to the game console. All receive-only for scan/wardrive."""

    def __init__(self, iface: str, on_row=None, msg=None):
        self.iface = iface
        self._on_row = on_row or (lambda row, bt: None)
        self._msg = msg or (lambda t, l="raw": None)
        self._stop = threading.Event()
        self._thread = None
        self._mode = ""
        self._monitor = False

    # ---- interface mode -----------------------------------------------
    def _set_mode(self, mode: str) -> bool:
        try:
            subprocess.run(["ip", "link", "set", self.iface, "down"], check=True, timeout=5)
            subprocess.run(["iw", "dev", self.iface, "set", "type", mode], check=True, timeout=5)
            subprocess.run(["ip", "link", "set", self.iface, "up"], check=True, timeout=5)
            return True
        except (OSError, subprocess.SubprocessError) as e:
            self._msg("[host-wifi] %s -> %s failed: %s" % (self.iface, mode, e), "err")
            return False

    def _current_type(self) -> str:
        out = ""
        try:
            out = subprocess.run(["iw", "dev", self.iface, "info"], capture_output=True,
                                 text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            pass
        m = re.search(r"type (\w+)", out)
        return m.group(1) if m else ""

    # ---- scan / wardrive ----------------------------------------------
    def _one_scan(self) -> int:
        try:
            r = subprocess.run(["iw", "dev", self.iface, "scan"], capture_output=True,
                               text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return 0
        except OSError as e:
            self._msg("[host-wifi] iw scan failed: %s" % e, "err")
            return 0
        if r.returncode != 0:
            # a card mid-monitor or busy: report once, keep looping
            return 0
        now = str(int(time.time()))
        n = 0
        for net in parse_iw_scan(r.stdout):
            row = wigle_row(net, now)
            self._on_row(row.rstrip("\n"), False)
            n += 1
        return n

    def _scan_loop(self):
        if self._current_type() == "monitor":
            self._set_mode("managed")                # scan needs managed mode
        self._msg("[host-wifi] scanning on %s" % self.iface, "tx")
        while not self._stop.is_set():
            self._one_scan()
            self._stop.wait(4.0)                     # iw scan itself takes a few seconds

    def start_scan(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._mode = "scan"
        self._thread = threading.Thread(target=self._scan_loop, daemon=True)
        self._thread.start()

    # ---- handshake capture (monitor) ----------------------------------
    def start_handshake(self, loot_dir: str):
        """airodump-ng on the card, saving a capture whose handshakes the loot
        manager picks up from the session folder, like the firmware's pcap stream."""
        if not have("airodump-ng"):
            self._msg("[host-wifi] airodump-ng not installed", "err")
            return False
        if self._current_type() != "monitor" and not self._set_mode("monitor"):
            return False
        self._monitor = True
        prefix = os.path.join(loot_dir or "/tmp", "handshakes",
                              "host_%s" % time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(os.path.dirname(prefix), exist_ok=True)
        self._stop.clear()
        self._mode = "handshake"
        try:
            self._proc = subprocess.Popen(
                ["airodump-ng", self.iface, "-w", prefix, "--output-format", "pcap"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._msg("[host-wifi] airodump failed: %s" % e, "err")
            return False
        self._msg("[host-wifi] capturing handshakes on %s" % self.iface, "tx")
        return True

    # ---- deauth (monitor) ---------------------------------------------
    def deauth(self, bssid: str, channel: str = "", count: int = 10) -> bool:
        if not have("aireplay-ng"):
            self._msg("[host-wifi] aireplay-ng not installed", "err")
            return False
        if self._current_type() != "monitor" and not self._set_mode("monitor"):
            return False
        self._monitor = True
        if channel:
            try:
                subprocess.run(["iw", "dev", self.iface, "set", "channel", str(channel)], timeout=5)
            except (OSError, subprocess.SubprocessError):
                pass
        try:
            subprocess.Popen(["aireplay-ng", "--deauth", str(count), "-a", bssid, self.iface],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            self._msg("[host-wifi] deauth failed: %s" % e, "err")
            return False
        self._msg("[host-wifi] deauth %s on %s" % (bssid, self.iface), "tx")
        return True

    # ---- lifecycle ----------------------------------------------------
    def stop(self):
        self._stop.set()
        proc = getattr(self, "_proc", None)
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            self._proc = None
        if self._monitor:
            self._set_mode("managed")                # leave the card usable for normal Wi-Fi
            self._monitor = False
        self._mode = ""

    @property
    def active(self) -> bool:
        return bool(self._mode)
