"""The audit-required soak: prove the reader thread keeps draining while the
event loop is deliberately stalled, with zero line loss.

A pty is not a UART, and the difference is not merely buffer sizing: a pty
writer BLOCKS once its buffers fill, so no stall length can make this test
drop a byte. It therefore pins the PROPERTY — reader-thread independence
from event-loop stalls — and nothing about margin. The wire is the only
place a stall can cost bytes; that rerun happens on the Pi against the real
~7s flip-buffer budget (ARCHITECTURE.md §7).

The 0.5s stall below is sized to exercise the property quickly, not to
approach any cliff.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest
from conftest import run
from fake_firmware import FakeFirmware

import serial
from hw1_ai_service.link.transport import SerialTransport


@pytest.mark.slow
def test_reader_survives_event_loop_stall(firmware: FakeFirmware):
    N_LINES = 2000          # ~2s of flood at full blast
    LINE = "SEQ {i:06d} " + "x" * 30

    async def main():
        ser = serial.Serial(firmware._slave_path, 115200, timeout=0.05)
        transport = SerialTransport(serial_obj=ser)
        transport.open()
        try:
            done = threading.Event()

            def flood():
                for i in range(N_LINES):
                    firmware._write_raw((LINE.format(i=i) + "\n").encode())
                done.set()

            flooder = threading.Thread(target=flood, daemon=True)
            flooder.start()

            # Stall the EVENT LOOP (not the reader thread) mid-flood, twice.
            time.sleep(0.2)
            for _ in range(2):
                time.sleep(0.5)          # deliberate loop stall (blocking!)
                await asyncio.sleep(0)   # let queued call_soon_threadsafe drain

            done.wait(timeout=20)

            seen: set[int] = set()
            deadline = time.monotonic() + 15
            while len(seen) < N_LINES and time.monotonic() < deadline:
                try:
                    ev = await asyncio.wait_for(transport.rx.get(), 1.0)
                except asyncio.TimeoutError:
                    continue
                if ev.kind == "line" and ev.text.startswith("SEQ "):
                    seen.add(int(ev.text.split()[1]))
            missing = N_LINES - len(seen)
            assert missing == 0, f"lost {missing}/{N_LINES} lines across loop stalls"
        finally:
            transport.close()
    run(main())
