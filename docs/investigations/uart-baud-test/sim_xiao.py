#!/usr/bin/env python3
"""Software stand-in for the XIAO test firmware, for developing the CM5 tool
without hardware. Speaks the full baud-test protocol over a pty.

Two ways to use it:

  python3 sim_xiao.py
      Prints a pty path and serves forever. On Linux you can point the real
      CLI at it: python3 uart_baud_test.py --port <that path>
      (a pty accepts and stores arbitrary termios2 rates, so even the
      preflight readback works there).

  python3 sim_xiao.py --run-suite [uart_baud_test args...]
      Runs the whole controller in-process against the simulator, with the
      termios2 baud-setting stubbed out (works on macOS too, where the
      Linux-only TCGETS2 ioctl doesn't exist). Used as an end-to-end check
      of negotiation, phases, accounting, and reporting.

The simulator mirrors the firmware's logic (echo, sink verification, paced
generation, stats, baud negotiation incl. rejecting rates above its "SoC max")
but not, of course, real electrical behavior — every configurable rate passes.
"""

import os
import pty
import select
import struct
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import baudtest_proto as P  # noqa: E402

SIM_SOC_MAX_BAUD = 5000000


class SimXiao(threading.Thread):
    def __init__(self, master_fd, deaf_above=None):
        super().__init__(daemon=True)
        self.fd = master_fd
        self.parser = P.FrameParser()
        self.cur_baud = 115200
        self.stop_bits = 1
        self.stats = dict.fromkeys(P.STATS_FIELDS, 0)
        self.expect_seq = 0
        self.gen_req = None
        self.abort = False
        # Failure injection: above this baud the sim "hears" nothing, like a
        # rate the receiver can't lock — exercises watchdog-revert recovery.
        self.deaf_above = deaf_above
        self.deaf_deadline = None

    def send(self, ftype, flags, seq, payload=b""):
        frame = P.build_frame(ftype, flags, seq, payload)
        os.write(self.fd, frame)
        self.stats["tx_frames"] += 1
        self.stats["tx_bytes"] += len(frame)

    def run(self):
        while True:
            try:
                data = os.read(self.fd, 4096)
            except OSError:
                return
            if not data:
                return
            if self.deaf_deadline is not None:
                if time.monotonic() > self.deaf_deadline:
                    # Firmware sync-watchdog behavior: revert to safe baud.
                    self.cur_baud = 115200
                    self.deaf_deadline = None
                else:
                    self.stats["rx_bytes"] += len(data)
                    continue  # deaf: drop everything at this rate
            self.stats["rx_bytes"] += len(data)
            for (t, f, seq, payload) in self.parser.feed(data):
                self.handle(t, f, seq, payload)
            if self.gen_req is not None:
                req, self.gen_req = self.gen_req, None
                self.run_gen(req)

    def handle(self, t, f, seq, payload):
        self.stats["rx_frames_ok"] += 1
        if t == P.CMD_PING:
            self.send(P.RESP_PONG, 0, seq,
                      payload[:16] + struct.pack("<I", self.cur_baud))
        elif t == P.CMD_SET_BAUD:
            baud, stop = struct.unpack(P.SET_BAUD_FMT, payload[:8])
            if baud < 9600 or baud > SIM_SOC_MAX_BAUD:
                self.send(P.RESP_ACK, 0, 0,
                          struct.pack(P.ACK_FMT, P.ACK_REJECT_RANGE,
                                      SIM_SOC_MAX_BAUD, P.SWITCH_DELAY_MS))
                return
            self.send(P.RESP_ACK, 0, 0,
                      struct.pack(P.ACK_FMT, P.ACK_OK, 0, P.SWITCH_DELAY_MS))
            time.sleep(P.SWITCH_DELAY_MS / 1000.0)
            self.cur_baud = baud
            self.stop_bits = 1 if baud == 115200 else stop
            if self.deaf_above and baud > self.deaf_above:
                self.deaf_deadline = time.monotonic() + P.SYNC_TIMEOUT_MS / 1000.0
        elif t == P.CMD_ECHO_DATA:
            self.send(P.RESP_ECHO, f, seq, payload)
        elif t == P.CMD_SINK_DATA:
            if seq == self.expect_seq:
                self.expect_seq += 1
            elif seq > self.expect_seq:
                self.stats["rx_seq_gap_frames"] += seq - self.expect_seq
                self.expect_seq = seq + 1
            else:
                self.stats["rx_seq_dup_frames"] += 1
                return
            expected = P.gen_payload(f, seq, len(payload))
            if payload != expected:
                self.stats["rx_mismatch_frames"] += 1
                self.stats["rx_mismatch_bytes"] += P.diff_bytes(expected, payload)
        elif t == P.CMD_GEN_START:
            self.gen_req = struct.unpack(P.GEN_START_FMT, payload[:12])
        elif t == P.CMD_GET_STATS:
            self.stats["rx_resync_bytes"] += self.parser.resync_bytes
            self.stats["rx_crc_err"] += self.parser.crc_errors
            self.parser.reset_counts()
            snap = dict(self.stats)
            snap["actual_baud"] = self.cur_baud
            self.send(P.RESP_STATS, 0, 0,
                      struct.pack(P.STATS_FMT, *[snap[k] for k in P.STATS_FIELDS]))
        elif t == P.CMD_CLEAR_STATS:
            self.stats = dict.fromkeys(P.STATS_FIELDS, 0)
            self.parser.reset_counts()
            self.expect_seq = 0
            self.send(P.RESP_ACK, 0, 0, struct.pack(P.ACK_FMT, P.ACK_OK, 0, 0))
        elif t == P.CMD_GET_INFO:
            self.send(P.RESP_INFO, 0, 0,
                      struct.pack(P.INFO_FMT, P.PROTO_VERSION, 0x010000,
                                  SIM_SOC_MAX_BAUD, self.cur_baud,
                                  0, 43, 44, self.stop_bits,
                                  32768, 32768, P.SYNC_TIMEOUT_MS,
                                  P.IDLE_REVERT_MS, b"sim"))
        elif t == P.CMD_ABORT:
            self.abort = True

    def run_gen(self, req):
        duration_ms, max_frames, plen, pattern, _ = req
        self.abort = False
        bps = self.cur_baud / 10.0  # pace at the pretend line rate
        t_end = time.monotonic() + duration_ms / 1000.0
        seq = 0
        wire = 0
        next_send = time.monotonic()
        while time.monotonic() < t_end and (not max_frames or seq < max_frames):
            pat = seq % P.PATTERN_COUNT if pattern == P.PAT_CYCLE else pattern
            frame = P.build_frame(P.RESP_GEN_DATA, pat, seq,
                                  P.gen_payload(pat, seq, plen))
            os.write(self.fd, frame)
            self.stats["tx_frames"] += 1
            self.stats["tx_bytes"] += len(frame)
            wire += len(frame)
            seq += 1
            next_send += len(frame) / bps
            delay = next_send - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            # Drain incoming traffic (duplex-phase SINK flood, or an ABORT) —
            # mirrors the firmware's per-frame pump_rx during generation.
            while True:
                r, _, _ = select.select([self.fd], [], [], 0)
                if not r:
                    break
                data = os.read(self.fd, 4096)
                if not data:
                    break
                self.stats["rx_bytes"] += len(data)
                for fr in self.parser.feed(data):
                    self.handle(*fr)
            if self.abort:
                break
        self.send(P.RESP_GEN_DONE, 0, 0,
                  struct.pack(P.GEN_DONE_FMT, seq, wire, 1 if self.abort else 0))


def main():
    argv = sys.argv[1:]
    deaf_above = None
    if "--deaf-above" in argv:
        i = argv.index("--deaf-above")
        deaf_above = int(argv[i + 1])
        del argv[i:i + 2]
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    sim = SimXiao(master, deaf_above=deaf_above)
    sim.start()

    sys.argv = [sys.argv[0]] + argv
    if len(sys.argv) > 1 and sys.argv[1] == "--run-suite":
        import uart_baud_test
        # No real termios on the sim link: accept every requested rate.
        uart_baud_test.set_custom_baud = lambda fd, baud, stop_bits=1: baud
        argv = ["--port", path] + sys.argv[2:]
        print("sim: serving on %s, running suite: %s" % (path, " ".join(argv)))
        return uart_baud_test.main(argv)

    print("sim: XIAO simulator on %s (ctrl-C to quit)" % path)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
