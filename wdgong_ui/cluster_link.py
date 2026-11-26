"""
cluster_link — the serial glue between the ENOW bridge firmware and the brain.

    bridge USB  <--->  ClusterLink  <--->  ClusterBrain
                           |  feeds brain.lines (CNODE/CWD/CJOIN) to `ingest`
                           |  writes brain.tx as 'enow_tx <mac> <hex>' to the port
                           |  ticks the brain so the channel split is (re)advertised

The testable core (ClusterLink) has no I/O: you hand it a `write(str)` sink and
an `ingest(str)` callback, then drive it with inbound lines and periodic polls.
`serve()` is the thin runtime wrapper that binds those to a real serial port
(pyserial, imported lazily so this module loads on a host without it).
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from cluster_brain import ClusterBrain, BrainOut
from cluster_enow import tx_line

START_CMD = "start_cluster_bridge"
STOP_CMD = "stop"
DEFAULT_BAUD = 115200
TICK_INTERVAL_S = 1.0
# Longest bridge line we accept. The largest frame is a 216-byte TEXT, i.e.
# "ENOW,<mac>,<rssi>,<ch>," + 432 hex chars (~470 B); anything far beyond that is
# a lost separator or garbage, and is dropped up to the next newline.
MAX_LINE_BYTES = 2048


class ClusterLink:
    """I/O-free driver: route inbound bridge lines through the brain and push its
    output to `write` (serial) and `ingest` (the UI's cluster-line consumer)."""

    def __init__(self, brain: ClusterBrain,
                 write: Callable[[str], None],
                 ingest: Callable[[str], None],
                 now: Callable[[], float] = time.monotonic,
                 tick_interval: float = TICK_INTERVAL_S):
        self.brain = brain
        # The serial reader thread (feed_line/poll) and the UI thread (accept,
        # release, pending_joins) both drive the brain: serialize every call.
        self._lock = threading.RLock()
        self._write = write
        self._ingest = ingest
        self._now = now
        self._tick_interval = tick_interval
        self._last_tick: Optional[float] = None    # None -> tick on the first poll
        self.started = False

    def _emit(self, out: BrainOut) -> None:
        for line in out.lines:
            self._ingest(line)
        for mac, payload in out.tx:
            self._write(tx_line(mac, payload))

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        self._write(START_CMD)
        self.started = True

    def stop(self) -> None:
        self._write(STOP_CMD)
        self.started = False

    # -- pumps --------------------------------------------------------------
    def feed_line(self, line: str) -> None:
        """One inbound serial line from the bridge."""
        if not line:
            return
        with self._lock:
            self._emit(self.brain.handle_line(line))

    def poll(self, now: Optional[float] = None) -> None:
        """Call often; (re)advertises the channel split on its own schedule."""
        now = self._now() if now is None else now
        # Monotonic clock, so an RTC step back cannot stall the schedule; a value
        # that still goes backwards (a caller-supplied clock) re-arms instead.
        if (self._last_tick is not None and 0 <= now - self._last_tick
                and now - self._last_tick < self._tick_interval):
            return
        self._last_tick = now
        with self._lock:
            self._emit(self.brain.tick(now))

    def accept(self, mac: str) -> bool:
        """Operator pressed [A] on a pending joiner.

        Returns True when the brain granted it and the JOIN_GRANT was handed to
        `write`, False when the brain refused (no fresh JOIN_REQUEST from that MAC:
        expired, never heard, already consumed, or no fleet key). An exception from
        `write` propagates, so the caller can report that the grant was not sent."""
        with self._lock:
            out = self.brain.accept(mac)
            self._emit(out)
        return bool(out.tx)

    def pending_joins(self) -> list:
        """MACs with a live (not yet expired) JOIN_REQUEST as the brain sees them
        right now: the source of truth for the operator's join list in bridge mode.
        Read-only snapshot (the serial thread may be updating the brain)."""
        with self._lock:
            if hasattr(self.brain, "live_joins"):
                return self.brain.live_joins()
            return list(self.brain.pending_joins)

    def release(self, mac: str) -> None:
        """Operator released one node from the fleet (signed RELEASE)."""
        with self._lock:
            self._emit(self.brain.release(mac))

    def release_all(self) -> None:
        """Operator released the whole fleet."""
        with self._lock:
            self._emit(self.brain.release_all())


# --------------------------------------------------------------------------- #
# Runtime wrapper around a real serial port
# --------------------------------------------------------------------------- #
def serve(brain: ClusterBrain, port: str, ingest: Callable[[str], None],
          stop_event, baud: int = DEFAULT_BAUD,
          log: Optional[Callable[[str], None]] = None) -> None:
    """Open the bridge port, drive a ClusterLink until stop_event is set.

    Runs on the uConsole (needs pyserial, same as serial_manager). Reads with a
    short timeout so the brain still ticks when the line is quiet."""
    import serial  # lazy: only needed at runtime on the device

    def _log(m):
        if log:
            log(m)

    ser = serial.Serial(port, baud, timeout=0.2)
    link = ClusterLink(brain, write=lambda s: ser.write((s + "\n").encode()),
                       ingest=ingest)
    try:
        link.start()
        _log("[CLUSTER] bridge up on %s @ %d" % (port, baud))
        buf = b""
        discarding = False                  # inside an over-long line: drop to next \n
        while not stop_event.is_set():
            try:
                chunk = ser.read(256)
            except Exception as e:                       # noqa: BLE001
                _log("[CLUSTER] serial read error: %s" % e)
                break
            if chunk:
                buf += chunk
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    if discarding:
                        discarding = False      # end of the dropped line
                        continue
                    if len(raw) > MAX_LINE_BYTES:
                        continue
                    link.feed_line(raw.decode("utf-8", "replace").strip())
                if len(buf) > MAX_LINE_BYTES:   # no separator in sight: bound memory
                    buf = b""
                    if not discarding:
                        discarding = True
                        _log("[CLUSTER] over-long bridge line dropped")
            link.poll()
    finally:
        try:
            link.stop()
        except Exception:                                # noqa: BLE001
            pass
        try:
            ser.close()
        except Exception:                                # noqa: BLE001
            pass
        _log("[CLUSTER] bridge down")
