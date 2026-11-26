"""Advertise a WDGWars identity via the BlueZ D-Bus LEAdvertisingManager1 API.

btmgmt add-adv proved unusable on the uConsole UART controller (never registered an
instance). BlueZ owns the controller, advertises AND scans at once, and is driven over
the same system bus bleak already uses -- so this is the reliable path and ends the
time-division hack.

Standalone test:
    sudo .venv/bin/python3 -m watchdogs.proximity.dbus_adv --secret peer-jeden --seconds 60
Re-registers the advertisement every rotation slot so the LocalName stays current,
and whenever the set of echoed peers changes.

Echoes: the manufacturer data carries the fingerprints of peers WE HEAR (that is
how the other side learns it is heard and reaches MUTUAL). This process does not
scan; the scanner (peer.py --scan-only) publishes them in its snapshot's "echo"
list, which --echo-file points at. Our own fingerprint is never echoed.

The game passes the secret on stdin (--secret-stdin), never in argv.
"""
import argparse
import asyncio
import json
import os
import sys
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method
from dbus_fast.constants import PropertyAccess

from . import prox, prox_ble

_ADV_PATH = "/wdgwars/adv0"
_COMPANY = int.from_bytes(prox_ble._COMPANY, "little")
ECHO_STALE_S = 20.0       # an echo file older than this is ignored (scanner down)


def read_echoes(path, now=None):
    """Heard-peer fingerprints from the scanner snapshot -> tuple of 2-byte values.
    Anything malformed or stale yields () -- never an error."""
    if not path:
        return ()
    now = time.time() if now is None else now
    try:
        if now - os.stat(path).st_mtime > ECHO_STALE_S:
            return ()
        with open(path) as f:
            data = json.load(f)
        out = []
        for h in (data.get("echo") or [])[:prox_ble.ECHO_MAX]:
            if isinstance(h, str) and len(h) == 4:
                b = bytes.fromhex(h)
                if b not in out:
                    out.append(b)
        return tuple(sorted(out))      # order-free: a reshuffle is not a change
    except Exception:
        return ()


class _Advert(ServiceInterface):
    def __init__(self, name, mfg_bytes, devtype="", ident=""):
        super().__init__("org.bluez.LEAdvertisement1")
        self._name = name
        self._mfg = bytes(mfg_bytes)
        self._devtype = devtype
        # BlueZ puts LocalName only in the scan response, which a passive scanner
        # never pairs with the ADV_IND, so the ident must ride in Service Data too
        # (same `<type><3 raw ident bytes>` layout a phone uses). 6 hex -> 3 bytes.
        try:
            self._ident = bytes.fromhex(ident)[:3] if ident else b""
        except ValueError:
            self._ident = b""

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self._name

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        if not self._mfg:
            return {}
        return {_COMPANY: Variant("ay", self._mfg)}

    @dbus_property(access=PropertyAccess.READ)
    def ServiceData(self) -> "a{sv}":
        # WDGWars device-type tag (0xFFF0). On-air this is the same Service Data AD
        # the raw-HCI side emits; old parsers ignore it, no wire regression.
        if not self._devtype:
            return {}
        body = self._devtype.encode("ascii", "ignore")[:1] + self._ident
        return {"0000fff0-0000-1000-8000-00805f9b34fb": Variant("ay", body)}

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @method()
    def Release(self):
        pass


async def run(secret, seconds, adapter="hci0", log=print, devtype="U", echo_file=""):
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    obj_path = "/org/bluez/" + adapter
    introspect = await bus.introspect("org.bluez", obj_path)
    mgr = bus.get_proxy_object("org.bluez", obj_path, introspect).get_interface(
        "org.bluez.LEAdvertisingManager1")

    cur_slot = None
    cur_echoes = None
    registered = False
    started = time.time()
    try:
        while True:
            slot = prox.slot_of(int(time.time() * 1000))
            ident = prox.derive(secret, slot)
            echoes = tuple(e for e in read_echoes(echo_file)
                           if e != prox_ble.fingerprint(ident))
            if slot != cur_slot or echoes != cur_echoes:
                name = prox_ble.PREFIX + ident
                if registered:
                    try:
                        await mgr.call_unregister_advertisement(_ADV_PATH)
                    except Exception:
                        pass
                    try:
                        bus.unexport(_ADV_PATH)
                    except Exception:
                        pass
                    registered = False
                adv = _Advert(name, b"".join(echoes), devtype, ident)
                bus.export(_ADV_PATH, adv)
                try:
                    await mgr.call_register_advertisement(_ADV_PATH, {})
                except Exception as exc:
                    # transient BlueZ refusal: retry on the next pass, don't die
                    try:
                        bus.unexport(_ADV_PATH)
                    except Exception:
                        pass
                    log("[%s] register failed: %s" % (time.strftime("%H:%M:%S"), exc))
                    cur_slot = cur_echoes = None
                    await asyncio.sleep(2)
                    continue
                registered = True
                if slot != cur_slot:
                    log("[%s] advertising %s (dbus/BlueZ)" % (
                        time.strftime("%H:%M:%S"), name))
                cur_slot = slot
                cur_echoes = echoes
            if seconds and (time.time() - started) >= seconds:
                break
            await asyncio.sleep(2)
    finally:
        if registered:
            try:
                await mgr.call_unregister_advertisement(_ADV_PATH)
            except Exception:
                pass
        log("advert released")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secret", default="peer-jeden",
                    help="manual testing only; the game uses --secret-stdin")
    ap.add_argument("--secret-stdin", action="store_true", dest="secret_stdin",
                    help="read the secret from the first line of stdin (keeps it out of argv)")
    ap.add_argument("--echo-file", default="", dest="echo_file",
                    help="scanner snapshot JSON whose 'echo' list we advertise")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--seconds", type=int, default=0)
    ap.add_argument("--devtype", default="U")
    a = ap.parse_args()
    secret = a.secret
    if a.secret_stdin:
        secret = (sys.stdin.readline() or "").strip()
        if not secret:
            ap.error("--secret-stdin: empty secret")
    asyncio.run(run(secret, a.seconds, a.adapter, devtype=a.devtype,
                    echo_file=a.echo_file))


if __name__ == "__main__":
    main()
