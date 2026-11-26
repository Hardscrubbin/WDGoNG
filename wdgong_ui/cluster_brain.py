"""
cluster_brain — the host-side fleet brain for the WatchDogsGo-NG cluster.

The XIAO-C5 in the uConsole is only a dumb ESP-NOW *bridge* (see cluster_enow).
Everything the Marauder core firmware used to do in C now happens here, in
Python, where it can be iterated without a reflash:

    bridge line  --> ClusterBrain.handle() --> {lines to feed the UI ingest,
                                                frames to transmit back}

`lines` are the same CNODE / CWD / CJOIN strings the fleet screen already
consumes (backend._ingest_cluster_line), so the existing UI needs no change —
the brain simply *becomes* the thing that used to produce them.

Increment 1 (this module): node registry + identity, CORE_REQUEST -> CORE_REPLY,
CLAIM -> CNODE telemetry, TEXT -> CWD records (one frame may pack several,
newline-separated), JOIN_REQUEST -> CJOIN, operator accept -> JOIN_GRANT,
host-side dropout accounting. Deferred: channel-slice assignment (ADMIN /
reassign policy) and GPS stamping (the backend already stamps CWD).

This module has no I/O: the caller owns the serial port and the UI. That keeps
it unit-testable and keeps the transport swappable.
"""
from __future__ import annotations

import base64
import os
import struct
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from cluster_enow import (
    CLAIM_FROM_CORE, ROLE_BLE, ROLE_VITA, NUM_CHANNELS,
    BridgeFrame, ClaimFrame, FleetKey, Frame, JoinReqFrame, TextFrame,
    CORE_REQUEST, CORE_REPLY, HEARTBEAT, TEXT, CLAIM, JOIN_REQUEST,
    build_admin, build_core_reply, build_join_grant, build_release, decode,
)

# A node not heard from for this long, then heard again, counts as one dropout.
# A WiFi node only returns to the control channel once per CAPTURE cycle (~20 s),
# so this MUST sit above that cadence or every normal report looks like a dropout.
GAP_THRESHOLD_S = 35.0
# A node unheard for longer than this is dropped from the channel split (it gets
# no slice until it checks back in). It reports ~every 20 s, so 45 s retired it
# after only two missed reports -> nodes flapped out of the active set, the split
# churned (assignment_version ran to 60+) and the fleet "disappeared". Match the
# firmware's 90 s node timeout: survive several missed reports before retiring.
RETIRE_S = 90.0
# How often an unchanged assignment is reaffirmed to a node. The node keeps its
# slice without it, but a periodic ADMIN re-advertises core_session so a node
# that missed a change still resyncs. A node is on the control channel for well
# under 1 s per ~20 s cycle, so reaffirming faster than that cadence just floods
# the serial link with ADMINs the node is not there to hear (and crowds out the
# inbound CLAIM/TEXT). Once per report cycle is plenty.
REAFFIRM_S = 20.0
# A released node only listens on the control channel for a moment every ~20 s,
# so RELEASE is repeated for this long (same as Marauder's ENOW_RELEASE window):
# broadcast every tick, plus unicast right after each frame we hear from it.
RELEASE_S = 30.0
# A JOIN_REQUEST stays acceptable for this long after it was last heard. A node
# waiting for adoption re-sends it every cycle, so a live joiner never expires;
# an [A] press for a node that went quiet (or was never heard here) does nothing.
JOIN_TTL_S = 120.0
BROADCAST = "ff:ff:ff:ff:ff:ff"


@dataclass
class BrainOut:
    """What the caller should do after a frame: feed `lines` to the UI ingest,
    and transmit each `tx` frame via the bridge (`enow_tx <mac> <hex>`)."""
    lines: List[str] = field(default_factory=list)
    tx: List[Tuple[str, bytes]] = field(default_factory=list)   # (dest_mac, payload)

    def extend(self, other: "BrainOut") -> None:
        self.lines.extend(other.lines)
        self.tx.extend(other.tx)


@dataclass
class NodeState:
    idx: int
    mac: str
    first_seen: float
    last_seen: float
    dropouts: int = 0
    last_gap_s: int = 0
    role: str = "wifi"
    # channel-slice assignment (indices into ENOW_CHANNELS) the core has handed it
    assigned_lo: int = -1
    assigned_hi: int = -1
    assign_ver_sent: int = 0
    last_admin: float = 0.0
    # (node_index, node_count, lo, hi) of the current assignment, so a CLAIM that
    # still reports another slice can be answered with its ADMIN right away
    admin_args: Optional[Tuple[int, int, int, int]] = None


def _band_for(channel: int, is_ble: bool) -> str:
    if is_ble:
        return "ble"
    return "wifi24" if channel <= 14 else "wifi5"


def _freq_for(channel: int, is_ble: bool) -> int:
    if is_ble or channel <= 0:
        return 0
    if channel <= 14:
        return 2484 if channel == 14 else 2407 + channel * 5
    return 5000 + channel * 5


def parse_text_record(rec: str) -> Optional[dict]:
    """Parse one node wardrive record 'bssid,ssid,sec,channel,rssi,B|W,age_ms'.

    The SSID is unescaped and may contain commas, so the fixed fields are read
    from the RIGHT (age, type, rssi, channel, sec) and the BSSID from the left;
    whatever is in between is the SSID."""
    parts = rec.split(",")
    if len(parts) < 6:
        return None
    bssid = parts[0]
    # age_ms is the 7th field but older firmware omitted it -> detect by count.
    has_age = len(parts) >= 7
    if has_age:
        age, typ, rssi, channel, sec = parts[-1], parts[-2], parts[-3], parts[-4], parts[-5]
        ssid = ",".join(parts[1:-5])
    else:
        typ, rssi, channel, sec = parts[-1], parts[-2], parts[-3], parts[-4]
        ssid = ",".join(parts[1:-4])
        age = "0"
    try:
        return {
            "bssid": bssid, "ssid": ssid, "sec": sec,
            "channel": int(channel), "rssi": int(rssi),
            "is_ble": typ.strip().upper().startswith("B"),
            "age_ms": int(age),
        }
    except (ValueError, TypeError):
        return None


class ClusterBrain:
    def __init__(self, key: FleetKey, core_session: Optional[int] = None,
                 now: Callable[[], float] = time.monotonic):
        # All brain timers are RELATIVE (gaps, retire, reaffirm, release window),
        # so they run on a monotonic clock: an RTC/NTP step backwards must not
        # freeze them (wall-clock time is only needed for record stamps).
        self.key = key
        self._now = now
        # A fresh random nonce per brain start: a node that sees it change resends
        # its WHOLE retained buffer, so a brain restart never silently loses records.
        self.core_session = core_session or (int.from_bytes(os.urandom(4), "little") or 1)
        self.nodes: Dict[str, NodeState] = {}
        self._next_idx = 0
        self.pending_joins: List[str] = []     # macs awaiting the operator's [A]
        self._join_expiry: Dict[str, float] = {}   # mac -> when its JOIN_REQUEST goes stale
        self.releasing: Dict[str, float] = {}  # mac -> until: RELEASE still being repeated
        self.assignment_version = 0            # bumped whenever the channel split changes
        self._slice_snapshot: Dict[str, Tuple] = {}   # mac -> (lo, hi) last computed
        # Keep one node of a >=2-node pack on Bluetooth duty so BLE is always
        # covered (the rest split the WiFi channels). Toggle with set_bt_duty().
        self.bt_duty = True
        self.ble_mac: Optional[str] = None     # the node currently told to do BLE
        # Operator channel-pool override: None = auto (all channels). Otherwise a
        # (lo_idx, hi_idx) inclusive window into ENOW_CHANNELS that the WiFi nodes
        # split among themselves instead of the full band. Set with set_channel_pool().
        self._pool: Optional[Tuple[int, int]] = None
        # Per-node manual channel pins: mac -> (lo_idx, hi_idx). A pinned WiFi node
        # scans exactly that window; the rest auto-split the pool. The operator sets
        # one when they know a better range than the auto split (set_node_channels).
        self._overrides: Dict[str, Tuple[int, int]] = {}

    # -- identity -----------------------------------------------------------
    def _touch(self, mac: str) -> NodeState:
        now = self._now()
        ns = self.nodes.get(mac)
        if ns is None:
            ns = NodeState(idx=self._next_idx, mac=mac, first_seen=now, last_seen=now)
            self._next_idx += 1
            self.nodes[mac] = ns
            return ns
        gap = now - ns.last_seen
        if gap > GAP_THRESHOLD_S:
            ns.dropouts += 1
            ns.last_gap_s = int(gap)
        ns.last_seen = now
        return ns

    # -- main entry ---------------------------------------------------------
    def handle(self, bf: BridgeFrame) -> BrainOut:
        out = BrainOut()
        frame = decode(bf.payload, self.key, src=bf.mac)
        if frame is None or not frame.authed:
            return out                          # ignore foreign / forged frames
        rel_until = self.releasing.get(bf.mac)
        if rel_until is not None:
            if self._now() > rel_until or frame.type == JOIN_REQUEST:
                self.releasing.pop(bf.mac, None)    # window over, or it dropped the key
            else:                                   # still on our key: hit it while it listens
                out.tx.append((bf.mac, build_release(self.key, bf.mac)))
                return out
        t = frame.type
        if t == CORE_REQUEST:
            self._touch(bf.mac)
            out.tx.append((bf.mac, build_core_reply(self.key)))
        elif t == HEARTBEAT:
            self._touch(bf.mac)
        elif t == CLAIM and isinstance(frame, ClaimFrame):
            out.lines.append(self._cnode_line(frame))
            # A node adds the core as an ESP-NOW peer ONLY when it gets a CORE_REPLY
            # (not on ADMIN). Without that peer it can never unicast its TEXT records
            # back, so it sits "core-ok" but silent. A node that latched via ADMIN
            # stops sending CORE_REQUEST, so we would never answer it -> deadlock.
            # Answer every CLAIM with a CORE_REPLY: it (re)establishes the peer and
            # refreshes the core-liveness timer. Cheap and idempotent on the node.
            out.tx.append((bf.mac, build_core_reply(self.key)))
            # The node only listens for a moment after it reports, so an ADMIN sent
            # on the tick can miss it for minutes (seen live: a channel half left
            # uncovered for ~4 min after a re-adoption). If the slice it reports is
            # not the one we assigned, resend its ADMIN now, while it is listening.
            ns = self.nodes.get(frame.mac)
            if (ns is not None and ns.admin_args is not None
                    and (frame.start_idx, frame.end_idx) != (ns.assigned_lo, ns.assigned_hi)):
                pos, count, lo, hi = ns.admin_args
                out.tx.append((bf.mac, build_admin(self.key, self.assignment_version,
                                                   pos, count, lo, hi, self.core_session)))
        elif t == TEXT and isinstance(frame, TextFrame):
            ns = self._touch(bf.mac)
            out.lines.extend(self._cwd_lines(ns, frame))
        elif t == JOIN_REQUEST and isinstance(frame, JoinReqFrame):
            src = (bf.mac or "").lower()
            mac = (frame.mac or src).lower()
            if src and mac != src:
                return out                      # payload MAC must match the radio source
            self._expire_joins()
            if mac in self.nodes:
                return out
            # A request is only valid for JOIN_TTL_S; the node keeps re-sending
            # while it waits, which refreshes it.
            self._join_expiry[mac] = self._now() + JOIN_TTL_S
            if mac not in self.pending_joins:
                self.pending_joins.append(mac)
                out.lines.append("CJOIN,%s" % mac)
        return out

    def live_joins(self) -> list:
        """MACs whose JOIN_REQUEST has not expired yet (read-only snapshot)."""
        now = self._now()
        expiry = dict(self._join_expiry)
        return [m for m in list(self.pending_joins) if m in expiry and now <= expiry[m]]

    def _expire_joins(self) -> None:
        now = self._now()
        for m in [m for m, until in self._join_expiry.items() if now > until]:
            self._join_expiry.pop(m, None)
        self.pending_joins = [m for m in self.pending_joins if m in self._join_expiry]

    def handle_line(self, line: str) -> BrainOut:
        from cluster_enow import parse_bridge_line
        bf = parse_bridge_line(line)
        return self.handle(bf) if bf else BrainOut()

    # -- operator action ----------------------------------------------------
    def accept(self, mac: str) -> BrainOut:
        """Operator pressed [A] on a pending joiner: hand it the fleet key.

        Only a MAC with a live JOIN_REQUEST heard by THIS brain (source-checked,
        not expired) gets a grant, and each request is consumed once: a stale UI
        entry or a CJOIN line from another path never makes the brain send the key."""
        out = BrainOut()
        mac = (mac or "").strip().lower()
        self._expire_joins()
        if not mac or mac not in self.pending_joins or not self.key.present:
            return out
        self.pending_joins.remove(mac)
        self._join_expiry.pop(mac, None)
        out.tx.append((mac, build_join_grant(self.key)))
        return out

    def release(self, mac: str) -> BrainOut:
        """Operator released a node: tell it to drop the fleet key (RELEASE, signed
        with the CURRENT key) and forget it locally so it stops getting a slice.

        The node, once it processes RELEASE, wipes its key and falls back to
        JOIN_REQUEST — so it can be adopted by another rig (e.g. the phone). We send
        it a few times because a node is on the control channel only briefly."""
        out = BrainOut()
        mac = (mac or "").strip().lower()
        if not mac or not self.key.present:
            return out
        payload = build_release(self.key, mac)
        self.releasing[mac] = self._now() + RELEASE_S
        out.tx.append((mac, payload))
        out.tx.append((BROADCAST, payload))
        self.nodes.pop(mac, None)
        self.pending_joins = [m for m in self.pending_joins if m != mac]
        self._join_expiry.pop(mac, None)
        if self.ble_mac == mac:
            self.ble_mac = None
        return out

    def release_all(self) -> BrainOut:
        """Release every node currently in the fleet."""
        out = BrainOut()
        for mac in list(self.nodes.keys()):
            out.extend(self.release(mac))
        return out

    # -- operator policy ---------------------------------------------------
    def set_bt_duty(self, on: bool) -> None:
        """Keep one node of a >=2-node pack on Bluetooth duty (True), or let the
        whole fleet scan WiFi (False)."""
        self.bt_duty = bool(on)

    def set_channel_pool(self, lo_hi: Optional[Tuple[int, int]]) -> None:
        """Restrict WiFi scanning to an inclusive ENOW_CHANNELS index window that
        the WiFi nodes split among themselves, or None for the whole band."""
        self._pool = self._clamp_window(lo_hi)

    def set_node_channels(self, mac: str, lo_hi: Optional[Tuple[int, int]]) -> None:
        """Pin one node to an inclusive ENOW_CHANNELS index window (manual override
        of the auto split), or None to hand it back to auto. The remaining WiFi
        nodes auto-split the pool around it."""
        w = self._clamp_window(lo_hi)
        if w is None:
            self._overrides.pop(mac, None)
        else:
            self._overrides[mac] = w

    def node_window(self, mac: str) -> Optional[Tuple[int, int]]:
        """The manual pin for a node, or None if it is on the auto split."""
        return self._overrides.get(mac)

    @staticmethod
    def _clamp_window(lo_hi: Optional[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
        if lo_hi is None:
            return None
        lo, hi = int(lo_hi[0]), int(lo_hi[1])
        lo = max(0, min(lo, NUM_CHANNELS - 1))
        hi = max(lo, min(hi, NUM_CHANNELS - 1))
        return (lo, hi)

    # -- role + channel-slice assignment -----------------------------------
    def _active_nodes(self, now: float) -> List[NodeState]:
        """Every node heard within RETIRE_S, in stable index order."""
        return sorted((ns for ns in self.nodes.values()
                       if now - ns.last_seen <= RETIRE_S),
                      key=lambda n: n.idx)

    def _roles(self, now: float) -> Tuple[Optional[str], List[NodeState]]:
        """Decide roles among the active nodes and return (designate, wifi):
        `designate` is the mac we must newly tell to do BLE this tick (or None),
        `wifi` is the WiFi-duty set to split channels over. VITA nodes bridge a
        Vita and self-reported BLE nodes already cover Bluetooth — both stay out
        of the WiFi split untouched. When bt_duty is on, no node is already doing
        BLE, and >=2 WiFi nodes exist, the last one is converted to BLE duty so
        Bluetooth is always covered. Picking the last index keeps it sticky (once
        it reports back as BLE it is simply excluded, never re-picked)."""
        active = self._active_nodes(now)
        wifi = [ns for ns in active if ns.role == "wifi"]
        ble_existing = next((ns.mac for ns in active if ns.role == "ble"), None)
        designate = None
        if self.bt_duty and ble_existing is None and len(wifi) >= 2:
            designate = wifi[-1].mac
            wifi = wifi[:-1]
        self.ble_mac = designate or ble_existing         # whichever node covers BLE now
        return designate, wifi

    def _compute_slices(self, wifi: List[NodeState]) -> Dict[str, Tuple[int, int, int, int]]:
        """mac -> (node_index, node_count, lo_idx, hi_idx). A node the operator has
        pinned (set_node_channels) scans exactly its window; the rest auto-split the
        pool contiguously around it. node_index/count span ALL WiFi nodes so the
        firmware's report-window skew still staggers everyone."""
        total = len(wifi)
        if total == 0:
            return {}
        lo_pool, hi_pool = self._pool if self._pool else (0, NUM_CHANNELS - 1)
        auto = [ns for ns in wifi if ns.mac not in self._overrides]
        # Channels a manual pin already covers are not handed out again by the auto
        # split (they used to overlap). Only pins of nodes that are active count.
        pinned = set()
        for ns in wifi:
            w = self._overrides.get(ns.mac)
            if w:
                pinned.update(range(w[0], w[1] + 1))
        pool = list(range(lo_pool, hi_pool + 1))
        free = [i for i in pool if i not in pinned] or pool
        auto_ranges: Dict[str, Tuple[int, int]] = {}
        na = len(auto)
        if na > len(free):
            # More WiFi nodes than channels: never send an empty/inverted range
            # (it used to produce e.g. (2, 1)); the extra nodes share single
            # channels round-robin.
            for pos, ns in enumerate(auto):
                c = free[pos % len(free)]
                auto_ranges[ns.mac] = (c, c)
        elif na:
            base, rem = divmod(len(free), na)
            start = 0
            for pos, ns in enumerate(auto):
                width = base + (1 if pos < rem else 0)   # spread the remainder
                chunk = free[start:start + width]
                start += width
                # The node firmware takes ONE contiguous range: a chunk that spans
                # a pinned hole is sent as (first, last) and covers that hole too.
                auto_ranges[ns.mac] = (chunk[0], chunk[-1])
        out: Dict[str, Tuple[int, int, int, int]] = {}
        for idx, ns in enumerate(wifi):
            lo, hi = self._overrides.get(ns.mac) or auto_ranges.get(ns.mac, (lo_pool, hi_pool))
            out[ns.mac] = (idx, total, lo, hi)
        return out

    def _admin_due(self, ns: NodeState, now: float) -> bool:
        """True (and arms the reaffirm timer) when this node is owed an ADMIN."""
        if ns.assign_ver_sent == self.assignment_version and now - ns.last_admin < REAFFIRM_S:
            return False
        ns.assign_ver_sent = self.assignment_version
        ns.last_admin = now
        return True

    def tick(self, now: Optional[float] = None) -> BrainOut:
        """Recompute roles + the channel split and (re)advertise. Call periodically
        (~every 1 s). ADMIN goes out when the split changes OR a node's assignment
        has not been reaffirmed in REAFFIRM_S. One node (of >=2) is held on BLE duty."""
        now = self._now() if now is None else now
        out = BrainOut()
        for mac, until in list(self.releasing.items()):
            if now > until:
                self.releasing.pop(mac, None)
            else:
                out.tx.append((BROADCAST, build_release(self.key, mac)))
        ble_mac, wifi = self._roles(now)
        slices = self._compute_slices(wifi)
        total = len(wifi) + (1 if ble_mac else 0)
        # snapshot drives the version bump; the BLE designation is part of it so a
        # role change re-advertises too.
        snapshot: Dict[str, Tuple] = {mac: (lo, hi) for mac, (_, _, lo, hi) in slices.items()}
        if ble_mac:
            snapshot[ble_mac] = ("ble",)
        if snapshot != self._slice_snapshot:
            self.assignment_version = (self.assignment_version % 254) + 1   # 1..254, never 0
            self._slice_snapshot = snapshot
        for mac, (pos, count, lo, hi) in slices.items():
            ns = self.nodes[mac]
            ns.assigned_lo, ns.assigned_hi = lo, hi
            ns.admin_args = (pos, count, lo, hi)
            if self._admin_due(ns, now):
                out.tx.append((mac, build_admin(
                    self.key, self.assignment_version, pos, count, lo, hi,
                    self.core_session)))
        if ble_mac:                              # ROLE_BLE sentinel in both channel fields
            ns = self.nodes[ble_mac]
            ns.assigned_lo = ns.assigned_hi = ROLE_BLE
            ns.admin_args = (total - 1, total, ROLE_BLE, ROLE_BLE)
            if self._admin_due(ns, now):
                out.tx.append((ble_mac, build_admin(
                    self.key, self.assignment_version, total - 1, total,
                    ROLE_BLE, ROLE_BLE, self.core_session)))
        return out

    # -- line builders ------------------------------------------------------
    def _cnode_line(self, c: ClaimFrame) -> str:
        ns = self._touch(c.mac)
        if c.ble_role:
            role, lo, hi = "ble", 0, 0
        elif c.vita_role:
            role, lo, hi = "vita", 0, 0
        else:
            role, lo, hi = "wifi", c.start_idx, c.end_idx
        ns.role = role
        temp = c.temp_c if c.temp_c is not None else -128
        by_core = 1 if (c.flags & CLAIM_FROM_CORE) or c.assignment_version > 0 else 0
        fw = "v%d" % c.fw_ver
        # CNODE,idx,mac,role,lo,hi,cov,rec_min,buffered,temp,uptime,fw,dropped,
        #       dedup,hops,dropouts,last_gap,by_core,serial_drop
        return "CNODE,%d,%s,%s,%d,%d,%d,%d,%d,%d,%d,%s,%d,%d,%d,%d,%d,%d,0" % (
            ns.idx, c.mac, role, lo, hi,
            100,                                 # cov% (TODO: derive once we assign slices)
            c.rec_per_min, c.buffered, temp, c.uptime_s, fw,
            c.dropped, c.dedup_pct, c.hops_per_min,
            ns.dropouts, ns.last_gap_s, by_core)

    def _cwd_lines(self, ns: NodeState, f: TextFrame) -> List[str]:
        lines: List[str] = []
        for rec in f.text.split("\n"):
            rec = rec.strip()
            if not rec:
                continue
            r = parse_text_record(rec)
            if r is None:
                continue
            b64 = base64.b64encode(r["ssid"].encode("utf-8")).decode() if r["ssid"] else ""
            band = _band_for(r["channel"], r["is_ble"])
            freq = _freq_for(r["channel"], r["is_ble"])
            # CWD,idx,BSSID,SSID_b64,AUTH,CH,FREQ,RSSI,BAND,age_ms
            lines.append("CWD,%d,%s,%s,%s,%d,%d,%d,%s,%d" % (
                ns.idx, r["bssid"], b64, r["sec"], r["channel"], freq,
                r["rssi"], band, r["age_ms"]))
        return lines
