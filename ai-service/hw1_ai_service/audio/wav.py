"""Minimal RIFF/WAV parser. The firmware writes canonical 16-bit PCM WAVs
(System_Microphone.cpp), but the header is still validated — a rate or
format surprise must be a loud error here, never a silent resample."""

from __future__ import annotations

import struct
from dataclasses import dataclass


class WavError(ValueError):
    pass


@dataclass
class WavData:
    rate: int
    channels: int
    bits: int
    pcm: bytes


def parse(data: bytes) -> WavData:
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise WavError("not a RIFF/WAVE file")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        (size,) = struct.unpack_from("<I", data, pos + 4)
        body = data[pos + 8: pos + 8 + size]
        if len(body) != size:
            # A lying header (declared size exceeds actual bytes) is a real
            # producer bug on the firmware side (short writes on a full FS
            # are ignored) — fail loudly instead of transcribing truncated
            # audio with no error (review finding).
            raise WavError(
                f"truncated {cid!r} chunk: header declares {size}B, "
                f"only {len(body)}B present")
        if cid == b"fmt ":
            if len(body) < 16:
                raise WavError("truncated fmt chunk")
            audio_fmt, channels, rate, _br, _ba, bits = struct.unpack_from("<HHIIHH", body, 0)
            if audio_fmt != 1:
                raise WavError(f"not linear PCM (fmt={audio_fmt})")
            fmt = (rate, channels, bits)
        elif cid == b"data":
            pcm = bytes(body)
        pos += 8 + size + (size & 1)   # chunks are word-aligned
    if fmt is None or pcm is None:
        raise WavError("missing fmt or data chunk")
    rate, channels, bits = fmt
    if bits == 16 and len(pcm) % 2:
        raise WavError(f"odd data length {len(pcm)} for 16-bit audio")
    return WavData(rate=rate, channels=channels, bits=bits, pcm=pcm)


def require_canonical(w: WavData) -> None:
    """The STT engines are configured for 16kHz/mono/16-bit — anything else
    means the firmware-side settings drifted; fail loudly."""
    if (w.rate, w.channels, w.bits) != (16000, 1, 16):
        raise WavError(
            f"expected 16000Hz/1ch/16-bit, got {w.rate}Hz/{w.channels}ch/{w.bits}-bit "
            f"(check micsamplerate/micbitdepth on the device)")


def build(pcm: bytes, rate: int) -> bytes:
    """Canonical mono 16-bit WAV around raw little-endian PCM.

    Used by the live-STT path, whose audio never existed as a device WAV on
    the host: persistence/diagnostics still expect one file format everywhere.
    The output round-trips through parse()+require_canonical() when rate is
    16000.
    """
    if len(pcm) % 2:
        raise WavError(f"odd PCM byte length {len(pcm)} for 16-bit audio")
    if rate <= 0:
        raise WavError(f"invalid sample rate {rate}")
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + len(pcm), b"WAVE",
        b"fmt ", 16, 1, 1, rate, rate * 2, 2, 16,
        b"data", len(pcm))
    return header + pcm
