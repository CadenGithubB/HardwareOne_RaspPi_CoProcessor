"""Bounded reader-thread inbox for unsolicited live PCM v1 frames.

This module is transport shadow infrastructure only. It validates and buffers
PCM, but deliberately knows nothing about Moonshine, the pipeline, or G2 UI.
``SerialTransport`` calls :meth:`LivePcmInbox.offer_frame` directly from its
reader thread, so the hot path performs only strict struct checks, CRC32, and a
non-blocking bounded enqueue.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass

from ..link import protocol


# 48 KiB = 1.5 s of 16 kHz PCM. Raised from 16 KiB (512 ms) for the firmware's
# since-wake preroll (2026-08-10): capture start delivers up to ~1.2 s of
# backlogged audio at ~5x real time (device-paced at 30 ms/chunk); the inbox
# must absorb that burst plus GIL-stall margin without pcm_queue_overflow.
DEFAULT_PCM_QUEUE_BYTES = 48 * 1024
DEFAULT_PCM_QUEUE_FRAMES = 96
DEFAULT_FIRST_PCM_TIMEOUT_S = 0.50
# Firmware converts a 2.0s recorder/source stall into an authoritative ABORT.
# Leave host-side scheduling/UART margin so that terminal wins the boundary
# instead of a local timeout invalidating the stream milliseconds too early.
DEFAULT_INTERFRAME_TIMEOUT_S = 3.0
DEFAULT_ABSOLUTE_TIMEOUT_S = 65.0


@dataclass(frozen=True)
class LivePcmChunk:
    wire_seq: int
    sample_offset: int
    sample_count: int
    pcm: bytes
    received_ns: int


@dataclass(frozen=True)
class LiveStreamTerminal:
    kind: str                    # "end" | "abort" | "invalid"
    valid: bool
    reason: int | str
    total_samples: int
    pcm_crc32: int
    dropped_samples: int
    received_ns: int


class LivePcmStream:
    """One exact exchange/controller stream with durable terminal state."""

    def __init__(
        self,
        begin: protocol.LiveBegin,
        *,
        begin_seq: int,
        received_ns: int,
        max_queue_bytes: int,
        max_queue_frames: int,
        first_pcm_timeout_s: float,
        interframe_timeout_s: float,
        absolute_timeout_s: float,
    ) -> None:
        self.begin = begin
        self.begin_seq = begin_seq
        self.begin_received_ns = received_ns
        self._max_queue_bytes = max_queue_bytes
        self._max_queue_frames = max_queue_frames
        self._first_pcm_timeout_ns = int(first_pcm_timeout_s * 1_000_000_000)
        self._interframe_timeout_ns = int(interframe_timeout_s * 1_000_000_000)
        self._absolute_timeout_ns = int(absolute_timeout_s * 1_000_000_000)
        self._condition = threading.Condition()
        self._chunks: collections.deque[LivePcmChunk] = collections.deque()
        self._queued_bytes = 0
        self._queued_samples = 0
        self._queue_high_water_bytes = 0
        self._queue_high_water_frames = 0
        self._expected_offset = 0
        self._pcm_crc32 = 0
        self._pcm_frames = 0
        self._first_pcm_ns: int | None = None
        self._last_frame_ns = received_ns
        self._last_wire_seq = begin_seq
        self._terminal: LiveStreamTerminal | None = None

    @property
    def exchange_id(self) -> int:
        return self.begin.exchange_id

    @property
    def controller_id(self) -> int:
        return self.begin.controller_id

    @property
    def terminal(self) -> LiveStreamTerminal | None:
        with self._condition:
            return self._terminal

    @property
    def complete(self) -> bool:
        with self._condition:
            return self._terminal is not None

    def snapshot(self) -> dict[str, int | float | bool | str | None]:
        with self._condition:
            terminal = self._terminal
            return {
                "exchange_id": protocol.live_id_hex(self.exchange_id),
                "controller_id": protocol.live_id_hex(self.controller_id),
                "synthetic": self.begin.synthetic,
                "source": self.begin.source,
                "sample_rate": self.begin.sample_rate,
                "logical_chunk_samples": self.begin.logical_chunk_samples,
                "received_samples": self._expected_offset,
                "pcm_frames": self._pcm_frames,
                "pcm_crc32": self._pcm_crc32,
                "queued_bytes": self._queued_bytes,
                "queued_frames": len(self._chunks),
                "queue_high_water_bytes": self._queue_high_water_bytes,
                "queue_high_water_frames": self._queue_high_water_frames,
                "first_pcm_latency_ms": (
                    (self._first_pcm_ns - self.begin_received_ns) / 1_000_000.0
                    if self._first_pcm_ns is not None else None
                ),
                "terminal_kind": terminal.kind if terminal else None,
                "terminal_valid": terminal.valid if terminal else None,
                "terminal_reason": str(terminal.reason) if terminal else None,
                "terminal_dropped_samples": (
                    terminal.dropped_samples if terminal else None),
            }

    def offer_pcm(self, frame: protocol.LivePcm, *, seq: int,
                  received_ns: int) -> None:
        with self._condition:
            if self._terminal is not None:
                return
            mismatch = self._identity_problem(
                frame.exchange_id, frame.controller_id, frame.flags)
            if mismatch is not None:
                self._invalidate_locked(mismatch, received_ns)
                return
            sequence_problem = self._wire_sequence_problem(seq)
            if sequence_problem is not None:
                self._invalidate_locked(sequence_problem, received_ns)
                return
            if frame.sample_offset != self._expected_offset:
                self._invalidate_locked(
                    f"sample_offset:{frame.sample_offset}!={self._expected_offset}",
                    received_ns)
                return
            if (self._queued_bytes + len(frame.pcm) > self._max_queue_bytes or
                    len(self._chunks) + 1 > self._max_queue_frames):
                self._invalidate_locked(
                    "pcm_queue_overflow", received_ns,
                    dropped_samples=frame.sample_count)
                return

            chunk = LivePcmChunk(
                wire_seq=seq,
                sample_offset=frame.sample_offset,
                sample_count=frame.sample_count,
                pcm=frame.pcm,
                received_ns=received_ns,
            )
            self._chunks.append(chunk)
            self._queued_bytes += len(frame.pcm)
            self._queued_samples += frame.sample_count
            self._queue_high_water_bytes = max(
                self._queue_high_water_bytes, self._queued_bytes)
            self._queue_high_water_frames = max(
                self._queue_high_water_frames, len(self._chunks))
            self._expected_offset += frame.sample_count
            self._pcm_crc32 = protocol.crc32_ieee(frame.pcm, self._pcm_crc32)
            self._pcm_frames += 1
            if self._first_pcm_ns is None:
                self._first_pcm_ns = received_ns
            self._last_frame_ns = received_ns
            self._last_wire_seq = seq
            self._condition.notify_all()

    def offer_terminal(self, frame: protocol.LiveTerminal, *, is_abort: bool,
                       seq: int, received_ns: int) -> None:
        with self._condition:
            if self._terminal is not None:
                return
            mismatch = self._identity_problem(
                frame.exchange_id, frame.controller_id, self.begin.flags)
            if mismatch is not None:
                self._invalidate_locked(mismatch, received_ns)
                return
            sequence_problem = self._wire_sequence_problem(seq)
            if sequence_problem is not None:
                self._invalidate_locked(sequence_problem, received_ns)
                return
            self._last_frame_ns = received_ns
            self._last_wire_seq = seq
            if is_abort:
                if frame.reason not in protocol.LIVE_ABORT_REASONS:
                    self._invalidate_locked(
                        f"abort_reason:{frame.reason}", received_ns,
                        dropped_samples=frame.dropped_samples)
                    return
                # ABORT is not a successful stream, but its sent-prefix
                # accounting is still authoritative.  A missing PCM frame
                # immediately before ABORT must be diagnosed as transport
                # corruption rather than being hidden behind the device's
                # abort cause.
                if frame.total_samples != self._expected_offset:
                    self._invalidate_locked(
                        f"abort_total:{frame.total_samples}!={self._expected_offset}",
                        received_ns, dropped_samples=frame.dropped_samples)
                    return
                if frame.pcm_crc32 != self._pcm_crc32:
                    self._invalidate_locked(
                        f"abort_crc32:{frame.pcm_crc32:08x}!={self._pcm_crc32:08x}",
                        received_ns, dropped_samples=frame.dropped_samples)
                    return
                host_queued_samples = self._queued_samples
                self._clear_chunks_locked()
                self._terminal = LiveStreamTerminal(
                    kind="abort",
                    valid=False,
                    reason=frame.reason,
                    total_samples=frame.total_samples,
                    pcm_crc32=frame.pcm_crc32,
                    dropped_samples=frame.dropped_samples + host_queued_samples,
                    received_ns=received_ns,
                )
                self._condition.notify_all()
                return

            if frame.reason != protocol.LIVE_END_REASON_OK:
                self._invalidate_locked(
                    f"end_reason:{frame.reason}", received_ns,
                    dropped_samples=frame.dropped_samples)
                return
            if frame.total_samples != self._expected_offset:
                self._invalidate_locked(
                    f"end_total:{frame.total_samples}!={self._expected_offset}",
                    received_ns, dropped_samples=frame.dropped_samples)
                return
            if frame.pcm_crc32 != self._pcm_crc32:
                self._invalidate_locked(
                    f"end_crc32:{frame.pcm_crc32:08x}!={self._pcm_crc32:08x}",
                    received_ns, dropped_samples=frame.dropped_samples)
                return
            if frame.dropped_samples != 0:
                self._invalidate_locked(
                    f"end_dropped_samples:{frame.dropped_samples}",
                    received_ns, dropped_samples=frame.dropped_samples)
                return
            self._terminal = LiveStreamTerminal(
                kind="end",
                valid=True,
                reason=frame.reason,
                total_samples=frame.total_samples,
                pcm_crc32=frame.pcm_crc32,
                dropped_samples=0,
                received_ns=received_ns,
            )
            self._condition.notify_all()

    def invalidate(self, reason: str, *, received_ns: int | None = None,
                   dropped_samples: int = 0) -> None:
        with self._condition:
            self._invalidate_locked(
                reason, received_ns or time.monotonic_ns(),
                dropped_samples=dropped_samples)

    def next_item(self, timeout: float | None = None) -> LivePcmChunk | LiveStreamTerminal:
        """Return queued PCM in order, then the durable terminal.

        A missing/corrupt terminal cannot wait forever: first-PCM, inter-frame,
        and absolute deadlines are checked inside this blocking consumer API.
        """
        user_deadline_ns = (
            time.monotonic_ns() + int(timeout * 1_000_000_000)
            if timeout is not None else None)
        with self._condition:
            while True:
                now_ns = time.monotonic_ns()
                self._expire_locked(now_ns)
                if self._chunks:
                    chunk = self._chunks.popleft()
                    self._queued_bytes -= len(chunk.pcm)
                    self._queued_samples -= chunk.sample_count
                    return chunk
                if self._terminal is not None:
                    return self._terminal

                stream_deadline = self._next_stream_deadline_locked()
                deadline = stream_deadline
                if user_deadline_ns is not None:
                    deadline = min(deadline, user_deadline_ns)
                remaining_ns = deadline - now_ns
                if remaining_ns <= 0:
                    if user_deadline_ns is not None and user_deadline_ns <= now_ns:
                        raise TimeoutError("timed out waiting for live PCM")
                    continue
                self._condition.wait(remaining_ns / 1_000_000_000.0)

    def wait_terminal(self, timeout: float | None = None) -> LiveStreamTerminal:
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            while self._terminal is None:
                now_ns = time.monotonic_ns()
                self._expire_locked(now_ns)
                if self._terminal is not None:
                    break
                wait_s = max(0.0, (
                    self._next_stream_deadline_locked() - now_ns) / 1_000_000_000.0)
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("timed out waiting for live PCM terminal")
                    wait_s = min(wait_s, remaining)
                self._condition.wait(wait_s)
            return self._terminal

    def _identity_problem(self, exchange_id: int, controller_id: int,
                          flags: int) -> str | None:
        if exchange_id != self.exchange_id:
            return (f"exchange_id:{exchange_id:016x}!={self.exchange_id:016x}")
        if controller_id != self.controller_id:
            return (f"controller_id:{controller_id:016x}!={self.controller_id:016x}")
        if flags != self.begin.flags:
            return f"flags:{flags:02x}!={self.begin.flags:02x}"
        return None

    def _wire_sequence_problem(self, seq: int) -> str | None:
        expected = (self._last_wire_seq + 1) & 0xFFFF
        if seq != expected:
            return f"wire_seq:{seq}!={expected}"
        return None

    def _invalidate_locked(self, reason: str, received_ns: int,
                           *, dropped_samples: int = 0) -> None:
        if self._terminal is not None:
            return
        dropped_samples += self._queued_samples
        self._clear_chunks_locked()
        self._terminal = LiveStreamTerminal(
            kind="invalid",
            valid=False,
            reason=reason,
            total_samples=self._expected_offset,
            pcm_crc32=self._pcm_crc32,
            dropped_samples=dropped_samples,
            received_ns=received_ns,
        )
        self._condition.notify_all()

    def _clear_chunks_locked(self) -> None:
        self._chunks.clear()
        self._queued_bytes = 0
        self._queued_samples = 0

    def _next_stream_deadline_locked(self) -> int:
        absolute = self.begin_received_ns + self._absolute_timeout_ns
        if self._first_pcm_ns is None:
            return min(absolute,
                       self.begin_received_ns + self._first_pcm_timeout_ns)
        return min(absolute, self._last_frame_ns + self._interframe_timeout_ns)

    def _expire_locked(self, now_ns: int) -> None:
        if self._terminal is not None:
            return
        if now_ns >= self.begin_received_ns + self._absolute_timeout_ns:
            self._invalidate_locked("absolute_timeout", now_ns)
        elif (self._first_pcm_ns is None and
              now_ns >= self.begin_received_ns + self._first_pcm_timeout_ns):
            self._invalidate_locked("first_pcm_timeout", now_ns)
        elif (self._first_pcm_ns is not None and
              now_ns >= self._last_frame_ns + self._interframe_timeout_ns):
            self._invalidate_locked("interframe_timeout", now_ns)


class LivePcmInbox:
    """FrameSink implementation with one bounded pre-wake active stream."""

    def __init__(
        self,
        controller_id: int,
        *,
        max_queue_bytes: int = DEFAULT_PCM_QUEUE_BYTES,
        max_queue_frames: int = DEFAULT_PCM_QUEUE_FRAMES,
        first_pcm_timeout_s: float = DEFAULT_FIRST_PCM_TIMEOUT_S,
        interframe_timeout_s: float = DEFAULT_INTERFRAME_TIMEOUT_S,
        absolute_timeout_s: float = DEFAULT_ABSOLUTE_TIMEOUT_S,
        tombstones: int = 8,
    ) -> None:
        # Reuse the protocol's strict two-half validation at construction.
        protocol.live_id_hex(controller_id)
        if max_queue_bytes <= 0 or max_queue_frames <= 0:
            raise ValueError("live PCM queue bounds must be positive")
        if min(first_pcm_timeout_s, interframe_timeout_s,
               absolute_timeout_s) <= 0:
            raise ValueError("live PCM deadlines must be positive")
        self.controller_id = controller_id
        self._max_queue_bytes = max_queue_bytes
        self._max_queue_frames = max_queue_frames
        self._first_pcm_timeout_s = first_pcm_timeout_s
        self._interframe_timeout_s = interframe_timeout_s
        self._absolute_timeout_s = absolute_timeout_s
        self._condition = threading.Condition()
        self._active: LivePcmStream | None = None
        self._generation = 0
        self._delivered_generation = 0
        self._tombstone_limit = max(1, tombstones)
        self._tombstone_order: collections.deque[int] = collections.deque()
        self._tombstone_set: set[int] = set()
        self._fault_count = 0
        self._late_frame_count = 0
        self._last_fault: str | None = None

    @property
    def last_fault(self) -> str | None:
        with self._condition:
            return self._last_fault

    def snapshot(self) -> dict[str, int | str | None]:
        with self._condition:
            return {
                "controller_id": protocol.live_id_hex(self.controller_id),
                "fault_count": self._fault_count,
                "late_frame_count": self._late_frame_count,
                "last_fault": self._last_fault,
                "active_exchange_id": (
                    protocol.live_id_hex(self._active.exchange_id)
                    if self._active is not None else None),
            }

    def offer_frame(self, ftype: int, seq: int, payload: bytes) -> bool:
        if ftype not in protocol.LIVE_FRAME_TYPES:
            return False
        received_ns = time.monotonic_ns()
        try:
            parsed = protocol.parse_live_payload(ftype, payload)
        except ValueError as exc:
            self._malformed(ftype, str(exc), received_ns)
            return True

        if ftype == protocol.FRAME_LIVE_BEGIN:
            assert isinstance(parsed, protocol.LiveBegin)
            self._offer_begin(parsed, seq=seq, received_ns=received_ns)
            return True

        assert isinstance(parsed, (protocol.LivePcm, protocol.LiveTerminal))
        stream = self._matching_stream(
            parsed.exchange_id, parsed.controller_id, received_ns)
        if stream is None:
            return True
        if ftype == protocol.FRAME_LIVE_PCM:
            assert isinstance(parsed, protocol.LivePcm)
            stream.offer_pcm(parsed, seq=seq, received_ns=received_ns)
        else:
            assert isinstance(parsed, protocol.LiveTerminal)
            stream.offer_terminal(
                parsed,
                is_abort=ftype == protocol.FRAME_LIVE_ABORT,
                seq=seq,
                received_ns=received_ns,
            )
        if stream.complete:
            self._remember_tombstone(stream.exchange_id)
        return True

    def link_closed(self) -> None:
        with self._condition:
            stream = self._active
        if stream is not None and not stream.complete:
            stream.invalidate("link_closed")
            self._remember_tombstone(stream.exchange_id)

    def next_stream(self, timeout: float | None = None, *,
                    for_exchange: int | None = None) -> LivePcmStream:
        """Deliver the newest undelivered active stream.

        With for_exchange set, the shared delivery slot is consumed ONLY when
        the active stream matches that exchange. A caller waiting for its own
        BEGIN can wake on a successor exchange's BEGIN (rapid re-wake); rather
        than consume that successor's one-and-only delivery (which would
        strand it for its rightful consumer), the foreign generation is
        skipped locally and the wait continues for the next BEGIN. The global
        _delivered_generation is left untouched so the successor's own
        next_stream still delivers it.
        """
        deadline = time.monotonic() + timeout if timeout is not None else None
        # Local high-water mark: generations at or below this have been seen
        # by THIS call. Starts at the shared delivered mark so a brand-new
        # stream (generation advanced) is what we look for.
        seen_generation = self._delivered_generation
        with self._condition:
            while True:
                fresh = (self._active is not None
                         and self._generation != seen_generation)
                if fresh:
                    if (for_exchange is None
                            or self._active.exchange_id == for_exchange):
                        self._delivered_generation = self._generation
                        return self._active
                    # Foreign stream: skip it locally without consuming its
                    # delivery, then keep waiting for our own BEGIN.
                    seen_generation = self._generation
                    continue
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    detail = f": {self._last_fault}" if self._last_fault else ""
                    raise TimeoutError(f"timed out waiting for LIVE_BEGIN{detail}")
                self._condition.wait(remaining)

    def _offer_begin(self, begin: protocol.LiveBegin, *, seq: int,
                     received_ns: int) -> None:
        with self._condition:
            if begin.controller_id != self.controller_id:
                self._record_fault_locked(
                    f"BEGIN controller {begin.controller_id:016x} != "
                    f"ready {self.controller_id:016x}")
                return
            if begin.exchange_id in self._tombstone_set:
                self._late_frame_count += 1
                return
            prior = self._active
            if prior is not None and prior.exchange_id == begin.exchange_id:
                prior.invalidate("duplicate_begin", received_ns=received_ns)
                self._record_fault_locked(
                    f"duplicate BEGIN {begin.exchange_id:016x}")
                self._remember_tombstone_locked(begin.exchange_id)
                return
            if prior is not None and not prior.complete:
                prior.invalidate("superseded_begin", received_ns=received_ns)
                self._remember_tombstone_locked(prior.exchange_id)
            elif prior is not None:
                self._remember_tombstone_locked(prior.exchange_id)

            self._active = LivePcmStream(
                begin,
                begin_seq=seq,
                received_ns=received_ns,
                max_queue_bytes=self._max_queue_bytes,
                max_queue_frames=self._max_queue_frames,
                first_pcm_timeout_s=self._first_pcm_timeout_s,
                interframe_timeout_s=self._interframe_timeout_s,
                absolute_timeout_s=self._absolute_timeout_s,
            )
            self._generation += 1
            self._condition.notify_all()

    def _matching_stream(self, exchange_id: int, controller_id: int,
                         received_ns: int) -> LivePcmStream | None:
        with self._condition:
            if exchange_id in self._tombstone_set:
                self._late_frame_count += 1
                return None
            stream = self._active
            if stream is None:
                self._record_fault_locked(
                    f"live frame without BEGIN exchange={exchange_id:016x}")
                return None
            if controller_id != self.controller_id:
                stream.invalidate(
                    f"controller_id:{controller_id:016x}!={self.controller_id:016x}",
                    received_ns=received_ns)
                self._record_fault_locked("live frame controller mismatch")
                self._remember_tombstone_locked(stream.exchange_id)
                return None
            if exchange_id != stream.exchange_id:
                stream.invalidate(
                    f"unexpected_exchange:{exchange_id:016x}",
                    received_ns=received_ns)
                self._record_fault_locked("unexpected live exchange ID")
                self._remember_tombstone_locked(stream.exchange_id)
                return None
            return stream

    def _malformed(self, ftype: int, detail: str, received_ns: int) -> None:
        with self._condition:
            stream = self._active
            if stream is not None and not stream.complete and ftype != protocol.FRAME_LIVE_BEGIN:
                stream.invalidate(
                    f"malformed_0x{ftype:02x}:{detail}",
                    received_ns=received_ns)
                self._remember_tombstone_locked(stream.exchange_id)
            self._record_fault_locked(f"malformed live frame 0x{ftype:02x}: {detail}")

    def _record_fault_locked(self, detail: str) -> None:
        self._fault_count += 1
        self._last_fault = detail
        self._condition.notify_all()

    def _remember_tombstone(self, exchange_id: int) -> None:
        with self._condition:
            self._remember_tombstone_locked(exchange_id)

    def _remember_tombstone_locked(self, exchange_id: int) -> None:
        if exchange_id in self._tombstone_set:
            return
        self._tombstone_order.append(exchange_id)
        self._tombstone_set.add(exchange_id)
        while len(self._tombstone_order) > self._tombstone_limit:
            old = self._tombstone_order.popleft()
            self._tombstone_set.discard(old)


def synthetic_pcm(exchange_id: int, total_samples: int) -> bytes:
    """Deterministic firmware-probe PCM pattern, packed S16LE."""
    low = exchange_id & 0xFFFF
    out = bytearray(total_samples * 2)
    for sample_index in range(total_samples):
        value = ((sample_index * 257) ^ low) & 0xFFFF
        out[sample_index * 2] = value & 0xFF
        out[sample_index * 2 + 1] = value >> 8
    return bytes(out)
