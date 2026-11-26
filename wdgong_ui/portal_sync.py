"""WDGWars portal sync for WatchDogsGo-NG (raylib).

A standalone, UI-agnostic WiGLE/loot uploader that the raylib backend drives
through callables. NG does not use a plugin system; this module owns the whole
upload path itself.

What it does (server contract):
  - POST <server>/api/upload/  — JSON {data: base64, nonce, sig} signed with
    HMAC-SHA256(key, nonce + data_b64); header X-API-Key: <64-hex>.
  - Uploads per loot session: WiFi/BLE networks, ADS-B aircraft, MeshCore nodes.
  - Pulls badges the server grants back; GET /api/me for identity/stats.

Two things are NG-specific (agreed with the portal side):
  - User-Agent product token is ``WatchDogsGo-NG`` so the portal can tell this client
    traffic apart for the "uConsole Operators" ranking. Bench/dev runs on a Mac
    report ``(Darwin; raylib)`` and the server filters them out of the ranking.
  - The upload payload carries ``game_xp`` (declared-by-game XP, never ranked).

Networks are parsed **header-aware** via ``watchdogs.wigle`` — the loot format is
now WiGLE 1.6, which inserts a ``Frequency`` column, so the OG fixed-index parser
(written for 1.4) would read RSSI/lat/lon from the wrong columns.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import platform as _plat
import secrets as _secrets
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler

from watchdogs import wigle

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://wdgwars.pl/api/upload/"


class _SameOriginRedirect(HTTPRedirectHandler):
    """Follow a redirect ONLY if it stays on the same https scheme+host+port.

    Every authenticated request carries ``X-API-Key``; urllib replays custom
    headers across redirects, so a redirect to another host (or an http
    downgrade) would hand the portal key to a different server. Any cross-origin
    or non-https redirect is refused here instead of followed (#2)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if new.scheme != "https" or (new.scheme, new.hostname, new.port) != (
                old.scheme, old.hostname, old.port):
            raise HTTPError(
                req.full_url, code,
                "refused cross-origin/insecure redirect to %s (API key not forwarded)"
                % (new.hostname or "?"), headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# Shared opener: all authenticated calls go through _open(), which uses this, so
# none of them can be redirected off-origin with the key attached.
_SECURE_OPENER = build_opener(_SameOriginRedirect())

# User-Agent: the portal parses product + version with ^WatchDogsGo(-NG)?/([0-9]...)
# and routes uConsole traffic into its own ranking. Keep the product token stable
# ("WatchDogsGo-NG") — renaming it silently drops us from the ranking.
def _detect_platform() -> str:
    """Real hardware for the UA platform field so the portal can tell a uConsole
    from a Raspberry Pi or a plain Linux box (they were all reporting 'uConsole').
    uConsole/ClockworkPi -> 'uConsole', other Pi -> 'RPi', other Linux -> 'Linux',
    non-Linux (dev) -> the OS name (e.g. 'Darwin', filtered out of the ranking)."""
    if _plat.system() != "Linux":
        return _plat.system()
    # ClockworkPi uConsole is a Raspberry Pi Compute Module whose device-tree model
    # only says "Raspberry Pi ..."; its boot config loads the uConsole overlay, so
    # look for an active clockworkpi-uconsole line before falling back to the model.
    for _cfg in ("/boot/firmware/config.txt", "/boot/config.txt"):
        try:
            with open(_cfg, "r", errors="ignore") as _cf:
                for _ln in _cf:
                    _s = _ln.strip()
                    if _s and not _s.startswith("#") and "clockworkpi-uconsole" in _s:
                        return "uConsole"
        except Exception:
            pass
    model = ""
    for _p in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
        try:
            with open(_p, "rb") as _f:
                model = _f.read().decode("utf-8", "ignore").replace("\x00", "").strip().lower()
            if model:
                break
        except Exception:
            pass
    if "uconsole" in model or "clockwork" in model:
        return "uConsole"
    if "raspberry pi" in model:
        return "RPi"
    return "Linux"


# The portal ranks uConsole, Raspberry Pi and other Linux players; keep the hardware
# kind honest so each is ranked fairly (a Mac dev run stays 'Darwin', filtered out).
_PLATFORM = _detect_platform()


def _user_agent(version: str, tag: str = "") -> str:
    # Platform hint carries a per-device tag (e.g. 'uConsole-W4GF32') so the portal
    # can tell individual uConsoles apart; still starts with WatchDogsGo-NG/<ver>
    # (the portal's product/version regex) and keeps the platform word for its
    # bench (Darwin) filter.
    plat = ("%s-%s" % (_PLATFORM, tag)) if tag else _PLATFORM
    return f"WatchDogsGo-NG/{version} ({plat}; raylib)"


# Badge ID mapping: game badge -> server badge (mirrors the OG plugin).
BADGE_GAME_TO_SERVER = {
    "wardriver": "wardriver",
    "handshake_hunter": "handshake_hunter",
    "evil_twin": "evil_twin",
    "meshcore": "mesh_first",
    "wpasec_uploader": "wpa_sec_user",
    "flipper": "flipper_user",
    "skywatch": "skywatch",
    "iot_hunter": "iot_hunter",
}
BADGE_SERVER_TO_GAME = {v: k for k, v in BADGE_GAME_TO_SERVER.items()}

AUTH_MAP = {
    "[WPA2-PSK]": "WPA2", "[WPA2]": "WPA2", "[WPA2-EAP]": "WPA2-EAP",
    "[WPA-PSK]": "WPA", "[WPA]": "WPA",
    "[WPA3-SAE]": "WPA3", "[SAE]": "WPA3",
    "[ESS]": "OPEN", "[OPEN]": "OPEN", "": "OPEN",
    "[WEP]": "WEP",
}


def _map_auth(wigle_auth: str) -> str:
    """Convert a WiGLE auth string like '[WPA2-PSK]' to the server's simple form."""
    if not wigle_auth:
        return "OPEN"
    result = AUTH_MAP.get(wigle_auth)
    if result:
        return result
    a = wigle_auth.upper()
    if "WPA3" in a or "SAE" in a:
        return "WPA3"
    if "WPA2" in a:
        return "WPA2"
    if "WPA" in a:
        return "WPA"
    if "WEP" in a:
        return "WEP"
    if "OPEN" in a or "ESS" in a:
        return "OPEN"
    return wigle_auth.strip("[]")


def _load_secrets_conf() -> dict:
    """Load key=value pairs from the repo-root secrets.conf (gitignored)."""
    result: dict = {}
    path = Path(__file__).resolve().parent.parent / "secrets.conf"
    if path.is_file():
        try:
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
        except Exception:
            pass
    return result


# Tokens that mark an UNFILLED template line in secrets.conf.example — never nag
# the user to "uncomment" one of these, they haven't filled it in yet.
_KEY_PLACEHOLDERS = ("your-", "-here", "xxxx", "<", "example", "...")


def commented_key_value(key: str, conf_path: Optional[Path] = None) -> str:
    """If `key` has NO active (uncommented) line but a COMMENTED line carries a
    real-looking value (not the shipped template placeholder), return that value;
    otherwise "". Lets a "no key" message instead say "you set KEY but left it
    commented out" — a `#`-prefixed key reads as absent and silently disables the
    feature, which is a confusing dead end (seen in the field for map keys)."""
    path = conf_path or (Path(__file__).resolve().parent.parent / "secrets.conf")
    try:
        if not path.is_file():
            return ""
        active = False
        commented = ""
        for raw in path.read_text(encoding="utf-8").splitlines():
            s = raw.strip()
            if not s:
                continue
            if s.startswith("#"):
                body = s.lstrip("#").strip()
                if body.startswith(key + "="):
                    v = body.split("=", 1)[1].strip()
                    if v and not any(p in v.lower() for p in _KEY_PLACEHOLDERS):
                        commented = v
            elif s.split("=", 1)[0].strip() == key:
                active = True
        return "" if active else commented
    except Exception:
        return ""


def _in_bounds(lat: float, lon: float) -> bool:
    if lat == 0.0 and lon == 0.0:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


# --- Batching (parameters agreed with the portal side,
# A large session is split into batches so one request never trips the shared
# host limits, and a partial failure only re-sends the missing batches.
#   - 5000 records / batch: the server imports APs in chunks of 500, so a batch
#     is ~10 chunks = seconds; smaller buys nothing (fixed per-request cost),
#     bigger holds DB locks longer and loses more on failure.
#   - aircraft + meshcore count into the SAME record budget (portal note #1).
#   - raw-JSON byte cap sized so the base64 signed body stays well under 4 MiB
#     (base64 is ~+33%); at 5000 records the byte cap almost never binds.
BATCH_MAX_RECORDS = 5000
BATCH_MAX_BYTES = 3 * 1024 * 1024        # raw data-row JSON; signed body < 4 MiB
BATCH_SPLIT_MIN = 50                      # don't halve below this on a 413 burst
UPLOAD_KEYS_MAX = 500                     # keys per /api/upload-keys request
# Receipt fields summed across a session's batches. 'total' is a global DB count
# and is deliberately NOT summed (portal note #6).
_AGG_FIELDS = ("imported", "updated", "duplicates", "cooldown", "cap_hits",
               "aircraft_imported", "aircraft_already_seen",
               "meshcore_imported", "meshcore_rejected",
               "no_gps", "bad_rows")


class _EmptyAnswer(OSError):
    """A 2xx answer without a readable body: delivery is unconfirmed, so the batch
    stays pending and is re-sent (the idempotency key makes that safe)."""


class _BatchPending(Exception):
    """A batch could not be confirmed this run (timeout / 409-still-importing /
    retries exhausted). The session stays PENDING and resumes its unsent batches
    on the next sync via /api/upload-keys — never marked uploaded without a
    receipt, never re-imported twice (the idempotency key guards duplicates)."""


def _split_combined(seq) -> tuple:
    """Turn a tagged ``[("n"|"a"|"m", obj), ...]`` stream back into the three
    row lists (networks, aircraft, meshcore_nodes), preserving order."""
    nets = [x for t, x in seq if t == "n"]
    ac = [x for t, x in seq if t == "a"]
    mc = [x for t, x in seq if t == "m"]
    return nets, ac, mc


def _halve(nets, ac, mc) -> tuple:
    """Split one batch's rows roughly in half (combined record stream) for the
    413-burst fallback. Returns two (nets, ac, mc) triples."""
    combined = ([("n", x) for x in nets] + [("a", x) for x in ac]
                + [("m", x) for x in mc])
    mid = len(combined) // 2 or 1
    return _split_combined(combined[:mid]), _split_combined(combined[mid:])


class PortalSync:
    """Headless WDGWars uploader driven by the raylib backend.

    The backend supplies accessors so this stays decoupled from the UI:
      log(msg, kind)       -> terminal line; kind in {tx, ok, warn, err, raw}
      active_session()     -> name of the currently-recording loot dir ("" if none)
      get_game_badges()    -> set of game badge ids
      apply_badge(game_id) -> import a server-granted badge into the game
      get_xp()             -> current declared XP (int) or None
    """

    def __init__(
        self,
        *,
        version: str,
        loot_base: Path,
        state_path: Path,
        active_session: Callable[[], str],
        log: Callable[[str, str], None],
        get_game_badges: Optional[Callable[[], set]] = None,
        apply_badge: Optional[Callable[[str], None]] = None,
        get_xp: Optional[Callable[[], Optional[int]]] = None,
        node_tag: str = "",
    ) -> None:
        self._version = version or "0.0.0"
        self._ua = _user_agent(self._version, node_tag)
        self._loot_base = Path(loot_base)
        self._state_path = Path(state_path)
        self._active_session = active_session
        self._log = log
        self._get_game_badges = get_game_badges or (lambda: set())
        self._apply_badge = apply_badge or (lambda _g: None)
        self._get_xp = get_xp or (lambda: None)

        self._uploaded_sessions: set = set()
        self._uploading = False
        self._api_key = ""
        self._api_url = DEFAULT_API_URL
        self._reload_config()

    # ------------------------------------------------------------------
    # config / state
    # ------------------------------------------------------------------
    def _reload_config(self) -> None:
        if self._state_path.is_file():
            try:
                data = json.loads(self._state_path.read_text())
                self._uploaded_sessions = set(data.get("uploaded", []))
            except Exception:
                pass
        conf = _load_secrets_conf()
        self._api_key = conf.get("WARDRIVE_API_KEY", "")
        self._api_url = conf.get("WARDRIVE_API_URL", "") or DEFAULT_API_URL

    def _save_state(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "uploaded": sorted(self._uploaded_sessions),
            }))
        except Exception:
            pass

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._api_url)

    @property
    def uploading(self) -> bool:
        return self._uploading

    def _open(self, req: Request, timeout: float = 30):
        req.add_header("User-Agent", self._ua)
        req.add_header("Accept", "application/json")
        # Use the same-origin opener so a redirect can never forward X-API-Key
        # to another host (#2).
        return _SECURE_OPENER.open(req, timeout=timeout)

    # ------------------------------------------------------------------
    # session discovery
    # ------------------------------------------------------------------
    def _pending_sessions(self) -> list:
        if not self._loot_base.is_dir():
            return []
        pending = []
        for d in sorted(self._loot_base.iterdir()):
            if not d.is_dir():
                continue
            has_data = any((d / f).is_file() for f in
                           ("wardriving.csv", "adsb_aircraft.csv", "meshcore_nodes.csv"))
            if has_data and d.name not in self._uploaded_sessions:
                pending.append(d)
        return pending

    def _mark_uploaded(self, session_dir: Path) -> bool:
        """Mark a session done so it drops out of the pending queue. Returns
        False (keeps it pending) when it is the active, still-growing session —
        otherwise rows appended after this upload would never be re-offered."""
        try:
            if session_dir.name == (self._active_session() or ""):
                return False
        except Exception:
            pass
        self._uploaded_sessions.add(session_dir.name)
        self._save_state()
        return True

    # ------------------------------------------------------------------
    # parsing (networks are header-aware for WiGLE 1.4 AND 1.6)
    # ------------------------------------------------------------------
    def _parse_networks(self, csv_path: Path) -> list:
        if not csv_path.is_file():
            return []
        rows = wigle.parse_file(csv_path)
        if rows is None:
            # Whole-file read error — propagate so the session stays PENDING
            # instead of being closed as "no GPS data" on a truncated parse (#9).
            raise OSError(f"cannot read {csv_path.name}")
        out, bad = [], 0
        for parts, cmap in rows:
            g = lambda name, d="": wigle.get(parts, cmap, name, d)
            try:
                # Require BOTH coordinates, non-empty and finite, before bounds —
                # an empty lat with lon=20 must not become (0, 20) (#20).
                slat, slon = g("CurrentLatitude").strip(), g("CurrentLongitude").strip()
                if not slat or not slon:
                    continue
                lat, lon = float(slat), float(slon)
                if lat != lat or lon != lon or abs(lat) == float("inf") or abs(lon) == float("inf"):
                    continue
                if not _in_bounds(lat, lon):
                    continue
                ch = g("Channel")
                rssi = g("RSSI")
                try:
                    alt = float(g("AltitudeMeters") or 0.0)
                    if alt != alt or abs(alt) == float("inf"):
                        alt = 0.0
                except ValueError:
                    alt = 0.0
                out.append({
                    "bssid": g("MAC"),
                    "ssid": g("SSID"),
                    "auth": _map_auth(g("AuthMode")),
                    "first_seen": g("FirstSeen"),
                    "channel": int(ch) if ch.strip().isdigit() else 0,
                    "rssi": int(rssi) if rssi.strip().lstrip("-").isdigit() else -100,
                    "lat": lat,
                    "lon": lon,
                    "alt": alt,
                    "type": g("Type", "WIFI") or "WIFI",
                })
            except Exception:
                bad += 1  # isolate a bad record, keep the rest (#9)
                continue
        if bad:
            self._log(f"  {csv_path.name}: skipped {bad} malformed network row(s)", "warn")
        return out

    def _parse_adsb(self, csv_path: Path) -> list:
        if not csv_path.is_file():
            return []
        import csv as _csv
        aircraft: dict = {}
        bad = 0
        # A whole-file read error (OSError) propagates so the session stays
        # PENDING; per-record errors are isolated so one bad value (e.g. an 'inf'
        # altitude raising OverflowError) can't truncate the rest (#9).
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = _csv.reader(f)
            if next(reader, None) is None:
                return []
            for parts in reader:
                try:
                    if len(parts) < 8:
                        continue
                    lat = float(parts[3]) if parts[3] else 0.0
                    lon = float(parts[4]) if parts[4] else 0.0
                    if not _in_bounds(lat, lon):
                        continue
                    icao = parts[1]
                    if not icao or icao in aircraft:
                        continue
                    try:
                        alt = int(float(parts[5])) if parts[5] else 0
                        spd = int(float(parts[6])) if parts[6] else 0
                        hdg = int(float(parts[7])) if parts[7] else 0
                    except (ValueError, OverflowError):
                        alt = spd = hdg = 0
                    aircraft[icao] = {
                        "icao": icao, "callsign": parts[2],
                        "lat": lat, "lon": lon, "alt_ft": alt,
                        "speed_kt": spd, "heading": hdg,
                        "first_seen": parts[0], "type": "ADSB",
                    }
                except Exception:
                    bad += 1
                    continue
        if bad:
            self._log(f"  {csv_path.name}: skipped {bad} malformed aircraft row(s)", "warn")
        return list(aircraft.values())

    def _parse_meshcore(self, csv_path: Path) -> list:
        if not csv_path.is_file():
            return []
        import csv as _csv
        nodes: dict = {}
        bad = 0
        # Whole-file read error propagates (session stays pending); per-record
        # errors are isolated and counted (#9).
        with open(csv_path, "r", encoding="utf-8", newline="") as f:
            reader = _csv.reader(f)
            if next(reader, None) is None:
                return []
            for parts in reader:
                try:
                    if len(parts) < 6:
                        continue
                    lat = float(parts[4]) if parts[4] else 0.0
                    lon = float(parts[5]) if parts[5] else 0.0
                    if not _in_bounds(lat, lon):
                        continue
                    node_id = parts[1]
                    if not node_id or node_id in nodes:
                        continue
                    try:
                        rssi = float(parts[6]) if len(parts) > 6 and parts[6] else 0
                    except ValueError:
                        rssi = 0
                    nodes[node_id] = {
                        "node_id": node_id, "node_type": parts[2],
                        "name": parts[3], "lat": lat, "lon": lon,
                        "rssi": rssi, "first_seen": parts[0], "type": "MESHCORE",
                    }
                except Exception:
                    bad += 1
                    continue
        if bad:
            self._log(f"  {csv_path.name}: skipped {bad} malformed mesh row(s)", "warn")
        return list(nodes.values())

    # ------------------------------------------------------------------
    # signing + upload
    # ------------------------------------------------------------------
    def _sign_payload(self, data: dict) -> bytes:
        raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
        nonce = _secrets.token_hex(8)
        data_b64 = base64.b64encode(raw).decode("ascii")
        sig = hmac.new(
            self._api_key.encode("utf-8"),
            (nonce + data_b64).encode("utf-8"),
            hashlib.sha256).hexdigest()
        return json.dumps({"data": data_b64, "nonce": nonce, "sig": sig}).encode("utf-8")

    def _idem_key(self, networks, aircraft, mc_nodes) -> str:
        """Idempotency key over the DATA rows only (NOT game_xp, which drifts):
        a re-send after a timeout carries the SAME key, so a server that already
        received the batch dedups it and returns the original receipt instead of
        importing twice — exactly-once across retries/timeouts. Also the batch
        identity used by /api/upload-keys for resume."""
        return hashlib.sha256(
            json.dumps([networks, aircraft, mc_nodes], separators=(",", ":"),
                       sort_keys=True).encode("utf-8")).hexdigest()

    def _post_session(self, networks, aircraft, mc_nodes,
                      include_xp: bool = True) -> dict:
        """One signed POST of a single batch. game_xp rides along as
        declared-by-game (never ranked); only on the first batch of a session to
        avoid re-sending a drifting value per batch."""
        body = {
            "networks": networks,
            "aircraft": aircraft,
            "meshcore_nodes": mc_nodes,
        }
        if include_xp:
            try:
                xp = self._get_xp()
                if isinstance(xp, int) and xp > 0:
                    body["game_xp"] = xp
            except Exception:
                pass
        idem = self._idem_key(networks, aircraft, mc_nodes)
        payload = self._sign_payload(body)
        # async upload: the server answers 202 fast and imports in the background,
        # so a slow import no longer races the client's timeout. We then poll the
        # job for the real receipt. A plain 200 (sync / idempotent replay) is used
        # as-is.
        url = self._api_url + ("&" if "?" in self._api_url else "?") + "async=1"
        req = Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("X-API-Key", self._api_key)
        req.add_header("X-Idempotency-Key", idem)
        parts = [f"{n} {w}" for n, w in ((len(networks), "nets"), (len(aircraft), "aircraft"),
                                            (len(mc_nodes), "mesh")) if n]
        self._log(f"  -> sending {', '.join(parts) or 'empty batch'}, {len(payload) // 1024 + 1} KB,"
                  f" key {idem[:8]}", "raw")
        t0 = time.time()
        with self._open(req, timeout=90) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            data = self._read_json(resp, f"HTTP {status}")
        if status == 202:
            if data.get("job_id"):
                self._log(f"  <- accepted in {time.time() - t0:.1f} s, queued as job {data['job_id']}"
                          " - waiting for the receipt", "raw")
                ra = data.get("retry_after")
                return self._poll_job(data["job_id"],
                                      first_wait=float(ra) if isinstance(ra, (int, float)) and 0 < ra <= 60 else 2.0)
            # Accepted (202) but no job id: there is no way to confirm the import,
            # so this is NOT a receipt. Keep the batch pending and re-send next sync
            # (the idempotency key dedups it server-side) (#7).
            raise _EmptyAnswer("HTTP 202 without a job id - import unconfirmed")
        # Synchronous answer: only a validated receipt confirms the import. An empty
        # or receipt-less 2xx body must leave the batch pending, not close it (#7).
        if not self._is_receipt(data):
            raise _EmptyAnswer(f"HTTP {status} without a receipt - import unconfirmed")
        rid = data.get("receipt_id")
        self._log(f"  <- HTTP {status}, receipt {rid if rid else '-'}"
                  + (" (the portal already had this batch)" if data.get("idempotent_replay") else ""), "raw")
        return data

    @staticmethod
    def _is_receipt(data: dict) -> bool:
        """True when a synchronous answer is an actual import receipt: it carries a
        receipt id, is an idempotent replay, or reports at least one counted field.
        A bare {} or a status-only body is not a receipt."""
        if not isinstance(data, dict):
            return False
        if data.get("receipt_id") or data.get("idempotent_replay"):
            return True
        return any(k in data for k in _AGG_FIELDS)

    # ---- the conversation with the portal, readable in the game console ----
    @staticmethod
    def _srv_text(v, n=80) -> str:
        """A server-provided string, made safe for one console line."""
        t = " ".join(str(v).split())
        return "".join(ch for ch in t if ch.isprintable())[:n]

    def _say_portal(self, msg, suffix="") -> None:
        """The portal's own sentence, word-wrapped onto as many console lines as it
        needs (the contract allows up to 300 characters; a console line fits ~84)."""
        words, line, first = self._srv_text(msg, 300).split(), "", True
        for w in words + ([suffix] if suffix else []):
            head = "  portal: " if first else "          "
            if line and len(head) + len(line) + 1 + len(w) > 88:
                self._log(head + line, "raw")
                line, first = w, False
            else:
                line = (line + " " + w) if line else w
        if line:
            self._log(("  portal: " if first else "          ") + line, "raw")

    def _read_json(self, resp, what: str) -> dict:
        """Body of a portal answer as a dict. Notes the contract version header once,
        and turns an empty or non-JSON 2xx body into a clear, retryable error."""
        ver = resp.headers.get("X-WDGWars-Upload-API") if getattr(resp, "headers", None) else None
        if ver and ver != getattr(self, "_api_ver_seen", None):
            self._api_ver_seen = ver
            self._log(f"  portal upload API {self._srv_text(ver, 24)}", "raw")
        raw = resp.read()
        if not raw.strip():
            raise _EmptyAnswer(f"{what}: empty answer from the portal")
        try:
            j = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise _EmptyAnswer(f"{what}: answer is not JSON ({self._srv_text(raw[:40], 40)!r})")
        if not isinstance(j, dict):
            raise _EmptyAnswer(f"{what}: unexpected answer")
        if j.get("message"):
            self._say_portal(j["message"])
        return j

    def _job_url(self, job_id) -> str:
        base = self._api_url.rsplit("/api/", 1)[0]
        return f"{base}/api/v2/upload-job/{job_id}"

    def _poll_job(self, job_id, first_wait: float = 2.0) -> dict:
        """Poll the async upload job until it returns a receipt. Raises
        TimeoutError on no result (caller then keeps the session PENDING — never
        marks unconfirmed data uploaded)."""
        url = self._job_url(job_id)
        t0 = time.time()
        deadline = t0 + 180.0
        delay = 2.0
        last_state, last_said = None, t0
        not_found = 0
        time.sleep(first_wait)
        while time.time() < deadline:
            req = Request(url, method="GET")
            req.add_header("X-API-Key", self._api_key)
            try:
                with self._open(req, timeout=30) as resp:
                    raw = resp.read()
                    final_url = resp.geturl() if hasattr(resp, "geturl") else url
                    ctype = (resp.headers.get("Content-Type", "") if getattr(resp, "headers", None) else "")
                if "/login" in (final_url or "") or "text/html" in ctype.lower():
                    # the portal sent us to a web page instead of answering the API:
                    # waiting will not change that - say so and keep the batch pending
                    raise RuntimeError(f"portal answered the job-status request for job {job_id} with a web"
                                       " page instead of JSON (API key not accepted there) - batch kept"
                                       " pending, please report it")
                j = json.loads(raw.decode("utf-8", "replace")) if raw.strip() else None
            except HTTPError as e:
                if e.code == 401:
                    raise RuntimeError("portal rejected the API key for job status (401) - check WARDRIVE_API_KEY")
                if e.code == 404:
                    # contract 2026-11-19: an existing job is visible right after the 202, so a
                    # repeated 404 is final - not something to wait out for 3 minutes
                    not_found += 1
                    if not_found >= 3:
                        raise RuntimeError(f"portal does not know job {job_id} (404)")
                if e.code in (404, 425, 429, 502, 503, 504):   # not visible yet / busy
                    ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                    wait = float(ra) if ra and str(ra).isdigit() and 0 < int(ra) <= 120 else delay
                    if e.code in (429, 503):
                        self._log(f"  .. portal busy (HTTP {e.code}), asking again in {wait:.0f} s", "raw")
                    time.sleep(wait); delay = min(delay * 1.5, 15.0); continue
                raise
            except (ValueError, OSError):
                j = None                          # empty / non-JSON / dropped: ask again
            if not isinstance(j, dict):
                # The receipt endpoint can answer before the job row is readable
                # (empty body) - that is "not yet", never a failed upload.
                j = {"status": "not visible yet"}
            st = (j.get("status") or "").lower()
            if st in ("done", "ok", "finished", "complete"):
                rec = j.get("result")
                if j.get("message"):
                    self._say_portal(j["message"])
                self._log(f"  <- job {job_id} done after {time.time() - t0:.0f} s", "raw")
                return rec if isinstance(rec, dict) else j
            if st in ("failed", "error", "cancelled"):
                why = self._srv_text(j.get("message") or j.get("error") or "no reason given", 300)
                raise RuntimeError(f"portal job {job_id} {st}: {why}")
            # still waiting: say so when the state changes, and every ~15 s
            now = time.time()
            if st != last_state or now - last_said >= 15:
                if j.get("message"):              # the portal's own words, e.g. queue position
                    self._say_portal(j["message"], "(%.0f s)" % (now - t0))
                else:
                    self._log(f"  .. job {job_id}: {st or 'unknown'}, waiting"
                              f" ({now - t0:.0f} s of 180)", "raw")
                last_state, last_said = st, now
            ra = j.get("retry_after")
            wait = float(ra) if isinstance(ra, (int, float)) and 0 < ra <= 60 else delay
            time.sleep(wait)
            delay = min(delay * 1.5, 15.0)
        raise TimeoutError(f"no receipt for job {job_id} within 180 s")

    def sync(self, all_sessions: bool = True) -> None:
        """Entry point — kick off a background upload of pending sessions."""
        self._reload_config()
        if not self.configured:
            if commented_key_value("WARDRIVE_API_KEY"):
                self._log("WDGWars: WARDRIVE_API_KEY is in secrets.conf but COMMENTED "
                          "OUT — remove the leading '#' to enable sync.", "warn")
            else:
                self._log("WDGWars: no API key. Add WARDRIVE_API_KEY to secrets.conf, "
                          "or use ADDONS > WDGWars Pair.", "warn")
            return
        if self._uploading:
            self._log("WDGWars: upload already in progress...", "warn")
            return
        sessions = self._pending_sessions()
        if not all_sessions and sessions:
            sessions = sessions[-1:]
        if not sessions:
            self._log("WDGWars: nothing new to upload.", "tx")
            return
        self._uploading = True
        threading.Thread(target=self._upload_worker, args=(sessions,),
                         daemon=True).start()

    # ------------------------------------------------------------------
    # batching: dedup -> split -> (resume lookup) -> serial POST -> aggregate
    # ------------------------------------------------------------------
    def _dedup_networks(self, nets: list) -> list:
        """One row per (bssid, ssid) per session. The portal dedups BEFORE the
        split (note #6) so the same AP can't fall into two batches and inflate
        updated/cooldown. Keep the strongest RSSI; first-seen order is preserved
        (dict keeps insertion position), which the portal uses for takeovers."""
        best: dict = {}
        for n in nets:
            k = (n.get("bssid", ""), n.get("ssid", ""))
            cur = best.get(k)
            if cur is None or n.get("rssi", -100) > cur.get("rssi", -100):
                best[k] = n
        return list(best.values())

    def _make_batches(self, networks, aircraft, mc_nodes) -> list:
        """Split a session into batches of <= BATCH_MAX_RECORDS rows AND
        <= BATCH_MAX_BYTES of raw row JSON (whichever binds first). aircraft and
        meshcore share the record budget. Each batch carries its own idempotency
        key (sha256 over its own rows)."""
        combined = ([("n", x) for x in networks] + [("a", x) for x in aircraft]
                    + [("m", x) for x in mc_nodes])
        groups, cur, cur_bytes = [], [], 0
        for item in combined:
            sz = len(json.dumps(item[1], separators=(",", ":")).encode("utf-8")) + 1
            if cur and (len(cur) >= BATCH_MAX_RECORDS
                        or cur_bytes + sz > BATCH_MAX_BYTES):
                groups.append(cur)
                cur, cur_bytes = [], 0
            cur.append(item)
            cur_bytes += sz
        if cur:
            groups.append(cur)
        out = []
        for g in groups:
            n, a, m = _split_combined(g)
            out.append({"networks": n, "aircraft": a, "meshcore_nodes": m,
                        "key": self._idem_key(n, a, m)})
        return out

    def _upload_keys_lookup(self, keys: list) -> tuple:
        """POST /api/upload-keys to learn which batch keys the server already has.
        Returns (done: {key: receipt_id}, processing: {key: job_id}, missing: set).
        Best-effort: on any error (incl. a server without the endpoint) every key
        falls into 'missing' and is simply (re-)sent — the idempotency key still
        guards against duplicates, so this only costs cheap replays, never dupes."""
        done: dict = {}
        processing: dict = {}
        missing: set = set()
        url = f"{self._api_base()}/api/upload-keys"
        for s in range(0, len(keys), UPLOAD_KEYS_MAX):
            chunk = keys[s:s + UPLOAD_KEYS_MAX]
            try:
                body = json.dumps({"keys": chunk}).encode()
                req = Request(url, data=body, method="POST")
                req.add_header("Content-Type", "application/json")
                req.add_header("X-API-Key", self._api_key)
                with self._open(req, timeout=20) as resp:
                    j = json.loads(resp.read().decode())
                if not j.get("ok"):
                    missing.update(chunk)
                    continue
                d = j.get("done", {}) or {}
                p = j.get("processing", {}) or {}
                done.update(d)
                processing.update(p)
                classified = set(d) | set(p) | set(j.get("missing", []) or [])
                for k in chunk:
                    if k not in classified:   # 'missing' list + anything unlisted
                        missing.add(k)
            except Exception:
                missing.update(chunk)         # 404/old server/network -> send all
        return done, processing, missing

    def _post_batch_split(self, nets, ac, mc, include_xp: bool) -> list:
        """POST one batch; on a 413 WAF burst (not a real size limit) halve it and
        send the halves. Returns the list of receipt dicts for the pieces that
        went. Raises _BatchPending when a piece can't be confirmed."""
        try:
            return [self._post_session(nets, ac, mc, include_xp=include_xp)]
        except HTTPError as e:
            if e.code == 429:
                ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
                wait = int(ra) if ra and str(ra).isdigit() and 0 < int(ra) <= 120 else 5
                self._log(f"  <- HTTP 429 rate limit - the portal asks to wait {wait} s, then retrying", "warn")
                time.sleep(wait)
                try:
                    return [self._post_session(nets, ac, mc, include_xp=include_xp)]
                except Exception:
                    raise _BatchPending()
            if e.code == 413:
                n = len(nets) + len(ac) + len(mc)
                if n <= BATCH_SPLIT_MIN:
                    self._log(f"  HTTP 413 on a minimal batch ({n} rows) — "
                              "kept pending", "err")
                    raise _BatchPending()
                (an, aa, am), (bn, ba, bm) = _halve(nets, ac, mc)
                self._log(f"  HTTP 413 — splitting {n} rows and retrying", "warn")
                out = self._post_batch_split(an, aa, am, include_xp)
                out += self._post_batch_split(bn, ba, bm, False)
                return out
            if e.code == 409:
                # same key still importing server-side — not a failure; resume later
                self._log("  Still importing (409) — batch kept pending", "warn")
                raise _BatchPending()
            body = ""
            try:
                raw = e.read().decode("utf-8", errors="replace")
                try:
                    jj = json.loads(raw)
                    body = jj.get("message") or jj.get("error") or raw
                except ValueError:
                    body = raw
            except Exception:
                pass
            self._log(f"  <- HTTP {e.code}: {self._srv_text(body, 70) or 'no details'}"
                      " - batch kept pending", "err")
            raise _BatchPending()
        except (TimeoutError, OSError) as exc:
            # No receipt -> UNCONFIRMED. Keep PENDING; re-sends next run, the
            # idempotency key dedups a batch the server actually did receive.
            why = str(exc) if isinstance(exc, (_EmptyAnswer, TimeoutError)) and str(exc) else "network timeout"
            self._log(f"  {why} - no receipt yet; batch kept pending, the next sync"
                      " re-sends it (no duplicates)", "warn")
            raise _BatchPending()

    def _accumulate(self, agg: dict, result: dict) -> None:
        for f in _AGG_FIELDS:
            v = result.get(f)
            if isinstance(v, (int, float)):
                agg[f] = agg.get(f, 0) + v
        if result.get("idempotent_replay"):
            agg["_replays"] = agg.get("_replays", 0) + 1

    def _report_agg(self, agg: dict) -> None:
        if not agg:
            return
        info = f"+{agg.get('imported', 0)} nets"
        if agg.get("updated"):
            info += f", {agg['updated']} upd"
        ac_i, ac_s = agg.get("aircraft_imported", 0), agg.get("aircraft_already_seen", 0)
        if ac_i or ac_s:
            info += f", +{ac_i} ac" + (f" ({ac_s} seen)" if ac_s else "")
        if agg.get("meshcore_imported"):
            info += f", +{agg['meshcore_imported']} mesh"
        if agg.get("duplicates"):
            info += f", {agg['duplicates']} dup"
        if agg.get("cooldown"):
            info += f", {agg['cooldown']} cooldown"
        if agg.get("_replays"):
            info += f" ({agg['_replays']} replay — server already had these)"
        self._log(f"  OK: {info}", "ok")
        # Rows the server could not place — worth surfacing: no_gps usually means
        # the GPS fix dropped mid-drive (rows stamped 0,0), bad_rows = malformed.
        no_gps, bad = agg.get("no_gps", 0), agg.get("bad_rows", 0)
        if no_gps or bad:
            parts = []
            if no_gps:
                parts.append(f"{no_gps} bez GPS")
            if bad:
                parts.append(f"{bad} uszkodzonych")
            self._log("  uwaga: odrzucono " + ", ".join(parts) + " (wiersze utracone)",
                      "warn")

    def _upload_one_session(self, session_dir, i: int, total: int) -> tuple:
        """Upload one session as 1+ batches. Returns (sessions_done_inc,
        nets_imported_inc). The session is marked uploaded only when EVERY batch
        is confirmed; otherwise it stays PENDING and resumes its unsent batches."""
        networks = self._dedup_networks(
            self._parse_networks(session_dir / "wardriving.csv"))
        aircraft = self._parse_adsb(session_dir / "adsb_aircraft.csv")
        mc_nodes = self._parse_meshcore(session_dir / "meshcore_nodes.csv")

        if not networks and not aircraft and not mc_nodes:
            if self._mark_uploaded(session_dir):
                self._log(f"[{i+1}/{total}] {session_dir.name}: no GPS data", "raw")
            else:
                self._log(f"[{i+1}/{total}] {session_dir.name}: active, "
                          "will re-check next upload", "raw")
            return 0, 0

        batches = self._make_batches(networks, aircraft, mc_nodes)
        nb = len(batches)
        parts = []
        if networks:
            parts.append(f"{len(networks)} nets")
        if aircraft:
            parts.append(f"{len(aircraft)} aircraft")
        if mc_nodes:
            parts.append(f"{len(mc_nodes)} mesh")
        self._log(f"[{i+1}/{total}] {session_dir.name}: {', '.join(parts)}"
                  + (f" in {nb} batches" if nb > 1 else ""), "tx")

        # Resume: ask which batch keys the server already has / is importing, so
        # we re-send only what's actually missing instead of every batch.
        done_keys, proc_keys = {}, {}
        if nb > 1:
            done_keys, proc_keys, _m = self._upload_keys_lookup(
                [b["key"] for b in batches])
            if done_keys or proc_keys:
                self._log(f"  resume: {len(done_keys)} already done, "
                          f"{len(proc_keys)} importing, "
                          f"{nb - len(done_keys) - len(proc_keys)} to send", "raw")

        agg: dict = {}
        all_confirmed = True
        first_send = True
        for bi, b in enumerate(batches):
            key = b["key"]
            if key in done_keys:
                continue                              # imported on a prior run
            try:
                if key in proc_keys:                  # importing from a prior run
                    rec = self._poll_job(proc_keys[key])
                    self._accumulate(agg, rec)
                    self._handle_upload_badges(rec)
                else:
                    for rec in self._post_batch_split(
                            b["networks"], b["aircraft"], b["meshcore_nodes"],
                            include_xp=first_send):
                        self._accumulate(agg, rec)
                        self._handle_upload_badges(rec)
                    first_send = False
            except _BatchPending:
                all_confirmed = False
                break
            except Exception as e:
                self._log(f"  batch {bi+1}/{nb} error: {e}", "err")
                all_confirmed = False
                break
            if bi < nb - 1:
                time.sleep(0.5)                       # serial; well under 120/min

        self._report_agg(agg)
        if all_confirmed:
            if not self._mark_uploaded(session_dir):
                self._log("  (active session — kept open for re-upload)", "raw")
            return 1, agg.get("imported", 0)
        self._log("  session kept PENDING — will resume unsent batches next sync",
                  "warn")
        return 0, agg.get("imported", 0)

    def _upload_worker(self, sessions: list) -> None:
        total = len(sessions)
        uploaded = 0
        total_nets = 0
        self._log(f"WDGWars: uploading {total} session(s) as {self._ua}", "tx")
        for i, session_dir in enumerate(sessions):
            try:
                inc_u, inc_n = self._upload_one_session(session_dir, i, total)
                uploaded += inc_u
                total_nets += inc_n
            except URLError as e:
                self._log(f"  Connection error: {e.reason}", "err")
            except Exception as e:
                self._log(f"  Error: {e}", "err")
            if i < total - 1:
                time.sleep(1)

        self._log(f"WDGWars: done {uploaded}/{total} session(s), +{total_nets} networks",
                  "ok" if uploaded == total else "warn")
        self._uploading = False
        if uploaded > 0:
            self._badge_sync_worker()

    # ------------------------------------------------------------------
    # badges
    # ------------------------------------------------------------------
    def _api_base(self) -> str:
        return self._api_url.rsplit("/api/", 1)[0]

    def _server_badges_from_game(self) -> list:
        out = []
        for game_id in self._get_game_badges():
            srv = BADGE_GAME_TO_SERVER.get(game_id)
            if srv:
                out.append(srv)
        return out

    def _apply_server_badges(self, server_badges) -> list:
        new = []
        have = self._get_game_badges()
        for srv_id in server_badges:
            game_id = BADGE_SERVER_TO_GAME.get(srv_id)
            if game_id and game_id not in have:
                try:
                    self._apply_badge(game_id)
                    new.append(game_id)
                except Exception:
                    pass
        return new

    def _handle_upload_badges(self, result: dict) -> None:
        new = self._apply_server_badges(result.get("new_badges", []))
        for b in new:
            self._log(f"  [BADGE] Unlocked: {b.upper().replace('_', ' ')}", "ok")

    def _badge_sync_worker(self) -> None:
        try:
            url = f"{self._api_base()}/api/badges/"
            payload = json.dumps({"badges": self._server_badges_from_game()}).encode()
            req = Request(url, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-API-Key", self._api_key)
            with self._open(req, timeout=15) as resp:
                result = json.loads(resp.read().decode())
            all_badges = result.get("badges", [])
            new_game = self._apply_server_badges(all_badges)
            parts = []
            if result.get("game_imported"):
                parts.append(f"{result['game_imported']} pushed")
            if result.get("server_awarded"):
                parts.append(f"{len(result['server_awarded'])} from server")
            if new_game:
                parts.append(f"{len(new_game)} new in game")
                for b in new_game:
                    self._log(f"  [BADGE] Unlocked: {b.upper().replace('_', ' ')}", "ok")
            info = ", ".join(parts) if parts else "in sync"
            self._log(f"WDGWars badges: {len(all_badges)} total — {info}", "ok")
        except HTTPError as e:
            self._log(f"Badge sync: HTTP {e.code}", "warn")
        except Exception as e:
            self._log(f"Badge sync: {e}", "warn")

    # ------------------------------------------------------------------
    # /api/me
    # ------------------------------------------------------------------
    def fetch_user_info(self) -> Optional[dict]:
        if not self.configured:
            return None
        try:
            req = Request(f"{self._api_base()}/api/me")
            req.add_header("X-API-Key", self._api_key)
            with self._open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("ok"):
                self._log(f"  /api/me: {data.get('error', 'unknown error')}", "warn")
                return None
            return data
        except HTTPError as e:
            self._log("  Invalid API key (401)" if e.code == 401
                      else f"  /api/me: HTTP {e.code}", "warn")
            return None
        except Exception as e:
            self._log(f"  /api/me: {e}", "warn")
            return None

    # ------------------------------------------------------------------
    # /api/rabbits — Chase the White Rabbit targets (read-only)
    # ------------------------------------------------------------------
    def fetch_rabbits(self) -> list:
        """GET /api/rabbits — list of white-rabbit tiles. [] on any error.

        The server computes distance/compass from its own 'area'; the caller
        recomputes those from the device GPS instead.
        """
        if not self.configured:
            return []
        try:
            req = Request(f"{self._api_base()}/api/rabbits")
            req.add_header("X-API-Key", self._api_key)
            with self._open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            if not data.get("ok"):
                return []
            return data.get("rabbits", []) or []
        except Exception as e:
            log.debug("fetch_rabbits failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # /api/device-pair — pair by short code instead of pasting a 64-hex key
    # ------------------------------------------------------------------
    def _save_secret(self, key: str, value: str) -> bool:
        """Upsert key=value in the repo-root secrets.conf (preserving the rest).

        Returns True only when the key was actually persisted. A read error on an
        existing file aborts WITHOUT writing, so a transient failure can't replace
        the whole file (wiping other settings) with a single entry. The file is
        written atomically (temp + rename) at mode 0600 so it is never left
        half-written or world-readable (#11, #3)."""
        path = Path(__file__).resolve().parent.parent / "secrets.conf"
        if path.is_file():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError as e:
                log.error("secrets.conf unreadable, not overwriting: %s", e)
                return False
        else:
            lines = []
        out, found = [], False
        for ln in lines:
            s = ln.strip()
            if s and not s.startswith("#") and s.split("=", 1)[0].strip() == key:
                out.append("%s=%s" % (key, value)); found = True
            else:
                out.append(ln)
        if not found:
            out.append("%s=%s" % (key, value))
        text = "\n".join(out) + "\n"
        # Write via a fresh mkstemp file (random name, O_EXCL, 0600) in the same
        # dir, then rename over. A fixed ".tmp" name could be pre-created as a
        # symlink by anyone with write access to the dir, redirecting the write
        # (secret leak / clobber); mkstemp never follows a planted link.
        import tempfile
        fd, tmpname = tempfile.mkstemp(dir=str(path.parent), prefix=".{}.".format(path.name), suffix=".tmp")
        tmp = type(path)(tmpname)
        try:
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                    fh.flush()
                    os.fsync(fh.fileno())
            except BaseException:
                try: os.close(fd)
                except OSError: pass
                raise
            os.replace(tmp, path)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return True
        except OSError as e:
            log.error("save_secret failed: %s", e)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return False

    def request_pair(self, label: str = "WatchDogsGo NG (uConsole)") -> Optional[dict]:
        """POST /api/device-pair action=request -> {code, poll, expires_in} or None."""
        try:
            body = json.dumps({"action": "request", "label": label}).encode()
            req = Request(f"{self._api_base()}/api/device-pair", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with self._open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            return data if data.get("ok") else None
        except Exception as e:
            log.debug("request_pair: %s", e)
            return None

    def poll_pair(self, code: str, poll_secret: str) -> dict:
        """POST action=poll -> {'pending':True} | {'api_key','username'} | {'error':...}."""
        try:
            body = json.dumps({"action": "poll", "code": code, "poll": poll_secret}).encode()
            req = Request(f"{self._api_base()}/api/device-pair", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            with self._open(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            return {"ok": False, "error": "bad_request" if e.code == 403 else "http_%d" % e.code}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def adopt_api_key(self, api_key: str) -> bool:
        """Persist a freshly paired key and use it immediately. Returns False if the
        key could NOT be written to secrets.conf, so the caller can warn that
        pairing will not survive a restart (#11). The in-memory key is still set so
        the current session works."""
        self._api_key = api_key
        return self._save_secret("WARDRIVE_API_KEY", api_key)

    # ------------------------------------------------------------------
    # /api/game-state — portal state for the HUD (rank, team, decay, contracts)
    # ------------------------------------------------------------------
    def fetch_game_state(self, lat=None, lon=None) -> Optional[dict]:
        """GET /api/game-state[?lat=&lng=] -> dict or None. Position is only sent
        in the query; the server never stores it."""
        if not self.configured:
            return None
        try:
            url = f"{self._api_base()}/api/game-state"
            if lat is not None and lon is not None:
                url += "?lat=%.6f&lng=%.6f" % (lat, lon)
            req = Request(url)
            req.add_header("X-API-Key", self._api_key)
            with self._open(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            return data if data.get("ok") else None
        except Exception as e:
            log.debug("fetch_game_state: %s", e)
            return None

    def show_profile(self) -> None:
        """Fetch /api/me in the background and print identity/stats to the terminal."""
        self._reload_config()
        if not self.configured:
            if commented_key_value("WARDRIVE_API_KEY"):
                self._log("WDGWars: WARDRIVE_API_KEY is in secrets.conf but COMMENTED "
                          "OUT — remove the leading '#' to enable it.", "warn")
            else:
                self._log("WDGWars: no API key. Add WARDRIVE_API_KEY to secrets.conf.", "warn")
            return
        threading.Thread(target=self._profile_worker, daemon=True).start()

    def _profile_worker(self) -> None:
        info = self.fetch_user_info()
        if not info or not info.get("username"):
            return
        gang = info.get("gang") or "(no gang)"
        self._log(f"WDGWars: {info.get('username', '?')} [{gang}] — "
                  f"{info.get('wifi', 0)} wifi, {info.get('ble', 0)} ble", "ok")
        srv = info.get("badges", []) or []
        self._apply_server_badges(srv)
        if "uconsole_pioneer" in srv:
            self._log("  [BADGE] uConsole Pioneer confirmed on your account", "ok")
        if srv:
            self._log(f"  WDGWars: {len(srv)} badge(s) on your portal account", "tx")
