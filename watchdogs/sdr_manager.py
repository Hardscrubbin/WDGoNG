"""SDR Manager — ADS-B aircraft tracking + 433 MHz sensor decoding.

Uses RTL-SDR (RTL2832U) on AIO v2 board via dump1090 and rtl_433.
Subprocess + thread + queue pattern (same as serial_manager).
"""

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from queue import Queue
from typing import Callable, Optional

log = logging.getLogger(__name__)

# --- ADS-B Constants ---
# dump1090-fa/mutability: --net enables SBS on 30003 by default
# --quiet suppresses interactive output
DUMP1090_CMD = ["dump1090", "--net", "--quiet"]
# Fallback without --quiet (some builds don't support it)
DUMP1090_CMD_FALLBACK = ["dump1090", "--net"]

# --- rtl_433 Constants ---
RTL433_CMD = [
    "rtl_433",
    "-f", "433920000",            # 433.92 MHz
    "-F", "json",                 # JSON output to stdout
    "-M", "time:utc",            # UTC timestamps
    "-M", "level",               # include signal level
]

# Device index: dump1090 uses device 0, rtl_433 uses device 1
# (or run them sequentially, not simultaneously)
RTL433_CMD_DEV1 = RTL433_CMD + ["-d", "1"]

ADSB_TIMEOUT = 60  # remove aircraft not seen for 60s
SENSOR_TIMEOUT = 300  # remove sensors not seen for 5min


@dataclass
class Aircraft:
    """Tracked aircraft from ADS-B."""
    icao: str            # ICAO 24-bit hex address
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0    # feet
    speed: int = 0       # knots
    heading: int = 0     # degrees
    squawk: str = ""
    last_seen: float = 0.0
    rssi: float = 0.0

    @property
    def has_position(self) -> bool:
        return self.lat != 0.0 or self.lon != 0.0


@dataclass
class Sensor433:
    """Decoded 433 MHz sensor from rtl_433."""
    model: str
    sid: str              # unique sensor ID (model + id combo)
    data: dict = field(default_factory=dict)
    lat: float = 0.0     # GPS position when first seen
    lon: float = 0.0
    last_seen: float = 0.0
    count: int = 0        # times seen


# --- Emergency-vehicle RF presence scanner (RTL-SDR / rtl_power) ---
# Presence/activity detection only (NO content decode). Polish emergency-service
# sub-bands: fire/EMS/police analog FM VHF + police/EMS TETRA. A "vehicle"
# channel firing = a signal jumping above its OWN rolling baseline (a keyup);
# the TETRA downlink is kept as a passive "network present" reference (base
# carrier is always on). Verified live on-device: strong TETRA @~392 MHz,
# fire @~148.63. Analog VHF plans sourced from czestotliwosci.pl.tl (Opolskie):
#   fire PSP/OSP 148.66-149.34 | EMS/karetki 168.53-169.15 | police 172.575 +
#   national 172.9/173.0 | municipal (SM) Opole 147.825 (147.74-148.95 range).
# NOTE: the SM/analog-police plans may be dated -- calibrate against live logs.
EMERGENCY_BANDS = [
    # name,        lo_hz,       hi_hz,       bin_hz, role,      thr_db
    # thr raised 7->10: a vehicle you'd actually SEE keys up strong (jump >10);
    # weak/distant blips you can't see anyway -> fewer false "emergency" alerts.
    ("SM-VHF",     147_700_000, 148_450_000, 12_500, "vehicle", 10.0),  # straż miejska (Opole 147.825)
    ("STRAZ-VHF",  148_500_000, 149_400_000, 12_500, "vehicle", 10.0),  # straż pożarna PSP/OSP
    ("EMS-VHF",    168_500_000, 169_150_000, 12_500, "vehicle", 10.0),  # pogotowie / karetki
    ("POL-VHF",    172_500_000, 173_100_000, 12_500, "vehicle", 10.0),  # policja analog (Opolskie 172.575)
    ("TETRA-UL",   381_000_000, 383_000_000, 25_000, "vehicle", 10.0),  # wóz TETRA (uplink)
    ("TETRA-DL",   391_000_000, 393_000_000, 25_000, "network", 10.0),  # sieć TETRA (downlink ref)
]

# Fixed non-vehicle carriers to notch out (freq_hz, tolerance_hz). These sit on a
# single frequency and key repeatedly for the WHOLE session (paging/telemetry/
# base link) — a real passing vehicle is a brief one-off, not a persistent tone.
# Confirmed over a full commute (2026-11-12 log): every false alert was one of
# these two. Excluded from peak AND floor, so real signals elsewhere still fire.
EMERGENCY_NOTCH = [
    (169_104_000, 30_000),   # EMS-band fixed carrier (not an ambulance)
    (172_655_000, 30_000),   # POL-band fixed carrier (not a police car)
]
EMERGENCY_GAIN = "45"
EMERGENCY_INTEG = "0.5"     # rtl_power integration s/band (faster revisit)

# --- WDG Key Hunter (433.92 MHz car key-fob fox hunt) ---
# Receive-only RSSI gradient hunt for a car remote (EU keyless fobs: 433.92 MHz,
# short OOK/FSK bursts fired only while a button is pressed). We sweep a window
# that KEEPS the fob off the tuner-centre DC spike: centre 434.00 MHz, so 433.92
# sits ~80 kHz below centre — clear of a small DC guard and of the band edges.
# A "burst" = the window peak jumping above its rolling noise floor; proximity
# ("hot") is that jump normalised, driving the fox-hunt beep rate + the bar.
KEY_HUNT_LO = 433_400_000
KEY_HUNT_HI = 434_600_000
KEY_HUNT_BIN = 8_000        # ~8 kHz bins -> ~150 bins, fine enough for a fob
KEY_HUNT_GUARD = 12_000     # tiny DC-spike notch only (NOT the 110 kHz emerg guard)
KEY_HUNT_GAIN = "40"
KEY_HUNT_INTEG = "0.08"     # short integration -> fast revisit for a moving hunt
KEY_HUNT_THR = 18.0         # dB over the bin's OWN background to call a fob burst
KEY_HUNT_HOT_SPAN = 25.0    # dB above the threshold mapped onto the 0..1 "hot" scale
# Calibrated 2026-11-19 with real remotes, from 1 m and 5-10 m:
#   car key (a car):  434.413-434.417 MHz, +30..41 dB at 5-10 m, core +-10 kHz
#   gate remote:        ~434.11 MHz (434.098-434.122), +35..39 dB at 5-10 m
# At 1 m a remote overloads the receiver and the whole band rises. Background: a
# constant carrier at ~433.913 MHz, a drifting one around 434.26-434.37 MHz (10-18
# dB) and neighbours' sensors at 433.83-433.96 MHz - hence a per-bin background
# and a narrow target window instead of "strongest bin over the median floor".
KEY_HUNT_TARGET = 434_415_000      # default: the car key (another remote: learn it with [L])
KEY_HUNT_TARGET_HALF = 20_000      # +- window around the fob centre
KEY_HUNT_BG_ROWS = 20              # sweeps per bin kept for its background (median)
KEY_HUNT_OVERLOAD = 10.0           # median rise of the WHOLE band = fob right next to you
KEY_HUNT_LEARN_MIN = 20.0          # dB a burst needs to be learned as the fob
KEY_HUNT_WF = 90            # waterfall history depth (sweeps)


@dataclass
class EmergencySignal:
    """A detected emergency-service RF signal (presence, not content)."""
    band: str
    freq: float = 0.0        # MHz of the peak bin
    rssi: float = 0.0        # peak power (dB, relative)
    baseline: float = 0.0    # channel's own rolling norm
    jump: float = 0.0        # rssi - baseline (keyup magnitude)
    trend: str = ""          # "up" (approaching) / "dn" / ""
    active: bool = False     # vehicle keyup event in progress
    role: str = "vehicle"    # "vehicle" or "network"
    last_seen: float = 0.0


@dataclass
class KeyHuntState:
    """Live state of the 433.92 MHz key-fob fox hunt (receive-only)."""
    rssi: float = -999.0      # current window peak power (dB, relative)
    floor: float = -999.0     # rolling noise floor (dB)
    jump: float = 0.0         # rssi - floor (burst magnitude)
    peak_freq: float = 0.0    # MHz of the current peak bin
    hot: float = 0.0          # 0..1 normalised proximity (beep rate + bar)
    best: float = -999.0      # strongest jump this session (hunt reference)
    burst: bool = False       # a fob burst is firing this sweep
    hits: int = 0             # total bursts counted
    last_burst: float = 0.0   # time.time() of the last burst
    sweeps: int = 0           # total sweeps (liveness)
    target_mhz: float = KEY_HUNT_TARGET / 1e6   # fob frequency being hunted
    learning: bool = False    # [L] pressed: the next strong burst becomes the target
    other_mhz: float = 0.0    # strongest signal OUTSIDE the fob window this sweep
    other_db: float = 0.0
    overload: bool = False    # the whole band rose: the fob is right next to the receiver


class SDRManager:
    """Manages dump1090 (ADS-B) and rtl_433 (ISM sensors) subprocesses."""

    def __init__(self):
        self.running = False
        self.mode: str = ""  # "adsb", "433", "adsb+433"

        # ADS-B state
        self.aircraft: dict[str, Aircraft] = {}  # icao -> Aircraft
        self.total_aircraft_seen: int = 0

        # 433 MHz state
        self.sensors: dict[str, Sensor433] = {}  # sid -> Sensor433
        self.total_sensors_seen: int = 0

        # Subprocess handles
        self._dump1090_proc: Optional[subprocess.Popen] = None
        self._rtl433_proc: Optional[subprocess.Popen] = None

        # Event queue for app.py
        self._events: Queue = Queue()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()

        # Callbacks
        self.on_new_aircraft: Optional[Callable] = None
        self.on_new_sensor: Optional[Callable] = None

        # Loot file handles
        self._adsb_log: Optional[object] = None
        self._sensor_log: Optional[object] = None

        # Emergency-vehicle RF scanner state
        self.emergency: dict[str, EmergencySignal] = {}  # band -> signal
        self.emergency_active = False   # any vehicle channel firing right now
        self.network_present = False    # TETRA base carrier seen (services present)
        self.total_emergency_hits = 0
        self._emerg_proc: Optional[subprocess.Popen] = None
        self._emerg_log: Optional[object] = None
        self._scan_log: Optional[object] = None   # full per-sweep calibration log

        # Key Hunter state (433.92 MHz fox hunt)
        from collections import deque as _deque
        self.keyhunt = KeyHuntState()
        self.keyhunt_wf: _deque = _deque(maxlen=KEY_HUNT_WF)  # waterfall (list of dB rows)
        self.keyhunt_axis: list = []     # freq axis (MHz) matching each waterfall row
        self._keyhunt_log: Optional[object] = None

        # 433 Capture state (rtl_433 -S all -> raw .cu8 + decoded log + Flipper .sub)
        self._grab_proc: Optional[subprocess.Popen] = None
        self.grab_count = 0              # signals captured this session
        self.grab_dir = ""               # capture output dir
        self.grab_last = ""              # last .sub basename written (or decoded label)

    @staticmethod
    def has_dump1090() -> bool:
        return shutil.which("dump1090") is not None

    @staticmethod
    def has_rtl433() -> bool:
        return shutil.which("rtl_433") is not None

    def start_adsb(self, loot_dir: str = "") -> bool:
        """Start ADS-B tracking via dump1090."""
        if not self.has_dump1090():
            self._events.put(("error", "dump1090 not installed"))
            return False
        if self._dump1090_proc:
            return True  # already running

        self._stop_event.clear()

        # Stop system dump1090 service if running (holds RTL-SDR device)
        try:
            subprocess.run(
                ["systemctl", "stop", "dump1090-mutability"],
                capture_output=True, timeout=5)
        except Exception:
            pass

        try:
            self._dump1090_proc = subprocess.Popen(
                DUMP1090_CMD,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
            # Check if process died immediately (bad flags)
            time.sleep(0.5)
            if self._dump1090_proc.poll() is not None:
                # Retry without --quiet
                self._dump1090_proc = subprocess.Popen(
                    DUMP1090_CMD_FALLBACK,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
        except Exception as e:
            self._events.put(("error", f"dump1090 failed: {e}"))
            return False

        # Open loot file
        if loot_dir:
            try:
                self._adsb_log = open(
                    os.path.join(loot_dir, "adsb_aircraft.csv"), "a")
                if self._adsb_log.tell() == 0:
                    self._adsb_log.write(
                        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,"
                        "heading,squawk\n")
            except Exception:
                pass

        # Start SBS parser thread (connects to port 30003)
        t = threading.Thread(target=self._sbs_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self.running = True
        self.mode = "adsb"
        self._events.put(("status", "ADS-B started (dump1090)"))
        return True

    def start_433(self, loot_dir: str = "", gps_lat: float = 0.0,
                  gps_lon: float = 0.0) -> bool:
        """Start 433 MHz sensor decoding via rtl_433."""
        if not self.has_rtl433():
            self._events.put(("error", "rtl_433 not installed"))
            return False
        if self._rtl433_proc:
            return True

        self._stop_event.clear()
        self._gps_lat = gps_lat
        self._gps_lon = gps_lon

        # Use device 1 if dump1090 is already using device 0
        cmd = RTL433_CMD_DEV1 if self._dump1090_proc else RTL433_CMD
        try:
            self._rtl433_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self._events.put(("error", f"rtl_433 failed: {e}"))
            return False

        # Open loot file
        if loot_dir:
            try:
                self._sensor_log = open(
                    os.path.join(loot_dir, "sdr_sensors.csv"), "a")
                if self._sensor_log.tell() == 0:
                    self._sensor_log.write(
                        "timestamp,model,id,data,lat,lon\n")
            except Exception:
                pass

        t = threading.Thread(target=self._rtl433_reader, daemon=True)
        t.start()
        self._threads.append(t)

        self.running = True
        if self.mode == "adsb":
            self.mode = "adsb+433"
        else:
            self.mode = "433"
        self._events.put(("status", "433 MHz scanner started (rtl_433)"))
        return True

    # ------------------------------------------------------------------
    # Emergency-vehicle RF presence scanner (rtl_power sweep loop)
    # ------------------------------------------------------------------
    @staticmethod
    def has_rtl_power() -> bool:
        return shutil.which("rtl_power") is not None

    def start_emergency(self, loot_dir: str = "", gps_lat: float = 0.0,
                        gps_lon: float = 0.0) -> bool:
        """Start emergency-vehicle RF presence scan (needs the RTL-SDR alone)."""
        if not self.has_rtl_power():
            self._events.put(("error", "rtl_power not installed (apt install rtl-sdr)"))
            return False
        if "emerg" in self.mode:
            return True
        self._stop_event.clear()
        # Coerce to float — the game may pass player_lat/lon as None before a GPS
        # fix, and "{None:.6f}" would raise in the loggers (silently swallowed,
        # leaving 0-byte logs). Fall back to 0.0 until a fix arrives.
        self._gps_lat = gps_lat if isinstance(gps_lat, (int, float)) else 0.0
        self._gps_lon = gps_lon if isinstance(gps_lon, (int, float)) else 0.0
        if loot_dir:
            try:
                self._emerg_log = open(
                    os.path.join(loot_dir, "emergency_rf.csv"), "a")
                if self._emerg_log.tell() == 0:
                    self._emerg_log.write(
                        "timestamp,band,freq_mhz,rssi_db,jump_db,trend,lat,lon\n")
            except Exception:
                self._emerg_log = None
            try:
                # full per-sweep log (every band, every pass) for calibration
                self._scan_log = open(
                    os.path.join(loot_dir, "emergency_scan.csv"), "a")
                if self._scan_log.tell() == 0:
                    self._scan_log.write(
                        "timestamp,band,role,freq_mhz,rssi_db,baseline_db,"
                        "floor_db,jump_db,thr_db,trend,active,net_present,"
                        "lat,lon\n")
            except Exception:
                self._scan_log = None
        t = threading.Thread(target=self._emergency_reader, daemon=True)
        t.start()
        self._threads.append(t)
        self.running = True
        self.mode = "emergency" if not self.mode else self.mode + "+emergency"
        self._events.put(("status", "Emergency radio scanner started (rtl_power)"))
        return True

    def _rtl_power_peak(self, lo: int, hi: int, binw: int):
        """One rtl_power single-shot -> (peak_freq_hz, peak_db, noise_floor_db)."""
        import statistics
        # Per-process temp file: a stale file owned by a different user (e.g. a
        # test run under another account) would make rtl_power's write fail and
        # every band read back empty. PID-scoping avoids that collision entirely.
        tmp = f"/tmp/_wdg_emerg_{os.getpid()}.csv"
        try:
            self._emerg_proc = subprocess.Popen(
                ["rtl_power", "-f", f"{lo}:{hi}:{binw}", "-1",
                 "-i", EMERGENCY_INTEG, "-g", EMERGENCY_GAIN, tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid)
            self._emerg_proc.wait(timeout=8)
        except Exception:
            return None
        finally:
            self._emerg_proc = None
        pts = []
        try:
            for line in open(tmp):
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                try:
                    l = float(p[2]); step = float(p[4])
                except ValueError:
                    continue
                for i, x in enumerate(p[6:]):
                    try:
                        d = float(x)
                    except ValueError:
                        continue
                    if d != d:  # nan
                        continue
                    pts.append((l + step * (i + 0.5), d))
        except OSError:
            return None
        if not pts:
            return None
        # Reject the RTL-SDR DC spike / centre spur (a fixed strong bin near the
        # tuning centre that otherwise dominates the peak on every sweep -> the
        # 382.086 MHz false alerts, and masks real vehicle signals) and the noisy
        # band edges. Verified: real service carriers sit >130 kHz off-centre,
        # the DC spur ~86 kHz, so a 110 kHz centre guard separates them cleanly.
        center = (lo + hi) / 2.0
        guard = max(4 * binw, 110_000)
        edge = binw
        usable = [(fq, dd) for (fq, dd) in pts
                  if abs(fq - center) > guard and (lo + edge) < fq < (hi - edge)
                  and not any(abs(fq - nf) <= nt for nf, nt in EMERGENCY_NOTCH)]
        if not usable:
            usable = pts
        floor = statistics.median(dd for _, dd in usable)
        f, d = max(usable, key=lambda t: t[1])
        return f, d, floor

    def _emergency_reader(self):
        """Sweep emergency sub-bands; keyup event vs steady infra + RSSI trend."""
        import statistics
        from collections import deque
        hist = {b[0]: deque(maxlen=8) for b in EMERGENCY_BANDS}
        cycle = 0
        while not self._stop_event.is_set():
            any_active = False
            cycle += 1
            for name, lo, hi, binw, role, thr in EMERGENCY_BANDS:
                if self._stop_event.is_set():
                    break
                # Prioritise the vehicle bands: the TETRA downlink is always-on
                # (network reference, slow-changing) and SM is low-priority, so
                # sample them less often -> tighter revisit on fire/EMS/police/
                # TETRA-uplink where a passing keyup must be caught.
                if role == "network" and (cycle % 4) != 0:
                    continue
                if name == "SM-VHF" and (cycle % 2) != 0:
                    continue
                r = self._rtl_power_peak(lo, hi, binw)
                if r is None:
                    continue
                freq, db, floor = r
                h = list(hist[name])
                base = statistics.median(h) if len(h) >= 4 else db
                hist[name].append(db)
                hh = list(hist[name])
                trend = ""
                if len(hh) >= 3:
                    if hh[-1] - min(hh[-3:]) > 3:
                        trend = "up"
                    elif max(hh[-3:]) - hh[-1] > 3:
                        trend = "dn"
                sig = self.emergency.setdefault(
                    name, EmergencySignal(band=name, role=role))
                sig.freq = freq / 1e6
                sig.rssi = db
                sig.baseline = base
                sig.trend = trend
                sig.last_seen = time.time()
                if role == "network":
                    sig.jump = db - floor
                    self.network_present = sig.jump > thr
                    sig.active = False
                else:
                    sig.jump = db - base
                    was = sig.active
                    sig.active = (sig.jump > thr) and ((db - floor) > thr)
                    if sig.active:
                        any_active = True
                    if sig.active and not was:
                        self.total_emergency_hits += 1
                        self._events.put(
                            ("emergency_alert", EmergencySignal(**sig.__dict__)))
                        self._log_emergency(sig)
                    elif was and not sig.active:
                        self._events.put(("emergency_clear", name))
                self._log_scan(name, role, freq, db, base, floor, thr, sig)
            self.emergency_active = any_active
            if self._stop_event.wait(0.15):
                break

    def _log_emergency(self, sig: "EmergencySignal"):
        if not self._emerg_log:
            return
        try:
            self._emerg_log.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{sig.band},"
                f"{sig.freq:.4f},{sig.rssi:.1f},{sig.jump:.1f},{sig.trend},"
                f"{getattr(self, '_gps_lat', 0.0):.6f},"
                f"{getattr(self, '_gps_lon', 0.0):.6f}\n")
            self._emerg_log.flush()
        except Exception:
            pass

    def _log_scan(self, name, role, freq_hz, db, base, floor, thr, sig):
        """Log EVERY band sweep (not just alerts) for later threshold calibration."""
        if not self._scan_log:
            return
        try:
            self._scan_log.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{name},{role},"
                f"{freq_hz/1e6:.4f},{db:.1f},{base:.1f},{floor:.1f},"
                f"{sig.jump:.1f},{thr:.1f},{sig.trend},"
                f"{int(sig.active)},{int(self.network_present)},"
                f"{getattr(self, '_gps_lat', 0.0):.6f},"
                f"{getattr(self, '_gps_lon', 0.0):.6f}\n")
            self._scan_log.flush()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # WDG Key Hunter — 433.92 MHz car key-fob fox hunt (receive-only)
    # ------------------------------------------------------------------
    def start_key_hunt(self, loot_dir: str = "", gps_lat: float = 0.0,
                       gps_lon: float = 0.0) -> bool:
        """Start the 433.92 MHz key-fob fox hunt (needs the RTL-SDR alone)."""
        if not self.has_rtl_power():
            self._events.put(("error", "rtl_power not installed (apt install rtl-sdr)"))
            return False
        if "keyhunt" in self.mode:
            return True
        self._stop_event.clear()
        self._gps_lat = gps_lat if isinstance(gps_lat, (int, float)) else 0.0
        self._gps_lon = gps_lon if isinstance(gps_lon, (int, float)) else 0.0
        self.keyhunt = KeyHuntState(target_mhz=self._keyhunt_target_load() / 1e6)
        self.keyhunt_wf.clear()
        self.keyhunt_axis = []
        if loot_dir:
            try:
                self._keyhunt_log = open(
                    os.path.join(loot_dir, "key_hunt.csv"), "a")
                if self._keyhunt_log.tell() == 0:
                    self._keyhunt_log.write(
                        "timestamp,peak_freq_mhz,rssi_db,floor_db,jump_db,"
                        "thr_db,burst,hot,lat,lon,target_mhz,overload,other_mhz,other_db\n")
            except Exception:
                self._keyhunt_log = None
        t = threading.Thread(target=self._key_hunt_reader, daemon=True)
        t.start()
        self._threads.append(t)
        self.running = True
        self.mode = "keyhunt" if not self.mode else self.mode + "+keyhunt"
        self._events.put(("status", "Key Hunter started (433.92 MHz fox hunt)"))
        return True

    def _rtl_power_scan(self, lo: int, hi: int, binw: int, guard_hz: int):
        """One rtl_power single-shot -> (sorted [(freq_hz, db)], noise_floor_db).

        Like _rtl_power_peak but returns the FULL spectrum (for the waterfall) and
        takes an explicit DC-spike guard (key hunt wants a near-centre signal, so
        it passes a small guard, unlike the wide emergency carrier guard)."""
        import statistics
        tmp = f"/tmp/_wdg_keyhunt_{os.getpid()}.csv"
        try:
            self._emerg_proc = subprocess.Popen(
                ["rtl_power", "-f", f"{lo}:{hi}:{binw}", "-1",
                 "-i", KEY_HUNT_INTEG, "-g", KEY_HUNT_GAIN, tmp],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid)
            self._emerg_proc.wait(timeout=8)
        except Exception:
            return None
        finally:
            self._emerg_proc = None
        pts = []
        try:
            for line in open(tmp):
                p = [x.strip() for x in line.split(",")]
                if len(p) < 7:
                    continue
                try:
                    l = float(p[2]); step = float(p[4])
                except ValueError:
                    continue
                for i, x in enumerate(p[6:]):
                    try:
                        d = float(x)
                    except ValueError:
                        continue
                    if d != d:  # nan
                        continue
                    pts.append((l + step * (i + 0.5), d))
        except OSError:
            return None
        if not pts:
            return None
        pts.sort(key=lambda t: t[0])
        center = (lo + hi) / 2.0
        edge = binw
        usable = [(fq, dd) for (fq, dd) in pts
                  if abs(fq - center) > guard_hz and (lo + edge) < fq < (hi - edge)]
        if not usable:
            usable = pts
        floor = statistics.median(dd for _, dd in usable)
        return usable, floor

    # ---- fob target: persisted so a learned fob survives a restart ----
    @staticmethod
    def _keyhunt_target_path():
        base = os.environ.get("WDGO_APP_DIR") or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(base, "keyhunt_target.json")

    def _keyhunt_target_load(self) -> int:
        try:
            with open(self._keyhunt_target_path()) as f:
                hz = int(json.load(f).get("target_hz", 0))
            if KEY_HUNT_LO < hz < KEY_HUNT_HI:
                return hz
        except Exception:
            pass
        return KEY_HUNT_TARGET

    def _keyhunt_target_save(self, hz: int) -> None:
        try:
            with open(self._keyhunt_target_path(), "w") as f:
                json.dump({"target_hz": int(hz), "learned": time.strftime("%Y-%m-%d %H:%M:%S")}, f)
        except Exception:
            pass

    def keyhunt_learn(self) -> None:
        """Arm learning: the next strong burst (anywhere in the window) becomes the fob."""
        self.keyhunt.learning = True
        self._events.put(("status", "Key Hunter: press the fob now - learning its frequency"))

    def keyhunt_reset_target(self) -> None:
        self.keyhunt.target_mhz = KEY_HUNT_TARGET / 1e6
        self.keyhunt.learning = False
        self._keyhunt_target_save(KEY_HUNT_TARGET)

    def _key_hunt_reader(self):
        """Continuous rtl_power (one full-band row per second, no gaps between rows),
        a rolling background PER BIN, and a target window around the fob frequency.
        A constant carrier becomes background; a fob press is a sudden rise above it."""
        import statistics
        from collections import deque
        cmd = ["rtl_power", "-f", f"{KEY_HUNT_LO}:{KEY_HUNT_HI}:{KEY_HUNT_BIN}",
               "-i", "1", "-g", KEY_HUNT_GAIN, "-"]
        try:
            self._emerg_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                                preexec_fn=os.setsid, text=True, bufsize=1)
        except Exception as e:
            self._events.put(("error", f"rtl_power failed: {e}"))
            return
        hist = None                                  # per-bin deque of recent quiet values
        try:
            for line in self._emerg_proc.stdout:
                if self._stop_event.is_set():
                    break
                p = [x.strip() for x in line.split(",")]
                if len(p) < 8:
                    continue
                try:
                    lo, step = float(p[2]), float(p[4])
                    vals = [float(x) for x in p[6:]]
                except ValueError:
                    continue
                n = len(vals)
                freqs = [lo + step * (i + 0.5) for i in range(n)]
                if hist is None or len(hist) != n:
                    hist = [deque(maxlen=KEY_HUNT_BG_ROWS) for _ in range(n)]
                if len(hist[0]) < 3:                 # first seconds: learn the background
                    for i in range(n):
                        hist[i].append(vals[i])
                    self.keyhunt.sweeps += 1
                    continue
                bg = [statistics.median(h) for h in hist]
                rise = [vals[i] - bg[i] for i in range(n)]
                kh = self.keyhunt
                tgt = kh.target_mhz * 1e6
                win = [i for i in range(n) if abs(freqs[i] - tgt) <= KEY_HUNT_TARGET_HALF]
                out = [i for i in range(n) if i not in set(win)]
                ti = max(win, key=lambda i: rise[i]) if win else max(range(n), key=lambda i: rise[i])
                oi = max(out, key=lambda i: rise[i]) if out else ti
                band = statistics.median(rise)
                kh.overload = band > KEY_HUNT_OVERLOAD
                kh.rssi = vals[ti]
                kh.floor = bg[ti]
                kh.jump = rise[ti]
                kh.peak_freq = freqs[ti] / 1e6
                kh.other_mhz, kh.other_db = freqs[oi] / 1e6, rise[oi]
                kh.sweeps += 1
                # learning: the strongest rise anywhere (not a constant carrier: that is
                # background already) becomes the fob, if it is clearly strong
                if kh.learning:
                    li = max(range(n), key=lambda i: rise[i])
                    if rise[li] >= KEY_HUNT_LEARN_MIN and not kh.overload:
                        kh.target_mhz = freqs[li] / 1e6
                        kh.learning = False
                        self._keyhunt_target_save(int(freqs[li]))
                        self._events.put(("status", "Key Hunter: learned the fob at %.3f MHz" % kh.target_mhz))
                was = kh.burst
                kh.burst = kh.jump > KEY_HUNT_THR or kh.overload
                if kh.jump > kh.best:
                    kh.best = kh.jump
                kh.hot = (1.0 if kh.overload else
                          max(0.0, min(1.0, (kh.jump - KEY_HUNT_THR) / KEY_HUNT_HOT_SPAN)) if kh.burst else 0.0)
                if kh.burst and not was:
                    kh.hits += 1
                    kh.last_burst = time.time()
                    self._events.put(("keyhunt_burst", KeyHuntState(**kh.__dict__)))
                elif kh.burst:
                    kh.last_burst = time.time()
                # only quiet rows feed the background, so a held fob never becomes "normal"
                if not kh.burst and band < 3.0:
                    for i in range(n):
                        if rise[i] < 6.0:
                            hist[i].append(vals[i])
                self.keyhunt_axis = [f / 1e6 for f in freqs]
                self.keyhunt_wf.append(vals)
                self._log_keyhunt(kh)
        finally:
            proc, self._emerg_proc = self._emerg_proc, None
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # 433 Capture — rtl_433 raw-save every signal + decode + Flipper .sub
    # ------------------------------------------------------------------
    def start_433_grab(self, loot_dir: str = "", gps_lat: float = 0.0,
                       gps_lon: float = 0.0) -> bool:
        """Capture 433 MHz signals: rtl_433 -S all auto-saves raw I/Q (.cu8) per
        detected transmission, decodes known protocols to the sensor log, and a
        watcher converts each new .cu8 to a Flipper RAW .sub. Needs the dongle."""
        if not self.has_rtl433():
            self._events.put(("error", "rtl_433 not installed"))
            return False
        if "grab433" in self.mode:
            return True
        self._stop_event.clear()
        self._gps_lat = gps_lat if isinstance(gps_lat, (int, float)) else 0.0
        self._gps_lon = gps_lon if isinstance(gps_lon, (int, float)) else 0.0
        self.grab_count = 0
        self.grab_last = ""
        # capture dir under loot (so raw I/Q + .sub live with the rest of the loot)
        base = loot_dir or "/tmp"
        self.grab_dir = os.path.join(base, "captures",
                                     time.strftime("%Y-%m-%d_%H-%M-%S"))
        try:
            os.makedirs(self.grab_dir, exist_ok=True)
        except Exception as e:
            self._events.put(("error", f"capture dir: {e}"))
            return False
        if loot_dir and self._sensor_log is None:
            try:
                self._sensor_log = open(os.path.join(loot_dir, "sdr_sensors.csv"), "a")
                if self._sensor_log.tell() == 0:
                    self._sensor_log.write("timestamp,model,id,data,lat,lon\n")
            except Exception:
                self._sensor_log = None
        cmd = ["rtl_433", "-f", "433920000", "-F", "json", "-M", "time:utc",
               "-M", "level", "-S", "all"]
        if self._dump1090_proc:
            cmd += ["-d", "1"]
        try:
            self._grab_proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                cwd=self.grab_dir, preexec_fn=os.setsid)
        except Exception as e:
            self._events.put(("error", f"rtl_433 grab failed: {e}"))
            return False
        tr = threading.Thread(target=self._grab_reader, daemon=True)
        tr.start(); self._threads.append(tr)
        tw = threading.Thread(target=self._grab_watcher, daemon=True)
        tw.start(); self._threads.append(tw)
        self.running = True
        self.mode = "grab433" if not self.mode else self.mode + "+grab433"
        self._events.put(("status", f"433 Capture started -> {self.grab_dir}"))
        return True

    def _grab_reader(self):
        """Read rtl_433 JSON (decoded protocols) during a capture; reuse the 433
        sensor parser so decoded packets land in the sensor log + display."""
        proc = self._grab_proc
        if not proc or not proc.stdout:
            return
        while not self._stop_event.is_set():
            try:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                self._parse_rtl433_line(line.decode("utf-8", errors="replace").strip())
            except Exception:
                if self._stop_event.is_set():
                    return
                continue

    def _grab_watcher(self):
        """Watch the capture dir for new .cu8 files; convert each to a Flipper RAW
        .sub and count it. Raw .cu8 (URH) + .sub (Flipper) + decoded log = all 3."""
        seen = set()
        while not self._stop_event.is_set():
            try:
                for fn in sorted(os.listdir(self.grab_dir)):
                    if not fn.endswith(".cu8") or fn in seen:
                        continue
                    seen.add(fn)
                    path = os.path.join(self.grab_dir, fn)
                    # let rtl_433 finish writing the file before reading it
                    time.sleep(0.2)
                    label = fn
                    try:
                        from watchdogs import subghz_export
                        out, npul = subghz_export.convert_cu8(path)
                        if out:
                            label = os.path.basename(out) + f" ({npul} pulses)"
                    except Exception as e:
                        label = fn + f" (sub failed: {e})"
                    self.grab_count += 1
                    self.grab_last = label
                    self._events.put(("log", f"[GRAB] {label}"))
            except FileNotFoundError:
                pass
            except Exception:
                pass
            if self._stop_event.wait(0.5):
                break

    def _log_keyhunt(self, kh: "KeyHuntState"):
        if not self._keyhunt_log:
            return
        try:
            self._keyhunt_log.write(
                f"{time.strftime('%Y-%m-%dT%H:%M:%S')},{kh.peak_freq:.4f},"
                f"{kh.rssi:.1f},{kh.floor:.1f},{kh.jump:.1f},{KEY_HUNT_THR:.1f},"
                f"{int(kh.burst)},{kh.hot:.2f},"
                f"{getattr(self, '_gps_lat', 0.0):.6f},"
                f"{getattr(self, '_gps_lon', 0.0):.6f},{kh.target_mhz:.4f},{int(kh.overload)},"
                f"{kh.other_mhz:.4f},{kh.other_db:.1f}\n")
            self._keyhunt_log.flush()
        except Exception:
            pass

    def stop(self):
        """Stop all SDR subprocesses."""
        self._stop_event.set()

        for proc, name in [
            (self._dump1090_proc, "dump1090"),
            (self._rtl433_proc, "rtl_433"),
            (self._emerg_proc, "rtl_power"),
            (self._grab_proc, "rtl_433_grab"),
        ]:
            if proc:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    proc.wait(timeout=3)
                except Exception:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except Exception:
                        pass

        self._dump1090_proc = None
        self._rtl433_proc = None
        self._emerg_proc = None
        self._grab_proc = None

        for f in (self._adsb_log, self._sensor_log, self._emerg_log,
                  self._scan_log, self._keyhunt_log):
            if f:
                try:
                    f.close()
                except Exception:
                    pass
        self._adsb_log = None
        self._sensor_log = None
        self._emerg_log = None
        self._scan_log = None
        self._keyhunt_log = None

        self.emergency_active = False
        self.network_present = False
        self.running = False
        self.mode = ""

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        return events

    def prune_stale(self):
        """Remove aircraft/sensors not seen recently."""
        now = time.time()
        stale_ac = [k for k, v in self.aircraft.items()
                    if now - v.last_seen > ADSB_TIMEOUT]
        for k in stale_ac:
            del self.aircraft[k]

        stale_s = [k for k, v in self.sensors.items()
                   if now - v.last_seen > SENSOR_TIMEOUT]
        for k in stale_s:
            del self.sensors[k]

    def update_gps(self, lat: float, lon: float):
        """Update GPS position for tagging new sensors / logs. Ignores None so a
        pre-fix position never reaches the loggers (would raise on "{None:.6f}"
        and silently leave 0-byte logs)."""
        if isinstance(lat, (int, float)):
            self._gps_lat = lat
        if isinstance(lon, (int, float)):
            self._gps_lon = lon

    # ------------------------------------------------------------------
    # SBS BaseStation format parser (ADS-B via dump1090 port 30003)
    # ------------------------------------------------------------------
    def _sbs_reader(self):
        """Connect to dump1090 SBS output and parse aircraft data."""
        import socket
        time.sleep(2)  # wait for dump1090 to start

        while not self._stop_event.is_set():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(5)
                sock.connect(("127.0.0.1", 30003))
                sock.settimeout(2)
                buf = ""
                while not self._stop_event.is_set():
                    try:
                        chunk = sock.recv(4096).decode("ascii", errors="ignore")
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self._parse_sbs_line(line.strip())
                    except socket.timeout:
                        continue
                    except Exception:
                        break
                sock.close()
            except Exception:
                if self._stop_event.is_set():
                    return
                time.sleep(3)  # retry connection

    def _parse_sbs_line(self, line: str):
        """Parse SBS BaseStation format line.

        Format: MSG,type,ses,id,icao,id,date,time,date,time,
                callsign,alt,speed,heading,lat,lon,vrate,squawk,...
        """
        parts = line.split(",")
        if len(parts) < 11 or parts[0] != "MSG":
            return

        msg_type = parts[1].strip()
        icao = parts[4].strip().upper()
        if not icao:
            return

        now = time.time()
        is_new = icao not in self.aircraft
        ac = self.aircraft.setdefault(icao, Aircraft(icao=icao))
        ac.last_seen = now

        try:
            if msg_type == "1" and len(parts) > 10:
                cs = parts[10].strip()
                if cs:
                    ac.callsign = cs
            elif msg_type == "3" and len(parts) > 15:
                alt = parts[11].strip()
                spd = parts[12].strip()
                hdg = parts[13].strip()
                lat = parts[14].strip()
                lon = parts[15].strip()
                if alt:
                    ac.altitude = int(float(alt))
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
                if lat and lon:
                    ac.lat = float(lat)
                    ac.lon = float(lon)
            elif msg_type == "4" and len(parts) > 13:
                spd = parts[12].strip()
                hdg = parts[13].strip()
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
            elif msg_type == "6" and len(parts) > 17:
                sq = parts[17].strip()
                if sq:
                    ac.squawk = sq
        except (ValueError, IndexError):
            pass

        had_pos_before = not is_new and ac.has_position and msg_type != "3"

        if is_new:
            self.total_aircraft_seen += 1
            label = ac.callsign or icao
            self._events.put(("aircraft_new", ac))
            pos = f"pos:{ac.lat:.3f},{ac.lon:.3f}" if ac.has_position else "waiting for position..."
            self._events.put(("log",
                              f"[ADS-B] New: {label} "
                              f"alt:{ac.altitude}ft {pos}"))
            ac._logged_pos = ac.has_position
        elif msg_type == "3" and ac.has_position and not getattr(ac, '_logged_pos', False):
            # First position fix for this aircraft
            label = ac.callsign or icao
            self._events.put(("log",
                              f"[ADS-B] {label} located: "
                              f"{ac.lat:.3f},{ac.lon:.3f} "
                              f"alt:{ac.altitude}ft spd:{ac.speed}kt"))
            ac._logged_pos = True

        # Log to CSV - a TRACK log: one row per new position (dump1090 repeats the last
        # position in many messages; logging each of them made the file ~16x larger)
        key = (round(ac.lat, 5), round(ac.lon, 5), ac.altitude)
        if ac.has_position and self._adsb_log and getattr(ac, "_log_key", None) != key:
            ac._log_key = key
            try:
                self._adsb_log.write(
                    f"{int(now)},{icao},{ac.callsign},{ac.lat:.6f},"
                    f"{ac.lon:.6f},{ac.altitude},{ac.speed},"
                    f"{ac.heading},{ac.squawk}\n")
                self._adsb_log.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # rtl_433 JSON parser (433 MHz ISM sensors)
    # ------------------------------------------------------------------
    def _rtl433_reader(self):
        """Read rtl_433 JSON output line by line."""
        proc = self._rtl433_proc
        if not proc or not proc.stdout:
            return

        while not self._stop_event.is_set():
            try:
                line = proc.stdout.readline()
                if not line:
                    if proc.poll() is not None:
                        break
                    continue
                self._parse_rtl433_line(
                    line.decode("utf-8", errors="replace").strip())
            except Exception:
                if self._stop_event.is_set():
                    return
                continue

    def _parse_rtl433_line(self, line: str):
        """Parse a JSON line from rtl_433."""
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        model = data.get("model", "Unknown")
        sid_num = data.get("id", data.get("channel", ""))
        sid = f"{model}_{sid_num}"
        now = time.time()

        is_new = sid not in self.sensors
        sensor = self.sensors.setdefault(sid, Sensor433(
            model=model, sid=sid,
            lat=getattr(self, "_gps_lat", 0.0),
            lon=getattr(self, "_gps_lon", 0.0),
        ))
        sensor.data = data
        sensor.last_seen = now
        sensor.count += 1

        if is_new:
            self.total_sensors_seen += 1
            self._events.put(("sensor_new", sensor))

        # Build display string
        display_parts = [f"[433] {model}"]
        for key in ("temperature_C", "humidity", "pressure_hPa",
                     "battery_ok", "wind_avg_km_h", "rain_mm"):
            if key in data:
                short = key.split("_")[0]
                display_parts.append(f"{short}:{data[key]}")
        rssi = data.get("rssi")
        if rssi is not None:
            display_parts.append(f"RSSI:{rssi}")
        self._events.put(("log", " ".join(display_parts)))

        # Log to CSV
        if self._sensor_log:
            try:
                safe_data = json.dumps(data, ensure_ascii=True)
                self._sensor_log.write(
                    f"{int(now)},{model},{sid_num},"
                    f"\"{safe_data}\","
                    f"{sensor.lat:.6f},{sensor.lon:.6f}\n")
                self._sensor_log.flush()
            except Exception:
                pass
