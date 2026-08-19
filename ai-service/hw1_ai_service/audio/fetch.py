"""A0 audio fetch: record on the ESP32, pull the WAV over the command channel.

Sequence (all replies verified against the firmware source, see
ARCHITECTURE.md §4):

    openmic                          -> status; repeat call is a SUCCESS on
                                        real firmware ("OK: Microphone started
                                        successfully")
    micrecord start [vad <ms>]       -> "Recording started" (+ "(auto-stop on
                                        silence)" when VAD armed)
    ...fixed window sleep, OR poll `micrecord` until the device auto-stops...
    micrecord stop                   -> "Recording stopped — <path> (N.Ns)"
                                        (returns the path even after auto-stop)
    fileread "<path>" <off> <len> b64
        -> {"success":true,"size":N,"offset":N,"len":N,"eof":bool,
            "enc":"b64","data":"..."}   (loop offset until eof)
    micdelete "<name>"               -> best-effort cleanup (admin in P0)

Review-hardened rules:
  - micrecord start/stop use replay=False — a timeout must NEVER blind-replay
    a non-idempotent command (a replayed start "fails" while recording runs;
    a replayed stop loses the path).
  - Every command traversing cmd_exec keeps the default 65s timeout, which
    outlasts the firmware's documented 62s worst case (shorter call-site
    timeouts guaranteed false timeouts under cmd_exec contention).
  - If capture started but the normal stop never ran (cancellation, link
    loss), a finally block best-effort stops the mic — otherwise it records
    to the 60s cap, strands ~2MB on flash, and blocks every later exchange.
  - A start that fails with "Failed to start recording" gets one stop+retry
    (the it-was-already-recording wedge).

Known property carried from the plan/audit: this audio has firmware
processing baked in (~24x gain, HPF, pre-emphasis) — good enough for the
pipeline and engine comparison, NOT evidence for the raw-PCM decision.

P2 replaces the fileread loop with the voicefetch burst path; this module
keeps both (A0 stays as the fallback/compat probe).
"""

from __future__ import annotations

import asyncio
import base64
import logging
import posixpath
import re
import time

from .. import bg
from .. import evenai_protocol as evenai_wire
from ..config import AudioConfig
from ..jobs import EvenAiCancelled, EvenAiExchange
from ..link import protocol
from ..link.session import CommandCancelled, Reply, Session

log = logging.getLogger("audio.fetch")

_STOP_PATH_RE = re.compile(r"Recording stopped — (\S+\.wav)")
_FALLBACK_PATH_RE = re.compile(r"(/\S+\.wav)")

# Legacy firmware published its recording flag before it finished the WAV and
# pushed mic_autostop. A status poll could therefore win by a few milliseconds
# and make the wake path send a redundant stop just to recover the filename.
# Current firmware reports STOPPING/FINALIZING until close and publishes IDLE
# last, so this grace is now backward-compatibility coverage for older ESP32s.
# The EVT remains unacknowledged and the explicit-stop fallback authoritative.
_WAKE_STOPPED_EVT_GRACE_S = 0.25
# Owner cleanup is best-effort and idempotent.  It must not inherit the generic
# 65-second command ceiling: dispatch drains this job-owned task before S2 and
# before releasing the activity lease.  Five seconds is ample once this command
# owns the serialized link; a miss is logged as a stray file, not a wedged UI.
_EVENAI_CLEANUP_TIMEOUT_S = 5.0


class FetchError(RuntimeError):
    pass


async def record_utterance(session: Session, cfg: AudioConfig) -> bytes:
    """Record a fixed window on the device and return the WAV bytes."""
    await _ensure_mic(session)
    path = await _record_window(session, cfg)
    log.info("recorded %s", path)
    try:
        return await _transfer(session, cfg, path)
    finally:
        bg.fire_and_forget(_cleanup(session, path), what="micdelete")


async def fetch_wake_utterance(
        session: Session, cfg: AudioConfig, exchange: EvenAiExchange) -> bytes:
    """Wake-push flow ("Hey Even"): the FIRMWARE already started a
    VAD-endpointed capture — the evenai_wake EVT is pushed only after the
    recording was confirmed live. Await the device's silence auto-stop, close
    the recording to learn its path, transfer, clean up. Never sends micrecord
    start: the capture belongs to the device.

    The exchange ID is also the recorder owner. Terminal events, status/stop,
    and cleanup all carry it, so a delayed previous capture can neither satisfy
    this wait nor mutate the current recorder."""
    t0 = time.monotonic()
    try:
        path = await _await_exchange_stop(session, cfg, exchange)
    except CommandCancelled:
        exchange.raise_if_cancelled()
        raise
    t_stopdetect = time.monotonic()
    if path is None:
        # No path-bearing EVT (lost, max window, or an external/session stop,
        # which intentionally emits none) — fall back to the round trip, which
        # both stops/finalizes the device and reports the path.
        exchange.raise_if_cancelled()
        rep = await session.command(
            evenai_wire.mic_stop_command(exchange.exchange_id),
            expect="status", replay=False,
            cancel_guard=lambda: exchange.cancelled)
        if not rep.ok:
            raise FetchError(f"micrecord stop failed: {rep.text}")
        if "discarded" in rep.text.lower():
            # Dismissal deletes the owner-scoped result.  If the preceding
            # unacknowledged cancel EVT was lost, the recorder result itself
            # remains an authoritative fail-closed cancellation signal.
            exchange.cancel("recorder_discarded")
            exchange.raise_if_cancelled()
        path = _parse_recording_path(rep)
    t_closed = time.monotonic()
    log.info("wake utterance closed: %s", path)
    try:
        try:
            out = await _transfer(
                session, cfg, path, cancel_guard=lambda: exchange.cancelled)
        except CommandCancelled:
            exchange.raise_if_cancelled()
            raise
        # Splits the single biggest stage in the exchange. `wait` is the device
        # recording (the user speaking plus the VAD's trailing window) and is
        # not compressible without the truncation tradeoff; `xfer` is the part
        # that responds to the CRC fix, the baud, and the SD clock. Conflating
        # them made a 63%-of-total stage unattributable.
        t_x = time.monotonic()
        log.info("fetch breakdown: wait=%.2fs stop=%.2fs xfer=%.2fs",
                 t_stopdetect - t0, t_closed - t_stopdetect, t_x - t_closed)
        return out
    finally:
        # MEASURED 0.54s, 4.2% of the exchange, spent deleting a file the user
        # is not waiting for. Off the critical path: the deletion then overlaps
        # STT (MEASURED 1.09s mean, 0.74s minimum), which comfortably covers it,
        # so the cost leaves the exchange instead of merely moving within it.
        # Overlap deletion with STT, but keep the task owned by S1. Dispatch
        # drains it before S2 can start, avoiding both the old 0.54s latency
        # charge and a global fire-and-forget tail crossing session boundaries.
        exchange.start_task(
            _cleanup(session, path, exchange.exchange_id),
            name=f"evenai-cleanup-{exchange.exchange_id}")


async def _transfer(session: Session, cfg: AudioConfig, path: str, *,
                    cancel_guard=None) -> bytes:
    if cfg.transfer == "voicefetch":
        return await fetch_frames(session, path, cancel_guard=cancel_guard)
    if cfg.transfer == "auto":
        try:
            return await fetch_frames(session, path, cancel_guard=cancel_guard)
        except FetchError as exc:
            # Firmware without P2 (older build) replies with an error
            # instead of streaming — fall back to the universal A0 path.
            log.warning("voicefetch unavailable (%s) — falling back to fileread", exc)
            return await read_file_b64(
                session, path, cfg.chunk_request_bytes,
                cancel_guard=cancel_guard)
    return await read_file_b64(
        session, path, cfg.chunk_request_bytes, cancel_guard=cancel_guard)


async def _cleanup(session: Session, path: str,
                   exchange_id: str | None = None) -> None:
    # Cleanup is best-effort: a failure leaves a stray WAV, not a broken
    # exchange. micdelete takes the bare filename (quoted-token rule).
    name = posixpath.basename(path)
    try:
        if exchange_id is not None:
            command = evenai_wire.mic_delete_command(exchange_id, name)
            rep = await session.command(
                command, expect="status",
                timeout=_EVENAI_CLEANUP_TIMEOUT_S, replay=False)
        else:
            command = f"micdelete {protocol.quote_path(name)}"
            rep = await session.command(command, expect="status")
        if not rep.ok:
            log.warning("micdelete failed (stray recording left): %s", rep.text)
    except Exception as exc:
        log.warning("micdelete errored (stray recording left): %s", exc)


async def _record_window(session: Session, cfg: AudioConfig) -> str:
    """start -> (poll for silence auto-stop | fixed sleep) -> stop.

    With VAD on, the device ends the recording itself on trailing silence and
    we poll `micrecord` until it reports stopped, using vad_max_seconds as the
    polling deadline (an already-started command can return after that point).
    With VAD off — or when the firmware predates VAD support — this is the
    original fixed-window path. Either way the left-recording guard applies.
    """
    use_vad = cfg.vad
    if use_vad:
        start_cmd = f"micrecord start vad {cfg.vad_silence_ms}"
        if cfg.vad_trim:
            start_cmd += " trim"
    else:
        start_cmd = "micrecord start"

    # Arm BEFORE the start command: a mic_autostop from a previous capture
    # must not be able to satisfy this one's wait.
    token = session.mic_autostop.arm()
    rep = await session.command(start_cmd, expect="status", replay=False)
    if not rep.ok and use_vad and "invalid arguments" in rep.text.lower():
        # Firmware predates `start vad` — degrade to a plain fixed window.
        log.warning("device rejected 'micrecord start vad' — firmware without "
                    "VAD support; using a fixed %.0fs window", cfg.record_seconds)
        use_vad = False
        start_cmd = "micrecord start"
        rep = await session.command(start_cmd, expect="status", replay=False)
    if not rep.ok:
        if "failed to start" in rep.text.lower():
            # Likely already recording (a previous exchange died mid-window):
            # stop whatever is running and retry once.
            log.warning("start refused (already recording?) — stop + retry")
            await _stop_best_effort(session)
            rep = await session.command(start_cmd, expect="status", replay=False)
        if not rep.ok:
            raise FetchError(f"micrecord start failed: {rep.text}")

    stopped = False
    try:
        if use_vad:
            log.info("=== RECORDING (auto-stop after %dms silence, max %.0fs) "
                     "— SPEAK NOW ===", cfg.vad_silence_ms, cfg.vad_max_seconds)
            # Ask path keeps its explicit `micrecord stop` below: it is what
            # sets `stopped` for the left-recording guard, and this path was
            # never the one measured at 0.42s. The returned path is ignored.
            await _await_auto_stop(session, cfg, token)
        else:
            log.info("=== RECORDING for %.0fs — SPEAK NOW ===", cfg.record_seconds)
            await asyncio.sleep(cfg.record_seconds)
        rep = await session.command("micrecord stop", expect="status", replay=False)
        stopped = True
        log.info("=== recording window closed ===")
        if not rep.ok:
            raise FetchError(f"micrecord stop failed: {rep.text}")
        return _parse_recording_path(rep)
    finally:
        if not stopped:
            # Cancellation or link failure mid-window: without this the mic
            # records to the 60s cap, strands ~2MB on flash, and every later
            # start fails until then.
            await _stop_best_effort(session)


async def _await_exchange_stop(session: Session, cfg: AudioConfig,
                               exchange: EvenAiExchange) -> str | None:
    """Wait for the terminal event belonging to this recorder owner.

    The status backstop is ID-scoped: a delayed S1 poll/stop is therefore
    incapable of observing or stopping S2. Cancellation wins over a terminal
    event when both become ready in the same loop turn.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.vad_max_seconds
    while True:
        exchange.raise_if_cancelled()
        if exchange.terminal_event.is_set():
            exchange.raise_if_cancelled()
            return exchange.recording_path
        remaining = deadline - loop.time()
        if remaining <= 0:
            log.info("=== max window (%.0fs) reached for %s ===",
                     cfg.vad_max_seconds, exchange.exchange_id)
            return None

        terminal_wait = asyncio.create_task(exchange.terminal_event.wait())
        cancel_wait = asyncio.create_task(exchange.cancel_event.wait())
        waiters = (terminal_wait, cancel_wait)
        try:
            done, _pending = await asyncio.wait(
                waiters, timeout=min(cfg.vad_poll_s, remaining),
                return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Daemon shutdown can cancel the parent while asyncio.wait is in
            # flight. Never strand child Event.wait tasks into S2/the next
            # loop, even though normal dismissal already completes one.
            for task in waiters:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
        if cancel_wait in done:
            exchange.raise_if_cancelled()
        if terminal_wait in done:
            exchange.raise_if_cancelled()
            return exchange.recording_path
        if loop.time() >= deadline:
            return None

        try:
            rep = await session.command(
                evenai_wire.mic_status_command(exchange.exchange_id),
                expect="status", replay=True,
                cancel_guard=lambda: exchange.cancelled)
        except CommandCancelled:
            exchange.raise_if_cancelled()
            raise
        reply_text = rep.text.lower()
        if not rep.ok:
            # ID-scoped ownership failures and recorder failure are terminal,
            # not transient "still recording" states. Session already handles
            # authentication recovery; polling this same rejected ID until the
            # 15-second VAD ceiling only strands the native card longer.
            raise FetchError(
                f"micrecord status failed for {exchange.exchange_id}: {rep.text}")
        if rep.ok and "discarded" in reply_text:
            # Firmware dismissal deletes the owner-scoped recording before it
            # publishes evenai_cancel.  If that unacknowledged EVT is lost, the
            # ID-scoped status is the authoritative backstop.  Do not burn the
            # rest of vad_max_seconds or fetch a deliberately removed file.
            exchange.cancel("recorder_discarded")
            exchange.raise_if_cancelled()
        if rep.ok and "stopped" in reply_text:
            # The terminal event normally carries the path. Give an event that
            # raced the status reply a short chance to land; an ID-scoped stop
            # remains the authoritative lost-event fallback.
            try:
                await asyncio.wait_for(
                    exchange.terminal_event.wait(), _WAKE_STOPPED_EVT_GRACE_S)
            except asyncio.TimeoutError:
                return None
            exchange.raise_if_cancelled()
            return exchange.recording_path


async def _await_auto_stop(session: Session, cfg: AudioConfig,
                           token: int, *,
                           stopped_evt_grace_s: float = 0.0) -> str | None:
    """Wait for the device's VAD to end the recording, or the host wait deadline.

    Returns the recording path when the device volunteered it in a
    `mic_autostop` EVT, else None (caller falls back to `micrecord stop`).

    Two signals race here on purpose:
      - the EVT, pushed once the WAV is closed and fetchable. It carries the
        path, which is the whole point: it removes a `micrecord stop` round
        trip whose only job was to report a filename the device already knew.
        The previously measured 0.35-0.45s stop stage included WAV finalization
        that both paths must await, so it is not the expected net saving.
      - the `micrecord` status poll, kept as a backstop. It is load-bearing:
        the EVT has no ACK and a CRC-corrupt frame is dropped silently, so
        without the poll a lost event would cost the whole vad_max_seconds.
        Bare `micrecord` is a pure status read that never touches I2S
        (System_Microphone.cpp cmd_micrecord), so it cannot perturb the
        capture it observes.

    `stopped_evt_grace_s` is deliberately non-zero only for the device-owned
    wake flow and only matters with legacy firmware that reported stopped before
    WAV finalization. Current firmware reports STOPPING/FINALIZING until the
    file and terminal event are complete. The manual ask flow still sends an
    explicit stop regardless of this return value and must not pay the grace. A
    bounded miss returns None and keeps the stop fallback load-bearing.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + cfg.vad_max_seconds
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            log.info("=== max window (%.0fs) reached — stopping ===", cfg.vad_max_seconds)
            return None
        if await session.mic_autostop.wait(token, min(cfg.vad_poll_s, remaining)):
            path = session.mic_autostop.payload or None
            log.info("=== device pushed mic_autostop%s ===",
                     f" ({path})" if path else "")
            return path
        # The latch wait may have consumed the final slice of the host wait
        # budget. Do not start a long-timeout status command after that point.
        # A poll already in flight can still return after the deadline.
        if loop.time() >= deadline:
            log.info("=== max window (%.0fs) reached — stopping ===", cfg.vad_max_seconds)
            return None
        rep = await session.command("micrecord", expect="status")
        if rep.ok and "stopped" in rep.text.lower():
            # Recompute after the command: a poll can begin just before the
            # capture deadline and return after it. In that case do not add a
            # grace penalty (notably, an external session EXIT emits no EVT).
            grace = min(stopped_evt_grace_s,
                        max(0.0, deadline - loop.time()))
            if grace > 0:
                grace_started = loop.time()
                if await session.mic_autostop.wait(token, grace):
                    path = session.mic_autostop.payload or None
                    waited = loop.time() - grace_started
                    log.info("=== device pushed mic_autostop %.2fs after stopped "
                             "poll%s ===", waited,
                             f" ({path})" if path else "")
                    return path
                log.info("=== device reports recording stopped; mic_autostop "
                         "grace expired after %.2fs ===",
                         loop.time() - grace_started)
            else:
                # "stopped" does not prove a VAD decision: session EXIT and an
                # external micrecord stop produce the same status and no EVT.
                log.info("=== device reports recording stopped (backstop poll) ===")
            return None


async def _stop_best_effort(session: Session) -> None:
    try:
        await session.command("micrecord stop", expect="status", replay=False)
    except BaseException as exc:  # includes CancelledError during teardown
        log.warning("best-effort micrecord stop failed: %s", exc)


_VOICEFETCH_CRC_RE = re.compile(r"crc16=([0-9A-Fa-f]{4})")
# The firmware refuses voicefetch while the live-pcm lane is winding down
# (mutually exclusive framed producers). The lane drains within ~200 ms of
# WAV close, but a live-STT fallback can race that window — retry briefly
# instead of failing the exchange (matters most for transfer=voicefetch,
# which has no fileread fallback).
_LIVE_LANE_BUSY_TEXT = "live audio stream is active"
_LIVE_LANE_RETRY_S = 5.0
_LIVE_LANE_POLL_S = 0.25


async def fetch_frames(session: Session, path: str, *,
                       cancel_guard=None) -> bytes:
    """P2 burst transfer: one voicefetch command, binary frames back.

    The firmware sends a META frame (total size), AUDIO frames (seq 1..N),
    then the text reply carrying byte/frame/crc totals. We verify: the META
    size, the per-frame CRC (already checked in the transport), contiguous
    seq ordering, the reassembled total, and the whole-file CRC against the
    reply — end-to-end integrity the base64 path never had.
    """
    t_cmd = time.monotonic()
    lane_deadline = time.monotonic() + _LIVE_LANE_RETRY_S
    while True:
        rep, frames = await session.command_with_frames(
            f"voicefetch {protocol.quote_path(path)}",
            timeout=protocol.DEFAULT_CMD_TIMEOUT_S,
            cancel_guard=cancel_guard)
        if (rep.ok or _LIVE_LANE_BUSY_TEXT not in rep.text
                or time.monotonic() >= lane_deadline):
            break
        log.info("voicefetch waiting for the live lane to drain")
        await asyncio.sleep(_LIVE_LANE_POLL_S)
    t_wire = time.monotonic()
    if not rep.ok:
        raise FetchError(f"voicefetch failed: {rep.text}")
    if not frames:
        raise FetchError("voicefetch returned no frames")

    total_expected: int | None = None
    chunks: dict[int, bytes] = {}
    for body in frames:
        ftype, seq, payload = protocol.parse_frame_body(body)  # re-parse (cheap, authoritative)
        if ftype == protocol.FRAME_META:
            if len(payload) != 4:
                raise FetchError("bad META frame length")
            total_expected = int.from_bytes(payload, "little")
        elif ftype == protocol.FRAME_AUDIO:
            if seq in chunks:
                raise FetchError(f"duplicate audio frame seq {seq}")
            chunks[seq] = payload
        else:
            log.debug("ignoring unknown frame type 0x%02x", ftype)

    if total_expected is None:
        raise FetchError("voicefetch stream had no META frame")
    # Audio frames are seq 1..N, contiguous.
    out = bytearray()
    for seq in range(1, len(chunks) + 1):
        if seq not in chunks:
            raise FetchError(f"missing audio frame seq {seq} "
                             f"(have {len(chunks)}, gap detected)")
        out.extend(chunks[seq])
    if len(out) != total_expected:
        raise FetchError(
            f"reassembled {len(out)} bytes, META declared {total_expected}")

    m = _VOICEFETCH_CRC_RE.search(rep.text)
    if m:
        want = int(m.group(1), 16)
        got = protocol.crc16_ccitt(bytes(out))
        if got != want:
            raise FetchError(f"whole-file CRC mismatch: got {got:04X}, reply says {want:04X}")
    # Per-frame cost is THE number to watch on this path, and it is directly
    # comparable to the firmware's own LOOPHEALTH stall duration. Expected
    # composition at 2 Mbaud: ~5.14 ms wire + SD read + ~0.37 ms firmware CRC.
    # The SD term is ~2.47 ms/frame at a 4 MHz card clock and ~0.99 at 10 MHz,
    # so a regression here points at which mount rung the card landed on.
    # `reassemble` is host-side only (dedupe + CRC + concat) and should stay
    # sub-millisecond now that crc_hqx is in; if it grows, the CRC swap regressed.
    wire_s = t_wire - t_cmd
    reasm_ms = (time.monotonic() - t_wire) * 1000.0
    per_frame = (wire_s * 1000.0 / len(frames)) if frames else 0.0
    log.info("voicefetch OK: %d bytes, %d frames, CRC verified | %.2fs wire "
             "(%.2f ms/frame, %.0f kB/s) + %.1f ms reassemble",
             len(out), len(frames), wire_s, per_frame,
             (len(out) / 1024.0) / wire_s if wire_s > 0 else 0.0, reasm_ms)
    return bytes(out)


async def read_file_b64(session: Session, path: str, chunk_request: int, *,
                        cancel_guard=None) -> bytes:
    """Pull any file via the chunked fileread b64 envelope."""
    out = bytearray()
    offset = 0
    expected_size: int | None = None
    while True:
        cmd = f"fileread {protocol.quote_path(path)} {offset} {chunk_request} b64"
        rep = await session.command(
            cmd, expect="json", cancel_guard=cancel_guard)
        env = rep.json
        if not isinstance(env, dict):
            raise FetchError(f"fileread: no JSON envelope: {rep.text!r}")
        if not env.get("success"):
            raise FetchError(f"fileread failed at offset {offset}: {env.get('error')}")
        if env.get("enc") not in (None, "b64"):
            raise FetchError(f"fileread: unexpected encoding {env.get('enc')!r}")

        size = int(env.get("size", -1))
        if expected_size is None:
            expected_size = size
            log.info("fetching %s: %d bytes", path, size)
        elif size != expected_size:
            raise FetchError(
                f"file changed mid-read (size {expected_size} -> {size})")

        got = int(env.get("len", 0))
        data = env.get("data", "")
        if got:
            raw = base64.b64decode(data, validate=True)
            if len(raw) != got:
                raise FetchError(
                    f"chunk length mismatch at {offset}: envelope says {got}, "
                    f"decoded {len(raw)}")
            if int(env.get("offset", offset)) != offset:
                raise FetchError(
                    f"offset mismatch: asked {offset}, envelope says {env.get('offset')}")
            out.extend(raw)
            offset += got
        if env.get("eof") or got == 0:
            break

    if expected_size is not None and len(out) != expected_size:
        raise FetchError(f"reassembly incomplete: {len(out)}/{expected_size} bytes")
    return bytes(out)


async def _ensure_mic(session: Session) -> None:
    rep = await session.command("openmic", expect="status")
    if rep.ok:
        return
    # Belt-and-braces: real firmware replies SUCCESS when the mic is already
    # running, but tolerate an "already ..." error shape too.
    if "already" in rep.text.lower():
        return
    raise FetchError(f"openmic failed: {rep.text}")


def _parse_recording_path(rep: Reply) -> str:
    m = _STOP_PATH_RE.search(rep.text) or _FALLBACK_PATH_RE.search(rep.text)
    if not m:
        raise FetchError(
            f"could not parse recording path from micrecord stop reply: {rep.text!r}")
    return m.group(1)
