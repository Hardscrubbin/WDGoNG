# Writing an EXTRA for WatchDogsGo NG

An **extra** is a self-contained mini-app that shows up under the **EXTRAS** menu and
runs inside the game, drawing into its own screen and reading the hardware
capabilities it declares. You write one folder, drop it in, and the game picks it up —
**no changes to the game's code.**

> **Your extra runs sandboxed.** By default every extra runs in a locked-down,
> **non-root subprocess with no filesystem, no network, no subprocess, and no
> threads** — it can only draw, read input, and use the capabilities it declares, all
> through `ctx`. `ctx` is the *only* thing that crosses that boundary. Code the **user**
> explicitly trusts (the bundled examples, or an extra whose content hash they pin) may
> run in-process instead, but **write for the sandbox** — it is the default and it is
> what the hub will always use. Build against `ctx` only (see §6 for exactly what works)
> and the same code runs in both tiers unchanged.

---

## 1. Where extras live & the EXTRAS API version

- Your extras live in **`~/.wdgo/extras/<id>/`** (user dir — it survives game updates;
  the game's OTA never touches it). That folder is all the EXTRAS list shows: what the
  Hub installed and what you copied in by hand. The reference examples under
  `extras/examples/` are not listed by default; start the game with
  `WDGO_EXTRAS_EXAMPLES=1` to list them too while developing.
- Each extra's writable storage is **`~/.wdgo/extras-data/<id>/`** (separate from the
  code, so it persists across updates); reach it only via `ctx.storage`.
- The game advertises an integer **`EXTRAS_API`** (currently **2**; `2` added images).
  Your manifest must declare the `api` it was written for - use `2` if you call
  `ctx.image`, `1` otherwise; the game refuses anything newer than it supports.

## 2. Naming conventions (follow these exactly)

| Thing | Rule |
|---|---|
| **Extra id** | `^[a-z0-9_]{3,32}$` — lowercase, digits, underscore. 3–32 chars. |
| **Folder name** | **MUST equal the id.** `~/.wdgo/extras/<id>/`. |
| **Manifest** | `extra.json` in the extra's folder (see §3). |
| **Entry module** | a Python file, default `main.py`; the class default is `Extra`. Override with `"entry": "module:ClassName"`. |
| **Entry class** | a plain class implementing the lifecycle hooks (§4). |
| **Writable data** | only `~/.wdgo/extras-data/<id>/` (the game creates it) — reach it via `ctx.storage`. Never write anywhere else. |
| **Version** | your extra's own `semver` (e.g. `1.2.0`), independent of the game. |

Pick an id that reads like a package: `gps_speedo`, `mesh_chat`, `loot_stats`.

## 3. The manifest — `extra.json`

The game reads this **without importing your code**. It is also exactly what will
**describe your extra in the hub** later, so fill in `description` and `about` well.

```json
{
  "extra": {
    "id": "gps_speedo",
    "name": "GPS Speedometer",
    "author": "yourname",
    "version": "1.0.0",
    "api": 1,
    "entry": "main:Extra",
    "permissions": ["gps"],
    "hardware": ["gps"],
    "category": "tools",
    "tags": ["gps", "speed", "navigation"],
    "description": "Live speed + heading from your GPS fix.",
    "about": "Longer text: what it does and how it works. Shown on the hub detail page.",
    "homepage": ""
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `id` | ✅ | unique, == folder name, id-regex above. |
| `name` | ✅ | menu label (keep ≤ 18 chars visible). |
| `author` | ✅ | your handle — shown in the menu and hub, never trusted. |
| `version` | ✅ | your extra's semver. |
| `api` | ✅ | integer EXTRAS_API you built against (`1`, or `2` if you draw images). |
| `entry` | — | `module:Class`, default `main:Extra`. |
| `permissions` | — | capabilities you request (§5). Only what you actually use. |
| `hardware` | — | managers you rely on: `gps`, `lora`, `sdr`, `esp32`, `aio` (informational in v1). |
| `icon` | — | relative path to a 64×64 PNG, else a glyph is generated. |
| **`description`** | ✅ (strongly) | **one line — what it is.** Menu subtitle + hub card. |
| **`about`** | ✅ (strongly) | **the full story: what it does and how it works.** Hub detail page. Write this for a stranger browsing the hub. |
| `category` | — | one of e.g. `tools`, `games`, `mesh`, `radio`, `map`, `fun`, `misc`. Groups it in the hub. |
| `tags` | — | up to 8 short keywords for hub search. |
| `homepage` | — | optional https/http link to your own repo/docs, shown on the hub listing. Not required: a hub extra's reviewed code lives in the hub repo itself, which the game links from the listing. |

**Write `description` and `about` as if someone is deciding whether to install your
extra.** `description` = the one-liner in a list; `about` = what it does, how it works,
what hardware it needs, and how to use it (keys). These are the hub's listing data.

## 4. The entry module & lifecycle

```python
class Extra:
    def on_load(self, ctx):      ...  # once, after import. Heavy setup. May raise -> disabled.
    def on_enter(self, ctx):     ...  # each time the user opens the extra.
    def on_frame(self, ctx, dt): ...  # EVERY frame while open. Draw here, via ctx only. dt = seconds.
    def on_event(self, ctx, ev): ...  # optional: key events not consumed by the host.
    def on_exit(self, ctx):      ...  # user pressed ESC / closed. Release transient state.
    def on_unload(self, ctx):    ...  # game shutdown / rescan. Release everything.
```

Only **`on_frame`** is required; the rest are optional.

Rules you must follow:
- **The host owns the frame and ESC.** Draw only through `ctx`, only inside your viewport
  (local coords `0,0 .. ctx.w,ctx.h`). **Never** `import pyray`, open a window, or call
  `begin_drawing`. **ESC always returns to the menu** — you never see it.
- **Never block** in `on_frame` (no `time.sleep`, no long loops, no blocking I/O). You get
  `dt` for animation; do a little work per frame.
- **Crashes are contained:** if any hook raises, the game disables your extra and shows the
  reason. It won't take the game down — but a broken extra is a disabled extra, so handle
  your own errors.

## 5. The `ctx` API (v1)

### Drawing (local coords, clamped to your viewport)
```python
ctx.w, ctx.h                      # your viewport size (ints)
ctx.text(s, x, y, size=16, color="txt", bold=False)
ctx.rect(x, y, w, h, color="txt", fill=True, alpha=255)
ctx.line(x1, y1, x2, y2, color="txt", thick=1)
ctx.circle(x, y, r, color="txt")
ctx.measure(s, size=16) -> width_px
```
**Colors are names only** (keeps the game's theme consistent and is required for the
sandbox): `txt, hi, dim, teal, danger, warn, success, magenta, ice, void, edge, purple`.

### Images and the icon (api 2)
Put plain **PNG** files in your extra's folder (subfolders are fine) and draw them:
```python
ctx.image("logo.png", x, y, scale=1, alpha=255)   # scale: whole number 1..8, pixels stay crisp
ctx.image_size("logo.png") -> (width, height)
```
| File | Limits | Used for |
|---|---|---|
| `icon.png` (in the extra's root) | at most **64x64** px and **16 KB** | the extra's icon in the store listing. Optional. You can also draw it yourself with `ctx.image("icon.png", ...)`. |
| any other `*.png` | at most **512x512** px and **256 KB** each | whatever your extra draws |

An extra may ship at most **8** PNG files and **512 KB** of them in total. Keep them
small: every player's device downloads the whole hub. A 16-colour greyscale logo at
384x384 is about 40 KB; reduce colours (`pngquant`, or "indexed" export in an image
editor) before you reach for a bigger file.

Supported: 8-bit greyscale, RGB, RGBA, grey+alpha, and 1/2/4/8-bit palette or
greyscale, with transparency. Not supported: interlaced and 16-bit PNG, and every
other format (no JPEG, GIF, SVG, WebP) - `hub_check.py` and the simulator tell you
exactly what is wrong with a file. Images are part of your extra's checksum, so they
are reviewed and signed together with the code. A name that does not exist raises
`KeyError`. Large images appear a few frames after the first draw call.

### Input (only while your extra is active)
```python
ctx.key_down(name)     # held
ctx.key_pressed(name)  # this frame
ctx.chars()            # list of printable chars typed this frame (for building text, e.g. a filename)
# names: up down left right enter space tab backspace minus equal comma period
#        slash bracket_left bracket_right, or a single letter/digit "a".."z","0".."9"
```

### Capabilities (present only if you declared the permission AND the hardware exists, else `None`)
```python
ctx.gps          # permission "gps";       ctx.gps.fix() -> {valid,lat,lon,sats,speed_kmh,heading} | None
ctx.loot         # permission "loot_read"; ctx.loot.networks() -> [..], ctx.loot.counts() -> {..}
ctx.loot_save    # permission "loot_write"; ctx.loot_save(name, text) -> abs path | None
                 #   writes ONLY into loot/extras/<id>/<name> (your own namespace in the loot tree)
ctx.mesh         # None in v1 (gated LoRa send/recv via the broker arrives in a later API)
ctx.require("gps")  # returns the cap or raises if not granted — nice for on_enter
```
**Always null-check** a capability (the user may run your extra without that hardware).

### Storage, log, clock (always available)
```python
ctx.storage.path                      # your data/ dir
ctx.storage.read(name, default="")    # read a file (basename only, confined to data/)
ctx.storage.write(name, text)         # write a file
ctx.storage.read_json(name, default=None)
ctx.storage.write_json(name, obj)
ctx.log("message", "info")            # into the game terminal ("info"|"ok"|"warn"|"error")
ctx.now()                             # seconds (float), for timing
```

### Permissions you may request in v1
`gps` (read the fix) · `loot_read` (read scan/wardrive counts & lists) · `loot_write`
(save files into `loot/extras/<id>/` only — your own namespace in the loot tree; never a
core loot file). `mesh`, `sdr`, `esp` and portal arrive in later API versions, each behind
its own permission.

## 6. What the sandbox gives you — and what it doesn't

Your extra runs in a jail. It can `import` only a fixed set of **pure-computation**
stdlib modules, and it reaches the outside world **only through `ctx`**:

**Importable stdlib:** `math cmath json time random re collections itertools functools
operator datetime calendar struct base64 binascii hashlib hmac string textwrap
statistics enum dataclasses typing abc copy heapq bisect array decimal fractions numbers
zlib colorsys difflib unicodedata io codecs weakref contextlib types pprint secrets
fnmatch html csv uuid` (+ your own modules in the extra's folder).

**Does NOT work** — these fail at runtime in the sandbox, so don't reach for them:
- **the filesystem** — no `open()`, no `pathlib` file I/O, no `os.listdir`; even your
  own files aren't readable. Keep persistent data in **`ctx.storage`** and ship any
  tables/constants **in your code**. (Saving to loot is `ctx.loot_save(...)`, not `open`.)
- **network** — no `socket`, `urllib`, `http`, `requests`, …
- **processes / threads** — no `subprocess`, `multiprocessing`, `os.fork`, `os.system`,
  and **no `threading`**. Spread work across `on_frame` calls instead.
- **native / third-party code** — no `ctypes`, no `.so`, no `pip` packages, no `import pyray`.

Also enforced at runtime: a **512 MB** memory cap, and a **watchdog** that kills the
extra if `on_frame` doesn't return — so **never block or loop forever**, and draw at most
a few thousand primitives per frame. Request the **least permissions** you need: the user
sees them on a consent card, and a changed extra re-asks (it is pinned to its content hash).

> If your extra works in the plain sim but breaks in the game with a `ModuleNotFoundError`
> or a missing file, it tried to leave the sandbox — move that logic to `ctx`/`ctx.storage`
> or drop it. Test with `--sandbox` (§7) to catch it on your laptop.

## 7. Test it without a uConsole — the sim harness

```bash
python3 extras/sim/run_in_sim.py ~/.wdgo/extras/gps_speedo 30
# or, from the repo examples:
python3 extras/sim/run_in_sim.py extras/examples/gps_speedo 30
```
It loads your extra against a **fake `ctx`** (a simulated moving GPS fix, fake loot, a
temp storage dir) and prints every `ctx.text(...)` plus any fault — so you iterate on a
laptop. A manifest error or a crash is reported with its reason.

**Before you ship, run it under the real sandbox** with `--sandbox` — this catches a
forbidden `import` or file access that the plain run would miss:
```bash
python3 extras/sim/run_in_sim.py extras/examples/gps_speedo 30 --sandbox
```
It runs your extra in the actual Phase-2 runtime (separate process, draw-ops over IPC,
the capability broker). On a Linux box **as root** it's the exact kernel jail the game
uses; on a laptop the kernel jail is off but the **import allowlist and broker rules are
identical**, so a `ModuleNotFoundError` here means it will fail on the device too.

## 8. Install & run on the device

1. Copy your folder to **`~/.wdgo/extras/<id>/`** (or drop a `.zip`/`.wdgx` there — the
   game unpacks it).
2. In the game: open the menu (**TAB**) → **EXTRAS**. Your extra is listed (a broken one
   shows greyed with the reason). Hit **ENTER** to launch, **ESC** to return.
3. Added/changed an extra while the game runs? Select **Rescan** at the top of EXTRAS.

### Getting it from the hub instead

**EXTRAS > Hub** lists reviewed extras. The game checks the catalogue's signature and
each extra's checksum before installing; `[I]` installs or updates, `[X]` removes
(your saved data stays). The first time any non-bundled extra runs — from the hub or
copied by hand — the game shows a **consent card** with its source, fingerprint and
the permissions it asks for; `[Y]` allows it. It asks again only if the code or the
permissions change.

## 9. Minimal example

See `extras/examples/gps_speedo/` — a complete, commented extra (manifest + `main.py`)
that reads the GPS fix, computes speed, draws it, and handles a key. Copy it as a starting
point.

`extras/examples/hello_wardriver/` is the reference for **images and the icon**: a
three-page card with `icon.png` (48x48) and a larger `logo.png`, no permissions. Its
folder is the layout to copy:

    hello_wardriver/
        extra.json      manifest ("api": 2 because it draws images)
        main.py         the Extra class
        icon.png        store icon, also drawn in the header
        logo.png        artwork drawn on the first page

## 10. Versioning & publishing to the hub

- Your extra declares `api`. When the game bumps `EXTRAS_API`, old extras keep running
  (back-compat shims); an extra built for a newer game than installed is shown disabled
  with *"needs a newer game — run [U]"*.
- The **hub** accepts extras via **GitHub pull request**: CI (`hub_check.py`) validates the
  manifest, scans imports/forbidden calls/permissions and smoke-runs the extra in the
  sandbox; a maintainer reviews and merges; the maintainer then signs the catalogue
  (offline) and publishes the release - that is when players see it. Run the same
  check yourself before submitting:
  `python3 extras/hub/hub_check.py ~/.wdgo/extras/<id>`. Your
  `description`/`about`/`category`/`tags` are the hub listing — write them well.
- An update is a new pull request that changes your folder and bumps `version`. If it
  asks for a permission the previous version did not have, players are shown the
  consent card again before it runs.
  Maintainer side: `docs/EXTRAS_hub.md`.
