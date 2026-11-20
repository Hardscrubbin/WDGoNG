"""Unit tests for cluster_enow — run: python3 wdgong_ui/test_cluster_enow.py

No hardware, no pytest. Validates the ENOW codec against the C header's exact
byte layouts and against the standard FNV-1a vectors (which proves the auth tag
matches the firmware's wdg_auth without needing a live capture).
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cluster_enow as E

_n = 0
def check(cond, msg):
    global _n
    assert cond, "FAIL: " + msg
    _n += 1


# --- FNV-1a known-answer (standard 32-bit vectors) --------------------------
# wdg_auth_key hashes key bytes then buffer bytes through the same FNV-1a, so
# wdg_auth_key(buf, b"") == FNV-1a(buf) and wdg_auth_key(b"bar", b"foo") ==
# FNV-1a("foobar"). These are the canonical test vectors.
check(E.wdg_auth_key(b"", b"") == 0x811C9DC5, "FNV empty")
check(E.wdg_auth_key(b"a", b"") == 0xE40C292C, "FNV 'a'")
check(E.wdg_auth_key(b"foobar", b"") == 0xBF9CF968, "FNV 'foobar'")
check(E.wdg_auth_key(b"bar", b"foo") == 0xBF9CF968, "FNV keyed concat == 'foobar'")
check(E.wdg_auth_key(b"", b"foobar") == 0xBF9CF968, "FNV key-only == 'foobar'")
# never zero
check(E.wdg_auth_key(bytes(4), bytes(16)) != 0, "auth never 0")

# join secret is the documented public string
check(E.JOIN_SECRET == b"wdgwars-fleet-2026", "join secret")
check(len(E.JOIN_SECRET) == 18, "join secret length (no NUL)")

# --- struct sizes match the C header ---------------------------------------
check(E._S_TEXT.size == 216, "text 216B")
check(E._S_ADMIN.size == 18, "admin 18B")
check(E._S_JOINREQ.size == 18, "joinreq 18B")
check(E._S_JOINGRANT.size == 25, "joingrant 25B")
check(E._S_RELEASE.size == 15, "release 15B")
check(E.NUM_CHANNELS == 40, "40 channels")

# a throwaway test key (NOT the real fleet key)
KEY = E.FleetKey(bytes(range(16)))
NOKEY = E.FleetKey(None)
check(KEY.present and not NOKEY.present, "key presence")
check(len(KEY.fingerprint) == 16 and NOKEY.fingerprint == "<none>", "fingerprint")

# --- ADMIN round-trip -------------------------------------------------------
adm = E.build_admin(KEY, assignment_version=3, node_index=2, node_count=5,
                    start_channel_idx=0, end_channel_idx=13, core_session=0xDEADBEEF)
check(len(adm) == 18, "admin frame 18B")
f = E.decode(adm, KEY, src="aa:bb:cc:dd:ee:ff")
check(f is not None and f.type == E.ADMIN, "admin decodes")
check(f.authed, "admin authed with correct key")
# re-unpack to confirm the fields landed
_, t, aver, nidx, ncnt, s0, s1, sess, _a = E._S_ADMIN.unpack(adm)
check((t, aver, nidx, ncnt, s0, s1, sess) == (5, 3, 2, 5, 0, 13, 0xDEADBEEF), "admin fields")
# wrong key -> not authed
check(not E.decode(adm, E.FleetKey(bytes(16)), src="x").authed, "admin fails wrong key")
# tamper -> not authed
bad = bytearray(adm); bad[6] ^= 0x01
check(not E.decode(bytes(bad), KEY).authed, "admin tamper detected")

# --- CORE_REPLY round-trip --------------------------------------------------
rep = E.build_core_reply(KEY)
check(len(rep) == 9, "core_reply 9B (minimal)")          # magic+type+auth, >= node's 9B floor
check(rep[4] == E.CORE_REPLY, "core_reply type byte")
check(KEY.auth(rep[:-4]) == struct.unpack("<I", rep[-4:])[0], "core_reply auth")

# --- RELEASE round-trip -----------------------------------------------------
rel = E.build_release(KEY, "02:00:00:00:00:d4")
check(len(rel) == 15, "release 15B")
fr = E.decode(rel, KEY)
check(fr.type == E.RELEASE and fr.authed, "release authed")
_, _t, target, _a = E._S_RELEASE.unpack(rel)
check(E.mac_to_str(target) == "02:00:00:00:00:d4", "release target mac")

# --- JOIN_GRANT (signed with the join secret, carries the fleet key) --------
grant = E.build_join_grant(KEY)
check(len(grant) == 25, "joingrant 25B")
# verify its tag is the JOIN tag, not the fleet-key tag
check(E._auth_tail(grant) == E.wdg_auth_join(grant[:-4]), "joingrant uses join auth")
_, _t, gkey, _a = E._S_JOINGRANT.unpack(grant)
check(gkey == bytes(range(16)), "joingrant carries the key")

# --- JOIN_REQUEST decode (node -> core, join auth) --------------------------
jr_body = E._S_JOINREQ.pack(E.MAGIC, E.JOIN_REQUEST, E.mac_to_bytes("de:ad:be:ef:00:01"),
                            29, 1, 0)[:-4]
jr = jr_body + struct.pack("<I", E.wdg_auth_join(jr_body))
jf = E.decode(jr, NOKEY)
check(jf is not None and jf.type == E.JOIN_REQUEST, "joinreq decodes")
check(jf.authed, "joinreq authed via join secret")
check(jf.mac == "de:ad:be:ef:00:01" and jf.fw_ver == 29 and jf.rekey is True, "joinreq fields")

# --- CLAIM tiered parsing ---------------------------------------------------
def make_claim(tier_len, key, *, start=0, end=13, temp=None):
    """Synthesize a claim of the given frame length with a valid tag."""
    prefix = E._S_CLAIM_PREFIX.pack(
        E.MAGIC, E.CLAIM, E.mac_to_bytes("02:00:00:00:00:bc"),
        start, end, 2, 123, E.CLAIM_FROM_CORE, 40, 60, 7)
    body = bytearray(prefix)
    if tier_len >= E.CLAIM_LEN_SHA:
        body += bytes(range(8))                       # fwsha
    if tier_len >= E.CLAIM_LEN_VER:
        body += struct.pack("<H", 31)                 # fw_ver
    if tier_len >= E.CLAIM_LEN_HEALTH:
        body += struct.pack("<BH", 55, 9)             # dedup_pct, dropped
    if tier_len >= E.CLAIM_LEN_UPTIME:
        body += struct.pack("<I", 123456)             # uptime_s
    if tier_len >= E.CLAIM_LEN_FULL:
        body += struct.pack("<b", temp if temp is not None else 42)
    body += struct.pack("<I", key.auth(bytes(body)))
    return bytes(body)

for tier in (E.CLAIM_LEN_BASE, E.CLAIM_LEN_SHA, E.CLAIM_LEN_VER,
             E.CLAIM_LEN_HEALTH, E.CLAIM_LEN_UPTIME, E.CLAIM_LEN_FULL):
    c = make_claim(tier, KEY)
    cf = E.decode(c, KEY)
    check(cf is not None and cf.type == E.CLAIM and cf.authed, f"claim@{tier} authed")
    check(cf.mac == "02:00:00:00:00:bc" and cf.records == 123 and cf.rec_per_min == 60,
          f"claim@{tier} base fields")
    check(cf.flags == E.CLAIM_FROM_CORE, f"claim@{tier} flags")
    # temp only present on the full frame
    check((cf.temp_c == 42) == (tier >= E.CLAIM_LEN_FULL), f"claim@{tier} temp presence")
    check((cf.fw_ver == 31) == (tier >= E.CLAIM_LEN_VER), f"claim@{tier} fw_ver presence")
    check((cf.dedup_pct == 55) == (tier >= E.CLAIM_LEN_HEALTH), f"claim@{tier} health presence")

# BLE role sentinel in the slice fields
cble = make_claim(E.CLAIM_LEN_FULL, KEY, start=E.ROLE_BLE, end=E.ROLE_BLE)
check(E.decode(cble, KEY).ble_role, "claim BLE role")

# --- bridge line protocol ---------------------------------------------------
line = f"ENOW,02:00:00:00:00:d4,-67,6,{adm.hex()}"
bf = E.parse_bridge_line(line)
check(bf is not None and bf.mac == "02:00:00:00:00:d4" and bf.rssi == -67 and bf.channel == 6,
      "bridge line parse")
check(bf.payload == adm, "bridge line payload")
check(E.decode(bf.payload, KEY, src=bf.mac).type == E.ADMIN, "decode from bridge line")
check(E.parse_bridge_line("garbage") is None, "bridge line rejects junk")
check(E.parse_bridge_line("ENOW,aa,bb") is None, "bridge line rejects short")
check(E.tx_line("aa:bb:cc:dd:ee:ff", b"\x01\x02") == "enow_tx aa:bb:cc:dd:ee:ff 0102",
      "tx_line format")

# --- junk / foreign frames --------------------------------------------------
check(E.decode(b"ENOWxxxxx", KEY) is None, "wrong magic rejected (too short/foreign)")
check(E.decode(b"", KEY) is None, "empty rejected")
check(E.decode(b"WDG1" + bytes(4), KEY) is None, "runt rejected")

print(f"OK — {_n} checks passed")
