"""Advertise a WDGWars identity via the BlueZ D-Bus LEAdvertisingManager1 API.

btmgmt add-adv proved unusable on the uConsole UART controller (never registered an
instance). BlueZ owns the controller, advertises AND scans at once, and is driven over
the same system bus bleak already uses -- so this is the reliable path and ends the
time-division hack.

Standalone test:
    sudo .venv/bin/python3 -m watchdogs.proximity.dbus_adv --secret REDACTED-SECRET --seconds 60
Re-registers the advertisement every rotation slot so the LocalName stays current.
"""
import argparse
import asyncio
import time

from dbus_fast import BusType, Variant
from dbus_fast.aio import MessageBus
from dbus_fast.service import ServiceInterface, dbus_property, method
from dbus_fast.constants import PropertyAccess

from . import prox, prox_ble

_ADV_PATH = "/wdgwars/adv0"
_COMPANY = int.from_bytes(prox_ble._COMPANY, "little")


class _Advert(ServiceInterface):
    def __init__(self, name, mfg_bytes):
        super().__init__("org.bluez.LEAdvertisement1")
        self._name = name
        self._mfg = bytes(mfg_bytes)

    @dbus_property(access=PropertyAccess.READ)
    def Type(self) -> "s":
        return "peripheral"

    @dbus_property(access=PropertyAccess.READ)
    def LocalName(self) -> "s":
        return self._name

    @dbus_property(access=PropertyAccess.READ)
    def ManufacturerData(self) -> "a{qv}":
        return {_COMPANY: Variant("ay", self._mfg)}

    @dbus_property(access=PropertyAccess.READ)
    def Includes(self) -> "as":
        return []

    @dbus_property(access=PropertyAccess.READ)
    def Discoverable(self) -> "b":
        return True

    @method()
    def Release(self):
        pass


async def run(secret, seconds, adapter="hci0", log=print):
    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    obj_path = "/org/bluez/" + adapter
    introspect = await bus.introspect("org.bluez", obj_path)
    mgr = bus.get_proxy_object("org.bluez", obj_path, introspect).get_interface(
        "org.bluez.LEAdvertisingManager1")

    cur_slot = None
    registered = False
    started = time.time()
    try:
        while True:
            slot = prox.slot_of(int(time.time() * 1000))
            if slot != cur_slot:
                ident = prox.derive(secret, slot)
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
                adv = _Advert(name, prox_ble.fingerprint(ident))
                bus.export(_ADV_PATH, adv)
                await mgr.call_register_advertisement(_ADV_PATH, {})
                registered = True
                cur_slot = slot
                log("[%s] advertising %s (dbus/BlueZ)" % (
                    time.strftime("%H:%M:%S"), name))
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
    ap.add_argument("--secret", default="REDACTED-SECRET")
    ap.add_argument("--adapter", default="hci0")
    ap.add_argument("--seconds", type=int, default=0)
    a = ap.parse_args()
    asyncio.run(run(a.secret, a.seconds, a.adapter))


if __name__ == "__main__":
    main()
