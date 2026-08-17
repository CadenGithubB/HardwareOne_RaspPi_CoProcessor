"""VoicePipeline: the one module that sees the whole exchange.

    trigger -> fetch audio -> STT (executor) -> LLM (stream) -> deliver

Exchanges are serialized end-to-end (matches the firmware chat layer's own
one-generation-at-a-time rule); a queue in the JobSource absorbs bursts.
STT runs in a single-worker ThreadPoolExecutor so the event loop — and
therefore the serial reader's consumer — never blocks on inference (the
hard rule from ARCHITECTURE.md §1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx

from . import bg
from . import deliver as deliver_mod
from . import evenai_protocol as evenai_wire
from .audio import fetch, wav
from .cm5_presence import Cm5Presence, Cm5PresenceMode
from .config import Config
from .jobs import EvenAiCancelled, EvenAiExchange, Job, JobSource
from .link.session import CommandCancelled, LinkClosed, Session

log = logging.getLogger("pipeline")

# Tagged g2evenai askid/replyid text rides ONE command line (cap 2047B incl.
# the command and exchange-ID prefix). Clip word-aware with a marker; LLM answers at
# voice-tuned max_tokens sit far below this — the clip is a safety net.
# Firmware ceiling for the SHOWN ask: the askid text rides ONE sid-0x07 pb
# frame (17 B fixed overhead vs the 253 B single-fragment envelope cap), so
# 237+ UTF-8 bytes doesn't truncate — g2BuildEnvelope fails and the firmware
# TERMINALIZES the whole exchange (EXIT/send_failed): no question, no answer.
# Verified 2026-08-11, docs/EVENAI_ASK_DISPLAY_DEBUG_PLAN.md. The LLM still
# receives the full transcript; only the lens text is clipped.
_ASK_MAX_BYTES = 236

# Streaming REPLY: each replypart is a DELTA the native window appends, and
# the firmware pb builder caps one message's text at ~250B — so parts stay
# under _STREAM_PART_BYTES. Flush policy: open the stream at the first
# sentence end past _STREAM_OPEN_MIN chars (early first paint), then flush
# on sentence ends or forced at _STREAM_FLUSH_CHARS on a word boundary.
# Answers that finish before ever flushing take the validated one-shot
# `g2evenai replyid` path — short answers keep the exact pre-streaming wire
# shape. Chunk glue: a part's leading space survives the trip (firmware
# keeps replypart text untrimmed), so deltas concatenate correctly.
_STREAM_PART_BYTES = 200
_STREAM_OPEN_MIN = 30
_STREAM_FLUSH_CHARS = 140
_SENTENCE_ENDS = (". ", "! ", "? ")

# Probation after the device reboots under us: the CM5 stays off the link
# while the XIAO settles (OTA plan).
_REBOOT_QUIESCE_S = 30.0
# How long a wake stays worth answering, measured from arrival to the START of
# dispatch (not to completion — a slow exchange is still a wanted one). Normal
# dispatch age is milliseconds: the dedupe gate allows one evenai job at a time
# and re-arms in a finally, so nothing queues behind a running exchange. Age
# only grows when something delayed dispatch itself, which today means the
# reboot quiesce. Anything past this has outlived the on-lens session, and
# replying would re-open a card the wearer already dismissed.
_WAKE_STALE_S = 15.0
# A terminal host abort is ID-fenced and best-effort.  It must not hold the
# power lease or the next wearer wake for the generic 65-second command limit.
_EVENAI_ABORT_TIMEOUT_S = 5.0


async def abort_evenai_best_effort(
        session: Session, exchange: EvenAiExchange, reason: str) -> None:
    """Close a native card when the host cannot successfully finish its job.

    No cancel guard is used: host-local failure marks the exchange cancelled
    before sending, while the exact ID is the firmware race fence.  An already
    dismissed or superseded ID therefore rejects harmlessly.  replay=False is
    required because a timed-out terminal mutation must not be blind-replayed.
    """
    exchange.cancel(reason)
    command = evenai_wire.exit_command(exchange.exchange_id)
    try:
        rep = await session.command(
            command, expect="status", timeout=_EVENAI_ABORT_TIMEOUT_S,
            replay=False)
    except Exception as exc:
        log.warning("EvenAI %s host abort failed (%s): %s",
                    exchange.exchange_id, reason, exc)
        return
    if rep.ok:
        log.info("EvenAI %s host abort submitted (%s)",
                 exchange.exchange_id, reason)
    else:
        # Expected when the wearer/native firmware already made this ID
        # terminal; the ID fence proves no newer session was touched.
        log.info("EvenAI %s host abort rejected as already terminal (%s): %s",
                 exchange.exchange_id, reason, rep.text)


@dataclass
class _TapWindow:
    """One opt-in observer of a naturally occurring cancellation window.

    The task only writes host logs.  ``stop`` is separate from the exchange's
    cancel event so the normal next action can close the window without
    mutating exchange state.
    """

    stage: str
    started_ns: int
    stop: asyncio.Event
    task: asyncio.Task | None = None
    end_reason: str = "next_action"
    stopped_ns: int | None = None


@dataclass
class _EvenAiDelivery:
    """Per-exchange state; no delayed S1 task can mutate S2's barrier."""

    ask_task: asyncio.Task | None = None
    ask_render_until: float | None = None
    question_tap: _TapWindow | None = None
    answer_tap: _TapWindow | None = None
    answer_started: bool = False
    # Ask-display instrumentation (docs/EVENAI_ASK_DISPLAY_DEBUG_PLAN.md
    # item 2): retained past the hold so the timings line and the corpus can
    # report the question's real on-lens window. hold_remain_s keeps its sign —
    # negative means the LLM already overspent the render budget by that much.
    question_bytes: int = 0
    ask_clipped: bool = False
    ask_submit_t: float | None = None
    ask_ack_t: float | None = None
    first_reply_t: float | None = None
    hold_remain_s: float | None = None


def _one_line(text: str) -> str:
    """Collapse whitespace/newlines: the wire protocol is line-framed and the
    glasses window wraps text itself."""
    return " ".join(text.split())


def _clip_ask(text: str) -> tuple[str, bool]:
    """Clip the shown ask to the firmware's 236-byte frame ceiling.

    Returns (shown, clipped). Word-boundary clip with 3 bytes reserved for
    the ellipsis, so the result is always <= _ASK_MAX_BYTES; a text at
    exactly the ceiling passes through untouched."""
    if len(text.encode("utf-8")) <= _ASK_MAX_BYTES:
        return text, False
    chunks = deliver_mod.chunk_text(text, _ASK_MAX_BYTES - 3)
    return ((chunks[0] + "…") if chunks else "", True)


def _flush_point(acc: str, sent: int, *, force: bool) -> int | None:
    """Absolute index to cut the next streamed delta at, or None to keep
    accumulating. Cuts land BEFORE the boundary space so the glue travels
    at the front of the next chunk.

    Opener (force=False): wait for a sentence end past _STREAM_OPEN_MIN
    pending chars — a short single-sentence answer never opens a stream and
    rides the validated one-shot path instead. Opened (force=True): flush
    at every sentence end. Either way, a run of _STREAM_FLUSH_CHARS with no
    sentence end forces a cut at the last word boundary."""
    pending = acc[sent:]
    best = -1
    for mark in _SENTENCE_ENDS:
        idx = pending.rfind(mark)
        if idx >= 0:
            best = max(best, idx + len(mark) - 1)   # index of the boundary space
    if best >= 0 and (force or best >= _STREAM_OPEN_MIN):
        return sent + best
    if len(pending) >= _STREAM_FLUSH_CHARS:
        sp = pending.rfind(" ", 0, _STREAM_FLUSH_CHARS)
        return sent + (sp if sp > 0 else _STREAM_FLUSH_CHARS)
    return None


class VoicePipeline:
    def __init__(self, session: Session, stt_engine, llm_client, cfg: Config,
                 *, power_activity=None, live_gate=None,
                 cancel_marker_interval_s: float = 0.0,
                 cm5_presence: Cm5Presence | None = None):
        self._session = session
        self._stt = stt_engine
        self._llm = llm_client
        self._cfg = cfg
        # Optional CM5 profile policy.  Kept as a tiny duck-typed interface so
        # the voice pipeline does not own or import the privileged subsystem.
        self._power_activity = power_activity
        # Optional Gate E live-STT gate: wake exchanges try the streamed
        # transcript first and fall back to the fetched-WAV batch path on any
        # live failure. None → the pre-Gate-E pipeline, byte for byte.
        self._live_gate = live_gate
        self._cancel_marker_interval_s = cancel_marker_interval_s
        self._cm5_presence = cm5_presence
        self._stt_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")

    async def close(self) -> None:
        # Give a backgrounded replyend/micdelete its chance to land before the
        # transport goes away — otherwise shutdown turns a normal exchange tail
        # into a spurious link error.
        await bg.drain(2.0)
        self._stt_pool.shutdown(wait=False, cancel_futures=True)

    # -- opt-in wearer cancellation markers ------------------------------

    def _start_tap_window(
            self, exchange: EvenAiExchange, stage: str) -> _TapWindow | None:
        """Start a log-only observer; never gate or sleep pipeline work.

        Stage windows measure UNCONDITIONALLY (the "<<< TAP WINDOW END >>>"
        line is the per-stage duration instrument — ask-display plan item 3);
        only the periodic ">>> TAP NOW <<<" wearer markers remain opt-in via
        the cancel-marker interval."""
        interval = self._cancel_marker_interval_s
        markers = interval > 0
        window = _TapWindow(stage, time.monotonic_ns(), asyncio.Event())
        if markers:
            log.warning(
                ">>> TAP NOW <<< evenai=%s stage=%s window=start "
                "start_ns=%d elapsed_ms=0",
                exchange.exchange_id, stage, window.started_ns)
        window.task = exchange.start_task(
            self._run_tap_window(
                exchange, window, interval if markers else None),
            name=f"evenai-tap-{stage}-{exchange.exchange_id}")
        return window

    async def _run_tap_window(
            self, exchange: EvenAiExchange, window: _TapWindow,
            interval: float | None) -> None:
        stop_wait = asyncio.create_task(window.stop.wait())
        cancel_wait = asyncio.create_task(exchange.cancel_event.wait())
        interrupted = False
        try:
            while True:
                # interval None (markers off) blocks until stop/cancel: the
                # window still measures, it just never emits wearer markers.
                done, _pending = await asyncio.wait(
                    (stop_wait, cancel_wait), timeout=interval,
                    return_when=asyncio.FIRST_COMPLETED)
                if done:
                    break
                elapsed_ms = round(
                    (time.monotonic_ns() - window.started_ns) / 1_000_000)
                log.warning(
                    ">>> TAP NOW <<< evenai=%s stage=%s window=open "
                    "elapsed_ms=%d",
                    exchange.exchange_id, window.stage, elapsed_ms)
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            for task in (stop_wait, cancel_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_wait, cancel_wait, return_exceptions=True)
            natural_ns = window.stopped_ns
            cancel_ns = exchange.cancelled_ns
            cancelled_first = (
                cancel_ns is not None
                and (natural_ns is None or cancel_ns <= natural_ns))
            stopped_ns = (
                cancel_ns if cancelled_first else
                natural_ns if natural_ns is not None else
                time.monotonic_ns())
            elapsed_ms = round(
                (stopped_ns - window.started_ns) / 1_000_000)
            outcome = (
                f"cancel:{exchange.cancel_reason or 'unknown'}"
                if cancelled_first else
                "interrupted" if interrupted else window.end_reason)
            # WARNING in marker (triage) mode to stand out beside the TAP NOW
            # lines; INFO for the always-on measurement so journals stay calm.
            (log.warning if interval else log.info)(
                "<<< TAP WINDOW END >>> evenai=%s stage=%s start_ns=%d "
                "stop_ns=%d elapsed_ms=%d outcome=%s",
                exchange.exchange_id, window.stage, window.started_ns, stopped_ns,
                elapsed_ms, outcome)

    def _stop_tap_window(
            self, window: _TapWindow | None, reason: str) -> None:
        """Close a diagnostic window without yielding pipeline control.

        The exchange's existing final task drain collects the observer.  That
        keeps terminal logging out of the critical first-reply/finalizer path.
        """
        if window is None:
            return
        window.end_reason = reason
        if window.stopped_ns is None:
            # Measure the stage boundary synchronously. The background logger
            # may not run until a later loop turn under load.
            window.stopped_ns = time.monotonic_ns()
        window.stop.set()

    # -- exchanges ---------------------------------------------------------

    async def run_ask(self) -> str:
        """Full voice exchange. Returns the answer text (also delivered).

        One-engine degradation (small-RAM configs): with no STT the command
        explains itself; with no LLM (configured 'none' OR the server gave
        up/is unreachable) the transcript itself is delivered — voice notes
        instead of a crashed exchange."""
        if self._stt is None:
            msg = "voice input disabled (stt.engine: none) — use `chat` instead"
            log.warning(msg)
            return msg
        t0 = time.monotonic()
        wav_bytes = await fetch.record_utterance(self._session, self._cfg.audio)
        t_fetch = time.monotonic()

        parsed = wav.parse(wav_bytes)
        wav.require_canonical(parsed)
        self._report_audio(wav_bytes, parsed)

        loop = asyncio.get_running_loop()
        transcript = await loop.run_in_executor(
            self._stt_pool, self._stt.transcribe, parsed.pcm, parsed.rate)
        t_stt = time.monotonic()
        log.info("transcript: %r", transcript)
        if not transcript.strip():
            await deliver_mod.deliver(self._session, self._cfg.deliver,
                                      "(heard nothing)")
            return ""

        if self._llm is None:
            answer = f"Heard: {transcript}"
            await deliver_mod.deliver(self._session, self._cfg.deliver, answer)
        else:
            try:
                answer = await self._answer(transcript)
            except (httpx.HTTPError, ConnectionError, RuntimeError) as exc:
                # LLM died (OOM-killed llama-server is the classic) — degrade
                # to transcript delivery rather than failing the exchange.
                log.warning("LLM unavailable (%s) — delivering transcript only", exc)
                answer = f"(assistant offline) heard: {transcript}"
                await deliver_mod.deliver(self._session, self._cfg.deliver, answer)
        log.info("timings: fetch=%.1fs stt=%.1fs llm+deliver=%.1fs total=%.1fs",
                 t_fetch - t0, t_stt - t_fetch, time.monotonic() - t_stt,
                 time.monotonic() - t0)
        return answer

    async def run_chat(self, prompt: str) -> str:
        """Text-only exchange (no mic) — proves LLM + delivery on their own."""
        if self._llm is None:
            msg = "LLM disabled (llm.engine: none) — only voice transcription runs"
            log.warning(msg)
            return msg
        return await self._answer(prompt)

    async def run_evenai(self, exchange: EvenAiExchange) -> str:
        """Wake-triggered exchange ("Hey Even"): the firmware already started
        a VAD-endpointed capture and pushed evenai_wake. Await + fetch it,
        then answer INTO the native EvenAI windows — `g2evenai askid` paints the
        transcript on the listening popup, `g2evenai replyid` the answer in the
        response window. The C0 oled/g2notify targets are deliberately NOT
        used here: the native UI is the delivery surface."""
        if self._stt is None:
            log.warning("evenai wake but stt.engine=none — cannot transcribe")
            return ""
        t0 = time.monotonic()
        exchange.raise_if_cancelled()

        live = None
        if self._live_gate is not None:
            live_tap = self._start_tap_window(exchange, "capture/live")
            live_outcome_label = "interrupted"
            try:
                live = await self._live_gate.capture(
                    exchange, vad_max_seconds=self._cfg.audio.vad_max_seconds)
                live_outcome_label = (
                    "live_stt" if live.valid else f"live_miss:{live.reason}")
            finally:
                self._stop_tap_window(live_tap, live_outcome_label)
            exchange.raise_if_cancelled()

        if live is not None and live.valid:
            # Streamed transcript: the WAV never crossed the wire. Build a
            # canonical local WAV from the streamed PCM so persistence,
            # level diagnostics, and the empty-transcript archive keep exact
            # parity with the batch path.
            wav_bytes = wav.build(live.pcm, live.sample_rate)
            parsed = wav.parse(wav_bytes)
            wav.require_canonical(parsed)
            self._report_audio(wav_bytes, parsed, persist=False)
            t_fetch = t_stt = time.monotonic()
            log.info(
                "live stt: %r (%.2fs audio, end→final %.2fs%s)",
                live.text,
                (len(live.pcm) / 2.0) / live.sample_rate if live.sample_rate else 0.0,
                live.end_to_final_s if live.end_to_final_s is not None else -1.0,
                f", dropped leading {live.dropped_leading!r}"
                if live.dropped_leading else "")
            # DIAG (temporary): does the richest running partial exceed the
            # stop() transcript? If yes, stop() dropped an uncommitted tail
            # (fixable here); if the richest partial is also short, the model
            # itself capped. Also dump the stop-line segment structure.
            _diag = (live.worker_result or {}).get("stream") or {}
            _richest = ""
            for _p in _diag.get("partials") or []:
                _t = str(_p.get("text") or "")
                if len(_t) > len(_richest):
                    _richest = _t
            _stop_lines = _diag.get("stop_lines") or []
            log.info(
                "live stt DIAG: updates=%s richest_partial=%r | stop=%r | "
                "stop_lines=%d [%s]",
                _diag.get("partial_updates"), _richest, live.raw_text,
                len(_stop_lines),
                " || ".join(str(_l.get("text") or "") for _l in _stop_lines))
            # The device-side WAV is now redundant: delete it off the critical
            # path, exchange-owned so dispatch drains it before S2.
            exchange.start_task(
                self._cleanup_live_recording(exchange),
                name=f"evenai-live-cleanup-{exchange.exchange_id}")
            # Corpus capture moved post-delivery (end of run_evenai) so
            # ask/reply timing rides the sample — plan item 4.
            transcript = _one_line(live.text)
        else:
            if live is not None and live.lane_active:
                log.warning("live STT unavailable (%s) — using fetched WAV",
                            live.reason)
                # Free the firmware's framed lane before voicefetch needs it.
                await self._live_gate.abort_stream(self._session, exchange)
                exchange.raise_if_cancelled()
            capture_tap = self._start_tap_window(exchange, "capture/fetch")
            capture_outcome = "interrupted"
            try:
                wav_bytes = await fetch.fetch_wake_utterance(
                    self._session, self._cfg.audio, exchange)
                capture_outcome = "audio_fetched"
            finally:
                self._stop_tap_window(capture_tap, capture_outcome)
            t_fetch = time.monotonic()

            exchange.raise_if_cancelled()
            parsed = wav.parse(wav_bytes)
            wav.require_canonical(parsed)
            self._report_audio(wav_bytes, parsed, persist=False)

            loop = asyncio.get_running_loop()
            stt_tap = self._start_tap_window(exchange, "stt")
            stt_outcome = "interrupted"
            try:
                transcript = await loop.run_in_executor(
                    self._stt_pool, self._stt.transcribe, parsed.pcm, parsed.rate)
                stt_outcome = "stt_complete"
            finally:
                self._stop_tap_window(stt_tap, stt_outcome)
            t_stt = time.monotonic()
            # Native inference is cancel-opaque. Awaiting the sole worker and then
            # discarding is intentional: returning sooner would not let S2 run STT,
            # and would release the power lease while S1 still consumes the CPU.
            exchange.raise_if_cancelled()
            # Per-second-of-audio, because raw STT seconds are meaningless across
            # captures of different lengths — that is what made two sessions look
            # incomparable earlier. This ratio is also a clean throttling detector:
            # moonshine is ONNX and compute-bound, so it degrades with CPU clock
            # exactly like LLM prefill does.
            audio_s = (len(parsed.pcm) / 2.0) / parsed.rate if parsed.rate else 0.0
            log.info("stt: %.2fs audio in %.2fs (%.3f s/s)",
                     audio_s, t_stt - t_fetch,
                     (t_stt - t_fetch) / audio_s if audio_s > 0 else 0.0)
            transcript = _one_line(transcript)
        log.info("wake transcript: %r", transcript)
        if not transcript:
            await self._display_command(
                exchange,
                evenai_wire.reply_command(
                    exchange.exchange_id, "Sorry, I didn't catch that."))
            exchange.raise_if_cancelled()
            exchange.mark_delivered()
            self._persist_utterance(wav_bytes)
            # Keep evidence only for a still-owned exchange whose nag was
            # accepted. Dismissed speech is canceled user data, not a failed-STT
            # diagnostic to persist under failed/.
            self._archive_failed_utterance(wav_bytes, parsed)
            return ""

        shown, ask_clipped = _clip_ask(transcript)
        if ask_clipped:
            log.warning(
                "ask clipped for the lens: transcript %dB > %dB firmware "
                "ceiling (unclipped it would terminalize the exchange); "
                "LLM still sees the full transcript",
                len(transcript.encode("utf-8")), _ASK_MAX_BYTES)
        # Issue the ask CONCURRENTLY with generation instead of ahead of it.
        # The lens needs the question before the first reply chunk, not before
        # the LLM starts — and _hold_for_ask_render is already the barrier that
        # enforces exactly that, so it is the natural place to collect this.
        # The ~0.3s UART round trip therefore hides under prefill (ttft MEASURED
        # 0.48-0.91s) rather than preceding it.
        delivery = _EvenAiDelivery()
        delivery.question_bytes = len(shown.encode("utf-8"))
        delivery.ask_clipped = ask_clipped
        delivery.ask_task = asyncio.create_task(
            self._send_ask(shown, exchange, delivery),
            name=f"evenai-ask-{exchange.exchange_id}")

        t_paint: float | None = None
        try:
            if self._llm is None:
                answer = f"Heard: {transcript}"
                await self._send_reply_whole(answer, exchange, delivery)
            else:
                try:
                    answer, t_paint = await self._stream_reply(
                        transcript, exchange, delivery)
                except (httpx.HTTPError, ConnectionError, RuntimeError) as exc:
                    log.warning("LLM unavailable (%s) — replying with transcript", exc)
                    answer = f"(assistant offline) heard: {transcript}"
                    await self._send_reply_whole(answer, exchange, delivery)
        finally:
            # Every reply path goes through _hold_for_ask_render, which consumes
            # the task — but an unexpected raise must not leave it dangling.
            await self._drain_ask(delivery)
        exchange.raise_if_cancelled()
        # From this point the complete answer (including replyend for a stream)
        # was accepted by the matching native session. Persistence/history are
        # local post-commit diagnostics and must not turn that success into an
        # EXIT if they fail independently.
        exchange.mark_delivered()
        self._persist_utterance(wav_bytes)
        commit = getattr(self._llm, "commit_turn", None)
        if commit is not None:
            commit(transcript, answer)
        if self._cfg.stt.live_debug_capture:
            # Captured POST-delivery (both paths) so the ask/reply timing
            # rides the corpus; a cancelled/failed exchange keeps no sample,
            # matching the dismissed-speech-is-cancelled-data rule.
            exchange.start_task(
                self._capture_corpus_sample(
                    exchange, wav_bytes,
                    live=live if (live is not None and live.valid) else None,
                    delivery=delivery, transcript=transcript,
                    audio_seconds=(
                        (len(parsed.pcm) / 2.0) / parsed.rate
                        if parsed.rate else 0.0),
                    sample_rate=parsed.rate),
                name=f"evenai-corpus-{exchange.exchange_id}")
        t_done = time.monotonic()
        ask_gap = (
            delivery.first_reply_t - delivery.ask_ack_t
            if delivery.first_reply_t is not None
            and delivery.ask_ack_t is not None else None)
        log.info("evenai timings: fetch=%.1fs stt=%.1fs llm+reply=%.1fs "
                 "first_paint=%s total=%.1fs ask_gap=%s hold_remain=%s "
                 "question=%dB%s",
                 t_fetch - t0, t_stt - t_fetch, t_done - t_stt,
                 f"{t_paint - t_stt:.1f}s" if t_paint is not None else "(one-shot)",
                 t_done - t0,
                 f"{ask_gap:.2f}s" if ask_gap is not None else "n/a",
                 f"{delivery.hold_remain_s:.2f}s"
                 if delivery.hold_remain_s is not None else "n/a",
                 delivery.question_bytes,
                 " clipped" if delivery.ask_clipped else "")
        return answer

    def _live_debug_dir(self) -> Path:
        raw = self._cfg.stt.live_debug_dir.strip()
        if raw:
            return Path(raw).expanduser()
        return Path.home() / ".cache" / "hw1-ai-service" / "live-corpus"

    async def _capture_corpus_sample(
            self, exchange: EvenAiExchange, wav_bytes: bytes, *,
            live=None, delivery: _EvenAiDelivery | None = None,
            transcript: str = "", audio_seconds: float = 0.0,
            sample_rate: int = 16000) -> None:
        """Persist one wake exchange (audio + STT snapshot + delivery timing)
        to the debug corpus. Live exchanges carry the full worker snapshot;
        batch exchanges are captured too (snapshot None) so ceiling/timing
        questions are answerable for EVERY wake. Gated by
        stt.live_debug_capture; best-effort, never affects the exchange."""
        try:
            def _gap(a: float | None, b: float | None) -> float | None:
                return round(b - a, 3) if a is not None and b is not None \
                    else None

            d = delivery
            sample = {
                "schema_version": 2,
                "exchange_id": exchange.exchange_id,
                "path": "live" if live is not None else "batch",
                "sample_rate": (
                    live.sample_rate if live is not None else sample_rate),
                "audio_seconds": (
                    (len(live.pcm) / 2.0) / live.sample_rate
                    if live is not None and live.sample_rate
                    else audio_seconds),
                "transcript": live.text if live is not None else transcript,
                "question": {
                    "shown_bytes": d.question_bytes if d else None,
                    "clipped": d.ask_clipped if d else None,
                },
                "delivery": {
                    "ask_rtt_s": _gap(d.ask_submit_t, d.ask_ack_t),
                    "ask_gap_s": _gap(d.ask_ack_t, d.first_reply_t),
                    "hold_remain_s": (
                        round(d.hold_remain_s, 3)
                        if d.hold_remain_s is not None else None),
                } if d else None,
                "config": {
                    "model_dir": self._cfg.stt.live_model_dir,
                    "model_arch": self._cfg.stt.live_model_arch,
                    "update_interval_s": self._cfg.stt.live_update_interval_s,
                    "queue_chunks": self._cfg.stt.live_queue_chunks,
                    "final_timeout_s": self._cfg.stt.live_final_timeout_s,
                    "wake_stream_timeout_s":
                        self._cfg.stt.live_wake_stream_timeout_s,
                },
            }
            if live is not None:
                snap = live.worker_result or {}
                stream = snap.get("stream") or {}
                sample.update({
                    "raw_text": live.raw_text,
                    "dropped_leading": live.dropped_leading,
                    "end_to_final_s": live.end_to_final_s,
                    "final_rescue_mode": stream.get("final_rescue_mode"),
                    "snapshot": snap,
                })
            else:
                sample["snapshot"] = None
            await asyncio.to_thread(
                self._write_corpus_sample, exchange.exchange_id, wav_bytes,
                sample)
        except Exception:
            log.exception("corpus capture failed (non-fatal)")

    def _write_corpus_sample(
            self, exchange_id: str, wav_bytes: bytes, sample: dict) -> None:
        directory = self._live_debug_dir()
        directory.mkdir(parents=True, exist_ok=True)
        base = f"{int(time.time())}_{exchange_id}"
        (directory / f"{base}.wav").write_bytes(wav_bytes)
        (directory / f"{base}.json").write_text(
            json.dumps(sample, indent=2, default=str))
        log.info("corpus: wrote %s.{wav,json} -> %s", base, directory)

    async def _cleanup_live_recording(self, exchange: EvenAiExchange) -> None:
        """Delete the device-side WAV a live-transcribed exchange never fetched.

        Best-effort and exchange-owned (dispatch drains it before S2). The
        terminal EVT usually lands within ~150 ms of the live END; if it was
        lost, an ID-scoped stop learns the path — with the 5 s cleanup budget,
        never the generic 65 s ceiling (fetch.py's documented rule)."""
        try:
            try:
                await asyncio.wait_for(exchange.terminal_event.wait(), 3.0)
            except asyncio.TimeoutError:
                pass
            path = exchange.recording_path
            if path is None:
                rep = await self._session.command(
                    evenai_wire.mic_stop_command(exchange.exchange_id),
                    expect="status", timeout=5.0, replay=False)
                if rep.ok and "discarded" in rep.text.lower():
                    return          # dismissal already deleted the recording
                if rep.ok:
                    path = fetch._parse_recording_path(rep)
            if path:
                await fetch._cleanup(
                    self._session, path, exchange.exchange_id)
        except Exception as exc:
            log.warning("live-path recording cleanup failed "
                        "(stray WAV may remain): %s", exc)

    def _archive_failed_utterance(self, wav_bytes: bytes, parsed) -> None:
        """Park an empty-transcript capture under <save_last_path>/../failed/.

        The 1280 ms arithmetic minimum (41004 B at 16 kHz mono 16-bit) is the
        signature worth grepping for: it means the firmware VAD auto-stopped
        after one full trailing-silence window, which can only happen once
        speech latched. Named with the duration so that stands out in ls.
        """
        path_cfg = self._cfg.audio.save_last_path
        if not path_cfg:
            return
        import os
        import time as _time
        from pathlib import Path
        dur_ms = round(len(parsed.pcm) / 2 / parsed.rate * 1000)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        dest = (Path(os.path.expanduser(path_cfg)).parent / "failed"
                / f"empty-{stamp}-{dur_ms}ms-{len(wav_bytes)}B.wav")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(wav_bytes)
            log.info("empty transcript — capture kept: %s", dest)
        except OSError as exc:
            log.warning("could not archive failed utterance: %s", exc)

    def _report_audio(self, wav_bytes: bytes, parsed, *, persist: bool = True) -> None:
        """Level readout + save-to-disk: the two diagnostics that separate
        'the mic heard nothing' from 'STT failed on real audio'."""
        import array
        import math
        samples = array.array("h")
        samples.frombytes(parsed.pcm)
        if samples:
            peak = max(abs(s) for s in samples)
            rms = math.sqrt(sum(s * s for s in samples) / len(samples))
            rms_db = 20 * math.log10(max(rms, 1) / 32768)
            peak_db = 20 * math.log10(max(peak, 1) / 32768)
            note = ""
            if peak_db > -1:
                note = " (CLIPPING — lower micgain on the device)"
            elif rms_db < -50:
                note = " (very quiet — near-silence; speak closer / check timing)"
            log.info("audio level: RMS %.0f dBFS, peak %.0f dBFS%s",
                     rms_db, peak_db, note)
        if persist:
            self._persist_utterance(wav_bytes)

    def _persist_utterance(self, wav_bytes: bytes) -> None:
        path_cfg = self._cfg.audio.save_last_path
        if path_cfg:
            import os
            from pathlib import Path
            path = Path(os.path.expanduser(path_cfg))
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(wav_bytes)
                log.info("utterance saved: %s", path)
            except OSError as exc:
                log.warning("could not save utterance: %s", exc)

    async def _generate(self, prompt: str) -> str:
        """LLM only — no delivery. run_evenai owns its own delivery surface."""
        parts: list[str] = []
        async for piece in self._llm.ask_stream(prompt):
            parts.append(piece)
        return "".join(parts).strip()

    # -- native-window reply delivery -------------------------------------

    async def _stream_reply(
            self, prompt: str, exchange: EvenAiExchange,
            delivery: _EvenAiDelivery) -> tuple[str, float | None]:
        """Stream LLM output into the native response window as it
        generates: `g2evenai replypartid` per flushed delta, `replyendid` to
        finalize. First words land at ~time-to-first-sentence instead of at
        end of generation. Returns (full answer, first-paint time or None).

        Generation never stalls on the UART round trips: while a part
        command is in flight the HTTP stream simply buffers, and the next
        loop iteration drains it. An answer that completes before the first
        flush is delivered via the pre-streaming one-shot path unchanged."""
        acc = ""
        sent = 0            # chars of acc already delivered
        opened = False
        t_paint: float | None = None
        stream_kwargs = ({"commit_history": False}
                         if hasattr(self._llm, "commit_turn") else {})
        stream = self._llm.ask_stream(prompt, **stream_kwargs)
        try:
            async for piece in self._cancel_aware_stream(stream, exchange):
                acc += piece
                while True:
                    cut = _flush_point(acc, sent, force=opened)
                    if cut is None:
                        break
                    part_paint = await self._send_part(
                        acc[sent:cut], exchange, delivery)
                    if part_paint is not None and t_paint is None:
                        t_paint = part_paint
                    opened = True
                    sent = cut
            answer = acc.strip()
            if not opened:
                await self._send_reply_whole(answer, exchange, delivery)
                return (answer or "(no answer)"), None
            remainder = acc[sent:]
            if remainder.strip():
                await self._send_part(remainder, exchange, delivery)
            tap, delivery.answer_tap = delivery.answer_tap, None
            self._stop_tap_window(tap, "before_replyend_attempt")
            await self._end_reply(exchange)
            return (answer or "(no answer)"), t_paint
        finally:
            tap, delivery.answer_tap = delivery.answer_tap, None
            self._stop_tap_window(tap, "interrupted")
            if tap is not None:
                # A handled generation failure may fall back to multipart
                # transcript delivery. That replacement gets its own tail cue.
                delivery.answer_started = False

    async def _cancel_aware_stream(self, stream, exchange: EvenAiExchange):
        """Observe dismissal while httpx is waiting for its next SSE delta."""
        iterator = stream.__aiter__()
        next_piece: asyncio.Task | None = None
        cancelled: asyncio.Task | None = None
        try:
            while True:
                exchange.raise_if_cancelled()
                next_piece = asyncio.create_task(anext(iterator))
                cancelled = asyncio.create_task(exchange.cancel_event.wait())
                done, pending = await asyncio.wait(
                    (next_piece, cancelled), return_when=asyncio.FIRST_COMPLETED)
                if cancelled in done:
                    next_piece.cancel()
                    await asyncio.gather(next_piece, return_exceptions=True)
                    exchange.raise_if_cancelled()
                cancelled.cancel()
                await asyncio.gather(cancelled, return_exceptions=True)
                try:
                    yield next_piece.result()
                except StopAsyncIteration:
                    return
        finally:
            # External task cancellation (daemon/link shutdown) can land while
            # anext and the cancel waiter are both pending. Collect them before
            # aclose; otherwise an async generator rejects aclose as "already
            # running" and the HTTP stream/task leaks past this exchange.
            children = tuple(
                task for task in (next_piece, cancelled) if task is not None)
            for task in children:
                if not task.done():
                    task.cancel()
            if children:
                await asyncio.gather(*children, return_exceptions=True)
            close = getattr(iterator, "aclose", None)
            if close is not None:
                await close()

    async def _end_reply(self, exchange: EvenAiExchange) -> None:
        # Session-dependent tails are deliberately job-owned. A detached S1
        # replyend could otherwise acquire the UART lock during S2.
        await self._display_command(
            exchange, evenai_wire.replyend_command(exchange.exchange_id))

    async def _send_ask(self, shown: str, exchange: EvenAiExchange,
                        delivery: _EvenAiDelivery) -> None:
        """Paint the question on the listening popup. Runs as a task so the
        round trip overlaps generation; _hold_for_ask_render awaits it."""
        delivery.ask_submit_t = time.monotonic()
        await self._display_command(
            exchange, evenai_wire.ask_command(exchange.exchange_id, shown))
        # Start the render clock from the ACK: that is when the lens has the
        # text and begins drawing it. Set here rather than at submit time so an
        # ask delayed behind another command does not eat its own render budget.
        # Budget = start-margin (paint-start jitter, worse under load) +
        # len/cps (draw time, calibrated worst case), floored at min_dwell so
        # a short fully-drawn question still gets reading time. cps=0 disables.
        now = time.monotonic()
        delivery.ask_ack_t = now
        d = self._cfg.deliver
        if d.g2_ask_render_cps > 0:
            budget = max(
                d.g2_ask_render_start_margin_s
                + len(shown) / d.g2_ask_render_cps,
                d.g2_ask_min_dwell_s)
            delivery.ask_render_until = now + budget
        else:
            delivery.ask_render_until = None
        delivery.question_tap = self._start_tap_window(exchange, "question")

    async def _drain_ask(self, delivery: _EvenAiDelivery) -> None:
        """Cleanup-path collection: only reached when a reply path never got as
        far as its first write, so swallow rather than mask the real error."""
        task, delivery.ask_task = delivery.ask_task, None
        try:
            if task is not None:
                await task
        except EvenAiCancelled:
            raise
        except Exception as exc:
            log.warning("g2evenai askid errored: %s", exc)
        finally:
            tap, delivery.question_tap = delivery.question_tap, None
            self._stop_tap_window(tap, "ask_cleanup")

    async def _hold_for_ask_render(
            self, exchange: EvenAiExchange,
            delivery: _EvenAiDelivery) -> None:
        """Block until the lens has had time to finish drawing the question.

        The G2 renders the ASK text progressively and the first reply chunk
        REPLACES it, so a fast answer to a long question truncates the question
        mid-word. MEASURED: a 104-char question was cut at ~char 81 when the
        first chunk arrived 1.84 s after the ask.

        Deliberately awaited HERE — immediately before the first reply write —
        and not before generation starts. That way the LLM's own latency counts
        toward the render window, so this is a no-op on every exchange where
        the model took longer to answer than the lens took to draw. It only
        ever engages when the reply would otherwise trample the question.

        Fires at most once per exchange; both the streaming and one-shot reply
        paths call it, since both replace the question.

        This is also the ordering barrier for the overlapped `g2evenai askid`:
        the question must be on the lens before anything replaces it. Pop the
        task BEFORE awaiting so a raise (link loss) leaves nothing dangling,
        and let it propagate — that is what the inline ask used to do.
        """
        exchange.raise_if_cancelled()
        task, delivery.ask_task = delivery.ask_task, None
        tap_outcome = "interrupted"
        try:
            if task is not None:
                await task
            exchange.raise_if_cancelled()
            if delivery.ask_render_until is None:
                tap_outcome = "before_first_reply_write"
                return
            remain = delivery.ask_render_until - time.monotonic()
            # Retained BEFORE the clock is nulled (plan item 2): negative
            # remain = the LLM already overspent the render budget by that
            # much — the "barely flashes" signal the timings line surfaces.
            delivery.hold_remain_s = remain
            delivery.ask_render_until = None
            if remain > 0:
                log.info("holding first reply %.2fs so the lens can finish the question",
                         remain)
                await exchange.sleep(remain)
            tap_outcome = "before_first_reply_write"
        finally:
            tap, delivery.question_tap = delivery.question_tap, None
            self._stop_tap_window(tap, tap_outcome)

    async def _send_physical_part(
            self, text: str, exchange: EvenAiExchange,
            delivery: _EvenAiDelivery) -> float:
        """Send one physical append and open the tail window after its ACK.

        A logical delta may exceed the protobuf text limit and split into
        several physical ``replypartid`` writes.  Starting the wearer cue in
        this shared helper guarantees that it appears after the first accepted
        physical part, not after the whole logical delta has already landed.
        """
        await self._display_command(
            exchange,
            evenai_wire.replypart_command(exchange.exchange_id, text))
        accepted_at = time.monotonic()
        if delivery.first_reply_t is None:
            delivery.first_reply_t = accepted_at
        if not delivery.answer_started:
            delivery.answer_started = True
            delivery.answer_tap = self._start_tap_window(
                exchange, "answer_tail")
        return accepted_at

    async def _send_part(self, chunk: str, exchange: EvenAiExchange,
                         delivery: _EvenAiDelivery) -> float | None:
        """One delta to the window. Preserves the chunk's leading space (the
        inter-chunk glue); collapses internal newlines; splits anything over
        the per-message byte cap on word boundaries."""
        lead = " " if chunk[:1].isspace() else ""
        body = _one_line(chunk)
        if not body:
            return None
        await self._hold_for_ask_render(exchange, delivery)
        first_accepted_at: float | None = None
        for i, piece in enumerate(deliver_mod.chunk_text(body, _STREAM_PART_BYTES)):
            glue = lead if i == 0 else " "
            accepted_at = await self._send_physical_part(
                f"{glue}{piece}", exchange, delivery)
            if first_accepted_at is None:
                first_accepted_at = accepted_at
        return first_accepted_at

    async def _send_reply_whole(
            self, text: str, exchange: EvenAiExchange,
            delivery: _EvenAiDelivery) -> None:
        """Non-streamed delivery without silent truncation: one-shot `reply`
        when the text fits a single pb message, else parts + finalize."""
        text = _one_line(text) or "(no answer)"
        # This path replaces the question too, and is the FASTER of the two —
        # a short answer that never opened the stream lands here.
        await self._hold_for_ask_render(exchange, delivery)
        pieces = deliver_mod.chunk_text(text, _STREAM_PART_BYTES)
        if len(pieces) == 1:
            await self._display_command(
                exchange,
                evenai_wire.reply_command(exchange.exchange_id, pieces[0]))
            if delivery.first_reply_t is None:
                delivery.first_reply_t = time.monotonic()
            return
        try:
            for i, piece in enumerate(pieces):
                await self._send_physical_part(
                    f"{' ' if i else ''}{piece}", exchange, delivery)
            tap, delivery.answer_tap = delivery.answer_tap, None
            self._stop_tap_window(tap, "before_replyend_attempt")
            await self._end_reply(exchange)
        finally:
            tap, delivery.answer_tap = delivery.answer_tap, None
            self._stop_tap_window(tap, "interrupted")

    async def _display_command(self, exchange: EvenAiExchange,
                               command: str) -> None:
        """One fail-closed display mutation.

        These commands are not blindly replayed: replypart is an append delta,
        so an executed command with a lost ACK would duplicate text. Firmware's
        ID check remains the final fence against a dismissal racing this ACK.
        """
        exchange.raise_if_cancelled()
        try:
            rep = await self._session.command(
                command, expect="status", replay=False,
                cancel_guard=lambda: exchange.cancelled)
        except CommandCancelled:
            exchange.raise_if_cancelled()
            raise EvenAiCancelled(
                f"EvenAI {exchange.exchange_id} display command cancelled")
        if not rep.ok:
            exchange.cancel("device_rejected")
            raise EvenAiCancelled(
                f"EvenAI {exchange.exchange_id} rejected display mutation: {rep.text}")

    async def _answer(self, prompt: str) -> str:
        answer = await self._generate(prompt)
        log.info("answer: %r", answer)
        if answer:
            await deliver_mod.deliver(self._session, self._cfg.deliver, answer)
        return answer

    # -- daemon loop -------------------------------------------------------

    async def daemon(self, source: JobSource) -> None:
        # Serve reboot probation HERE, while nobody is waiting on us. Doing it
        # lazily at first dispatch meant the daemon announced itself ready,
        # idled, and then charged the whole 30 s to the first wake — which by
        # then was answering into a card the wearer had already dismissed.
        if self._session.reboot_suspected:
            log.info("device reboot seen — serving probation before accepting jobs")
            if self._cm5_presence is not None:
                self._cm5_presence.link_reset()
                # Revoke an already-acknowledged READY lease before quieting
                # the wire. A local desired-state change alone would leave
                # firmware admitting wakes until its old 15 s lease expired.
                await self._cm5_presence.set_mode(Cm5PresenceMode.STARTING)
            if self._live_gate is not None:
                self._live_gate.link_reset()
            await self._session.quiesce(_REBOOT_QUIESCE_S)
            self._session.clear_reboot_flag()
            await self._session.settle()
        if self._live_gate is not None:
            await self._live_gate.ensure_armed(self._session)
        if self._cm5_presence is not None:
            await self._cm5_presence.set_mode(Cm5PresenceMode.READY)
        log.info("daemon ready — waiting for jobs")
        while True:
            job = await source.next_job()
            try:
                await self._dispatch(job, source)
            except LinkClosed:
                # The link is dead — reconnection is the supervisor's job
                # (__main__), not something to retry blindly per-job.
                raise
            except Exception:
                log.exception("exchange failed (%s) — daemon continues", job.kind)

    async def _dispatch(self, job: Job, source: JobSource) -> None:
        presence_recovering = False
        if self._session.reboot_suspected:
            # ROM-burst garbage was seen mid-run: the XIAO rebooted under us.
            # Respect OTA probation (plan: CM5 stays idle during probation)
            # before touching it, then let command() re-login lazily. Unlike
            # the startup path there is no idle window to hide this in, so
            # settle() here and let the staleness check below decide whether
            # the job that triggered it is still worth running.
            log.info("reboot suspected — quiescing before next exchange")
            if self._cm5_presence is not None:
                self._cm5_presence.link_reset()
                await self._cm5_presence.set_mode(Cm5PresenceMode.STARTING)
                presence_recovering = True
            if self._live_gate is not None:
                # A rebooted device wiped the lease and shadow; every armed
                # assumption is void even though the serial link never dropped.
                self._live_gate.link_reset()
            await self._session.quiesce(_REBOOT_QUIESCE_S)
            self._session.clear_reboot_flag()
            await self._session.settle()
            if self._live_gate is not None:
                await self._live_gate.ensure_armed(self._session)

        power_started = False
        presence_busy = False
        try:
            if job.kind == "evenai" and job.exchange is None:
                log.warning("dropping uncorrelated evenai job")
                return
            if job.kind not in ("ask", "chat", "evenai"):
                log.warning("unknown job kind %r", job.kind)
                return
            if job.kind == "evenai":
                assert job.exchange is not None
                age = time.monotonic() - job.created
                if age > _WAKE_STALE_S:
                    # Answering now would re-open a card the wearer already
                    # dismissed. The finalizer below also closes the exact
                    # still-live native ID instead of waiting for its 60s cap.
                    log.info("wake is %.0fs old (> %.0fs) — dropping instead "
                             "of reopening a closed session",
                             age, _WAKE_STALE_S)
                    job.exchange.cancel("stale")
                    return
                job.exchange.raise_if_cancelled()
            if self._cm5_presence is not None:
                await self._cm5_presence.set_mode(Cm5PresenceMode.BUSY)
                presence_busy = True
            if self._power_activity is not None:
                await self._power_activity.activity_started()
                power_started = True
            if job.kind == "ask":
                await self.run_ask()
            elif job.kind == "chat":
                await self.run_chat(job.text)
            elif job.kind == "evenai":
                try:
                    assert job.exchange is not None
                    await self.run_evenai(job.exchange)
                except EvenAiCancelled as exc:
                    log.info("%s", exc)
        finally:
            cleanup_succeeded = False
            try:
                try:
                    try:
                        if (job.kind == "evenai" and
                                job.exchange is not None and
                                not job.exchange.delivered):
                            await abort_evenai_best_effort(
                                self._session, job.exchange,
                                job.exchange.cancel_reason or "host_incomplete")
                    finally:
                        if job.kind == "evenai" and job.exchange is not None:
                            await job.exchange.drain_tasks()
                finally:
                    try:
                        if power_started:
                            await self._power_activity.activity_finished()
                    finally:
                        try:
                            # Cover failure/cancellation in abort, cleanup, or
                            # power release itself. Registry release is
                            # synchronous and conditional by ID, so it remains
                            # an inner guarantee.
                            if (job.kind == "evenai" and
                                    job.exchange is not None):
                                source.evenai_done(job.exchange.exchange_id)
                        finally:
                            # Re-establish the standing live-STT arm for the
                            # next wake. A no-op while armed; only pays its two
                            # command round trips after a disarm.
                            if (job.kind == "evenai" and
                                    self._live_gate is not None):
                                await self._live_gate.ensure_armed(self._session)
                cleanup_succeeded = True
            finally:
                if ((presence_busy or presence_recovering) and
                        self._cm5_presence is not None):
                    if cleanup_succeeded:
                        # Do not await a sibling actor during TaskGroup
                        # cancellation. Remaining BUSY until the actor sends
                        # this edge is fail-closed for new G2 admissions.
                        self._cm5_presence.set_mode_nowait(
                            Cm5PresenceMode.READY)
                    else:
                        self._cm5_presence.set_mode_nowait(
                            Cm5PresenceMode.DEGRADED)
