# Changelog

All notable changes to **Watch Dogs Go** are documented in this file.
Format inspired by [Keep a Changelog](https://keepachangelog.com/).
This project follows [Semantic Versioning](https://semver.org/) — currently in
the `0.x` series, meaning the API and on-disk format may still change between
minor versions.

---

## [Unreleased]

---

## [0.10.6] — 2026-11-24

This release expands offline map coverage and identifies device models on the proximity radar.

### Changed

- Filled approximately 590 blank basemap tiles in the dark ctOS style, adding about 12 MB. Tiles use English labels where the bundled Latin font supports them.

### Added

- Added a backward-compatible BLE Service Data tag for the hardware model. Fleet scanners can identify a uConsole, and older parsers ignore the tag.

## [0.10.5] — 2026-11-23

This release fixes session logging, BLE process cleanup and radar updates.

### Fixed

- Updated `_mc_on_node` to accept the public key passed by the LoRa manager. MeshCore adverts now reach loot, the map and WDGWars. Incoming channel messages and direct messages are saved to loot.
- Stopped both proximity BLE child processes on exit. When one process fails, the radio supervisor stops its partner before restarting the pair.
- Saved incoming and outgoing WDGWars `#general` messages to loot and added delivery failures to the chat log.
- Populated the contacts store used by the CONTACTS panel and its counter with MeshCore nodes.
- Capped the MeshCore log, WDGWars chat log and Meshtastic message list.
- Updated the UI to take snapshots of the aircraft, 433 MHz sensor and emergency-reader dictionaries before iterating, preventing radar flicker and dropped contacts during background updates.

## [0.10.4] — 2026-11-23

The offline map now supports panning and displays English place names.

### Changed

- Re-rendered the dark ctOS basemap with the map style's `text-field` set to `name:en`. Names outside the bundled font's Latin glyph set may appear without labels.

### Added

- Added arrow-key panning and a `0` shortcut to recenter the map and reset zoom. The avatar tracks its geographic position and stays visible at the pane edge.
- Reserved the arrow keys for navigation while the loot, cluster, data or proximity screen is open.

## [0.10.3] — 2026-11-23

The radar expires stale contacts and the HUD displays low-battery warnings based on pack voltage.

### Fixed

- `poll()` now calls `SDRManager.prune_stale()` each frame. The method checks a snapshot every few seconds and removes aircraft after 60 seconds and 433 MHz sensors after five minutes.

### Added

- Read `voltage_now` to trigger a terminal warning below approximately 3.45 V and re-arm it above 3.65 V. The HUD shows the live voltage and turns the battery indicator red on warning. The Status screen includes a BATT line.

## [0.10.2] — 2026-11-22

Setup now handles distribution-specific radio packages and fullscreen startup on the uConsole.

### Fixed

- Made optional `meshtasticd` installation non-fatal. Setup selects the matching Debian or Raspbian repository for Debian, Raspberry Pi OS, Raspbian, Kali and Parrot, and skips the step on Ubuntu and DragonOS.
- Added an idempotent labwc window rule that starts the game fullscreen on the rotated DSI panel. Setup backs up `rc.xml` before applying the rule.

## [0.10.1] — 2026-11-22

Fresh clones and in-game updates now include the offline world map.

### Fixed

- Corrected the `.gitignore` exclusion and bundled `basemap.mbtiles` (approximately 28 MB, zoom levels 0–6). The map centers on the GPS position once a fix is available.

## [0.10.0] — 2026-11-20

This is the first public release of the raylib-based Next-Gen build.

### Added

- Added EXTRAS, a sandboxed mini-app platform for browsing, installing, rating and running reviewed extras. The hub catalogue is signed offline with a hardware key and displays official extras in green.
- Added Cluster Wardrive with the uConsole XIAO as an ESP-NOW core for parallel scanning by Marauder V8 nodes, with node adoption, coverage radar and per-node health.
- Combined MeshCore and native Meshtastic in one LoRa picker with shared chat and map markers.
- Added an offline ctOS world basemap layer beneath map markers.
- Added RTL-SDR tools for ADS-B tracking and KML export, 433 MHz scanning, 433 Hunter direction finding, 433 Capture recording (`.cu8` and Flipper-compatible `.sub`) and emergency-radio reception.
- Added host WiFi card support and per-function ESP32, host or automatic radio selection for scanning, wardriving, handshakes, deauthentication and BLE.
- Added WDGWars pairing by code, Chase the White Rabbit objectives, a HUD for rank, team and territory, and stable per-device identity.
- Added a loot session journal with date browsing, file previews and single-file backup and restore.
- Added BLE proximity radar using rotating identities and a protocol shared across the WDGW firmware family.

### Changed

- Relicensed the project under PolyForm Noncommercial 1.0.0.
- Updated the README with the Next-Gen installer, features and hub links. The classic build remains available as a secondary option.

### Fixed

- Restored AIO rail power when enabling GPS. `ensure_gps` powers the rail for wardrive and cluster modes.
- Counted distinct ICAO codes for the session aircraft total.
- Treated empty responses, HTML and login redirects as pending WDGWars uploads.

## [0.9.14] — 2026-10-07

Game updates are available from the SYSTEM menu.

### Added

- Added a launch-time update check and HUD notification. **Update Game** runs `git pull` and prompts for a restart. Loot and saves are excluded from Git.
- Documented in-game, terminal and installer update paths in the README.

### Changed

- The version check fetches the checkout's own `origin/<branch>` and compares commits against the upstream used by the updater.

## [0.9.13] — 2026-06-04

Wardrive uploads can recover from transient HTTP 413 responses.

### Added

- Added up to three retries in `plugins/wardrive_upload.py`, after 1, 5 and 30 seconds. Each attempt signs a fresh nonce. A successful retry resumes totals, badge sync and normal `_mark_uploaded` handling.
- Logged persistent HTTP 413 failures and kept the session in the pending queue for the next upload. A different HTTP status ends the 413 retry loop immediately.

## [0.9.12] — 2026-06-04

The Captured Passwords screen and loot totals filter diagnostic events out of credential records.

### Fixed

- Updated the `passwords` and `et_captures` counters in `watchdogs/loot_manager.py` to count lines containing `received post` or `form submission`, case-insensitively.
- Restricted `_load_all_captures` in `watchdogs/app.py` to entries that parse as POST form data. Existing sessions are rescanned at launch without rewriting their logs.

## [0.9.11] — 2026-05-08

Map downloads now check for a Stadia Maps API key before starting.

### Changed

- Updated `_start_map_download()` in `watchdogs/app.py` to call `_load_stadia_key()`. If the key is empty, the game displays setup instructions and skips the download worker.
- Updated the `download_tiles()` docstring and throttling comment in `watchdogs/tile_manager.py` to identify the Stadia Maps Alidade Smooth Dark tile source.

## [0.9.10] — 2026-05-02

Offline tile downloads are available through Stadia Maps.

### Added

- Added `_load_stadia_key()` and updated `_tile_url()` in `watchdogs/tile_manager.py` to read `STADIA_API_KEY` from `secrets.conf` and construct Alidade Smooth Dark tile URLs. Updated the User-Agent.
- Enabled the `_dl_map` handler in `watchdogs/app.py` and removed the WIP label from **Download Map**. Pressing `m` again cancels the active download.
- Documented `STADIA_API_KEY` in `secrets.conf.example`.

## [0.9.9] — 2026-05-02

The loot screen now tracks aircraft, and wardrive uploads allow longer processing times.

### Added

- Added `save_adsb_aircraft()` to `watchdogs/loot_manager.py`. It appends each distinct ICAO code once per session to `adsb_aircraft.csv`, writes the header on first use and syncs the file to disk. The format is compatible with `_parse_adsb` in the upload plugin.
- Updated `_scan_session_dir`, `_recalc_totals` and `_rebuild_db` to include the `adsb` count in all-time totals.
- Connected the `aircraft_new` event in `watchdogs/app.py` to the loot writer to record callsign, position, altitude, speed and heading.
- Added an Aircraft row to both loot columns. ALL-TIME uses synced aircraft totals with a local fallback, and THIS SESSION uses the live SDR count.

### Fixed

- Increased the request timeout in `plugins/wardrive_upload.py` from 30 to 90 seconds for both uploads and rate-limit retries.
- Added handling for `TimeoutError` and `OSError` that logs a warning and marks closed sessions as uploaded. The active session stays pending.

## [0.9.8] — 2026-04-26

The upload log distinguishes newly imported aircraft from aircraft already seen.

### Changed

- Updated `plugins/wardrive_upload.py` to display `aircraft_already_seen` alongside `aircraft_imported` on success. A missing `aircraft_already_seen` field defaults to zero.

## [0.9.7] — 2026-04-26

The game accepts serial bridges exposed as character devices outside `/dev/`.

### Fixed

- Added `_looks_like_serial(path)` in `watchdogs/__main__.py`. It recognizes `/dev/*`, `COM*` and paths that `os.stat()` resolves to character devices, including symlinks to PTYs.

## [0.9.6] — 2026-04-24

Wardrive sync keeps the recording session eligible for upload as new data arrives.

### Fixed

- Centralized completion tracking in `_active_session_name()` and `_mark_uploaded(session_dir)` in `plugins/wardrive_upload.py`. The active directory stays in `_pending_sessions()` until a new session starts.
- Added log messages for successful and empty active-session uploads to indicate that the session will be checked again.

## [0.9.5] — 2026-04-24

MeshCore radio presets can now be selected at runtime.

### Added

- Added six SX1262 regional presets to `MESHCORE_PRESETS` in `watchdogs/lora_manager.py`, with narrow and legacy wide modes.
- Added the **MeshCore Region** picker to ADDONS. It displays frequency, bandwidth and spreading factor, saves the selection in `~/.watchdogs_meshcore.json` and restarts an active MeshCore sniffer with the selected settings.

### Changed

- Added the optional `region` argument to `LoRaManager.start_meshcore()`. Startup and `_toggle_lora` in `app.py` pass `self._mc_region`.
- Updated `load_meshcore_config()` and `save_meshcore_config()` to persist the `region` field. Missing or unknown values use `DEFAULT_MESHCORE_REGION`.

## [0.9.4] — 2026-04-24

The installer now supplies missing ADS-B and wireless interface tools.

### Fixed

- Updated `setup.sh` to install build dependencies and build FlightAware's `dump1090` from source when the binary is missing. The build installs to `/usr/local/bin/dump1090`.
- Added `iw` to the apt dependencies for Dragon Drain and interface bridging.
- Updated `watchdogs/app.py` to direct installations missing `dump1090` to `./setup.sh`.
- Updated the system dependencies in `README.md`, including `python3-gi` and `gir1.2-glib-2.0`.

## [0.9.3] — 2026-04-21

The WDGWars sync plugin now identifies the client consistently across HTTP requests.

### Fixed

- Set an explicit User-Agent in `plugins/wardrive_upload.py` with the game version, platform and Python version. Linux builds identify the platform as uConsole, and other builds use `platform.system()`.
- Routed all six HTTP request sites through `_open(req, timeout)`, which sets the User-Agent and `Accept: application/json` before calling `urlopen`.

## [0.9.2] — 2026-04-20

Watch pairing supports the authenticated BLE connection required by PipBoy-3000 firmware v0.4 and later on the LilyGO T-Watch Ultra.

### Added

- Updated `watch_manager._async_connect` to check BlueZ pairing and trust before opening GATT. When pairing or trust is missing, a worker runs `bluetoothctl pair` with a 90-second timeout. The D-Bus agent routes passkey requests through `provide_pin()`, and successful pairing marks the watch as trusted.
- Updated `setup.sh` to install `python3-gi` and `gir1.2-glib-2.0` and link the system GI package and native extensions into the game environment.
- Added a `python3-gi` preflight check to `watchdogs/__main__.py`.

### Changed

- Enlarged the Watch PIN overlay to 320×112, centered the six cyan PIN digits using Spleen 5×8 metrics, and added entry, erase and confirmation hints.
- Updated `__version__` from `0.9.0` to `0.9.2`.

### Fixed

- Changed map drawing order to player, aircraft, sensors, markers and particles. Nearby contacts remain visible over the player sprite at low zoom.

## [0.9.1] — 2026-04-19

This release updates text and map rendering and makes session data more resilient to power loss.

### Added

- Bundled the BSD-2-Clause Spleen 5×8 font at `assets/spleen-5x8.bdf`. `WatchDogsGame.__init__` loads it as the default for `pyxel.text`, preserves explicit font arguments and falls back to the built-in 4×6 font if loading fails. MeshCore chat transliterates Polish letters to ASCII.
- Updated `_draw_markers` to render MeshCore nodes as pulsing cyan diamonds with labels from zoom 4 and handshake markers as red skulls with labels from zoom 5. `_draw_aircraft` uses pulsing, altitude-colored aircraft symbols.
- Added `_fsync_file`, `_sync_append` and `_sync_write` in `watchdogs/loot_manager.py` to flush event logs, wardrive and scan CSVs, Bluetooth and MeshCore records, handshake files and loot database writes to disk.
- Added a background thread in `LootManager` that runs `flush_all()` every 30 seconds to sync `serial_full.log` and call `os.sync()`. On shutdown, `close()` stops the thread, performs a final sync, writes the session summary and updates the loot database before closing the serial log.

### Changed

- Adjusted HUD anchors, menu rows, dialogs, pickers, overlays and list spacing in `watchdogs/app.py` for the 5×8 font. The terminal now displays approximately 12 lines.
- Updated `_draw_mc_screen` to use the global font with 5-pixel character widths and 10-pixel line spacing.
- Cached cluster colors and radii in `_update_clusters` for reuse by `_draw_loot_points`.
- Precomputed coastline segment bounds and combined line and point rendering into one projection pass in `_draw_coastlines`.
- Updated `_term_add` to cache colors from `_color_for_terminal_line()` alongside the last 500 terminal lines for reuse by `_draw_terminal`.

### Fixed

- Added `_fsync_file` to `_save_db` before `Path.replace` renames `loot_db.tmp` to `loot_db.json`, preventing a zero-byte database after an interrupted save.
- Corrected map symbols for MeshCore nodes and ADS-B aircraft that previously appeared as handshake markers.

## [0.9.0] — 2026-04-12 (early access release)

The first public release includes the core gameplay loop, plugins and hardware integrations for the uConsole.

### Added

- Added the Watch Dogs Go Wars Sync plugin for session uploads, identity and stats retrieval from `GET /api/me`, two-way sync for eight badges and MeshCore node naming. The plugin includes a built-in endpoint, validates the API key inline and unlocks at level 6 (6000 XP).
- Added the JanOS Loot Import plugin with automatic XP recalculation for imported sessions.
- Added BLE NUS support for the PipBoy Watch (T-Watch Ultra), including NFC reads, MeshCore messaging and ESP32 controls.
- Added wardriving CSV compatibility with Bruce Firmware on more than 50 ESP32 boards.
- Added a one-line installer, dependency installation via `setup.sh`, a `secrets.conf.example` template, a desktop launcher and a first-launch `dialout` group check.
- Added unhandled-exception logging to `~/.watchdogs/last_run.log` with rotation to `previous_run.log`.
- Added the node name and `Ctrl+N`, `Ctrl+A`, `Ctrl+C` and `Ctrl+X` shortcut hints to MeshCore Messenger.
- Added support for multiple plugins with overlay interfaces.

### Changed

- Renamed `janos_*` files and `JANOS_*` environment variables to `watchdogs_*` and `WDG_*`. MeshCore key and configuration files migrate automatically. Reads accept legacy variable names and writes use the new names.
- Updated the WiGLE CSV header to `appRelease=WatchDogsGo` and pointed automatic updates at the public release repository.
- Updated XIAO ESP32-C5 flashing to use `--before usb-reset` for in-game firmware updates.
- Encoded the plugin index in dispatch keys using `_p_<idx>_<action>`.
- Generated first-run MeshCore node names from eight hex characters of the Ed25519 public key. Configuring the WDGWars API key switches to the account-based node name.
- Marked Download Map, BLE HID, HID Type, BlueDucky and RACE Attack as disabled in their menus.

### Fixed

- Corrected the upload URL to include `/api/upload/`, resolving HTTP 403 on uploads.
- Replaced comma splitting with `csv.reader` and `csv.writer` to handle commas in MeshCore node names.
- Fixed fresh installs broadcasting the same default MeshCore node name.
- Fixed overlay dispatch when `janos_import` and `wardrive_upload` both register `open_overlay`.
- Fixed a five-second LVGL UI stall from `get_local_time(timeout=5000)` in the PipBoy Watch firmware.

## [0.x history] — pre-release

Watch Dogs Go evolved from the JanOS wardriving application for the ClockworkPi uConsole.

### Added

- Added a Pyxel UI, RPG progression and badges in the Watch Dogs Go rewrite.
- Added projectZero firmware for the ESP32-C5 and PipBoy-3000 firmware for the T-Watch Ultra.
- Rewrote the WDGWars portal from Django to PHP, with gang warfare, anti-cheat, WiGLE-compatible uploads and badges.
- Proposed native WDGWars upload support for Bruce Firmware.

[0.9.13]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.12...v0.9.13
[0.9.12]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.11...v0.9.12
[0.9.11]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.10...v0.9.11
[0.9.10]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.9...v0.9.10
[0.9.9]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.8...v0.9.9
[0.9.8]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.7...v0.9.8
[0.9.7]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.6...v0.9.7
[0.9.6]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.5...v0.9.6
[0.9.5]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.4...v0.9.5
[0.9.4]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.3...v0.9.4
[0.9.3]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.2...v0.9.3
[0.9.2]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.1...v0.9.2
[0.9.1]: https://github.com/LOCOSP/WDGoNG/compare/v0.9.0...v0.9.1
[0.9.0]: https://github.com/LOCOSP/WDGoNG/releases/tag/v0.9.0
