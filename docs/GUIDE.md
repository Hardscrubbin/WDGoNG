# ESP32 Watch Dogs — Complete Guide

Open-world hacking RPG for the ClockworkPi uConsole with real WiFi/BLE scanning, attacks, wardriving, mesh networking, and handshake capture. Frontend for ESP32-C5 running [projectZero](https://github.com/LOCOSP/projectZero) firmware.

---

## Installation

### Requirements

| Component | Description |
|-----------|-------------|
| **ClockworkPi uConsole** | Handheld Linux console (ARM) |
| **ESP32-C5 XIAO** | Seeed Studio XIAO ESP32-C5 with [projectZero](https://github.com/LOCOSP/projectZero) firmware |
| **Python 3.10+** | Runtime |
| **AIO v2** *(optional)* | [HackerGadgets](https://hackergadgets.com/products/uconsole-aio-v2) extension board (GPS, LoRa SX1262, SDR, USB hub) |

### Quick Start

```bash
git clone https://github.com/LOCOSP/WDGoNG.git
cd WDGoNG
bash setup.sh             # creates venv, installs dependencies
./wdgong_ui/run_ui.sh     # auto-detects ESP32, starts game (asks for sudo itself)
```

### Manual Port

```bash
sudo ./run.sh /dev/ttyACM1   # explicit serial port
```

### AIO v2

If AIO v2 is detected, the game automatically:
- Enables USB power (GPIO 23) on startup — powers ESP32 XIAO
- Waits 4 seconds for ESP32 firmware boot
- Detects and connects to serial port

---

## Display Layout

```
+----------------------------------------------+
| HUD TOP: Level | XP bar | Badges | BAT | Z  |  16px
+----------------------------------------------+
|                                              |
|           MAP: Coastlines, OSM tiles,        |
|           markers, radar, player             |  278px
|                                              |
+----------------------------------------------+
| TERMINAL: ESP32 output, attack logs          |  50px
+----------------------------------------------+
| HUD BOT: Stats | Active ops | GPS | LoRa    |  16px
+----------------------------------------------+
```

---

## Main Controls

| Key | Action |
|-----|--------|
| **TAB** | Open/close menu |
| **Arrow keys** | Navigate menu / pan map |
| **ENTER** | Select menu item / confirm |
| **ESC** | Back / close overlay / quit dialog |
| **\`** (backtick) | Toggle Loot Database screen |
| **+/-** | Zoom in/out on map |
| **1-9** | Quick switch menu categories |
| **PAGEUP/PAGEDOWN** | Scroll terminal |

---

## Menu Categories

### SCAN
| Key | Command | Description |
|-----|---------|-------------|
| w | WiFi Wardriving | Continuous WiFi scan with GPS logging |
| b | BLE Wardriving | Continuous BLE scan with GPS logging |
| s | WiFi Scan | Single WiFi network scan |
| e | BLE Scan | Single BLE device scan |

### SNIFF
| Key | Command | Description |
|-----|---------|-------------|
| n | Sniffer | Packet capture (promiscuous mode) |
| h | Handshake | WPA handshake capture (PCAP + HCCAPX + HC22000) |

### ATTACK
| Key | Command | Description |
|-----|---------|-------------|
| d | Deauth | Targeted deauthentication (requires BSSID + channel) |
| k | Blackout | All-channel broadcast deauth |
| t | Evil Twin | Rogue AP with captive portal (multi-network select) |
| p | Evil Portal | Standalone captive portal with custom HTML |
| o | SAE Overflow | WPA3 SAE Commit flood |

### TOOLS
| Key | Command | Description |
|-----|---------|-------------|
| x | Stop All | Stop all running operations |
| r | Reboot ESP32 | Send reboot command to ESP32 |
| m | Download Map | Download OSM tiles for current GPS area |
| g | GPS | Toggle GPS module on/off (AIO v2) |
| l | LoRa | Toggle LoRa radio + MeshCore Messenger |
| d | SDR | Toggle SDR module on/off (AIO v2) |
| b | USB | Toggle USB power on/off (AIO v2) |
| w | Whitelist | MAC address ignore list |
| u | Upload WPA-SEC | Upload handshakes to wpa-sec.stanev.org |
| p | Download WPA-SEC | Download cracked passwords |
| f | Flash ESP32 | OTA firmware update (auto bootloader via USB-JTAG) |

### HOST ATTACKS (Python-native, run on uConsole)
| Key | Command | Description |
|-----|---------|-------------|
| i | MITM | ARP spoofing + packet capture (scapy) |
| a | Dragon Drain | WPA3 SAE flood from host WiFi adapter |
| y | BlueDucky | BLE HID injection (D-Bus) |
| c | RACE Attack | Airoha BT exploit (GATT via bleak) |

### EXTRA
| Key | Command | Description |
|-----|---------|-------------|
| 9 | MeshCore Messenger | LoRa mesh chat (encrypted, off-grid) |
| f | Flipper Zero | NFC read/SubGHz RX/TX via serial |
| a | ADS-B Radar | Aircraft tracking via RTL-SDR + dump1090 |
| 4 | 433 MHz Scanner | ISM sensor decoding via RTL-SDR + rtl_433 |
| p | PipBoy Watch | Connect to T-Watch Ultra companion device |

---

## PipBoy Watch Integration

Connect the game to a [PipBoy Watch](https://github.com/LOCOSP/twatchultra) (LilyGo T-Watch Ultra with ESP32-S3) for NFC scanning, remote recon, GPS, and LoRa mesh — all controlled from the game UI.

### Requirements

- LilyGo T-Watch Ultra with PipBoy firmware
- uConsole Bluetooth enabled (`bluetoothctl power on`)

### Connection Flow

1. Enable BLE on watch: WiFi app > BLE ON/OFF
2. In game: ADDONS > PipBoy Watch (`p`)
3. Game scans for `PipBoy-*` devices (10 seconds)
4. Select device with arrows + ENTER
5. If previously connected, game auto-reconnects on startup

### Watch Overlay

The PipBoy Watch overlay has two panels:

**Left panel** — Command menu:

| Command | Description |
|---------|-------------|
| Request Status | Battery, GPS, time, heap |
| Haptic Buzz | Vibrate watch (connection test) |
| WiFi Recon | Scan WiFi networks from watch |
| BLE Recon | Scan BLE devices from watch |
| Stop Recon | Stop active scan |
| Get Recon Results | Retrieve scan results |
| NFC Scan | Start NFC tag polling |
| NFC Stop | Stop NFC polling |
| NFC Save Tag | Save detected tag to watch SD |
| NFC List Tags | List saved tags on watch |
| NFC Download All | Transfer all .nfc files to game loot |
| LoRa Start | Start LoRa radio on watch |
| LoRa Stop | Stop LoRa radio |
| LoRa Advertise | Broadcast MeshCore presence |
| GPS ON/OFF | Toggle watch GPS |
| Disconnect | Close BLE connection |
| Forget Watch | Remove BLE bonding (unpair) |

**Right panel** — Live log of all watch events (NFC tags, recon results, status, errors).

### NFC Tag Scanning

1. Select **NFC Scan** from watch menu
2. Hold NFC tag to watch back
3. Tag UID + NDEF data appears in watch log (+25 XP)
4. **NFC Save Tag** saves to watch SD card
5. **NFC Download All** transfers all tags to `loot/<session>/nfc/` (Flipper .nfc format)

### LoRa via Watch

The watch LoRa integrates with the **MeshCore Messenger**:

- Messages from watch LoRa appear in MeshCore chat with `W` tag (e.g., `14:32 2hop W`)
- Messages from AIO LoRa appear without `W` tag (e.g., `14:32 2hop`)
- When sending: if AIO LoRa active, uses AIO; if only watch connected, forwards via watch
- Opening MeshCore Messenger with only watch connected auto-starts watch LoRa

### Auto-Connect

- On game startup, checks if PipBoy is already connected to uConsole Bluetooth
- If yes — attaches silently in background (no scan needed)
- If watch disconnects (or game crashes), watch auto-restores after 20-60 seconds
- Game sends keepalive every 30 seconds to maintain connection

### Recon (Remote WiFi/BLE Scanning)

Use the watch as a remote scanner — useful when uConsole doesn't have ESP32 or for stealth recon:

1. **WiFi Recon** — watch scans nearby WiFi networks
2. **BLE Recon** — watch scans BLE devices
3. **Get Recon Results** — retrieves scan data, displayed in watch log

---

## MeshCore Messenger

Off-grid encrypted mesh chat over LoRa (SX1262). Compatible with [MeshCore](https://github.com/meshcore-dev/MeshCore) / MeshOS devices.

### Opening Messenger

Menu > EXTRA > MeshCore Messenger (key **9**), or toggle LoRa in TOOLS first.

### Messenger Controls

| Key | Action |
|-----|--------|
| **Type + ENTER** | Send message on current channel |
| **Ctrl+H** | Toggle Contacts panel (open/close) |
| **Ctrl+A** | Broadcast advertisement (announce presence) |
| **Ctrl+N** | Change node name |
| **Ctrl+C** | Channel picker |
| **Ctrl+X** | Clear chat log |
| **[ / ]** | Quick switch channel (left/right) |
| **PAGEUP/PAGEDOWN** | Scroll chat history |
| **ESC** | Exit DM mode / close messenger |

### Contacts Panel (Ctrl+H)

| Key | Action |
|-----|--------|
| **UP/DOWN** | Navigate contact list |
| **ENTER** | Open action menu |
| **ESC** | Close panel |

#### Contact Actions

| Action | Description |
|--------|-------------|
| **DM** | Start encrypted Direct Message (ECDH + AES-128, peer-to-peer) |
| **Note** | Add/edit note for contact (saved to loot_db.json) |
| **Info** | Show contact details in chat (ID, type, first seen, GPS) |
| **Delete** | Remove contact (will re-appear on next advertisement) |

### Direct Messages (DM)

- Select contact > DM from action menu
- Blue title bar and input bar indicate DM mode
- Messages encrypted with ECDH (Ed25519 to X25519 key agreement) + AES-128-ECB
- **ESC** exits DM mode, returns to channel chat
- Delivery confirmed with checkmark when recipient sends ACK

### Chat Display

- Incoming messages show **timestamp** and **hop count** on the right (e.g., `14:32 2hop`)
- Outgoing messages show **timestamp**, then **checkmark** when confirmed
- DMs shown in blue with `[DM]` prefix
- Long messages wrap to next line
- Character counter appears when typing (max 120 characters)

### Channels

- **public** — default channel, everyone can read
- **#hashtag** — PSK derived from hashtag name
- Custom private channels configurable in `~/.janos_meshcore.json`

### Node Types

| Icon | Type | Description |
|------|------|-------------|
| C | Client | Chat companion device (can DM) |
| R | Repeater | Infrastructure relay node (highlighted green) |
| M | Room | Message storage server (BBS) |
| S | Sensor | Telemetry-only node |

### Identity

- Node name auto-generated as `WatchDogs_<hex>` (from Ed25519 public key)
- Change with **Ctrl+N** in messenger
- Ed25519 keypair stored in `~/.janos_meshcore_key`

---

## XP System & Progression

Earn XP through real-world cybersecurity activities. XP persists across sessions in `loot_db.json`.

### XP Rewards

| Activity | XP |
|----------|-----|
| New WiFi network discovered | +15 |
| Duplicate WiFi network | +1 |
| New BLE device discovered | +10 |
| Duplicate BLE device | +1 |
| WPA handshake captured | +200 |
| Evil Twin credential captured | +150 |
| Evil Twin client connected | +25 |
| NFC tag saved | +25 |
| Flipper SubGHz TX | +25 |
| New MeshCore contact | +20 |
| New aircraft tracked (ADS-B) | +10 |
| New 433 MHz sensor decoded | +5 |
| NFC tag scanned (PipBoy Watch) | +25 |
| NFC file downloaded from watch | +25 |

### Level Progression

| Level | XP Required | Title |
|-------|-------------|-------|
| 1 | 0 | NOOB |
| 2 | 100 | SCRIPT_KIDDIE |
| 3 | 500 | SKIDDIE+ |
| 4 | 1,500 | WANNABE |
| 5 | 3,000 | PACKET_MONKEY |
| 6 | 6,000 | WARDRIVER |
| 7 | 10,000 | HACKER |
| 8 | 20,000 | NETRUNNER |
| 9 | 35,000 | PHREAKER |
| 10 | 50,000 | EXPLOIT_DEV |
| 11 | 75,000 | ELITE |
| 12 | 100,000 | SHADOW_OPS |
| 13 | 150,000 | GHOST |
| 14 | 250,000 | ZERO_DAY |
| 15 | 400,000 | APT_AGENT |
| 16 | 600,000 | CYBER_DEMON |

### Achievement Badges

| Badge | How to Earn |
|-------|-------------|
| WARDRIVER | Start WiFi or BLE wardriving scan |
| HANDSHAKE | Capture a WPA handshake |
| EVIL TWIN | Capture credentials via Evil Twin |
| MESHCORE | Receive a MeshCore message |
| WPA-SEC | Upload handshakes to WPA-sec |
| FLIPPER | Use Flipper Zero features |
| SKYWATCH | Start ADS-B radar |
| IOT HUNTER | Start 433 MHz scanner |

---

## SDR — Software Defined Radio

Requires RTL-SDR (RTL2832U) on AIO v2 board + external tools.

### Prerequisites

```bash
sudo apt install dump1090-mutability rtl-433
```

### ADS-B Radar (Aircraft Tracking)

1. Enable SDR hardware: SYSTEM > SDR (toggle ON)
2. Start radar: ADDONS > ADS-B Radar

Aircraft appear on the map as arrow icons:
- **Green** — low altitude (<5,000 ft)
- **Yellow** — mid altitude (5,000–20,000 ft)
- **Cyan** — high altitude (>20,000 ft)

At zoom level 4+, callsign and altitude (in thousands of feet) are shown.
Aircraft disappear from map after 60 seconds without signal.

Data saved to `loot/<session>/adsb_aircraft.csv`.

### 433 MHz Scanner (IoT Sensors)

1. Enable SDR hardware: SYSTEM > SDR (toggle ON)
2. Start scanner: ADDONS > 433 MHz Scanner

Decoded sensors appear in the terminal with model, temperature, humidity, etc.
Sensors with GPS fix are shown as blue squares on the map.

Supported protocols (via rtl_433): weather stations, TPMS tire pressure,
door/window sensors, smoke detectors, remote controls, and 200+ more.

Data saved to `loot/<session>/sdr_sensors.csv`.

---

## Loot Database

Press **\`** (backtick) to view all-time statistics:

- **Sessions** — total game sessions
- **WiFi networks** — unique networks discovered
- **BT devices** — unique BLE devices found
- **Handshakes** — WPA captures (PCAP, HCCAPX, HC22000)
- **Passwords** — portal + Evil Twin credentials
- **MC Nodes** — MeshCore nodes heard (per session)
- **MC Messages** — MeshCore messages received
- **Contacts** — persistent MeshCore contacts
- **GPS points** — wardriving data points

### Loot Directory Structure

```
loot/
  loot_db.json                    # Aggregate database
  <session>/
    serial_full.log               # Raw ESP32 serial output
    wardriving.csv                # WiFi (lat, lon, ssid, bssid, rssi, ch, enc)
    bt_devices.csv                # BLE (mac, name, type, rssi, lat, lon)
    handshakes/
      *.pcap *.hccapx *.22000    # WPA captures in 3 formats
    meshcore_nodes.csv            # Discovered LoRa nodes
    meshcore_messages.log         # Chat messages received
    evil_twin_capture.log         # Captured credentials
    portal_passwords.log          # Portal form submissions
    mitm/mitm.pcap               # MITM packet capture
    nfc/*.txt                     # Flipper NFC tag dumps
    adsb_aircraft.csv             # ADS-B tracked aircraft
    sdr_sensors.csv               # 433 MHz decoded sensors
```

---

## HUD Elements

### Top Bar
- **Level + Title** — current progression rank
- **XP bar** — visual progress to next level
- **Badges** — earned achievement icons
- **BAT:XX%** — uConsole battery (green >25%, yellow >10%, red <=10%)
- **Z:level** — current map zoom

### Bottom Bar
- **BLE/WiFi/HS/PWD/PWN** — session counters
- **Active tools** — currently running operations (WiFi, BT, SNF, HS, EP, ET, ADS-B, 433)
- **GPS** — fix status + satellite count
- **LoRa** — MeshCore active/off + packet count
- **SDR** — satellite dish icon above radar when active (AC:N aircraft, S:N sensors)

---

## ESP32 Flashing

### With AIO v2 (no disassembly needed)

1. Menu > TOOLS > Flash ESP32
2. Select **XIAO ESP32-C5**
3. Game automatically:
   - Closes serial connection
   - USB power cycles ESP32 (GPIO 23)
   - Downloads latest [projectZero](https://github.com/LOCOSP/projectZero) firmware
   - Flashes via esptool with auto-bootloader (USB-JTAG DTR/RTS)
   - Reconnects after reboot

### Without AIO v2

1. Hold **BOOT** button on XIAO
2. Press **RESET** while holding BOOT
3. Release BOOT
4. Select Flash from menu

---

## Configuration Files

| File | Location | Description |
|------|----------|-------------|
| `~/.janos_meshcore.json` | Home dir | MeshCore node name + channels |
| `~/.janos_meshcore_key` | Home dir | Ed25519 keypair (identity) |
| `loot/loot_db.json` | Project dir | XP, badges, contacts, stats |
| `secrets.conf` | Project dir | WPA-sec API key |
| `loot/<session>/` | Project dir | Per-session loot data |

---

## Supported Hardware

### ESP32 Boards
- ESP32-C5 WROOM-1 (Dev Kit) — via CP210x/CH340 USB-UART
- Seeed Studio XIAO ESP32-C5 — via native USB-JTAG/Serial

### PipBoy Watch (Companion)
- LilyGo T-Watch Ultra (ESP32-S3)
- Connection: BLE (Nordic UART Service)
- Features: NFC (PN532), LoRa, GPS, WiFi/BLE recon, compass
- Firmware: [PipBoy-3000](https://github.com/LOCOSP/twatchultra)

### AIO v2 GPIO Map
| Module | GPIO | Default |
|--------|------|---------|
| GPS | 27 | OFF |
| LoRa (SX1262) | 16 | OFF |
| SDR (RTL2832U) | 7 | ON |
| USB Hub | 23 | OFF |

### Auto-detected Serial Ports
- `/dev/ttyUSB0` — CP210x, CH340, FTDI
- `/dev/ttyACM1` — ESP32-C5 native USB-JTAG/Serial
