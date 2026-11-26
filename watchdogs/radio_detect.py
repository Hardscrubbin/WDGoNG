"""What radios are on this machine: the ESP32-C5 (firmware over serial) and/or a
host Wi-Fi card that can do monitor mode (e.g. an AC1200 USB adapter, RTL88xx).

Pure detection, no side effects: reads `iw`, `lsusb` and the serial ports. The game
uses it in the boot splash to decide which backend drives Wi-Fi, and offers a choice
when both are present.
"""
import glob
import os
import re
import subprocess

# USB Wi-Fi chipsets that do monitor + injection on Linux (mac80211 drivers).
# Matched on lsusb "ID vvvv:pppp" and on the chipset name. AC1200 cards are
# usually RTL8812AU/8811AU; a few common friends are listed too.
_MONITOR_USB_IDS = {
    "0bda:8812", "0bda:881a", "0bda:8813", "2357:0101", "2357:0103", "0e66:0023",  # RTL8812AU
    "0bda:a811", "0bda:0811", "0bda:8811",                                          # RTL8811AU
    "148f:5572", "0bda:8187", "148f:3070", "0cf3:9271",                             # older monitor cards
    "0e8d:7961", "0846:9060",                                                       # MediaTek MT7921AU (AC1200/AX)
    "0e8d:7612", "0846:9053", "0e8d:7610", "148f:7601",                             # MT7612U, MT7610U, MT7601U
}
_MONITOR_NAME_RE = re.compile(r"rtl88(12|11|14)|8812au|8811au|awus036ac|mt79\d\d|mt76\d\d|mediatek", re.I)


def _run(argv, timeout=4):
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def esp32_present() -> str:
    """The ESP32 serial port if one is detected, else ''. Honours WDGO_PORT."""
    port = os.environ.get("WDGO_PORT")
    if port and os.path.exists(port):
        return port
    try:
        from watchdogs.serial_manager import detect_esp32_port
        return detect_esp32_port() or ""
    except Exception:
        return ""


def _iface_driver(iface: str) -> str:
    link = "/sys/class/net/%s/device/driver" % iface
    try:
        return os.path.basename(os.path.realpath(link))
    except OSError:
        return ""


def _iface_is_usb(iface: str) -> bool:
    """True when the interface's device sits on a USB bus (any chipset): the sysfs
    device path of a USB adapter runs through .../usbN/... ."""
    try:
        return "/usb" in os.path.realpath("/sys/class/net/%s/device" % iface)
    except OSError:
        return False


def _iface_phy(iface: str) -> str:
    try:
        return open("/sys/class/net/%s/phy80211/name" % iface).read().strip()
    except OSError:
        return ""


def _phy_monitor_capable(phy: str) -> bool:
    """True if `iw phy <phy> info` lists the 'monitor' interface mode."""
    out = _run(["iw", "phy", phy, "info"])
    if "* monitor" in out:
        return True
    # fall back: a chunk after "Supported interface modes" that contains monitor
    m = re.search(r"Supported interface modes:(.*?)(?:\n\s*\n|\Z)", out, re.S)
    return bool(m and "monitor" in m.group(1))


def _usb_monitor_ids() -> set:
    """IDs from lsusb that look like monitor-capable Wi-Fi adapters."""
    ids = set()
    for ln in _run(["lsusb"]).splitlines():
        m = re.search(r"ID ([0-9a-f]{4}:[0-9a-f]{4})\s+(.*)", ln, re.I)
        if not m:
            continue
        vid, name = m.group(1).lower(), m.group(2)
        if vid in _MONITOR_USB_IDS or _MONITOR_NAME_RE.search(name):
            ids.add(vid)
    return ids


def host_wifi_ifaces() -> list:
    """Monitor-capable host Wi-Fi interfaces: [{iface, driver, phy, usb, monitor, up}].
    `monitor` True = the phy advertises monitor mode (or it is already in it)."""
    out = []
    usb = _usb_monitor_ids()
    for path in sorted(glob.glob("/sys/class/net/*")):
        iface = os.path.basename(path)
        if iface == "lo" or not os.path.isdir(os.path.join(path, "wireless")) \
                and not os.path.exists(os.path.join(path, "phy80211")):
            continue
        driver = _iface_driver(iface)
        phy = _iface_phy(iface)
        # is it a USB adapter whose id we flagged?
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read().lower()
        except OSError:
            uevent = ""
        is_usb = _iface_is_usb(iface) or (
            bool(usb) and any(u.replace(":", "/") in uevent or u in uevent for u in usb))
        monitor = _phy_monitor_capable(phy) if phy else False
        try:
            up = "up" in open(os.path.join(path, "operstate")).read()
        except OSError:
            up = False
        # only surface cards that can actually do monitor - a plain built-in
        # managed-only adapter is not useful here
        if monitor:
            out.append({"iface": iface, "driver": driver, "phy": phy,
                        "usb": is_usb, "monitor": True, "up": up})
    return out


def detect() -> dict:
    """{'esp32': '<port>'|'', 'host_wifi': [iface dicts], 'both': bool}."""
    esp = esp32_present()
    host = host_wifi_ifaces()
    return {"esp32": esp, "host_wifi": host, "both": bool(esp and host)}


def summary(d=None) -> str:
    """One short line for the boot splash / logs."""
    d = d or detect()
    bits = []
    if d["esp32"]:
        bits.append("ESP32 " + os.path.basename(d["esp32"]))
    for h in d["host_wifi"]:
        bits.append("host-WiFi %s%s" % (h["iface"], "" if h["up"] else " (down)"))
    return "  ".join(bits) if bits else "no radio detected"


if __name__ == "__main__":
    import json
    print(json.dumps(detect(), indent=1))
    print(summary())
