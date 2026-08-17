"""Wire-protocol constants and reply classifiers.

Everything here mirrors the firmware side (System_UartLink.cpp and the
uniform OK:/Error: return contract). P2 adds the COBS frame demux beside
the text rules — which is why this module owns byte-level conventions and
nothing else.
"""

from __future__ import annotations

import binascii
import re
import struct

from dataclasses import dataclass

# Firmware caps (System_UartLink.cpp kUartLineCap; CMD_RESULT_MAX).
MAX_CMD_LINE = 2047        # bytes, excluding the newline; firmware discards over-length WHOLE
MAX_REPLY = 4095           # bytes per reply blob

# submitAndExecuteSync worst case is 2s queue + 60s semaphore = 62s.
DEFAULT_CMD_TIMEOUT_S = 65.0

# Reply-collection quiet gap for multi-line replies (auto mode). Replies are
# written as one blob so their lines arrive back-to-back; 150ms of silence
# after at least one line means the reply is over. Retired at P2 (framing).
QUIET_GAP_S = 0.15

# Reader-side line sanity bound: legitimate replies are <= MAX_REPLY + '\n';
# anything longer without a newline is garbage (wrong-baud boot burst etc).
MAX_RX_LINE = 8192

# The firmware's REAL status vocabulary (review-corrected; the success test
# in System_Utils.cpp strncmp's both spellings and stampOkStatus never
# rewrites failures): successes are "OK"/"OK: ..."; failures appear as
# "Error...", bare "ERROR", "ERROR: ...", and the executor's own
# "[ERROR] Command timed out". "Unknown command:" is the two-line
# unprefixed reply for commands missing from the target build — treated as
# a terminal error so a missing feature fails fast instead of timing out.
_OK_RE = re.compile(r"^OK\b")
_ERR_RE = re.compile(r"^(?:\[ERROR\]|(?:ERROR|Error)\b|Unknown command:)")
_AUTH_REQUIRED_RE = re.compile(r"authentication required", re.IGNORECASE)
_SIGNED_OUT_RE = re.compile(r"signed out due to inactivity", re.IGNORECASE)
_LOCKOUT_RE = re.compile(r"login locked out.*?(\d+)\s*seconds", re.IGNORECASE)
_LIVE_CAPABILITY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def is_status_line(line: str) -> bool:
    return bool(_OK_RE.match(line) or _ERR_RE.match(line))


def is_ok_line(line: str) -> bool:
    return bool(_OK_RE.match(line))


def is_error_line(line: str) -> bool:
    return bool(_ERR_RE.match(line))


def is_auth_required(line: str) -> bool:
    return bool(_AUTH_REQUIRED_RE.search(line) or _SIGNED_OUT_RE.search(line))


def lockout_seconds(line: str) -> int | None:
    m = _LOCKOUT_RE.search(line)
    return int(m.group(1)) if m else None


def quote_path(path: str) -> str:
    """Firmware path arguments use the uniform quoted-token rule."""
    if '"' in path:
        raise ValueError(f"path may not contain a double quote: {path!r}")
    return f'"{path}"'


def parse_live_capabilities(text: str) -> dict[str, str]:
    """Parse the strict ``liveaudio capabilities`` key/value reply.

    Capability consumers intentionally select the individual feature they
    need.  In particular, the Phase-2A synthetic probe must continue to work
    when a newer firmware changes ``recorder_shadow=0`` to ``1``; matching the
    entire human-readable reply made those independent capabilities mutually
    incompatible.

    Unknown keys are retained for forward-compatible diagnostics, while
    malformed or duplicate keys fail closed so a contradictory reply cannot
    silently select whichever token a caller happened to inspect first.
    """
    tokens = text.strip().split()
    if tokens[:1] == ["OK:"]:
        tokens = tokens[1:]
    if not tokens or tokens[0] != "live-pcm-v1":
        raise ValueError("liveaudio capabilities missing live-pcm-v1 marker")
    out: dict[str, str] = {}
    for token in tokens[1:]:
        if token.count("=") != 1:
            raise ValueError(f"malformed live capability token {token!r}")
        key, value = token.split("=", 1)
        if not _LIVE_CAPABILITY_KEY_RE.fullmatch(key) or not value:
            raise ValueError(f"malformed live capability token {token!r}")
        if key in out:
            raise ValueError(f"duplicate live capability {key!r}")
        out[key] = value
    return out


@dataclass(frozen=True)
class LiveReadyReply:
    """Strict fields from a successful ``liveaudio ready`` grant.

    ``renew_direct`` is an explicit firmware capability.  The timing values
    predate the direct renewal path, so they are not sufficient on their own
    to select a faster host cadence.
    """

    controller_id: int
    session_epoch: int
    lease_ttl_ms: int | None
    renew_ms: int | None
    renew_direct: bool


@dataclass(frozen=True)
class LiveLeaseTiming:
    renew_ms: int
    lease_ttl_ms: int
    direct: bool


# Direct-renew scheduling contract. Current firmware advertises 1000/3000 ms;
# the bounds prevent a malformed marked reply from creating a command flood or
# eliminating the host's opportunity for one complete retry before expiry.
LIVE_DIRECT_RENEW_MIN_MS = 500
LIVE_DIRECT_RENEW_MAX_MS = 5000
LIVE_DIRECT_TTL_MIN_MS = 2000
LIVE_DIRECT_TTL_MAX_MS = 60000
LIVE_LEGACY_RENEW_MS = 2000
LIVE_LEGACY_TTL_MS = 3000


def parse_live_ready(text: str, *, expected_controller: int) -> LiveReadyReply:
    """Parse one canonical ``OK: liveaudio ready`` reply.

    Duplicate or malformed key/value fields fail closed.  Legacy version-1
    replies may omit ``renew_direct`` and continue to use the host's legacy
    cadence; a reply that advertises direct renewal must also carry both
    decimal timing fields.
    """

    tokens = text.strip().split()
    if tokens[:3] != ["OK:", "liveaudio", "ready"]:
        raise ValueError("liveaudio ready reply has an invalid prefix")
    fields: dict[str, str] = {}
    for token in tokens[3:]:
        if token.count("=") != 1:
            raise ValueError(f"malformed live ready token {token!r}")
        key, value = token.split("=", 1)
        if not _LIVE_CAPABILITY_KEY_RE.fullmatch(key) or not value:
            raise ValueError(f"malformed live ready token {token!r}")
        if key in fields:
            raise ValueError(f"duplicate live ready field {key!r}")
        fields[key] = value

    if fields.get("version") != str(LIVE_PROTOCOL_VERSION):
        raise ValueError("unsupported liveaudio ready version")
    controller_text = fields.get("controller")
    if controller_text != f"{expected_controller:016x}":
        raise ValueError("liveaudio ready controller mismatch")

    def decimal_field(name: str, *, required: bool) -> int | None:
        value = fields.get(name)
        if value is None:
            if required:
                raise ValueError(f"liveaudio ready missing {name}")
            return None
        if (not value.isascii() or not value.isdecimal() or
                str(int(value, 10)) != value):
            raise ValueError(f"liveaudio ready {name} is not canonical decimal")
        return int(value, 10)

    session_epoch = decimal_field("session_epoch", required=True)
    assert session_epoch is not None
    if session_epoch <= 0:
        raise ValueError("liveaudio ready session_epoch must be positive")

    direct_value = fields.get("renew_direct")
    if direct_value not in {None, "1"}:
        raise ValueError("liveaudio ready renew_direct must be 1 when present")
    renew_direct = direct_value == "1"
    lease_ttl_ms = decimal_field(
        "lease_ttl_ms", required=renew_direct)
    renew_ms = decimal_field("renew_ms", required=renew_direct)
    return LiveReadyReply(
        controller_id=expected_controller,
        session_epoch=session_epoch,
        lease_ttl_ms=lease_ttl_ms,
        renew_ms=renew_ms,
        renew_direct=renew_direct,
    )


def live_lease_timing_from_ready(
        ready: LiveReadyReply, *,
        legacy_renew_ms: int = LIVE_LEGACY_RENEW_MS,
        legacy_ttl_ms: int = LIVE_LEGACY_TTL_MS) -> LiveLeaseTiming:
    """Select bounded scheduling from a parsed ready grant.

    Timing fields alone are not a capability: pre-intrinsic firmware already
    advertised 1000/3000 ms. Marker-less replies deliberately retain the
    caller's legacy cadence.
    """

    if not ready.renew_direct:
        return LiveLeaseTiming(
            renew_ms=legacy_renew_ms,
            lease_ttl_ms=legacy_ttl_ms,
            direct=False,
        )
    renew_ms = ready.renew_ms
    ttl_ms = ready.lease_ttl_ms
    if renew_ms is None or ttl_ms is None:
        raise ValueError("direct renewal grant is missing timing")
    if not (LIVE_DIRECT_RENEW_MIN_MS <= renew_ms <=
            LIVE_DIRECT_RENEW_MAX_MS):
        raise ValueError(f"direct renew_ms out of bounds: {renew_ms}")
    if not (LIVE_DIRECT_TTL_MIN_MS <= ttl_ms <= LIVE_DIRECT_TTL_MAX_MS):
        raise ValueError(f"direct lease_ttl_ms out of bounds: {ttl_ms}")
    if 2 * renew_ms >= ttl_ms:
        raise ValueError(
            f"direct renewal has no retry margin: renew={renew_ms} ttl={ttl_ms}")
    return LiveLeaseTiming(
        renew_ms=renew_ms, lease_ttl_ms=ttl_ms, direct=True)


# --- P2 binary frame layer (mirror of System_UartLink.cpp; change both) ------
#
#   0x00  COBS(body)  0x00
#   body := type(1) | seq_le(2) | len_le(2) | payload(len) | crc_le(2)
#   crc  := CRC16-CCITT-FALSE over type..payload
FRAME_DELIM = 0x00
FRAME_AUDIO = 0x01
FRAME_META = 0x02
FRAME_EVT = 0x03    # spontaneous device event push (short ASCII payload)

# Live PCM v1. These types are deliberately disjoint from voicefetch's
# command-scoped AUDIO/META frames. Live frames are unsolicited and must be
# claimed by the reader-thread sink before they can reach Session's generic
# event queue.
FRAME_LIVE_BEGIN = 0x10
FRAME_LIVE_PCM = 0x11
FRAME_LIVE_END = 0x12
FRAME_LIVE_ABORT = 0x13
LIVE_FRAME_TYPES = frozenset({
    FRAME_LIVE_BEGIN,
    FRAME_LIVE_PCM,
    FRAME_LIVE_END,
    FRAME_LIVE_ABORT,
})

LIVE_PROTOCOL_VERSION = 1
LIVE_FLAG_SYNTHETIC = 0x01
LIVE_KNOWN_FLAGS = LIVE_FLAG_SYNTHETIC
LIVE_SOURCE_SYNTHETIC = 0
LIVE_SOURCE_PDM = 1
LIVE_SOURCE_G2 = 2
LIVE_SOURCES = frozenset({
    LIVE_SOURCE_SYNTHETIC,
    LIVE_SOURCE_PDM,
    LIVE_SOURCE_G2,
})
LIVE_FORMAT_S16LE_MONO = 1

# Terminal reasons are part of live-pcm-v1, not an open-ended diagnostic
# namespace.  A successful END must carry zero; ABORT carries one of the
# finite firmware causes below.  Keeping this strict prevents a corrupted or
# future-incompatible terminal from being promoted as a valid stream.
LIVE_END_REASON_OK = 0
LIVE_ABORT_REASON_LEASE_EXPIRED = 1
LIVE_ABORT_REASON_AUTH_LOST = 2
LIVE_ABORT_REASON_LINK_LOST = 3
LIVE_ABORT_REASON_RELEASED = 4
LIVE_ABORT_REASON_HOST_REQUEST = 5
LIVE_ABORT_REASON_TX_BACKPRESSURE = 6
LIVE_ABORT_REASON_INTERNAL = 7
LIVE_ABORT_REASONS = frozenset({
    LIVE_ABORT_REASON_LEASE_EXPIRED,
    LIVE_ABORT_REASON_AUTH_LOST,
    LIVE_ABORT_REASON_LINK_LOST,
    LIVE_ABORT_REASON_RELEASED,
    LIVE_ABORT_REASON_HOST_REQUEST,
    LIVE_ABORT_REASON_TX_BACKPRESSURE,
    LIVE_ABORT_REASON_INTERNAL,
})

# Payloads mirror the packed firmware structs exactly. The PCM header is 24 B,
# leaving a deliberately round 1000 B / 500 samples inside the existing 1024 B
# outer-frame payload ceiling.
LIVE_BEGIN_STRUCT = struct.Struct("<BBBBIQQHH")
LIVE_PCM_HEADER_STRUCT = struct.Struct("<BBQQIH")
LIVE_TERMINAL_STRUCT = struct.Struct("<BBQQIII")
LIVE_PCM_MAX_BYTES = 1000
LIVE_PCM_MAX_SAMPLES = LIVE_PCM_MAX_BYTES // 2


@dataclass(frozen=True)
class LiveBegin:
    flags: int
    source: int
    sample_format: int
    sample_rate: int
    exchange_id: int
    controller_id: int
    logical_chunk_samples: int

    @property
    def synthetic(self) -> bool:
        return bool(self.flags & LIVE_FLAG_SYNTHETIC)


@dataclass(frozen=True)
class LivePcm:
    flags: int
    exchange_id: int
    controller_id: int
    sample_offset: int
    sample_count: int
    pcm: bytes


@dataclass(frozen=True)
class LiveTerminal:
    reason: int
    exchange_id: int
    controller_id: int
    total_samples: int
    pcm_crc32: int
    dropped_samples: int


def live_id_hex(value: int) -> str:
    """Canonical text form shared with the existing EvenAI command grammar."""
    _validate_live_id(value, "ID")
    return f"{value:016x}"


def _validate_live_id(value: int, label: str) -> None:
    if not (0 < value <= 0xFFFFFFFFFFFFFFFF):
        raise ValueError(f"live {label} is outside uint64")
    if (value >> 32) == 0 or (value & 0xFFFFFFFF) == 0:
        raise ValueError(f"live {label} requires nonzero high and low 32-bit halves")


def _validate_live_common(version: int, flags: int,
                          exchange_id: int, controller_id: int) -> None:
    if version != LIVE_PROTOCOL_VERSION:
        raise ValueError(f"unsupported live PCM version {version}")
    if flags & ~LIVE_KNOWN_FLAGS:
        raise ValueError(f"unknown live PCM flags 0x{flags:02x}")
    _validate_live_id(exchange_id, "exchange ID")
    _validate_live_id(controller_id, "controller ID")


def parse_live_begin(payload: bytes) -> LiveBegin:
    if len(payload) != LIVE_BEGIN_STRUCT.size:
        raise ValueError(
            f"LIVE_BEGIN length {len(payload)} != {LIVE_BEGIN_STRUCT.size}")
    (version, flags, source, sample_format, sample_rate, exchange_id,
     controller_id, logical_chunk_samples, reserved) = LIVE_BEGIN_STRUCT.unpack(payload)
    _validate_live_common(version, flags, exchange_id, controller_id)
    if source not in LIVE_SOURCES:
        raise ValueError(f"unknown live PCM source {source}")
    if sample_format != LIVE_FORMAT_S16LE_MONO:
        raise ValueError(f"unsupported live PCM format {sample_format}")
    if sample_rate != 16_000:
        raise ValueError(f"unsupported live PCM sample rate {sample_rate}; v1 requires 16000")
    if logical_chunk_samples == 0:
        raise ValueError("live PCM logical chunk cannot be empty")
    if reserved != 0:
        raise ValueError(f"LIVE_BEGIN reserved field is nonzero ({reserved})")
    if bool(flags & LIVE_FLAG_SYNTHETIC) != (source == LIVE_SOURCE_SYNTHETIC):
        raise ValueError("live PCM synthetic flag/source mismatch")
    return LiveBegin(
        flags=flags,
        source=source,
        sample_format=sample_format,
        sample_rate=sample_rate,
        exchange_id=exchange_id,
        controller_id=controller_id,
        logical_chunk_samples=logical_chunk_samples,
    )


def parse_live_pcm(payload: bytes) -> LivePcm:
    header_size = LIVE_PCM_HEADER_STRUCT.size
    if len(payload) < header_size:
        raise ValueError(f"LIVE_PCM length {len(payload)} < header {header_size}")
    (version, flags, exchange_id, controller_id,
     sample_offset, sample_count) = LIVE_PCM_HEADER_STRUCT.unpack_from(payload)
    _validate_live_common(version, flags, exchange_id, controller_id)
    pcm = payload[header_size:]
    if sample_count == 0 or sample_count > LIVE_PCM_MAX_SAMPLES:
        raise ValueError(f"LIVE_PCM sample count {sample_count} outside 1..500")
    if len(pcm) != sample_count * 2:
        raise ValueError(
            f"LIVE_PCM declares {sample_count} samples but carries {len(pcm)} bytes")
    if len(pcm) > LIVE_PCM_MAX_BYTES:
        raise ValueError(f"LIVE_PCM carries {len(pcm)} bytes, max is 1000")
    if sample_offset + sample_count > 0x1_0000_0000:
        raise ValueError("LIVE_PCM sample range overflows uint32")
    return LivePcm(
        flags=flags,
        exchange_id=exchange_id,
        controller_id=controller_id,
        sample_offset=sample_offset,
        sample_count=sample_count,
        pcm=pcm,
    )


def parse_live_terminal(payload: bytes) -> LiveTerminal:
    if len(payload) != LIVE_TERMINAL_STRUCT.size:
        raise ValueError(
            f"live terminal length {len(payload)} != {LIVE_TERMINAL_STRUCT.size}")
    (version, reason, exchange_id, controller_id, total_samples,
     pcm_crc32, dropped_samples) = LIVE_TERMINAL_STRUCT.unpack(payload)
    # Terminal byte 1 is a reason, not the common flags field.
    if version != LIVE_PROTOCOL_VERSION:
        raise ValueError(f"unsupported live PCM version {version}")
    _validate_live_id(exchange_id, "exchange ID")
    _validate_live_id(controller_id, "controller ID")
    return LiveTerminal(
        reason=reason,
        exchange_id=exchange_id,
        controller_id=controller_id,
        total_samples=total_samples,
        pcm_crc32=pcm_crc32,
        dropped_samples=dropped_samples,
    )


def parse_live_payload(ftype: int, payload: bytes) -> LiveBegin | LivePcm | LiveTerminal:
    if ftype == FRAME_LIVE_BEGIN:
        return parse_live_begin(payload)
    if ftype == FRAME_LIVE_PCM:
        return parse_live_pcm(payload)
    if ftype in (FRAME_LIVE_END, FRAME_LIVE_ABORT):
        return parse_live_terminal(payload)
    raise ValueError(f"not a live PCM frame type: 0x{ftype:02x}")


def crc32_ieee(data: bytes, crc: int = 0) -> int:
    """Streaming zlib/IEEE CRC32; ``123456789`` has check value CBF43926."""
    return binascii.crc32(data, crc) & 0xFFFFFFFF


def _crc16_ccitt_bitwise(data: bytes, crc: int = 0xFFFF) -> int:
    """Bit-serial reference: the executable spec, mirroring uartCrc16() in
    System_UartLink.cpp. Kept so the firmware's algorithm stays documented
    and any drift is caught by a test — not used on the hot path."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC16-CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflect, no xorout.
    Byte-identical to uartCrc16() in the firmware.

    binascii.crc_hqx is the same algorithm in C. This runs over ~4x the
    file's bytes per voicefetch (three frame parses plus the whole-file
    check), so the bit-serial version above cost ~750us per 1KB frame and
    was the single largest term in transfer time — see the reference impl
    for what the firmware does.
    """
    return binascii.crc_hqx(data, crc)


def cobs_decode(encoded: bytes) -> bytes:
    """Inverse of the firmware's uartCobsEncode. Raises ValueError on a
    malformed run (which the frame layer treats as a dropped frame)."""
    out = bytearray()
    i = 0
    n = len(encoded)
    while i < n:
        code = encoded[i]
        if code == 0:
            raise ValueError("zero code byte inside COBS data")
        i += 1
        end = i + code - 1
        if end > n:
            raise ValueError("COBS run overruns buffer")
        out.extend(encoded[i:end])
        i = end
        if code < 0xFF and i < n:
            out.append(0)
    return bytes(out)


def parse_frame_body(body: bytes) -> tuple[int, int, bytes]:
    """(type, seq, payload) from a decoded frame body, or raise ValueError
    on a length or CRC mismatch."""
    if len(body) < 7:
        raise ValueError(f"frame body too short: {len(body)}B")
    ftype = body[0]
    seq = body[1] | (body[2] << 8)
    length = body[3] | (body[4] << 8)
    if len(body) != 5 + length + 2:
        raise ValueError(
            f"frame length mismatch: header {length}, have {len(body) - 7}")
    payload = body[5:5 + length]
    got_crc = body[5 + length] | (body[6 + length] << 8)
    want_crc = crc16_ccitt(body[:5 + length])
    if got_crc != want_crc:
        raise ValueError(f"frame CRC mismatch: got {got_crc:04X}, want {want_crc:04X}")
    return ftype, seq, payload
