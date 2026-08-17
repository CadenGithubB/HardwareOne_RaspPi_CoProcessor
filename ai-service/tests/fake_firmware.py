"""A firmware double for tests: speaks the REAL drain protocol over a pty.

Mirrors System_UartLink.cpp + the commands the P0 pipeline uses, verified
against the firmware source (see ARCHITECTURE.md §4):
  - login gate: "OK: logged in as <u>", rate-limited auth nag (one per 2s),
    silent drop of commands while unauthenticated inside the nag window
  - success replies stamped "OK: " (stampOkStatus behavior); JSON replies
    exempt (returned bare)
  - openmic / micrecord start|stop (stop reply carries the WAV path)
  - fileread "<path>" <off> <len> b64 -> {success,size,offset,len,eof,enc,data}
    honoring the firmware's rawCap = ((4096-192-len(path))/4)*3
  - micdelete "<name>", oledtext, oledstart, g2notify, uartlink status
  - fault injection: garbage bursts (ROM boot spew), forced reboot, OLED
    running/not-running state

Replies are written as ONE os.write (blob + newline) exactly like the
firmware's single-blob reply write.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import math
import os
import re
import struct
import threading
import time


TEST_EVENAI_ID = "a1b2c3d400000001"


def _cobs_encode(src: bytes) -> bytes:
    """Mirror of the firmware's uartCobsEncode (System_UartLink.cpp). The
    client only DECODES, so the test owns the encoder — same as the firmware
    being the only encoder on real hardware."""
    dst = bytearray([0])          # placeholder for first code
    code_pos = 0
    code = 1
    for byte in src:
        if byte == 0:
            dst[code_pos] = code
            code_pos = len(dst)
            dst.append(0)
            code = 1
        else:
            dst.append(byte)
            code += 1
            if code == 0xFF:
                dst[code_pos] = code
                code_pos = len(dst)
                dst.append(0)
                code = 1
    dst[code_pos] = code
    return bytes(dst)


def make_wav(seconds: float = 1.0, rate: int = 16000, freq: float = 440.0) -> bytes:
    n = int(seconds * rate)
    pcm = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * freq * i / rate)))
        for i in range(n))
    out = io.BytesIO()
    out.write(b"RIFF")
    out.write(struct.pack("<I", 36 + len(pcm)))
    out.write(b"WAVEfmt ")
    out.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    out.write(b"data")
    out.write(struct.pack("<I", len(pcm)))
    out.write(pcm)
    return out.getvalue()


class FakeFirmware:
    def __init__(self, *, user: str = "cm5svc", password: str = "pw",
                 role: str = "admin",
                 require_auth: bool = True, wav_bytes: bytes | None = None,
                 oled_running: bool = False):
        self.user = user
        self.password = password
        self.role = role
        self.require_auth = require_auth
        self.wav_bytes = wav_bytes if wav_bytes is not None else make_wav()
        self.oled_running = oled_running
        self.mic_disabled = False       # True -> openmic replies uppercase ERROR
        self.support_voicefetch = True  # False -> simulate pre-P2 firmware
        # Optional per-frame stall lets cancellation/EVT tests interleave with
        # a stream instead of racing a whole in-memory burst.
        self.voicefetch_frame_delay_s = 0.0
        self.voicefetch_event_after_frames: tuple[int, str] | None = None
        self.support_vad = True         # False -> simulate pre-VAD firmware (rejects `start vad`)
        # A VAD recording auto-stops after this many bare `micrecord` status
        # polls, standing in for the device's real silence detector. None ->
        # never auto-stops (only an explicit `micrecord stop` ends it).
        self.vad_auto_stop_after: int | None = 2
        # Straggler injection: {command-line-or-first-token: seconds}. The
        # delay is consumed on FIRST use and blocks the (serial) handler
        # thread, exactly like a busy cmd_exec delays the real reply.
        self.delay_once: dict[str, float] = {}

        self.authed_user: str | None = None
        self.mic_open = False
        self.mic_source = "pdm"
        self.recording = False
        self._vad_armed = False
        self._vad_polls = 0
        self._rec_seq = 0
        self._evt_seq = 0
        self._last_path: str | None = None
        self._recording_owner: str | None = None
        # Production retains owner-scoped terminal results after a WAV is
        # removed so status/stop/discard remain exact and idempotent. ``None``
        # is the retained, deliberately-discarded disposition.
        self._owned_results: dict[str, str | None] = {}
        self.files: dict[str, bytes] = {}
        self.oled_texts: list[str] = []
        self.g2_texts: list[str] = []
        self.deleted: list[str] = []
        self.command_log: list[str] = []
        # EvenAI ("Hey Even") native-session double: g2evenai targets.
        self.evenai_active = False
        self.evenai_exchange_id: str | None = None
        self.push_mic_autostop = True
        self.evenai_asks: list[str] = []
        self.evenai_replies: list[str] = []       # one-shot `reply` texts
        self.evenai_reply_parts: list[str] = []   # streamed deltas, verbatim
        self.evenai_reply_ended = False
        self.evenai_streaming = False
        self.evenai_stream_speeds: list[int] = []
        self.cm5_power_acks: list[tuple[str, str]] = []
        self.cm5_power_reports: list[tuple[str, str, str, str]] = []
        self.cm5_fan_acks: list[tuple[str, str]] = []
        self.cm5_fan_reports: list[
            tuple[str, str, str, int, int, int, int, str]
        ] = []
        self.support_cm5_presence = True
        self.cm5_session_generation = 0
        self.cm5_session_epoch = 0
        self.cm5_presence_epoch = 0
        self.cm5_task_started = False
        self.cm5_mode: str | None = None
        self.cm5_sequence = 0
        self.cm5_last_seen = 0.0
        # Mirror the firmware's bounded liveness bridge around ordinary UART
        # registry commands.  A command may keep an already-fresh CM5 session
        # alive while it owns cmd_exec, followed by at most five seconds of
        # grace after its reply.  CM5 protocol commands themselves are not
        # bracketed, so status remains an observation rather than activity.
        self.cm5_command_in_flight = False
        self.cm5_command_grace = False
        self.cm5_command_was_fresh = False
        self.cm5_command_started = 0.0
        self.cm5_command_finished = 0.0

        # Synthetic live-pcm-v1 probe state. The producer is asynchronous so
        # the command ACK does not hold the host Session lock through the stream
        # (the real host must remain able to renew its short ready lease).
        self.live_controller_id: int | None = None
        self.live_lease_ttl_s = 3.0
        self.live_renew_ms = 1000
        # Current firmware marks the cheap renewal intrinsic explicitly.
        # Tests set this false to model older firmware, which advertised the
        # same 1000 ms timing but routed every ready through cmd_exec.
        self.live_renew_direct = True
        self.live_lease_expires_at = 0.0
        self.live_exchange_id: int | None = None
        self.live_stream_session_epoch = 0
        self.live_synth_thread: threading.Thread | None = None
        self.live_abort = threading.Event()
        self.live_abort_reason = 0
        self.live_last_terminal = "idle"
        self.live_last_exchange: int | None = None
        self.live_last_sent = 0
        self.live_last_dropped = 0
        self.live_last_crc32 = 0
        self.live_last_terminal_sent = False
        self.live_shadow_suppress_last_update = False
        self.live_shadow_armed = False
        self.live_shadow_native = False
        self.live_shadow_expected_exchange: int | None = None
        self.live_shadow_thread: threading.Thread | None = None
        self.live_shadow_capture_done = threading.Event()
        self.live_shadow_begin_sent = threading.Event()
        self.live_shadow_terminal_sent = threading.Event()
        self.live_shadow_frame_delay_s = 0.001
        self.live_shadow_drop_frame_index: int | None = None
        self.live_shadow_terminal_crc_xor = 0
        self.live_shadow_pcm_xor = 0
        self.live_lease_session_epoch = 0
        self.evenai_uart_epoch = 1
        self.native_begin_exchange_id: str | None = None
        self.native_prebegin_foreign_exchange_id: str | None = None
        self.native_trim_padding_samples = 500
        self.native_wake_before_begin = False
        self.native_autostop_before_end = False
        # Durable fake-side wire journal for tests that must prove both legal
        # producer orderings.  Host callback timestamps cannot establish wire
        # order because LIVE frames and EVT frames use different consumers.
        self.native_emit_order: list[str] = []

        self._last_nag = 0.0
        self._master_fd = -1
        self._slave_keepalive_fd = -1
        self._slave_path = ""
        self._thread: threading.Thread | None = None
        self._closing = False
        self._lock = threading.Lock()
        self._session_fence = threading.Lock()
        self._tx_lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------

    # macOS resolves a freshly cloned pty's /dev/ttysNNN through a devfs
    # lookup that transiently misses under parallel pty churn (pytest -n auto
    # against a SYSTEM-WIDE kern.tty.ptmx_max of 511). Libc reports that miss
    # as ERANGE out of ttyname_r, which surfaces here as OSError errno 34 --
    # not the ENXIO you get from actually exhausting the pool. The pty is
    # fine; only the name lookup raced, so retry with a fresh pair.
    _PTY_NAME_ATTEMPTS = 5

    def _close_fds(self) -> None:
        for fd_attr in ("_master_fd", "_slave_keepalive_fd"):
            fd = getattr(self, fd_attr, -1)
            if fd is None or fd < 0:
                continue
            try:
                os.close(fd)
            except OSError:
                pass
            # Clear so a second stop() cannot close an unrelated fd that the
            # process has since recycled onto the same number.
            setattr(self, fd_attr, -1)

    def start(self) -> str:
        for attempt in range(self._PTY_NAME_ATTEMPTS):
            master, slave = os.openpty()
            # Claim BOTH fds before anything that can raise. If ttyname below
            # throws with the slave still unclaimed, stop() cannot find it and
            # the pty leaks for the worker's whole lifetime -- which feeds the
            # very pressure that caused the miss, so one flake breeds the next.
            self._master_fd = master
            # Keep OUR slave fd open: if every slave fd closes, reads on the
            # master return EOF and the firmware thread would exit before the
            # client (which reopens the slave by path) ever connects.
            self._slave_keepalive_fd = slave
            try:
                self._slave_path = os.ttyname(slave)
            except OSError:
                self._close_fds()
                if attempt == self._PTY_NAME_ATTEMPTS - 1:
                    raise
                time.sleep(0.05 * (attempt + 1))
                continue
            break

        self._thread = threading.Thread(target=self._main, daemon=True,
                                        name="fake-firmware")
        self._thread.start()
        return self._slave_path

    def stop(self) -> None:
        # Join FIRST, close after: on macOS, closing a pty master while
        # another thread is blocked reading it can wedge in the kernel.
        # The reader polls via select, so it notices _closing within 100ms.
        self._closing = True
        self.live_abort_reason = 3  # protocol: link lost
        self.live_abort.set()
        for live_thread in (self.live_synth_thread, self.live_shadow_thread):
            if live_thread is not None:
                live_thread.join(timeout=2)
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._close_fds()

    # -- fault injection ---------------------------------------------------

    def inject_garbage(self, n: int = 200) -> None:
        """ROM-boot-burst lookalike: high-bit trash with embedded newlines."""
        burst = bytes((i * 37 + 130) % 256 for i in range(n))
        self._write_raw(burst + b"\n")

    def reboot(self) -> None:
        """Garbage burst + all session state dropped (what a reset does)."""
        with self._lock:
            with self._session_fence:
                self.authed_user = None
                self.cm5_session_generation = 0
                self.cm5_session_epoch = 0
            self.cm5_presence_epoch = 0
            self.cm5_task_started = False
            self.cm5_mode = None
            self.cm5_sequence = 0
            self.cm5_last_seen = 0.0
            self.cm5_command_in_flight = False
            self.cm5_command_grace = False
            self.cm5_command_was_fresh = False
            self.cm5_command_started = 0.0
            self.cm5_command_finished = 0.0
            self.mic_open = False
            self.recording = False
            self._recording_owner = None
            if self.live_exchange_id is not None:
                self.live_abort_reason = 2  # protocol: auth/session lost
                self.live_abort.set()
            # A device reset destroys every liveaudio static. In particular,
            # the next named login legitimately reuses epoch 1, so retaining
            # an epoch-1 lease here would make the fake's renewal classifier
            # resurrect authority that no longer exists on real firmware.
            self.live_controller_id = None
            self.live_lease_session_epoch = 0
            self.live_lease_expires_at = 0.0
            self.live_shadow_armed = False
            self.live_shadow_native = False
            self.live_shadow_expected_exchange = None
            self.live_exchange_id = None
            self.live_stream_session_epoch = 0
            self.live_last_terminal = "idle"
            self.live_last_exchange = None
            self.live_last_sent = 0
            self.live_last_dropped = 0
            self.live_last_crc32 = 0
            self.live_last_terminal_sent = False
        self.inject_garbage(300)

    # -- EvenAI wake double --------------------------------------------------

    def push_event(self, text: str) -> None:
        """One EVT frame, exactly like uartLinkPushEvent (own seq counter)."""
        from hw1_ai_service.link import protocol as P
        self._evt_seq += 1
        self._write_raw(self._frame_wire(P.FRAME_EVT, self._evt_seq,
                                         text.encode("ascii")))

    def begin_wake_capture(self, *, push: bool = True,
                           exchange_id: str = TEST_EVENAI_ID,
                           start_shadow: bool = True) -> None:
        """Simulate the firmware's wake auto-start: a VAD-armed recording is
        already running (openmic + micrecord start vad happened on-device)
        and the evenai_wake EVT goes out once the recording is live."""
        with self._lock:
            self.mic_open = True
            self.recording = True
            self._vad_armed = True
            self._vad_polls = 0
            self._recording_owner = exchange_id
            self.evenai_active = True
            self.evenai_exchange_id = exchange_id
        if push and self.native_wake_before_begin:
            self.native_emit_order.append("evenai_wake")
            self.push_event(f"evenai_wake {exchange_id}")
        # Native System_LiveAudio converts the persistent native lease arm to
        # the exact firmware-issued exchange immediately before startid.
        if start_shadow:
            if self.native_prebegin_foreign_exchange_id is not None:
                from hw1_ai_service.link import protocol as P
                assert self.live_controller_id is not None
                foreign = int(self.native_prebegin_foreign_exchange_id, 16)
                payload = struct.pack(
                    "<BBQQIH", 1, 0, foreign, self.live_controller_id,
                    0, 1) + b"\x00\x00"
                self._write_raw(
                    self._frame_wire(P.FRAME_LIVE_PCM, 0, payload))
            begin_id = self.native_begin_exchange_id or exchange_id
            self._start_live_shadow(int(begin_id, 16))
        if push and not self.native_wake_before_begin:
            if start_shadow:
                self.live_shadow_begin_sent.wait(timeout=1.0)
            self.native_emit_order.append("evenai_wake")
            self.push_event(f"evenai_wake {exchange_id}")

    def dismiss_evenai(self, reason: str = "dismiss", *, push: bool = True) -> None:
        with self._lock:
            self.evenai_active = False
            eid = self.evenai_exchange_id
            if self.recording and self._recording_owner == eid:
                self._finish_recording(push_event=False)
                self._discard_owned_result(eid)
        if eid and push:
            self.push_event(f"evenai_cancel {eid} {reason}")

    # -- protocol ----------------------------------------------------------

    def _main(self) -> None:
        import select
        buf = bytearray()
        while not self._closing:
            ready, _, _ = select.select([self._master_fd], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(self._master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            buf.extend(chunk)
            while True:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line = bytes(buf[:nl]).rstrip(b"\r").decode("utf-8", "replace").strip()
                del buf[:nl + 1]
                if line:
                    self._handle(line)

    def _reply(self, text: str) -> bool:
        """One blob + newline in a single write — the firmware's TX shape."""
        return self._write_raw(text.encode("utf-8") + b"\n")

    def _write_raw(self, data: bytes) -> bool:
        # Mirror the firmware link-level TX mutex: async live frames, EVT, and
        # command replies are each one write and never byte-interleave.
        with self._tx_lock:
            try:
                return os.write(self._master_fd, data) == len(data)
            except OSError:
                return False

    def _stamp_ok(self, text: str) -> str:
        # Mirrors stampOkStatus: failures ("Error"/"ERROR"), JSON, and
        # unknown-command replies are never stamped.
        if text.startswith(("OK", "Error", "ERROR", "Unknown", "{", "[")):
            return text
        return f"OK: {text}"

    def _reject_legacy_evenai(self) -> str:
        """Mirror production: legacy mutation closes, never mutates, a card."""
        had_active = self.evenai_active
        eid = self.evenai_exchange_id
        if had_active:
            self.evenai_active = False
            self.evenai_streaming = False
            if self.recording and self._recording_owner == eid:
                self._finish_recording(push_event=False)
                self._discard_owned_result(eid)
        return (
            "Error: G2: tagged EvenAI exchange ID required; active "
            "exchange terminated" if had_active else
            "Error: G2: tagged EvenAI exchange ID required; use "
            "askid/replyid/replypartid/replyendid/exitid")

    def _cm5_snapshot_locked(self, now: float | None = None) -> dict[str, object]:
        """Return the authoritative CM5 lease view for the active login."""
        if now is None:
            now = time.monotonic()
        has_record = self.cm5_mode is not None
        seen = (has_record and self.cm5_session_epoch != 0 and
                self.cm5_presence_epoch == self.cm5_session_epoch)
        age_s = max(0.0, now - self.cm5_last_seen) if has_record else 0.0
        lease_s = 75.0 if self.cm5_mode == "busy" else 15.0
        heartbeat_fresh = seen and age_s < lease_s
        command_age_s = max(0.0, now - self.cm5_command_started)
        command_within_cap = (seen and self.cm5_command_was_fresh and
                              command_age_s < 75.0)
        command_grace = (command_within_cap and self.cm5_command_grace and
                         max(0.0, now - self.cm5_command_finished) < 5.0)
        command_fresh = command_within_cap and (
            self.cm5_command_in_flight or command_grace)
        return {
            "has_record": has_record,
            "seen": seen,
            "age_ms": int(age_s * 1000),
            "lease_ms": int(lease_s * 1000),
            "fresh": heartbeat_fresh or command_fresh,
            "command_in_flight": seen and self.cm5_command_in_flight,
            "command_grace": command_grace,
        }

    def _live_session_may_control(self) -> bool:
        """Mirror requireMutableTransport's named-account requirement."""
        return (self.authed_user is not None and
                self.cm5_session_epoch != 0 and
                self.role in {"user", "admin", "superadmin"})

    def _live_session_epoch_is_current(self, session_epoch: int) -> bool:
        return (session_epoch != 0 and self._live_session_may_control() and
                self.cm5_session_epoch == session_epoch)

    def _live_lease_matches(self, controller: int,
                            now: float | None = None) -> bool:
        """Mirror leaseMatchesLocked(), including the exact login epoch."""
        if now is None:
            now = time.monotonic()
        return (self._live_session_may_control() and
                self.live_controller_id == controller and
                self.live_lease_session_epoch == self.cm5_session_epoch and
                now < self.live_lease_expires_at)

    def _cm5_bridges_command(self, line: str) -> bool:
        """Match the UART adapter's exclusions from command grace."""
        # Mirrors cm5PresenceIsProtocolCommand(): only the PRESENCE verbs are
        # daemon housekeeping. `cm5 power ...` / `cm5 fan ...` are real work
        # and bridge exactly like the old hostpower/hostfan commands did.
        if re.match(r"cm5(?:\s+(status|capabilities|heartbeat)(?:\s|$)|\s*$)",
                    line.strip(), re.IGNORECASE):
            return False
        normalized = line.strip().lower()
        if re.match(r"liveaudio\s+(status|capabilities)(?:\s|$)",
                    normalized):
            return False
        # Unlike status/capabilities, the firmware's renewal-only parser is
        # deliberately case-sensitive. It accepts token whitespace but not a
        # differently-cased command that the ordinary registry must diagnose.
        ready = re.fullmatch(
            r"liveaudio[\t\n\v\f\r ]+ready[\t\n\v\f\r ]+1"
            r"[\t\n\v\f\r ]+([0-9a-f]{16})", line.strip())
        if ready:
            # The firmware UART intrinsic handles only a healthy exact
            # renewal. Initial acquisition, expiry repair, and controller
            # mismatch fall through to cmd_exec and retain the busy fence.
            if not self.live_renew_direct:
                return True
            return not self._live_lease_matches(int(ready.group(1), 16))
        if normalized == "logout" or normalized == "whoami":
            return False
        return not normalized.startswith("login ")

    def _cm5_command_started_locked(self) -> None:
        now = time.monotonic()
        snapshot = self._cm5_snapshot_locked(now)
        if (self.cm5_presence_epoch == self.cm5_session_epoch and
                self.cm5_command_grace):
            # As in firmware, grace cannot be chained by another command.
            self.cm5_command_grace = False
            self.cm5_command_was_fresh = False
        heartbeat_fresh = (snapshot["seen"] and
                           snapshot["age_ms"] < snapshot["lease_ms"])
        if heartbeat_fresh:
            self.cm5_command_in_flight = True
            self.cm5_command_was_fresh = True
            self.cm5_command_started = now

    def _cm5_command_finished_locked(self, reply_admitted: bool) -> None:
        if (self.cm5_presence_epoch != self.cm5_session_epoch or
                not self.cm5_command_in_flight):
            return
        now = time.monotonic()
        self.cm5_command_in_flight = False
        self.cm5_command_finished = now
        command_age_s = max(0.0, now - self.cm5_command_started)
        self.cm5_command_grace = (reply_admitted and
                                  self.cm5_command_was_fresh and
                                  command_age_s < 75.0)
        if not self.cm5_command_grace:
            self.cm5_command_was_fresh = False

    def _handle(self, line: str) -> None:
        self.command_log.append(line)
        bridge_command = self._cm5_bridges_command(line)
        if bridge_command:
            with self._lock:
                self._cm5_command_started_locked()
        delay = self.delay_once.pop(line, None)
        if delay is None:
            delay = self.delay_once.pop(line.split(" ")[0], None)
        if delay:
            time.sleep(delay)   # handler thread blocks = late reply, like a busy cmd_exec
        with self._lock:
            reply = self._dispatch(line)
        reply_admitted = False
        if reply is not None:
            reply_admitted = self._reply(self._stamp_ok(reply))
        if bridge_command:
            with self._lock:
                self._cm5_command_finished_locked(reply_admitted)

    def _dispatch(self, line: str) -> str | None:
        if re.match(r"login(?:\s|$)", line, re.IGNORECASE):
            # Exact firmware CommandArgs subset: ASCII whitespace separates
            # unquoted tokens; a token beginning with `"` consumes through
            # the next quote with no escape syntax. Do not use shlex here—its
            # mid-token quote/backslash rules differ from the firmware.
            raw = line[len("login"):].strip()
            parts: list[str] = []
            pos = 0
            while pos < len(raw):
                while pos < len(raw) and raw[pos].isspace():
                    pos += 1
                if pos >= len(raw):
                    break
                if raw[pos] == '"':
                    end = raw.find('"', pos + 1)
                    if end < 0:
                        parts = []
                        break
                    parts.append(raw[pos + 1:end])
                    pos = end + 1
                else:
                    end = pos
                    while end < len(raw) and not raw[end].isspace():
                        end += 1
                    parts.append(raw[pos:end])
                    pos = end
            if len(parts) != 2:
                return "Usage: login <username> <password>"
            if parts[0] == self.user and parts[1] == self.password:
                with self._session_fence:
                    self.authed_user = parts[0]
                    self.cm5_session_generation += 1
                    self.cm5_session_epoch = self.cm5_session_generation
                suffix = " (admin)" if self.role in {"admin", "superadmin"} else ""
                return f"OK: logged in as {parts[0]}{suffix}"
            return "Error: authentication failed"

        if self.require_auth and self.authed_user is None:
            # Firmware nag is rate-limited: one per 2s, otherwise SILENCE.
            now = time.monotonic()
            if now - self._last_nag >= 2.0:
                self._last_nag = now
                return ("Error: authentication required. "
                        "Use: login <username> <password>")
            return None

        if line == "logout":
            with self._session_fence:
                self.authed_user = None
                self.cm5_session_epoch = 0
            return "OK: logged out"
        if line == "uartlink status":
            return ("OK: UART link: running (enabled=1) uart0 tx=43 rx=44 "
                    "baud=921600 auth=required user=" + (self.authed_user or "(none)"))
        cm5_namespace = bool(re.match(r"cm5(?:\s|$)", line, re.IGNORECASE))
        cm5_heartbeat_namespace = bool(re.match(
            r"cm5\s+heartbeat(?:\s|$)", line, re.IGNORECASE))
        # Host power/fan live in the same `cm5` namespace but are handled by
        # their own matchers further down, so they must not be swallowed by
        # the presence usage catch-all.
        cm5_host_control = bool(re.match(
            r"cm5\s+(power|fan)(?:\s|$)", line, re.IGNORECASE))
        if (cm5_namespace and not cm5_heartbeat_namespace and
                self.authed_user is not None and self.role == "guest"):
            return ("Error: Guest accounts are view-only. "
                    "Only login/logout are allowed.")
        if self.support_cm5_presence and re.fullmatch(
                r"cm5\s+capabilities", line, re.IGNORECASE):
            return ("OK: cm5-presence-v1 heartbeat_modes="
                    "starting,ready,busy,degraded interval_ms=5000 "
                    "lease_ms=15000 busy_lease_ms=75000 "
                    "cmd_grace_ms=5000")
        if self.support_cm5_presence and re.fullmatch(
                r"cm5\s+status", line, re.IGNORECASE):
            state = self.cm5_mode or "unknown"
            snapshot = self._cm5_snapshot_locked()
            has_record = bool(snapshot["has_record"])
            seen = bool(snapshot["seen"])
            age_ms = int(snapshot["age_ms"])
            lease_ms = int(snapshot["lease_ms"])
            fresh = bool(snapshot["fresh"])
            return (f"OK: CM5 presence task="
                    f"{'running' if self.cm5_task_started else 'dormant'} "
                    f"state={state} fresh={int(fresh)} seen={int(seen)} "
                    f"epoch={self.cm5_presence_epoch if has_record else 0} "
                    f"seq={self.cm5_sequence} age_ms={age_ms} "
                    f"lease_ms={lease_ms} "
                    f"cmd_busy={int(snapshot['command_in_flight'])} "
                    f"cmd_grace={int(snapshot['command_grace'])} "
                    f"monitor={int(fresh)} stale_n=0 "
                    f"stack_free_min={1024 if seen else 0}")
        heartbeat_line = self.support_cm5_presence and cm5_heartbeat_namespace
        if heartbeat_line:
            m = re.fullmatch(
                r"cm5\s+heartbeat\s+1\s+([0-9]+)\s+"
                r"(starting|ready|busy|degraded)", line, re.IGNORECASE)
            if not m:
                return ("Error: Usage: cm5 heartbeat 1 <sequence> "
                        "<starting|ready|busy|degraded>")
            sequence = int(m.group(1), 10)
            if sequence == 0 or sequence > 0xFFFFFFFF:
                return ("Error: Usage: cm5 heartbeat 1 <sequence> "
                        "<starting|ready|busy|degraded>")
            if self.authed_user is None or self.cm5_session_epoch == 0:
                return ("Error: cm5 heartbeat requires a named "
                        "authenticated UART session")
            if self.role not in {"user", "admin", "superadmin"}:
                return ("Error: Guest accounts are view-only. "
                        "Only login/logout are allowed.")
            self.cm5_sequence = sequence
            self.cm5_mode = m.group(2).lower()
            self.cm5_presence_epoch = self.cm5_session_epoch
            self.cm5_task_started = True
            self.cm5_last_seen = time.monotonic()
            lease_ms = 75000 if self.cm5_mode == "busy" else 15000
            return (f"OK: cm5 heartbeat version=1 seq={sequence} "
                    f"state={self.cm5_mode} "
                    f"session_epoch={self.cm5_session_epoch} "
                    f"lease_ms={lease_ms}")
        if self.support_cm5_presence and re.match(
                r"cm5\s+status(?:\s|$)", line, re.IGNORECASE):
            return "Error: Usage: cm5 status"
        if self.support_cm5_presence and re.match(
                r"cm5\s+capabilities(?:\s|$)", line, re.IGNORECASE):
            return "Error: Usage: cm5 capabilities"
        if self.support_cm5_presence and cm5_namespace and not cm5_host_control:
            normalized = line.strip().lower()
            if normalized == "cm5 heartbeat" or normalized.startswith(
                    "cm5 heartbeat "):
                return ("Error: cm5 heartbeat is available only on the "
                        "authenticated UART host link")
            return ("Error: Usage: cm5 <status|capabilities> "
                    "(heartbeat is UART control-plane only)")
        live_namespace = bool(re.match(
            r"liveaudio(?:\s|$)", line, re.IGNORECASE))
        if (live_namespace and self.authed_user is not None and
                self.role not in {"user", "admin", "superadmin"}):
            return ("Error: Guest accounts are view-only. "
                    "Only local login/logout and whoami are allowed.")
        if line == "liveaudio capabilities":
            return ("OK: live-pcm-v1 synthetic=1 recorder_shadow=1 "
                    f"shadow_default=off protocol=1 "
                    + ("renew_direct=1 " if self.live_renew_direct else "") +
                    f"lease_ttl_ms={int(self.live_lease_ttl_s * 1000)} "
                    f"lease_renew_ms={self.live_renew_ms} "
                    "max_pcm_samples=500")
        m = re.fullmatch(r"liveaudio ready 1 ([0-9a-f]{16})", line)
        if m:
            controller = int(m.group(1), 16)
            if not self._valid_live_id(controller):
                return "Error: invalid live controller ID"
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            # Fidelity (System_LiveAudio.cpp 'ready', verified 2026-08-11):
            # only an UNEXPIRED lease held by another controller is busy; a
            # lapsed lease re-mints — and the re-mint SILENTLY WIPES the
            # shadow flags (sLease = LeaseState{}) while replying plain OK
            # with no token revealing the arm was lost. This is the exact
            # trap LiveSttGate._renew_loop exists to defend against; the
            # fake must model it or the defense is untestable.
            now = time.monotonic()
            unexpired = (self.live_controller_id is not None
                         and now < self.live_lease_expires_at)
            current = (unexpired and self.cm5_session_epoch != 0
                       and self.live_lease_session_epoch
                       == self.cm5_session_epoch)
            if unexpired and not current and self.live_exchange_id is not None:
                return "Error: liveaudio lease busy"
            if current and self.live_controller_id != controller:
                return "Error: liveaudio lease busy"
            if not current:
                if self.live_exchange_id is not None:
                    return "Error: liveaudio lease busy"
                self.live_shadow_armed = False
                self.live_shadow_native = False
                self.live_shadow_expected_exchange = None
                self.live_controller_id = controller
                self.live_lease_session_epoch = self.cm5_session_epoch
            self.live_lease_expires_at = now + self.live_lease_ttl_s
            return (f"OK: liveaudio ready version=1 "
                    f"controller={controller:016x} "
                    f"session_epoch={self.live_lease_session_epoch} "
                    + ("renew_direct=1 " if self.live_renew_direct else "") +
                    f"lease_ttl_ms={int(self.live_lease_ttl_s * 1000)} "
                    f"renew_ms={self.live_renew_ms} baud=921600")
        m = re.fullmatch(
            r"liveaudio shadow 1 ([0-9a-f]{16}) on ([0-9a-f]{16}|native)",
            line)
        if m:
            controller = int(m.group(1), 16)
            owner_token = m.group(2)
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            if not self._live_lease_matches(controller):
                return "Error: liveaudio lease does not match controller"
            if owner_token == "native":
                expected = None
            else:
                expected = int(owner_token, 16)
                if not self._valid_live_id(expected):
                    return "Error: invalid live exchange ID"
            # Fidelity: the real 'shadow on' handler REPLACES the shadow
            # config unconditionally on a lease match (no already-armed
            # error) — a re-arm overwrites and replies OK.
            self.live_shadow_armed = True
            self.live_shadow_native = owner_token == "native"
            self.live_shadow_expected_exchange = expected
            target = "native" if self.live_shadow_native else f"{expected:016x}"
            return (f"OK: live-pcm-v1 recorder shadow armed "
                    f"controller={controller:016x} target={target}")
        m = re.fullmatch(r"liveaudio shadow 1 ([0-9a-f]{16}) off", line)
        if m:
            controller = int(m.group(1), 16)
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            if not self._live_lease_matches(controller):
                return "Error: liveaudio lease does not match controller"
            self.live_shadow_armed = False
            self.live_shadow_native = False
            self.live_shadow_expected_exchange = None
            if self.live_exchange_id is not None:
                self.live_abort_reason = 5  # protocol: host request
                self.live_abort.set()
            return (f"OK: live-pcm-v1 recorder shadow disarmed "
                    f"controller={controller:016x}")
        m = re.fullmatch(r"liveaudio release 1 ([0-9a-f]{16})", line)
        if m:
            controller = int(m.group(1), 16)
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            if (controller != self.live_controller_id or
                    self.live_lease_session_epoch != self.cm5_session_epoch):
                return "Error: liveaudio lease does not match controller"
            self.live_abort_reason = 4  # protocol: released
            self.live_abort.set()
            self.live_shadow_armed = False
            self.live_shadow_native = False
            self.live_shadow_expected_exchange = None
            self.live_controller_id = None
            self.live_lease_session_epoch = 0
            self.live_lease_expires_at = 0.0
            return f"OK: live-pcm-v1 released controller={controller:016x}"
        m = re.fullmatch(
            r"liveaudio synth 1 ([0-9a-f]{16}) ([0-9a-f]{16}) (\d+)", line)
        if m:
            controller = int(m.group(1), 16)
            exchange = int(m.group(2), 16)
            duration_ms = int(m.group(3))
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            if not self._live_lease_matches(controller):
                return "Error: liveaudio lease missing, mismatched, or expired"
            if not self._valid_live_id(exchange):
                return "Error: invalid live exchange ID"
            if not (1 <= duration_ms <= 60_000):
                return "Error: synth duration must be 1..60000 ms"
            if self.live_exchange_id is not None:
                return "Error: live stream already active"
            self.live_abort = threading.Event()
            self.live_abort_reason = 0
            self.live_exchange_id = exchange
            self.live_stream_session_epoch = self.cm5_session_epoch
            self.live_synth_thread = threading.Thread(
                target=self._live_synth_main,
                args=(controller, exchange, duration_ms,
                      self.live_stream_session_epoch, self.live_abort),
                daemon=True,
                name="fake-live-pcm",
            )
            self.live_synth_thread.start()
            return (f"OK: live-pcm-v1 synth started controller={controller:016x} "
                    f"exchange={exchange:016x} duration_ms={duration_ms}")
        m = re.fullmatch(
            r"liveaudio abort 1 ([0-9a-f]{16}) ([0-9a-f]{16})", line)
        if m:
            controller = int(m.group(1), 16)
            exchange = int(m.group(2), 16)
            if not self._live_session_may_control():
                return ("Error: liveaudio control requires a real "
                        "logged-in UART session")
            if (controller != self.live_controller_id or
                    exchange != self.live_exchange_id or
                    self.live_stream_session_epoch != self.cm5_session_epoch):
                return "Error: live stream mismatch"
            self.live_abort_reason = 5  # protocol: host request
            self.live_abort.set()
            return f"OK: live-pcm-v1 abort requested exchange={exchange:016x}"
        if line == "liveaudio status":
            controller = (f"{self.live_controller_id:016x}"
                          if self.live_controller_id is not None else "-")
            exchange = (f"{self.live_exchange_id:016x}"
                        if self.live_exchange_id is not None else "-")
            last_exchange = (f"{self.live_last_exchange:016x}"
                             if self.live_last_exchange is not None else "-")
            state = ("active" if self.live_exchange_id is not None
                     else self.live_last_terminal)
            # Fidelity: real reply says shadow=on|off (never 'armed') and
            # always carries shadow_mode=native|exact (LiveSttGate requires
            # shadow==on AND shadow_mode==native for a verified arm).
            return (f"OK: live-pcm-v1 state={state} "
                    f"active={1 if self.live_exchange_id is not None else 0} "
                    f"controller={controller} exchange={exchange} "
                    "bulk=0 "
                    f"shadow={'on' if self.live_shadow_armed else 'off'} "
                    f"shadow_mode="
                    f"{'native' if self.live_shadow_native else 'exact'} "
                    f"last={self.live_last_terminal} "
                    f"last_exchange={last_exchange} "
                    f"last_sent={self.live_last_sent} "
                    f"last_dropped={self.live_last_dropped} "
                    f"last_crc32={self.live_last_crc32:08x} "
                    f"last_terminal={1 if self.live_last_terminal_sent else 0}")
        m = re.fullmatch(
            r"cm5 power ack 1 ([0-9a-f]{16}) "
            r"(accepted|committed|applied|failed)", line)
        if m:
            self.cm5_power_acks.append((m.group(1), m.group(2)))
            return f"OK: host-power ACK id={m.group(1)} state={m.group(2)}"
        m = re.fullmatch(
            r"cm5 power report 1 (0|[0-9a-f]{16}) "
            r"(unknown|awake|sleeping|suspending|rebooting|halting|error) "
            r"(unknown|eco|balanced|performance|auto) ([0-9a-f]{32})", line)
        if m:
            self.cm5_power_reports.append(
                (m.group(1), m.group(2), m.group(3), m.group(4)))
            return (f"OK: CM5 report id={m.group(1)} state={m.group(2)} "
                    f"profile={m.group(3)} host_boot={m.group(4)}")
        m = re.fullmatch(
            r"cm5 fan ack 1 ([0-9a-f]{16}) "
            r"(accepted|applied|failed)", line)
        if m:
            self.cm5_fan_acks.append((m.group(1), m.group(2)))
            return f"OK: host-fan ACK id={m.group(1)} state={m.group(2)}"
        m = re.fullmatch(
            r"cm5 fan report 1 ([0-9a-f]{16}) "
            r"(auto|quiet|max) (auto|quiet|max) (-1|[0-9]{1,6}) "
            r"([0-9]{1,3}) ([0-9]{1,3}) (-1|[0-9]{1,6}) "
            r"(ok|boosting|tach_unavailable|safety_temp|safety_stall|"
            r"unavailable|io_error)", line)
        if m:
            self.cm5_fan_reports.append((
                m.group(1), m.group(2), m.group(3), int(m.group(4)),
                int(m.group(5)), int(m.group(6)), int(m.group(7)), m.group(8)))
            return (f"OK: CM5 fan report id={m.group(1)} "
                    f"mode={m.group(2)} effective={m.group(3)}")
        if line == "openmic":
            if self.mic_disabled:
                # Real firmware failure form: bare uppercase ERROR, unstamped
                # (System_Microphone.cpp cmd_micstart).
                return "ERROR: Microphone is disabled - run 'micenabled 1' first"
            if self.mic_open:
                # Real firmware: repeat openmic is a SUCCESS, not an error.
                return "OK: Microphone started successfully"
            self.mic_open = True
            return f"OK: Microphone enabled ({self.mic_source} 16000Hz)"
        if line == "micread json":
            return json.dumps({
                "schema": 1,
                "enabled": self.mic_open,
                "connected": self.mic_open,
                "recording": self.recording,
                "recordingState": "capturing" if self.recording else "idle",
                "source": self.mic_source,
                "pdmAvailable": True,
                "g2Available": True,
                "sampleRate": 16000,
                "bitDepth": 16,
                "channels": 1,
                "level": 0,
            }, separators=(",", ":"))
        if line == "miclist":
            # Real multi-line stamped blob: stampOkStatus prefixes the WHOLE
            # buffer, so line 1 is 'OK: ...' and the body follows.
            return "OK: Recordings (2):\nrec_1.wav:64044\nrec_2.wav:32044"
        if line == "micrecord":
            # Bare status poll (the VAD path). Count polls so a VAD recording
            # auto-stops mid-poll, exactly like the device's silence detector.
            if self.recording and self._vad_armed and \
                    self.vad_auto_stop_after is not None:
                self._vad_polls += 1
                if self._vad_polls >= self.vad_auto_stop_after:
                    self._finish_recording()
                    return "Recording: stopped"
            return ("Recording: active (1s, 16000 samples)"
                    if self.recording else "Recording: stopped")
        m = re.fullmatch(r"micrecord statusid ([0-9a-f]{16})", line)
        if m:
            eid = m.group(1)
            if self.recording and self._recording_owner != eid:
                return "Error: recorder owner mismatch"
            if self.recording and self._vad_armed and \
                    self.vad_auto_stop_after is not None:
                self._vad_polls += 1
                if self._vad_polls >= self.vad_auto_stop_after:
                    self._finish_recording()
            if self.recording:
                return "Recording: active (1s, 16000 samples)"
            if eid in self._owned_results:
                return self._owned_result_reply(eid)
            return "Error: recording ID not found"
        m = re.fullmatch(
            r"micrecord startid ([0-9a-f]{16})(?: vad(?: (\d+))?)?", line)
        if m:
            eid = m.group(1)
            if not self.mic_open:
                return "Error: Microphone not enabled. Use 'openmic' first."
            if self.recording or eid in self._owned_results:
                return "Error: Failed to start owned recording (busy or ID already consumed)"
            self.recording = True
            self._recording_owner = eid
            self._vad_armed = " vad" in line
            self._vad_polls = 0
            self._start_live_shadow(int(eid, 16))
            return (f"Recording {eid} started" +
                    (" (auto-stop on silence)" if self._vad_armed else ""))
        if line.startswith("micrecord start"):
            rest = line[len("micrecord start"):].strip()
            armed = rest.startswith("vad")
            if rest and not armed:
                return "Error: invalid arguments — Usage: micrecord <start|stop|1|0>"
            if armed and not self.support_vad:
                # Pre-VAD firmware: `start vad` is just bad arguments.
                return "Error: invalid arguments — Usage: micrecord <start|stop|1|0>"
            if not self.mic_open:
                return "Error: Microphone not enabled. Use 'openmic' first."
            self.recording = True
            self._recording_owner = None
            self._vad_armed = armed
            self._vad_polls = 0
            return ("Recording started (auto-stop on silence)"
                    if armed else "Recording started")
        if line == "micrecord stop":
            if not self.recording:
                # May have already auto-stopped: hand back the path exactly
                # like the firmware does from currentRecordingPath.
                if self._last_path:
                    return f"Recording stopped — {self._last_path}"
                return "Recording stopped"
            self._finish_recording(push_event=False)
            secs = len(self.wav_bytes) / 32000
            return f"Recording stopped — {self._last_path} ({secs:.1f}s)"
        m = re.fullmatch(
            r"micrecord stopid ([0-9a-f]{16})(?: (discard))?", line)
        if m:
            eid, discard = m.group(1), bool(m.group(2))
            if self.recording and self._recording_owner == eid:
                self._finish_recording(push_event=False)
            elif self.recording and eid not in self._owned_results:
                return "Error: recorder owner mismatch"
            if eid not in self._owned_results:
                return "Error: recording ID not found"
            if discard:
                self._discard_owned_result(eid)
            return self._owned_result_reply(eid)
        if line.startswith("voicefetch "):
            return self._voicefetch(line)
        if line.startswith("fileread "):
            return self._fileread(line)
        if line.startswith("micdelete "):
            m = re.match(r'micdelete "([^"]+)"', line)
            if m:
                self.deleted.append(m.group(1))
                self.files = {p: d for p, d in self.files.items()
                              if not p.endswith(m.group(1))}
                return "OK: Deleted"
            return "Error: filename must be a quoted token"
        m = re.fullmatch(r'micdeleteid ([0-9a-f]{16}) "([^"]+)"', line)
        if m:
            eid, filename = m.group(1), m.group(2)
            if eid not in self._owned_results:
                return ("Error: recorder owner mismatch" if self.recording
                        else "Error: recording ID not found")
            path = self._owned_results[eid]
            expected = (os.path.basename(path) if path else
                        f"rec_{eid}.wav")
            if filename != expected:
                return "Error: filename does not belong to recording ID"
            self.deleted.append(filename)
            self._discard_owned_result(eid)
            return "OK: Deleted"
        if line.startswith("oledtext"):
            if not self.oled_running:
                # Real wire behavior: the descriptive text goes to the
                # broadcast/audit sink (never this channel); the reply is a
                # bare "ERROR" (OLED_Utils.cpp cmd_oledtext).
                return "ERROR"
            self.oled_texts.append(line[len("oledtext"):].strip())
            return "OK: text displayed"
        if line == "oledstart":
            self.oled_running = True
            return "OK"
        if line.startswith("g2notify "):
            self.g2_texts.append(line.split(" ", 2)[-1])
            return "OK: notified"
        m = re.fullmatch(r"g2aiconfig - (\d+) -", line)
        if m:
            self.evenai_stream_speeds.append(int(m.group(1)))
            return (
                "G2: aiconfig sent — magic=201 voiceSwitch=(omit) "
                f"streamSpeed={m.group(1)} duplexMode=(omit) — "
                "watch logs for CONFIG echo or COMM_RSP errorCode"
            )
        m = re.fullmatch(r"g2evenai replypartid ([0-9a-f]{16}) (.*)", line)
        if m:
            if not self.evenai_active or m.group(1) != self.evenai_exchange_id:
                return "Error: EvenAI session mismatch or terminal"
            first = not self.evenai_streaming
            self.evenai_streaming = True
            self.evenai_reply_parts.append(m.group(2))
            return ("G2: EvenAI REPLY stream opened (part sent)" if first
                    else "G2: EvenAI REPLY part sent")
        m = re.fullmatch(r"g2evenai replyendid ([0-9a-f]{16})", line)
        if m:
            if not self.evenai_active or m.group(1) != self.evenai_exchange_id:
                return "Error: EvenAI session mismatch or terminal"
            self.evenai_streaming = False
            self.evenai_reply_ended = True
            return "G2: EvenAI REPLY stream finalized"
        m = re.fullmatch(r"g2evenai askid ([0-9a-f]{16}) (.*)", line)
        if m:
            if not self.evenai_active or m.group(1) != self.evenai_exchange_id:
                return "Error: EvenAI session mismatch or terminal"
            self.evenai_asks.append(m.group(2))
            return "G2: EvenAI ASK sent (prompt -> listening popup)"
        m = re.fullmatch(r"g2evenai replyid ([0-9a-f]{16}) (.*)", line)
        if m:
            if not self.evenai_active or m.group(1) != self.evenai_exchange_id:
                return "Error: EvenAI session mismatch or terminal"
            self.evenai_replies.append(m.group(2))
            self.evenai_streaming = False
            return "G2: EvenAI REPLY sent (answer -> response window)"
        m = re.fullmatch(r"g2evenai exitid ([0-9a-f]{16})", line)
        if m:
            if not self.evenai_active or m.group(1) != self.evenai_exchange_id:
                return "Error: EvenAI session mismatch or terminal"
            eid = m.group(1)
            self.evenai_active = False
            self.evenai_streaming = False
            if self.recording and self._recording_owner == eid:
                self._finish_recording(push_event=False)
                self._discard_owned_result(eid)
            # Production termination publishes the exact cancellation before
            # the command reply and retries it as an advisory tombstone.
            self.push_event(f"evenai_cancel {eid} host_exit")
            return "G2: EvenAI EXIT sent; exchange terminal"
        if line == "g2evenai" or line.startswith("g2evenai "):
            # Mirrors the production fail-closed legacy grammar. Historic log
            # analysis remains legacy-tolerant elsewhere, but the integration
            # double must never let an untagged host regression mutate state.
            rest = line[len("g2evenai"):].strip()
            verb = rest.split(None, 1)[0].lower() if rest else ""
            if verb in {"ask", "reply", "replypart", "replyend", "exit"}:
                # Current firmware converts any legacy live mutation into a
                # fail-closed terminal action before returning the grammar
                # error. It never applies the untagged text to the card.
                return self._reject_legacy_evenai()
            if rest == "capabilities":
                return ("OK: EvenAI exchange-id-v1 "
                        "verbs=askid,replyid,replypartid,replyendid,exitid "
                        "legacy=fail-closed")
            state = "active" if self.evenai_active else "idle"
            eid = self.evenai_exchange_id if self.evenai_active else "-"
            arm = "R" if self.evenai_active else "-"
            generation = 1 if self.evenai_active else 0
            return (f"EvenAI session: {state} id={eid} arm={arm} "
                    f"gen={generation} "
                    f"uart_epoch={self.evenai_uart_epoch if self.evenai_active else 0} "
                    "(hb=3, idle=100ms)")
        # Real unknown-command reply: TWO unprefixed lines in one blob
        # (System_Utils.cpp executeCommand).
        return f"Unknown command: {line.split(' ')[0]}\nType 'help' for available commands"

    def _owned_result_reply(self, eid: str) -> str:
        path = self._owned_results[eid]
        if path is None:
            return f"Recording {eid} discarded"
        return f"Recording {eid} stopped — {path}"

    def _discard_owned_result(self, eid: str | None) -> bool:
        if eid is None or eid not in self._owned_results:
            return False
        path = self._owned_results[eid]
        if path is not None:
            self.files.pop(path, None)
            if self._last_path == path:
                self._last_path = None
        self._owned_results[eid] = None
        return True

    def _finish_recording(self, *, push_event: bool = True) -> None:
        """End the current recording and stash its WAV (shared by explicit stop
        and VAD auto-stop). Deterministic path avoids same-second collisions."""
        owner = self._recording_owner
        self.recording = False
        self._vad_armed = False
        self._rec_seq += 1
        path = (f"/recordings/rec_{owner}.wav" if owner is not None
                else f"/recordings/rec_{self._rec_seq}.wav")
        self.files[path] = self.wav_bytes
        self._last_path = path
        native_live = bool(
            owner is not None and self.live_shadow_native and
            self.live_exchange_id == int(owner, 16))
        event_pushed = False
        if owner is not None:
            self._owned_results[owner] = path
            if self.live_exchange_id == int(owner, 16):
                if (push_event and self.push_mic_autostop and native_live and
                        self.native_autostop_before_end):
                    self.native_emit_order.append("mic_autostop")
                    self.push_event(f"mic_autostop {owner} {path}")
                    event_pushed = True
                self.live_shadow_capture_done.set()
                if (push_event and self.push_mic_autostop and native_live and
                        not self.native_autostop_before_end):
                    self.live_shadow_terminal_sent.wait(timeout=1.0)
                    self.native_emit_order.append("mic_autostop")
                    self.push_event(f"mic_autostop {owner} {path}")
                    event_pushed = True
        self._recording_owner = None
        if (push_event and self.push_mic_autostop and owner is not None and
                not event_pushed):
            self.push_event(
                f"mic_autostop {owner} {path}")

    def _frame_wire(self, ftype: int, seq: int, payload: bytes) -> bytes:
        """One framed wire blob (shared by voicefetch and EVT pushes). Uses
        the client's protocol module for the CRC so tests exercise the real
        wire format, not a second implementation."""
        from hw1_ai_service.link import protocol as P
        body = bytes([ftype, seq & 0xFF, seq >> 8,
                      len(payload) & 0xFF, len(payload) >> 8]) + payload
        crc = P.crc16_ccitt(body)
        body += bytes([crc & 0xFF, crc >> 8])
        return bytes([0x00]) + _cobs_encode(body) + bytes([0x00])

    def _write_live_frame_for_session(self, session_epoch: int, ftype: int,
                                      seq: int, payload: bytes) -> bool:
        """Fence each unsolicited live frame to its exact named login."""
        with self._session_fence:
            if not self._live_session_epoch_is_current(session_epoch):
                return False
            return self._write_raw(self._frame_wire(ftype, seq, payload))

    @staticmethod
    def _valid_live_id(value: int) -> bool:
        return ((value >> 32) & 0xFFFFFFFF) != 0 and \
            (value & 0xFFFFFFFF) != 0

    @staticmethod
    def _live_synth_pcm(exchange: int, start: int, count: int) -> bytes:
        low = exchange & 0xFFFF
        out = bytearray(count * 2)
        for relative in range(count):
            index = start + relative
            value = ((index * 257) ^ low) & 0xFFFF
            out[relative * 2] = value & 0xFF
            out[relative * 2 + 1] = value >> 8
        return bytes(out)

    @staticmethod
    def _wav_pcm(data: bytes) -> bytes:
        if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
            raise ValueError("fake recorder WAV is malformed")
        pos = 12
        while pos + 8 <= len(data):
            chunk_id = data[pos:pos + 4]
            size = struct.unpack_from("<I", data, pos + 4)[0]
            body = data[pos + 8:pos + 8 + size]
            if len(body) != size:
                raise ValueError("fake recorder WAV chunk is truncated")
            if chunk_id == b"data":
                return bytes(body)
            pos += 8 + size + (size & 1)
        raise ValueError("fake recorder WAV has no data chunk")

    def _start_live_shadow(self, exchange: int) -> None:
        """Attach an exact or native shadow arm to this owned capture."""
        if not self.live_shadow_armed:
            return
        if (not self.live_shadow_native and
                self.live_shadow_expected_exchange != exchange):
            return
        if (self.live_controller_id is None or
                not self._live_lease_matches(self.live_controller_id)):
            return
        if self.live_exchange_id is not None:
            return
        self.live_abort = threading.Event()
        self.live_abort_reason = 0
        self.live_shadow_capture_done = threading.Event()
        self.live_shadow_begin_sent = threading.Event()
        self.live_shadow_terminal_sent = threading.Event()
        self.live_exchange_id = exchange
        self.live_stream_session_epoch = self.cm5_session_epoch
        controller = self.live_controller_id
        pcm = self._wav_pcm(self.wav_bytes)
        if self.live_shadow_native:
            # Native capture has trim enabled. The live tee sees pre-trim lead
            # and tail that are intentionally absent from the canonical WAV.
            padding = self.native_trim_padding_samples
            pcm = (struct.pack("<h", 111) * padding + pcm +
                   struct.pack("<h", -222) * padding)
        source = 2 if self.mic_source == "g2" else 1
        self.live_shadow_thread = threading.Thread(
            target=self._live_shadow_main,
            args=(controller, exchange, source, pcm,
                  self.live_stream_session_epoch, self.live_abort,
                  self.live_shadow_capture_done),
            daemon=True,
            name="fake-live-recorder-shadow",
        )
        self.live_shadow_thread.start()

    def _live_shadow_main(self, controller: int, exchange: int, source: int,
                          pcm_all: bytes, session_epoch: int,
                          abort_evt: threading.Event,
                          capture_done: threading.Event) -> None:
        """Async real-source producer; END waits for exact recorder stop."""
        from hw1_ai_service.link import protocol as P

        begin = struct.pack(
            "<BBBBIQQHH", 1, 0, source, 1, 16_000,
            exchange, controller, 2048, 0)
        seq = 0
        begin_sent = self._write_live_frame_for_session(
            session_epoch, P.FRAME_LIVE_BEGIN, seq, begin)
        if self.live_shadow_native and begin_sent:
            self.native_emit_order.append("live_begin")
        if begin_sent:
            self.live_shadow_begin_sent.set()
        total_samples = len(pcm_all) // 2
        sent = 0
        crc32 = 0
        abort_reason = 0
        frame_index = 0

        while sent < total_samples and not self._closing:
            with self._lock:
                requested_reason = self.live_abort_reason
                session_current = self._live_session_epoch_is_current(
                    session_epoch)
                lease_valid = (
                    session_current and
                    self.live_stream_session_epoch == session_epoch and
                    self.live_controller_id == controller and
                    self.live_lease_session_epoch == session_epoch and
                    time.monotonic() < self.live_lease_expires_at)
            if abort_evt.is_set():
                abort_reason = requested_reason or 5
                break
            if not session_current:
                abort_reason = 2  # protocol: auth/session lost
                break
            if not lease_valid:
                abort_reason = 1  # protocol: lease expired
                break
            count = min(500, total_samples - sent)
            raw = pcm_all[sent * 2:(sent + count) * 2]
            if self.live_shadow_pcm_xor and sent == 0 and raw:
                raw = bytes([raw[0] ^ self.live_shadow_pcm_xor]) + raw[1:]
            payload = struct.pack(
                "<BBQQIH", 1, 0, exchange, controller, sent, count) + raw
            seq = (seq + 1) & 0xFFFF
            if frame_index != self.live_shadow_drop_frame_index:
                if not self._write_live_frame_for_session(
                        session_epoch, P.FRAME_LIVE_PCM, seq, payload):
                    abort_reason = 2
                    break
            crc32 = binascii.crc32(raw, crc32) & 0xFFFFFFFF
            sent += count
            frame_index += 1
            if self.live_shadow_frame_delay_s:
                time.sleep(self.live_shadow_frame_delay_s)

        while not abort_reason and not capture_done.wait(0.01):
            if self._closing:
                abort_reason = 3
                break
            with self._lock:
                requested_reason = self.live_abort_reason
                session_current = self._live_session_epoch_is_current(
                    session_epoch)
                lease_valid = (
                    session_current and
                    self.live_stream_session_epoch == session_epoch and
                    self.live_controller_id == controller and
                    self.live_lease_session_epoch == session_epoch and
                    time.monotonic() < self.live_lease_expires_at)
            if abort_evt.is_set():
                abort_reason = requested_reason or 5
            elif not session_current:
                abort_reason = 2
            elif not lease_valid:
                abort_reason = 1

        seq = (seq + 1) & 0xFFFF
        terminal_crc = crc32 ^ self.live_shadow_terminal_crc_xor
        if abort_reason or sent != total_samples:
            terminal = struct.pack(
                "<BBQQIII", 1, abort_reason or 7, exchange, controller,
                sent, terminal_crc, max(0, total_samples - sent))
            terminal_sent = self._write_live_frame_for_session(
                session_epoch, P.FRAME_LIVE_ABORT, seq, terminal)
            terminal_name = "abort"
        else:
            terminal = struct.pack(
                "<BBQQIII", 1, 0, exchange, controller,
                sent, terminal_crc, 0)
            terminal_sent = self._write_live_frame_for_session(
                session_epoch, P.FRAME_LIVE_END, seq, terminal)
            if self.live_shadow_native and terminal_sent:
                self.native_emit_order.append("live_terminal")
            terminal_name = "end"
        if terminal_sent:
            self.live_shadow_terminal_sent.set()
        with self._lock:
            if (self.live_exchange_id == exchange and
                    self.live_stream_session_epoch == session_epoch):
                if not self.live_shadow_suppress_last_update:
                    self.live_last_terminal = terminal_name
                    self.live_last_exchange = exchange
                    self.live_last_sent = sent
                    self.live_last_dropped = max(0, total_samples - sent)
                    self.live_last_crc32 = terminal_crc
                    self.live_last_terminal_sent = terminal_sent
                self.live_exchange_id = None
                self.live_stream_session_epoch = 0

    def _live_synth_main(self, controller: int, exchange: int,
                         duration_ms: int, session_epoch: int,
                         abort_evt: threading.Event) -> None:
        """Paced async live-pcm-v1 producer used by host integration tests."""
        from hw1_ai_service.link import protocol as P

        total_samples = (16_000 * duration_ms) // 1000
        begin = struct.pack(
            "<BBBBIQQHH",
            1,                 # protocol version
            1,                 # bit 0: synthetic
            0,                 # synthetic source
            1,                 # S16LE mono
            16_000,
            exchange,
            controller,
            2048,              # recorder's logical chunk size
            0,
        )
        seq = 0
        self._write_live_frame_for_session(
            session_epoch, P.FRAME_LIVE_BEGIN, seq, begin)
        started = time.monotonic()
        sent = 0
        crc32 = 0
        abort_reason = 0

        while sent < total_samples and not self._closing:
            if abort_evt.is_set():
                with self._lock:
                    abort_reason = self.live_abort_reason or 5
                break
            with self._lock:
                session_current = self._live_session_epoch_is_current(
                    session_epoch)
                lease_valid = (
                    session_current and
                    self.live_stream_session_epoch == session_epoch and
                    self.live_controller_id == controller and
                    self.live_lease_session_epoch == session_epoch and
                    time.monotonic() < self.live_lease_expires_at)
            if not session_current:
                abort_reason = 2       # protocol: auth/session lost
                break
            if not lease_valid:
                abort_reason = 1       # protocol: lease expired
                break

            count = min(500, total_samples - sent)
            pcm = self._live_synth_pcm(exchange, sent, count)
            payload = struct.pack(
                "<BBQQIH", 1, 1, exchange, controller, sent, count) + pcm
            # Pace from the END of this physical chunk, matching capture.
            target = started + (sent + count) / 16_000.0
            delay = target - time.monotonic()
            if delay > 0 and abort_evt.wait(delay):
                with self._lock:
                    abort_reason = self.live_abort_reason or 5
                break
            seq = (seq + 1) & 0xFFFF
            if not self._write_live_frame_for_session(
                    session_epoch, P.FRAME_LIVE_PCM, seq, payload):
                abort_reason = 2
                break
            crc32 = binascii.crc32(pcm, crc32) & 0xFFFFFFFF
            sent += count

        seq = (seq + 1) & 0xFFFF
        if abort_reason or sent != total_samples:
            terminal = struct.pack(
                "<BBQQIII", 1, abort_reason or 3, exchange, controller,
                sent, crc32, max(0, total_samples - sent))
            terminal_sent = self._write_live_frame_for_session(
                session_epoch, P.FRAME_LIVE_ABORT, seq, terminal)
            terminal_name = "abort"
        else:
            terminal = struct.pack(
                "<BBQQIII", 1, 0, exchange, controller,
                sent, crc32, 0)
            terminal_sent = self._write_live_frame_for_session(
                session_epoch, P.FRAME_LIVE_END, seq, terminal)
            terminal_name = "end"
        with self._lock:
            if (self.live_exchange_id == exchange and
                    self.live_stream_session_epoch == session_epoch):
                self.live_last_terminal = terminal_name
                self.live_last_exchange = exchange
                self.live_last_sent = sent
                self.live_last_dropped = max(0, total_samples - sent)
                self.live_last_crc32 = crc32
                self.live_last_terminal_sent = terminal_sent
                self.live_exchange_id = None
                self.live_stream_session_epoch = 0

    def _voicefetch(self, line: str) -> str:
        """Mirror cmd_voicefetch: stream META + AUDIO frames, then reply."""
        import re as _re
        from hw1_ai_service.link import protocol as P

        if self.support_voicefetch is False:
            return "Error: unknown command"   # simulate pre-P2 firmware
        # Production rejects file transfer while unsolicited live PCM owns
        # the framed lane.  Tests therefore catch probes that fetch the WAV
        # before draining the exact live terminal.
        if self.live_exchange_id is not None:
            return "Error: live PCM stream active"
        m = _re.match(r'voicefetch "([^"]+)"', line)
        if not m:
            return "Error: usage: voicefetch \"<path>\""
        data = self.files.get(m.group(1))
        if data is None:
            return "Error: not found or access denied"

        # META
        self._write_raw(self._frame_wire(P.FRAME_META, 0,
                                         len(data).to_bytes(4, "little")))
        seq = 1
        for off in range(0, len(data), 1024):
            self._write_raw(self._frame_wire(P.FRAME_AUDIO, seq,
                                             data[off:off + 1024]))
            injection = self.voicefetch_event_after_frames
            if injection is not None and seq == injection[0]:
                # Same firmware thread/write order as the real device. A test
                # thread writing a PTY concurrently can interleave bytes and
                # test frame corruption rather than event routing.
                self.push_event(injection[1])
                self.voicefetch_event_after_frames = None
            if self.voicefetch_frame_delay_s:
                time.sleep(self.voicefetch_frame_delay_s)
            seq += 1
        crc = P.crc16_ccitt(data)
        return f"OK: voicefetch {len(data)} bytes in {seq} frames crc16={crc:04X}"

    def _fileread(self, line: str) -> str:
        m = re.match(r'fileread "([^"]+)"(?:\s+(\d+))?(?:\s+(\d+))?(?:\s+b64)?$', line)
        if not m:
            return '{"success":false,"error":"path must be a quoted token"}'
        path, off_s, len_s = m.group(1), m.group(2), m.group(3)
        data = self.files.get(path)
        if data is None:
            return '{"success":false,"error":"Not found or access denied"}'
        offset = int(off_s or 0)
        want = int(len_s or 0) or 4096
        # Firmware rawCap: reply must fit the 4096B result buffer.
        reserve = 192 + len(path)
        raw_cap = ((4096 - reserve) // 4) * 3
        want = min(want, raw_cap, 4096)
        offset = min(offset, len(data))
        chunk = data[offset:offset + want]
        eof = offset + len(chunk) >= len(data)
        return json.dumps({
            "success": True, "size": len(data), "offset": offset,
            "len": len(chunk), "eof": eof, "enc": "b64",
            "data": base64.b64encode(chunk).decode("ascii"),
        }, separators=(",", ":"))
