# Cluster-serial protocol

The **Cluster Wardrive** feature (SNIFF → *Cluster Wardrive*) lets the uConsole act as
the operator console for an **ESP-NOW node fleet**. The fleet's core (a Marauder C5, or a
projectZero XIAO in core-mode) runs the radio coordination; it streams the merged result
to the host over the USB serial link as plain newline-terminated ASCII lines. The host
(`wdgong_ui/backend.py` → `_ingest_cluster_line`) parses them, drives the fleet screen,
GPS-stamps the hits into loot/map, and sends control back.

This file is the wire contract. It is the single source of truth for both sides; the host
parser and any core firmware must agree on it field-for-field.

## Host → core (control, one token per line)

| Line | Meaning |
|------|---------|
| `start_cluster_serial` | enter core mode: coordinate the fleet and begin streaming the lines below |
| `cluster_accept <MAC>` | adopt the node that asked to join (operator pressed **[A]**) |
| `stop` | leave core mode / stop streaming |

## Core → host (telemetry + records)

All lines are `,`-separated. Extra trailing fields are ignored, so the core may append new
fields later without breaking an older host. Missing trailing fields default on the host.

### `CNODE` — per-node telemetry (emit one per node, ~1–2 s cadence)

```
CNODE,idx,MAC,role,claim_lo,claim_hi,cov,rec_min,buffered,temp_c,uptime_s,fw,dropped,dedup_pct,hops_min,dropouts,last_gap_s,by_core,serial_drop
```

| Field | Source (Marauder `EnowNode`) | Notes |
|-------|------------------------------|-------|
| `idx` | fleet slot index | stable per node |
| `MAC` | `n.mac[6]` | `AA:BB:CC:DD:EE:FF` |
| `role` | `n.ble_role?"ble": n.vita_role?"vita":"wifi"` | drives colour + placement |
| `claim_lo`/`claim_hi` | `n.claim_start` / `n.claim_end` | **indices into `ENOW_CHANNELS`** (ground truth the node reports covering), not raw channels |
| `cov` | coverage %, or `claim_hi-claim_lo+1` | display only |
| `rec_min` | `n.rate` (core-measured) | records/min |
| `buffered` | `n.buffered` | records still queued in the node's PSRAM |
| `temp_c` | `n.temp_known ? n.temp_c : -128` | `-128`/`?`/empty → shown as `--` |
| `uptime_s` | `n.uptime_s` | separates a reboot (small) from a long silence |
| `fw` | `"v%u"` of `n.fw_ver` | |
| `dropped` | `n.node_dropped` | records the node threw away (ring full) |
| `dedup_pct` | `n.dedup_pct` | ≥70 → host shows FULL/red |
| `hops_min` | `n.hops_per_min` | channel changes/min |
| `dropouts` | `n.dropouts` | times the core lost & re-found it |
| `last_gap_s` | `n.last_gap_ms/1000` | how long it was gone last time |
| `by_core` | `n.claim_version>0 ? 1 : 0` | 0 = node self-assigned its slice (core was silent) → host shows SELF |
| `serial_drop` | count of CWD lines the core could not push to serial | flow-control visibility; 0 if none |

The host maps `claim_lo`/`claim_hi` back onto the same `ENOW_CHANNELS` table (mirrored in
`backend._SIM_CHANNELS` and the fleet radar) so a node sits on the arc of the spectrum it
actually covers — a stale node greys its arc and the gap becomes visible.

### `CWD` — one fleet wardrive hit

```
CWD,idx,BSSID,SSID_b64,AUTH,CH,FREQ,RSSI,BAND[,age_ms]
```

- `idx` — which node found it (NOT the node's MAC; `BSSID` is the scanned AP).
- `SSID_b64` — base64 of the SSID (empty for hidden), so a comma/UTF-8 SSID is safe on the wire.
- `AUTH` — WiGLE bracket string (`[WPA2-PSK-CCMP]`, `[OPEN]`, `[BLE]`, …).
- `CH`/`FREQ` — channel + MHz; `BAND` ∈ `2.4` / `5` / `ble`.
- `age_ms` *(optional)* — how long ago the node captured it, so the host can place it back
  along its own GPS route-history instead of at "now". Not yet consumed (TODO in backend).

The host builds a WiGLE 1.6 row from this, stamps **its own** GPS fix + time (the core may
have no GPS), and upserts it into `wardriving.csv` + the map — the core is the only device
that needs no GPS.

### `CJOIN` — a node asks to be adopted

```
CJOIN,MAC
```

Raised from the core's `ENOW_JOIN_REQUEST` handler. The host shows the join banner; on
**[A]** it replies `cluster_accept <MAC>`, which the core turns into its `ENOW_JOIN_GRANT`
(hand the fleet key). Re-emit periodically until adopted.

### `CEVT` — a fleet event (free text, shown in the terminal)

```
CEVT,MAC,message
```

e.g. `channel rebalance`, `dedup table full`, `watchdog reboot recovered`.

## Core-side implementation plan (Marauder `wdg_panel.ino`)

Additive and self-contained — no existing behaviour changes, the uplink only *reads* fleet
state and *writes* serial:

1. `static bool g_uplink = false;` — set in the serial-command handler on
   `start_cluster_serial`, cleared on `stop`. Handle `cluster_accept <MAC>` there too, routing
   to the existing adopt/grant path.
2. **CNODE tick** in `loop()`, every ~1.5 s when `g_uplink`: iterate `g_nodes[ENOW_MAX_NODES]`,
   skip `!used`, `Serial.printf` the `CNODE` line from the fields above.
3. **CWD hook** at the fleet-record receive site (where a node's `ENOW_TEXT` record is logged
   to the WiGLE CSV): when `g_uplink`, also emit the `CWD` line (base64 the SSID; derive the
   node `idx` from the sender MAC).
4. **CJOIN hook** in the `ENOW_JOIN_REQUEST` handler: `Serial.printf("CJOIN,%s\n", mac)`.
5. **CEVT** (optional) wherever the core already logs a notable fleet event.

Keep each `Serial.printf` guarded by `g_uplink` and bounded (USB-CDC can backpressure — count
drops into `serial_drop` rather than blocking the radio task).

## Testing without hardware

`WDGO_CLUSTER_SIM=1` makes the host synthesise a realistic fleet straight into
`_ingest_cluster_line` (same byte format as above), so the whole screen + loot path can be
built and validated with no nodes attached. See `backend._cluster_sim_loop`.
