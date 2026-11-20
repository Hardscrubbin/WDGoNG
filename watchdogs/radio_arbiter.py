"""Which radio serves which Wi-Fi/BLE function. The player sets a per-function
matrix (ESP32 / host card / auto); 'auto' resolves from what is actually present.

Persisted to ~/.wdgo/radio.json. With only one radio present everything resolves to
it, so a single-ESP32 machine behaves exactly as before - the matrix only matters
when both are connected.
"""
import json
import os

# capability -> radios that CAN serve it. Functions tied to the ESP32 firmware
# (evil twin, portal, the WPA3 SAE overflow, firmware BLE) are not in the matrix.
CAPS = {
    "scan":      ("esp32", "host"),   # Wi-Fi scan (menu + wardrive discovery)
    "wardrive":  ("esp32", "host"),   # continuous Wi-Fi wardrive
    "handshake": ("esp32", "host"),   # WPA handshake / PMKID capture
    "deauth":    ("esp32", "host"),   # targeted deauth
    "ble":       ("esp32", "host"),   # BLE scan (host = BlueZ hci)
}
CAP_LABEL = {"scan": "WiFi scan", "wardrive": "WiFi wardrive", "handshake": "Handshake capture",
             "deauth": "Deauth", "ble": "BLE scan"}
CHOICES = ("auto", "esp32", "host")


def _path() -> str:
    base = os.environ.get("WDGO_APP_DIR") or os.path.expanduser("~")
    return os.path.join(base, ".wdgo", "radio.json")


class RadioArbiter:
    """Holds the matrix and resolves a capability to a concrete backend. `detect`
    is a dict from radio_detect.detect(); pass a fresh one when hardware changes."""

    def __init__(self, detect: dict = None):
        self.detect = detect or {"esp32": "", "host_wifi": [], "both": False}
        self.matrix = {c: "auto" for c in CAPS}
        self._load()

    # ---- persistence --------------------------------------------------
    def _load(self):
        try:
            with open(_path(), encoding="utf-8") as f:
                saved = json.load(f)
            for c in CAPS:
                v = saved.get(c)
                if v in CHOICES:
                    self.matrix[c] = v
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            p = _path()
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(self.matrix, f)
        except OSError:
            pass

    # ---- state --------------------------------------------------------
    def has_esp32(self) -> bool:
        return bool(self.detect.get("esp32"))

    def host_iface(self) -> str:
        ifs = self.detect.get("host_wifi") or []
        return ifs[0]["iface"] if ifs else ""

    def has_host(self) -> bool:
        return bool(self.host_iface())

    def set(self, cap: str, choice: str):
        if cap in CAPS and choice in CHOICES:
            self.matrix[cap] = choice
            self.save()

    def cycle(self, cap: str) -> str:
        """Advance a capability to the next VALID choice and return it. Choices that
        name an absent radio are skipped, so you can never pick something not there."""
        if cap not in CAPS:
            return ""
        order = [c for c in CHOICES
                 if c == "auto" or (c == "esp32" and self.has_esp32()) or (c == "host" and self.has_host())]
        if not order:
            return self.matrix[cap]
        i = order.index(self.matrix[cap]) if self.matrix[cap] in order else -1
        self.matrix[cap] = order[(i + 1) % len(order)]
        self.save()
        return self.matrix[cap]

    def resolve(self, cap: str) -> str:
        """The backend that will actually serve `cap` right now: 'esp32', 'host' or
        '' (none available). 'auto' prefers the ESP32, falls back to the host card."""
        if cap not in CAPS:
            return "esp32" if self.has_esp32() else ""
        choice = self.matrix.get(cap, "auto")
        if choice == "esp32":
            return "esp32" if self.has_esp32() else ("host" if self.has_host() else "")
        if choice == "host":
            return "host" if self.has_host() else ("esp32" if self.has_esp32() else "")
        # auto: ESP32 is primary, the host card is the fallback / second radio
        if self.has_esp32():
            return "esp32"
        return "host" if self.has_host() else ""

    def uses_host(self) -> bool:
        """True if any capability currently resolves to the host card."""
        return any(self.resolve(c) == "host" for c in CAPS)

    def rows(self) -> list:
        """UI rows: [(cap, label, choice, resolved, [valid choices])]."""
        out = []
        for c in CAPS:
            valid = ["auto"] + [r for r in ("esp32", "host")
                                if (r == "esp32" and self.has_esp32()) or (r == "host" and self.has_host())]
            out.append((c, CAP_LABEL[c], self.matrix[c], self.resolve(c), valid))
        return out
