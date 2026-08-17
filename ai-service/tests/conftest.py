from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_firmware import FakeFirmware  # noqa: E402
from hw1_ai_service.link.session import Session  # noqa: E402
from hw1_ai_service.link.transport import SerialTransport  # noqa: E402


@pytest.fixture
def firmware():
    fw = FakeFirmware()
    fw.start()
    yield fw
    fw.stop()


def open_link(fw: FakeFirmware, *, frame_sink=None) -> tuple[SerialTransport, Session]:
    """Open a transport+session against the fake firmware's pty. Must be
    called from inside a running event loop (transport.open binds to it).
    Baud is meaningless on a pty — and macOS rejects non-termios rates on
    one — so tests open at 115200; the real port config lives in config.yaml."""
    ser = serial.Serial(fw._slave_path, 115200, timeout=0.05, write_timeout=2.0)
    transport = SerialTransport(serial_obj=ser, frame_sink=frame_sink)
    transport.open()
    session = Session(transport, fw.user, fw.password)
    return transport, session


def run(coro):
    """asyncio.run with a hard test timeout so a deadlock fails, not hangs."""
    async def _wrapped():
        return await asyncio.wait_for(coro, timeout=30)
    return asyncio.run(_wrapped())
