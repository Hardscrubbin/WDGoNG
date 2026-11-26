# EXTRAS — third-party mini-app platform for WatchDogsGo-NG

**Status (2026-11-19): Phases 1, 2 and 3 are implemented.** This document is the
original design; where the build differs, the code and the guides win:
`docs/EXTRAS_authoring.md` (writing an extra), `docs/EXTRAS_hub.md` (running the hub).
Differences worth knowing: the manifest is `extra.json` (not TOML); the sandbox drops
to a dedicated uid inside an empty chroot with its own network namespace (stronger than
the "login user" sketched in 7.3); the signed index uses OpenSSH signatures
(`ssh-keygen -Y`), not minisign; `ctx.mesh`/`sdr` are still not granted in API 1.
Validated on the uConsole: `extras/sim/test_sandbox.py` (42 checks incl. the kernel
jail, as root) and `extras/sim/test_hub.py` (31 checks incl. forged-index attacks).

Original header: design spec covering architecture/loader, SDK/hardware API,
security/sandbox and DX/packaging, following the project owner's direction: *ultra-secure*, creator-only first, a GitHub-PR-curated
hub later.

A new **EXTRAS** menu category lists mini-apps dropped into a folder. Each is written
against a small, documented `ctx` facade and runs inside the game as if it were a
native module, with access (only) to the hardware capabilities it declares and the
user grants. The game runs as **root**, so security is the dominant constraint and
drives every decision below.

---

## 0. Guiding constraints

- **The game runs as root** (pinctrl/GPIO, ESP32 serial, SX1262 SPI, SDR; holds
  `secrets.conf` WDGWars key, `~/.wdgo/fleet.key`, loot DB). Third-party code in-process
  = root RCE. **A `ctx` facade is good DX but is NOT a security boundary.**
- **Extras are stdlib + `ctx` only.** No `pip install`, no native `.so`, no imports
  outside an allowlist, writes only to the extra's own `data/`, hardware/network only
  via the broker. This single rule is the biggest security *and* stability lever.
- **The `ctx` facade is transport-agnostic from day 1** (verbs only, no raw managers, no
  `import pyray`) so the *same extra code* runs either in-process (Phase 1, own code) or
  over an IPC broker in a non-root subprocess (Phase 2, foreign code) with no changes.
- **Defense in depth:** human PR review + CI + signed curation (before code ships) *and*
  a non-root subprocess sandbox (at runtime). "Ultra-secure" = both.

---

## 1. Roadmap (ordering is deliberate)

| Phase | Scope | Why |
|---|---|---|
| **1 — Creator SDK (self-use)** | loader, manifest, EXTRAS category, transport-agnostic `ctx` facade, lifecycle, error isolation, API versioning, 3 example extras, desktop sim-harness. In-process. | A creator writes and runs **their own** extra with **zero edits to game code**. Low risk (own code, own device). |
| **2 — Sandbox (BEFORE the hub)** | non-root subprocess + capability broker + draw-ops over IPC + watchdog + hardware lease. | The ultra-secure runtime. **Must exist before any foreign code can be downloaded**, so foreign extras *always* run sandboxed. Extra code unchanged (same facade). |
| **3 — Hub / "store" (GitHub-PR)** | `wdgo-extras-hub` repo, PR template, CI pipeline, maintainer review, signed index, in-game browse/install `[E]`, checksum verification, auto-sandbox. | Anyone submits via PR → after merge it's in the hub → others pull reviewed, signed extras. |

**Rule:** sandbox (Phase 2) ships before the hub (Phase 3). There is never a path where
foreign code runs as root.

---

## 2. Folder layout & manifest

```
~/.wdgo/extras/<id>/        # user drop-in — NOT under the repo tree (OTA `git reset --hard` would wipe it)
  extra.toml               # manifest; read WITHOUT importing any code
  main.py                  # entry module: class Extra
  icon.png                 # optional 64x64, else a generated glyph
  assets/                  # optional author files (read-only)
  data/                    # the ONLY writable path, created by the game
extras/examples/           # in-repo, read-only, copied to ~/.wdgo/extras/ on first run
extras/_disabled/          # manual kill-switch; anything here is skipped
```

`extra.toml`:
```toml
[extra]
id          = "speedo"          # ^[a-z0-9_]{3,32}$, MUST equal the dir name
name        = "GPS Speedometer" # menu label (<=18 chars shown)
author      = "nick"
version     = "1.0.0"           # the extra's own semver
api         = 1                 # EXTRAS_API major it targets (int)
entry       = "main:Extra"      # module:symbol
permissions = ["gps", "loot_read"]  # capabilities it will request
hardware    = ["gps"]           # managers it needs present
description = "Live speed/heading from the GPS fix."
min_game_version = "0.1.0"      # optional
```

> **Implementation note (Phase 1 ships this as `extra.json`, not TOML** — stdlib `json`,
> zero-dependency, portable across Python versions.) The authoritative author schema is
> `docs/EXTRAS_authoring.md`; it adds hub-listing fields `description` (one-line),
> `about` (what it does / how it works), `category`, `tags`.

**Validation** (no code imported): id regex, id == dir name, `api <= EXTRAS_API`,
`min_game_version` met, entry file present, no duplicate id. A rejected extra still
appears in the EXTRAS list **greyed, with the reason** — never silently hidden.

---

## 3. Discovery & menu integration

- Scan `~/.wdgo/extras/` at startup (and unpack any dropped `*.zip` / `*.wdgx`, then
  delete the archive). **Lazy import:** the scan reads only `extra.toml`; the entry
  module is imported on first launch, not at scan — a broken/heavy extra never touches
  startup.
- The **EXTRAS** category is appended to `TABS` only when >=1 extra is found (mirror the
  conditional CRISIS tab), rendered in the same tech-HUD chip style.
- Dispatch via one prefix handler in the `backend.act` table:
  `cmd.startswith("_extra:")` -> `_launch_extra(cmd.split(":",1)[1])`. No per-extra
  entries in the static dict.
- **`[R] Rescan`** action at the top of the EXTRAS list re-reads manifests live (no
  restart — important on a handheld). A running extra keeps its loaded code until relaunched.
- Hotkeys auto-assigned 1..9 then letters; the list already supports windowed scroll.

---

## 4. Lifecycle contract

The entry module defines a class (name from `entry`):
```python
class Extra:
    def on_load(self, ctx):      ...  # once, after import; heavy setup; may raise -> disabled
    def on_enter(self, ctx):     ...  # each time the user opens it
    def on_frame(self, ctx, dt): ...  # EVERY frame while active; draw ONLY via ctx, inside ctx.view
    def on_event(self, ctx, ev): ...  # key/char events not consumed by the host chrome
    def on_exit(self, ctx):      ...  # user left (ESC) / shutdown; release transient state
    def on_unload(self, ctx):    ...  # game shutdown / rescan; release hardware, close files
```
Only `on_frame` is required; the rest are optional no-ops.

Rules:
- **The host owns the frame**: it does `begin_drawing`/`clear`, draws the EXTRAS chrome
  (title, back hint), calls `on_frame`, then draws the footer. The extra draws only inside
  the host-provided viewport `ctx.view` (scissored). The extra must never call
  `begin_drawing`/`end_drawing`/`init_window`/`close_window` or `import pyray`.
- **ESC is reserved by the host** — always returns to the menu and calls `on_exit`; a
  wedged extra can never trap the user. All other input is routed to `on_event`.
- `dt` = seconds since last frame. No blocking in `on_frame`; the SDK hands out only
  non-blocking / timeout-bounded calls.

---

## 5. The `ctx` facade — v1 surface

Colors are referenced **by name** from a fixed palette (not raw `Color`); coordinates are
local to `ctx.view` and clamped by the host scissor.

**Drawing:** `ctx.text/rect/line/circle/panel/measure/image` (wrap existing
`text()`/`draw_*`/`cyber_panel`). `ctx.image` only loads from the extra's own dir; the host
caches and unloads textures on exit.

**Input:** keys by name, delivered only while the extra is active; ESC never reaches the extra.

**Capability accessors** (present only if declared in `permissions` AND hardware present,
else `None` — extras must null-check):

| Capability | Access | v1 | Notes |
|---|---|---|---|
| `ctx.gps.fix()` | read | ✅ | lat/lon/sats/valid/speed/heading |
| `ctx.storage` (per-extra dir) | read/write | ✅ | the only writable location |
| `ctx.log` / `ctx.notify` | write | ✅ | into the game terminal/notification stream |
| `ctx.clock` / `ctx.timer` | read | ✅ | no raw `time.sleep` in `on_frame` |
| `ctx.loot` | **read** | ✅ | network/device/handshake counts + lists |
| `ctx.loot.write_*` | mutate | ⏸ | extras may log only to their OWN namespace, never core loot/XP |
| `ctx.mesh` (MeshCore send/recv) | mutate | ✅ gated | via arbiter **acquire/release** (see §6) |
| `ctx.meshtastic` | mutate | ⏸ v1.1 | same radio; daemon self-reboots on region change |
| `ctx.sdr` (spectrum read) | read | ⏸ v1.1 | attractive, low-risk once a clean read API exists |
| `ctx.esp` (ESP32 serial) | mutate | ❌ | exposes deauth/blackout/SAE/evil-twin — never in v1 |
| `ctx.aio` (GPIO rails) | mutate | ❌ | breaks XIAO/arbiter invariants; the host owns rails |
| `ctx.portal` (WDGWars) | network | ⏸ | never sign requests with the user's API key in v1 |

**v1 cut:** draw + input + gps(read) + loot(read) + storage + log/notify + clock/timer +
gated MeshCore. Everything else is deferred behind its own permission once its shape is nailed.

---

## 6. Exclusive hardware — acquire/release lease

The SX1262 LoRa radio (and later SDR) is a single exclusive resource shared with the
game's own mesh.

- `h = ctx.mesh.acquire()` -> a handle, or `None` if the game or another extra holds the
  radio (**fail fast**, don't rip the radio away from active game mesh).
- The host's arbiter performs the real release-SPI/GPIO dance (MeshCore <-> Meshtastic) —
  reuse the pattern already built this project (GPIO-line release + the quit-screen
  "restore pre-launch rails" snapshot).
- The host **force-releases** the lease on `on_exit`, ESC, crash, or watchdog timeout — an
  extra can never leave a radio wedged. On entry the host snapshots which radios it owns and
  restores that state on exit (re-arms game mesh if it was running).

---

## 7. Security model

### 7.1 Threat model
Stakes, ranked: **key theft** (silent, remote: `secrets.conf`, `fleet.key`, WDGWars key) >
**radio abuse** (RF/legal: LoRa/sub-GHz TX, ESP32 attacks) > **destructive FS/firmware** >
**DoS/crash**. Actors: malicious extra, buggy extra, supply-chain (a good extra turns bad).

### 7.2 Why a facade is not enough
In-process, the extra shares the interpreter and address space; it can ignore `ctx` and call
`os.system`. `RestrictedPython`/bytecode denylists are bypassable and must NOT be relied on
as a boundary. The only real in-Python boundary is a **separate process with dropped
privileges**.

### 7.3 Two tiers
- **Trusted / native (Phase 1 default, own code):** in-process, full access. Framed
  explicitly as "code you wrote or vetted," off by default for anything from elsewhere,
  behind a loud consent screen + content-hash pin.
- **Sandboxed (Phase 2, the default for foreign code):** the extra runs as a **non-root
  subprocess** (`seteuid`/`setgid` to the login user via `SUDO_UID/GID`, `start_new_session`,
  `$HOME`/cwd pinned to its own dir, minimal `PATH`, **default-deny network and FS outside
  its dir**). It cannot touch raylib (GL lives in the game process), so it is a **headless
  logic process**: it sends draw-ops/reads input over IPC, and reaches hardware **only**
  through a **capability broker** in the root game process that checks the granted permission
  set and calls the real `self._mgr(...)`.

### 7.4 Consent & provenance
- First launch of a new/changed extra shows a **consent card** listing exactly what it
  requests ("draws UI; reads GPS; reads loot; sends LoRa messages"); the grant is persisted
  per **id + content-hash**. Changing the code -> new hash -> re-consent (detects tamper /
  supply-chain updates).
- `author` / `source` shown verbatim on the card (shown, never trusted).
- A permanent **"UNVERIFIED THIRD-PARTY CODE — runs with the permissions you grant"** banner
  on every non-allowlisted extra.
- Hub extras (Phase 3) are checksum-verified against a **signed index** and skip the warning.

### 7.5 Never allow
Reading `secrets.conf` / `*.key` / tokens (the broker exposes *actions* that use a key, never
the key bytes) · running an extra as root · broker calls that exec arbitrary commands /
`eval` extra-supplied strings / load extra-supplied `.so` · flashing ESP32 firmware · raw
GPIO/pinctrl or arbitrary-frequency TX from a sandboxed extra · unbounded FS / network ·
`pip install` at load time.

### 7.6 Fail-safe
- **Every hook wrapped** in try/except (same as `act`). First fault -> log
  `[EXTRAS] <id> faulted: <exc>`, mark FAULTED, `on_exit`, grey the row. No exception reaches
  the main loop.
- **Fault budget:** 3 faults in a session -> auto-move to `_disabled/` (persisted).
- **Watchdog:** monotonic timestamp before each `on_frame`; a daemon thread detects an
  overrun. Honest limit for the in-process tier: pure in-thread Python **cannot be
  force-preempted** — we detect, report, and disable, not interrupt. True preemption is the
  subprocess tier's guarantee (kill the process). Design the SDK to hand out only
  non-blocking calls so the common hang cause is engineered out.
- On fault/exit the host **force-releases** hardware leases and unloads tracked textures.

---

## 8. API / ABI versioning

- A single integer `EXTRAS_API` in the game (start at **1**). Manifest `api` must be
  `<= EXTRAS_API`.
- **Back-compat:** support `api` down to `EXTRAS_API - N` (N=2) via thin shims in the `ctx`
  factory. An extra built for a newer game is refused with a clear
  **"needs a newer game — run [U] update"** (ties into the OTA banner).
- **Additive-only within a major**: new optional hooks / `ctx` methods bump minor; removals
  or signature changes bump the major (rare, loud). **Freeze the drawing primitives early.**
- Ship `docs/EXTRAS_API.md` (the full `ctx` surface + hook contract) and an
  `extras/_template/` starter targeting an exact version.
- API incompatibility is a **visible disabled state, never a crash**.

---

## 9. Developer experience

- **`wdgo-extra-template`** (cookiecutter): `extra.toml`, a `main.py` stub with all hooks, a
  `stubs/wdgo_ctx.pyi` type stub (autocomplete/mypy **without the game installed**), and a
  **`run_in_sim.py`** harness that loads the extra against a **fake ctx** (mock GPS/LoRa
  feeds) on a desktop — iterate without a uConsole.
- **Three shipped example extras** (also the golden-CI acceptance tests — they cover the
  three hardware-access shapes):
  1. **GPS Speedometer** (`gps`) — one-shot read; facade read + text/needle drawing.
  2. **Mesh Mini-Chat** (`mesh`) — exclusive duplex over MeshCore via the arbiter; handles
     "radio busy" gracefully.
  3. **SDR Spectrum Toy** (`sdr`) — streaming read + a heavy-but-read-only capability with a
     clean "hardware absent" fallback.
- **Broken / refused extras are always shown**, disabled with a red reason
  (`✖ needs api 2 (game has 1)` / `✖ manifest error` / `✖ crashed: KeyError` /
  `✖ perm denied: sdr`) + a `[L] logs` view. Silent disappearance is a bug.

### Hello-world sketch
```python
# extras/speedo/main.py  (pseudocode)
class Extra:
    def on_enter(self, ctx):
        self.gps = ctx.require("gps")        # raises if not granted -> host disables
    def on_frame(self, ctx, dt):
        fix = self.gps.fix()
        kmh = fix.speed_kmh if fix.valid else 0.0
        ctx.text(f"{kmh:5.1f} km/h", 40, 40, size=48, color="teal")
        if ctx.key_pressed("r"):
            self.reset()
        # ESC is the host's, not the extra's
    def on_exit(self, ctx):
        pass
```

---

## 10. Updates & the hub (Phase 3)

- **Extras update independently of the game** (they live in `~/.wdgo/extras/`, outside the
  OTA git tree). A separate **`[E] EXTRA UPDATE`** banner, distinct from the game's `[U]`, so
  the two channels never get confused.
- **The hub is a curated GitHub repo** (`wdgo-extras-hub`). Submission = **pull request**:
  1. **CI on the PR** (before any human review): validate the manifest + `api`; **AST scan**
     rejecting `os.system`/`subprocess`/`eval`/`exec`/`ctypes`/`socket`/`open()` outside
     `data/` and any import off the allowlist; **permission audit** (code only uses declared
     caps); **no pip deps / no native `.so`**; run the extra in the sim-harness (must pass).
  2. **Maintainer review** of the diff, then merge.
  3. On merge, CI builds a **signed `index.json`** (per-extra checksums) with minisign/age;
     the game ships the public key.
- **In-game browse/install** (`[E]`): the game fetches the signed index, verifies the
  signature, lists approved extras, downloads one, **verifies its checksum against the
  index**, installs it into `~/.wdgo/extras/`, and **always runs it sandboxed** (Phase 2).
- This turns "foreign code from the internet" into "reviewed + checksum-pinned + signed +
  sandboxed" — defense in depth.

---

## 11. Open decisions / risks

1. **Phase 1 vs Phase 2 effort split** — Phase 1 (in-process, own code) is ~80% of the
   platform and low-risk; Phase 2 (subprocess broker + draw-ops IPC) is the heavy,
   ultra-secure part and the precondition for the hub.
2. **Draw-ops over IPC** (Phase 2) is the trickiest engineering — a headless extra can't call
   raylib, so it emits a draw-op list the host renders. Keep the primitive set tiny and frozen
   so the IPC schema is stable.
3. **Can't preempt an in-thread hang** in the in-process tier — accept detect-and-disable;
   the subprocess tier gets real kill.
4. **Review load** — human PR review is effort; CI must carry most of it so merges are cheap.
5. **`EXTRAS_API` discipline** — the golden examples run in CI every release; if an API change
   breaks them it breaks third parties, so that's the gate.

---

*Next concrete step when building starts: Phase 1 — add the loader + manifest reader + EXTRAS
tab synthesis + the `ctx` facade (draw/input/gps-read/loot-read/storage/log) + lifecycle +
error isolation, plus the sim-harness and the GPS Speedometer example. No game-logic changes
required beyond the tab synthesis and one `_extra:` dispatch entry.*
