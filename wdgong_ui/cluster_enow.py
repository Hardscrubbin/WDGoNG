"""
cluster_enow — ENOW (ESP-NOW) wire codec for the WatchDogsGo-NG cluster bridge.

Architecture: the XIAO-C5 in the uConsole runs a thin ESP-NOW *bridge* (a mode in
projectZero). It carries no fleet logic and no secret — it just pipes frames:

    node --(ESP-NOW)--> XIAO bridge --USB--> "ENOW,<mac>,<rssi>,<ch>,<hex>" --> this codec
    this codec --> "enow_tx <mac> <hex>" --USB--> XIAO bridge --(ESP-NOW)--> node

All the brains live here in Python: auth, the node registry, dedup, channel
assignment, join/release, GPS stamping. This module is the lowest layer of that —
it turns raw ENOW payloads into typed objects and back, and nothing else. It has
no I/O and no dependencies beyond the stdlib, so it is trivially unit-testable.

Wire facts (verified against the Marauder firmware's wdg_cluster.h, 2026-11):
  * little-endian, packed structs; the C sizes are reproduced exactly below.
  * every frame ends with a uint32 auth tag = FNV-1a over all preceding bytes,
    keyed by the 16-byte fleet key (JOIN_REQUEST / JOIN_GRANT use the public
    join secret instead, so adoption can happen before a node has the key).
  * encryption is OFF (peers added with encrypt=false) -> payloads are plaintext
    on air, which is exactly why the bridge can stay a dumb pipe.
  * the node's MAC is the ESP-NOW frame src_addr (the bridge prepends it to the
    line); only CLAIM and JOIN_REQUEST also carry a mac field in the payload,
    because they travel by broadcast where some paths hide the src.
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Optional

# --------------------------------------------------------------------------- #
# Constants (mirror wdg_cluster.h)
# --------------------------------------------------------------------------- #
MAGIC = b"WDG1"
LEGACY_MAGIC = b"ENOW"
JOIN_SECRET = b"wdgwars-fleet-2026"      # public by design (see header)
WDG_KEY_LEN = 16
TEXT_MAX = 200

# message types (EnowMsgType)
CORE_REQUEST = 1
CORE_REPLY = 2
HEARTBEAT = 3
TEXT = 4
ADMIN = 5
CLAIM = 6
OTA_OFFER = 7
OTA_WANT = 8
OTA_DATA = 9
OTA_NAK = 10
OTA_DONE = 11
JOIN_REQUEST = 12
JOIN_GRANT = 13
SENSE_CMD = 14
SENSE_LEVEL = 15
RANGE_CMD = 16
RANGE_RESULT = 17
SENSE_ACK = 18
ILLUM = 19
MESH_SERVE = 20
RELEASE = 21
VITA_FEED = 22

TYPE_NAME = {
    CORE_REQUEST: "CORE_REQUEST", CORE_REPLY: "CORE_REPLY", HEARTBEAT: "HEARTBEAT",
    TEXT: "TEXT", ADMIN: "ADMIN", CLAIM: "CLAIM", JOIN_REQUEST: "JOIN_REQUEST",
    JOIN_GRANT: "JOIN_GRANT", RELEASE: "RELEASE", SENSE_CMD: "SENSE_CMD",
    SENSE_LEVEL: "SENSE_LEVEL", VITA_FEED: "VITA_FEED",
}

# role sentinels smuggled in start_channel_idx / reported in a claim
ROLE_BLE = 0xFE
ROLE_VITA = 0xFD
# claim flags
CLAIM_FROM_CORE = 0x01
CLAIM_VITA_CLIENT = 0x02
CLAIM_VITA_PAIRED = 0x04

# node channel table — MSG_ADMIN carries INDICES into this, so it must match the
# node firmware exactly (wdg_cluster.h ENOW_CHANNELS).
ENOW_CHANNELS = (
    1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14,
    36, 40, 44, 48,
    52, 56, 60, 64,
    100, 112, 116, 120, 124, 128, 132, 136, 140, 144,
    149, 153, 157, 161, 165, 169, 173, 177,
)
NUM_CHANNELS = len(ENOW_CHANNELS)


# --------------------------------------------------------------------------- #
# Auth — FNV-1a 32-bit, keyed (wdg_auth_key / wdg_auth_join)
# --------------------------------------------------------------------------- #
_FNV_OFFSET = 0x811C9DC5
_FNV_PRIME = 0x01000193
_MASK32 = 0xFFFFFFFF


def _fnv1a(data: bytes, h: int) -> int:
    for b in data:
        h ^= b
        h = (h * _FNV_PRIME) & _MASK32
    return h


def wdg_auth_key(buf: bytes, key: bytes) -> int:
    """FNV-1a over the key bytes, then the buffer bytes (never returns 0)."""
    h = _fnv1a(key, _FNV_OFFSET)
    h = _fnv1a(buf, h)
    return h or 1


def wdg_auth_join(buf: bytes) -> int:
    """The migration/join tag: anyone can derive it (public secret)."""
    return wdg_auth_key(buf, JOIN_SECRET)


class FleetKey:
    """Holds the 16-byte fleet key (a secret). Load it from a file OUTSIDE the
    repo; never log or commit the raw bytes."""

    DEFAULT_PATH = os.path.expanduser("~/.wdgo/fleet.key")

    def __init__(self, key: Optional[bytes] = None):
        if key is not None and len(key) != WDG_KEY_LEN:
            raise ValueError(f"fleet key must be {WDG_KEY_LEN} bytes, got {len(key)}")
        self._key = key

    @classmethod
    def _candidate_paths(cls, path: Optional[str]):
        if path:
            yield path
            return
        env = os.environ.get("WDGO_FLEET_KEY")
        if env:
            yield env
        yield os.path.expanduser("~/.wdgo/fleet.key")
        # The game runs as root (sudo) but the key lives in the invoking user's
        # home; expanduser("~") then resolves to /root and misses it. Fall back
        # to SUDO_USER's home (root can read it regardless of file perms).
        sudo_user = os.environ.get("SUDO_USER")
        if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
            try:
                import pwd
                yield os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".wdgo", "fleet.key")
            except (KeyError, ImportError):
                pass

    @classmethod
    def load(cls, path: Optional[str] = None) -> "FleetKey":
        for p in cls._candidate_paths(path):
            try:
                with open(p, "rb") as f:
                    data = f.read()
                if len(data) == WDG_KEY_LEN:
                    return cls(data)
            except OSError:
                continue
        return cls(None)          # no key -> join-secret auth only

    @property
    def present(self) -> bool:
        return self._key is not None

    @property
    def fingerprint(self) -> str:
        import hashlib
        if self._key is None:
            return "<none>"
        return hashlib.sha256(self._key).hexdigest()[:16]

    def auth(self, buf: bytes) -> int:
        return wdg_auth_key(buf, self._key) if self._key is not None else wdg_auth_join(buf)

    def verify(self, payload: bytes) -> bool:
        """True only if a fleet key is loaded AND the frame's tag matches it.

        Fleet frames (CLAIM/TEXT/ADMIN/...) are never accepted on the public join
        secret: without a key, nothing but JOIN_REQUEST may count as authenticated
        (see _check_join_auth)."""
        if self._key is None or len(payload) < 4:
            return False
        return _auth_tail(payload) == wdg_auth_key(payload[:-4], self._key)

    def raw(self) -> Optional[bytes]:
        """The raw key, e.g. to hand a node a JOIN_GRANT. Handle with care."""
        return self._key


def _auth_tail(payload: bytes) -> int:
    return int.from_bytes(payload[-4:], "little")


# --------------------------------------------------------------------------- #
# Struct layouts (little-endian, packed) — sizes asserted against the C header
# --------------------------------------------------------------------------- #
_S_TEXT = struct.Struct("<4sBIH201sI")          # enow_text_msg_t            216 B
_S_ADMIN = struct.Struct("<4sBBBBBBII")          # enow_admin_msg_t            18 B
_S_JOINREQ = struct.Struct("<4sB6sHBI")          # enow_join_req_t             18 B
_S_JOINGRANT = struct.Struct("<4sB16sI")         # enow_join_grant_t           25 B
_S_RELEASE = struct.Struct("<4sB6sI")            # enow_release_t              15 B
# claim: fixed prefix up to `buffered`, then optional tiers, then auth (last 4B)
_S_CLAIM_PREFIX = struct.Struct("<4sB6sBBBHBHHH")  # ...up to buffered         23 B

assert _S_TEXT.size == 216
assert _S_ADMIN.size == 18
assert _S_JOINREQ.size == 18
assert _S_JOINGRANT.size == 25
assert _S_RELEASE.size == 15
assert _S_CLAIM_PREFIX.size == 23

# claim frame-length tiers (ENOW_CLAIM_LEN_*): auth is always the last 4 bytes
CLAIM_LEN_BASE = 27      # prefix(23) + auth(4)
CLAIM_LEN_SHA = 35       # + fwsha(8)
CLAIM_LEN_VER = 37       # + fw_ver(2)
CLAIM_LEN_HEALTH = 40    # + dedup_pct(1) + dropped(2)
CLAIM_LEN_UPTIME = 44    # + uptime_s(4)
CLAIM_LEN_FULL = 45      # + temp_c(1)


# --------------------------------------------------------------------------- #
# Decoded frame objects
# --------------------------------------------------------------------------- #
@dataclass
class Frame:
    type: int
    authed: bool
    src: str = ""            # node MAC, from the bridge line (src_addr)
    raw: bytes = b""

    @property
    def type_name(self) -> str:
        return TYPE_NAME.get(self.type, f"0x{self.type:02x}")


@dataclass
class TextFrame(Frame):
    counter: int = 0
    text: str = ""           # CORE_REQUEST/HEARTBEAT: empty; TEXT: the CSV record


@dataclass
class ClaimFrame(Frame):
    mac: str = ""
    start_idx: int = 0
    end_idx: int = 0
    assignment_version: int = 0
    records: int = 0
    flags: int = 0
    hops_per_min: int = 0
    rec_per_min: int = 0
    buffered: int = 0
    fwsha: bytes = b""
    fw_ver: int = 0
    dedup_pct: int = 0
    dropped: int = 0
    uptime_s: int = 0
    temp_c: Optional[int] = None     # None when the node is too old to report it
    # convenience
    @property
    def ble_role(self) -> bool:
        return self.start_idx == ROLE_BLE or self.end_idx == ROLE_BLE
    @property
    def vita_role(self) -> bool:
        return self.start_idx == ROLE_VITA or self.end_idx == ROLE_VITA


@dataclass
class JoinReqFrame(Frame):
    mac: str = ""
    fw_ver: int = 0
    rekey: bool = False


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def mac_to_str(b: bytes) -> str:
    return ":".join(f"{x:02x}" for x in b)


def mac_to_bytes(s: str) -> bytes:
    parts = s.replace("-", ":").split(":")
    if len(parts) != 6:
        raise ValueError(f"bad MAC: {s!r}")
    return bytes(int(p, 16) for p in parts)


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def _check_join_auth(payload: bytes) -> bool:
    return _auth_tail(payload) == wdg_auth_join(payload[:-4])


def decode(payload: bytes, key: FleetKey, src: str = "") -> Optional[Frame]:
    """Turn a raw ENOW payload into a typed Frame. Returns None if the frame is
    too short or does not carry our magic. `authed` records whether the tag
    checked out; the caller decides whether to trust an unauthenticated frame
    (it never should, except to observe)."""
    if len(payload) < 9 or payload[:4] != MAGIC:
        return None
    mtype = payload[4]

    if mtype in (CORE_REQUEST, CORE_REPLY, HEARTBEAT, TEXT):
        if len(payload) < _S_TEXT.size:
            return None
        _, _t, counter, tlen, text, _auth = _S_TEXT.unpack(payload[:_S_TEXT.size])
        tlen = min(tlen, TEXT_MAX)
        txt = text[:tlen].decode("utf-8", "replace")
        return TextFrame(
            type=mtype, authed=key.verify(payload),
            src=src, raw=payload, counter=counter, text=txt,
        )

    if mtype == CLAIM:
        return _decode_claim(payload, key, src)

    if mtype == JOIN_REQUEST:
        if len(payload) < _S_JOINREQ.size:
            return None
        _, _t, mac, fw_ver, rekey, _auth = _S_JOINREQ.unpack(payload[:_S_JOINREQ.size])
        return JoinReqFrame(
            type=mtype, authed=_check_join_auth(payload[:_S_JOINREQ.size]),
            src=src or mac_to_str(mac), raw=payload,
            mac=mac_to_str(mac), fw_ver=fw_ver, rekey=bool(rekey),
        )

    # types we recognise but do not decode in v1 (OTA/SENSE/RANGE/VITA/…)
    return Frame(type=mtype, authed=key.verify(payload),
                 src=src, raw=payload)


def _decode_claim(payload: bytes, key: FleetKey, src: str) -> Optional[ClaimFrame]:
    n = len(payload)
    if n < CLAIM_LEN_BASE:
        return None
    (_, _t, mac, start_idx, end_idx, aver, records, flags,
     hops, recmin, buffered) = _S_CLAIM_PREFIX.unpack(payload[:_S_CLAIM_PREFIX.size])
    cf = ClaimFrame(
        type=CLAIM, authed=key.verify(payload),
        src=src or mac_to_str(mac), raw=payload,
        mac=mac_to_str(mac), start_idx=start_idx, end_idx=end_idx,
        assignment_version=aver, records=records, flags=flags,
        hops_per_min=hops, rec_per_min=recmin, buffered=buffered,
    )
    # optional tiers, in order; read a field only if the frame is long enough.
    off = _S_CLAIM_PREFIX.size
    if n >= CLAIM_LEN_SHA:
        cf.fwsha = payload[off:off + 8]; off += 8
    if n >= CLAIM_LEN_VER:
        cf.fw_ver = struct.unpack_from("<H", payload, off)[0]; off += 2
    if n >= CLAIM_LEN_HEALTH:
        cf.dedup_pct = payload[off]; off += 1
        cf.dropped = struct.unpack_from("<H", payload, off)[0]; off += 2
    if n >= CLAIM_LEN_UPTIME:
        cf.uptime_s = struct.unpack_from("<I", payload, off)[0]; off += 4
    if n >= CLAIM_LEN_FULL:
        cf.temp_c = struct.unpack_from("<b", payload, off)[0]; off += 1
    return cf


# --------------------------------------------------------------------------- #
# Encoding (core -> node)
# --------------------------------------------------------------------------- #
def _sign(body: bytes, key: FleetKey) -> bytes:
    return body + struct.pack("<I", key.auth(body))


def _sign_join(body: bytes) -> bytes:
    return body + struct.pack("<I", wdg_auth_join(body))


def build_core_reply(key: FleetKey) -> bytes:
    """type 2: a MINIMAL authenticated reply (9 bytes: magic+type+auth).

    The node reads the core's MAC off the ESP-NOW header, not the payload, and
    accepts any authentic type-2 frame of >= 9 bytes (wdg_node authentic()), so
    there is no reason to ship a 216 B enow_text_msg_t: that floods the uConsole's
    USB-serial-JTAG link to the bridge, the replies get dropped/garbled under load,
    and the node never latches (NO-CORE). Keeping it at the 9 B floor cuts the per
    reply air/serial cost ~24x so CORE_REPLY reliably lands inside the node's short
    control-channel report window."""
    body = MAGIC + bytes([CORE_REPLY])          # 5 bytes: magic + type
    return _sign(body, key)                      # + 4-byte auth = 9 bytes


def build_admin(key: FleetKey, assignment_version: int, node_index: int,
                node_count: int, start_channel_idx: int, end_channel_idx: int,
                core_session: int) -> bytes:
    """type 5: assign a channel slice (or a role sentinel) to a node."""
    body = _S_ADMIN.pack(MAGIC, ADMIN, assignment_version & 0xFF,
                         node_index & 0xFF, node_count & 0xFF,
                         start_channel_idx & 0xFF, end_channel_idx & 0xFF,
                         core_session & _MASK32, 0)[:-4]
    return _sign(body, key)


def build_join_grant(fleet_key: FleetKey) -> bytes:
    """type 13: hand the fleet key to a node we just adopted. Signed with the
    JOIN secret (the node has no fleet key yet to verify against)."""
    raw = fleet_key.raw()
    if raw is None:
        raise ValueError("no fleet key to grant")
    body = _S_JOINGRANT.pack(MAGIC, JOIN_GRANT, raw, 0)[:-4]
    return _sign_join(body)


def build_release(key: FleetKey, target_mac: str) -> bytes:
    """type 21: release a node from the fleet. Signed with the CURRENT fleet key
    (that is what proves we own it)."""
    body = _S_RELEASE.pack(MAGIC, RELEASE, mac_to_bytes(target_mac), 0)[:-4]
    return _sign(body, key)


# --------------------------------------------------------------------------- #
# Bridge line protocol (over USB serial)
# --------------------------------------------------------------------------- #
@dataclass
class BridgeFrame:
    mac: str
    rssi: int
    channel: int
    payload: bytes


def parse_bridge_line(line: str) -> Optional[BridgeFrame]:
    """Parse one 'ENOW,<mac>,<rssi>,<ch>,<hex>' line from the bridge firmware.

    The firmware's console prompt can glue onto the first frame after
    start_cluster_bridge ('> ENOW,...'), so a leading prompt is stripped."""
    line = line.strip().lstrip(">").strip()
    if not line.startswith("ENOW,"):
        return None
    parts = line.split(",", 4)
    if len(parts) != 5:
        return None
    _, mac, rssi, ch, hexs = parts
    try:
        return BridgeFrame(mac=mac.lower(), rssi=int(rssi), channel=int(ch),
                           payload=bytes.fromhex(hexs.strip()))
    except (ValueError, TypeError):
        return None


def tx_line(mac: str, payload: bytes) -> str:
    """Format a downlink command for the bridge firmware."""
    return f"enow_tx {mac} {payload.hex()}"


# --------------------------------------------------------------------------- #
# Fleet-key share line: WDGFLEET1:<32 hex>:<label>  (same format as Marauder
# `fleetkey show` / the phone app). One fleet, many cores, ONE core at a time.
# --------------------------------------------------------------------------- #
FLEET_LINE_PREFIX = "WDGFLEET1:"


def fleet_fingerprint(raw: bytes) -> str:
    """SHA256 of the RAW 16 key bytes (not of the hex string), first 16 hex."""
    import hashlib
    return hashlib.sha256(raw).hexdigest()[:16]


def fleet_line(raw: bytes, label: str = "") -> str:
    import re
    label = re.sub(r"[^A-Za-z0-9 _-]", "", label or "")[:24]
    return "%s%s:%s" % (FLEET_LINE_PREFIX, raw.hex(), label)


def parse_fleet_line(line: str):
    """-> (raw16, label, "") on success or (None, "", reason)."""
    import re
    s = (line or "").strip()
    if not s.startswith(FLEET_LINE_PREFIX):
        return None, "", "not a WDGFLEET1 line"
    body = s[len(FLEET_LINE_PREFIX):]
    hexpart, _, label = body.partition(":")
    hexpart = hexpart.strip().lower()
    if len(hexpart) != 2 * WDG_KEY_LEN or not re.fullmatch(r"[0-9a-f]+", hexpart):
        return None, "", "key must be 32 hex chars"
    if label and not re.fullmatch(r"[A-Za-z0-9 _-]{0,24}", label):
        return None, "", "bad label"
    return bytes.fromhex(hexpart), label, ""


def _fleet_key_write_path() -> str:
    """Where import writes: the same file FleetKey.load() reads first. An explicit
    WDGO_FLEET_KEY wins even when that file does not exist yet."""
    env = os.environ.get("WDGO_FLEET_KEY")
    if env:
        return env
    for p in FleetKey._candidate_paths(None):
        if os.path.exists(p):
            return p
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            return os.path.join(pwd.getpwnam(sudo_user).pw_dir, ".wdgo", "fleet.key")
        except (KeyError, ImportError):
            pass
    return FleetKey.DEFAULT_PATH


def _target_owner():
    """(uid, gid) the key file should belong to: SUDO_USER when running as root
    via sudo, else None (keep the caller's own identity)."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and hasattr(os, "geteuid") and os.geteuid() == 0:
        try:
            import pwd
            pw = pwd.getpwnam(sudo_user)
            return pw.pw_uid, pw.pw_gid
        except (KeyError, ImportError):
            return None
    return None


def _open_key_dir(d: str, owner):
    """Open the key directory WITHOUT following a symlink and return its fd.

    The directory lives in the invoking user's home while this process may run
    as root, so the user controls the path: every later operation goes through
    this fd (openat/renameat), never through the path again. Refuses a symlinked
    directory and a directory owned by anyone but root or the target user."""
    import stat as _stat
    if os.path.islink(d):
        raise OSError("refusing symlinked key directory %s" % d)
    created = False
    if not os.path.isdir(d):
        parent = os.path.dirname(d) or "."
        os.makedirs(parent, exist_ok=True)
        try:
            os.mkdir(d, 0o700)
            created = True
        except FileExistsError:
            pass
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    dfd = os.open(d, flags)
    try:
        st = os.fstat(dfd)
        if not _stat.S_ISDIR(st.st_mode):
            raise OSError("key path parent is not a directory")
        allowed = {os.geteuid() if hasattr(os, "geteuid") else st.st_uid, 0}
        if owner is not None:
            allowed.add(owner[0])
        if st.st_uid not in allowed:
            raise OSError("key directory %s is owned by another user" % d)
        if created:                            # only touch a directory WE made
            if owner is not None:
                os.fchown(dfd, owner[0], owner[1])
            os.fchmod(dfd, 0o700)
        return dfd
    except Exception:
        os.close(dfd)
        raise


def _read_existing_key(dfd: int, name: str) -> Optional[bytes]:
    """The current key file's bytes, or None if there is none. Never follows a
    symlink and never blocks on a FIFO; anything but a regular file is refused."""
    import stat as _stat
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(name, flags, dir_fd=dfd)
    except FileNotFoundError:
        return None
    except OSError as exc:                     # ELOOP: the key path is a symlink
        raise OSError("refusing key path (symlink or unreadable): %s" % exc.strerror)
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("refusing key path: not a regular file")
        return os.read(fd, 4096)
    finally:
        os.close(fd)


def _write_new_file(dfd: int, name: str, data: bytes, owner) -> None:
    """Create `name` in the directory fd as a NEW 0600 file (O_EXCL + O_NOFOLLOW,
    so a planted file or symlink makes this fail instead of redirecting it)."""
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    fd = os.open(name, flags, 0o600, dir_fd=dfd)
    try:
        os.fchmod(fd, 0o600)
        if owner is not None:
            os.fchown(fd, owner[0], owner[1])
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def import_fleet_line(line: str, force: bool = False):
    """Install the fleet key from a WDGFLEET1 line. -> (ok, message).

    Same rules as Marauder `fleetkey import`: an identical key is a no-op; a
    DIFFERENT existing key is replaced only with force=True, and the old key is
    copied aside first (fleet.key.bak-<time>). The caller must refuse while the
    cluster is running (two brains on one fleet fight over the nodes).

    The game runs as root but writes into the invoking user's home, so the
    write is symlink-safe: the directory is opened once with O_NOFOLLOW, the
    backup and the new key are created there as fresh 0600 files (O_EXCL), owned
    by SUDO_USER, and swapped in with a rename inside that same directory."""
    import time as _t
    raw, label, err = parse_fleet_line(line)
    if raw is None:
        return False, err
    path = os.path.abspath(_fleet_key_write_path())
    d, name = os.path.split(path)
    owner = _target_owner()
    try:
        dfd = _open_key_dir(d, owner)
    except OSError as exc:
        return False, "cannot use key directory: %s" % exc
    try:
        try:
            old = _read_existing_key(dfd, name)
        except OSError as exc:
            return False, str(exc)
        new_fp = fleet_fingerprint(raw)
        if old == raw:
            return True, "already this fleet key (fp %s)" % new_fp
        if old and len(old) == WDG_KEY_LEN and not force:
            return False, "a different key is here (fp %s) - use force" % fleet_fingerprint(old)
        try:
            if old:
                stamp = _t.strftime("%Y%m%d-%H%M%S")
                for i in range(100):
                    bak = "%s.bak-%s%s" % (name, stamp, ("-%d" % i) if i else "")
                    try:
                        _write_new_file(dfd, bak, old, owner)
                        break
                    except FileExistsError:
                        continue
                else:
                    return False, "could not create a backup of the old key"
            tmp = "%s.tmp-%d-%s" % (name, os.getpid(), os.urandom(4).hex())
            _write_new_file(dfd, tmp, raw, owner)
            try:
                os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
            except OSError:
                try:
                    os.unlink(tmp, dir_fd=dfd)
                except OSError:
                    pass
                raise
        except OSError as exc:
            return False, "could not write the fleet key: %s" % exc
    finally:
        os.close(dfd)
    return True, "imported fleet key fp %s%s" % (new_fp, (" from " + label) if label else "")


# --------------------------------------------------------------------------- #
# Log redaction: a JOIN_GRANT carries the raw fleet key, so no bridge line may
# reach a screen or a log file with its payload intact.
# --------------------------------------------------------------------------- #
import re as _re

_TX_RE = _re.compile(r"(enow_tx\s+[0-9A-Fa-f:]{17}\s+)([0-9A-Fa-f]+)")
_RX_RE = _re.compile(r"(ENOW,[0-9A-Fa-f:]{17},-?\d+,\d+,)([0-9A-Fa-f]+)")


def _summary(hexs: str) -> str:
    t = hexs[8:10] if len(hexs) >= 10 else "??"
    return "<type %s, %d B>" % (t, len(hexs) // 2)


def redact_line(line: str) -> str:
    """Mask frame payloads in bridge traffic before it is shown or logged: every
    `enow_tx` payload (echoed by the firmware console too) and every received
    JOIN_GRANT (type 0x0d). Only the frame type and length are kept."""
    if not line or ("enow_tx" not in line and "ENOW," not in line):
        return line
    line = _TX_RE.sub(lambda m: m.group(1) + _summary(m.group(2)), line)
    return _RX_RE.sub(lambda m: m.group(1) + (_summary(m.group(2))
                      if m.group(2)[8:10].lower() == "%02x" % JOIN_GRANT else m.group(2)), line)
