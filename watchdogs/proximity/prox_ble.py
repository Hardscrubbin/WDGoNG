"""The BLE half of proximity: advertise who we are, listen for everyone else.

Kept apart from prox.py on purpose. prox.py holds the maths and must stay
importable on a desktop; this file imports `bluetooth` and can only run on the
badge. The page above talks to `Radio` and never learns which of the two
answered, so swapping the transport later costs one file.

Measured on firmware 3.4.0 before any of this was written:
  bluetooth / ubluetooth / aioble all present
  gap_advertise and gap_scan run AT THE SAME TIME -- 12 devices in a 5 s scan
That concurrency is what lets the hunt feel continuous instead of turn-based.

The advertisement is deliberately plain: flags, a complete local name of
WDG-XXXXXX, and a scrap of manufacturer data listing a couple of peers we have
heard lately. The name carries the rotating identity, which means any phone
with nRF Connect can impersonate a badge for testing without a line of code --
that is worth more than the few bytes a tighter encoding would save.
"""

PREFIX = "WDG-"

# Company id 0xFFFF is the one reserved for testing and internal use, which is
# exactly what this is. It must change before anything ships widely.
_COMPANY = b"\xff\xff"

_IRQ_SCAN_RESULT = 5
_IRQ_SCAN_DONE = 6

_ADV_NAME_SHORT = 0x08
_ADV_NAME_FULL = 0x09
_ADV_MFG = 0xFF
_ADV_SVC16 = 0x16                         # Service Data, 16-bit UUID

# WDGWars device-type tag: a Service Data AD (0x16) under a reserved 16-bit UUID,
# one ASCII char for the hardware kind. It rides ALONGSIDE the name + echoes, so a
# parser that reads only the name/echoes (older firmware) ignores it — no wire
# regression — while updated ones can show the device model.
WDG_SVC_UUID = b"\xf0\xff"                # 0xFFF0, little-endian
DEVTYPE_LABEL = {"Z": "WDGoNG Zero", "U": "uConsole", "M": "Marauder", "B": "Badge",
                 "A": "Android", "I": "iPhone"}

# How many peer fingerprints we advertise back. Each is two bytes: enough for
# the other side to recognise itself, small enough to fit beside the name in
# the 31 bytes a legacy advertisement gets.
ECHO_MAX = 3


def _adv_fields(payload, want):
    """Yield the value of every AD structure of type `want`."""
    out = []
    i = 0
    n = len(payload)
    while i + 1 < n:
        ln = payload[i]
        if ln == 0 or i + ln >= n + 1:
            break
        t = payload[i + 1]
        if t == want:
            out.append(payload[i + 2:i + 1 + ln])
        i += 1 + ln
    return out


def adv_name(payload):
    """The advertised local name, or "" -- this is where the identity lives."""
    for want in (_ADV_NAME_FULL, _ADV_NAME_SHORT):
        for v in _adv_fields(payload, want):
            try:
                return bytes(v).decode("utf-8")
            except Exception:
                continue
    return ""


def adv_echoes(payload):
    """Fingerprints this advertiser says it has heard lately."""
    out = []
    for v in _adv_fields(payload, _ADV_MFG):
        v = bytes(v)
        if len(v) < 2 or v[:2] != _COMPANY:
            continue
        body = v[2:]
        for i in range(0, len(body) - 1, 2):
            out.append(body[i:i + 2])
    return out


def adv_devtype(payload):
    """The advertised WDGWars device-type char ('Z'/'U'/'M'/'B'), or '' if none."""
    for v in _adv_fields(payload, _ADV_SVC16):
        v = bytes(v)
        if len(v) >= 3 and v[:2] == WDG_SVC_UUID:
            try:
                return v[2:].decode("ascii", "ignore")[:1]
            except Exception:
                return ""
    return ""


# 128-bit form of the 0xFFF0 service UUID, the key BlueZ/bleak use in an
# AdvertisementData.service_data dict.
WDG_SVC_UUID128 = "0000fff0-0000-1000-8000-00805f9b34fb"


def adv_ident(payload):
    """Rotating ident carried in Service Data 0xFFF0 as `<type><3 raw ident bytes>`.

    A phone cannot set an arbitrary BLE Local Name (Android takes the adapter's
    name), so instead of WDG-<ident> it advertises its ident here: the same 3 raw
    bytes as SHA256(secret|wdgprox|slot)[:3], behind a 1-byte device-type ('A').
    Returns the 6-hex-UPPER ident, or "" when the block is just the 1-byte
    device-type tag (Z/U/M/B) or absent. The WDG- name wins when both are present.
    """
    for v in _adv_fields(payload, _ADV_SVC16):
        v = bytes(v)
        if len(v) >= 6 and v[:2] == WDG_SVC_UUID:    # uuid(2) + type(1) + ident(3)
            return v[3:6].hex().upper()
    return ""


def ident_from_service_data(sd):
    """adv_ident for a BlueZ/bleak service_data dict {uuid: value}. The value has
    NO UUID prefix: it is `<type><3 raw ident bytes>` (>=4 B for a phone ident)."""
    if not sd:
        return ""
    v = sd.get(WDG_SVC_UUID128) or sd.get(WDG_SVC_UUID128.upper())
    if v is None:
        return ""
    v = bytes(v)
    return v[1:4].hex().upper() if len(v) >= 4 else ""


def devtype_from_service_data(sd):
    """The device-type char ('Z'/'U'/'M'/'B'/'A'/'I') from a BlueZ/bleak
    service_data dict, or '' when the 0xFFF0 block is absent."""
    if not sd:
        return ""
    v = sd.get(WDG_SVC_UUID128) or sd.get(WDG_SVC_UUID128.upper())
    if not v:
        return ""
    try:
        return bytes(v)[:1].decode("ascii", "ignore")
    except Exception:
        return ""


def fingerprint(ident):
    """Two bytes of an identity, for echoing back inside an advertisement.

    Truncating invites collisions, and they are tolerable here: this only feeds
    the "they can see me too" hint. A false positive costs a MUTUAL marker that
    should not be there; it cannot fabricate a meeting on its own, because a
    meeting also needs a band and fifteen seconds of it.
    """
    if not ident:
        return b"\x00\x00"
    try:
        return bytes([int(ident[0:2], 16), int(ident[2:4], 16)])
    except Exception:
        return b"\x00\x00"


def build_payload(name, echoes=(), devtype=""):
    """Flags + complete local name + optional device-type tag + up to ECHO_MAX echoes."""
    nb = name.encode("utf-8")[:20]
    out = bytearray(b"\x02\x01\x06")
    out += bytes([len(nb) + 1, _ADV_NAME_FULL]) + nb
    if devtype:                              # device-type as service data (old parsers ignore AD 0x16)
        body = WDG_SVC_UUID + devtype.encode("ascii", "ignore")[:1]
        out += bytes([len(body) + 1, _ADV_SVC16]) + body
    ech = b"".join(echoes[:ECHO_MAX])
    if ech:
        body = _COMPANY + ech
        out += bytes([len(body) + 1, _ADV_MFG]) + body
    return bytes(out)


class Radio:
    """Advertise and scan at once. Everything above this talks to these five."""

    def __init__(self):
        self.ble = None
        self.err = ""
        self.seen = []          # (ident, rssi, echoes) drained by the page
        self.scanning = False
        self._name = ""

    # -- lifecycle --------------------------------------------------------
    def available(self):
        # __import__ rather than a plain import: this is a presence probe, not
        # a use, and an unused import is exactly the thing the linter should go
        # on flagging everywhere else.
        try:
            __import__("bluetooth")
            return True
        except Exception:
            return False

    def start(self):
        if self.ble is not None:
            return True
        try:
            import bluetooth
            self.ble = bluetooth.BLE()
            self.ble.active(True)
            self.ble.irq(self._irq)
            return True
        except Exception as e:
            self.err = ("ble: " + str(e))[:40]
            self.ble = None
            return False

    def stop(self):
        """Drop the radio. It is the biggest draw on the board -- do not leave
        it up because the page might come back."""
        if self.ble is None:
            return
        try:
            self.ble.gap_scan(None)
        except Exception:
            pass
        try:
            self.ble.gap_advertise(None)
        except Exception:
            pass
        try:
            self.ble.active(False)
        except Exception:
            pass
        self.ble = None
        self.scanning = False

    # -- advertising ------------------------------------------------------
    def advertise(self, ident, echoes=()):
        """(Re)advertise under `ident`. Cheap to call on every rotation."""
        if self.ble is None:
            return False
        name = PREFIX + ident
        try:
            self.ble.gap_advertise(None)
            self.ble.gap_advertise(
                250_000, adv_data=build_payload(name, echoes),
                connectable=False)
            self._name = name
            return True
        except Exception as e:
            self.err = ("adv: " + str(e))[:40]
            return False

    # -- scanning ---------------------------------------------------------
    def scan(self, duration_ms=0):
        """Start scanning. 0 means keep going until told to stop."""
        if self.ble is None:
            return False
        try:
            # Window equal to interval is a continuous listen. It costs more
            # than duty-cycling, but a hunt is a handful of minutes and missing
            # advertisements is what makes a trend jump about.
            self.ble.gap_scan(duration_ms, 30_000, 30_000, False)
            self.scanning = True
            return True
        except Exception as e:
            self.err = ("scan: " + str(e))[:40]
            return False

    def _irq(self, event, data):
        if event == _IRQ_SCAN_RESULT:
            _at, _addr, _adt, rssi, adv = data
            try:
                payload = bytes(adv)
            except Exception:
                return
            name = adv_name(payload)
            if name.startswith(PREFIX):
                ident = name[len(PREFIX):].upper()
            else:
                ident = adv_ident(payload)      # phone path: ident in Service Data 0xFFF0
            if not ident:
                return
            # The IRQ must return fast, so nothing is decided here: the page
            # drains this list on its own schedule.
            if len(self.seen) < 64:
                self.seen.append((ident, rssi, adv_echoes(payload)))
        elif event == _IRQ_SCAN_DONE:
            self.scanning = False

    def drain(self):
        out, self.seen = self.seen, []
        return out
