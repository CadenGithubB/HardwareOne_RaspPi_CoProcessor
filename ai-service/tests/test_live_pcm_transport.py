"""Phase-2A synthetic live PCM wire, direct routing, and bounded inbox."""

from __future__ import annotations

import asyncio
import importlib.util
import struct
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial

from conftest import open_link, run
from fake_firmware import _cobs_encode

from hw1_ai_service.audio.live import (
    DEFAULT_INTERFRAME_TIMEOUT_S,
    LivePcmChunk,
    LivePcmInbox,
    LiveStreamTerminal,
    synthetic_pcm,
)
from hw1_ai_service.link import protocol
from hw1_ai_service.link.transport import SerialTransport


_PROBE_PATH = Path(__file__).parents[1] / "tools" / "live_pcm_transport_probe.py"
_PROBE_SPEC = importlib.util.spec_from_file_location(
    "live_pcm_transport_probe", _PROBE_PATH)
assert _PROBE_SPEC is not None and _PROBE_SPEC.loader is not None
probe = importlib.util.module_from_spec(_PROBE_SPEC)
_PROBE_SPEC.loader.exec_module(probe)


CONTROLLER = 0xC0DEC0DE00000001
EXCHANGE = 0xA1B2C3D400000001
LATE_EXCHANGE = 0xA1B2C3D400000002


def _begin(*, exchange=EXCHANGE, controller=CONTROLLER, flags=1,
           source=0, rate=16_000) -> bytes:
    return struct.pack(
        "<BBBBIQQHH", 1, flags, source, 1, rate,
        exchange, controller, 2048, 0)


def _pcm(offset: int, count: int, *, exchange=EXCHANGE,
         controller=CONTROLLER, flags=1) -> tuple[bytes, bytes]:
    full = synthetic_pcm(exchange, offset + count)
    raw = full[offset * 2:(offset + count) * 2]
    return (struct.pack(
        "<BBQQIH", 1, flags, exchange, controller, offset, count) + raw, raw)


def _terminal(total: int, crc32: int, *, exchange=EXCHANGE,
              controller=CONTROLLER, reason=0, dropped=0) -> bytes:
    return struct.pack(
        "<BBQQIII", 1, reason, exchange, controller,
        total, crc32, dropped)


def _outer_body(ftype: int, seq: int, payload: bytes) -> bytes:
    body = bytes([
        ftype, seq & 0xFF, (seq >> 8) & 0xFF,
        len(payload) & 0xFF, (len(payload) >> 8) & 0xFF,
    ]) + payload
    crc = protocol.crc16_ccitt(body)
    return body + struct.pack("<H", crc)


def test_live_wire_contract_and_crc_goldens():
    assert protocol.LIVE_BEGIN_STRUCT.size == 28
    assert protocol.LIVE_PCM_HEADER_STRUCT.size == 24
    assert protocol.LIVE_TERMINAL_STRUCT.size == 30
    assert protocol.LIVE_PCM_HEADER_STRUCT.size + protocol.LIVE_PCM_MAX_BYTES == 1024
    assert protocol.crc32_ieee(b"123456789") == 0xCBF43926

    begin = protocol.parse_live_begin(_begin())
    assert begin.exchange_id == EXCHANGE
    assert begin.controller_id == CONTROLLER
    assert begin.synthetic and begin.sample_rate == 16_000

    payload, raw = _pcm(0, 500)
    pcm = protocol.parse_live_pcm(payload)
    assert pcm.sample_count == 500 and pcm.pcm == raw


def test_default_interframe_timeout_outlasts_firmware_stall_boundary():
    assert DEFAULT_INTERFRAME_TIMEOUT_S > 2.0


def test_live_capabilities_are_selected_by_feature_not_whole_reply():
    older = protocol.parse_live_capabilities(
        "OK: live-pcm-v1 synthetic=1 recorder_shadow=0 protocol=1")
    newer = protocol.parse_live_capabilities(
        "OK: live-pcm-v1 synthetic=1 recorder_shadow=1 "
        "shadow_default=off protocol=1 renew_direct=1 "
        "future_key=future-value")
    assert older["synthetic"] == "1"
    assert older["recorder_shadow"] == "0"
    assert newer["synthetic"] == "1"
    assert newer["recorder_shadow"] == "1"
    assert newer["shadow_default"] == "off"
    assert newer["renew_direct"] == "1"
    assert newer["future_key"] == "future-value"


@pytest.mark.parametrize("reply,match", [
    ("OK: synthetic=1", "missing live-pcm-v1"),
    ("OK: live-pcm-v1 synthetic", "malformed"),
    ("OK: live-pcm-v1 synthetic=1 synthetic=0", "duplicate"),
    ("OK: live-pcm-v1 Recorder_shadow=1", "malformed"),
])
def test_live_capabilities_reject_ambiguous_or_malformed_tokens(reply, match):
    with pytest.raises(ValueError, match=match):
        protocol.parse_live_capabilities(reply)


def test_live_ready_parser_distinguishes_direct_from_legacy():
    legacy = protocol.parse_live_ready(
        f"OK: liveaudio ready version=1 controller={CONTROLLER:016x} "
        "session_epoch=7 lease_ttl_ms=3000 renew_ms=1000 baud=2000000",
        expected_controller=CONTROLLER)
    direct = protocol.parse_live_ready(
        f"OK: liveaudio ready version=1 controller={CONTROLLER:016x} "
        "session_epoch=7 renew_direct=1 lease_ttl_ms=3000 "
        "renew_ms=1000 baud=2000000",
        expected_controller=CONTROLLER)
    assert not legacy.renew_direct
    assert direct.renew_direct
    assert direct.lease_ttl_ms == 3000
    assert direct.renew_ms == 1000


@pytest.mark.parametrize("reply,match", [
    (f"OK: liveaudio ready version=1 controller={CONTROLLER:016x} "
     "session_epoch=7 renew_direct=1 lease_ttl_ms=3000 renew_ms=1000 "
     "renew_ms=1000", "duplicate"),
    (f"OK: liveaudio ready version=1 controller={CONTROLLER:016x} "
     "session_epoch=7 renew_direct=1 lease_ttl_ms=3000", "missing renew_ms"),
    (f"OK: liveaudio ready version=1 controller={CONTROLLER:016x} "
     "session_epoch=0", "must be positive"),
])
def test_live_ready_parser_rejects_ambiguous_contract(reply, match):
    with pytest.raises(ValueError, match=match):
        protocol.parse_live_ready(reply, expected_controller=CONTROLLER)


@pytest.mark.parametrize("payload,match", [
    (_begin(rate=48_000), "requires 16000"),
    (_begin(flags=0, source=0), "flag/source mismatch"),
    (_begin(exchange=1), "nonzero high and low"),
])
def test_live_begin_rejects_noncanonical_v1(payload, match):
    with pytest.raises(ValueError, match=match):
        protocol.parse_live_begin(payload)


def test_valid_end_survives_concurrent_pcm_drain():
    inbox = LivePcmInbox(CONTROLLER)
    assert inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    consumed = bytearray()
    terminal: list[LiveStreamTerminal] = []

    def consumer():
        while True:
            item = stream.next_item(timeout=2.0)
            if isinstance(item, LiveStreamTerminal):
                terminal.append(item)
                return
            assert isinstance(item, LivePcmChunk)
            consumed.extend(item.pcm)

    worker = threading.Thread(target=consumer)
    worker.start()
    crc32 = 0
    offset = 0
    for seq, count in enumerate((500, 500, 237), start=1):
        payload, raw = _pcm(offset, count)
        crc32 = protocol.crc32_ieee(raw, crc32)
        assert inbox.offer_frame(protocol.FRAME_LIVE_PCM, seq, payload)
        offset += count
    assert inbox.offer_frame(
        protocol.FRAME_LIVE_END, 4, _terminal(offset, crc32))
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert bytes(consumed) == synthetic_pcm(EXCHANGE, offset)
    assert len(terminal) == 1 and terminal[0].valid
    assert stream.snapshot()["queue_high_water_bytes"] <= 16 * 1024


@pytest.mark.parametrize("ftype,reason,expected", [
    (protocol.FRAME_LIVE_END, 4, "end_reason:4"),
    (protocol.FRAME_LIVE_ABORT, 0, "abort_reason:0"),
    (protocol.FRAME_LIVE_ABORT, 255, "abort_reason:255"),
])
def test_terminal_reason_vocabulary_is_strict(ftype, reason, expected):
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    inbox.offer_frame(
        ftype, 1, _terminal(0, 0, reason=reason))
    terminal = stream.wait_terminal(0.1)
    assert not terminal.valid
    assert terminal.reason == expected


@pytest.mark.parametrize("total,crc32,expected", [
    (2, 0, "abort_total:2!=1"),
    (1, 0, "abort_crc32:00000000!="),
])
def test_abort_validates_the_received_pcm_prefix(total, crc32, expected):
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, raw = _pcm(0, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, payload)
    inbox.offer_frame(
        protocol.FRAME_LIVE_ABORT, 2,
        _terminal(total, crc32, reason=protocol.LIVE_ABORT_REASON_HOST_REQUEST))
    terminal = stream.wait_terminal(0.1)
    assert not terminal.valid
    assert str(terminal.reason).startswith(expected)
    assert protocol.crc32_ieee(raw) != 0


def test_valid_abort_preserves_device_reason_after_prefix_validation():
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, raw = _pcm(0, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, payload)
    inbox.offer_frame(
        protocol.FRAME_LIVE_ABORT, 2,
        _terminal(
            1, protocol.crc32_ieee(raw),
            reason=protocol.LIVE_ABORT_REASON_HOST_REQUEST,
            dropped=10))
    terminal = stream.wait_terminal(0.1)
    assert terminal.kind == "abort" and not terminal.valid
    assert terminal.reason == protocol.LIVE_ABORT_REASON_HOST_REQUEST
    # One received-but-not-consumed sample is included in the diagnostic
    # dropped count after the device's ten unsent samples.
    assert terminal.dropped_samples == 11


def test_pcm_queue_overflow_is_terminal_and_bounded():
    inbox = LivePcmInbox(
        CONTROLLER, max_queue_bytes=1000, max_queue_frames=1)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    first, _ = _pcm(0, 500)
    second, _ = _pcm(500, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, first)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 2, second)
    item = stream.next_item(timeout=0.1)
    assert isinstance(item, LiveStreamTerminal)
    assert not item.valid and item.reason == "pcm_queue_overflow"
    assert item.dropped_samples == 501
    snap = stream.snapshot()
    assert snap["queued_bytes"] == 0 and snap["queued_frames"] == 0


def test_offset_gap_invalidates_and_late_old_id_cannot_touch_next_stream():
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    old = inbox.next_stream(timeout=0.1)
    gap, _ = _pcm(10, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, gap)
    assert old.wait_terminal(0.1).reason == "sample_offset:10!=0"

    inbox.offer_frame(
        protocol.FRAME_LIVE_BEGIN, 0, _begin(exchange=LATE_EXCHANGE))
    new = inbox.next_stream(timeout=0.1)
    late, _ = _pcm(0, 1, exchange=EXCHANGE)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 2, late)
    assert not new.complete
    current, _ = _pcm(0, 1, exchange=LATE_EXCHANGE)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, current)
    crc = protocol.crc32_ieee(protocol.parse_live_pcm(current).pcm)
    inbox.offer_frame(
        protocol.FRAME_LIVE_END, 2,
        _terminal(1, crc, exchange=LATE_EXCHANGE))
    assert new.wait_terminal(0.1).valid
    assert inbox.snapshot()["late_frame_count"] >= 1


def test_outer_wire_sequence_gap_invalidates_even_when_pcm_offset_is_contiguous():
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 9, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, _ = _pcm(0, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 11, payload)
    terminal = stream.wait_terminal(0.1)
    assert not terminal.valid
    assert terminal.reason == "wire_seq:11!=10"


def test_outer_wire_sequence_wrap_is_contiguous():
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0xFFFF, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, raw = _pcm(0, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 0, payload)
    inbox.offer_frame(
        protocol.FRAME_LIVE_END, 1,
        _terminal(1, protocol.crc32_ieee(raw)))
    assert stream.wait_terminal(0.1).valid


def test_crc_corrupt_end_is_outer_dropped_then_interframe_times_out():
    inbox = LivePcmInbox(
        CONTROLLER, first_pcm_timeout_s=0.05,
        interframe_timeout_s=0.03, absolute_timeout_s=1.0)
    transport = SerialTransport(frame_sink=inbox)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, raw = _pcm(0, 1)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, payload)

    body = bytearray(_outer_body(
        protocol.FRAME_LIVE_END, 2,
        _terminal(1, protocol.crc32_ieee(raw))))
    body[-1] ^= 0x80
    transport._finish_frame(_cobs_encode(bytes(body)))
    terminal = stream.wait_terminal(timeout=0.5)
    assert terminal.kind == "invalid"
    assert terminal.reason == "interframe_timeout"
    assert transport.garbage_count == 1


def test_link_close_is_durable_terminal_outside_pcm_queue():
    inbox = LivePcmInbox(CONTROLLER)
    inbox.offer_frame(protocol.FRAME_LIVE_BEGIN, 0, _begin())
    stream = inbox.next_stream(timeout=0.1)
    payload, _ = _pcm(0, 500)
    inbox.offer_frame(protocol.FRAME_LIVE_PCM, 1, payload)
    inbox.link_closed()
    terminal = stream.next_item(timeout=0.1)
    assert isinstance(terminal, LiveStreamTerminal)
    assert terminal.reason == "link_closed"


def test_fake_firmware_synthetic_stream_interleaves_with_command_reply(firmware):
    async def main():
        inbox = LivePcmInbox(CONTROLLER)
        transport, session = open_link(firmware, frame_sink=inbox)
        try:
            await session.login()
            cap = await session.command(
                "liveaudio capabilities", expect="status", timeout=5)
            assert cap.ok and "live-pcm-v1" in cap.text
            ready = await session.command(
                f"liveaudio ready 1 {CONTROLLER:016x}",
                expect="status", timeout=5)
            assert ready.ok
            started = await session.command(
                f"liveaudio synth 1 {CONTROLLER:016x} {EXCHANGE:016x} 128",
                expect="status", timeout=5, replay=False)
            assert started.ok
            # A normal text command shares the wire while unsolicited PCM is
            # routed directly; neither side may steal the other's boundary.
            status = await session.command(
                "uartlink status", expect="status", timeout=5)
            assert status.ok
            stream = await asyncio.to_thread(inbox.next_stream, 2.0)
            out = bytearray()
            while True:
                item = await asyncio.to_thread(stream.next_item, 2.0)
                if isinstance(item, LiveStreamTerminal):
                    assert item.valid, item
                    break
                out.extend(item.pcm)
            assert bytes(out) == synthetic_pcm(EXCHANGE, 2048)
            released = await session.command(
                f"liveaudio release 1 {CONTROLLER:016x}",
                expect="status", timeout=5, replay=False)
            assert released.ok
        finally:
            transport.close()
    run(main())


@pytest.mark.parametrize("control,expected_reason", [
    ("abort", protocol.LIVE_ABORT_REASON_HOST_REQUEST),
    ("release", protocol.LIVE_ABORT_REASON_RELEASED),
])
def test_fake_firmware_preserves_control_abort_reason(
        firmware, control, expected_reason):
    async def main():
        inbox = LivePcmInbox(CONTROLLER)
        transport, session = open_link(firmware, frame_sink=inbox)
        try:
            await session.login()
            ready = await session.command(
                f"liveaudio ready 1 {CONTROLLER:016x}",
                expect="status", timeout=5)
            assert ready.ok
            started = await session.command(
                f"liveaudio synth 1 {CONTROLLER:016x} {EXCHANGE:016x} 1000",
                expect="status", timeout=5, replay=False)
            assert started.ok
            stream = await asyncio.to_thread(inbox.next_stream, 2.0)
            command = (
                f"liveaudio abort 1 {CONTROLLER:016x} {EXCHANGE:016x}"
                if control == "abort" else
                f"liveaudio release 1 {CONTROLLER:016x}")
            stopped = await session.command(
                command, expect="status", timeout=5, replay=False)
            assert stopped.ok
            while True:
                item = await asyncio.to_thread(stream.next_item, 2.0)
                if isinstance(item, LiveStreamTerminal):
                    assert item.kind == "abort"
                    assert item.reason == expected_reason
                    break
        finally:
            transport.close()
    run(main())


def test_fake_firmware_logout_fences_active_live_stream(firmware):
    async def main():
        inbox = LivePcmInbox(CONTROLLER)
        transport, session = open_link(firmware, frame_sink=inbox)
        try:
            await session.login()
            assert (await session.command(
                f"liveaudio ready 1 {CONTROLLER:016x}",
                expect="status", timeout=5)).ok
            assert (await session.command(
                f"liveaudio synth 1 {CONTROLLER:016x} {EXCHANGE:016x} 1000",
                expect="status", timeout=5, replay=False)).ok
            await asyncio.to_thread(inbox.next_stream, 2.0)

            assert (await session.command(
                "logout", expect="status", timeout=5, replay=False,
                auth_replay=False)).ok
            deadline = time.monotonic() + 2.0
            while firmware.live_exchange_id is not None:
                assert time.monotonic() < deadline
                await asyncio.sleep(0.01)

            # Firmware detects auth loss internally, but its exact-session TX
            # fence also prevents the old stream's terminal crossing logout.
            assert firmware.live_last_terminal == "abort"
            assert firmware.live_last_terminal_sent is False
            assert firmware.live_stream_session_epoch == 0
        finally:
            transport.close()

    run(main())


def _probe_args():
    return SimpleNamespace(
        config="unused.yaml",
        duration_ms=128,
        controller_id=CONTROLLER,
        exchange_id=EXCHANGE,
    )


def _patch_probe_config(monkeypatch, firmware):
    cfg = SimpleNamespace(link=SimpleNamespace(
        port=firmware._slave_path,
        baud=921_600,
        credentials_file="unused.credentials",
    ))
    monkeypatch.setattr(probe.config_mod, "load", lambda _: cfg)
    monkeypatch.setattr(
        probe.config_mod, "read_credentials",
        lambda _: (firmware.user, firmware.password))

    # macOS refuses the production 921600 custom-baud ioctl on a pseudo-TTY.
    # Preserve the probe's configured-rate admission check while injecting a
    # 115200 pty handle; baud has no timing meaning on this in-memory link.
    def pty_transport(port, baud, *, frame_sink):
        ser = serial.Serial(
            port, 115_200, timeout=0.05, write_timeout=2.0)
        return SerialTransport(serial_obj=ser, frame_sink=frame_sink)

    monkeypatch.setattr(probe, "SerialTransport", pty_transport)


def test_standalone_probe_runs_end_to_end_and_confirms_release(
        firmware, monkeypatch):
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_probe(_probe_args()))
    assert result["ok"], result
    assert result["pattern_ok"]
    assert result["lease_errors"] == []
    assert firmware.live_controller_id is None


def test_probe_late_renewal_error_cannot_leave_ok_true(firmware, monkeypatch):
    _patch_probe_config(monkeypatch, firmware)

    async def late_renewal_error(
            session, controller_id, stop, errors, timing, session_epoch):
        await stop.wait()
        errors.append("injected late renewal failure")

    monkeypatch.setattr(probe, "_renew_lease", late_renewal_error)
    result = run(probe.run_probe(_probe_args()))
    assert not result["ok"]
    assert result["pattern_ok"]
    assert result["lease_errors"] == ["injected late renewal failure"]
    assert firmware.live_controller_id is None


@pytest.mark.parametrize("direct,expected_renew_ms", [
    (True, 1000),
    (False, 2000),
])
def test_probe_uses_ready_grant_timing(
        firmware, monkeypatch, direct, expected_renew_ms):
    _patch_probe_config(monkeypatch, firmware)
    firmware.live_renew_direct = direct
    observed = {}

    async def capture_timing(
            session, controller_id, stop, errors, timing, session_epoch):
        observed["timing"] = timing
        observed["epoch"] = session_epoch
        await stop.wait()

    monkeypatch.setattr(probe, "_renew_lease", capture_timing)
    result = run(probe.run_probe(_probe_args()))
    assert result["ok"], result
    assert observed["timing"].renew_ms == expected_renew_ms
    assert observed["timing"].direct is direct
    assert observed["epoch"] > 0
