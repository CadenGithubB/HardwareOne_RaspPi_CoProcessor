"""Job sources: where exchanges come from.

P0: ManualTrigger — CLI one-shots and a tiny unix-socket control channel
for the daemon ("ask\n" or "chat <text>\n" per connection).

EvenAI wake (G2 "Hey Even"): the firmware pushes an "evenai_wake" EVT frame
the moment its auto-capture is confirmed recording; route_link_event turns
that into a Job("evenai"). At most ONE evenai job is pending/running at a
time — the capture on the device is a singleton, so a duplicate job could
only ever re-fetch stale audio.

P1 adds HostJobsPoller (2-5Hz `hostjobs json since=<seq>`) behind the same
JobSource interface — the pipeline never learns the difference.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from . import evenai_protocol as evenai_wire

log = logging.getLogger("jobs")


@dataclass
class Job:
    kind: str          # "ask" | "chat" | "evenai"
    text: str = ""     # chat prompt (kind == "chat")
    # Arrival stamp. A wake is only worth answering while the wearer is still
    # looking at the card; past that, replying re-opens a session they already
    # dismissed. The pipeline drops evenai jobs older than its staleness bound.
    created: float = field(default_factory=time.monotonic)
    exchange: "EvenAiExchange | None" = None


class EvenAiCancelled(Exception):
    """Cooperative cancellation of one wearer-owned exchange."""


@dataclass
class EvenAiExchange:
    """Loop-thread-owned state shared by routing, fetch, and delivery."""

    exchange_id: str
    created: float = field(default_factory=time.monotonic)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    terminal_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_reason: str | None = None
    cancelled_ns: int | None = None
    recording_path: str | None = None
    delivered: bool = False
    tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def cancel(self, reason: str) -> None:
        if not self.cancel_event.is_set():
            self.cancel_reason = reason
            # Capture the loop-thread cancellation boundary before waking any
            # observers. Diagnostic marker tasks can then report the actual
            # routed dismissal time instead of their later scheduling time.
            self.cancelled_ns = time.monotonic_ns()
            self.cancel_event.set()

    def recording_stopped(self, path: str) -> None:
        self.recording_path = path
        self.terminal_event.set()

    def mark_delivered(self) -> None:
        """Commit the only outcome that suppresses host-side EXIT cleanup."""
        self.raise_if_cancelled()
        self.delivered = True

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise EvenAiCancelled(
                f"EvenAI {self.exchange_id} cancelled: {self.cancel_reason or 'unknown'}")

    async def sleep(self, delay: float) -> None:
        """Cancellation-aware sleep used by render and recorder waits."""
        if delay <= 0:
            self.raise_if_cancelled()
            return
        try:
            await asyncio.wait_for(self.cancel_event.wait(), delay)
        except asyncio.TimeoutError:
            return
        self.raise_if_cancelled()

    def start_task(self, coro, *, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self.tasks.add(task)
        return task

    async def drain_tasks(self) -> None:
        tasks, self.tasks = tuple(self.tasks), set()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class JobSource(Protocol):
    async def next_job(self) -> Job: ...
    def evenai_done(self, exchange_id: str) -> None: ...


class ManualTrigger:
    """asyncio.Queue-backed source; optionally served by a unix socket."""

    def __init__(self) -> None:
        # Native-wake work is wearer-facing and expires quickly.  Keep manual
        # ask/chat FIFO ordering, but do not let a valid wake sit behind an
        # arbitrary manual backlog and age into _WAKE_STALE_S.  The sequence
        # makes equal-priority entries stable without comparing Job objects.
        self._queue: asyncio.PriorityQueue[tuple[int, int, Job]] = (
            asyncio.PriorityQueue())
        self._queue_seq = itertools.count()
        self._server: asyncio.AbstractServer | None = None
        self._evenai: dict[str, EvenAiExchange] = {}
        # Unknown/late cancellation must remain sticky so cancel-before-wake
        # cannot resurrect a dismissed card. IDs contain a boot nonce, so a
        # small bounded cache is sufficient and cannot reject a future boot.
        self._evenai_tombstones: OrderedDict[str, str] = OrderedDict()
        self._tombstone_limit = 64

    async def next_job(self) -> Job:
        while True:
            _priority, _seq, job = await self._queue.get()
            if job.kind == "evenai" and job.exchange is not None \
                    and job.exchange.cancelled:
                self.evenai_done(job.exchange.exchange_id)
                continue
            return job

    def submit(self, job: Job) -> None:
        self._enqueue(job, priority=1)

    def _enqueue(self, job: Job, *, priority: int) -> None:
        self._queue.put_nowait((priority, next(self._queue_seq), job))

    def submit_evenai(self, exchange_id: str) -> EvenAiExchange | None:
        """Queue a correlated wake, idempotently and with newest-wake wins."""
        eid = evenai_wire.exchange_id(exchange_id)
        if eid in self._evenai_tombstones:
            log.info("evenai wake %s ignored — exchange is terminal", eid)
            return None
        existing = self._evenai.get(eid)
        if existing is not None:
            log.info("duplicate evenai wake %s ignored", eid)
            return existing

        # The recorder/native card are singletons. A newer valid wake proves a
        # prior exchange is no longer the device owner even if its cancel EVT
        # was lost. Mark it superseded; its ID-scoped operations cannot touch
        # the new recording.
        for old in tuple(self._evenai.values()):
            if not old.cancelled:
                old.cancel("superseded")
                self._remember_terminal(old.exchange_id, "superseded")
        exchange = EvenAiExchange(eid)
        self._evenai[eid] = exchange
        self._enqueue(
            Job("evenai", created=exchange.created, exchange=exchange),
            priority=0)
        return exchange

    def cancel_evenai(self, exchange_id: str, reason: str) -> None:
        eid = evenai_wire.exchange_id(exchange_id)
        self._remember_terminal(eid, reason)
        exchange = self._evenai.get(eid)
        if exchange is not None:
            exchange.cancel(reason)
        else:
            log.info("evenai cancel %s arrived before/after its wake", eid)

    def recording_stopped(self, exchange_id: str, path: str) -> None:
        eid = evenai_wire.exchange_id(exchange_id)
        exchange = self._evenai.get(eid)
        if exchange is None:
            log.info("mic_autostop for unknown/terminal exchange %s", eid)
            return
        exchange.recording_stopped(path)

    def evenai_done(self, exchange_id: str) -> None:
        eid = evenai_wire.exchange_id(exchange_id)
        self._evenai.pop(eid, None)
        self._remember_terminal(eid, "complete")

    def _remember_terminal(self, exchange_id: str, reason: str) -> None:
        self._evenai_tombstones[exchange_id] = reason
        self._evenai_tombstones.move_to_end(exchange_id)
        while len(self._evenai_tombstones) > self._tombstone_limit:
            self._evenai_tombstones.popitem(last=False)

    async def serve_socket(self, socket_path: str) -> None:
        path = Path(os.path.expanduser(socket_path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            path.unlink()
        self._server = await asyncio.start_unix_server(self._on_connect, path=str(path))
        os.chmod(path, 0o600)
        log.info("control socket: %s (echo ask | nc -U %s)", path, path)

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _on_connect(self, reader: asyncio.StreamReader,
                          writer: asyncio.StreamWriter) -> None:
        try:
            raw = await asyncio.wait_for(reader.readline(), 10)
            line = raw.decode("utf-8", errors="replace").strip()
            if line == "ask":
                self.submit(Job("ask"))
                writer.write(b"queued\n")
            elif line.startswith("chat "):
                self.submit(Job("chat", line[5:]))
                writer.write(b"queued\n")
            elif line == "evenai":
                writer.write(b"evenai requires a firmware-issued exchange ID\n")
            else:
                writer.write(b"usage: ask | chat <text> | evenai\n")
            await writer.drain()
        except asyncio.TimeoutError:
            pass
        finally:
            writer.close()


def route_link_event(payload: bytes, trigger: ManualTrigger, session=None,
                     power=None, fan=None) -> None:
    """EVT frame payloads from the firmware -> jobs. Payloads are short ASCII:
    an event name plus optional space-separated args.
    Called from Session.on_event on the loop thread — must stay non-blocking."""
    # The control-plane submitters are synchronous parser/enqueue operations;
    # they preserve Session's rule that on_event must never perform I/O.
    if fan is not None and fan.submit_event(payload):
        return
    if power is not None and power.submit_event(payload):
        return
    try:
        text = payload.decode("ascii").strip()
    except UnicodeDecodeError:
        log.warning("undecodable device event payload (%d bytes)", len(payload))
        return
    # Manual/ask recordings are not EvenAI-owned and retain the legacy
    # path-only terminal event. Keep that fast path without ever accepting an
    # uncorrelated wake/cancel. EvenAI's three-token form is parsed below.
    legacy = text.split()
    if len(legacy) == 2 and legacy[0] == "mic_autostop":
        path = legacy[1]
        if (not path.startswith("/") or len(path) > 255 or
                any(c in path for c in "\r\n\0")):
            log.warning("rejected malformed legacy mic_autostop path")
            return
        if session is not None:
            session.mic_autostop.fire(path)
        else:
            log.info("legacy mic_autostop had no Session latch consumer")
        return
    try:
        event = evenai_wire.parse_event(text)
    except evenai_wire.EvenAiProtocolError as exc:
        log.warning("rejected malformed EvenAI event %r: %s", text, exc)
        return
    if isinstance(event, evenai_wire.WakeEvent):
        log.info("device wake %s — queueing evenai exchange", event.exchange_id)
        trigger.submit_evenai(event.exchange_id)
    elif isinstance(event, evenai_wire.CancelEvent):
        log.info("device cancelled EvenAI %s (%s)", event.exchange_id, event.reason)
        trigger.cancel_evenai(event.exchange_id, event.reason)
    elif isinstance(event, evenai_wire.MicAutostopEvent):
        trigger.recording_stopped(event.exchange_id, event.path)
    elif event is None:
        log.info("unhandled device event: %r", text)
    else:
        # Telemetry-only events (evenai_timing, evenai_stream_complete):
        # recorded by diagnostics; the daemon acknowledges them at INFO so
        # journald shows the device-side record without debug logging on.
        log.info("telemetry device event: %r", text)
