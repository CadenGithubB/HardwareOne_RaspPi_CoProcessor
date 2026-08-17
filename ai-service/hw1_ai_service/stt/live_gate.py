"""Gate E: live streaming STT for wake exchanges, batch path as fallback.

The firmware's recorder shadow (live-pcm-v1) streams a wake capture's PCM to
the host WHILE the wearer speaks; this module owns everything the daemon
needs to consume it: the standing shadow arm + negotiated lease renewal, one
pre-warmed LiveMoonshineWorker so inference starts the moment PCM arrives,
and a single capture() call whose every failure mode resolves to "the caller
runs the existing fetched-WAV batch path". Live STT is a latency
optimization; the batch path stays load-bearing and correct on its own.

Contract with the firmware (verified in System_LiveAudio.cpp, adversarially
reviewed 2026-08-11):
- `liveaudio shadow 1 <ctl> on native` is standing state on the lease; each
  EvenAI wake mints its own 5 s capture arm internally. BUT a `ready` that
  lands after the 3 s lease expired silently re-mints the lease with the
  shadow flags WIPED and still replies OK — renewal success is NOT proof the
  arm survived. The renew loop therefore re-verifies `liveaudio status`
  whenever a renew gap exceeded the suspect threshold (and periodically),
  and re-arms only when the status verifiably shows the shadow off; a blind
  re-arm would clear a pending per-wake capture arm.
- A mismatched BEGIN is never invalidated from here: it may belong to a
  successor exchange (rapid re-wake). An unconsumed stale stream is
  superseded by the next BEGIN automatically.
- All blocking inbox/worker calls run in ONE capture thread whose finally
  block owns worker finalization — a cancelled coroutine (link loss tearing
  down the daemon TaskGroup) can never leak a resident transcriber.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from ..audio.live import LivePcmInbox, LivePcmStream, LiveStreamTerminal
from ..config import SttConfig
from ..link import protocol
from .live import LiveMoonshineWorker, exact_moonshine_factory

log = logging.getLogger("stt.live_gate")

# Legacy fallback. Firmware predating the direct ready-renewal path advertises
# the same renew_ms=1000 value as current firmware, but pays a full synchronous
# command round-trip on every renewal (~450 ms INPUT stall measured). Only an
# explicit renew_direct=1 grant may replace this 2 s compatibility cadence.
# HARD-LEARNED (first deploy of the bump): renew sends MUST be scheduled by
# absolute deadline (previous send + interval), never "sleep interval after
# the cycle finishes" — a verify+re-arm cycle adds ~1.3 s of round-trips, and
# interval-after-cycle pushed firmware-receipt spacing past the 3 s TTL,
# producing a self-sustaining lapse->wipe->re-arm loop every 3.2 s on real
# hardware. With deadline scheduling the spacing stays ~interval + one RTT.
_RENEW_INTERVAL_S = 2.0
_RENEW_TIMEOUT_S = 2.5
# Legacy suspect threshold for firmware without the direct-renew marker. A
# marked grant derives the same 250 ms-before-TTL boundary from its advertised
# lease_ttl_ms. The margin is deliberately thin on the under-detect side — a
# lapse the gap check misses is still caught by periodic verification and by
# BEGIN-timeout forced re-arm (both fail soft to the batch path).
_RENEW_SUSPECT_GAP_S = 2.75
_RENEW_SUSPECT_MARGIN_S = 0.25
# Periodic status verification covers arm-loss modes no renew reply can reveal
# (device reboot without link drop). Keep this elapsed-time based so selecting
# a 1 s direct cadence does not double ordinary status-command traffic.
_VERIFY_INTERVAL_S = 16.0
_ARM_FAILURE_LIMIT = 3
_BEGIN_TIMEOUT_DISARM_LIMIT = 2
_WARM_START_TIMEOUT_S = 120.0
_WARM_JOIN_TIMEOUT_S = 10.0
_CAPTURE_BUDGET_MARGIN_S = 5.0
_ABORT_TIMEOUT_S = 5.0


def fresh_controller_id() -> int:
    high = secrets.randbits(32) or 1
    low = secrets.randbits(32) or 1
    return (high << 32) | low


def _status_tokens(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in text.split():
        if "=" in token:
            key, _, value = token.partition("=")
            out[key] = value
    return out


@dataclass(frozen=True)
class _LeaseTiming:
    renew_interval_s: float
    lease_ttl_s: float
    suspect_gap_s: float
    direct: bool


def _legacy_lease_timing() -> _LeaseTiming:
    return _LeaseTiming(
        renew_interval_s=_RENEW_INTERVAL_S,
        lease_ttl_s=3.0,
        suspect_gap_s=_RENEW_SUSPECT_GAP_S,
        direct=False,
    )


def _lease_timing_from_ready(
        ready: protocol.LiveReadyReply) -> _LeaseTiming:
    wire_timing = protocol.live_lease_timing_from_ready(
        ready,
        legacy_renew_ms=round(_RENEW_INTERVAL_S * 1000),
        legacy_ttl_ms=3000,
    )
    if not wire_timing.direct:
        return _legacy_lease_timing()
    ttl_s = wire_timing.lease_ttl_ms / 1000.0
    return _LeaseTiming(
        renew_interval_s=wire_timing.renew_ms / 1000.0,
        lease_ttl_s=ttl_s,
        suspect_gap_s=ttl_s - _RENEW_SUSPECT_MARGIN_S,
        direct=True,
    )


@dataclass
class LiveSttOutcome:
    valid: bool
    reason: str
    text: str = ""
    raw_text: str = ""
    dropped_leading: str | None = None
    pcm: bytes = b""
    sample_rate: int = 16000
    # False only when no live stream can exist for this wake (gate was never
    # armed): the caller then skips the firmware-lane abort round trip.
    lane_active: bool = True
    worker_result: dict[str, Any] | None = None
    stream_snapshot: dict[str, Any] | None = None
    end_to_final_s: float | None = None


def strip_leading_wake_fragment(
        text: str, worker_result: dict[str, Any]) -> tuple[str, str | None]:
    """Drop a preroll wake-word tail rendered as its own leading line.

    NOT a prefix check — the final text is by construction the join of the
    stop lines, so a prefix rule would delete the first sentence of any real
    multi-sentence question. The structural signals (validated on hardware:
    "been." / "even." / "them." at start_time 0.0 vs question lines at
    0.45-0.8 s) are: the fragment line starts at the capture head, is at most
    two words, and the real content line starts within the preroll+wake
    window. A recovered-from-history final (stop-time erasure rescue) is
    never stripped — its text does not come from the stop-line structure.
    """
    stream = worker_result.get("stream") or {}
    if stream.get("final_recovered"):
        return text, None
    stop_lines = stream.get("stop_lines") or []
    if len(stop_lines) < 2:
        return text, None
    first = stop_lines[0]
    second = stop_lines[1]
    fragment = str(first.get("text") or "").strip()
    if not fragment or len(fragment.split()) > 2:
        return text, None
    first_start = first.get("start_time")
    second_start = second.get("start_time")
    if not isinstance(first_start, (int, float)) or first_start >= 0.05:
        return text, None
    if not isinstance(second_start, (int, float)) or second_start > 1.5:
        return text, None
    if not text.startswith(fragment):
        return text, None
    rest = text[len(fragment):].strip()
    if not rest:
        return text, None
    return rest, fragment


class _CaptureRun:
    """One exchange's capture, executed entirely on a worker thread.

    The thread — not the awaiting coroutine — owns worker finalization: its
    finally always aborts an unfinished worker and hands it to the gate for
    discard+rewarm, so TaskGroup cancellation mid-capture cannot leak a
    loaded transcriber.
    """

    def __init__(self, gate: "LiveSttGate", worker: LiveMoonshineWorker,
                 exchange_int: int, *, wake_stream_timeout_s: float,
                 capture_budget_s: float, final_timeout_s: float) -> None:
        self._gate = gate
        self._worker = worker
        self._exchange_int = exchange_int
        self._wake_stream_timeout_s = wake_stream_timeout_s
        self._capture_budget_s = capture_budget_s
        self._final_timeout_s = final_timeout_s
        self._cancel_reason: str | None = None
        self._stream: LivePcmStream | None = None
        self._lock = threading.Lock()

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self._cancel_reason is None:
                self._cancel_reason = reason
            stream = self._stream
        if stream is not None:
            stream.invalidate(reason)
        self._worker.abort(reason)

    def _cancelled(self) -> str | None:
        with self._lock:
            return self._cancel_reason

    def run(self) -> LiveSttOutcome:
        worker = self._worker
        pcm = bytearray()
        stream: LivePcmStream | None = None
        sample_rate = 16000
        finalized = False
        try:
            deadline = time.monotonic() + self._wake_stream_timeout_s
            while True:
                cancel = self._cancelled()
                if cancel is not None:
                    return self._invalid(cancel, pcm, sample_rate, None)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._gate._note_begin_timeout()
                    return self._invalid("begin_timeout", pcm, sample_rate, None)
                try:
                    # for_exchange: a successor's BEGIN (rapid re-wake) is
                    # skipped without consuming its delivery slot, so it is
                    # never invalidated nor stranded for its own consumer.
                    stream = self._gate._inbox.next_stream(
                        timeout=min(0.25, remaining),
                        for_exchange=self._exchange_int)
                except TimeoutError:
                    continue
                break
            with self._lock:
                self._stream = stream
                cancel = self._cancel_reason
            if cancel is not None:
                stream.invalidate(cancel)
                return self._invalid(cancel, pcm, sample_rate, stream)
            self._gate._note_stream_consumed()
            if stream.begin.synthetic:
                stream.invalidate("daemon_synthetic_begin")
                return self._invalid("synthetic_begin", pcm, sample_rate, stream)
            sample_rate = stream.begin.sample_rate
            worker.on_begin({"sample_rate": sample_rate})

            terminal: LiveStreamTerminal | None = None
            capture_deadline = time.monotonic() + self._capture_budget_s
            while terminal is None:
                remaining = capture_deadline - time.monotonic()
                if remaining <= 0:
                    stream.invalidate("daemon_capture_deadline")
                    remaining = 0.1
                try:
                    item = stream.next_item(timeout=remaining)
                except TimeoutError:
                    continue
                if isinstance(item, LiveStreamTerminal):
                    terminal = item
                    continue
                pcm.extend(item.pcm)
                # A False return means the model queue overflowed; the worker
                # already marked itself invalid. Keep draining the transport
                # so the stream reaches a durable terminal cleanly.
                worker.offer_pcm(item.pcm)

            t_terminal = time.monotonic()
            if terminal.kind != "end" or not terminal.valid:
                return self._invalid(
                    f"stream_{terminal.kind}:{terminal.reason}",
                    pcm, sample_rate, stream)
            worker.end_input()
            result = worker.wait(self._final_timeout_s + 1.0)
            finalized = True
            end_to_final = time.monotonic() - t_terminal
            if not result.get("valid"):
                reasons = ",".join(result.get("failure_reasons") or []) or "invalid"
                return LiveSttOutcome(
                    valid=False, reason=f"stt:{reasons}", pcm=bytes(pcm),
                    sample_rate=sample_rate, worker_result=result,
                    stream_snapshot=stream.snapshot(),
                    end_to_final_s=end_to_final)
            raw = str(result.get("text") or "").strip()
            text, dropped = strip_leading_wake_fragment(raw, result)
            return LiveSttOutcome(
                valid=True, reason="", text=text, raw_text=raw,
                dropped_leading=dropped, pcm=bytes(pcm),
                sample_rate=sample_rate, worker_result=result,
                stream_snapshot=stream.snapshot(),
                end_to_final_s=end_to_final)
        except BaseException as exc:  # fail-closed: the caller falls back
            log.exception("live capture failed internally")
            if stream is not None:
                stream.invalidate(f"daemon_error:{type(exc).__name__}")
            return self._invalid(
                f"exception:{type(exc).__name__}:{exc}", pcm, sample_rate,
                stream)
        finally:
            if not finalized:
                worker.abort("capture_discarded")
            # This thread is the taken worker's sole owner: it is never
            # returned to the warm slot (double-fill races), always replaced.
            self._gate._discard_and_rewarm(worker)

    def _invalid(self, reason: str, pcm: bytearray, sample_rate: int,
                 stream: LivePcmStream | None) -> LiveSttOutcome:
        return LiveSttOutcome(
            valid=False, reason=reason, pcm=bytes(pcm),
            sample_rate=sample_rate,
            stream_snapshot=stream.snapshot() if stream is not None else None)


class LiveSttGate:
    def __init__(self, cfg: SttConfig, inbox: LivePcmInbox, *,
                 clock=time.monotonic, sleep=asyncio.sleep) -> None:
        # Raises ValueError on a bad model dir/arch: the caller degrades to
        # batch-only at startup, loudly.
        self._factory = exact_moonshine_factory(
            cfg.live_model_dir, cfg.live_model_arch)
        self._cfg = cfg
        self._inbox = inbox
        # Injectable time sources so the renew loop's gap/tick behavior is
        # testable deterministically; production uses the real ones.
        self._clock = clock
        self._sleep = sleep
        self.controller_id = inbox.controller_id
        self._controller_text = protocol.live_id_hex(inbox.controller_id)
        self._armed = False
        self._armed_epoch = 0
        self._arm_failures = 0
        self._disabled = False
        self._renew_gen = 0
        self._renew_task: asyncio.Task | None = None
        self._lease_timing = _legacy_lease_timing()
        self._begin_timeouts = 0
        self._warm_lock = threading.Lock()
        self._warm: LiveMoonshineWorker | None = None
        self._warming = False
        self._warm_failures = 0
        self._warm_thread: threading.Thread | None = None
        self._closing = False
        self._current_run: _CaptureRun | None = None
        self._current_task: asyncio.Task | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._spawn_warm(None)

    async def close(self) -> None:
        self._closing = True
        self._renew_gen += 1
        task, self._renew_task = self._renew_task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        # Cancel an in-flight capture and await its task: run.cancel aborts the
        # worker (sets _input_done) and invalidates the stream (wakes next_item),
        # so the capture thread exits promptly and finalizes its worker.
        run = self._current_run
        if run is not None:
            run.cancel("gate_closed")
        ctask = self._current_task
        if ctask is not None:
            await asyncio.gather(ctask, return_exceptions=True)
        with self._warm_lock:
            warm, self._warm = self._warm, None
            warm_thread = self._warm_thread
        if warm is not None:
            warm.abort("gate_closed")
        if warm_thread is not None:
            # Bounded: a build stuck in native model load is a daemon thread
            # that dies with the interpreter; don't block shutdown on it.
            await asyncio.to_thread(warm_thread.join, _WARM_JOIN_TIMEOUT_S)

    def link_reset(self) -> None:
        """Link loss/reconnect or device reboot: every firmware-side lease and
        shadow assumption is void. ensure_armed() re-establishes lazily."""
        self._renew_gen += 1
        self._armed = False
        self._arm_failures = 0
        self._disabled = False
        self._begin_timeouts = 0
        self._lease_timing = _legacy_lease_timing()

    # -- arming ------------------------------------------------------------

    async def ensure_armed(self, session) -> bool:
        if self._disabled or self._closing:
            return False
        arm_gen = self._renew_gen
        with self._warm_lock:
            need_warm = self._warm is None and not self._warming
        if need_warm:
            self._spawn_warm(None)
        if self._armed:
            return True
        try:
            # The lease begins when firmware handles ready. The host's send
            # timestamp is an intentionally early lower bound, so setup RTTs
            # consume sleep time rather than hidden lease margin.
            arm_ready_at = self._clock()
            ready = await session.command(
                f"liveaudio ready 1 {self._controller_text}",
                expect="status", timeout=_RENEW_TIMEOUT_S, replay=False)
            if (arm_gen != self._renew_gen or self._closing):
                return False
            if not ready.ok:
                return self._arm_failed(f"ready rejected: {ready.text}")
            parsed_ready = protocol.parse_live_ready(
                ready.text, expected_controller=self.controller_id)
            timing = _lease_timing_from_ready(parsed_ready)
            epoch = parsed_ready.session_epoch
            status = await session.command(
                "liveaudio status", expect="status",
                timeout=_RENEW_TIMEOUT_S, replay=False)
            if (arm_gen != self._renew_gen or self._closing):
                return False
            if not status.ok:
                return self._arm_failed(f"status rejected: {status.text}")
            tokens = _status_tokens(status.text)
            if not (tokens.get("shadow") == "on"
                    and tokens.get("shadow_mode") == "native"):
                arm = await session.command(
                    f"liveaudio shadow 1 {self._controller_text} on native",
                    expect="status", timeout=_RENEW_TIMEOUT_S, replay=False)
                if (arm_gen != self._renew_gen or self._closing):
                    return False
                if not arm.ok:
                    return self._arm_failed(f"arm rejected: {arm.text}")
        except Exception as exc:
            if arm_gen != self._renew_gen or self._closing:
                return False
            return self._arm_failed(f"{type(exc).__name__}: {exc}")
        self._armed = True
        self._armed_epoch = epoch
        self._lease_timing = timing
        self._arm_failures = 0
        self._begin_timeouts = 0
        self._renew_gen += 1
        gen = self._renew_gen
        old, self._renew_task = self._renew_task, None
        if old is not None:
            old.cancel()
        self._renew_task = asyncio.create_task(
            self._renew_loop(session, gen, arm_ready_at, timing),
            name="live-stt-lease-renew")
        log.info(
            "live STT armed (controller %s, session epoch %d, renew %.3fs, "
            "ttl %.3fs, direct=%d)", self._controller_text, epoch,
            timing.renew_interval_s, timing.lease_ttl_s,
            1 if timing.direct else 0)
        return True

    def _arm_failed(self, why: str) -> bool:
        self._arm_failures += 1
        level = log.warning if self._arm_failures == 1 else log.info
        level("live STT arm failed (%d/%d): %s",
              self._arm_failures, _ARM_FAILURE_LIMIT, why)
        if self._arm_failures >= _ARM_FAILURE_LIMIT:
            self._disabled = True
            log.warning("live STT disabled until link reset — batch path only")
        return False

    def _disarm(self, gen: int, why: str) -> None:
        if gen != self._renew_gen or not self._armed:
            return
        self._armed = False
        self._lease_timing = _legacy_lease_timing()
        log.info("live STT disarmed: %s", why)

    async def _renew_loop(self, session, gen: int, arm_ready_at: float,
                          timing: _LeaseTiming) -> None:
        command = f"liveaudio ready 1 {self._controller_text}"
        last_ok = arm_ready_at
        # Deadline-scheduled: each renew targets previous-send + interval, so
        # verify/arm round-trips consume sleep time, not lease margin. The
        # send stamp is a conservative lower bound on the firmware's lease
        # restart (which happens at receipt, strictly later).
        next_send = arm_ready_at + timing.renew_interval_s
        next_verify = arm_ready_at + _VERIFY_INTERVAL_S
        while True:
            await self._sleep(max(0.05, next_send - self._clock()))
            if gen != self._renew_gen or not self._armed or self._closing:
                return
            sent_at = self._clock()
            try:
                rep = await session.command(
                    command, expect="status", timeout=_RENEW_TIMEOUT_S,
                    replay=False)
            except Exception as exc:
                self._disarm(gen, f"renew failed: {type(exc).__name__}: {exc}")
                return
            if gen != self._renew_gen or not self._armed or self._closing:
                return
            if not rep.ok:
                self._disarm(gen, f"renew rejected: {rep.text}")
                return
            next_send = sent_at + timing.renew_interval_s
            try:
                parsed_ready = protocol.parse_live_ready(
                    rep.text, expected_controller=self.controller_id)
                reply_timing = _lease_timing_from_ready(parsed_ready)
            except ValueError as exc:
                self._disarm(gen, f"invalid renew grant: {exc}")
                return
            if reply_timing != timing:
                self._disarm(gen, "renew timing contract changed")
                return
            if parsed_ready.session_epoch != self._armed_epoch:
                # Timing and lease authority are session-local. The normal
                # link-reset path will re-arm; if an epoch transition raced an
                # in-flight command, fail soft instead of inheriting it here.
                self._disarm(gen, "renew session epoch changed")
                return
            now = self._clock()
            suspect = (now - last_ok) > timing.suspect_gap_s
            last_ok = now
            periodic_verify = now >= next_verify
            if not suspect and not periodic_verify:
                continue
            # A renew landing after the lease lapsed re-minted it with the
            # shadow WIPED while still replying OK — verify, and re-arm only
            # on verified loss (a blind arm clears a pending per-wake arm).
            try:
                status = await session.command(
                    "liveaudio status", expect="status",
                    timeout=_RENEW_TIMEOUT_S, replay=False)
                if gen != self._renew_gen or not self._armed or self._closing:
                    return
                if not status.ok:
                    self._disarm(gen, f"verify rejected: {status.text}")
                    return
                tokens = _status_tokens(status.text)
                if not (tokens.get("shadow") == "on"
                        and tokens.get("shadow_mode") == "native"):
                    arm = await session.command(
                        f"liveaudio shadow 1 {self._controller_text} on native",
                        expect="status", timeout=_RENEW_TIMEOUT_S,
                        replay=False)
                    if gen != self._renew_gen or not self._armed or self._closing:
                        return
                    if not arm.ok:
                        self._disarm(gen, f"re-arm rejected: {arm.text}")
                        return
                    log.info("live STT shadow re-armed (%s)",
                             "suspect renew gap" if suspect else "periodic verify")
            except Exception as exc:
                self._disarm(gen, f"verify failed: {type(exc).__name__}: {exc}")
                return
            next_verify = self._clock() + _VERIFY_INTERVAL_S

    # -- capture -----------------------------------------------------------

    async def capture(self, exchange, *, vad_max_seconds: float) -> LiveSttOutcome:
        """Consume this exchange's live stream through streaming STT.

        Never raises (fail-closed): every failure returns an invalid outcome
        whose reason tells the caller why the batch path is about to run.
        """
        if self._disabled or not self._armed or self._closing:
            return LiveSttOutcome(
                valid=False, reason="not_armed", lane_active=False)
        try:
            exchange_int = int(exchange.exchange_id, 16)
        except ValueError:
            return LiveSttOutcome(
                valid=False, reason="bad_exchange_id", lane_active=False)
        worker = self._take_warm()
        if worker is None:
            self._spawn_warm(None)
            await asyncio.to_thread(
                self._tombstone_own_stream, exchange_int, "no_warm_worker")
            return LiveSttOutcome(valid=False, reason="no_warm_worker")

        run = _CaptureRun(
            self, worker, exchange_int,
            wake_stream_timeout_s=self._cfg.live_wake_stream_timeout_s,
            capture_budget_s=vad_max_seconds + _CAPTURE_BUDGET_MARGIN_S,
            final_timeout_s=self._cfg.live_final_timeout_s)
        self._current_run = run
        thread_task = asyncio.create_task(
            asyncio.to_thread(run.run), name="live-stt-capture")
        self._current_task = thread_task
        cancel_wait = asyncio.create_task(exchange.cancel_event.wait())
        try:
            done, _pending = await asyncio.wait(
                (thread_task, cancel_wait),
                return_when=asyncio.FIRST_COMPLETED)
            if thread_task not in done:
                run.cancel("host_cancelled")
            outcome = await thread_task
        except asyncio.CancelledError:
            # The capture thread's finally still finalizes the worker.
            run.cancel("daemon_cancelled")
            raise
        except Exception as exc:
            log.exception("live capture wrapper failed")
            run.cancel("wrapper_error")
            return LiveSttOutcome(
                valid=False, reason=f"exception:{type(exc).__name__}:{exc}")
        finally:
            self._current_run = None
            self._current_task = None
            cancel_wait.cancel()
            await asyncio.gather(cancel_wait, return_exceptions=True)
        return outcome

    async def abort_stream(self, session, exchange) -> None:
        """Best-effort firmware lane release before a fallback transfer."""
        command = (f"liveaudio abort 1 {self._controller_text} "
                   f"{exchange.exchange_id}")
        try:
            rep = await session.command(
                command, expect="status", timeout=_ABORT_TIMEOUT_S,
                replay=False)
            if not rep.ok:
                log.debug("live abort rejected (no active stream?): %s", rep.text)
        except Exception as exc:
            log.debug("live abort failed: %s", exc)

    def _tombstone_own_stream(self, exchange_int: int, reason: str) -> None:
        """Consume+invalidate OUR stream only (never a successor's)."""
        try:
            stream = self._inbox.next_stream(
                timeout=0.05, for_exchange=exchange_int)
        except TimeoutError:
            return
        # for_exchange guarantees a match here (a foreign active stream would
        # have raised TimeoutError rather than be returned).
        stream.invalidate(reason)

    def _note_begin_timeout(self) -> None:
        self._begin_timeouts += 1
        if self._begin_timeouts >= _BEGIN_TIMEOUT_DISARM_LIMIT:
            # The arm is provably not producing streams (a loss mode the
            # renew loop cannot see). Disarm so ensure_armed re-verifies.
            self._begin_timeouts = 0
            self._armed = False
            log.warning("live STT: %d consecutive BEGIN timeouts — forcing a "
                        "verified re-arm", _BEGIN_TIMEOUT_DISARM_LIMIT)

    def _note_stream_consumed(self) -> None:
        self._begin_timeouts = 0

    # -- warm worker provider ---------------------------------------------

    def _take_warm(self) -> LiveMoonshineWorker | None:
        with self._warm_lock:
            worker, self._warm = self._warm, None
        return worker

    def _discard_and_rewarm(self, worker: LiveMoonshineWorker) -> None:
        self._spawn_warm(worker)

    def _spawn_warm(self, retiring: LiveMoonshineWorker | None) -> None:
        with self._warm_lock:
            if self._warming or self._closing or self._disabled:
                return
            self._warming = True
            thread = threading.Thread(
                target=self._warm_build, args=(retiring,), daemon=True,
                name="live-stt-warm")
            self._warm_thread = thread
        thread.start()

    def _warm_build(self, retiring: LiveMoonshineWorker | None) -> None:
        worker: LiveMoonshineWorker | None = None
        try:
            if retiring is not None:
                # Cap transcriber co-residency: let the retiring worker finish
                # closing before loading the replacement model.
                retiring.join(_WARM_JOIN_TIMEOUT_S)
            if self._closing:
                return
            worker = LiveMoonshineWorker(
                self._factory,
                update_interval_s=self._cfg.live_update_interval_s,
                queue_chunks=self._cfg.live_queue_chunks)
            worker.start(_WARM_START_TIMEOUT_S)
            stale: LiveMoonshineWorker | None = None
            with self._warm_lock:
                if self._closing or self._warm is not None:
                    stale = worker
                else:
                    self._warm = worker
                self._warm_failures = 0
            if stale is not None:
                stale.abort("warm_slot_conflict")
        except Exception as exc:
            # A constructed worker whose start() timed out (or a later step
            # raised) owns a native transcriber whose thread is still loading
            # or spinning — abort so _input_done is set and it drains+closes
            # instead of leaking a resident model forever.
            if worker is not None:
                worker.abort("warm_build_failed")
            self._note_warm_failure(exc)
        finally:
            with self._warm_lock:
                self._warming = False

    def _note_warm_failure(self, exc: Exception) -> None:
        with self._warm_lock:
            self._warm_failures += 1
            failures = self._warm_failures
        level = log.warning if failures == 1 else log.info
        level("live STT warm worker build failed (%d/%d): %s",
              failures, _ARM_FAILURE_LIMIT, exc)
        if failures >= _ARM_FAILURE_LIMIT:
            # A persistently un-buildable worker (missing model package, OOM)
            # would otherwise re-arm the firmware shadow and re-attempt the
            # build on every wake forever. Disable until a link reset.
            self._disabled = True
            log.warning("live STT disabled — warm worker cannot build; "
                        "batch path only until link reset")
