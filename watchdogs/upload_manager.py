"""WPA-sec integration — upload handshake pcaps, download cracked passwords."""

import json
import logging
import os
import threading
from pathlib import Path

from .config import (WPASEC_URL, WPASEC_DL_URL, WPASEC_KEY,
                     WIGLE_API_URL, WIGLE_API_NAME, WIGLE_API_TOKEN)

log = logging.getLogger(__name__)

# Runtime token cache (set from secrets.conf OR via in-game input dialog)
_runtime_key: str = ""

# Only one WiGLE sync may run at a time: a manual upload racing the cluster's
# automatic sync could otherwise read the same pending sessions and upload each
# twice, burning the free-tier daily cap (#14).
_wigle_sync_lock = threading.Lock()


def _write_private(path: Path, text: str, private_dir: bool = False) -> None:
    """Write text to a 0600 file (fsync'd), tightening a pre-existing looser mode.

    Files holding secrets (API keys) or cracked passwords must never be
    world/group-readable. Creating with mode 0600 means the file is private from
    the instant it exists (umask can only clear bits it already lacks), and the
    explicit chmod repairs any file that already existed as 0644. With
    private_dir=True the parent dir is also tightened to 0700 — use it only for a
    dedicated secrets dir (e.g. loot/passwords), never a shared one like repo root.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if private_dir:
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or ""


def get_wpasec_key() -> str:
    """Return the active WPA-sec key (runtime > env > config)."""
    if _runtime_key:
        return _runtime_key
    # Try new env var first, then legacy, then config
    return (os.environ.get("WDG_WPASEC_KEY")
            or os.environ.get("JANOS_WPASEC_KEY")
            or WPASEC_KEY
            or "")


def set_wpasec_key(key: str) -> None:
    """Set token at runtime (from user input dialog)."""
    global _runtime_key
    _runtime_key = key.strip()


def wpasec_configured() -> bool:
    return bool(get_wpasec_key())


def save_wpasec_key(app_dir: str, key: str) -> None:
    """Persist the WPA-sec key to secrets.conf as WDG_WPASEC_KEY.
    Migrates legacy JANOS_WPASEC_KEY entries on the fly."""
    conf = Path(app_dir) / "secrets.conf"
    lines: list[str] = []
    found = False
    if conf.is_file():
        for line in conf.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("WDG_WPASEC_KEY=") or stripped.startswith("JANOS_WPASEC_KEY="):
                if not found:
                    lines.append(f"WDG_WPASEC_KEY={key}")
                    found = True
                # Drop legacy line (replaced by new one above)
            else:
                lines.append(line)
    if not found:
        if not lines:
            lines.append("# Watch Dogs Go secrets (gitignored)")
        lines.append(f"WDG_WPASEC_KEY={key}")
    # 0600: secrets.conf holds the WPA-sec key; do NOT tighten the repo-root dir.
    _write_private(conf, "\n".join(lines) + "\n", private_dir=False)
    set_wpasec_key(key)


# ── Upload ────────────────────────────────────────────────────────────────

def upload_wpasec(pcap_path: Path) -> tuple[bool, str]:
    """Upload a single .pcap to WPA-sec.  Returns (ok, message)."""
    key = get_wpasec_key()
    if not key:
        return False, "WPA-sec key not configured"
    if not pcap_path.is_file():
        return False, f"File not found: {pcap_path}"
    try:
        import requests
    except ImportError:
        return False, "requests library not installed"
    try:
        with open(pcap_path, "rb") as fh:
            files = {"file": (pcap_path.name, fh, "application/octet-stream")}
            resp = requests.post(
                WPASEC_URL,
                files=files,
                cookies={"key": key},
                timeout=60,
            )
        if resp.status_code == 200:
            body = resp.text.strip()
            if "already" in body.lower():
                return True, "Already submitted"
            return True, body[:200] if body else "Uploaded"
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        log.error("WPA-sec upload error: %s", exc)
        return False, str(exc)


def _bssid_from_filename(name: str) -> str:
    """Extract BSSID from pcap filename like 'SSID_AABBCCDDEEFF_HHMMSS.pcap'.

    Returns MAC with colons (e.g. 'AA:BB:CC:DD:EE:FF') or empty string.
    """
    stem = name.rsplit(".", 1)[0]  # strip .pcap
    parts = stem.split("_")
    for part in parts:
        clean = part.replace("-", "").replace(":", "")
        if len(clean) == 12 and all(c in "0123456789ABCDEFabcdef" for c in clean):
            return ":".join(clean[i:i+2] for i in range(0, 12, 2)).upper()
    return ""


def _classify_wpasec(ok: bool, msg: str) -> str:
    """Classify a wpa-sec submit response. HTTP 200 does NOT mean a handshake was
    accepted — wpa-sec returns 200 with 'No valid handshakes/PMKIDs found' for a
    capture without a usable handshake, and 'We support pcap and pcapng' for a
    wrong file type. Only a parsed capture (hcxpcapngtool output) really counts."""
    if not ok:
        return "error"
    m = msg.lower()
    if "no valid" in m or "not a valid capture" in m or "we support pcap" in m:
        return "no_handshake"
    if "already" in m:
        return "duplicate"
    return "accepted"


def upload_wpasec_all(loot_dir: Path, blocked_macs: set[str] | None = None,
                      ) -> tuple[int, int, str]:
    """Upload all .pcap from loot/*/handshakes/ to wpa-sec. Returns (submitted,
    total, msg) where `submitted` is the count that actually carried a handshake
    (accepted + already-known), NOT every HTTP 200. wpa-sec only ingests pcap/
    pcapng, so .22000/.hccapx are intentionally not sent (they're derived from the
    same captures, whose .pcap is uploaded).

    blocked_macs: set of uppercase MACs to skip (whitelist).
    """
    pcaps = list(loot_dir.rglob("handshakes/*.pcap"))
    # wpa-sec also accepts raw .cap / .pcapng — include them so nothing is missed.
    pcaps += list(loot_dir.rglob("handshakes/*.cap"))
    pcaps += list(loot_dir.rglob("handshakes/*.pcapng"))
    if not pcaps:
        return 0, 0, "No capture files found"

    # Filter out whitelisted BSSIDs. When a whitelist is active, also hold back any
    # capture whose BSSID can't be read from its filename: an indeterminate-origin
    # file could belong to a whitelisted device, so it must not be sent to the
    # external service while the user is deliberately excluding MACs (#1).
    skipped = 0
    if blocked_macs:
        blocked = {m.strip().upper() for m in blocked_macs if m and m.strip()}
        filtered = []
        for p in pcaps:
            bssid = _bssid_from_filename(p.name)
            if not bssid or bssid in blocked:
                skipped += 1
            else:
                filtered.append(p)
        pcaps = filtered

    cats = {"accepted": 0, "duplicate": 0, "no_handshake": 0, "error": 0}
    errors = []
    for p in pcaps:
        ok, msg = upload_wpasec(p)
        cat = _classify_wpasec(ok, msg)
        cats[cat] += 1
        if cat == "error":
            errors.append(f"{p.name}: {msg}")
    submitted = cats["accepted"] + cats["duplicate"]
    summary = (f"{submitted} handshakes submitted "
               f"({cats['accepted']} new, {cats['duplicate']} already known); "
               f"{cats['no_handshake']} without a handshake")
    if skipped:
        summary += f", {skipped} skipped (whitelist)"
    if errors:
        summary += f", {len(errors)} errors: {errors[0][:80]}"
    return submitted, len(pcaps), summary


# ── Download (potfile) ────────────────────────────────────────────────────

def download_wpasec_potfile(loot_dir: Path) -> tuple[bool, int, str]:
    """Download cracked passwords from WPA-sec.

    Returns (ok, count, message).
    Saves to loot_dir/passwords/wpasec_cracked.potfile
    """
    key = get_wpasec_key()
    if not key:
        return False, 0, "WPA-sec key not configured"
    try:
        import requests
    except ImportError:
        return False, 0, "requests library not installed"
    try:
        resp = requests.get(
            WPASEC_DL_URL,
            cookies={"key": key},
            timeout=30,
        )
        if resp.status_code != 200:
            return False, 0, f"HTTP {resp.status_code}: {resp.text[:200]}"
        body = resp.text.strip()
        if not body:
            return True, 0, "No cracked passwords yet"
        # An error / login / proxy page is HTML, not a potfile. Never let it
        # overwrite the last good potfile (which would wipe the cracked index and
        # claim "N passwords saved" for N HTML lines) (#12).
        if body.lstrip()[:1] == "<":
            return False, 0, "WPA-sec returned an HTML page, not a potfile (check key)"
        # Parse first (into memory) so a corrupt download can't clobber good loot.
        parsed = _parse_potfile_text(body)
        if parsed["count"] == 0:
            return False, 0, "no recognizable cracked entries in response - kept previous potfile"
        out = loot_dir / "passwords" / "wpasec_cracked.potfile"
        # 0600, private dir: potfile + JSON contain cracked passwords.
        _write_private(out, body + "\n", private_dir=True)
        _save_potfile_json(loot_dir, parsed)
        return True, parsed["count"], f"{parsed['count']} passwords saved"
    except Exception as exc:
        log.error("WPA-sec download error: %s", exc)
        return False, 0, str(exc)


# ── Potfile parsing ───────────────────────────────────────────────────────

def parse_potfile(potfile_path: Path) -> dict:
    """Parse WPA-sec potfile into structured dict.

    Format per line: AP_MAC:CLIENT_MAC:SSID:PASSWORD
    MACs are 17 chars each (xx:xx:xx:xx:xx:xx) with internal colons.

    Returns {"by_ssid": {"SSID": [{"ap_mac", "client_mac", "password"}]},
             "count": N}
    """
    if not potfile_path.is_file():
        return {"by_ssid": {}, "count": 0}
    try:
        text = potfile_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"by_ssid": {}, "count": 0}
    return _parse_potfile_text(text)


def _parse_potfile_text(text: str) -> dict:
    """Parse potfile CONTENT (shared by the file and the live-download paths).
    Returns {"by_ssid": {...}, "count": N}; count is the number of RECOGNISED
    records, which the caller uses to reject an unparseable (e.g. HTML) body."""
    result: dict = {"by_ssid": {}, "count": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # WPA-sec potfile line: AP_MAC : CLIENT_MAC : SSID : PASSWORD. The MACs
        # come in two shapes depending on the dump: 12 hex chars with NO colons
        # (aabbccddeeff:..., colons at 12 & 25) or the older 17-char colon form
        # (aa:bb:..., colons at 17 & 35). Detect by the delimiter positions so
        # both parse — the 12-hex form is what wpa-sec returns today, and the old
        # char-level parser silently dropped every one of those lines.
        if len(line) > 26 and line[12] == ":" and line[25] == ":":
            ap_mac, client_mac, rest = line[:12], line[13:25], line[26:]
        elif len(line) > 36 and line[17] == ":" and line[35] == ":":
            ap_mac, client_mac, rest = line[:17], line[18:35], line[36:]
        else:
            continue
        if ":" not in rest:            # need SSID:PASSWORD
            continue
        ssid, password = rest.rsplit(":", 1)
        if not ssid:
            continue
        result["count"] += 1
        result["by_ssid"].setdefault(ssid, []).append(
            {"ap_mac": ap_mac, "client_mac": client_mac, "password": password})
    return result


def _save_potfile_json(loot_dir: Path, data: dict) -> None:
    """Save parsed potfile data as JSON cache (0600 — holds cracked passwords)."""
    out = loot_dir / "passwords" / "wpasec_cracked.json"
    try:
        _write_private(out, json.dumps(data, ensure_ascii=False), private_dir=True)
    except OSError:
        pass


def load_wpasec_passwords(loot_dir: Path) -> dict:
    """Load parsed WPA-sec passwords from JSON cache (or parse potfile).

    Returns {"by_ssid": {...}, "count": N} or empty dict.
    """
    json_path = loot_dir / "passwords" / "wpasec_cracked.json"
    potfile = loot_dir / "passwords" / "wpasec_cracked.potfile"
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            # Trust the cache only when it actually has entries. A count-0 cache
            # over a non-empty potfile means it was written by the old parser that
            # dropped every line — re-parse the potfile instead of trusting 0.
            if isinstance(data, dict) and data.get("count"):
                return data
            if isinstance(data, dict) and "by_ssid" in data and not potfile.is_file():
                return data
        except (json.JSONDecodeError, OSError):
            pass
    # (Re)parse the potfile: cache missing, or empty-but-potfile-has-data.
    if potfile.is_file():
        data = parse_potfile(potfile)
        _save_potfile_json(loot_dir, data)
        return data
    return {"by_ssid": {}, "count": 0}


# ── WiGLE — upload wardriving CSV to wigle.net ─────────────────────────────
# Endpoint + auth per WiGLE API v2: POST multipart 'file' to
# https://api.wigle.net/api/v2/file/upload with HTTP Basic auth (API Name :
# API Token from the account page), optional 'donate'. Our loot wardriving.csv
# is already WiGLE 1.6 format (watchdogs.wigle.row16), so it uploads as-is.

def wigle_configured() -> bool:
    return bool(_wigle_name() and _wigle_token())


def _wigle_name() -> str:
    return (os.environ.get("WDG_WIGLE_NAME")
            or os.environ.get("JANOS_WIGLE_NAME") or WIGLE_API_NAME or "")


def _wigle_token() -> str:
    return (os.environ.get("WDG_WIGLE_TOKEN")
            or os.environ.get("JANOS_WIGLE_TOKEN") or WIGLE_API_TOKEN or "")


def _wigle_state_path(app_dir) -> Path:
    return Path(app_dir) / ".wigle_sync_state.json"


def _wigle_uploaded(app_dir) -> set:
    try:
        p = _wigle_state_path(app_dir)
        if p.is_file():
            return set(json.loads(p.read_text()).get("uploaded", []))
    except Exception:
        pass
    return set()


def _wigle_mark(app_dir, name: str) -> None:
    try:
        up = _wigle_uploaded(app_dir)
        up.add(name)
        # Atomic replace so a crash mid-write can't lose the whole upload history
        # (and truncate it to nothing) (#14).
        p = _wigle_state_path(app_dir)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps({"uploaded": sorted(up)}))
        os.replace(tmp, p)
    except Exception:
        pass


def positioned_wigle_csv(path):
    """A WiGLE 1.6 CSV reduced to the rows that carry a GPS position (both header
    lines kept). Rows saved without a fix (empty or 0,0), or with only one
    coordinate, are dropped so they are never uploaded. -> (bytes, kept, dropped).

    The whole file is read through ONE csv.reader and re-emitted through a
    csv.writer, so an SSID containing a comma or an embedded newline stays a single
    logical record instead of being split into a truncated row (#20, #21). Never
    raises on bad rows."""
    import csv as _csv
    import io as _io
    out = _io.StringIO()
    writer = _csv.writer(out, lineterminator="\n")
    kept = dropped = 0
    lat_i, lon_i = 7, 8
    with open(path, "r", newline="", encoding="utf-8", errors="replace") as fh:
        for row in _csv.reader(fh):
            if not row:
                continue
            if row[0].startswith("WigleWifi"):
                writer.writerow(row)
                continue
            if row[0] == "MAC":
                if "CurrentLatitude" in row:
                    lat_i = row.index("CurrentLatitude")
                if "CurrentLongitude" in row:
                    lon_i = row.index("CurrentLongitude")
                writer.writerow(row)
                continue
            slat = row[lat_i].strip() if lat_i < len(row) else ""
            slon = row[lon_i].strip() if lon_i < len(row) else ""
            ok = False
            if slat and slon:          # require BOTH coordinates (keep explicit 0)
                try:
                    lat, lon = float(slat), float(slon)
                    ok = (not (lat == 0.0 and lon == 0.0)
                          and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0)
                except ValueError:
                    ok = False
            if ok:
                writer.writerow(row)
                kept += 1
            else:
                dropped += 1
    return out.getvalue().encode("utf-8"), kept, dropped


def upload_wigle(csv_path: Path, donate: bool = False) -> tuple[bool, str]:
    """Upload one WiGLE CSV. Returns (ok, message). ok=True also for a server
    'already have it' dedup. Never raises."""
    name, token = _wigle_name(), _wigle_token()
    if not (name and token):
        return False, "WiGLE key not configured"
    if not csv_path.is_file():
        return False, "file not found: %s" % csv_path.name
    try:
        import requests
        from requests.auth import HTTPBasicAuth
    except ImportError:
        return False, "requests library not installed"
    try:
        body, kept, dropped = positioned_wigle_csv(csv_path)
    except OSError as exc:
        return False, "cannot read %s: %s" % (csv_path.name, exc)
    if not kept:
        return False, "no rows with a GPS position yet (%d without)" % dropped
    try:
        resp = requests.post(
            WIGLE_API_URL,
            auth=HTTPBasicAuth(name, token),
            headers={"Accept": "application/json"},
            files={"file": (csv_path.name, body, "text/csv")},
            data={"donate": "on" if donate else "off"},
            timeout=120,
        )
    except Exception as exc:
        return False, "network error: %s" % exc
    if resp.status_code == 401:
        return False, "WiGLE auth failed (401) - check API Name/Token"
    if resp.status_code == 429:
        return False, "WiGLE rate limit (429) - try later (free tier daily cap)"
    # Success requires a real JSON object with an explicit success flag. A 200
    # carrying a login/proxy HTML page (JSON decode fails) or a JSON list (no
    # .get) must NOT be treated as an accepted upload, or the session gets marked
    # uploaded and its data is lost (#8).
    try:
        j = resp.json()
    except Exception:
        return False, "unexpected non-JSON response (HTTP %d)" % resp.status_code
    if not isinstance(j, dict):
        return False, "unexpected response shape (HTTP %d)" % resp.status_code
    if resp.status_code != 200:
        return False, str(j.get("message") or j.get("error") or ("HTTP %d" % resp.status_code))
    if j.get("success"):
        tid = j.get("transid") or j.get("fileName") or ""
        return True, ("accepted%s" % ((" " + str(tid)) if tid else ""))
    return False, str(j.get("message") or j.get("error") or ("HTTP %d" % resp.status_code))


def upload_wigle_all(loot_dir: Path, app_dir, donate: bool = False,
                     keep_pending: str = "") -> tuple[int, int, str]:
    """Upload every session's wardriving.csv that has data and hasn't been sent
    yet (tracked in .wigle_sync_state.json). Returns (uploaded, total, summary).
    Serial, polite to the free-tier daily cap — stops early on a rate limit.

    A module-level lock makes two concurrent callers (manual upload + automatic
    cluster sync) mutually exclusive, so they can't both grab the same pending
    sessions and upload each twice (#14)."""
    if not _wigle_sync_lock.acquire(blocking=False):
        return 0, 0, "a WiGLE upload is already running"
    try:
        return _upload_wigle_all_locked(loot_dir, app_dir, donate, keep_pending)
    finally:
        _wigle_sync_lock.release()


def _upload_wigle_all_locked(loot_dir: Path, app_dir, donate: bool,
                             keep_pending: str) -> tuple[int, int, str]:
    base = Path(loot_dir)
    if not base.is_dir():
        return 0, 0, "no loot directory"
    already = _wigle_uploaded(app_dir)
    pending = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        wd = d / "wardriving.csv"
        if not wd.is_file() or d.name in already:
            continue
        # skip empty logs (WiGLE 1.6: pre-header + header, so >2 lines = data)
        try:
            with open(wd, encoding="utf-8", errors="replace") as fh:
                if sum(1 for _ in fh) <= 2:
                    continue
        except OSError:
            continue
        pending.append(d)
    if not pending:
        return 0, 0, "nothing new to upload"
    uploaded = 0
    last = ""
    for d in pending:
        ok, msg = upload_wigle(d / "wardriving.csv", donate=donate)
        last = msg
        if ok:
            uploaded += 1
            if d.name != keep_pending:          # the active session keeps growing:
                _wigle_mark(app_dir, d.name)    # re-offer it next time
        elif "rate limit" in msg.lower():
            return uploaded, len(pending), "rate limit hit - %d/%d sent, rest later" % (uploaded, len(pending))
        elif "not configured" in msg.lower():
            return 0, len(pending), msg
    return uploaded, len(pending), last or ("%d uploaded" % uploaded)
