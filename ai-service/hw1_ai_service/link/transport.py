"""SerialTransport: the byte mover.

One dedicated reader thread drains the serial port and assembles newline-
framed text lines; complete events are handed to the asyncio loop through a
queue. THE HARD RULE (plan §4/audit): this thread never blocks on anything
but the port itself — no inference, no JSON, no pipeline calls. The wire
has no flow control (the carrier routes no RTS/CTS); if nobody drains the
kernel tty flip buffers for ~7s at full burst rate — 640KB of
TTYB_DEFAULT_MEM_LIMIT, not userspace-tunable — bytes vanish silently.

TX is small (commands <= 2047B) and goes through a single write path; the
Session layer's command lock provides the single-writer discipline.

Garbage is normal, not exceptional: every ESP32 reset sprays the ROM boot
banner at 115200 into our 921600 reader (decodes as trash), and a mid-line
connect starts with a partial line. Bad decode -> a "garbage" event (the
session uses a burst of them as a reboot hint); alignment restores itself
at the next newline — the same recovery story the firmware uses.

Review-hardened properties:
  - rx queue is BOUNDED (drop-oldest): an unpowered ESP32 flooding break
    noise overnight must not OOM a Pi that also hosts the LLM.
  - garbage events are rate-limited to one per window (the count still
    tracks every incident) — the session only needs "garbage happened",
    not one event per 8KB of trash.
  - close()/open() is a supported cycle: open() rebuilds the queue and
    reader, so the daemon's reconnect supervisor can recover a dead link.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import serial

from . import protocol

log = logging.getLogger("link.transport")

# Queue bound sizing: legitimate traffic is request/response — a reply is
# <=4095B (a few dozen lines) and the session stops collecting at 500 lines,
# so 4096 events is far above any real burst while still bounding a
# garbage/line flood to a few hundred KB instead of an overnight OOM.
# (The P4 audio stream gets its own frame path; it never rides this queue.)
_RX_QUEUE_MAX = 4096
_GARBAGE_EVENT_WINDOW_S = 0.25
# Encoded frame ceiling: body (5 + 1024 + 2) + COBS worst case (~1/254) ≈ 1036;
# anything larger is a lost delimiter, not a real frame.
_MAX_FRAME_WIRE = 1100


@dataclass
class LinkEvent:
    kind: str        # "line" | "garbage" | "closed" | "frame"
    text: str = ""
    frame: bytes = b""   # kind == "frame": one decoded+verified frame body


class FrameSink(Protocol):
    """Optional direct reader-thread route for unsolicited binary streams.

    Implementations must be synchronous, non-blocking, and exception-free. A
    True result claims the frame before it can enter the loop-owned generic RX
    queue. This is what keeps live PCM draining while the asyncio loop stalls.
    """

    def offer_frame(self, ftype: int, seq: int, payload: bytes) -> bool:
        ...

    def link_closed(self) -> None:
        ...


class SerialTransport:
    def __init__(self, port: str = "", baud: int = 921600, *,
                 serial_obj: serial.SerialBase | None = None,
                 frame_sink: FrameSink | None = None):
        self._port = port
        self._baud = baud
        self._injected = serial_obj
        self._ser: serial.SerialBase | None = None
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._closing = False
        # Installed before open and intentionally immutable for the lifetime of
        # the transport. Swapping a sink underneath the reader would create an
        # ownership gap in which a BEGIN lands in one receiver and PCM in another.
        self._frame_sink = frame_sink
        self._last_garbage_emit = 0.0
        self.rx: asyncio.Queue[LinkEvent] = asyncio.Queue(maxsize=_RX_QUEUE_MAX)
        self.garbage_count = 0          # cumulative incidents (not events)

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        if self._injected is not None:
            if self._ser is not None:
                return                   # injected object: single-shot open
            self._ser = self._injected
        else:
            if self._ser is not None:
                return
            if not self._port:
                raise ConnectionError("no port configured")
            self._ser = serial.Serial(
                self._port, self._baud,
                timeout=0.05,            # reader-loop poll granularity
                write_timeout=2.0,
            )
        self._loop = asyncio.get_running_loop()
        # Fresh queue per open: a stale "closed" event from the previous
        # reader must not instantly kill the new session (review finding).
        self.rx = asyncio.Queue(maxsize=_RX_QUEUE_MAX)
        self._closing = False
        self._thread = threading.Thread(
            target=self._reader_main, name="uart-reader", daemon=True)
        self._thread.start()
        log.info("link open: %s @ %d", self._ser.port or "(injected)", self._baud)

    def close(self) -> None:
        self._closing = True
        ser = self._ser
        if ser is not None:
            try:
                ser.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._ser = None
        if self._injected is not None:
            self._injected = None       # injected objects cannot reopen

    # -- TX ----------------------------------------------------------------

    def write_line(self, text: str) -> None:
        """One command line out. Caller holds the Session command lock."""
        data = text.encode("utf-8")
        if len(data) > protocol.MAX_CMD_LINE:
            raise ValueError(
                f"command line {len(data)}B exceeds firmware cap "
                f"{protocol.MAX_CMD_LINE}B (would be discarded whole)")
        if self._ser is None:
            raise ConnectionError("link is closed")
        self._ser.write(data + b"\n")

    # -- reader thread -----------------------------------------------------

    def _emit(self, ev: LinkEvent) -> None:
        # Bind the DESTINATION queue now: call_soon_threadsafe defers the
        # actual enqueue to the loop thread, which can run AFTER a
        # close()/open() cycle replaced self.rx — without the binding, a
        # dying reader's "closed" event lands in the fresh session's queue
        # and instantly kills it (found by the reopen-cycle test).
        loop, q = self._loop, self.rx
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._enqueue_into, q, ev)

    def _enqueue_into(self, q: asyncio.Queue, ev: LinkEvent) -> None:
        # Runs on the loop thread. Drop events addressed to a queue that a
        # reopen has since retired; bounded drop-oldest otherwise (during a
        # flood the freshest events are the ones that matter).
        if q is not self.rx:
            return
        if q.full():
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(ev)

    def _note_garbage(self) -> None:
        self.garbage_count += 1
        now = time.monotonic()
        if now - self._last_garbage_emit >= _GARBAGE_EVENT_WINDOW_S:
            self._last_garbage_emit = now
            self._emit(LinkEvent("garbage"))

    def _reader_main(self) -> None:
        # Two interleaved framings share the wire: newline-terminated TEXT and
        # 0x00-delimited binary FRAMES. 0x00 never occurs in text (the reply
        # pipeline is NUL-terminated C strings), so a 0x00 unambiguously opens
        # a frame; the frame ends at the next 0x00. We scan byte-oriented,
        # switching mode at each 0x00 boundary.
        line = bytearray()
        frame = bytearray()
        in_frame = False
        ser = self._ser
        try:
            while not self._closing:
                chunk = ser.read(4096)
                if not chunk:
                    # Idle (50ms with no bytes). A real frame is a contiguous
                    # ~11ms burst, so silence mid-frame means we entered frame
                    # mode on a stray 0x00 (a ROM boot burst has arbitrary
                    # 0x00s and can leave odd delimiter parity). WITHOUT this,
                    # the reader stays in_frame forever, swallowing every
                    # subsequent NUL-free text reply (login, command replies) —
                    # the HIGH desync the review found. Abort back to text mode.
                    if in_frame:
                        in_frame = False
                        frame.clear()
                        self._note_garbage()
                    continue
                for byte in chunk:
                    if byte == protocol.FRAME_DELIM:
                        if in_frame:
                            self._finish_frame(frame)   # closing delim
                            frame.clear()
                            in_frame = False
                        else:
                            # opening delim: flush any partial text first
                            if line:
                                self._dispatch_line(bytes(line).rstrip(b"\r"))
                                line.clear()
                            in_frame = True
                        continue
                    if in_frame:
                        frame.append(byte)
                        if len(frame) > _MAX_FRAME_WIRE:
                            in_frame = False   # runaway: resync at next 0x00
                            frame.clear()
                            self._note_garbage()
                    elif byte == 0x0A:          # newline: end of text line
                        self._dispatch_line(bytes(line).rstrip(b"\r"))
                        line.clear()
                    else:
                        line.append(byte)
                        if len(line) > protocol.MAX_RX_LINE:
                            line.clear()
                            self._note_garbage()
        except Exception as exc:  # includes SerialException on close/unplug
            if not self._closing:
                log.warning("reader stopped: %s", exc)
        finally:
            sink = self._frame_sink
            if sink is not None:
                try:
                    sink.link_closed()
                except Exception:
                    # A diagnostic sink bug must not keep the serial reader alive
                    # or suppress the supervisor's authoritative closed event.
                    log.exception("frame sink failed while closing")
            self._emit(LinkEvent("closed"))

    def _finish_frame(self, encoded: bytes) -> None:
        if not encoded:
            return   # 0x00 0x00 pair (SOF immediately followed by EOF) — ignore
        try:
            body = protocol.cobs_decode(encoded)
            ftype, seq, payload = protocol.parse_frame_body(body)
        except ValueError as exc:
            log.debug("dropped bad frame: %s", exc)
            self._note_garbage()
            return
        sink = self._frame_sink
        if sink is not None:
            try:
                if sink.offer_frame(ftype, seq, payload):
                    return
            except Exception:
                # Never let application code kill the byte-draining thread. The
                # frame is dropped visibly as garbage; a live stream then fails
                # closed on its next offset/terminal check or receive deadline.
                log.exception("frame sink failed for type 0x%02x", ftype)
                self._note_garbage()
                return
        self._emit(LinkEvent("frame", frame=body))

    def _dispatch_line(self, raw: bytes) -> None:
        if not raw:
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            self._note_garbage()
            return
        # Wrong-baud garbage often decodes "successfully" into control trash;
        # require mostly-printable before calling it a line.
        printable = sum(1 for ch in text if ch.isprintable() or ch == "\t")
        if printable < max(1, int(len(text) * 0.8)):
            self._note_garbage()
            return
        self._emit(LinkEvent("line", text))
