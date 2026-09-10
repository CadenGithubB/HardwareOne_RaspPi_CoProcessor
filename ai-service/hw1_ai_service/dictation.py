"""CM5 speech-to-text controller for on-device keyboard dictation.

The firmware owns capture and the destination text field.  This actor only
accepts the exact owner/path event, fetches the closed WAV, runs the existing
batch STT worker, and returns one ID-fenced terminal command.  ``submit_event``
runs on Session's event loop callback and therefore does parsing/enqueue work
only; all UART and model work lives in ``run``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .audio import fetch
from .link.session import (
    CommandCancelled,
    CommandTimeout,
    LinkClosed,
    LoginFailed,
    Session,
)

if TYPE_CHECKING:
    from .pipeline import VoicePipeline

log = logging.getLogger("dictation")

_REQUEST_RE = re.compile(
    r"^dictate_request ([0-9A-Fa-f]{16}) "
    r"((?:/sd)?/recordings/rec_([0-9A-Fa-f]{16})\.wav)$"
)
_CANCEL_RE = re.compile(r"^dictate_cancel ([0-9A-Fa-f]{16})$")
_TERMINAL_TIMEOUT_S = 5.0
_TRANSCRIPT_MAX_BYTES = 256
_RECENT_LIMIT = 64
_PUNCTUATION_FOLD = str.maketrans({
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
    "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2026": "...",
    "\u00a0": " ",
})


class DictationProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class DictationRequest:
    request_id: str
    path: str
    generation: int = 0
    created: float = 0.0


def _request_id(raw: str) -> str:
    if len(raw) != 16 or any(c not in "0123456789abcdefABCDEF" for c in raw):
        raise DictationProtocolError(
            "dictation ID must be exactly 16 hexadecimal digits")
    value = raw.lower()
    if int(value[:8], 16) == 0 or int(value[8:], 16) == 0:
        raise DictationProtocolError(
            "dictation ID nonce and counter must be non-zero")
    return value


def parse_request(payload: bytes) -> DictationRequest:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DictationProtocolError("dictation event must be ASCII") from exc
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise DictationProtocolError("dictation event contains a control byte")
    match = _REQUEST_RE.fullmatch(text)
    if match is None:
        raise DictationProtocolError(
            "expected dictate_request <16hex> </recordings|/sd/recordings>/rec_<id>.wav")
    request_id = _request_id(match.group(1))
    path_id = _request_id(match.group(3))
    if path_id != request_id:
        raise DictationProtocolError("recording path is not owned by the request ID")
    return DictationRequest(request_id, match.group(2))


def parse_cancel(payload: bytes) -> str:
    try:
        text = payload.decode("ascii")
    except UnicodeDecodeError as exc:
        raise DictationProtocolError("dictation cancel must be ASCII") from exc
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise DictationProtocolError("dictation cancel contains a control byte")
    match = _CANCEL_RE.fullmatch(text)
    if match is None:
        raise DictationProtocolError("expected dictate_cancel <16hex>")
    return _request_id(match.group(1))


def sanitize_transcript(text: str) -> str:
    """Return printable ASCII accepted by firmware's direct UART protocol.

    The dictation intrinsic deliberately admits only one printable line, so
    fold common Unicode punctuation/accents here, discard other non-ASCII code
    points, collapse whitespace, and honor the field's 256-byte limit (ASCII
    makes bytes and characters identical).
    """
    folded = unicodedata.normalize("NFKD", text.translate(_PUNCTUATION_FOLD))
    folded = folded.encode("ascii", errors="ignore").decode("ascii")
    normalized: list[str] = []
    for char in folded:
        if char.isspace() or unicodedata.category(char).startswith("C"):
            normalized.append(" ")
        else:
            normalized.append(char)
    collapsed = " ".join("".join(normalized).split())
    return collapsed[:_TRANSCRIPT_MAX_BYTES].strip()


class DictationController:
    """One bounded, reconnect-fenced dictation actor."""

    def __init__(self, session: Session, *, power=None, cm5_presence=None) -> None:
        self._session = session
        self._power = power
        self._cm5_presence = cm5_presence
        self._pipeline: VoicePipeline | None = None
        self._queue: asyncio.Queue[DictationRequest] = asyncio.Queue(maxsize=1)
        self._generation = 1
        self._latest_id: str | None = None
        self._active_id: str | None = None
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self._capability_supported: bool | None = None
        self._capability_lock = asyncio.Lock()
        self._republish_tasks: set[asyncio.Task] = set()
        add_login_listener = getattr(session, "add_login_listener", None)
        if callable(add_login_listener):
            add_login_listener(self._session_logged_in)

    def submit_event(self, payload: bytes) -> bool:
        """Parse/enqueue one event without doing I/O. Return whether consumed."""
        if payload.startswith(b"dictate_cancel"):
            try:
                request_id = parse_cancel(payload)
            except DictationProtocolError as exc:
                log.warning("rejected malformed dictation cancel: %s", exc)
                return True
            self._cancel_request(request_id)
            return True
        if not payload.startswith(b"dictate_request"):
            return False
        try:
            parsed = parse_request(payload)
        except DictationProtocolError as exc:
            log.warning("rejected malformed dictation event: %s", exc)
            return True
        request_id = parsed.request_id
        if (request_id == self._active_id or request_id == self._latest_id or
                request_id in self._recent):
            log.info("duplicate dictation request %s ignored", request_id)
            return True

        request = DictationRequest(
            request_id, parsed.path, self._generation, time.monotonic())
        self._latest_id = request_id
        if self._queue.full():
            old = self._queue.get_nowait()
            self._queue.task_done()
            self._remember(old.request_id)
            log.info("dictation request %s superseded queued %s",
                     request_id, old.request_id)
        self._queue.put_nowait(request)
        log.info("dictation request %s queued", request_id)
        return True

    async def attach(self, pipeline: VoicePipeline) -> None:
        self._pipeline = pipeline
        try:
            if not await self._publish_capability():
                # Firmware and daemon are deployed as one protocol pair. Never
                # service audio when the device did not accept this session's
                # explicit capability declaration.
                log.error("dictation controller remains fail-closed")
        except LinkClosed as exc:
            # Model initialization must not become permanently failed because
            # the UART flapped during this optional publication. The session
            # pump owns reconnect; the next actor run republishes per epoch.
            log.warning("dictation readiness deferred until reconnect: %s", exc)

    async def run(self) -> None:
        await self._publish_capability()
        while True:
            request = await self._queue.get()
            self._active_id = request.request_id
            try:
                await self._handle(request)
            finally:
                if self._active_id == request.request_id:
                    self._active_id = None
                if self._latest_id == request.request_id:
                    self._latest_id = None
                self._remember(request.request_id)
                self._queue.task_done()

    def link_reset(self) -> None:
        self._generation += 1
        self._latest_id = None
        self._active_id = None
        self._capability_supported = None
        self._recent.clear()
        self._cancel_republish_tasks()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

    async def close(self) -> None:
        should_revoke = self._pipeline is not None
        self._closed = True
        self._generation += 1
        tasks = tuple(self._republish_tasks)
        self._cancel_republish_tasks()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if should_revoke:
            try:
                await self._session.command(
                    "dictate hostready off", expect="status",
                    timeout=_TERMINAL_TIMEOUT_S, replay=False,
                    auth_replay=False)
            except Exception as exc:
                log.info("dictation capability revoke did not land: %s", exc)
        self._pipeline = None

    def _session_logged_in(self, _generation: int) -> None:
        """Invalidate the old capability and republish after Session unlocks."""
        self._generation += 1
        self._latest_id = None
        self._active_id = None
        self._capability_supported = None
        self._recent.clear()
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()
        if self._closed or self._pipeline is None:
            return
        task = asyncio.create_task(
            self._republish_after_login(), name="dictation-hostready")
        self._republish_tasks.add(task)
        task.add_done_callback(self._republish_tasks.discard)

    async def _republish_after_login(self) -> None:
        try:
            await self._publish_capability()
        except LinkClosed as exc:
            log.warning("dictation readiness lost with UART session: %s", exc)
        except Exception:
            log.exception("dictation readiness republish failed")

    def _cancel_republish_tasks(self) -> None:
        for task in tuple(self._republish_tasks):
            task.cancel()

    async def _publish_capability(self) -> bool:
        pipeline = self._pipeline
        if self._closed or pipeline is None or not pipeline.batch_stt_available:
            return False
        async with self._capability_lock:
            if self._capability_supported is not None:
                return self._capability_supported
            try:
                reply = await self._session.command(
                    "dictate hostready v1", expect="status",
                    timeout=_TERMINAL_TIMEOUT_S, replay=True)
            except LinkClosed:
                raise
            except (CommandTimeout, LoginFailed) as exc:
                # A mandatory capability command has no safe "maybe enabled"
                # state. Force the supervisor through a clean reconnect instead
                # of continuing on a stream with an ambiguous late reply.
                raise LinkClosed(
                    f"dictation readiness lost UART synchronization: {exc}"
                ) from exc
            self._capability_supported = reply.ok
            if reply.ok:
                log.info("dictation-v1 ready for this UART session")
            else:
                log.error("firmware rejected mandatory dictation-v1 readiness: %s",
                          reply.text)
            return reply.ok

    def _current(self, request: DictationRequest) -> bool:
        return (not self._closed and request.generation == self._generation and
                request.request_id == self._latest_id)

    async def _handle(self, request: DictationRequest) -> None:
        started = time.monotonic()
        event_started = request.created or started
        pipeline = self._pipeline
        if (pipeline is None or not pipeline.batch_stt_available or
                self._capability_supported is not True):
            await self._send_failure(request, "host_not_ready")
            return

        presence_token = None
        power_started = False
        try:
            if self._cm5_presence is not None:
                presence_token = await self._cm5_presence.acquire_busy(
                    f"dictation:{request.request_id}")
            if self._power is not None:
                await self._power.activity_started()
                power_started = True

            wav_bytes = await fetch.fetch_frames(
                self._session, request.path,
                cancel_guard=lambda: not self._current(request))
            if not self._current(request):
                return
            fetched = time.monotonic()
            transcript = await pipeline.transcribe_dictation(wav_bytes)
            if not self._current(request):
                return
            transcribed = time.monotonic()
            safe = sanitize_transcript(transcript)
            if not safe:
                delivered = await self._send_failure(
                    request, "no_speech_recognized")
            else:
                delivered = await self._send_terminal(
                    request, f"dictate result {request.request_id} {safe}")
            finished = time.monotonic()
            log.info(
                "dictation %s complete: bytes=%d queue=%.3fs fetch=%.3fs "
                "stt=%.3fs result=%.3fs total=%.3fs accepted=%d",
                request.request_id, len(wav_bytes), started - event_started,
                fetched - started,
                transcribed - fetched, finished - transcribed,
                finished - event_started, 1 if delivered else 0)
        except CommandCancelled:
            log.info("dictation %s superseded while fetching", request.request_id)
        except (CommandTimeout, LinkClosed) as exc:
            raise LinkClosed(
                f"dictation {request.request_id} lost UART synchronization: {exc}") from exc
        except fetch.FetchError as exc:
            log.warning("dictation %s fetch failed: %s", request.request_id, exc)
            await self._send_failure(request, "audio_fetch_failed")
        except Exception:
            log.exception("dictation %s transcription failed", request.request_id)
            await self._send_failure(request, "transcription_failed")
        finally:
            try:
                if power_started:
                    await self._power.activity_finished()
            finally:
                if presence_token is not None:
                    self._cm5_presence.release_busy(presence_token)

    async def _send_failure(self, request: DictationRequest,
                            reason: str) -> bool:
        try:
            return await self._send_terminal(
                request, f"dictate fail {request.request_id} {reason}")
        except CommandCancelled:
            log.info("dictation %s failure suppressed after cancellation",
                     request.request_id)
            return False

    async def _send_terminal(self, request: DictationRequest,
                             command: str) -> bool:
        if not self._current(request):
            return False
        try:
            reply = await self._session.command(
                command, expect="status", timeout=_TERMINAL_TIMEOUT_S,
                replay=False, auth_replay=False,
                cancel_guard=lambda: not self._current(request))
        except CommandTimeout as exc:
            # The ID mutation may have committed. Never follow it with a second
            # terminal command or reuse the possibly-tailed session.
            raise LinkClosed("ambiguous dictation terminal command") from exc
        if not reply.ok:
            log.info("dictation %s terminal command rejected: %s",
                     request.request_id, reply.text)
        return reply.ok

    def _remember(self, request_id: str) -> None:
        self._recent[request_id] = None
        self._recent.move_to_end(request_id)
        while len(self._recent) > _RECENT_LIMIT:
            self._recent.popitem(last=False)

    def _cancel_request(self, request_id: str) -> None:
        """Tombstone an ID and flip any active fetch/STT cancellation guard."""
        if self._latest_id == request_id:
            self._latest_id = None
        if self._queue.full():
            queued = self._queue.get_nowait()
            self._queue.task_done()
            if queued.request_id != request_id:
                self._queue.put_nowait(queued)
        self._remember(request_id)
        log.info("dictation request %s cancelled by device", request_id)
