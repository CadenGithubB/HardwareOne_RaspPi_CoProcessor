"""P2 binary frame layer: codec round-trips, transport demux, and the
voicefetch burst-transfer path end to end against the fake firmware."""

from __future__ import annotations

import pytest
from conftest import open_link, run
from fake_firmware import _cobs_encode, make_wav

from hw1_ai_service import bg
from hw1_ai_service.audio import fetch, wav
from hw1_ai_service.config import AudioConfig
from hw1_ai_service.link import protocol


# -- codec units -------------------------------------------------------------

def test_crc16_known_vector():
    # CRC16-CCITT-FALSE("123456789") == 0x29B1 (standard check value).
    assert protocol.crc16_ccitt(b"123456789") == 0x29B1


def test_crc16_matches_bitwise_reference():
    """The hot path delegates to binascii; this is what stops it drifting
    from the firmware's uartCrc16(). Covers non-default init values because
    parse_frame_body seeds a running CRC, not a fresh one."""
    import random
    rng = random.Random(0)
    for _ in range(200):
        data = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 1200)))
        for init in (0xFFFF, 0x0000, 0x1234):
            assert (protocol.crc16_ccitt(data, init)
                    == protocol._crc16_ccitt_bitwise(data, init))


@pytest.mark.parametrize("payload", [
    b"", b"\x00", b"\x00\x00\x00", b"no zeros here", bytes(range(256)),
    b"\xff" * 300, bytes([0] * 260), bytes(range(256)) * 5,
])
def test_cobs_roundtrip(payload):
    encoded = _cobs_encode(payload)
    assert 0 not in encoded, "COBS output must contain no 0x00"
    assert protocol.cobs_decode(encoded) == payload


def test_frame_body_parse_and_crc():
    payload = b"hello frame"
    body = bytes([protocol.FRAME_AUDIO, 7, 0, len(payload), 0]) + payload
    crc = protocol.crc16_ccitt(body)
    body += bytes([crc & 0xFF, crc >> 8])
    ftype, seq, got = protocol.parse_frame_body(body)
    assert (ftype, seq, got) == (protocol.FRAME_AUDIO, 7, payload)


def test_frame_body_rejects_bad_crc():
    payload = b"tamper"
    body = bytes([protocol.FRAME_AUDIO, 0, 0, len(payload), 0]) + payload + b"\x00\x00"
    with pytest.raises(ValueError, match="CRC"):
        protocol.parse_frame_body(body)


# -- transport demux ---------------------------------------------------------

def test_transport_demux_frames_and_text(firmware):
    """Interleave a text line and a binary frame on the wire; the reader must
    surface both correctly and keep text/line alignment."""
    import asyncio
    import serial
    from hw1_ai_service.link.transport import SerialTransport

    async def main():
        ser = serial.Serial(firmware._slave_path, 115200, timeout=0.05)
        transport = SerialTransport(serial_obj=ser)
        transport.open()
        try:
            payload = b"\x01\x02\x00\x03frame-with-zero"
            body = bytes([protocol.FRAME_AUDIO, 1, 0, len(payload), 0]) + payload
            crc = protocol.crc16_ccitt(body)
            body += bytes([crc & 0xFF, crc >> 8])
            wire_frame = b"\x00" + _cobs_encode(body) + b"\x00"

            # text line, then frame, then text line
            firmware._write_raw(b"OK: before\n")
            firmware._write_raw(wire_frame)
            firmware._write_raw(b"OK: after\n")

            kinds = []
            import time
            deadline = time.monotonic() + 5
            while len(kinds) < 3 and time.monotonic() < deadline:
                try:
                    ev = await asyncio.wait_for(transport.rx.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                kinds.append(ev)
            by_kind = {e.kind: e for e in kinds}
            assert "line" in by_kind and "frame" in by_kind
            frame_ev = by_kind["frame"]
            ftype, seq, got = protocol.parse_frame_body(frame_ev.frame)
            assert got == payload
            texts = [e.text for e in kinds if e.kind == "line"]
            assert "OK: before" in texts and "OK: after" in texts
        finally:
            transport.close()
    run(main())


# -- voicefetch end to end ---------------------------------------------------

def test_voicefetch_roundtrip(firmware):
    firmware.wav_bytes = make_wav(seconds=2.0)   # ~64KB -> 63 audio frames

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = AudioConfig(record_seconds=0.05, transfer="voicefetch")
            data = await fetch.record_utterance(session, cfg)
            await bg.drain()
            assert data == firmware.wav_bytes
            parsed = wav.parse(data)
            wav.require_canonical(parsed)
            assert firmware.deleted
        finally:
            transport.close()
    run(main())


def test_voicefetch_auto_falls_back_to_fileread(firmware):
    """A pre-P2 firmware (no voicefetch) must transparently fall back."""
    firmware.support_voicefetch = False
    firmware.wav_bytes = make_wav(seconds=1.0)

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            cfg = AudioConfig(record_seconds=0.05, transfer="auto")
            data = await fetch.record_utterance(session, cfg)
            await bg.drain()
            assert data == firmware.wav_bytes
            # proves it used the base64 path
            assert any("fileread" in c for c in firmware.command_log)
        finally:
            transport.close()
    run(main())


def test_reader_recovers_from_frame_mode_wedge(firmware):
    """A boot burst with odd 0x00 parity leaves the reader mid-frame; without
    the idle-abort it would swallow every later text reply forever. Assert a
    normal command works right after such a burst (the HIGH review finding)."""
    import asyncio
    import serial
    from hw1_ai_service.link.transport import SerialTransport
    from hw1_ai_service.link.session import Session

    async def main():
        ser = serial.Serial(firmware._slave_path, 115200, timeout=0.05)
        transport = SerialTransport(serial_obj=ser)
        transport.open()
        session = Session(transport, firmware.user, firmware.password)
        try:
            # Inject bytes with a SINGLE 0x00 (odd parity) — reader enters
            # frame mode and, pre-fix, stays there.
            firmware._write_raw(b"\x00garbage-with-one-delim-no-close")
            await asyncio.sleep(0.2)   # let the idle-abort fire (>50ms idle)
            # Now a normal login + command must work.
            await session.login()
            rep = await session.command("uartlink status", expect="status", timeout=10)
            assert rep.ok
        finally:
            transport.close()
    run(main())


def test_voicefetch_speed_advantage(firmware):
    """voicefetch is one command; fileread is ~N round trips. Assert the
    command COUNT collapses (the real-world speed win), not wall-clock."""
    firmware.wav_bytes = make_wav(seconds=3.0)   # ~96KB

    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            firmware.command_log.clear()
            cfg = AudioConfig(record_seconds=0.05, transfer="voicefetch")
            await fetch.record_utterance(session, cfg)
            await bg.drain()
            voicefetch_cmds = [c for c in firmware.command_log if c.startswith("voicefetch")]
            fileread_cmds = [c for c in firmware.command_log if c.startswith("fileread")]
            assert len(voicefetch_cmds) == 1
            assert len(fileread_cmds) == 0  # zero base64 round trips
        finally:
            transport.close()
    run(main())
