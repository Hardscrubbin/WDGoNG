#!/usr/bin/env python3
"""Desktop simulator for WatchDogsGo EXTRAS — test an extra WITHOUT the game.

    python3 extras/sim/run_in_sim.py <path-to-extra-dir> [frames]

Loads the extra against a FAKE ctx (no raylib): a simulated moving GPS fix,
fake loot, a temp storage dir, and a text renderer that prints every ctx.text()
to the console. Runs the lifecycle (on_enter -> N x on_frame -> on_exit) so you
can iterate on a laptop. Reuses the real platform (wdgo_extras) so you test the
exact facade the game uses.
"""
import os
import sys
import math
import time
import tempfile
import types


def _find_sdk(here):
    """Directory holding wdgo_extras.py: a 'sdk' dir next to this tool (the hub
    repository layout), else the game package found near the repository root."""
    cand = [os.path.join(here, "sdk")]
    root = here
    for _ in range(3):
        root = os.path.dirname(root)
        try:
            cand += [os.path.join(root, d) for d in sorted(os.listdir(root))]
        except OSError:
            pass
    for c in cand:
        if os.path.isfile(os.path.join(c, "wdgo_extras.py")):
            return c
    return os.path.join(here, "sdk")

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _find_sdk(_HERE))
import wdgo_extras as wx  # noqa: E402


class _SimGps:
    """A fix that drifts ~north-east so speed/heading are non-zero."""
    def __init__(self):
        self.lat, self.lon = 50.0610, 19.9370   # Krakow-ish
        self.t0 = time.time()

    def __call__(self):
        dt = time.time() - self.t0
        return {"valid": True,
                "lat": self.lat + 0.00008 * dt,
                "lon": self.lon + 0.00005 * dt,
                "sats": 9, "speed_kmh": 0.0, "heading": 0.0}


def _print_text(s, x, y, size, color, bold):
    print("  text(%-7s %3d,%3d %2dpx %-7s%s) %s"
          % ("", int(x), int(y), int(size), color, "*" if bold else " ", s))


def make_bridge():
    gps = _SimGps()
    b = types.SimpleNamespace()
    b.draw_text = _print_text
    b.draw_rect = lambda *a, **k: None
    b.draw_line = lambda *a, **k: None
    b.draw_circle = lambda *a, **k: None
    b.draw_image = lambda name, w, h, rgba, x, y, scale, alpha: print(
        "  image(%s %dx%d at %d,%d x%d)" % (name, w, h, int(x), int(y), scale))
    b.measure = lambda s, size=16: len(str(s)) * size * 0.55
    b.key_down = lambda name: False
    b.key_pressed = lambda name: False
    b.view = lambda: (1232, 616)
    b.gps = gps
    b.loot_networks = lambda: [{"ssid": "SIM-AP", "channel": 6}]
    b.loot_counts = lambda: {"networks": 1, "devices": 0}
    b.log = lambda msg, level="info": print("  [log/%s] %s" % (level, msg))
    b.chars = lambda: []            # no typing in the sim
    def _loot_save(name, text):     # write into a temp "loot" so you can inspect it
        import os
        base = os.path.join(tempfile.gettempdir(), "wdgo_sim_loot", "key_copier")
        os.makedirs(base, exist_ok=True)
        p = os.path.join(base, os.path.basename(str(name)))
        with open(p, "w", encoding="utf-8") as f:
            f.write(str(text))
        print("  [loot_save] %s" % p)
        return p
    b.loot_save = _loot_save
    b.release_all = lambda: None
    return b


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    sandbox = "--sandbox" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--sandbox"]
    d = os.path.abspath(args[0])
    frames = int(args[1]) if len(args) > 1 else 20

    meta = wx._load_meta(d)
    print("== %s v%s by %s ==" % (meta.name, meta.version, meta.author or "?"))
    print("   id=%s api=%s perms=%s hw=%s"
          % (meta.id, meta.api, meta.permissions, meta.hardware))
    print("   %s" % (meta.description or "(no description)"))
    if meta.error:
        print("!! manifest INVALID: %s" % meta.error)
        sys.exit(2)

    data_dir = os.path.join(tempfile.gettempdir(), "wdgo_sim_" + meta.id)
    if sandbox:
        import wdgo_extras_sandbox as sb
        host = sb.SandboxHost(meta, make_bridge(), data_dir,
                              require_isolation=sb.full_isolation_possible())
        host.open()
        t_end = time.time() + 10
        while host.state == "loading" and time.time() < t_end:
            host.frame(0.0)
            time.sleep(0.01)
        print("-- sandbox: isolation=%s hash=%s --" % (host.isolation, host.content_hash[:16]))
    else:
        host = wx.ExtraHost(meta, make_bridge(), data_dir)
        host.open()
    if host.state == "faulted":
        print("!! on_load/on_enter faulted: %s" % host.error)
        sys.exit(3)

    print("-- running %d frames --" % frames)
    for i in range(frames):
        print("frame %d:" % i)
        host.frame(1.0 / 30.0)
        if host.state == "faulted":
            print("!! on_frame faulted: %s" % host.error)
            break
        time.sleep(0.05)
    host.close()
    print("-- done (state=%s) --" % host.state)


if __name__ == "__main__":
    main()
