"""WiGLE wardriving CSV format — single source of truth (WiGLE 1.6, header-aware).

Both old 1.4 (11 columns) and new 1.6 (14 columns) files are READABLE: we locate
columns by the header row, never by fixed index. New files are WRITTEN as 1.6, the
same 14-column layout the projectZero firmware streams, so a serial-wardrive row from
the XIAO node is appended to the host CSV verbatim (after the host stamps GPS + time).

1.6 header:
  MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,
  CurrentLongitude,AltitudeMeters,AccuracyMeters,RCOIs,MfgrId,Type
"""
from __future__ import annotations

import csv
import io
from typing import Optional

PRE_HEADER = (
    "WigleWifi-1.6,appRelease=WatchDogsGo,model=uConsole,release=1.0,"
    "device=WatchDogsGo,display=Pyxel,board=ESP32,brand=LOCOSP,"
    "star=Sol,body=3,subBody=0\n"
)
HEADER_COLS = [
    "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "Frequency", "RSSI",
    "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters",
    "RCOIs", "MfgrId", "Type",
]
HEADER = ",".join(HEADER_COLS) + "\n"

# Canonical column index in the 1.6 layout.
IDX = {name: i for i, name in enumerate(HEADER_COLS)}

# How a 1.4 (11-col) header maps onto canonical names, in file order.
_V14_ORDER = [
    "MAC", "SSID", "AuthMode", "FirstSeen", "Channel", "RSSI",
    "CurrentLatitude", "CurrentLongitude", "AltitudeMeters", "AccuracyMeters", "Type",
]


def channel_to_freq_mhz(channel) -> int:
    """WiGLE Frequency column (MHz) from a Wi-Fi channel number. 0 if unknown."""
    try:
        ch = int(str(channel).strip())
    except (ValueError, TypeError):
        return 0
    if 1 <= ch <= 13:
        return 2407 + ch * 5
    if ch == 14:
        return 2484
    if 32 <= ch <= 177:          # 5 GHz (and 6 GHz uses same arithmetic here, close enough)
        return 5000 + ch * 5
    return 0


def header_map(header_line: str) -> dict:
    """Return {canonical_name: column_index} for a wardriving.csv data header row.

    Works for the real header (starts with MAC) in either 1.4 or 1.6; falls back to
    the 1.6 layout if the row isn't a recognised header.
    """
    cols = [c.strip() for c in header_line.rstrip("\n").split(",")]
    if cols and cols[0].upper() == "MAC":
        m = {}
        for i, c in enumerate(cols):
            # normalise a couple of aliases
            key = {"LAT": "CurrentLatitude", "LON": "CurrentLongitude"}.get(c, c)
            m[key] = i
        # a bare 1.4 with no explicit names still lands here by position
        return m
    # Not a header: assume positional 1.4 (legacy files sometimes lack a clean header)
    return {name: i for i, name in enumerate(_V14_ORDER)}


def get(parts: list, cmap: dict, name: str, default: str = "") -> str:
    i = cmap.get(name)
    if i is None or i >= len(parts):
        return default
    return parts[i]


def is_pre_header(line: str) -> bool:
    return line.startswith("WigleWifi")


def is_header(line: str) -> bool:
    return line.lstrip().upper().startswith("MAC,")


def parse_file(path) -> Optional[list]:
    """Yield data rows of a wardriving.csv as (parts, cmap). Header-aware, comma-safe.

    Returns None on error. The caller reads columns via get(parts, cmap, "RSSI") etc.
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            text = fh.read()
    except OSError:
        return None
    cmap = {name: i for i, name in enumerate(_V14_ORDER)}  # default until we see a header
    first = True
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        line0 = row[0]
        if is_pre_header(line0):
            continue
        if first and is_header(",".join(row)):
            cmap = header_map(",".join(row))
            first = False
            continue
        first = False
        if is_header(",".join(row)):   # defensive: a stray header mid-file
            continue
        out.append((row, cmap))
    return out


def row16(mac, ssid, auth, first_seen, channel, rssi,
          lat, lon, alt, acc, mfgr_id="", rcois="", rtype="WIFI") -> str:
    """Build one WiGLE 1.6 data row (14 columns). SSID is quoted if it needs it."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([
        mac, ssid, auth, first_seen, channel, channel_to_freq_mhz(channel), rssi,
        lat, lon, alt, acc, rcois, mfgr_id, rtype,
    ])
    return buf.getvalue()
