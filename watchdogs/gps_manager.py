"""GPS receiver — NMEA parser for UART/USB GPS modules."""

import glob
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Optional

import serial

from .config import GPS_DEVICE, GPS_BAUD_RATE

log = logging.getLogger(__name__)


@dataclass
class GpsFix:
    """Snapshot of current GPS state."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    speed_knots: float = 0.0
    course_deg: float = 0.0    # course over ground, degrees true (RMC field 8)
    satellites: int = 0
    satellites_visible: int = 0
    fix_quality: int = 0       # 0=no fix, 1=GPS, 2=DGPS
    hdop: float = 99.9
    timestamp: str = ""        # UTC time from NMEA (hhmmss.ss)
    valid: bool = False


class _LineBuffer:
    """Accumulate raw bytes and yield complete NMEA sentences."""

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, raw: bytes) -> List[str]:
        self._buf += raw
        lines: List[str] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            decoded = line.decode("ascii", errors="replace").strip()
            if decoded.startswith("$"):
                lines.append(decoded)
        # Prevent unbounded growth if no newlines arrive
        if len(self._buf) > 1024:
            self._buf = self._buf[-512:]
        return lines


class GpsManager:
    """Manage a UART GPS receiver via pyserial + urwid watch_file."""

    # True when user explicitly set the GPS device via env var
    _user_configured = bool(
        os.environ.get("WDG_GPS_DEVICE")
        or os.environ.get("JANOS_GPS_DEVICE")
    )

    # A fix older than this (no fresh valid sentence within the window) is treated
    # as stale: the receiver went silent or lost lock, so the last known position
    # must not keep geo-tagging new loot. GPS emits GGA/RMC at ~1 Hz, so this
    # tolerates ~10 missed sentences before invalidating.
    MAX_FIX_AGE = 10.0

    def __init__(self, device: str = GPS_DEVICE,
                 baud: int = GPS_BAUD_RATE) -> None:
        self.device = device
        self._baud = baud
        self._conn: Optional[serial.Serial] = None
        self._buf = _LineBuffer()
        self.fix = GpsFix()
        self._available = False
        self._gsv_visible: dict = {}  # constellation prefix → satellite count
        # time.monotonic() of the last VALID fix; 0.0 = never had one. Monotonic
        # so it is immune to NTP/system clock jumps.
        self._fix_monotonic = 0.0
        self._last_device: Optional[str] = None   # the port that opened last time

    def reopen(self) -> bool:
        """Reopen the port that worked last, after a read error closed it. Never
        probes other ports, so it cannot poke an unrelated serial device (an
        ESP32 can reset when its port is opened)."""
        if self._available:
            return True
        if not self._last_device:
            return False
        return self._try_open(self._last_device)

    @property
    def available(self) -> bool:
        return self._available

    def _mark_fix_time(self) -> None:
        """Record that a valid fix was just parsed (for staleness ageing)."""
        self._fix_monotonic = time.monotonic()

    def fix_age(self) -> float:
        """Seconds since the last valid fix (inf if there has never been one)."""
        if self._fix_monotonic <= 0.0:
            return float("inf")
        return time.monotonic() - self._fix_monotonic

    @property
    def has_valid_fix(self) -> bool:
        """True only when the port is open, the last fix was valid AND it is still
        fresh. After GPS loss (silence or a read error) the frozen position goes
        stale and this returns False — callers must gate geo-tagging on this, not
        on ``available``/``fix.valid`` alone, or old loot gets the last position."""
        return (self._available and self.fix.valid
                and self.fix_age() <= self.MAX_FIX_AGE)

    def _invalidate_fix(self, reason: str) -> None:
        """Drop the current fix and close the port so it can be reopened. Called on
        a read error (USB GPS unplugged / cable fault): the position is no longer
        trustworthy and must not be reused for the rest of the drive."""
        log.info("GPS invalidated (%s) — position dropped, port closed for reopen",
                 reason)
        self.fix.valid = False
        self._fix_monotonic = 0.0
        self._available = False
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Try to open GPS serial port. Returns True on success.
        If user set WDG_GPS_DEVICE (or legacy JANOS_GPS_DEVICE), use that directly.
        Otherwise probe default path for NMEA, then auto-detect USB GPS.
        Never raises — GPS is optional."""
        device = self.device

        if self._user_configured:
            # User explicitly set env var — trust it
            if self._try_open(device):
                return True
            log.info("GPS device %s (from env) not available — GPS disabled",
                     device)
            return False

        # No env var — probe default path, then auto-detect
        if os.path.exists(device) and self._probe_nmea(device, self._baud):
            if self._try_open(device):
                return True

        log.info("GPS not on %s, scanning for USB GPS...", device)
        detected = self._auto_detect(exclude={device})
        if detected:
            self.device = detected
            if self._try_open(detected):
                return True
        log.info("No GPS found — GPS disabled")
        return False

    @staticmethod
    def _probe_nmea(device: str, baud: int) -> bool:
        """Quick check if a serial port outputs NMEA sentences."""
        try:
            conn = serial.Serial(port=device, baudrate=baud, timeout=2)
            data = conn.read(512)
            conn.close()
            return b"$GP" in data or b"$GN" in data
        except Exception:
            return False

    def _try_open(self, device: str) -> bool:
        """Try to open a serial port as GPS. Returns True on success."""
        if not os.path.exists(device):
            return False
        if not os.access(device, os.R_OK):
            log.warning("No read access to %s", device)
            return False
        try:
            self._conn = serial.Serial(
                port=device,
                baudrate=self._baud,
                timeout=0,
            )
            self._conn.reset_input_buffer()
            self._available = True
            self._last_device = device
            log.info("GPS opened: %s @ %d baud", device, self._baud)
            return True
        except Exception as exc:
            log.debug("GPS open failed on %s: %s", device, exc)
            return False

    @staticmethod
    def _auto_detect(exclude: Optional[set] = None) -> Optional[str]:
        """Scan common serial paths for a GPS device (NMEA probe)."""
        skip = exclude or set()
        candidates = sorted(
            glob.glob("/dev/ttyUSB*")
            + glob.glob("/dev/ttyACM*")
            + glob.glob("/dev/ttyAMA*")
        )
        # Some Raspberry Pi / CM4 boards expose the GPS UART (e.g. the AIO board's
        # built-in GPS) as the mini UART /dev/ttyS0. Probe only that one port, not
        # ttyS*, so PCs with many ttyS ports don't wait 2 s each. (LOCOSP/WatchDogsGo#17)
        if os.path.exists("/dev/ttyS0"):
            candidates.append("/dev/ttyS0")
        for path in candidates:
            if path in skip:
                continue
            if not os.access(path, os.R_OK):
                continue
            try:
                conn = serial.Serial(port=path, baudrate=9600, timeout=2)
                data = conn.read(512)
                conn.close()
                if b"$GP" in data or b"$GN" in data:
                    log.info("GPS auto-detected on %s", path)
                    return path
                log.debug("GPS probe %s — no NMEA data", path)
            except Exception:
                continue
        return None

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        self._available = False

    @property
    def fd(self) -> int:
        """File descriptor for urwid watch_file."""
        if self._conn is None:
            raise RuntimeError("GPS port not open")
        return self._conn.fileno()

    # ------------------------------------------------------------------
    # Reading & parsing
    # ------------------------------------------------------------------

    def read_available(self) -> List[str]:
        """Non-blocking read — return complete NMEA sentences."""
        if not self._conn:
            return []
        try:
            waiting = self._conn.in_waiting
            if waiting <= 0:
                return []
            raw = self._conn.read(waiting)
            return self._buf.feed(raw)
        except Exception as exc:
            # A read error means the port died (GPS unplugged / cable fault).
            # Invalidate immediately so no further loot is tagged with a stale
            # position, and close the port so a later setup() can reopen it.
            log.debug("GPS read error: %s", exc)
            self._invalidate_fix("read error")
            return []

    def process_sentences(self, sentences: List[str]) -> None:
        """Parse NMEA sentences and update self.fix."""
        for s in sentences:
            try:
                self._parse(s)
            except Exception as exc:
                log.debug("GPS parse error: %s — %s", s.strip(), exc)

    @staticmethod
    def _checksum_ok(sentence: str) -> bool:
        """Validate the NMEA ``*HH`` checksum (XOR of every char between $ and *).
        A corrupted sentence (bit flips on a noisy UART) is rejected here so its
        half-mangled fields never reach self.fix."""
        if not sentence.startswith("$") or "*" not in sentence:
            return False
        body, _, cks = sentence[1:].partition("*")
        cks = cks.strip()[:2]
        if len(cks) != 2:
            return False
        try:
            want = int(cks, 16)
        except ValueError:
            return False
        got = 0
        for ch in body:
            got ^= ord(ch)
        return got == want

    def _parse(self, sentence: str) -> None:
        # Require and verify the NMEA checksum before trusting any field.
        if not self._checksum_ok(sentence):
            return
        sentence = sentence.split("*")[0]
        parts = sentence.split(",")
        if len(parts) < 3:
            return
        kind = parts[0]
        if kind in ("$GPGGA", "$GNGGA"):
            self._parse_gga(parts)
        elif kind in ("$GPRMC", "$GNRMC"):
            self._parse_rmc(parts)
        elif kind in ("$GPGSV", "$GLGSV", "$GNGSV", "$GBGSV", "$GAGSV"):
            self._parse_gsv(parts)

    def _parse_gga(self, p: List[str]) -> None:
        """$GPGGA: time, lat, N/S, lon, E/W, quality, sats, hdop, alt, ...

        Validate the whole fix (both coordinates present and in range) before
        committing ANY of it, so a sentence with a good latitude but a truncated
        longitude cannot leave self.fix holding a new lat beside a stale lon."""
        if len(p) < 10:
            return
        try:
            quality = int(p[6]) if p[6] else 0
        except ValueError:
            return
        valid = quality > 0
        lat = lon = None
        if valid:
            if not (p[2] and p[3] and p[4] and p[5]):
                return  # incomplete fix — do not publish a partial update
            try:
                lat = self._to_decimal(p[2], p[3])
                lon = self._to_decimal(p[4], p[5])
            except (ValueError, IndexError):
                return
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return
        # Commit atomically now that the measurement validated.
        self.fix.fix_quality = quality
        self.fix.valid = valid
        if p[1]:
            self.fix.timestamp = p[1]
        try:
            self.fix.satellites = int(p[7]) if p[7] else 0
        except ValueError:
            pass
        try:
            self.fix.hdop = float(p[8]) if p[8] else 99.9
        except ValueError:
            pass
        if valid:
            self.fix.latitude = lat
            self.fix.longitude = lon
            if p[9]:
                try:
                    self.fix.altitude = float(p[9])
                except ValueError:
                    pass
            self._mark_fix_time()

    def _parse_rmc(self, p: List[str]) -> None:
        """$GPRMC: time, status, lat, N/S, lon, E/W, speed, ...

        Same all-or-nothing rule as GGA: only publish lat+lon together, and only
        after both validate, so a bad field cannot mix two measurements."""
        if len(p) < 8:
            return
        valid = (p[2] == "A")
        lat = lon = speed = course = None
        if valid:
            if not (p[3] and p[4] and p[5] and p[6]):
                return
            try:
                lat = self._to_decimal(p[3], p[4])
                lon = self._to_decimal(p[5], p[6])
            except (ValueError, IndexError):
                return
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return
            if p[7]:
                try:
                    speed = float(p[7])
                except ValueError:
                    speed = None
            if len(p) > 8 and p[8]:
                try:
                    course = float(p[8])
                except ValueError:
                    course = None
                if course is not None and not (0.0 <= course <= 360.0):
                    course = None
        self.fix.valid = valid
        if p[1]:
            self.fix.timestamp = p[1]
        if valid:
            self.fix.latitude = lat
            self.fix.longitude = lon
            if speed is not None:
                self.fix.speed_knots = speed
            if course is not None:
                self.fix.course_deg = course
            self._mark_fix_time()

    def _parse_gsv(self, p: List[str]) -> None:
        """$xxGSV: total_msgs, msg_num, sats_in_view, ..."""
        if len(p) < 4:
            return
        prefix = p[0][:3]  # $GP, $GL, $GN, $GB, $GA
        total_visible = int(p[3]) if p[3] else 0
        self._gsv_visible[prefix] = total_visible
        self.fix.satellites_visible = sum(self._gsv_visible.values())

    @staticmethod
    def _to_decimal(value: str, direction: str) -> float:
        """Convert NMEA ddmm.mmmm to decimal degrees."""
        dot = value.index(".")
        degrees = int(value[:dot - 2])
        minutes = float(value[dot - 2:])
        result = degrees + minutes / 60.0
        if direction in ("S", "W"):
            result = -result
        return result
