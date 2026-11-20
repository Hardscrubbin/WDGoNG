"""EXTRAS sandbox wire protocol — shared by the host (root game process) and the
sandboxed child. Pure stdlib, no raylib.

Transport: one AF_UNIX SOCK_STREAM socketpair. Every message is a 4-byte
big-endian length followed by that many bytes of UTF-8 JSON encoding ONE object
with a string field "t" (the message type). Nothing else is ever sent.

host -> child
    hello   first message: what to load and the limits to apply
    frame   {n, dt, view:[w,h], down:[key..], pressed:[key..], chars:[..], gps, now}
    reply   {id, ok, v | err}      answer to a child "call"
    exit    leave now (the child runs on_exit, answers "exited", and quits)

child -> host
    ready   {iso:{level, why}}     isolation is established; extra code not yet run
    frame   {n, ops:[..], logs:[[level, text]..]}
    call    {id, m, a:[..]}        broker request (storage / loot)
    fault   {err}                  the extra raised; it will not run again
    exited  the extra finished on_exit

Draw ops are compact JSON arrays; this primitive set is FROZEN for PROTO 1:
    ["t", text, x, y, size, color, bold]
    ["r", x, y, w, h, color, fill, alpha]
    ["l", x1, y1, x2, y2, color, thick]
    ["c", x, y, r, color]
Added with EXTRAS api 2 (host and child always ship together):
    ["i", name, x, y, scale, alpha]      draw an image the child has declared
and a child "frame" may carry  imgs: {name: [w, h, y0, base64(raw RGBA rows)]}  - each of
the extra's images once, decoded IN THE CHILD and sent as strips of whole rows, in
order from row 0. The host only checks sizes; it never parses an image file.

The host treats every byte from the child as hostile: sizes are capped here and
every op / call argument is re-validated on the host before use.
"""
import json
import struct

PROTO = 1
MAX_MSG = 512 * 1024          # one framed message, either direction
MAX_OPS = 4000                # draw ops per frame
MAX_TEXT = 256                # characters in one text op
MAX_LOGS = 8                  # log lines per frame
MAX_LOG_LEN = 200
MAX_CHARS = 64                # typed characters delivered per frame
IMG_STRIP_BYTES = 96 * 1024   # raw pixel bytes of one image strip (one strip per frame)

KEY_NAMES = (["up", "down", "left", "right", "enter", "space", "tab", "backspace",
              "minus", "equal", "comma", "period", "slash",
              "bracket_left", "bracket_right"]
             + [chr(c) for c in range(ord("a"), ord("z") + 1)]
             + [str(d) for d in range(10)])


class ProtocolError(Exception):
    pass


def pack(obj) -> bytes:
    try:
        data = json.dumps(obj, separators=(",", ":"), ensure_ascii=True,
                          allow_nan=False).encode("ascii")
    except (TypeError, ValueError) as exc:
        raise ProtocolError("message is not plain JSON data (%s)" % exc)
    if len(data) > MAX_MSG:
        raise ProtocolError("message too large (%d bytes)" % len(data))
    return struct.pack(">I", len(data)) + data


class Framer:
    """Incremental decoder: feed() raw bytes, get complete message dicts back."""
    def __init__(self):
        self._buf = bytearray()

    def feed(self, data) -> list:
        self._buf += data
        out = []
        while True:
            if len(self._buf) < 4:
                break
            (n,) = struct.unpack(">I", bytes(self._buf[:4]))
            if n > MAX_MSG:
                raise ProtocolError("oversized frame (%d bytes)" % n)
            if len(self._buf) < 4 + n:
                break
            raw = bytes(self._buf[4:4 + n])
            del self._buf[:4 + n]
            try:
                msg = json.loads(raw.decode("utf-8"))
            except Exception:
                raise ProtocolError("frame is not valid JSON")
            if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
                raise ProtocolError("frame is not a typed object")
            out.append(msg)
        return out
