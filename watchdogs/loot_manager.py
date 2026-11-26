"""Loot manager — persists all captured data to disk.

Session directory layout:
    <app_dir>/loot/<YYYY-MM-DD_HH-MM-SS>/
        serial_full.log          – every line from ESP32 (timestamped)
        scan_results.csv         – networks found during scan
        sniffer_aps.csv          – access points from sniffer
        sniffer_probes.csv       – probe requests captured
        handshakes/
            <ssid>_<bssid>.txt   – handshake metadata from serial
            <ssid>_<bssid>.pcap  – real pcap (from start_handshake_serial)
            <ssid>_<bssid>.hccapx – hashcat format (from start_handshake_serial)
            <ssid>_<bssid>.22000 – hc22000 hash (auto-generated from complete hccapx)
        portal_passwords.log     – portal form submissions
        evil_twin_capture.log    – evil twin captured data
        attacks.log              – attack start/stop events
"""

import base64
import csv
import io
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from .app_state import AppState, Network, SnifferAP, ProbeEntry
from . import wigle

log = logging.getLogger(__name__)


@dataclass
class _SidecarFix:
    """Minimal GpsFix stand-in built from a capture's .gps.json sidecar, so an
    archive rebuild tags a file with the position recorded WHEN it was captured,
    never the device's position now."""
    latitude: float = 0.0
    longitude: float = 0.0
    altitude: float = 0.0
    valid: bool = True


def _fsync_file(fh, strict: bool = False) -> None:
    """Flush Python buffer + OS page cache to physical disk.

    Without this, `with open(...).write(...)` only pushes bytes down to the
    OS cache. If the uConsole battery dies before Linux lazily writes that
    cache back to the eMMC/SD, the data is gone. fsync makes the write
    survive a hard power cut. Costs ~5–20 ms on SD — cheap per event,
    expensive per frame, so call it only from discrete save-on-event paths.

    strict=True re-raises an fsync/flush failure so a durable-write path does NOT
    proceed to rename/commit data it could not actually persist (a silently
    swallowed fsync error used to be reported to the caller as success).
    """
    try:
        fh.flush()
        os.fsync(fh.fileno())
    except OSError:
        if strict:
            raise


def _sync_append(path: Path, text: str, encoding: str = "utf-8",
                 newline: Optional[str] = None) -> None:
    """Append text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "a", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


def _sync_write(path: Path, text: str, encoding: str = "utf-8",
                newline: Optional[str] = None) -> None:
    """Write text to path and fsync. Silently ignores I/O errors."""
    try:
        kwargs = {"encoding": encoding}
        if newline is not None:
            kwargs["newline"] = newline
        with open(path, "w", **kwargs) as fh:
            fh.write(text)
            _fsync_file(fh)
    except OSError:
        pass


class LootManager:
    """Manages a per-session loot directory and auto-saves captured data."""

    @property
    def session_dir(self) -> str:
        return str(self._session)

    def __init__(self, app_dir: str, gps_manager=None) -> None:
        self._gps = gps_manager  # Optional GpsManager for geo-tagging
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._base = Path(app_dir) / "loot"
        self._session = self._base / ts
        self._serial_fh: Optional[io.TextIOWrapper] = None
        self._handshake_dir: Optional[Path] = None
        self._session_active = False

        # Wardriving CSV in-memory index — avoids re-parsing the whole file on
        # every observation. A repeat that is not stronger is an O(1) dict lookup,
        # a new BSSID is an O(1) append, only a stronger sighting rewrites. Seeded
        # once per session (the session dir is fixed for this manager's lifetime).
        self._wd_lock = threading.Lock()
        self._wd_lines: Optional[List[str]] = None   # full file cache, lazy
        self._wd_index: dict = {}                     # mac -> (line_idx, rssi)
        self._wd_nopos: set = set()                   # macs whose row has no GPS position yet

        # Serialises MeshCore node CSV read-modify-write (dedup + position upgrade).
        self._mc_lock = threading.Lock()

        # Guards every mutation of self._db AND its serialisation to disk. The game
        # loop, the portal thread (badges) and the MeshCore callback all touch the
        # DB; without one shared lock two writers could interleave a dict mutation
        # with json.dump (corrupt file) or race on a shared temp file. RLock so a
        # mutator may hold it across its own _save_db call.
        self._db_lock = threading.RLock()

        # Handshake metadata parser state (from serial log keywords)
        self._hs_buffer: List[str] = []
        self._hs_collecting = False

        # PCAP base64 parser state (from start_handshake_serial command)
        # Firmware outputs:
        #   --- PCAP BEGIN ---
        #   <base64 lines>
        #   --- PCAP END ---
        #   PCAP_SIZE: <N>
        #   --- HCCAPX BEGIN ---
        #   <base64 lines>
        #   --- HCCAPX END ---
        #   SSID: <ssid>  AP: <bssid>
        self._pcap_collecting = False
        self._pcap_b64_lines: List[str] = []
        self._hccapx_collecting = False
        self._hccapx_b64_lines: List[str] = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"

        # Aggregate loot database
        self._db_path = self._base / "loot_db.json"
        self._db: dict = {}
        self._session_key = ts

        try:
            self._session.mkdir(parents=True, exist_ok=True)
            # Private to this user: handshakes, credentials and GPS tracks live
            # here, so no other local account may enter the session directory.
            try:
                os.chmod(self._session, 0o700)
            except OSError:
                pass
            (self._session / "handshakes").mkdir(exist_ok=True)
            try:
                os.chmod(self._session / "handshakes", 0o700)
            except OSError:
                pass
            self._handshake_dir = self._session / "handshakes"
            self._serial_fh = open(
                self._session / "serial_full.log", "a", encoding="utf-8"
            )
            self._session_active = True
            log.info("Loot session: %s", self._session)
        except OSError as exc:
            log.error("Cannot create loot directory: %s", exc)

        # Load or build aggregate loot database
        self._db = self._load_or_build_db()

        # Periodic background sync — protects against power loss between
        # event-driven fsyncs. Every BACKUP_INTERVAL seconds it fsyncs the
        # open serial log handle and calls os.sync() so everything still
        # sitting in the OS page cache gets pushed to the SD card.
        self._backup_stop = False
        self._backup_thread = threading.Thread(
            target=self._periodic_backup_loop,
            name="loot-backup",
            daemon=True,
        )
        self._backup_thread.start()

    BACKUP_INTERVAL = 30  # seconds between sync passes

    def _periodic_backup_loop(self) -> None:
        """Run on a daemon thread; fsync the serial log and os.sync the FS.

        os.sync() is a syscall that schedules all dirty pages across every
        mounted filesystem to be written out. It can take ~100 ms–2 s on a
        busy SD card, which is why it runs off the game loop thread.
        """
        while not self._backup_stop:
            # Use short sleeps so shutdown is responsive
            for _ in range(self.BACKUP_INTERVAL):
                if self._backup_stop:
                    return
                time.sleep(1)
            self.flush_all()

    def flush_all(self) -> None:
        """Force every buffered write to reach the SD card.

        Safe to call from any thread. Called automatically every
        BACKUP_INTERVAL seconds; also hookable for pre-shutdown flushes.
        """
        fh = self._serial_fh
        if fh is not None:
            try:
                fh.flush()
                os.fsync(fh.fileno())
            except (OSError, ValueError):
                pass  # Handle may be closed mid-call; ignore.
        try:
            os.sync()
        except AttributeError:
            pass  # Non-POSIX — nothing else we can do.

    @property
    def session_path(self) -> str:
        return str(self._session)

    @property
    def active(self) -> bool:
        return self._session_active

    @property
    def loot_root(self) -> Path:
        """Root loot directory (contains all session dirs)."""
        return self._base

    # ------------------------------------------------------------------
    # Aggregate loot database
    # ------------------------------------------------------------------

    def _load_or_build_db(self) -> dict:
        """Load loot_db.json, or rebuild from session dirs if missing/corrupt."""
        if self._db_path.is_file():
            try:
                with open(self._db_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                # Require the current schema version — a bump forces a one-time
                # rebuild so new per-session counters (e.g. emergency) populate.
                if (isinstance(data, dict) and data.get("version") == 2
                        and "sessions" in data):
                    log.info("Loot DB loaded: %d sessions", len(data.get("sessions", {})))
                    if not data.get("adsb_unique"):
                        self._fix_adsb_counts(data)
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Loot DB corrupted (%s), rebuilding", exc)
        return self._rebuild_db()

    def _fix_adsb_counts(self, db: dict) -> None:
        """One-time fix: older DBs stored ADS-B as rows of the track log, not aircraft.
        Recount only that field per session (XP, badges and everything else stay as
        they are), recompute the totals and remember that it was done."""
        for name, counts in db.get("sessions", {}).items():
            f = self._base / name / "adsb_aircraft.csv"
            if isinstance(counts, dict) and counts.get("adsb") and f.is_file():
                try:
                    with open(f, encoding="utf-8", errors="replace") as fh:
                        next(fh, None)
                        counts["adsb"] = len({p[1].strip().upper() for p in (ln.split(",", 2) for ln in fh)
                                              if len(p) > 1 and p[1].strip()})
                except OSError:
                    pass
        self._recalc_totals(db)
        db["adsb_unique"] = True
        self._save_db(db)
        log.info("Loot DB: ADS-B counts converted to distinct aircraft")

    def _rebuild_db(self) -> dict:
        """Scan all loot session directories and build the DB from scratch.

        Also generates .22000 files retroactively for any .hccapx that
        doesn't already have a corresponding .22000 file.
        """
        db: dict = {"version": 2, "sessions": {}, "totals": {}, "adsb_unique": True}
        if not self._base.is_dir():
            return db
        for entry in sorted(self._base.iterdir()):
            if not entry.is_dir():
                continue
            name = entry.name
            if len(name) >= 19 and name[4] == "-" and name[7] == "-" and name[10] == "_":
                self._retroactive_22000(entry)
                db["sessions"][name] = self._scan_session_dir(entry)
        self._recalc_totals(db)
        self._save_db(db)
        log.info("Loot DB rebuilt: %d sessions", len(db["sessions"]))
        return db

    def _retroactive_22000(self, session_path: Path) -> None:
        """Generate .22000 for any .hccapx without a matching .22000.

        This runs on rebuild, possibly long after capture, so it must NOT use the
        live GPS fix (that would stamp an old handshake with today's location).
        It uses only the capture's own .gps.json sidecar; with no sidecar the
        .22000 is written without a GPS header (#15)."""
        from .hc22000 import convert_hccapx_to_22000
        hs_dir = session_path / "handshakes"
        if not hs_dir.is_dir():
            return
        try:
            for f in hs_dir.iterdir():
                if f.suffix == ".hccapx" and not f.with_suffix(".22000").exists():
                    convert_hccapx_to_22000(f, gps_fix=self._sidecar_gps(f))
        except OSError:
            pass

    def _scan_session_dir(self, session_path: Path) -> dict:
        """Count loot items in a single session directory."""
        counts = {k: 0 for k in (
            "pcap", "hccapx", "hc22000", "passwords", "et_captures",
            "mc_nodes", "mc_messages", "bt_devices", "bt_airtags", "bt_smarttags",
            "bt_devices_gps", "wardriving", "adsb", "mitm_pcaps", "emergency")}
        hs_dir = session_path / "handshakes"
        if hs_dir.is_dir():
            try:
                for f in hs_dir.iterdir():
                    if f.suffix == ".pcap":
                        counts["pcap"] += 1
                    elif f.suffix == ".hccapx":
                        counts["hccapx"] += 1
                    elif f.suffix == ".22000":
                        counts["hc22000"] += 1
            except OSError:
                pass
        pw_file = session_path / "portal_passwords.log"
        if pw_file.is_file():
            try:
                # Only count lines that contain captured POST form data —
                # the log file also stores diagnostic events (client connects,
                # channel switches) which shouldn't inflate the password
                # count on the loot screen.
                with open(pw_file, encoding="utf-8") as f:
                    counts["passwords"] = sum(
                        1 for line in f
                        if "received post" in line.lower()
                        or "form submission" in line.lower())
            except OSError:
                pass
        et_file = session_path / "evil_twin_capture.log"
        if et_file.is_file():
            try:
                # Same filter as portal_passwords.log above — keep raw events
                # in the file for forensics but only count actual captures.
                with open(et_file, encoding="utf-8") as f:
                    counts["et_captures"] = sum(
                        1 for line in f
                        if "received post" in line.lower()
                        or "form submission" in line.lower())
            except OSError:
                pass
        mc_nodes_file = session_path / "meshcore_nodes.csv"
        if mc_nodes_file.is_file():
            try:
                lines = sum(1 for _ in open(mc_nodes_file, encoding="utf-8"))
                counts["mc_nodes"] = max(0, lines - 1)  # minus header
            except OSError:
                pass
        mc_msgs_file = session_path / "meshcore_messages.log"
        if mc_msgs_file.is_file():
            try:
                counts["mc_messages"] = sum(1 for _ in open(mc_msgs_file, encoding="utf-8"))
            except OSError:
                pass
        # Emergency-vehicle RF alerts (one row per keyup detection; skip header)
        emerg_file = session_path / "emergency_rf.csv"
        if emerg_file.is_file():
            try:
                lines = sum(1 for _ in open(emerg_file, encoding="utf-8"))
                counts["emergency"] = max(0, lines - 1)
            except OSError:
                pass
        bt_dev_file = session_path / "bt_devices.csv"
        if bt_dev_file.is_file():
            try:
                gps_count = 0
                total = 0
                for i, line in enumerate(open(bt_dev_file, encoding="utf-8")):
                    if i == 0:
                        continue  # skip header
                    total += 1
                    parts = line.strip().split(",")
                    # lat,lon are last two columns
                    if len(parts) >= 8:
                        try:
                            lat = float(parts[-2])
                            lon = float(parts[-1])
                            if lat != 0.0 or lon != 0.0:
                                gps_count += 1
                        except ValueError:
                            pass
                counts["bt_devices"] = total
                counts["bt_devices_gps"] = gps_count
            except OSError:
                pass
        bt_at_file = session_path / "bt_airtag.log"
        if bt_at_file.is_file():
            try:
                total_at = 0
                total_st = 0
                for line in open(bt_at_file, encoding="utf-8"):
                    if "AirTags:" in line:
                        try:
                            part = line.split("AirTags:")[1].split("|")[0].strip()
                            total_at = max(total_at, int(part))
                        except (ValueError, IndexError):
                            pass
                    if "SmartTags:" in line:
                        try:
                            part = line.split("SmartTags:")[1].strip()
                            total_st = max(total_st, int(part))
                        except (ValueError, IndexError):
                            pass
                counts["bt_airtags"] = total_at
                counts["bt_smarttags"] = total_st
            except OSError:
                pass
        wd_file = session_path / "wardriving.csv"
        if wd_file.is_file():
            try:
                lines = sum(1 for _ in open(wd_file, encoding="utf-8"))
                counts["wardriving"] = max(0, lines - 2)  # minus pre-header + header
            except OSError:
                pass
        # ADS-B: count distinct aircraft (ICAO), not track rows - the SDR log
        # appends one row per position update, so rows far outnumber aircraft.
        adsb_file = session_path / "adsb_aircraft.csv"
        if adsb_file.is_file():
            icaos = set()
            try:
                with open(adsb_file, encoding="utf-8", errors="replace") as fh:
                    next(fh, None)  # skip header
                    for ln in fh:
                        parts = ln.split(",", 2)
                        if len(parts) > 1 and parts[1].strip():
                            icaos.add(parts[1].strip().upper())
            except OSError:
                pass
            counts["adsb"] = len(icaos)
        mitm_dir = session_path / "mitm"
        if mitm_dir.is_dir():
            try:
                counts["mitm_pcaps"] = sum(
                    1 for f in mitm_dir.iterdir() if f.suffix == ".pcap"
                )
            except OSError:
                pass
        return counts

    def _recalc_totals(self, db: dict) -> None:
        """Recalculate totals from all session entries."""
        keys = ("pcap", "hccapx", "hc22000", "passwords", "et_captures",
                "mc_nodes", "mc_messages", "bt_devices", "bt_airtags", "bt_smarttags",
                "bt_devices_gps", "wardriving", "adsb", "emergency")
        totals: dict = {k: 0 for k in keys}
        totals["sessions"] = len(db["sessions"])
        for session_counts in db["sessions"].values():
            for k in keys:
                totals[k] += session_counts.get(k, 0)
        db["totals"] = totals

    def _save_db(self, db: dict) -> None:
        """Write the DB to loot_db.json atomically. fsync'd before rename so
        a power cut between tmp write and rename can't leave a zero-byte
        loot_db.json (the rename is atomic on the same filesystem).

        Held under _db_lock so the dict can't be mutated by another thread during
        json.dump, and each write uses its OWN temp file (mkstemp) so two
        concurrent writers never clobber a shared ``.tmp``."""
        with self._db_lock:
            tmpname = None
            try:
                fd, tmpname = tempfile.mkstemp(
                    dir=str(self._db_path.parent), prefix=".loot_db.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(db, fh, indent=2)
                    _fsync_file(fh, strict=True)
                os.replace(tmpname, self._db_path)
                tmpname = None
            except OSError as exc:
                log.error("Cannot save loot DB: %s", exc)
            finally:
                if tmpname is not None:
                    try:
                        os.unlink(tmpname)
                    except OSError:
                        pass

    def update_session_loot(self) -> None:
        """Rescan current session and update aggregate DB."""
        if not self._session_active:
            return
        with self._db_lock:
            self._db["sessions"][self._session_key] = self._scan_session_dir(self._session)
            self._recalc_totals(self._db)
            self._save_db(self._db)

    @property
    def loot_totals(self) -> dict:
        """Aggregate totals across all sessions, including cracked passwords."""
        totals = self._db.get("totals", {})
        if "cracked" not in totals:
            totals["cracked"] = self.cracked_count
        return totals

    @property
    def cracked_count(self) -> int:
        """Count of cracked passwords from WPA-sec potfile JSON."""
        if not hasattr(self, "_cracked_cache"):
            self._cracked_cache: int | None = None
        if self._cracked_cache is not None:
            return self._cracked_cache
        json_path = self._base / "passwords" / "wpasec_cracked.json"
        if json_path.is_file():
            try:
                with open(json_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                self._cracked_cache = data.get("count", 0)
                return self._cracked_cache
            except (json.JSONDecodeError, OSError):
                pass
        self._cracked_cache = 0
        return 0

    def invalidate_cracked_cache(self) -> None:
        """Force reload of cracked count on next access."""
        self._cracked_cache = None

    # ------------------------------------------------------------------
    # Persistent XP
    # ------------------------------------------------------------------

    def load_xp(self) -> int:
        """Load persisted XP from loot_db.json."""
        return self._db.get("xp", 0)

    def save_xp(self, xp: int) -> None:
        """Persist current XP to loot_db.json."""
        with self._db_lock:
            if self._db.get("xp") != xp:
                self._db["xp"] = xp
                self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent badges
    # ------------------------------------------------------------------

    def load_badges(self) -> set[str]:
        """Load earned badges from loot_db.json."""
        return set(self._db.get("badges", []))

    def save_badge(self, badge: str) -> None:
        """Add a badge and persist to loot_db.json."""
        with self._db_lock:
            badges = set(self._db.get("badges", []))
            if badge not in badges:
                badges.add(badge)
                self._db["badges"] = sorted(badges)
                self._save_db(self._db)

    # ------------------------------------------------------------------
    # Persistent MeshCore contacts
    # ------------------------------------------------------------------

    def load_contacts(self) -> dict[str, dict]:
        """Load MeshCore contacts from loot_db.json.

        Returns dict keyed by node_id with fields:
        id, type, name, lat, lon, rssi, snr, last_seen, first_seen, note
        """
        return dict(self._db.get("mc_contacts", {}))

    def save_contact(self, node_id: str, data: dict) -> bool:
        """Save or update a MeshCore contact. Returns True if new contact."""
        with self._db_lock:
            contacts = self._db.setdefault("mc_contacts", {})
            is_new = node_id not in contacts
            if is_new:
                data.setdefault("first_seen", time.time())
                data.setdefault("note", "")
            else:
                # Preserve first_seen and note from existing
                data["first_seen"] = contacts[node_id].get("first_seen", time.time())
                data["note"] = contacts[node_id].get("note", "")
            contacts[node_id] = data
            self._save_db(self._db)
            return is_new

    def save_contact_note(self, node_id: str, note: str) -> None:
        """Update note for a MeshCore contact."""
        with self._db_lock:
            contacts = self._db.setdefault("mc_contacts", {})
            if node_id in contacts:
                contacts[node_id]["note"] = note
                self._save_db(self._db)

    def delete_contact(self, node_id: str) -> None:
        """Remove a MeshCore contact from loot_db."""
        with self._db_lock:
            contacts = self._db.get("mc_contacts", {})
            if node_id in contacts:
                del contacts[node_id]
                self._save_db(self._db)

    def calculate_xp_from_loot(self) -> int:
        """Calculate XP from all historical loot data.

        Used for initial XP bootstrap when no XP has been saved yet.
        """
        t = self._db.get("totals", {})
        xp = 0
        xp += t.get("wardriving", 0) * 15       # WiFi networks
        xp += t.get("bt_devices", 0) * 10        # BLE devices
        xp += t.get("pcap", 0) * 200             # Handshake captures
        xp += t.get("et_captures", 0) * 150      # Evil Twin credentials
        xp += t.get("passwords", 0) * 150         # Portal passwords
        xp += self.cracked_count * 150             # Cracked passwords
        return xp

    def get_known_devices(self) -> tuple[set, set]:
        """Load all known BLE MACs and WiFi BSSIDs from all loot sessions.

        Returns (known_ble_macs, known_wifi_bssids).
        """
        import csv as _csv

        ble_macs: set[str] = set()
        wifi_bssids: set[str] = set()

        if not self._base.is_dir():
            return ble_macs, wifi_bssids

        for entry in self._base.iterdir():
            if not entry.is_dir() or not entry.name[0].isdigit():
                continue

            # WiFi BSSIDs from wardriving.csv
            wd = entry / "wardriving.csv"
            if wd.is_file():
                try:
                    with open(wd, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0]:
                                wifi_bssids.add(row[0].strip().upper())
                except Exception:
                    pass

            # BLE MACs from bt_devices.csv
            bt = entry / "bt_devices.csv"
            if bt.is_file():
                try:
                    with open(bt, "r", encoding="utf-8", errors="replace") as f:
                        reader = _csv.reader(f)
                        for row in reader:
                            if len(row) >= 1 and ":" in row[0] and row[0] != "mac":
                                ble_macs.add(row[0].strip().upper())
                except Exception:
                    pass

        return ble_macs, wifi_bssids

    # ------------------------------------------------------------------
    # Full serial log
    # ------------------------------------------------------------------

    def log_serial(self, line: str) -> None:
        """Append a timestamped serial line to the full log."""
        if not self._serial_fh:
            return
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        try:
            self._serial_fh.write(f"[{ts}] {line}\n")
            self._serial_fh.flush()
        except OSError:
            pass

        # Check for handshake metadata in serial stream
        self._detect_handshake(line)
        # Check for pcap base64 data from start_handshake_serial
        self._detect_pcap_stream(line)

    # ------------------------------------------------------------------
    # Handshake detection from serial stream
    # ------------------------------------------------------------------

    # Keywords that START handshake collection
    _HS_START_KW = ("message pair:", "message_pair:", "ANonce present")

    # Keywords that belong to a handshake block (keep collecting)
    _HS_RELATED_KW = (
        "message pair:", "message_pair:", "ANonce present", "SNonce present",
        "Key MIC", "AP MAC:", "STA MAC:", "EAPOL data:", "SSID:",
        "HANDSHAKE IS COMPLETE", "HANDSHAKE IS VALID",
        "Handshake #", "captured!", "Created ", "Failed ",
        "Cycle:", "networks captured", "attack cleanup",
        "cleanup complete", "task finished",
        "=====",  # separator lines
    )

    # Keywords that END handshake collection (save after this line)
    _HS_END_KW = ("task finished", "cleanup complete")

    def _detect_handshake(self, line: str) -> None:
        """Collect handshake metadata from serial and save when complete.

        Collects from 'message pair:' through 'task finished', saves ONCE.
        """
        stripped = line.strip()

        # Start collecting on handshake start indicators
        if not self._hs_collecting:
            if any(kw in stripped for kw in self._HS_START_KW):
                self._hs_collecting = True
                self._hs_buffer = [stripped]
            return

        # We are collecting — check if this line belongs to the block
        if any(kw in stripped for kw in self._HS_RELATED_KW) or not stripped:
            self._hs_buffer.append(stripped)
            # Check for end marker
            if any(kw in stripped for kw in self._HS_END_KW):
                self._save_handshake_buffer()
        else:
            # Unrelated line — save what we have and stop
            self._save_handshake_buffer()

    def _save_handshake_buffer(self) -> None:
        """Write collected handshake metadata to a file."""
        if not self._hs_buffer or not self._handshake_dir:
            self._hs_collecting = False
            self._hs_buffer = []
            return

        # Extract SSID and BSSID for filename
        ssid = "unknown"
        bssid = "unknown"
        for line in self._hs_buffer:
            if "SSID:" in line:
                parts = line.split("SSID:")
                if len(parts) > 1:
                    ssid = parts[1].strip().split()[0].strip(",")
            if "AP MAC:" in line:
                parts = line.split("AP MAC:")
                if len(parts) > 1:
                    bssid = parts[1].strip().replace(":", "")[:12]

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(c if c.isalnum() or c in "-_" else "_" for c in ssid)
        filename = f"{safe_ssid}_{bssid}_{ts}.txt"
        filepath = self._handshake_dir / filename

        try:
            with open(filepath, "w", encoding="utf-8") as fh:
                fh.write(f"# Handshake captured at {datetime.now().isoformat()}\n")
                fh.write(f"# SSID: {ssid}\n")
                fh.write(f"# BSSID: {bssid}\n")
                fh.write(self._gps_header_lines())
                fh.write("\n")
                for line in self._hs_buffer:
                    fh.write(line + "\n")
                _fsync_file(fh)
            log.info("Handshake saved: %s", filepath)
            self._save_gps_sidecar(filepath)
            self.update_session_loot()
        except OSError as exc:
            log.error("Cannot save handshake: %s", exc)

        self._hs_collecting = False
        self._hs_buffer = []

    # ------------------------------------------------------------------
    # PCAP base64 stream parser (start_handshake_serial output)
    # ------------------------------------------------------------------

    def _detect_pcap_stream(self, line: str) -> None:
        """Parse base64-encoded pcap/hccapx blocks from serial stream.

        Triggered by start_handshake_serial firmware command which outputs:
            --- PCAP BEGIN ---
            <base64 lines>
            --- PCAP END ---
            PCAP_SIZE: <N>
            --- HCCAPX BEGIN ---
            <base64 lines>
            --- HCCAPX END ---
            SSID: <ssid>  AP: <bssid>
        """
        stripped = line.strip()

        # PCAP block
        if "--- PCAP BEGIN ---" in stripped:
            self._pcap_collecting = True
            self._pcap_b64_lines = []
            return

        if self._pcap_collecting:
            if "--- PCAP END ---" in stripped:
                self._pcap_collecting = False
            else:
                # Collect only valid base64 chars
                clean = stripped.replace(" ", "")
                if clean:
                    self._pcap_b64_lines.append(clean)
            return

        # HCCAPX block
        if "--- HCCAPX BEGIN ---" in stripped:
            self._hccapx_collecting = True
            self._hccapx_b64_lines = []
            return

        if self._hccapx_collecting:
            if "--- HCCAPX END ---" in stripped:
                self._hccapx_collecting = False
            else:
                clean = stripped.replace(" ", "")
                if clean:
                    self._hccapx_b64_lines.append(clean)
            return

        # Metadata line: firmware prints SSID and AP MAC after the blocks
        if "SSID:" in stripped and "AP:" in stripped:
            try:
                parts = stripped.split("SSID:")
                if len(parts) > 1:
                    rest = parts[1].strip()
                    if "AP:" in rest:
                        ssid_part, ap_part = rest.split("AP:", 1)
                        self._pcap_meta_ssid = ssid_part.strip()
                        self._pcap_meta_bssid = ap_part.strip().replace(":", "")[:12]
                    else:
                        self._pcap_meta_ssid = rest.strip().split()[0]
            except Exception:
                pass
            # Save when we have both pcap and hccapx (or at least pcap)
            if self._pcap_b64_lines:
                self._save_pcap_from_b64()
            return

        # Fallback: if we got hccapx end and pcap is ready, save after short wait
        # (in case firmware doesn't print SSID/AP line)
        if self._pcap_b64_lines and self._hccapx_b64_lines and not self._hccapx_collecting:
            # Check if this is an unrelated line that signals end of block
            if not any(c in stripped for c in ("BEGIN", "END", "SIZE:", "PCAP", "HCCAPX")):
                self._save_pcap_from_b64()

    def _save_pcap_from_b64(self) -> None:
        """Decode and save accumulated base64 pcap/hccapx data as binary files."""
        if not self._handshake_dir or not self._pcap_b64_lines:
            self._pcap_b64_lines = []
            self._hccapx_b64_lines = []
            self._pcap_meta_ssid = "unknown"
            self._pcap_meta_bssid = "unknown"
            return

        ts = datetime.now().strftime("%H%M%S")
        safe_ssid = "".join(
            c if c.isalnum() or c in "-_" else "_"
            for c in self._pcap_meta_ssid
        )
        base_name = f"{safe_ssid}_{self._pcap_meta_bssid}_{ts}"

        # Save .pcap — fsync'd, this is unrecoverable capture data
        try:
            pcap_data = base64.b64decode("".join(self._pcap_b64_lines))
            pcap_path = self._handshake_dir / f"{base_name}.pcap"
            with open(pcap_path, "wb") as fh:
                fh.write(pcap_data)
                _fsync_file(fh)
            log.info("PCAP saved: %s (%d bytes)", pcap_path, len(pcap_data))
            self._save_gps_sidecar(pcap_path)
        except Exception as exc:
            log.error("Cannot save PCAP: %s", exc)

        # Save .hccapx (if present) — fsync'd
        if self._hccapx_b64_lines:
            try:
                hccapx_data = base64.b64decode("".join(self._hccapx_b64_lines))
                hccapx_path = self._handshake_dir / f"{base_name}.hccapx"
                with open(hccapx_path, "wb") as fh:
                    fh.write(hccapx_data)
                    _fsync_file(fh)
                log.info("HCCAPX saved: %s (%d bytes)",
                         hccapx_path, len(hccapx_data))
                self._save_gps_sidecar(hccapx_path)
                # Generate .22000 from complete handshakes
                self._try_generate_22000(hccapx_path)
            except Exception as exc:
                log.error("Cannot save HCCAPX: %s", exc)

        # Reset state and update DB
        self._pcap_b64_lines = []
        self._hccapx_b64_lines = []
        self._pcap_meta_ssid = "unknown"
        self._pcap_meta_bssid = "unknown"
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS sidecar (Pwnagotchi-compatible .gps.json)
    # ------------------------------------------------------------------

    def _valid_gps_fix(self):
        """Current GPS fix ONLY if it is present and fresh, else None. Gates on the
        manager's staleness-aware has_valid_fix (falls back to available+valid for a
        manager without it) so a frozen last-known position after GPS loss never
        geo-tags new loot (#10)."""
        gps = self._gps
        if not gps:
            return None
        fresh = getattr(gps, "has_valid_fix", None)
        if fresh is None:
            # Older manager without staleness ageing
            if not getattr(gps, "available", False) or not gps.fix.valid:
                return None
        elif not fresh:
            return None
        return gps.fix

    def _save_gps_sidecar(self, base_path: Path) -> None:
        """Write a .gps.json sidecar alongside a capture file.

        Creates e.g. MyWiFi_AABB_143022.pcap.gps.json with raw GPS fix.
        Loot always contains full (unmasked) data.
        """
        fix = self._valid_gps_fix()
        if not fix:
            return
        geo_path = base_path.parent / (base_path.name + ".gps.json")
        try:
            data = {
                "Latitude": round(fix.latitude, 7),
                "Longitude": round(fix.longitude, 7),
                "Altitude": round(fix.altitude, 1),
                "Date": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "Satellites": fix.satellites,
                "HDOP": round(fix.hdop, 1),
            }
            with open(geo_path, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            log.info("GPS sidecar: %s", geo_path)
        except Exception as exc:
            log.error("Cannot save GPS sidecar: %s", exc)

    def _gps_header_lines(self) -> str:
        """Return GPS header lines for .txt handshake files, or empty string."""
        fix = self._valid_gps_fix()
        if not fix:
            return ""
        return (
            f"# GPS: {fix.latitude:.7f}, {fix.longitude:.7f}\n"
            f"# Alt: {fix.altitude:.1f}m  Sat: {fix.satellites}  HDOP: {fix.hdop:.1f}\n"
        )

    # ------------------------------------------------------------------
    # HC22000 generation
    # ------------------------------------------------------------------

    def _try_generate_22000(self, hccapx_path: Path) -> None:
        """Generate a .22000 file from an HCCAPX captured NOW, tagging it with the
        current (fresh) GPS fix."""
        from .hc22000 import convert_hccapx_to_22000

        convert_hccapx_to_22000(hccapx_path, gps_fix=self._valid_gps_fix())

    def _sidecar_gps(self, capture_path: Path):
        """GPS fix recorded WITH a capture (its ``<name>.gps.json`` sidecar), or
        None. Used on archive rebuild so an old file's .22000 header carries the
        position from when it was captured — never the device's position today."""
        sidecar = capture_path.parent / (capture_path.name + ".gps.json")
        if not sidecar.is_file():
            return None
        try:
            d = json.loads(sidecar.read_text(encoding="utf-8"))
            return _SidecarFix(
                latitude=float(d["Latitude"]),
                longitude=float(d["Longitude"]),
                altitude=float(d.get("Altitude", 0.0)),
                valid=True,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    # ------------------------------------------------------------------
    # Scan results
    # ------------------------------------------------------------------

    # WiGLE AuthMode mapping
    _AUTH_MAP: dict[str, str] = {
        "OPEN": "[ESS]",
        "WEP": "[WEP][ESS]",
        "WPA": "[WPA-PSK-CCMP+TKIP][ESS]",
        "WPA2": "[WPA2-PSK-CCMP][ESS]",
        "WPA3": "[WPA3-SAE-CCMP][ESS]",
        "WPA/WPA2": "[WPA-PSK-CCMP+TKIP][WPA2-PSK-CCMP][ESS]",
        "WPA2/WPA3": "[WPA2-PSK-CCMP][WPA3-SAE-CCMP][ESS]",
    }

    # WiGLE 1.6 (14 columns) — same layout the projectZero firmware streams, so a
    # serial-wardrive row from the XIAO node appends to this file verbatim. Readers are
    # header-aware (watchdogs/wigle.py), so legacy 1.4 files still parse.
    _WIGLE_PRE_HEADER = wigle.PRE_HEADER
    _WIGLE_HEADER = wigle.HEADER

    def _wigle_auth(self, auth: str) -> str:
        """Convert ESP32 auth string to WiGLE AuthMode format."""
        return self._AUTH_MAP.get(auth.strip(), f"[{auth}][ESS]")

    def _gps_tag(self):
        """Host GPS fix as (lat, lon, alt, accuracy), zeros if no (fresh) fix."""
        lat = lon = alt = accuracy = 0.0
        fix = self._valid_gps_fix()
        if fix:
            lat = round(fix.latitude, 7)
            lon = round(fix.longitude, 7)
            alt = round(fix.altitude, 1)
            accuracy = round(fix.hdop * 5.0, 1) if fix.hdop < 99 else 0.0
        return lat, lon, alt, accuracy

    @staticmethod
    def _atomic_write_lines(path: Path, lines) -> None:
        """Replace `path` with `lines` atomically: write a temp file in the same
        directory, fsync it, rename over the original, fsync the directory. A crash
        or full disk mid-write leaves the old file intact instead of a truncated one."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            with open(tmp, "w", newline="", encoding="utf-8") as fh:
                fh.writelines(lines)
                _fsync_file(fh, strict=True)   # a failed fsync must abort the commit
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            dfd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass

    @staticmethod
    def _row_has_pos(parts) -> bool:
        """True when a WiGLE row carries a real position: BOTH coordinates present,
        finite and in range. A row with one empty coordinate (e.g. lat blank, lon
        20) is NOT positioned — otherwise it would read as 0,20."""
        try:
            slat = str(parts[wigle.IDX["CurrentLatitude"]]).strip()
            slon = str(parts[wigle.IDX["CurrentLongitude"]]).strip()
        except (IndexError, KeyError):
            return False
        if not slat or not slon:
            return False
        try:
            lat = float(slat)
            lon = float(slon)
        except ValueError:
            return False
        return not (lat == 0.0 and lon == 0.0) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0

    @staticmethod
    def _carry_first_seen(old_line: str, new_row: str) -> str:
        """Return new_row with its FirstSeen replaced by old_line's, so replacing a
        row with a stronger sighting keeps the ORIGINAL first-detection time. If the
        old value is missing/blank, new_row is returned unchanged."""
        fs = wigle.IDX["FirstSeen"]
        try:
            old_parts = next(csv.reader([old_line]))
            new_parts = next(csv.reader([new_row]))
        except Exception:
            return new_row
        if fs >= len(old_parts) or fs >= len(new_parts):
            return new_row
        old_fs = old_parts[fs].strip()
        if not old_fs:
            return new_row
        new_parts[fs] = old_parts[fs]
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(new_parts)
        return buf.getvalue()

    def _wd_load_index(self, path: Path, rssi_idx: int) -> None:
        """Seed the in-memory wardrive cache from disk ONCE (handles a pre-existing
        session file on resume). After this, writes never re-read the file. Line
        indices in self._wd_index match positions in self._wd_lines, headers included.

        Raises OSError if the file EXISTS but cannot be read — the caller must then
        leave the cache uninitialised and retry later, never treat the session as
        empty (that would overwrite an unreadable-but-present route with just a
        header + the new row). A genuinely absent file is the normal empty case."""
        lines: List[str] = []
        index: dict = {}
        nopos: set = set()
        if path.is_file():
            with open(path, "r", encoding="utf-8", newline="") as fh:
                for i, line in enumerate(fh):
                    lines.append(line)
                    if wigle.is_pre_header(line) or wigle.is_header(line):
                        continue
                    try:
                        parts = next(csv.reader([line]))
                    except Exception:
                        continue
                    if not parts:
                        continue
                    try:
                        index[parts[0]] = (i, int(parts[rssi_idx]))
                    except (ValueError, IndexError):
                        index[parts[0]] = (i, -100)
                    if not self._row_has_pos(parts):
                        nopos.add(parts[0])
        self._wd_lines = lines
        self._wd_index = index
        self._wd_nopos = nopos

    def _wardrive_upsert(self, key_mac: str, new_row: str, rssi_val: int) -> bool:
        """Dedup wardriving.csv by MAC (column 0): one row per BSSID, the strongest
        sighting wins. Replace the row in place when the new RSSI is stronger, else append.

        Backed by an in-memory index so the hot paths cost no disk read: a repeat that
        is not stronger is an O(1) dict lookup (the common case while driving past known
        APs), a new BSSID is an O(1) append, and only a genuinely stronger sighting
        rewrites the file. The file is seeded once per session. Header-aware + comma-safe,
        and fsync'd on every disk write (research data must survive a hard power loss).
        Returns True if a row was written."""
        path = self._session / "wardriving.csv"
        rssi_idx = wigle.IDX["RSSI"]
        try:
            has_pos = self._row_has_pos(next(csv.reader([new_row])))
        except Exception:
            has_pos = False
        with self._wd_lock:
            if self._wd_lines is None:
                try:
                    self._wd_load_index(path, rssi_idx)
                except OSError as exc:
                    # Existing file unreadable: keep the cache uninitialised and
                    # retry next sighting rather than overwrite the route (#4).
                    log.error("wardriving.csv unreadable, keeping route pending: %s", exc)
                    return False
            prev = self._wd_index.get(key_mac)
            if prev is not None:
                old_idx, old_rssi = prev
                old_nopos = key_mac in self._wd_nopos
                # A row saved without a GPS fix (indoors, cold GPS) takes the first
                # sighting that has a position, whatever its RSSI; a positioned row is
                # never traded for one without a position.
                if not (has_pos and old_nopos):
                    if rssi_val <= old_rssi:
                        return False  # existing observation is stronger or equal
                    if not has_pos and not old_nopos:
                        return False
                # Preserve the original FirstSeen when a stronger/positioned sighting
                # replaces the row — the first detection time must not jump forward
                # to "now" just because the signal got stronger later (#25).
                new_row = self._carry_first_seen(self._wd_lines[old_idx], new_row)
                new_lines = list(self._wd_lines)
                new_lines[old_idx] = new_row
                try:
                    self._atomic_write_lines(path, new_lines)
                except OSError as exc:
                    log.error("Cannot update wardriving CSV: %s", exc)
                    return False      # disk unchanged -> index unchanged
                self._wd_lines = new_lines
                self._wd_index[key_mac] = (old_idx, rssi_val)
                if has_pos:
                    self._wd_nopos.discard(key_mac)
                return True
            # new BSSID — O(1) append, no full rewrite
            fresh = not self._wd_lines
            try:
                if fresh:
                    self._atomic_write_lines(
                        path, [self._WIGLE_PRE_HEADER, self._WIGLE_HEADER, new_row])
                else:
                    try:
                        size_before = path.stat().st_size
                    except OSError:
                        size_before = None
                    try:
                        with open(path, "a", newline="", encoding="utf-8") as fh:
                            fh.write(new_row)
                            _fsync_file(fh, strict=True)   # fsync fail -> treat as not written
                    except OSError:
                        if size_before is not None:     # drop a half-written tail line
                            try:
                                os.truncate(path, size_before)
                            except OSError:
                                pass
                        raise
            except OSError as exc:
                log.error("Cannot save wardriving row: %s", exc)
                return False          # not on disk -> not in the index either
            if fresh:
                self._wd_lines = [self._WIGLE_PRE_HEADER, self._WIGLE_HEADER]
            self._wd_index[key_mac] = (len(self._wd_lines), rssi_val)
            self._wd_lines.append(new_row)
            if not has_pos:
                self._wd_nopos.add(key_mac)
            return True

    def save_wardriving_network(self, network: Network) -> bool:
        """Append a geo-tagged network to wardriving.csv (WiGLE 1.6, dedup by BSSID).

        Returns True if the network was new or updated (stronger RSSI).
        """
        if not self._session_active or not network.bssid:
            return False
        lat, lon, alt, accuracy = self._gps_tag()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            rssi_val = int(network.rssi)
        except (ValueError, TypeError):
            rssi_val = -100
        new_row = wigle.row16(network.bssid, network.ssid or "",
                              self._wigle_auth(network.auth), ts, network.channel,
                              network.rssi, lat, lon, alt, accuracy, rtype="WIFI")
        return self._wardrive_upsert(network.bssid, new_row, rssi_val)

    def save_wardriving_bt(self, mac: str, rssi: int, name: str) -> bool:
        """Append a geo-tagged BLE device to wardriving.csv (WiGLE 1.6, dedup by MAC)."""
        if not self._session_active or not mac:
            return False
        lat, lon, alt, accuracy = self._gps_tag()
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            rssi_val = int(rssi)
        except (ValueError, TypeError):
            rssi_val = -100
        new_row = wigle.row16(mac, name or "", "[BLE]", ts, "", rssi,
                              lat, lon, alt, accuracy, rtype="BLE")
        return self._wardrive_upsert(mac, new_row, rssi_val)

    def append_wardrive_wigle(self, raw_row: str) -> bool:
        """CORE path: a node (XIAO) streamed a full WiGLE 1.6 row over serial with empty
        GPS/time columns. Stamp the host's GPS fix + current time into that row and upsert
        by MAC. The node already did the promiscuous capture + dedup; the host only tags
        and persists. Returns True if a row was written."""
        if not self._session_active:
            return False
        try:
            parts = next(csv.reader([raw_row.strip()]))
        except Exception:
            return False
        I = wigle.IDX
        if len(parts) < len(wigle.HEADER_COLS) or ":" not in parts[0]:
            return False
        lat, lon, alt, accuracy = self._gps_tag()
        parts[I["FirstSeen"]] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts[I["CurrentLatitude"]] = f"{lat}"
        parts[I["CurrentLongitude"]] = f"{lon}"
        parts[I["AltitudeMeters"]] = f"{alt}"
        parts[I["AccuracyMeters"]] = f"{accuracy}"
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(parts)
        try:
            rssi_val = int(parts[I["RSSI"]])
        except (ValueError, IndexError):
            rssi_val = -100
        return self._wardrive_upsert(parts[0], buf.getvalue(), rssi_val)

    def save_scan_results(self, networks: List[Network]) -> None:
        """Save scan results as CSV. fsync'd."""
        if not self._session_active or not networks:
            return
        filepath = self._session / "scan_results.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["#", "SSID", "BSSID", "Channel", "Auth",
                                 "RSSI", "Band", "Vendor"])
                for n in networks:
                    writer.writerow([
                        n.index, n.ssid, n.bssid, n.channel,
                        n.auth, n.rssi, n.band, n.vendor,
                    ])
                _fsync_file(fh)
            log.info("Scan results saved: %d networks", len(networks))
        except OSError as exc:
            log.error("Cannot save scan results: %s", exc)

    # ------------------------------------------------------------------
    # Sniffer results
    # ------------------------------------------------------------------

    def save_sniffer_aps(self, aps: List[SnifferAP]) -> None:
        """Save sniffer AP results as CSV. fsync'd."""
        if not self._session_active or not aps:
            return
        filepath = self._session / "sniffer_aps.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "Channel", "Clients", "Client_MACs"])
                for ap in aps:
                    writer.writerow([
                        ap.ssid, ap.channel, ap.client_count,
                        ";".join(ap.clients),
                    ])
                _fsync_file(fh)
            log.info("Sniffer APs saved: %d", len(aps))
        except OSError as exc:
            log.error("Cannot save sniffer APs: %s", exc)

    def save_sniffer_probes(self, probes: List[ProbeEntry]) -> None:
        """Save probe requests as CSV. fsync'd."""
        if not self._session_active or not probes:
            return
        filepath = self._session / "sniffer_probes.csv"
        try:
            with open(filepath, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["SSID", "MAC"])
                for p in probes:
                    writer.writerow([p.ssid, p.mac])
                _fsync_file(fh)
            log.info("Sniffer probes saved: %d", len(probes))
        except OSError as exc:
            log.error("Cannot save sniffer probes: %s", exc)

    # ------------------------------------------------------------------
    # Portal
    # ------------------------------------------------------------------

    def save_portal_event(self, line: str) -> None:
        """Append a portal password/form submission line. fsync'd — this is
        captured-credential data we can't afford to lose on power failure."""
        if not self._session_active:
            return
        filepath = self._session / "portal_passwords.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Evil Twin
    # ------------------------------------------------------------------

    def save_evil_twin_event(self, line: str) -> None:
        """Append an evil twin capture line. fsync'd — captured credentials."""
        if not self._session_active:
            return
        filepath = self._session / "evil_twin_capture.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {line}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # Attack events
    # ------------------------------------------------------------------

    def log_attack_event(self, event: str) -> None:
        """Log attack start/stop/result events. fsync'd."""
        if not self._session_active:
            return
        filepath = self._session / "attacks.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(filepath, f"[{ts}] {event}\n")

    # ------------------------------------------------------------------
    # MeshCore
    # ------------------------------------------------------------------

    _MC_HEADER = "timestamp,node_id,type,name,lat,lon,rssi,snr\n"

    @staticmethod
    def _mc_row_has_pos(row: list) -> bool:
        """True when a meshcore CSV row (node_id at 1, lat at 4, lon at 5) carries a
        real position: both present and not 0,0."""
        if len(row) < 6:
            return False
        slat, slon = row[4].strip(), row[5].strip()
        if not slat or not slon:
            return False
        try:
            lat, lon = float(slat), float(slon)
        except ValueError:
            return False
        return not (lat == 0.0 and lon == 0.0)

    def save_meshcore_node(self, node_id: str, node_type: str, name: str,
                           lat: float, lon: float, rssi: float, snr: float) -> None:
        """Upsert a node into meshcore_nodes.csv, keyed on the node_id COLUMN (not a
        substring match, which could collide with a node name). A node first heard
        without a position and later re-announced WITH one upgrades the stored row,
        so the located announcement is not lost behind the earlier position-less
        one (#22). fsync'd."""
        if not self._session_active:
            return
        path = self._session / "meshcore_nodes.csv"
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        new_row = [ts, node_id, node_type, name, lat, lon, rssi, snr]
        new_has_pos = self._mc_row_has_pos([str(c) for c in new_row])
        with self._mc_lock:
            try:
                rows: list = []
                have_header = False
                if path.is_file():
                    with open(path, "r", encoding="utf-8", newline="") as fh:
                        reader = csv.reader(fh)
                        for i, r in enumerate(reader):
                            if i == 0 and r and r[0].strip().lower() == "timestamp":
                                have_header = True
                                continue
                            if r:
                                rows.append(r)
                existing_idx = next(
                    (i for i, r in enumerate(rows) if len(r) > 1 and r[1] == node_id),
                    None)
                if existing_idx is not None:
                    # Known node: only rewrite to upgrade a position-less row to a
                    # located one; otherwise leave the file untouched.
                    if new_has_pos and not self._mc_row_has_pos(rows[existing_idx]):
                        rows[existing_idx] = [str(c) for c in new_row]
                        out = io.StringIO()
                        out.write(self._MC_HEADER)
                        csv.writer(out, lineterminator="\n").writerows(rows)
                        self._atomic_write_lines(path, [out.getvalue()])
                        self.update_session_loot()
                    return
                # New node: create-with-header or append one row.
                if not have_header and not path.is_file():
                    _sync_write(path, self._MC_HEADER)
                buf = io.StringIO()
                csv.writer(buf, lineterminator="\n").writerow(new_row)
                _sync_append(path, buf.getvalue())
                self.update_session_loot()
            except OSError:
                pass

    def save_meshcore_message(self, channel: str, message: str, rssi: float) -> None:
        """Append message to meshcore_messages.log. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "meshcore_messages.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] [{channel}] {message} (RSSI:{rssi})\n")
        self.update_session_loot()

    def save_wdgwars_message(self, nick: str, message: str) -> None:
        """Append a WDGWars #general chat message to wdgwars_chat.log."""
        if not self._session_active:
            return
        path = self._session / "wdgwars_chat.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] {nick}: {message}\n")

    # ------------------------------------------------------------------
    # Bluetooth
    # ------------------------------------------------------------------

    def save_bt_device(self, mac: str, rssi: int, name: str,
                       is_airtag: bool, is_smarttag: bool) -> None:
        """Append BLE device to bt_devices.csv (dedup by MAC). fsync'd."""
        if not self._session_active:
            return
        path = self._session / "bt_devices.csv"
        # Get GPS coords only from a fresh fix
        lat, lon = 0.0, 0.0
        fix = self._valid_gps_fix()
        if fix:
            lat = round(fix.latitude, 7)
            lon = round(fix.longitude, 7)
        if path.is_file():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            if f",{mac}," in existing or existing.startswith(f"{mac},"):
                return  # already known
        else:
            _sync_write(path,
                        "timestamp,mac,rssi,name,airtag,smarttag,lat,lon\n")
        ts = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
        # csv.writer so a device name containing a comma stays in ONE field and
        # cannot shift lat/lon into the wrong columns (#24).
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerow(
            [ts, mac, rssi, name, is_airtag, is_smarttag, lat, lon])
        _sync_append(path, buf.getvalue())
        self.update_session_loot()

    def save_bt_airtag_event(self, airtags: int, smarttags: int) -> None:
        """Log an AirTag scanner detection event. fsync'd."""
        if not self._session_active:
            return
        path = self._session / "bt_airtag.log"
        ts = datetime.now().strftime("%H:%M:%S")
        _sync_append(path, f"[{ts}] AirTags:{airtags} | SmartTags:{smarttags}\n")
        self.update_session_loot()

    # ------------------------------------------------------------------
    # GPS point collection (for Map tab)
    # ------------------------------------------------------------------

    _gps_points_cache: list[dict] | None = None
    _gps_points_ts: float = 0.0

    def get_gps_points(self) -> list[dict]:
        """Collect all GPS-tagged loot across all sessions.

        Returns list of dicts: {lat, lon, type, label}.
        Cached for 30 seconds to avoid excessive FS scans.
        """
        now = time.monotonic()
        if self._gps_points_cache is not None and now - self._gps_points_ts < 30.0:
            return self._gps_points_cache

        points: list[dict] = []
        if not self._base.is_dir():
            self._gps_points_cache = points
            self._gps_points_ts = now
            return points

        for session_dir in self._base.iterdir():
            if not session_dir.is_dir():
                continue

            # Wardriving CSV (WiGLE format) — header-aware (1.4 and 1.6)
            wd_file = session_dir / "wardriving.csv"
            if wd_file.is_file():
                try:
                    for parts, cmap in (wigle.parse_file(wd_file) or []):
                        try:
                            lat = float(wigle.get(parts, cmap, "CurrentLatitude", "0") or 0)
                            lon = float(wigle.get(parts, cmap, "CurrentLongitude", "0") or 0)
                        except (ValueError, IndexError):
                            continue
                        if lat == 0.0 and lon == 0.0:
                            continue
                        ptype = "bt" if wigle.get(parts, cmap, "Type").strip().upper() == "BLE" else "wifi"
                        points.append({
                            "lat": lat, "lon": lon,
                            "type": ptype,
                            "label": wigle.get(parts, cmap, "SSID"),
                            "bssid": wigle.get(parts, cmap, "MAC"),
                            "auth": wigle.get(parts, cmap, "AuthMode"),
                            "rssi": wigle.get(parts, cmap, "RSSI"),
                            "channel": wigle.get(parts, cmap, "Channel"),
                        })
                except OSError:
                    pass

            # BT devices CSV (only WiFi + BT on map, no handshake/meshcore).
            # Layout: timestamp,mac,rssi,name,airtag,smarttag,lat,lon — head (3)
            # and tail (airtag,smarttag,lat,lon = 4) are fixed; the name sits in
            # between. Parse by the fixed ends so a legacy unquoted comma in the
            # name (extra columns) still yields the right lat/lon (#24).
            bt_file = session_dir / "bt_devices.csv"
            if bt_file.is_file():
                try:
                    with open(bt_file, "r", encoding="utf-8", newline="") as fh:
                        reader = csv.reader(fh)
                        for i, row in enumerate(reader):
                            if i == 0 and row and row[0].strip().lower() == "timestamp":
                                continue  # header
                            if len(row) < 8:
                                continue
                            try:
                                lat = float(row[-2])
                                lon = float(row[-1])
                            except (ValueError, TypeError):
                                continue
                            if lat == 0.0 and lon == 0.0:
                                continue
                            mac = row[1] if len(row) > 1 else ""
                            name = ",".join(row[3:-4]) if len(row) > 7 else ""
                            points.append({
                                "lat": lat, "lon": lon,
                                "type": "bt",
                                "label": name or mac or "?",
                                "bssid": mac,
                                "auth": "[BLE]",
                                "rssi": row[2] if len(row) > 2 else "",
                                "channel": "",
                            })
                except OSError:
                    pass

        self._gps_points_cache = points
        self._gps_points_ts = now
        return points

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close file handles, write session summary, and update loot DB."""
        # Stop background sync thread before closing the handle it touches
        self._backup_stop = True
        self.flush_all()

        if self._serial_fh:
            try:
                self._serial_fh.close()
            except OSError:
                pass
            self._serial_fh = None

        # Write session summary
        if self._session_active:
            try:
                summary = self._session / "session_info.txt"
                with open(summary, "w", encoding="utf-8") as fh:
                    fh.write(f"Session ended: {datetime.now().isoformat()}\n")
                    # List files in session
                    for f in sorted(self._session.rglob("*")):
                        if f.is_file() and f.name != "session_info.txt":
                            size = f.stat().st_size
                            fh.write(f"  {f.relative_to(self._session)} "
                                     f"({size} bytes)\n")
                    _fsync_file(fh)
            except OSError:
                pass

            # Final DB update + one last OS sync so every file on the card
            # reflects our final state.
            self.update_session_loot()
            try:
                os.sync()
            except AttributeError:
                pass
