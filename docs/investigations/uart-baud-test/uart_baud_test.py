#!/usr/bin/env python3
"""Automated UART baud-rate reliability sweep: Raspberry Pi CM5 <-> XIAO ESP32-S3.

The CM5 is the test controller. The XIAO must be running the companion
firmware in firmware/ (see README.md). For each requested baud rate the tool:

  1. Verifies the CM5 kernel can actually configure the rate (termios2/BOTHER
     set + readback — a clamped readback is reported as UNSUPPORTED, not FAIL).
  2. Negotiates the switch with the XIAO at a safe control baud (115200).
  3. Re-synchronizes at the test rate (PING/PONG with nonce).
  4. Runs data-integrity stress phases: full-duplex echo, CM5->XIAO sink,
     XIAO->CM5 generator — all CRC32-framed, sequence-numbered, byte-verified.
  5. Collects both sides' error counters (including the ESP32's hardware
     framing/overrun error counts) and grades the rate PASS/MARGINAL/FAIL.
  6. Recovers to the safe baud even if the rate was a total loss (the firmware
     auto-reverts on sync/idle watchdogs), then moves to the next rate.

Run with no arguments for the standard suite. See --help and README.md.
"""

import argparse
import csv
import fcntl
import json
import os
import platform
import re
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baudtest_proto as P  # noqa: E402

try:
    import serial
except ImportError:
    serial = None

TOOL_VERSION = "1.0.0"

DEFAULT_RATES = [115200, 230400, 460800, 921600, 1000000, 1500000,
                 2000000, 2500000, 3000000, 3500000, 4000000, 5000000]

# Phase weights when splitting --duration / --bytes across enabled phases.
PHASE_WEIGHTS = {"echo": 3.0, "sink": 1.0, "gen": 1.0, "duplex": 2.0}

# ---------------------------------------------------------------------------
# termios2 (BOTHER) — arbitrary baud rates on Linux
# ---------------------------------------------------------------------------
# Python's termios module only knows the classic Bnnn constants, which stop
# well short of multi-megabaud. Linux supports arbitrary rates through the
# TCGETS2/TCSETS2 ioctls with BOTHER set in c_cflag and the numeric rate in
# c_ispeed/c_ospeed. The serial core writes the *clamped* rate back into the
# termios (uart_get_baud_rate -> tty_termios_encode_baud_rate), so reading
# TCGETS2 after setting reveals whether the driver silently limited us —
# e.g. the CM5's RP1 PL011 (uartclk ~48 MHz) clamps everything above 3 Mbaud.
# These values are correct for asm-generic Linux (aarch64 and armhf).

TCGETS2 = 0x802C542A
TCSETS2 = 0x402C542B
TERMIOS2_FMT = "<4IB19B2I"  # c_iflag c_oflag c_cflag c_lflag c_line c_cc[19] c_ispeed c_ospeed
TERMIOS2_SIZE = struct.calcsize(TERMIOS2_FMT)  # 44
BOTHER = 0o010000
CBAUD = 0o010017
IBSHIFT = 16
CSTOPB = 0o000100

assert TERMIOS2_SIZE == 44, "unexpected termios2 layout on this platform"


def set_custom_baud(fd, baud, stop_bits=1):
    """Set an arbitrary baud rate via termios2. Returns the kernel's readback.

    Raises OSError if the ioctl itself fails. A readback that differs from the
    request means the driver clamped or substituted the rate.
    """
    buf = bytearray(TERMIOS2_SIZE)
    fcntl.ioctl(fd, TCGETS2, buf, True)
    fields = list(struct.unpack(TERMIOS2_FMT, bytes(buf)))
    cflag = fields[2]
    cflag &= ~(CBAUD | (CBAUD << IBSHIFT))
    cflag |= BOTHER
    if stop_bits == 2:
        cflag |= CSTOPB
    else:
        cflag &= ~CSTOPB
    fields[2] = cflag
    fields[-2] = baud  # c_ispeed (input follows output when CIBAUD is clear)
    fields[-1] = baud  # c_ospeed
    fcntl.ioctl(fd, TCSETS2, struct.pack(TERMIOS2_FMT, *fields))

    fcntl.ioctl(fd, TCGETS2, buf, True)
    fields = struct.unpack(TERMIOS2_FMT, bytes(buf))
    return fields[-1]


# ---------------------------------------------------------------------------
# Serial link with framed transactions
# ---------------------------------------------------------------------------

class LinkError(Exception):
    pass


def find_port_holders(port):
    """Other processes with this device open, via /proc/<pid>/fd.

    Two readers on one tty silently steal each other's bytes, which shows up
    as inexplicable corruption or a pyserial "multiple access on port" error
    mid-run — so the sweep refuses to start when the port is shared. Root sees
    every process; an unprivileged run still sees the user's own services.
    """
    holders = []
    if not os.path.isdir("/proc"):
        return holders
    target = os.path.realpath(port)
    me = os.getpid()
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return holders
    for p in pids:
        if int(p) == me:
            continue
        fddir = "/proc/%s/fd" % p
        try:
            fds = os.listdir(fddir)
        except OSError:
            continue  # gone, or not ours to inspect
        for fd in fds:
            try:
                dest = os.readlink(os.path.join(fddir, fd))
            except OSError:
                continue
            if dest == target or dest == port:
                try:
                    with open("/proc/%s/comm" % p) as f:
                        comm = f.read().strip()
                except OSError:
                    comm = "?"
                holders.append((int(p), comm))
                break
    return holders


class Link:
    def __init__(self, port, safe_baud, verbose=False):
        if serial is None:
            raise LinkError("pyserial is required: pip install pyserial "
                            "(or: sudo apt install python3-serial)")
        self.port = port
        self.safe_baud = safe_baud
        self.verbose = verbose
        self.current_baud = None
        self.ser = None
        self.parser = P.FrameParser()
        self._open()

    def _open(self):
        try:
            self.ser = serial.Serial(self.port, baudrate=115200, timeout=0.02,
                                     write_timeout=5.0)
        except serial.SerialException as e:
            raise LinkError(
                "cannot open %s: %s\n"
                "  - is another process holding it? try: fuser -v %s\n"
                "  - is the overlay enabled and are you in the dialout group?"
                % (self.port, e, self.port))
        self.parser.clear()
        self.parser.reset_counts()
        self.set_local_baud(self.safe_baud)

    def reopen(self):
        """Recover from an I/O error by closing and reopening at safe baud."""
        try:
            if self.ser is not None:
                self.ser.close()
        except Exception:
            pass
        time.sleep(0.2)
        self._open()

    def set_local_baud(self, baud, stop_bits=1):
        """Configure the CM5 tty. Returns the kernel readback rate."""
        self.ser.flush()  # drain pending TX at the old rate first
        readback = set_custom_baud(self.ser.fileno(), baud, stop_bits)
        self.current_baud = baud
        return readback

    def flush_input(self):
        self.ser.reset_input_buffer()
        self.parser.clear()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def send(self, frame):
        self.ser.write(frame)

    def read_frames(self, max_bytes=8192):
        data = self.ser.read(max_bytes)
        return self.parser.feed(data)

    def xact(self, frame, expect_type, timeout=0.5, retries=3):
        """Send a command and wait for a specific response type.

        Unexpected frame types (stale echoes etc.) are discarded. Returns the
        (flags, seq, payload) of the first matching response, or None.
        """
        for _ in range(max(1, retries)):
            self.ser.write(frame)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                for (t, f, seq, payload) in self.read_frames():
                    if t == expect_type:
                        return (f, seq, payload)
        return None

    def ping(self, timeout=0.3, retries=5):
        """Nonce-checked ping. Returns the XIAO's actual baud, or None."""
        for attempt in range(retries):
            nonce = struct.pack("<II", 0xB00D7E57, (attempt * 0x01010101) & 0xFFFFFFFF)
            frame = P.build_frame(P.CMD_PING, 0, attempt, nonce)
            resp = self.xact(frame, P.RESP_PONG, timeout=timeout, retries=1)
            if resp is not None:
                _, _, payload = resp
                if payload[:8] == nonce and len(payload) >= 12:
                    return struct.unpack_from("<I", payload, 8)[0]
        return None


# ---------------------------------------------------------------------------
# Stress phases
# ---------------------------------------------------------------------------

class PhaseResult:
    def __init__(self, name):
        self.name = name
        self.completed = False
        self.aborted_reason = None
        self.duration = 0.0
        self.tx_frames = 0
        self.tx_wire_bytes = 0
        self.tx_payload_bytes = 0
        self.rx_frames_ok = 0
        self.rx_payload_bytes_ok = 0
        self.crc_err_frames = 0
        self.resync_bytes = 0
        self.mismatch_frames = 0
        self.mismatch_bytes = 0
        self.timeouts = 0
        self.dup_frames = 0
        self.gap_frames = 0
        self.unexpected_frames = 0
        self.xiao = None  # dict of firmware-side stats for this phase

    def error_events(self):
        n = (self.crc_err_frames + self.mismatch_frames + self.timeouts +
             self.dup_frames + self.gap_frames + self.unexpected_frames)
        if self.resync_bytes:
            n += 1
        if self.xiao:
            n += (self.xiao["rx_crc_err"] + self.xiao["rx_mismatch_frames"] +
                  self.xiao["rx_seq_gap_frames"] + self.xiao["rx_seq_dup_frames"] +
                  self.xiao["hw_frame_err"] + self.xiao["hw_parity_err"] +
                  self.xiao["hw_fifo_ovf"] + self.xiao["hw_buffer_full"] +
                  self.xiao["rx_breaks"])
            if self.xiao["rx_resync_bytes"]:
                n += 1
        return n

    def error_bytes(self, payload_size):
        b = (self.mismatch_bytes + self.resync_bytes +
             (self.crc_err_frames + self.timeouts + self.gap_frames) * payload_size)
        if self.xiao:
            b += (self.xiao["rx_mismatch_bytes"] + self.xiao["rx_resync_bytes"] +
                  (self.xiao["rx_crc_err"] + self.xiao["rx_seq_gap_frames"]) * payload_size)
        return b

    def to_dict(self):
        d = dict(self.__dict__)
        return d


def run_echo_phase(link, res, seconds, byte_budget, payload_size, patterns,
                   window, baud, verbose):
    """Full-duplex windowed echo: CM5 streams DATA frames while a reader
    thread verifies the echoes byte-for-byte. Both directions are loaded
    simultaneously, which is the closest a UART gets to full-duplex stress."""
    frame_overhead = P.HDR_LEN + P.CRC_LEN
    wire_len = payload_size + frame_overhead
    window = max(window, wire_len)  # lockstep mode passes window=1
    # Echo latency bound: time to drain a full window both ways, with margin.
    per_frame_deadline = max(1.0, (window + wire_len) * 10.0 / baud * 4 + 0.25)

    pending = {}  # seq -> (pattern, deadline)
    lock = threading.Lock()
    space = threading.Condition(lock)
    state = {"in_flight": 0, "writer_done": False, "abort": False,
             "consec_timeouts": 0}

    def reader():
        try:
            reader_loop()
        except serial.SerialException as e:
            res.aborted_reason = "serial I/O error while reading: %s" % e
            with lock:
                state["abort"] = True
                space.notify_all()

    def reader_loop():
        while True:
            with lock:
                if state["abort"]:
                    return
                if state["writer_done"] and not pending:
                    return
            for (t, f, seq, payload) in link.read_frames():
                if t != P.RESP_ECHO:
                    res.unexpected_frames += 1
                    continue
                with lock:
                    ent = pending.pop(seq, None)
                    if ent is not None:
                        state["in_flight"] -= wire_len
                        space.notify()
                if ent is None:
                    res.dup_frames += 1
                    continue
                pat, _dl = ent
                expected = P.gen_payload(pat, seq, payload_size)
                if payload == expected:
                    res.rx_frames_ok += 1
                    res.rx_payload_bytes_ok += len(payload)
                    state["consec_timeouts"] = 0
                else:
                    res.mismatch_frames += 1
                    res.mismatch_bytes += P.diff_bytes(expected, payload)
            # Expire lost frames from the head (lowest seq = oldest).
            now = time.monotonic()
            with lock:
                while pending:
                    first = next(iter(pending))
                    if pending[first][1] > now:
                        break
                    del pending[first]
                    state["in_flight"] -= wire_len
                    res.timeouts += 1
                    state["consec_timeouts"] += 1
                    space.notify()
                if state["consec_timeouts"] >= 100 and res.rx_frames_ok == 0:
                    state["abort"] = True
                    space.notify_all()
                    return

    t_reader = threading.Thread(target=reader, daemon=True)
    start = time.monotonic()
    t_reader.start()
    seq = 0
    stop_at = start + seconds
    try:
        while time.monotonic() < stop_at and res.tx_payload_bytes < byte_budget:
            pat = patterns[seq % len(patterns)]
            frame = P.build_frame(P.CMD_ECHO_DATA, pat, seq,
                                  P.gen_payload(pat, seq, payload_size))
            with lock:
                while (state["in_flight"] + wire_len > window
                       and not state["abort"]):
                    space.wait(0.5)
                if state["abort"]:
                    break
                pending[seq] = (pat, time.monotonic() + per_frame_deadline)
                state["in_flight"] += wire_len
            try:
                link.send(frame)
            except serial.SerialTimeoutException:
                with lock:
                    state["abort"] = True
                res.aborted_reason = "write timeout (far side not draining?)"
                break
            except serial.SerialException as e:
                with lock:
                    state["abort"] = True
                res.aborted_reason = "serial I/O error while writing: %s" % e
                break
            res.tx_frames += 1
            res.tx_wire_bytes += len(frame)
            res.tx_payload_bytes += payload_size
            seq += 1
    finally:
        with lock:
            state["writer_done"] = True
        t_reader.join(timeout=per_frame_deadline + 2.0)
        if t_reader.is_alive():
            # Force it out so nothing else races the parser afterwards.
            with lock:
                state["abort"] = True
            t_reader.join(timeout=2.0)
        res.duration = time.monotonic() - start
        res.crc_err_frames += link.parser.crc_errors
        res.resync_bytes += link.parser.resync_bytes
        link.parser.reset_counts()
        if state["abort"] and res.aborted_reason is None:
            res.aborted_reason = "no echoes returning (link dead at this rate)"
        res.completed = res.aborted_reason is None
    return res


def run_sink_phase(link, res, seconds, byte_budget, payload_size, patterns,
                   verbose):
    """CM5 -> XIAO one-way flood. The XIAO regenerates the expected pattern
    from (pattern id, seq) and verifies; its stats tell us what arrived."""
    start = time.monotonic()
    stop_at = start + seconds
    seq = 0
    try:
        while time.monotonic() < stop_at and res.tx_payload_bytes < byte_budget:
            pat = patterns[seq % len(patterns)]
            frame = P.build_frame(P.CMD_SINK_DATA, pat, seq,
                                  P.gen_payload(pat, seq, payload_size))
            try:
                link.send(frame)
            except serial.SerialTimeoutException:
                res.aborted_reason = "write timeout"
                break
            except serial.SerialException as e:
                res.aborted_reason = "serial I/O error while writing: %s" % e
                break
            res.tx_frames += 1
            res.tx_wire_bytes += len(frame)
            res.tx_payload_bytes += payload_size
            seq += 1
    finally:
        res.duration = time.monotonic() - start
        res.completed = res.aborted_reason is None
    return res


def run_gen_phase(link, res, seconds, byte_budget, payload_size, patterns,
                  verbose):
    """XIAO -> CM5 one-way flood. The firmware streams GEN_DATA frames at
    line rate; we verify seq continuity and payload content."""
    pattern_arg = patterns[0] if len(patterns) == 1 else P.PAT_CYCLE
    duration_ms = int(seconds * 1000)
    max_frames = 0
    if byte_budget < float("inf"):
        max_frames = max(1, int(byte_budget // payload_size))
    req = struct.pack(P.GEN_START_FMT, duration_ms, max_frames,
                      payload_size, pattern_arg, 0)
    start = time.monotonic()
    link.send(P.build_frame(P.CMD_GEN_START, 0, 0, req))
    expected_seq = 0
    done = None
    # Allow generous drain time beyond the nominal duration.
    hard_deadline = start + seconds + 10.0
    last_progress = start
    while time.monotonic() < hard_deadline:
        try:
            frames = link.read_frames()
        except serial.SerialException as e:
            res.duration = time.monotonic() - start
            res.aborted_reason = "serial I/O error while reading: %s" % e
            res.completed = False
            return res
        if frames:
            last_progress = time.monotonic()
        for (t, f, seq, payload) in frames:
            if t == P.RESP_GEN_DATA:
                if seq == expected_seq:
                    expected_seq += 1
                elif seq > expected_seq:
                    res.gap_frames += seq - expected_seq
                    expected_seq = seq + 1
                else:
                    res.dup_frames += 1
                    continue
                if pattern_arg == P.PAT_CYCLE:
                    pat = seq % P.PATTERN_COUNT
                else:
                    pat = pattern_arg
                expected = P.gen_payload(pat, seq, payload_size)
                if payload == expected:
                    res.rx_frames_ok += 1
                    res.rx_payload_bytes_ok += len(payload)
                else:
                    res.mismatch_frames += 1
                    res.mismatch_bytes += P.diff_bytes(expected, payload)
            elif t == P.RESP_GEN_DONE:
                done = struct.unpack_from(P.GEN_DONE_FMT, payload)
            else:
                res.unexpected_frames += 1
        if done is not None:
            break
        if time.monotonic() - last_progress > 3.0:
            break  # stream died
    res.duration = time.monotonic() - start
    res.crc_err_frames += link.parser.crc_errors
    res.resync_bytes += link.parser.resync_bytes
    link.parser.reset_counts()
    if done is not None:
        frames_sent = done[0]
        # Frames the firmware sent but we never saw (tail loss).
        if frames_sent > expected_seq:
            res.gap_frames += frames_sent - expected_seq
        res.tx_frames = frames_sent          # firmware-side TX, for reference
        res.tx_wire_bytes = done[1]
        if done[2]:
            res.aborted_reason = "firmware aborted generation (TX stalled)"
        res.completed = res.aborted_reason is None
    else:
        res.aborted_reason = "GEN_DONE never received"
        # Send ABORT in case the firmware is still generating.
        try:
            link.send(P.build_frame(P.CMD_ABORT, 0, 0))
        except Exception:
            pass
    return res


def read_pl011_counts(port):
    """Kernel-side PL011 counters for this port (tx/rx bytes, fe/pe/brk/oe
    error counts) from /proc/tty/driver/ttyAMA. Needs root; returns None if
    unreadable or the port isn't a ttyAMA device. Note the error fields count
    line-level events even during baud switches, so they are reported but not
    folded into verdicts."""
    m = re.match(r".*/ttyAMA(\d+)$", port)
    if not m:
        return None
    idx = m.group(1) + ":"
    try:
        with open("/proc/tty/driver/ttyAMA") as f:
            for line in f:
                line = line.strip()
                if line.startswith(idx):
                    d = {}
                    for k in ("tx", "rx", "fe", "pe", "brk", "oe"):
                        mm = re.search(r"\b%s:(\d+)" % k, line)
                        d[k] = int(mm.group(1)) if mm else 0
                    return d
    except OSError:
        return None
    return None


def run_duplex_phase(link, res, seconds, byte_budget, payload_size, patterns,
                     baud, tx_util, verbose):
    """Simultaneous independent floods in both directions — the production
    traffic shape (e.g. bulk stream up, commands down), with no echo coupling.
    The XIAO streams GEN_DATA at line rate while the CM5 floods SINK frames;
    each direction is verified independently, so errors attribute cleanly:
    XIAO stats = CM5->XIAO leg, CM5-side counters = XIAO->CM5 leg."""
    pattern_arg = patterns[0] if len(patterns) == 1 else P.PAT_CYCLE
    duration_ms = int(seconds * 1000)
    max_frames = 0
    if byte_budget < float("inf"):
        max_frames = max(1, int(byte_budget // payload_size))
    req = struct.pack(P.GEN_START_FMT, duration_ms, max_frames,
                      payload_size, pattern_arg, 0)
    start = time.monotonic()
    state = {"done": None, "abort": False}

    def reader():
        try:
            reader_loop()
        except serial.SerialException as e:
            res.aborted_reason = "serial I/O error while reading: %s" % e
            state["abort"] = True

    def reader_loop():
        expected_seq = 0
        hard_deadline = start + seconds + 10.0
        last_progress = start
        while time.monotonic() < hard_deadline and not state["abort"]:
            frames = link.read_frames()
            if frames:
                last_progress = time.monotonic()
            for (t, f, seq, payload) in frames:
                if t == P.RESP_GEN_DATA:
                    if seq == expected_seq:
                        expected_seq += 1
                    elif seq > expected_seq:
                        res.gap_frames += seq - expected_seq
                        expected_seq = seq + 1
                    else:
                        res.dup_frames += 1
                        continue
                    pat = seq % P.PATTERN_COUNT if pattern_arg == P.PAT_CYCLE \
                        else pattern_arg
                    expected = P.gen_payload(pat, seq, payload_size)
                    if payload == expected:
                        res.rx_frames_ok += 1
                        res.rx_payload_bytes_ok += len(payload)
                    else:
                        res.mismatch_frames += 1
                        res.mismatch_bytes += P.diff_bytes(expected, payload)
                elif t == P.RESP_GEN_DONE:
                    state["done"] = struct.unpack_from(P.GEN_DONE_FMT, payload)
                    return
                else:
                    res.unexpected_frames += 1
            if time.monotonic() - last_progress > 3.0:
                return  # stream died

    link.send(P.build_frame(P.CMD_GEN_START, 0, 0, req))
    t_reader = threading.Thread(target=reader, daemon=True)
    t_reader.start()
    seq = 0
    stop_at = start + seconds
    # tx_util < 1.0 paces the CM5->XIAO flood below line rate — e.g. 0.1
    # approximates sparse command traffic riding under a bulk return stream.
    bps = (baud / 10.0) * max(0.001, min(1.0, tx_util))
    next_send = time.monotonic()
    try:
        while time.monotonic() < stop_at and res.tx_payload_bytes < byte_budget:
            pat = patterns[seq % len(patterns)]
            frame = P.build_frame(P.CMD_SINK_DATA, pat, seq,
                                  P.gen_payload(pat, seq, payload_size))
            if tx_util < 1.0:
                now = time.monotonic()
                if now > next_send + 1.0:
                    next_send = now  # don't burst to catch up after a stall
                delay = next_send - now
                if delay > 0:
                    time.sleep(delay)
                next_send += len(frame) / bps
            try:
                link.send(frame)
            except serial.SerialTimeoutException:
                res.aborted_reason = "write timeout"
                state["abort"] = True
                break
            except serial.SerialException as e:
                res.aborted_reason = "serial I/O error while writing: %s" % e
                state["abort"] = True
                break
            res.tx_frames += 1
            res.tx_wire_bytes += len(frame)
            res.tx_payload_bytes += payload_size
            seq += 1
    finally:
        # The reader self-terminates on GEN_DONE, 3 s of no progress, or its
        # own hard deadline — so wait it out rather than cutting it short.
        # Matters for byte-budgeted runs, where the CM5 writer can finish well
        # before the XIAO's generator does.
        t_reader.join(timeout=seconds + 15.0)
        if t_reader.is_alive():
            state["abort"] = True
            t_reader.join(timeout=2.0)
        res.duration = time.monotonic() - start
        res.crc_err_frames += link.parser.crc_errors
        res.resync_bytes += link.parser.resync_bytes
        link.parser.reset_counts()
        done = state["done"]
        if done is None:
            res.aborted_reason = res.aborted_reason or "GEN_DONE never received"
            try:
                link.send(P.build_frame(P.CMD_ABORT, 0, 0))
            except Exception:
                pass
        elif done[2]:
            res.aborted_reason = "firmware aborted generation (TX stalled)"
        res.completed = res.aborted_reason is None
        time.sleep(0.3)  # let the firmware finish parsing the sink tail
    return res


# ---------------------------------------------------------------------------
# Per-rate orchestration
# ---------------------------------------------------------------------------

class RateResult:
    def __init__(self, baud):
        self.baud = baud
        self.verdict = "NOT_RUN"
        self.reason = ""
        self.kernel_readback = None
        self.esp_actual_baud = None
        self.duration = 0.0
        self.phases = {}
        self.error_events = 0
        self.error_bytes = 0
        self.total_payload_bytes = 0
        self.byte_error_rate = None
        self.throughput_bps = 0.0
        self.tx_bytes = 0
        self.rx_bytes = 0
        self.timeouts = 0
        self.recovered = True
        self.started_at = None
        self.ended_at = None
        self.pl011_delta = None  # CM5-side kernel counters, when readable


def get_stats(link, timeout=3.0):
    resp = link.xact(P.build_frame(P.CMD_GET_STATS, 0, 0), P.RESP_STATS,
                     timeout=timeout, retries=2)
    if resp is None:
        return None
    return P.parse_stats(resp[2])


def clear_stats(link, timeout=1.0):
    resp = link.xact(P.build_frame(P.CMD_CLEAR_STATS, 0, 0), P.RESP_ACK,
                     timeout=timeout, retries=3)
    return resp is not None


def resync_at_safe(link, args, patient=True):
    """Drop to the safe baud and wait for the firmware watchdog to bring the
    XIAO back. Firmware reverts after SYNC_TIMEOUT_MS (never synced) or
    IDLE_REVERT_MS (synced then silent), so be patient."""
    link.set_local_baud(args.safe_baud)
    link.flush_input()
    deadline = time.monotonic() + ((P.IDLE_REVERT_MS / 1000.0 + 6.0) if patient else 4.0)
    while time.monotonic() < deadline:
        if link.ping(timeout=0.3, retries=1) is not None:
            return True
        time.sleep(0.1)
    return False


def negotiate_baud(link, baud, args):
    """SET_BAUD handshake at the current (safe) baud, then switch and sync.

    Returns (ok, esp_actual_baud, failure_reason, xiao_rejected)."""
    payload = struct.pack(P.SET_BAUD_FMT, baud, args.stop_bits)
    resp = link.xact(P.build_frame(P.CMD_SET_BAUD, 0, 0, payload),
                     P.RESP_ACK, timeout=0.5, retries=2)
    if resp is None:
        # ACK lost — the XIAO may or may not have switched. Try the new rate;
        # the recovery path handles the mismatch case.
        switch_delay = P.SWITCH_DELAY_MS / 1000.0
    else:
        status, detail, delay_ms = struct.unpack(P.ACK_FMT, resp[2][:12])
        if status != P.ACK_OK:
            return (False, None,
                    "XIAO rejected rate (its UART max is %d baud)" % detail, True)
        switch_delay = delay_ms / 1000.0
    time.sleep(switch_delay + 0.04)
    stop_bits = args.stop_bits if baud != args.safe_baud else 1
    link.set_local_baud(baud, stop_bits)
    link.flush_input()
    time.sleep(args.settle)
    actual = link.ping(timeout=0.25, retries=8)
    if actual is None:
        return (False, None, "no sync at %d baud" % baud, False)
    return (True, actual, None, False)


def test_rate(link, baud, args, log):
    r = RateResult(baud)
    r.started_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    t0 = time.monotonic()
    pl011_before = read_pl011_counts(args.port)

    # --- Preflight: can the CM5 kernel actually produce this rate? ---------
    try:
        readback = link.set_local_baud(baud, args.stop_bits)
    except OSError as e:
        link.set_local_baud(args.safe_baud)
        r.verdict = "UNSUPPORTED"
        r.reason = "termios2 set failed: %s" % e
        log("preflight: %s" % r.reason)
        return r
    r.kernel_readback = readback
    link.set_local_baud(args.safe_baud)
    link.flush_input()
    if readback and abs(readback - baud) / float(baud) > 0.02:
        r.reason = "kernel clamped to %d" % readback
        if not args.ignore_preflight:
            r.verdict = "UNSUPPORTED"
            log("preflight: requested %d, kernel configured %d -> UNSUPPORTED"
                % (baud, readback))
            return r
        log("preflight: kernel clamped to %d (continuing: --ignore-preflight)"
            % readback)
    else:
        log("preflight ok (kernel accepts %d)" % baud)

    # --- Make sure we're talking at the safe baud, then negotiate ----------
    if link.ping(timeout=0.3, retries=3) is None:
        if not resync_at_safe(link, args):
            r.verdict = "FAIL"
            r.reason = "lost contact at safe baud before test"
            r.recovered = False
            return r
    clear_stats(link)
    ok, actual, why, rejected = negotiate_baud(link, baud, args)
    if not ok:
        if rejected:
            r.verdict = "UNSUPPORTED"
            r.reason = why
            log(why)
            return r
        r.verdict = "FAIL"
        r.reason = why + " (link configured but no valid frames)"
        log("sync FAILED at %d baud" % baud)
        r.recovered = resync_at_safe(link, args)
        r.duration = time.monotonic() - t0
        return r
    r.esp_actual_baud = actual
    dev = abs(actual - baud) / float(baud) if actual else 0.0
    log("switch+sync ok (XIAO actual %d baud%s)"
        % (actual, ", dev %.2f%%" % (dev * 100) if dev > 0.001 else ""))

    # --- Stress phases ------------------------------------------------------
    weights = {ph: PHASE_WEIGHTS[ph] for ph in args.phases}
    wsum = sum(weights.values())
    total_seconds = args.duration
    total_bytes = args.bytes if args.bytes else float("inf")
    link_dead = False
    for ph in args.phases:
        share = weights[ph] / wsum
        seconds = total_seconds * share
        byte_budget = total_bytes * share if total_bytes != float("inf") else float("inf")
        if byte_budget != float("inf"):
            # When a byte budget drives the test, cap time generously instead.
            seconds = byte_budget * 10.0 / baud * 6 + 20.0
        pr = PhaseResult(ph)
        clear_stats(link)
        # Discard any late sync/ack stragglers so they can't be miscounted
        # as phase errors.
        time.sleep(0.05)
        link.flush_input()
        link.parser.reset_counts()
        if ph == "echo":
            if args.lockstep:
                window = args.payload_size + P.HDR_LEN + P.CRC_LEN
            else:
                window = args.window
            run_echo_phase(link, pr, seconds, byte_budget, args.payload_size,
                           args.patterns, window, baud, args.verbose)
        elif ph == "sink":
            run_sink_phase(link, pr, seconds, byte_budget, args.payload_size,
                           args.patterns, args.verbose)
            time.sleep(0.3)  # let the firmware finish parsing the tail
        elif ph == "gen":
            run_gen_phase(link, pr, seconds, byte_budget, args.payload_size,
                          args.patterns, args.verbose)
        elif ph == "duplex":
            run_duplex_phase(link, pr, seconds, byte_budget, args.payload_size,
                             args.patterns, baud, args.duplex_tx_util,
                             args.verbose)
        pr.xiao = get_stats(link)
        if pr.xiao is None:
            pr.aborted_reason = (pr.aborted_reason or "") + "; XIAO stats unavailable"
            pr.completed = False
        r.phases[ph] = pr
        thr = ((pr.tx_payload_bytes if ph != "gen" else 0) +
               pr.rx_payload_bytes_ok) / pr.duration if pr.duration else 0
        log("%-4s %5.1fs: %d tx / %d ok-rx frames, %d err events, %s"
            % (ph, pr.duration, pr.tx_frames, pr.rx_frames_ok,
               pr.error_events(), fmt_rate(thr)))
        if pr.error_events():
            x = pr.xiao or {}
            log("     CM5->XIAO: crc=%d resync=%dB mism=%dB gaps=%d dups=%d "
                "hw(fe=%d pe=%d ovf=%d full=%d brk=%d) | XIAO->CM5: crc=%d "
                "resync=%dB mism=%dB gaps=%d dups=%d lost=%d"
                % (x.get("rx_crc_err", -1), x.get("rx_resync_bytes", -1),
                   x.get("rx_mismatch_bytes", -1), x.get("rx_seq_gap_frames", -1),
                   x.get("rx_seq_dup_frames", -1), x.get("hw_frame_err", -1),
                   x.get("hw_parity_err", -1), x.get("hw_fifo_ovf", -1),
                   x.get("hw_buffer_full", -1), x.get("rx_breaks", -1),
                   pr.crc_err_frames, pr.resync_bytes, pr.mismatch_bytes,
                   pr.gap_frames, pr.dup_frames, pr.timeouts))
        if not pr.completed and pr.xiao is None:
            link_dead = True
            break  # no point running further phases on a dead link

    # --- Return to safe baud ------------------------------------------------
    if not link_dead:
        ok, _, _, _ = negotiate_baud(link, args.safe_baud, args)
        r.recovered = ok or resync_at_safe(link, args)
    else:
        r.recovered = resync_at_safe(link, args)
    if not r.recovered:
        log("WARNING: could not re-establish safe-baud contact")

    # --- CM5 kernel-side hardware counters ----------------------------------
    pl011_after = read_pl011_counts(args.port)
    if pl011_before and pl011_after:
        r.pl011_delta = {k: pl011_after[k] - pl011_before[k] for k in pl011_after}
        hw = {k: v for k, v in r.pl011_delta.items()
              if k in ("fe", "pe", "brk", "oe") and v}
        if hw:
            log("CM5 PL011 hw this rate: %s (incl. baud-switch glitches; "
                "not graded)" % " ".join("%s+%d" % kv for kv in sorted(hw.items())))

    # --- Grade --------------------------------------------------------------
    r.duration = time.monotonic() - t0
    for pr in r.phases.values():
        r.error_events += pr.error_events()
        r.error_bytes += pr.error_bytes(args.payload_size)
        r.timeouts += pr.timeouts
        r.tx_bytes += pr.tx_wire_bytes
        if pr.xiao:
            r.rx_bytes += pr.xiao["tx_bytes"]  # what the XIAO put on the wire toward us
        expected_rx = pr.rx_payload_bytes_ok + pr.mismatch_bytes
        r.total_payload_bytes += pr.tx_payload_bytes + expected_rx
    stress_time = sum(pr.duration for pr in r.phases.values())
    verified = sum(pr.rx_payload_bytes_ok for pr in r.phases.values())
    verified += sum(pr.xiao["rx_frames_ok"] * args.payload_size
                    for pr in r.phases.values()
                    if pr.name in ("sink", "duplex") and pr.xiao)
    r.throughput_bps = verified / stress_time if stress_time else 0.0
    if r.total_payload_bytes:
        r.byte_error_rate = min(1.0, r.error_bytes / float(r.total_payload_bytes))
    incomplete = any(not pr.completed for pr in r.phases.values())
    if incomplete or not r.phases:
        r.verdict = "FAIL"
        r.reason = r.reason or "; ".join(
            "%s: %s" % (pr.name, pr.aborted_reason)
            for pr in r.phases.values() if pr.aborted_reason) or "no phases ran"
    elif r.error_events == 0:
        r.verdict = "PASS"
    elif r.byte_error_rate is not None and r.byte_error_rate < args.marginal_threshold:
        r.verdict = "MARGINAL"
        r.reason = "%d error events, byte error rate %.2e" % (
            r.error_events, r.byte_error_rate)
    else:
        r.verdict = "FAIL"
        r.reason = "%d error events, byte error rate %s" % (
            r.error_events,
            "%.2e" % r.byte_error_rate if r.byte_error_rate is not None else "n/a")
    r.ended_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    return r


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def fmt_rate(bps):
    if bps >= 1e6:
        return "%.2f MB/s" % (bps / 1e6)
    if bps >= 1e3:
        return "%.1f KB/s" % (bps / 1e3)
    return "%d B/s" % bps


def fmt_int(n):
    return "{:,}".format(n)


def print_table(results):
    hdr = ("Baud", "Result", "Duration", "TX Bytes", "RX Bytes", "Errors",
           "Timeouts", "Throughput", "Notes")
    rows = [hdr]
    for r in results:
        if r.verdict in ("UNSUPPORTED", "NOT_RUN", "ERROR"):
            rows.append((str(r.baud), r.verdict, "-", "-", "-", "-", "-", "-",
                         r.reason))
            continue
        rows.append((str(r.baud), r.verdict, "%.1f s" % r.duration,
                     fmt_int(r.tx_bytes), fmt_int(r.rx_bytes),
                     fmt_int(r.error_events), fmt_int(r.timeouts),
                     fmt_rate(r.throughput_bps), r.reason))
    widths = [max(len(row[i]) for row in rows) for i in range(len(hdr))]
    print()
    for i, row in enumerate(rows):
        print("  ".join(cell.ljust(widths[j]) for j, cell in enumerate(row)).rstrip())
        if i == 0:
            print("  ".join("-" * widths[j] for j in range(len(hdr))))
    passing = [r.baud for r in results if r.verdict == "PASS"]
    print()
    if passing:
        print("Highest PASS rate: %d baud" % max(passing))
    else:
        print("No rate passed cleanly.")


def write_json(path, args, results, xiao_info, started, ended):
    doc = {
        "tool": {"name": "uart_baud_test", "version": TOOL_VERSION,
                 "proto_version": P.PROTO_VERSION},
        "started_at": started,
        "ended_at": ended,
        "host": {"platform": platform.platform(), "node": platform.node(),
                 "machine": platform.machine()},
        "config": {
            "port": args.port, "safe_baud": args.safe_baud,
            "rates": args.rates, "duration_per_rate_s": args.duration,
            "byte_budget": args.bytes, "payload_size": args.payload_size,
            "window": args.window, "stop_bits": args.stop_bits,
            "phases": args.phases, "lockstep": args.lockstep,
            "patterns": [P.PATTERN_NAMES[p] for p in args.patterns],
            "marginal_threshold": args.marginal_threshold,
            "quick": args.quick,
        },
        "xiao_info": xiao_info,
        "results": [],
        "summary": {},
    }
    for r in results:
        d = dict(r.__dict__)
        d["phases"] = {name: pr.to_dict() for name, pr in r.phases.items()}
        doc["results"].append(d)
    passing = [r.baud for r in results if r.verdict == "PASS"]
    doc["summary"] = {
        "highest_pass_baud": max(passing) if passing else None,
        "pass": passing,
        "marginal": [r.baud for r in results if r.verdict == "MARGINAL"],
        "fail": [r.baud for r in results if r.verdict == "FAIL"],
        "unsupported": [r.baud for r in results if r.verdict == "UNSUPPORTED"],
        "error": [r.baud for r in results if r.verdict == "ERROR"],
    }
    with open(path, "w") as f:
        json.dump(doc, f, indent=2, default=str)
    print("JSON results written to %s" % path)


def write_csv(path, results):
    cols = ["baud", "verdict", "reason", "kernel_readback", "esp_actual_baud",
            "duration", "tx_bytes", "rx_bytes", "error_events", "error_bytes",
            "byte_error_rate", "timeouts", "throughput_bps", "recovered",
            "started_at", "ended_at"]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in results:
            w.writerow([getattr(r, c) for c in cols])
    print("CSV results written to %s" % path)


# ---------------------------------------------------------------------------
# Self-test (no hardware): codec + pattern round-trips
# ---------------------------------------------------------------------------

def self_test():
    import random
    rnd = random.Random(1234)
    frames_in = []
    stream = bytearray()
    for i in range(500):
        pat = i % P.PATTERN_COUNT
        payload = P.gen_payload(pat, i, rnd.randrange(0, 300))
        fr = P.build_frame(P.CMD_ECHO_DATA, pat, i, payload)
        frames_in.append((P.CMD_ECHO_DATA, pat, i, payload))
        if rnd.random() < 0.1:
            stream += bytes(rnd.randrange(256) for _ in range(rnd.randrange(1, 9)))
        stream += fr
    parser = P.FrameParser()
    got = []
    pos = 0
    while pos < len(stream):
        n = rnd.randrange(1, 700)
        got += parser.feed(stream[pos:pos + n])
        pos += n
    got += parser.feed(b"")
    ok = [g for g in got if g in frames_in]
    assert len(ok) >= len(frames_in) - 2, \
        "parser lost frames: %d/%d" % (len(ok), len(frames_in))
    # Corruption must be detected, never silently accepted.
    fr = bytearray(P.build_frame(P.CMD_ECHO_DATA, P.PAT_PRNG, 42,
                                 P.gen_payload(P.PAT_PRNG, 42, 256)))
    fr[40] ^= 0x01
    p2 = P.FrameParser()
    assert p2.feed(bytes(fr)) == [] and p2.crc_errors == 1
    # Pattern determinism.
    assert P.gen_payload(P.PAT_PRNG, 7, 64) == P.gen_payload(P.PAT_PRNG, 7, 64)
    assert P.gen_payload(P.PAT_PRNG, 7, 64) != P.gen_payload(P.PAT_PRNG, 8, 64)
    assert P.gen_payload(P.PAT_INC, 3, 8) == bytes([3, 4, 5, 6, 7, 8, 9, 10])
    assert P.gen_payload(P.PAT_ALT, 0, 4) == b"\x55\xAA\x55\xAA"
    assert struct.calcsize(P.STATS_FMT) == 72
    assert struct.calcsize(P.INFO_FMT) == 48
    print("self-test OK (codec, parser resync, CRC detection, patterns)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv):
    ap = argparse.ArgumentParser(
        description="CM5 <-> XIAO UART baud-rate reliability sweep",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyAMA2",
                    help="CM5 serial device (carrier default: uart2-pi5 overlay)")
    ap.add_argument("--baud-rates", default=None,
                    help="comma-separated list; default is the standard suite")
    ap.add_argument("--duration", type=float, default=10.0,
                    help="stress seconds per baud rate (split across phases)")
    ap.add_argument("--bytes", type=int, default=None,
                    help="payload byte budget per rate (overrides --duration)")
    ap.add_argument("--quick", action="store_true",
                    help="short development run: echo phase only, 2 s per rate")
    ap.add_argument("--phases", default="echo,sink,gen,duplex",
                    help="comma list from: echo (full-duplex echo), sink "
                         "(CM5->XIAO), gen (XIAO->CM5), duplex (simultaneous "
                         "independent floods both ways — production shape)")
    ap.add_argument("--payload-size", type=int, default=1024,
                    help="payload bytes per frame (64..%d)" % P.MAX_PAYLOAD)
    ap.add_argument("--window", type=int, default=8192,
                    help="max un-acked bytes in flight during echo phase")
    ap.add_argument("--safe-baud", type=int, default=115200,
                    help="control-channel baud used between tests")
    ap.add_argument("--stop-bits", type=int, choices=[1, 2], default=1,
                    help="stop bits at the test rate (safe baud is always 8N1)")
    ap.add_argument("--patterns", default="inc,prng,alt,zero,ones",
                    help="comma list of test patterns to cycle through")
    ap.add_argument("--output", default=None, help="JSON results path "
                    "(default: uart_baud_results_<timestamp>.json)")
    ap.add_argument("--csv", default=None, help="also write CSV results here")
    ap.add_argument("--full-duplex", action="store_true", default=True,
                    help=argparse.SUPPRESS)  # default behavior; kept for CLI compat
    ap.add_argument("--lockstep", action="store_true",
                    help="disable windowing: one frame in flight at a time")
    ap.add_argument("--duplex-tx-util", type=float, default=1.0,
                    help="duplex phase: CM5->XIAO flood rate as a fraction of "
                         "line rate (0.1 ~ sparse command traffic under a "
                         "bulk return stream)")
    ap.add_argument("--marginal-threshold", type=float, default=1e-4,
                    help="byte error rate below which a rate grades MARGINAL "
                         "instead of FAIL")
    ap.add_argument("--settle", type=float, default=0.15,
                    help="seconds to wait after each baud switch")
    ap.add_argument("--allow-shared-port", action="store_true",
                    help="run even if another process has the port open "
                         "(results will not be trustworthy)")
    ap.add_argument("--ignore-preflight", action="store_true",
                    help="attempt sync even if the kernel readback mismatches")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true",
                    help="run protocol/codec unit checks without hardware")
    args = ap.parse_args(argv)

    if args.baud_rates:
        args.rates = [int(x) for x in args.baud_rates.replace(" ", "").split(",") if x]
    else:
        args.rates = list(DEFAULT_RATES)
    if args.quick:
        args.phases = "echo"
        if args.duration == 10.0:
            args.duration = 2.0
    args.phases = [p.strip() for p in args.phases.split(",") if p.strip()]
    for p in args.phases:
        if p not in PHASE_WEIGHTS:
            ap.error("unknown phase %r" % p)
    args.patterns = [P.NAME_TO_PATTERN[n.strip()]
                     for n in args.patterns.split(",") if n.strip()]
    if not (64 <= args.payload_size <= P.MAX_PAYLOAD):
        ap.error("--payload-size must be 64..%d" % P.MAX_PAYLOAD)
    return args


def main(argv=None):
    args = parse_args(argv)
    if args.self_test:
        self_test()
        return 0

    started = time.strftime("%Y-%m-%dT%H:%M:%S")
    out_path = args.output or ("uart_baud_results_%s.json"
                               % time.strftime("%Y%m%d_%H%M%S"))
    print("UART baud-rate sweep  (%s, safe baud %d, %s)"
          % (args.port, args.safe_baud,
             "quick" if args.quick else "%.0fs/rate" % args.duration))

    holders = find_port_holders(args.port)
    if holders and not args.allow_shared_port:
        print("FATAL: another process already has %s open:" % args.port,
              file=sys.stderr)
        for pid, comm in holders:
            print("         %s (pid %d)" % (comm, pid), file=sys.stderr)
        print("  Two readers on one tty steal each other's bytes, which shows "
              "up as\n  phantom corruption or a mid-run 'multiple access on "
              "port' error.\n"
              "  Stop the holder first, e.g.:\n"
              "    systemctl --user stop hw1-ai-service.service\n"
              "  then confirm with:  fuser -v %s\n"
              "  (--allow-shared-port overrides, but results will not be "
              "trustworthy.)" % args.port, file=sys.stderr)
        return 1

    try:
        link = Link(args.port, args.safe_baud, args.verbose)
    except LinkError as e:
        print("FATAL: %s" % e, file=sys.stderr)
        return 1

    results = []
    xiao_info = None
    try:
        # Initial contact + info/version handshake at the safe baud.
        if link.ping(timeout=0.4, retries=6) is None and \
                not resync_at_safe(link, args):
            print("FATAL: no response from XIAO at %d baud on %s.\n"
                  "  - is the baud-test firmware flashed and powered?\n"
                  "  - correct port/pins/wiring? (see README troubleshooting)"
                  % (args.safe_baud, args.port), file=sys.stderr)
            return 1
        resp = link.xact(P.build_frame(P.CMD_GET_INFO, 0, 0), P.RESP_INFO,
                         timeout=0.5, retries=3)
        if resp:
            xiao_info = P.parse_info(resp[2])
        if xiao_info is None:
            print("FATAL: XIAO answered ping but not GET_INFO — wrong or stale "
                  "firmware on the XIAO?", file=sys.stderr)
            return 1
        if xiao_info["proto_version"] != P.PROTO_VERSION:
            print("FATAL: protocol version mismatch (CM5 %d, XIAO %d) — "
                  "reflash the test firmware."
                  % (P.PROTO_VERSION, xiao_info["proto_version"]), file=sys.stderr)
            return 1
        print("XIAO: %s, UART%d TX=%d RX=%d, rings rx/tx %d/%d, UART max %d baud"
              % (xiao_info["chip"], xiao_info["uart_num"], xiao_info["tx_pin"],
                 xiao_info["rx_pin"], xiao_info["rx_ring"], xiao_info["tx_ring"],
                 xiao_info["soc_max_baud"]))

        for baud in args.rates:
            def log(msg, _b=baud):
                print("[%8d] %s" % (_b, msg))
            print()
            try:
                r = test_rate(link, baud, args, log)
            except serial.SerialException as e:
                # An I/O error on the tty (port stolen, driver hiccup) must not
                # end the sweep: record it, reopen, keep testing the next rate.
                r = RateResult(baud)
                r.verdict = "ERROR"
                r.reason = "serial I/O error: %s" % e
                log(r.reason)
                holders = find_port_holders(args.port)
                if holders:
                    log("another process now holds %s: %s"
                        % (args.port, ", ".join("%s(%d)" % (c, p)
                                                for p, c in holders)))
                try:
                    link.reopen()
                    r.recovered = resync_at_safe(link, args)
                except LinkError as e2:
                    log("cannot reopen port: %s" % e2)
                    r.recovered = False
            results.append(r)
            log("verdict: %s%s" % (r.verdict, "  (%s)" % r.reason if r.reason else ""))
            if not r.recovered:
                log("aborting sweep: safe-baud contact lost (power-cycle the XIAO)")
                break
    except KeyboardInterrupt:
        print("\ninterrupted — attempting to restore safe baud...")
        try:
            resync_at_safe(link, args)
        except Exception:
            pass
    finally:
        ended = time.strftime("%Y-%m-%dT%H:%M:%S")
        if results:
            print_table(results)
            write_json(out_path, args, results, xiao_info, started, ended)
            if args.csv:
                write_csv(args.csv, results)
        link.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
