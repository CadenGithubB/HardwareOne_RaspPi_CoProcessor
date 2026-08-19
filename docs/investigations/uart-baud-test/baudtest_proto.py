"""Shared protocol definitions for the CM5 <-> XIAO UART baud-rate test.

KEEP IN SYNC with firmware/main/uart_test_proto.h — the firmware returns
PROTO_VERSION in GET_INFO and the controller refuses to run on a mismatch,
so drift is caught at runtime, but the two files must be edited together.

Wire format (all little-endian):

    offset  size  field
    0       2     magic 0xA5 0x5A
    2       1     type
    3       1     flags (pattern id for DATA frames, else 0)
    4       4     seq
    8       2     payload length N (0..MAX_PAYLOAD)
    10      N     payload
    10+N    4     CRC32 (zlib/IEEE, over bytes 0..10+N-1)
"""

import struct
import zlib

PROTO_VERSION = 1

MAGIC = b"\xA5\x5A"
HDR_LEN = 10
CRC_LEN = 4
MAX_PAYLOAD = 4096
MAX_FRAME = HDR_LEN + MAX_PAYLOAD + CRC_LEN

# Commands (CM5 -> XIAO); responses are command | 0x80 unless noted.
CMD_PING = 0x01        # payload: 8-byte nonce
CMD_SET_BAUD = 0x02    # payload: u32 baud, u8 stop_bits, u8[3] rsv
CMD_ECHO_DATA = 0x03   # payload: pattern data; flags = pattern id
CMD_SINK_DATA = 0x04   # payload: pattern data; no response (XIAO verifies)
CMD_GEN_START = 0x05   # payload: u32 duration_ms, u32 max_frames, u16 len, u8 pattern, u8 rsv
CMD_GET_STATS = 0x06
CMD_CLEAR_STATS = 0x07
CMD_GET_INFO = 0x08
CMD_ABORT = 0x09       # stop an in-progress GEN

RESP_PONG = 0x81       # payload: echoed nonce + u32 actual_baud
RESP_ACK = 0x82        # payload: u8 status, u8[3] rsv, u32 detail, u32 switch_delay_ms
RESP_ECHO = 0x83
RESP_GEN_DATA = 0x85
RESP_GEN_DONE = 0x86   # payload: u32 frames_sent, u64 wire_bytes_sent, u32 aborted
RESP_STATS = 0x87      # payload: stats struct below
RESP_INFO = 0x88       # payload: info struct below

ACK_OK = 0
ACK_REJECT_RANGE = 1   # requested baud outside what the ESP32 UART can do
ACK_REJECT_BUSY = 2

# Test patterns. flags byte of DATA frames carries the pattern id.
PAT_INC = 0     # byte i = (seq + i) & 0xFF
PAT_PRNG = 1    # xorshift32 keyed by seq (see gen_payload)
PAT_ALT = 2     # 0x55 / 0xAA alternating
PAT_ZERO = 3    # all 0x00
PAT_ONES = 4    # all 0xFF
PAT_CYCLE = 0xFF  # GEN_START only: firmware uses pattern = seq % 5
PATTERN_COUNT = 5

PATTERN_NAMES = {PAT_INC: "inc", PAT_PRNG: "prng", PAT_ALT: "alt",
                 PAT_ZERO: "zero", PAT_ONES: "ones"}
NAME_TO_PATTERN = {v: k for k, v in PATTERN_NAMES.items()}

# PRNG constants — must match the firmware exactly.
PRNG_KEY = 0xC0FFEE42
PRNG_MIX = 0x9E3779B9
PRNG_ZERO_FALLBACK = 0xDEADBEEF

# Firmware timing constants (mirrored so the controller can reason about them).
SWITCH_DELAY_MS = 40      # XIAO waits this long after ACK tx-drain before switching
SYNC_TIMEOUT_MS = 3000    # XIAO reverts to safe baud if no valid frame after a switch
IDLE_REVERT_MS = 8000     # XIAO reverts to safe baud after this much silence

STATS_FMT = "<13I2QI"
STATS_FIELDS = (
    "rx_frames_ok", "rx_crc_err", "rx_resync_bytes",
    "rx_mismatch_frames", "rx_mismatch_bytes",
    "rx_seq_gap_frames", "rx_seq_dup_frames",
    "hw_frame_err", "hw_parity_err", "hw_fifo_ovf", "hw_buffer_full",
    "rx_breaks", "tx_frames", "rx_bytes", "tx_bytes", "actual_baud",
)
STATS_SIZE = struct.calcsize(STATS_FMT)  # 72

INFO_FMT = "<4I4B4I12s"
INFO_FIELDS = (
    "proto_version", "fw_version", "soc_max_baud", "actual_baud",
    "uart_num", "tx_pin", "rx_pin", "stop_bits",
    "rx_ring", "tx_ring", "sync_timeout_ms", "idle_revert_ms", "chip",
)
INFO_SIZE = struct.calcsize(INFO_FMT)  # 48

ACK_FMT = "<B3xII"
SET_BAUD_FMT = "<IB3x"
GEN_START_FMT = "<IIHBB"
GEN_DONE_FMT = "<IQI"


def build_frame(ftype, flags, seq, payload=b""):
    hdr = MAGIC + struct.pack("<BBIH", ftype, flags, seq & 0xFFFFFFFF, len(payload))
    body = hdr + payload
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


# ---------------------------------------------------------------------------
# Deterministic payload generation (identical logic in the firmware)
# ---------------------------------------------------------------------------

_INC_TABLE = bytes(range(256)) * ((MAX_PAYLOAD // 256) + 2)
_ALT_TABLE = b"\x55\xAA" * ((MAX_PAYLOAD // 2) + 1)


def gen_payload(pattern, seq, length):
    if pattern == PAT_INC:
        start = seq & 0xFF
        return _INC_TABLE[start:start + length]
    if pattern == PAT_PRNG:
        x = ((seq * PRNG_MIX) ^ PRNG_KEY) & 0xFFFFFFFF
        if x == 0:
            x = PRNG_ZERO_FALLBACK
        out = bytearray()
        while len(out) < length:
            x = (x ^ (x << 13)) & 0xFFFFFFFF
            x = x ^ (x >> 17)
            x = (x ^ (x << 5)) & 0xFFFFFFFF
            out += x.to_bytes(4, "little")
        return bytes(out[:length])
    if pattern == PAT_ALT:
        return _ALT_TABLE[:length]
    if pattern == PAT_ZERO:
        return b"\x00" * length
    if pattern == PAT_ONES:
        return b"\xFF" * length
    raise ValueError("unknown pattern %r" % (pattern,))


def diff_bytes(expected, got):
    """Count byte positions that differ (plus any length difference)."""
    if expected == got:
        return 0
    n = min(len(expected), len(got))
    d = sum(1 for i in range(n) if expected[i] != got[i])
    return d + abs(len(expected) - len(got))


class FrameParser:
    """Incremental frame extractor with resync + CRC accounting.

    feed() returns a list of (type, flags, seq, payload) tuples and keeps
    running counts of discarded (resync) bytes and CRC-failed frames.
    """

    def __init__(self):
        self.buf = bytearray()
        self.resync_bytes = 0
        self.crc_errors = 0

    def reset_counts(self):
        self.resync_bytes = 0
        self.crc_errors = 0

    def clear(self):
        self.buf.clear()

    def feed(self, data):
        frames = []
        b = self.buf
        if data:
            b.extend(data)
        start = 0
        n = len(b)
        while True:
            i = b.find(MAGIC, start)
            if i < 0:
                # Keep a trailing 0xA5 in case its 0x5A partner is in flight.
                keep = 1 if n and b[n - 1] == MAGIC[0] else 0
                self.resync_bytes += (n - keep) - start
                del b[:n - keep]
                break
            self.resync_bytes += i - start
            start = i
            if n - start < HDR_LEN:
                del b[:start]
                break
            ftype, flags, seq, plen = struct.unpack_from("<BBIH", b, start + 2)
            if plen > MAX_PAYLOAD:
                # Bogus header — the magic was a coincidence in data.
                self.resync_bytes += 1
                start += 1
                continue
            total = HDR_LEN + plen + CRC_LEN
            if n - start < total:
                del b[:start]
                break
            frame = bytes(b[start:start + total])
            (crc,) = struct.unpack_from("<I", frame, HDR_LEN + plen)
            if (zlib.crc32(frame[:HDR_LEN + plen]) & 0xFFFFFFFF) != crc:
                self.crc_errors += 1
                self.resync_bytes += 2
                start += 2
                continue
            frames.append((ftype, flags, seq, frame[HDR_LEN:HDR_LEN + plen]))
            start += total
        return frames


def parse_stats(payload):
    if len(payload) < STATS_SIZE:
        return None
    return dict(zip(STATS_FIELDS, struct.unpack_from(STATS_FMT, payload)))


def parse_info(payload):
    if len(payload) < INFO_SIZE:
        return None
    vals = struct.unpack_from(INFO_FMT, payload)
    info = dict(zip(INFO_FIELDS, vals))
    info["chip"] = info["chip"].split(b"\x00", 1)[0].decode("ascii", "replace")
    return info
