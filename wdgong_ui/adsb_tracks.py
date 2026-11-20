"""ADS-B tracks of one loot session: load, receiver coverage, KML export.

adsb_aircraft.csv (written by the SDR module) is a track log: one row per new
position of an aircraft. adsb_receiver.csv (written by the game while ADS-B runs
and the GPS has a fix) holds where the receiver was, so range is measured from
the real listening point even when the receiver moves.

Pure stdlib, no raylib: the UI draws what load() returns.
"""
import bisect
import csv
import math
import os
import statistics
from xml.sax.saxutils import escape

SECTORS = 36                     # coverage resolution: 10 degrees
MAX_POINTS = 200_000             # guard against a runaway file
RX_FILE = "adsb_receiver.csv"
AC_FILE = "adsb_aircraft.csv"


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def km_between(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def bearing(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _read_receiver(session_dir):
    """[(t, lat, lon)] sorted by time, from adsb_receiver.csv."""
    out = []
    try:
        with open(os.path.join(session_dir, RX_FILE), encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                lat, lon = _f(r.get("lat")), _f(r.get("lon"))
                if lat or lon:
                    out.append((int(_f(r.get("timestamp"))), lat, lon))
    except OSError:
        pass
    out.sort()
    return out


def _wardriving_fix(session_dir):
    """Median position of the session's own wardriving rows, or None."""
    lats, lons = [], []
    try:
        with open(os.path.join(session_dir, "wardriving.csv"), encoding="utf-8", errors="replace") as f:
            for ln in f:
                p = ln.split(",")
                if len(p) > 7:
                    lat, lon = _f(p[6], None), _f(p[7], None)
                    if lat and lon and -90 <= lat <= 90 and -180 <= lon <= 180:
                        lats.append(lat)
                        lons.append(lon)
    except OSError:
        pass
    return (statistics.median(lats), statistics.median(lons)) if lats else None


def load(session_dir, current_fix=None) -> dict:
    """Read a session's tracks. current_fix: (lat, lon) of the game's GPS now, used
    only when the session itself has no receiver position. Returns:
      tracks   {icao: {"callsign", "pts": [(t, lat, lon, alt_ft, speed_kt, heading)]}}
      receiver (lat, lon) or None;  rx_source "gps-log" | "wardriving" | "gps-now" | "estimate"
      coverage [km per 10-degree sector, from north clockwise];  max_km, max_icao
      bounds   (min_lat, min_lon, max_lat, max_lon);  n_points, n_aircraft
    """
    tracks = {}
    n = 0
    try:
        with open(os.path.join(session_dir, AC_FILE), encoding="utf-8", errors="replace") as f:
            for r in csv.DictReader(f):
                icao = (r.get("icao") or "").strip().upper()
                lat, lon = _f(r.get("lat")), _f(r.get("lon"))
                if not icao or not (lat or lon) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    continue
                tr = tracks.setdefault(icao, {"callsign": "", "pts": []})
                cs = (r.get("callsign") or "").strip()
                if cs:
                    tr["callsign"] = cs
                pt = (int(_f(r.get("timestamp"))), lat, lon, int(_f(r.get("alt_ft"))),
                      int(_f(r.get("speed_kt"))), int(_f(r.get("heading"))))
                if tr["pts"] and tr["pts"][-1][1:4] == pt[1:4]:
                    continue                      # older logs repeat the same position
                tr["pts"].append(pt)
                n += 1
                if n >= MAX_POINTS:
                    break
    except OSError:
        pass

    rx_log = _read_receiver(session_dir)
    if rx_log:
        receiver, src = (statistics.median(p[1] for p in rx_log), statistics.median(p[2] for p in rx_log)), "gps-log"
    else:
        receiver, src = _wardriving_fix(session_dir), "wardriving"
        if receiver is None and current_fix and (current_fix[0] or current_fix[1]):
            receiver, src = (float(current_fix[0]), float(current_fix[1])), "gps-now"
        if receiver is None and tracks:
            # no position at all: the middle of what was heard (aircraft surround
            # the receiver) - shown as an estimate
            allp = [p for tr in tracks.values() for p in tr["pts"]]
            receiver = (statistics.median(p[1] for p in allp), statistics.median(p[2] for p in allp))
            src = "estimate"

    coverage = [0.0] * SECTORS
    max_km, max_icao = 0.0, ""
    rx_t = [p[0] for p in rx_log]
    lats, lons = [], []
    for icao, tr in tracks.items():
        for t, lat, lon, *_ in tr["pts"]:
            lats.append(lat)
            lons.append(lon)
            if receiver is None:
                continue
            if rx_log:                            # nearest receiver fix in time
                i = bisect.bisect_left(rx_t, t)
                cand = [rx_log[j] for j in (i - 1, i) if 0 <= j < len(rx_log)]
                rx = min(cand, key=lambda p: abs(p[0] - t))
                rlat, rlon = rx[1], rx[2]
            else:
                rlat, rlon = receiver
            d = km_between(rlat, rlon, lat, lon)
            s = int(bearing(rlat, rlon, lat, lon) // (360 / SECTORS)) % SECTORS
            if d > coverage[s]:
                coverage[s] = d
            if d > max_km:
                max_km, max_icao = d, icao
    if receiver:
        lats.append(receiver[0])
        lons.append(receiver[1])
    return {"tracks": tracks, "receiver": receiver, "rx_source": src if receiver else "",
            "coverage": coverage, "max_km": max_km, "max_icao": max_icao,
            "bounds": (min(lats), min(lons), max(lats), max(lons)) if lats else None,
            "n_points": n, "n_aircraft": len(tracks)}


def alt_color(alt_ft):
    """Altitude -> (r, g, b): low warm, high cool - the usual flight-tracker scale."""
    stops = ((0, (255, 176, 32)), (10000, (255, 230, 80)), (20000, (80, 230, 120)),
             (30000, (53, 230, 255)), (40000, (180, 120, 255)))
    a = max(0, min(45000, alt_ft))
    for (a0, c0), (a1, c1) in zip(stops, stops[1:]):
        if a <= a1:
            k = (a - a0) / float(a1 - a0)
            return tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
    return stops[-1][1]


def write_kml(session_dir, data, name=None) -> str:
    """Write <session>/adsb_tracks.kml: one 3D line per aircraft (absolute altitude,
    coloured by its highest altitude), the receiver, and the coverage outline.
    Opens in Google Earth and most map apps. Returns the file path."""
    name = name or os.path.basename(os.path.normpath(session_dir))
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           "<name>%s</name>" % escape("ADS-B " + name),
           "<description>%s</description>" % escape(
               "%d aircraft, %d positions. Max range %.0f km. Recorded with WatchDogsGo NG."
               % (data["n_aircraft"], data["n_points"], data["max_km"]))]
    for icao, tr in sorted(data["tracks"].items()):
        pts = tr["pts"]
        if not pts:
            continue
        r, g, b = alt_color(max(p[3] for p in pts))
        label = tr["callsign"] or icao
        out.append("<Placemark><name>%s</name>" % escape(label))
        out.append("<description>%s</description>" % escape(
            "ICAO %s, %d positions, altitude %d-%d ft" % (icao, len(pts), min(p[3] for p in pts),
                                                          max(p[3] for p in pts))))
        out.append("<Style><LineStyle><color>ff%02x%02x%02x</color><width>3</width></LineStyle>"
                   "<PolyStyle><color>40%02x%02x%02x</color></PolyStyle></Style>" % (b, g, r, b, g, r))
        if len(pts) == 1:
            p = pts[0]
            out.append("<Point><altitudeMode>absolute</altitudeMode><coordinates>%.6f,%.6f,%.0f"
                       "</coordinates></Point>" % (p[2], p[1], p[3] * 0.3048))
        else:
            out.append("<LineString><extrude>1</extrude><tessellate>1</tessellate>"
                       "<altitudeMode>absolute</altitudeMode><coordinates>")
            out.append(" ".join("%.6f,%.6f,%.0f" % (p[2], p[1], p[3] * 0.3048) for p in pts))
            out.append("</coordinates></LineString>")
        out.append("</Placemark>")
    rx = data["receiver"]
    if rx:
        note = {"gps-log": "GPS during the session", "wardriving": "session wardriving fixes",
                "gps-now": "GPS when exported", "estimate": "estimated from the aircraft heard"}
        out.append("<Placemark><name>Receiver</name><description>%s</description><Point><coordinates>"
                   "%.6f,%.6f,0</coordinates></Point></Placemark>"
                   % (escape("position: " + note.get(data["rx_source"], "")), rx[1], rx[0]))
        ring = []
        for s in range(SECTORS + 1):
            d = data["coverage"][s % SECTORS]
            brg = math.radians((s % SECTORS + 0.5) * 360 / SECTORS)
            dlat = d / 111.32 * math.cos(brg)
            dlon = d / (111.32 * max(0.01, math.cos(math.radians(rx[0])))) * math.sin(brg)
            ring.append("%.6f,%.6f,0" % (rx[1] + dlon, rx[0] + dlat))
        out.append("<Placemark><name>Coverage (max range per 10 degrees)</name><Style><LineStyle>"
                   "<color>ffffe635</color><width>2</width></LineStyle></Style><LineString><tessellate>1"
                   "</tessellate><coordinates>%s</coordinates></LineString></Placemark>" % " ".join(ring))
    out.append("</Document></kml>")
    path = os.path.join(session_dir, "adsb_tracks.kml")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    os.replace(tmp, path)
    return path
