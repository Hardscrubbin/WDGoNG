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

import time
from typing import Callable, Optional

from cluster_brain import ClusterBrain, BrainOut
from cluster_enow import tx_line

START_CMD = "start_cluster_bridge"
STOP_CMD = "stop"
DEFAULT_BAUD = 115200
TICK_INTERVAL_S = 1.0


class ClusterLink:
    """I/O-free driver: route inbound bridge lines through the brain and push its
    output to `write` (serial) and `ingest` (the UI's cluster-line consumer)."""

    def __init__(self, brain: ClusterBrain,
                 write: Callable[[str], None],
                 ingest: Callable[[str], None],
                 now: Callable[[], float] = time.time,
                 tick_interval: float = TICK_INTERVAL_S):
        self.brain = brain
        self._write = write
        self._ingest = ingest
        self._now = now
        self._tick_interval = tick_interval
        self._last_tick = 0.0
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
        self._emit(self.brain.handle_line(line))

    def poll(self, now: Optional[float] = None) -> None:
        """Call often; (re)advertises the channel split on its own schedule."""
        now = self._now() if now is None else now
        if now - self._last_tick < self._tick_interval:
            return
        self._last_tick = now
        self._emit(self.brain.tick(now))

    def accept(self, mac: str) -> None:
        """Operator pressed [A] on a pending joiner."""
        self._emit(self.brain.accept(mac))


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
                    link.feed_line(raw.decode("utf-8", "replace").strip())
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
