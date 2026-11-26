"""PipBoy Watch BLE Manager — connect, pair, command, receive events.

Uses bleak for BLE GATT + dbus-python Agent1 for PIN pairing.
Thread + queue pattern (same as serial_manager / sdr_manager).

Protocol: JSON lines over Nordic UART Service (NUS), \\n terminated.
"""

import asyncio
import json
import logging
import os
import threading
import time
from queue import Queue
from typing import Callable, Optional

log = logging.getLogger(__name__)

# Nordic UART Service UUIDs
NUS_SERVICE = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"  # game → watch (write)
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"  # watch → game (notify)

SCAN_TIMEOUT = 10.0
CONNECT_TIMEOUT = 15.0
# Longest NUS JSON line we buffer (an NFC file dump is the biggest message).
# A watch that keeps sending without a newline must not grow memory forever.
NUS_MAX_LINE = 256 * 1024


class WatchManager:
    """Manages BLE connection to PipBoy watch."""

    def __init__(self):
        self.connected = False
        self.paired = False
        self.scanning = False
        self.device_name: str = ""
        self.device_address: str = ""

        # Event queue for app.py (type, data)
        self._events: Queue = Queue()
        # Command TX queue
        self._tx_queue: Queue = Queue()

        # RX buffer for chunked NUS messages
        self._rx_buffer = ""
        self._rx_discard = False   # dropping an over-long line up to its newline

        # State
        self._client = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._scan_results: list = []

        # PIN pairing
        self._pin_requested = False
        self._pin_value: Optional[int] = None
        self._pin_event = threading.Event()
        # The BlueZ agent only answers for THIS device while a connect attempt
        # is pairing; it is stopped (and unregistered) when that attempt ends.
        self._agent_target: str = ""          # D-Bus object-path suffix dev_XX_..
        self._agent_stop = threading.Event()
        self._agent_stop.set()

        # Watch state cache
        self.battery: int = 0
        self.version: str = ""
        self.features: list = []

        # Callbacks
        self.on_nfc_tag: Optional[Callable] = None
        self.on_lora_msg: Optional[Callable] = None

    def check_existing(self) -> Optional[str]:
        """Check if a PipBoy is already known/connected in BlueZ."""
        try:
            import subprocess
            # List all known devices (not just paired)
            result = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True, text=True, timeout=5)
            for line in result.stdout.splitlines():
                if "PipBoy" not in line:
                    continue
                parts = line.split()
                if len(parts) < 2:
                    continue
                addr = parts[1]
                name = " ".join(parts[2:])
                # Check if actually connected
                info = subprocess.run(
                    ["bluetoothctl", "info", addr],
                    capture_output=True, text=True, timeout=5)
                connected = "Connected: yes" in info.stdout
                if connected:
                    self._events.put(("log",
                        f"[Watch] Found connected: {name} [{addr}]"))
                    return addr
                else:
                    self._events.put(("log",
                        f"[Watch] Found known (not connected): {name} [{addr}]"))
        except Exception:
            pass
        return None

    def scan(self) -> None:
        """Start scanning for PipBoy devices."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._scan_results.clear()

        # Check if already known in BlueZ — reconnect
        existing = self.check_existing()
        if existing:
            # Disconnect stale BlueZ connection first (previous game session)
            try:
                import subprocess
                subprocess.run(
                    ["bluetoothctl", "disconnect", existing],
                    capture_output=True, timeout=5)
                self._events.put(("log",
                    f"[Watch] Cleared stale connection, reconnecting..."))
                time.sleep(3)  # wait for watch to readvertise
            except Exception:
                pass
            self.connect(existing)
            return

        self.scanning = True
        self._thread = threading.Thread(
            target=self._run_scan, daemon=True)
        self._thread.start()

    def connect(self, address: str) -> None:
        """Connect to a specific PipBoy device."""
        if self.connected:
            return
        self._stop_event.clear()
        self.device_address = address
        self._thread = threading.Thread(
            target=self._run_connect, args=(address,), daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        """Disconnect from watch."""
        self._stop_event.set()
        self.connected = False
        self.device_name = ""

    def forget(self) -> None:
        """Disconnect and remove BlueZ bonding for the watch."""
        address = self.device_address
        self.disconnect()
        if not address:
            return
        # Remove bonding via bluetoothctl
        try:
            import subprocess
            subprocess.run(
                ["bluetoothctl", "remove", address],
                capture_output=True, timeout=5)
            self._events.put(("status", f"Removed bonding for {address}"))
        except Exception as e:
            self._events.put(("error", f"Forget failed: {e}"))
        self.device_address = ""

    def provide_pin(self, pin: int) -> None:
        """Provide the 6-digit PIN shown on the watch."""
        try:
            pin = int(pin)
        except (TypeError, ValueError):
            return
        if not 0 <= pin <= 999999:
            return
        self._pin_value = pin
        self._pin_event.set()

    def send_command(self, cmd: str, params: dict = None) -> None:
        """Queue a JSON command for the watch."""
        if not self.connected:
            self._events.put(("error", "Not connected to watch"))
            return
        msg = {"cmd": cmd}
        if params:
            msg["params"] = params
        self._tx_queue.put(json.dumps(msg) + "\n")

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        return events

    @property
    def scan_results(self) -> list:
        return list(self._scan_results)

    @property
    def pin_requested(self) -> bool:
        return self._pin_requested

    # ------------------------------------------------------------------
    # BLE scan
    # ------------------------------------------------------------------
    def _run_scan(self):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._async_scan())
        except Exception as e:
            self._events.put(("error", f"Scan failed: {e}"))
        finally:
            self.scanning = False

    async def _async_scan(self):
        try:
            from bleak import BleakScanner
        except ImportError:
            self._events.put(("error", "bleak not installed"))
            return

        self._events.put(("status", "Scanning for PipBoy (10s)..."))
        try:
            devices = await BleakScanner.discover(
                timeout=SCAN_TIMEOUT, return_adv=True)
        except Exception as e:
            self._events.put(("error", f"BLE scan error: {e}"))
            return

        total = len(devices)
        self._events.put(("log", f"[Watch] BLE scan: {total} devices total"))

        for dev, adv in devices.values():
            name = dev.name or adv.local_name or ""
            # Log all named devices for diagnostics
            if name:
                self._events.put(("log",
                    f"[Watch]   {name} [{dev.address}] "
                    f"RSSI:{adv.rssi}"))
            if name.startswith("PipBoy"):
                self._scan_results.append({
                    "name": name,
                    "address": dev.address,
                    "rssi": adv.rssi,
                })
                self._events.put(("device", {
                    "name": name, "address": dev.address,
                    "rssi": adv.rssi}))

        if not self._scan_results:
            self._events.put(("status",
                f"No PipBoy found ({total} other devices seen)"))
        else:
            n = len(self._scan_results)
            self._events.put(("status", f"Found {n} PipBoy device(s)"))

    # ------------------------------------------------------------------
    # BLE connect + pair + NUS
    # ------------------------------------------------------------------
    def _run_connect(self, address: str):
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            loop.run_until_complete(self._async_connect(address))
        except Exception as e:
            self._events.put(("error", f"Connection failed: {e}"))
            self.connected = False
        finally:
            self._loop = None

    async def _async_connect(self, address: str):
        try:
            from bleak import BleakClient, BleakScanner
        except ImportError:
            self._events.put(("error", "bleak not installed"))
            return

        # Register D-Bus agent for PIN pairing (in background thread).
        # Watch firmware ≥ v0.4 requires MITM-authenticated encryption on
        # the NUS characteristics: the first NUS write/notify-subscribe
        # returns Insufficient Authentication (0x05) which BlueZ resolves
        # by calling our agent's RequestPasskey. Agent needs to be up
        # before pairing is triggered.
        self._agent_target = "dev_" + address.upper().replace(":", "_")
        self._agent_stop.clear()
        agent_thread = threading.Thread(
            target=self._run_dbus_agent, daemon=True)
        agent_thread.start()
        try:
            await asyncio.sleep(0.5)  # let agent register
            await self._connect_session(address, BleakClient, BleakScanner)
        finally:
            # Every attempt ends the agent: found or not, paired or not.
            self._end_agent()
            await asyncio.to_thread(agent_thread.join, 3.0)

    def _end_agent(self) -> None:
        self._agent_stop.set()
        self._agent_target = ""
        self._pin_requested = False
        self._pin_event.set()           # release a RequestPasskey still waiting

    async def _connect_session(self, address, BleakClient, BleakScanner):
        # Discover device first (populates D-Bus cache)
        self._events.put(("status", f"Scanning for {address}..."))
        dev = await BleakScanner.find_device_by_address(address, timeout=10)
        if not dev:
            self._events.put(("error", f"Device {address} not found in scan"))
            return

        # Pre-pair via bluetoothctl if the watch isn't bonded yet. Doing this
        # explicitly (instead of relying on bleak's implicit retry when GATT
        # returns Insufficient Authentication) is more reliable on BlueZ —
        # it keeps the PIN prompt inside a well-defined flow and lets us
        # mark the device trusted before the GATT session starts.
        await self._ensure_paired(address)

        self._events.put(("status", f"Connecting to {dev.name or address}..."))

        def on_disconnect(_client):
            self.connected = False
            self._events.put(("disconnected", address))

        self._rx_buffer = ""
        self._rx_discard = False
        async with BleakClient(
            dev,
            timeout=CONNECT_TIMEOUT,
            disconnected_callback=on_disconnect,
        ) as client:
            self._client = client
            self.connected = True
            self.device_name = client.address

            # Try to read device name
            for svc in client.services:
                for char in svc.characteristics:
                    if "2a00" in str(char.uuid).lower():
                        try:
                            name_bytes = await client.read_gatt_char(char)
                            self.device_name = name_bytes.decode(
                                "utf-8", errors="replace")
                        except Exception:
                            pass

            self._events.put(("connected", self.device_name))

            # Subscribe to NUS TX (watch → game)
            await client.start_notify(NUS_TX_CHAR, self._on_nus_notify)

            # Request initial status
            await self._nus_write(client, '{"cmd":"version"}\n')
            await self._nus_write(client, '{"cmd":"status"}\n')
            # Subscribed + first writes went through = the link is authenticated;
            # pairing is over, so stop answering agent requests.
            self._end_agent()

            # Main loop: send queued commands, keepalive every 30s
            last_keepalive = time.time()
            while not self._stop_event.is_set() and client.is_connected:
                # Process TX queue
                while not self._tx_queue.empty():
                    try:
                        msg = self._tx_queue.get_nowait()
                        await self._nus_write(client, msg)
                        last_keepalive = time.time()
                    except Exception as e:
                        self._events.put(("error", f"TX: {e}"))

                # Keepalive: send status every 30s (resets 60s watchdog on watch)
                if time.time() - last_keepalive >= 30:
                    try:
                        await self._nus_write(client, '{"cmd":"status"}\n')
                        last_keepalive = time.time()
                    except Exception:
                        pass

                await asyncio.sleep(0.1)

            self._client = None
            self.connected = False

    async def _nus_write(self, client, data: str):
        """Write data to NUS RX characteristic, chunked by MTU."""
        raw = data.encode("utf-8")
        try:
            rx_char = client.services.get_characteristic(NUS_RX_CHAR)
            mtu = rx_char.max_write_without_response_size
        except Exception:
            mtu = 20

        for i in range(0, len(raw), mtu):
            chunk = raw[i:i + mtu]
            await client.write_gatt_char(NUS_RX_CHAR, chunk, response=False)

    def _on_nus_notify(self, _sender, data: bytearray):
        """Handle incoming NUS data (chunked, reassemble on \\n)."""
        self._rx_buffer += data.decode("utf-8", errors="replace")
        while "\n" in self._rx_buffer:
            line, self._rx_buffer = self._rx_buffer.split("\n", 1)
            if self._rx_discard:              # tail of an over-long line
                self._rx_discard = False
                continue
            if len(line) > NUS_MAX_LINE:
                continue
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                self._handle_message(msg)
            except json.JSONDecodeError:
                self._events.put(("log", f"[Watch] {line[:200]}"))
        if len(self._rx_buffer) > NUS_MAX_LINE:   # no newline in sight: drop it
            self._rx_buffer = ""
            if not self._rx_discard:
                self._rx_discard = True
                self._events.put(("log", "[Watch] over-long NUS message dropped"))

    def _handle_message(self, msg: dict):
        """Route parsed JSON message from watch."""
        # Version response
        if "version" in msg:
            self.version = msg["version"]
            self.features = msg.get("features", [])
            self._events.put(("version", msg))

        # Status response
        elif "bat" in msg:
            self.battery = msg.get("bat", 0)
            self._events.put(("status_data", msg))

        # Event: NFC tag scanned
        elif msg.get("event") == "nfc_tag":
            self._events.put(("nfc_tag", msg))

        # Event: LoRa message
        elif msg.get("event") == "lora_msg":
            self._events.put(("lora_msg", msg))

        # Compass response
        elif "heading" in msg and "roll" in msg:
            self._events.put(("compass", msg))

        # Event: Evil Twin credential
        elif msg.get("event") == "et_cred":
            self._events.put(("et_cred", msg))

        # Event: deauth detected (TSCM)
        elif msg.get("event") == "deauth_detected":
            self._events.put(("deauth_detected", msg))

        # NFC file download
        elif msg.get("type") == "nfc_file":
            self._events.put(("nfc_file", msg))

        # NFC tag list
        elif "tags" in msg:
            self._events.put(("nfc_list", msg))

        # Recon results (WiFi + BLE)
        elif "wifi" in msg or "ble" in msg:
            self._events.put(("recon", msg))

        # LoRa message history
        elif "messages" in msg:
            self._events.put(("lora_history", msg))

        # Generic OK/error
        elif "ok" in msg:
            self._events.put(("ack", msg))

        else:
            self._events.put(("log", f"[Watch] {json.dumps(msg)}"))

    # ------------------------------------------------------------------
    # Pairing helper — explicit bluetoothctl pair/trust before GATT
    # ------------------------------------------------------------------
    async def _ensure_paired(self, address: str):
        """If the watch isn't paired + trusted yet, drive `bluetoothctl pair`
        so our D-Bus agent gets the PIN prompt, then mark trusted."""
        import subprocess
        try:
            info = subprocess.run(
                ["bluetoothctl", "info", address],
                capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return  # bluetoothctl missing — bleak's implicit retry will try
        paired = "Paired: yes" in info
        trusted = "Trusted: yes" in info
        if paired and trusted:
            return  # already bonded; subsequent connect is silent

        self._events.put(("status", "Pairing — check the watch for PIN"))
        # Runs the pair in a subprocess; D-Bus agent handles the PIN prompt.
        # 90 s covers user reading PIN from watch + typing it in the overlay.
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["bluetoothctl", "pair", address],
                capture_output=True, text=True, timeout=90,
            )
        except Exception as e:
            self._events.put(("log", f"[Watch] pair command: {e}"))
        # Mark trusted so BlueZ skips re-auth prompts on future connects.
        try:
            await asyncio.to_thread(
                subprocess.run,
                ["bluetoothctl", "trust", address],
                capture_output=True, text=True, timeout=10,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # D-Bus Agent for PIN pairing
    # ------------------------------------------------------------------
    def _run_dbus_agent(self):
        """Register BlueZ Agent1 for passkey pairing (runs GLib mainloop)."""
        try:
            import dbus
            import dbus.service
            import dbus.mainloop.glib
            from gi.repository import GLib
        except ImportError:
            self._events.put((
                "log", "[Watch] dbus/GLib not available — manual pair needed"))
            return

        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        manager = self

        class Rejected(dbus.DBusException):
            _dbus_error_name = "org.bluez.Error.Rejected"

        def _check(device):
            """Only the watch we are connecting to, only while that attempt runs."""
            target = manager._agent_target
            if (manager._agent_stop.is_set() or not target
                    or not str(device).endswith("/" + target)):
                manager._events.put((
                    "log", "[Watch] rejected pairing request from %s" % device))
                raise Rejected("not the watch being paired")

        def _wait_pin(device):
            _check(device)
            manager._pin_value = None
            manager._pin_event.clear()
            manager._pin_requested = True
            manager._events.put(("pin_request", str(device)))
            # Block until user provides PIN in game UI (or the attempt ends)
            got = manager._pin_event.wait(timeout=60)
            pin = manager._pin_value
            manager._pin_requested = False
            manager._pin_value = None
            if not got or pin is None or manager._agent_stop.is_set():
                raise Rejected("no PIN entered")
            return pin

        class PipBoyAgent(dbus.service.Object):
            AGENT_PATH = "/watchdogs/pipboy_agent"

            def __init__(self):
                super().__init__(bus, self.AGENT_PATH)

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="o", out_signature="u")
            def RequestPasskey(self, device):
                return dbus.UInt32(_wait_pin(device))

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="ouq", out_signature="")
            def DisplayPasskey(self, device, passkey, entered):
                _check(device)
                manager._events.put((
                    "log", f"[Watch] Passkey: {passkey:06d}"))

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="ou", out_signature="")
            def RequestConfirmation(self, device, passkey):
                # Numeric comparison needs the user to compare two numbers; the
                # game has no prompt for that, so never auto-confirm. The watch
                # pairs by passkey entry (RequestPasskey) instead.
                _check(device)
                manager._events.put((
                    "log", f"[Watch] numeric-comparison pairing ({passkey:06d}) "
                           "rejected - use PIN entry"))
                raise Rejected("confirmation not supported")

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="o", out_signature="")
            def RequestAuthorization(self, device):
                _check(device)
                raise Rejected("just-works pairing not allowed")

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="o", out_signature="s")
            def RequestPinCode(self, device):
                return str(_wait_pin(device)).zfill(6)

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="", out_signature="")
            def Release(self):
                pass

            @dbus.service.method("org.bluez.Agent1",
                                 in_signature="", out_signature="")
            def Cancel(self):
                manager._pin_requested = False
                manager._pin_event.set()

        agent = None
        try:
            agent = PipBoyAgent()
            agent_mgr = dbus.Interface(
                bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.AgentManager1")
            agent_mgr.RegisterAgent(PipBoyAgent.AGENT_PATH, "KeyboardDisplay")
            agent_mgr.RequestDefaultAgent(PipBoyAgent.AGENT_PATH)
            self._events.put(("log", "[Watch] BLE agent registered"))
        except Exception as e:
            self._events.put(("log", f"[Watch] Agent error: {e}"))
            try:
                if agent is not None:
                    agent.remove_from_connection()
            except Exception:
                pass
            return

        loop = GLib.MainLoop()
        self._glib_loop = loop

        # Run until this connect attempt ends (or the manager stops)
        def check_stop():
            if self._stop_event.is_set() or self._agent_stop.is_set():
                loop.quit()
                return False
            return True

        GLib.timeout_add(250, check_stop)
        try:
            loop.run()
        except Exception:
            pass
        finally:
            try:
                agent_mgr.UnregisterAgent(PipBoyAgent.AGENT_PATH)
            except Exception:
                pass
            try:                         # free the object path for the next attempt
                agent.remove_from_connection()
            except Exception:
                pass
            self._glib_loop = None
