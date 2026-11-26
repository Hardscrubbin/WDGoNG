"""Unit tests for cluster_brain — run: python3 wdgong_ui/test_cluster_brain.py

Feeds the brain synthetic (but byte-exact) node frames and asserts the CNODE /
CWD / CJOIN lines it emits match the format the backend ingest expects, plus the
CORE_REPLY / JOIN_GRANT it transmits. Deterministic clock -> dropout accounting
is testable. No hardware.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cluster_enow as E
import cluster_brain as B

_n = 0
def check(cond, msg):
    global _n
    assert cond, "FAIL: " + msg
    _n += 1


KEY = E.FleetKey(bytes(range(16)))          # throwaway test key, not the real one


# --- synthetic node-side frame builders ------------------------------------
def make_text(text: str, key=KEY, mtype=E.TEXT, counter=0) -> bytes:
    raw = text.encode("utf-8")[:E.TEXT_MAX]
    body = E._S_TEXT.pack(E.MAGIC, mtype, counter, len(raw), raw, 0)[:-4]
    return body + struct.pack("<I", key.auth(body))

def make_claim(mac, key=KEY, *, start=0, end=13, aver=1, records=50, flags=E.CLAIM_FROM_CORE,
               hops=90, recmin=60, buffered=12, fw_ver=31, dedup=40, dropped=3,
               uptime=1234, temp=42) -> bytes:
    prefix = E._S_CLAIM_PREFIX.pack(E.MAGIC, E.CLAIM, E.mac_to_bytes(mac),
                                    start, end, aver, records, flags, hops, recmin, buffered)
    body = bytearray(prefix)
    body += bytes(range(8))                       # fwsha
    body += struct.pack("<H", fw_ver)
    body += struct.pack("<BH", dedup, dropped)
    body += struct.pack("<I", uptime)
    body += struct.pack("<b", temp)
    body += struct.pack("<I", key.auth(bytes(body)))
    return bytes(body)

def make_join_req(mac, key=KEY, fw_ver=31, rekey=0) -> bytes:
    body = E._S_JOINREQ.pack(E.MAGIC, E.JOIN_REQUEST, E.mac_to_bytes(mac), fw_ver, rekey, 0)[:-4]
    return body + struct.pack("<I", E.wdg_auth_join(body))

def bline(mac, payload, rssi=-60, ch=6) -> E.BridgeFrame:
    return E.BridgeFrame(mac=mac, rssi=rssi, channel=ch, payload=payload)

NODE1 = "02:00:00:00:00:d4"
NODE2 = "02:00:00:00:00:bc"
JOINER = "02:00:00:00:00:99"


# --- parse_text_record ------------------------------------------------------
r = B.parse_text_record("AA:BB:CC:DD:EE:FF,MyWifi,WPA2,6,-55,W,1200")
check(r and r["bssid"] == "AA:BB:CC:DD:EE:FF" and r["ssid"] == "MyWifi", "record basic")
check(r["sec"] == "WPA2" and r["channel"] == 6 and r["rssi"] == -55, "record fields")
check(r["is_ble"] is False and r["age_ms"] == 1200, "record wifi+age")
# SSID with a comma must survive (fixed fields read from the right)
r = B.parse_text_record("AA:BB:CC:DD:EE:FF,Cafe, Bar,OPEN,11,-70,W,50")
check(r and r["ssid"] == "Cafe, Bar" and r["channel"] == 11, "record ssid comma")
# BLE record
r = B.parse_text_record("AA:BB:CC:DD:EE:FF,Tile,[BLE],0,-88,B,10")
check(r and r["is_ble"] is True and r["channel"] == 0, "record ble")
# legacy 6-field (no age)
r = B.parse_text_record("AA:BB:CC:DD:EE:FF,Net,WPA2,36,-60,W")
check(r and r["age_ms"] == 0 and r["channel"] == 36, "record no-age")
check(B.parse_text_record("garbage") is None, "record junk")

# band / freq
check(B._band_for(6, False) == "wifi24" and B._band_for(36, False) == "wifi5", "band wifi")
check(B._band_for(0, True) == "ble", "band ble")
check(B._freq_for(6, False) == 2437 and B._freq_for(14, False) == 2484, "freq 2.4")
check(B._freq_for(36, False) == 5180 and B._freq_for(0, True) == 0, "freq 5/ble")


# --- CLAIM -> CNODE ---------------------------------------------------------
brain = B.ClusterBrain(KEY, core_session=0x11223344)
out = brain.handle(bline(NODE1, make_claim(NODE1, start=0, end=13, buffered=12, temp=42)))
check(len(out.lines) == 1 and out.lines[0].startswith("CNODE,"), "claim -> one CNODE")
p = out.lines[0].split(",")
check(p[2] == NODE1 and p[3] == "wifi", "CNODE mac+role")
check(int(p[1]) == 0, "CNODE idx 0 (first node)")
check(int(p[4]) == 0 and int(p[5]) == 13, "CNODE slice lo/hi")
check(int(p[7]) == 60 and int(p[8]) == 12, "CNODE rec_min/buffered")
check(int(p[9]) == 42 and int(p[10]) == 1234, "CNODE temp/uptime")
check(p[11] == "v31" and int(p[13]) == 40, "CNODE fw/dedup")
check(int(p[17]) == 1, "CNODE by_core=1 (assignment_version>0)")
check(len(p) == 19, "CNODE has 19 fields")

# a second node gets idx 1, and idx is stable across its later frames
brain.handle(bline(NODE2, make_claim(NODE2)))
out = brain.handle(bline(NODE2, make_claim(NODE2, records=99)))
check(out.lines[0].split(",")[1] == "1", "second node idx stable at 1")

# BLE role sentinel -> role ble, lo/hi 0
outb = brain.handle(bline(NODE1, make_claim(NODE1, start=E.ROLE_BLE, end=E.ROLE_BLE)))
pb = outb.lines[0].split(",")
check(pb[3] == "ble" and pb[4] == "0" and pb[5] == "0", "CNODE ble role")

# temp -128 passthrough (node too old / sensor n/a) when the claim omits temp
short_claim = make_claim(NODE1)[:E.CLAIM_LEN_UPTIME]      # drop temp byte + re-sign
short_claim = short_claim[:-4]
short_claim = short_claim + struct.pack("<I", KEY.auth(short_claim))
pc = brain.handle(bline(NODE1, short_claim)).lines[0].split(",")
check(pc[9] == "-128", "CNODE temp -128 when claim has no temp")


# --- TEXT -> CWD (several records packed, newline-separated) ----------------
brain = B.ClusterBrain(KEY)
brain.handle(bline(NODE1, make_claim(NODE1)))            # register idx 0
# a real (unescaped) comma in the middle SSID proves right-anchored field parsing
packed = ("AA:BB:CC:00:00:01,HomeNet,WPA2,6,-55,W,1200\n"
          "AA:BB:CC:00:00:02,Cafe, Bar,OPEN,11,-70,W,50\n"
          "AA:BB:CC:00:00:03,Tile,BLE,0,-88,B,10")
out = brain.handle(bline(NODE1, make_text(packed)))
check(len(out.lines) == 3 and all(l.startswith("CWD,") for l in out.lines), "3 CWD lines")
c0 = out.lines[0].split(",")
check(c0[1] == "0" and c0[2] == "AA:BB:CC:00:00:01", "CWD idx + bssid")
import base64 as _b64
check(_b64.b64decode(c0[3]).decode() == "HomeNet", "CWD ssid b64")
check(c0[4] == "WPA2" and int(c0[5]) == 6 and int(c0[6]) == 2437, "CWD auth/ch/freq")
check(int(c0[7]) == -55 and c0[8] == "wifi24" and int(c0[9]) == 1200, "CWD rssi/band/age")
# the comma SSID survived
c1 = out.lines[1].split(",", 9)                           # only split into 10 fields max... but ssid is b64 (no commas)
check(_b64.b64decode(out.lines[1].split(",")[3]).decode() == "Cafe, Bar", "CWD comma ssid")
# the BLE record
cble = out.lines[2].split(",")
check(cble[8] == "ble" and int(cble[6]) == 0, "CWD ble band/freq")

# empty / junk lines in a frame are skipped
out = brain.handle(bline(NODE1, make_text("\n\ngarbage\nAA:BB:CC:00:00:09,X,OPEN,1,-40,W,0\n")))
check(len(out.lines) == 1, "CWD skips blank/junk lines")


# --- CORE_REQUEST -> CORE_REPLY --------------------------------------------
brain = B.ClusterBrain(KEY)
req = make_text("", mtype=E.CORE_REQUEST)
out = brain.handle(bline(NODE1, req))
check(not out.lines and len(out.tx) == 1, "core_request -> a tx, no line")
dst, payload = out.tx[0]
check(dst == NODE1, "reply goes to the requester")
# CORE_REPLY is a minimal 9-byte outbound frame (magic+type+auth); it is never
# decoded by the core, so verify the raw bytes the node will authenticate.
check(len(payload) == 9 and payload[4] == E.CORE_REPLY, "reply is a minimal CORE_REPLY")
check(KEY.auth(payload[:-4]) == int.from_bytes(payload[-4:], "little"), "reply is authed")
check(NODE1 in brain.nodes, "requester registered")

# HEARTBEAT just touches, emits nothing
out = brain.handle(bline(NODE1, make_text("", mtype=E.HEARTBEAT, counter=5)))
check(not out.lines and not out.tx, "heartbeat silent")


# --- JOIN_REQUEST -> CJOIN, accept -> JOIN_GRANT ---------------------------
brain = B.ClusterBrain(KEY)
out = brain.handle(bline(JOINER, make_join_req(JOINER)))
check(out.lines == ["CJOIN,%s" % JOINER] and JOINER in brain.pending_joins, "join -> CJOIN + pending")
# a repeat does not double-list
out = brain.handle(bline(JOINER, make_join_req(JOINER)))
check(out.lines == [], "join repeat suppressed")
# operator accepts -> a JOIN_GRANT carrying the fleet key, pending cleared
out = brain.accept(JOINER)
check(len(out.tx) == 1 and JOINER not in brain.pending_joins, "accept -> grant, pending cleared")
dst, g = out.tx[0]
_, _t, gk, _a = E._S_JOINGRANT.unpack(g)
check(_t == E.JOIN_GRANT and gk == bytes(range(16)), "grant carries fleet key")
check(E._auth_tail(g) == E.wdg_auth_join(g[:-4]), "grant signed with join secret")


# --- dropout accounting (deterministic clock) ------------------------------
clock = {"t": 1000.0}
brain = B.ClusterBrain(KEY, now=lambda: clock["t"])
brain.handle(bline(NODE1, make_claim(NODE1)))             # first seen @1000
clock["t"] = 1005.0                                       # 5s later: within threshold
p = brain.handle(bline(NODE1, make_claim(NODE1))).lines[0].split(",")
check(int(p[15]) == 0, "no dropout within gap threshold")
clock["t"] = 1005.0 + B.GAP_THRESHOLD_S + 20              # a long gap
p = brain.handle(bline(NODE1, make_claim(NODE1))).lines[0].split(",")
check(int(p[15]) == 1 and int(p[16]) >= int(B.GAP_THRESHOLD_S), "one dropout + gap recorded")


# --- auth gate --------------------------------------------------------------
brain = B.ClusterBrain(KEY)
forged = bytearray(make_claim(NODE1)); forged[-1] ^= 0xFF   # break the tag
check(brain.handle(bline(NODE1, bytes(forged))).lines == [], "forged claim ignored")
check(NODE1 not in brain.nodes, "forged claim did not register a node")
# wrong-key brain cannot read a frame signed with the real test key
other = B.ClusterBrain(E.FleetKey(bytes([9]) * 16))
check(other.handle(bline(NODE1, make_claim(NODE1))).lines == [], "wrong key -> ignored")

# --- line protocol passthrough ---------------------------------------------
brain = B.ClusterBrain(KEY)
line = "ENOW,%s,-61,6,%s" % (NODE1, make_claim(NODE1).hex())
check(brain.handle_line(line).lines[0].startswith("CNODE,"), "handle_line works")
check(brain.handle_line("not-an-enow-line").lines == [], "handle_line junk safe")


# --- increment 2: channel-slice assignment (ADMIN / tick) ------------------
def admin_fields(payload):
    _, t, aver, nidx, ncnt, s0, s1, sess, _a = E._S_ADMIN.unpack(payload)
    return t, aver, nidx, ncnt, s0, s1, sess

clk = {"t": 2000.0}
brain = B.ClusterBrain(KEY, core_session=0xABCD1234, now=lambda: clk["t"])
brain.set_bt_duty(False)        # isolate the pure WiFi channel-split math here
# no nodes -> tick does nothing
check(brain.tick().tx == [], "tick with no nodes -> nothing")
# two wifi nodes register
brain.handle(bline(NODE1, make_claim(NODE1)))
brain.handle(bline(NODE2, make_claim(NODE2)))
out = brain.tick()
check(len(out.tx) == 2, "tick -> ADMIN for both nodes")
check(brain.assignment_version == 1, "first assignment is version 1")
# decode both and check the split is contiguous and covers 0..39
by_mac = {}
for mac, pay in out.tx:
    f = E.decode(pay, KEY)
    check(f.type == E.ADMIN and f.authed, "ADMIN authed")
    by_mac[mac] = admin_fields(pay)
t, aver, nidx, ncnt, s0, s1, sess = by_mac[NODE1]
check(ncnt == 2 and aver == 1 and sess == 0xABCD1234, "ADMIN count/ver/session")
check(by_mac[NODE1][4:6] == (0, 19) and by_mac[NODE2][4:6] == (20, 39), "2-way split 0-19 / 20-39")
check(by_mac[NODE1][2] == 0 and by_mac[NODE2][2] == 1, "node_index 0 and 1")

# immediate re-tick within REAFFIRM_S, no change -> no ADMIN
clk["t"] += 1.0
check(brain.tick().tx == [], "no re-ADMIN within reaffirm window")
# past the reaffirm window -> ADMIN re-sent (same version)
clk["t"] += B.REAFFIRM_S + 0.1
out = brain.tick()
check(len(out.tx) == 2 and brain.assignment_version == 1, "reaffirm re-sends, version steady")

# a third node joins -> version bumps and the split becomes 14/13/13
NODE3 = "02:00:00:00:00:33"
clk["t"] += 1.0
brain.handle(bline(NODE3, make_claim(NODE3)))
out = brain.tick()
check(brain.assignment_version == 2, "membership change bumps version")
check(len(out.tx) == 3, "all three get the new split")
sp = {mac: admin_fields(pay)[4:6] for mac, pay in out.tx}
check(sp[NODE1] == (0, 13) and sp[NODE2] == (14, 26) and sp[NODE3] == (27, 39),
      "3-way split 0-13 / 14-26 / 27-39 (remainder on the first)")

# a BLE-duty node is left out of the split
NODE4 = "02:00:00:00:00:44"
clk["t"] += 1.0
brain.handle(bline(NODE4, make_claim(NODE4, start=E.ROLE_BLE, end=E.ROLE_BLE)))
out = brain.tick()
macs_assigned = {mac for mac, _ in out.tx}
check(NODE4 not in macs_assigned, "BLE node excluded from channel split")

# a node that goes stale drops out of the split (its slice is reclaimed)
clk["t"] += B.RETIRE_S + 5                      # everyone goes stale...
brain.handle(bline(NODE1, make_claim(NODE1)))   # ...but NODE1 just checked in
out = brain.tick()
sp = {mac: admin_fields(pay)[4:6] for mac, pay in out.tx}
check(list(sp.keys()) == [NODE1] and sp[NODE1] == (0, 39), "sole live node gets the whole range")


# --- BT duty: one node of a >=2-node pack is put on Bluetooth ---------------
clk2 = {"t": 5000.0}
b2 = B.ClusterBrain(KEY, core_session=0x11112222, now=lambda: clk2["t"])   # bt_duty on by default
for nm in (NODE1, NODE2, NODE3):
    b2.handle(bline(nm, make_claim(nm)))
out = b2.tick()
fields = {mac: admin_fields(pay) for mac, pay in out.tx}
ble = [mac for mac, f in fields.items() if f[4] == E.ROLE_BLE and f[5] == E.ROLE_BLE]
wifi = [mac for mac, f in fields.items() if f[4] != E.ROLE_BLE]
check(len(ble) == 1, "BT duty: exactly one node put on BLE")
check(len(wifi) == 2 and b2.ble_mac == ble[0], "BT duty: other two on WiFi, ble_mac tracks it")
check(sorted(fields[m][4:6] for m in wifi) == [(0, 19), (20, 39)], "BT duty: WiFi pair split 0-19 / 20-39")

# sticky: the BLE node reports back as BLE -> not re-designated, stays BLE, no churn
clk2["t"] += 1.0
b2.handle(bline(ble[0], make_claim(ble[0], start=E.ROLE_BLE, end=E.ROLE_BLE)))
out = b2.tick()
check(E.ROLE_BLE not in [admin_fields(p)[4] for _, p in out.tx], "BT duty sticky: no re-designation")
check(b2.ble_mac == ble[0], "BT duty sticky: same node keeps BLE")

# channel-pool override: the WiFi split is restricted to the given window
clk2["t"] += B.REAFFIRM_S + 1
b2.set_channel_pool((0, 9))
out = b2.tick()
wf = sorted(admin_fields(p)[4:6] for _, p in out.tx if admin_fields(p)[4] != E.ROLE_BLE)
check(wf == [(0, 4), (5, 9)], "channel pool: WiFi split restricted to indices 0-9")

# bt_duty off with a >=2 pack still covers BLE if a node already self-reports it
b2b = B.ClusterBrain(KEY, now=lambda: clk2["t"]); b2b.set_bt_duty(False)
b2b.handle(bline(NODE1, make_claim(NODE1)))
b2b.handle(bline(NODE2, make_claim(NODE2)))
out = b2b.tick()
check(all(admin_fields(p)[4] != E.ROLE_BLE for _, p in out.tx), "bt_duty off: no node forced to BLE")

# --- per-node channel pin: operator overrides one node, the rest auto-split --
b3 = B.ClusterBrain(KEY, now=lambda: clk2["t"]); b3.set_bt_duty(False)
for nm in (NODE1, NODE2, NODE3):
    b3.handle(bline(nm, make_claim(nm)))
b3.set_node_channels(NODE2, (0, 13))        # pin NODE2 to the 2.4GHz indices
out = b3.tick()
fx = {mac: admin_fields(pay)[4:6] for mac, pay in out.tx}
check(fx[NODE2] == (0, 13), "per-node pin: NODE2 scans its pinned window")
check(fx[NODE1] == (14, 26) and fx[NODE3] == (27, 39), "per-node pin: other two auto-split the channels the pin leaves free")
check(b3.node_window(NODE2) == (0, 13), "node_window reports the pin")
clk2["t"] += B.REAFFIRM_S + 1
b3.set_node_channels(NODE2, None)           # clear -> back to the 3-way auto split
out = b3.tick()
fx = {mac: admin_fields(pay)[4:6] for mac, pay in out.tx}
check(b3.node_window(NODE2) is None and fx[NODE1] == (0, 13), "clearing pin -> 3-way auto split resumes")

# --- link protocol: line parse -> CNODE already covered above --------------
print(f"OK — {_n} checks passed")
