"""Offline ctOS basemap tiles from an MBTiles (SQLite) file.

Whole-world z0-6 vector-rendered raster tiles in the WDGWorldMap/ctOS style,
packed into a single ~29 MB MBTiles. Read-only, stdlib sqlite3 only (no deps).
The game reads a tile's PNG bytes by (z,x,y) and uploads it to a GPU texture
ONCE, then blits the cached texture every frame (see wdgong_ui/main.py). That is
the opposite of the OG tile path (no per-pixel Python, no live download).

MBTiles stores rows in TMS order (y flipped); XYZ/slippy callers are converted.
"""

import math
import os
import sqlite3


class Basemap:
    def __init__(self, path: str):
        self.ok = False
        self._db = None
        self.minzoom, self.maxzoom = 0, 6
        try:
            if path and os.path.isfile(path):
                # read-only, shared across threads (reads only, sqlite handles it)
                self._db = sqlite3.connect("file:%s?mode=ro" % path, uri=True,
                                           check_same_thread=False)
                for name, attr in (("minzoom", "minzoom"), ("maxzoom", "maxzoom")):
                    r = self._db.execute(
                        "SELECT value FROM metadata WHERE name=?", (name,)).fetchone()
                    if r:
                        setattr(self, attr, int(r[0]))
                self.ok = True
        except Exception:
            self.ok = False
            self._db = None

    def tile(self, z: int, x: int, y: int):
        """PNG bytes for XYZ tile (z,x,y), or None if absent/unavailable."""
        if not self.ok or self._db is None:
            return None
        try:
            row = (1 << z) - 1 - y            # XYZ y -> TMS row
            r = self._db.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? "
                "AND tile_row=?", (z, x, row)).fetchone()
            return bytes(r[0]) if r else None
        except Exception:
            return None

    def close(self):
        try:
            if self._db is not None:
                self._db.close()
        except Exception:
            pass
        self._db = None


# --- Web Mercator / slippy-map tile math ---
WEB_MERC_LAT = 85.05112878


def lonlat_to_tile(lon: float, lat: float, z: int):
    """lon/lat -> (x, y) tile index at zoom z (clamped in-range)."""
    n = 1 << z
    x = int((lon + 180.0) / 360.0 * n)
    lat = max(-WEB_MERC_LAT, min(WEB_MERC_LAT, lat))
    lat_r = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_r)) / math.pi) / 2.0 * n)
    return (max(0, min(n - 1, x)), max(0, min(n - 1, y)))


def tile_corner(x: int, y: int, z: int):
    """NW corner (lon, lat) of tile (x, y, z)."""
    n = 1 << z
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return (lon, lat)
