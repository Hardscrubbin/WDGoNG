"""Unit tests for cluster_link — run: python3 wdgong_ui/test_cluster_link.py

Drives the I/O-free ClusterLink with fake write/ingest sinks (no serial, no
hardware) and asserts it routes brain output to the right place: CNODE/CWD/CJOIN
to `ingest`, 'enow_tx ...' and the start/stop commands to `write`.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cluster_enow as E
import cluster_brain as B
import cluster_link as L

_n = 0
def check(cond, msg):
    global _n
    assert cond, "FAIL: " + msg
    _n += 1


KEY = E.FleetKey(bytes(range(16)))
NODE1 = "02:00:00:00:00:73"
JOINER = "02:00:00:00:00:51"

# synthetic node-side frame builders (self-contained, so importing a sibling
# test does not run its checks as a side effect)
def make_text(text, key=KEY, mtype=E.TEXT, counter=0):
    raw = text.encode("utf-8")[:E.TEXT_MAX]
    body = E._S_TEXT.pack(E.MAGIC, mtype, counter, len(raw), raw, 0)[:-4]
    return body + struct.pack("<I", key.auth(body))

def make_claim(mac, key=KEY, *, start=0, end=13):
    prefix = E._S_CLAIM_PREFIX.pack(E.MAGIC, E.CLAIM, E.mac_to_bytes(mac),
                                    start, end, 1, 50, E.CLAIM_FROM_CORE, 90, 60, 12)
    body = bytearray(prefix)
    body += bytes(range(8)) + struct.pack("<H", 31) + struct.pack("<BH", 40, 3)
    body += struct.pack("<I", 1234) + struct.pack("<b", 42)
    body += struct.pack("<I", key.auth(bytes(body)))
    return bytes(body)

def make_join_req(mac, key=KEY, fw_ver=31, rekey=0):
    body = E._S_JOINREQ.pack(E.MAGIC, E.JOIN_REQUEST, E.mac_to_bytes(mac), fw_ver, rekey, 0)[:-4]
    return body + struct.pack("<I", E.wdg_auth_join(body))


def new_link(clock):
    writes, ingests = [], []
    brain = B.ClusterBrain(KEY, core_session=0x01020304, now=lambda: clock["t"])
    link = L.ClusterLink(brain, write=writes.append, ingest=ingests.append,
                         now=lambda: clock["t"])
    return link, brain, writes, ingests


def enow(mac, payload, rssi=-60, ch=6):
    return "ENOW,%s,%d,%d,%s" % (mac, rssi, ch, payload.hex())


# --- lifecycle commands -----------------------------------------------------
clk = {"t": 100.0}
link, brain, writes, ingests = new_link(clk)
link.start()
check(writes == [L.START_CMD] and link.started, "start sends start_cluster_bridge")
writes.clear()
link.stop()
check(writes == [L.STOP_CMD] and not link.started, "stop sends stop")

# --- CLAIM line -> CNODE to ingest + a CORE_REPLY so the node peers us -------
link, brain, writes, ingests = new_link(clk)
link.feed_line(enow(NODE1, make_claim(NODE1)))
check(len(ingests) == 1 and ingests[0].startswith("CNODE,"), "claim -> CNODE ingested")
check(len(writes) == 1 and writes[0].startswith("enow_tx %s " % NODE1), "claim -> CORE_REPLY write")

# --- CORE_REQUEST line -> a reply written as enow_tx ------------------------
link, brain, writes, ingests = new_link(clk)
link.feed_line(enow(NODE1, make_text("", mtype=E.CORE_REQUEST)))
check(len(writes) == 1 and writes[0].startswith("enow_tx %s " % NODE1), "core_request -> enow_tx reply")
# the written frame is a minimal (9-byte) CORE_REPLY
hexpart = writes[0].split(" ", 2)[2]
_frame = bytes.fromhex(hexpart)
check(len(_frame) == 9 and _frame[4] == E.CORE_REPLY, "written frame is a minimal CORE_REPLY")

# --- TEXT line -> CWD lines ingested ---------------------------------------
link, brain, writes, ingests = new_link(clk)
link.feed_line(enow(NODE1, make_claim(NODE1)))       # register idx 0
ingests.clear()
link.feed_line(enow(NODE1, make_text(
    "AA:BB:CC:00:00:01,Home,WPA2,6,-55,W,100\nAA:BB:CC:00:00:02,Cafe,OPEN,36,-70,W,5")))
check(len(ingests) == 2 and all(s.startswith("CWD,") for s in ingests), "text -> 2 CWD ingested")

# --- poll() advertises the split as enow_tx ADMIN, with interval gating -----
clk["t"] = 200.0
link, brain, writes, ingests = new_link(clk)
link.feed_line(enow(NODE1, make_claim(NODE1)))
writes.clear()
link.poll()                                          # first poll ticks
admins = [w for w in writes if w.startswith("enow_tx")]
check(len(admins) == 1, "poll advertises one ADMIN for the one node")
adm_hex = admins[0].split(" ", 2)[2]
check(E.decode(bytes.fromhex(adm_hex), KEY).type == E.ADMIN, "poll frame is ADMIN")
# a second poll immediately after is gated by tick_interval -> no new tick
writes.clear()
clk["t"] += 0.2
link.poll()
check(writes == [], "poll gated within tick_interval")
# past the interval -> ticks again (reaffirm window not yet passed -> no ADMIN,
# but the tick ran without error)
clk["t"] += L.TICK_INTERVAL_S + 0.1
link.poll()
check(True, "poll past interval runs clean")

# --- accept() -> JOIN_GRANT written ----------------------------------------
link, brain, writes, ingests = new_link(clk)
link.feed_line(enow(JOINER, make_join_req(JOINER)))
check(ingests == ["CJOIN,%s" % JOINER], "join -> CJOIN ingested")
writes.clear()
link.accept(JOINER)
check(len(writes) == 1 and writes[0].startswith("enow_tx %s " % JOINER), "accept -> grant written")
g = bytes.fromhex(writes[0].split(" ", 2)[2])
_, t, gk, _a = E._S_JOINGRANT.unpack(g)
check(t == E.JOIN_GRANT and gk == bytes(range(16)), "written grant carries the key")

# --- junk / empty inbound lines are safe -----------------------------------
link, brain, writes, ingests = new_link(clk)
link.feed_line("")
link.feed_line("random noise from the port")
link.feed_line("ENOW,bad")
check(writes == [] and ingests == [], "junk inbound lines ignored")

print(f"OK — {_n} checks passed")
