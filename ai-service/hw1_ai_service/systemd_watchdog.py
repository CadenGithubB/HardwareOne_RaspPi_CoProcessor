"""Minimal systemd service-watchdog integration.

The watchdog deliberately runs on the main asyncio loop.  If that loop stops
making progress, keep-alive datagrams stop too and systemd can restart the
whole service control group.  Native STT/model work that is correctly running
off-loop does not interfere with it.

No systemd Python package is required: ``sd_notify`` messages are datagrams to
the address supplied in ``NOTIFY_SOCKET``.  Outside a watchdog-enabled systemd
unit the object is an inert no-op.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket


log = logging.getLogger("systemd.watchdog")

_WATCHDOG_MESSAGE = b"WATCHDOG=1"


def _notification_address(value: str) -> str:
    """Convert systemd's environment spelling to an AF_UNIX address."""
    if value.startswith("@") and len(value) > 1:
        # Linux abstract-namespace socket. Python spells the leading NUL
        # directly; systemd uses '@' so it can live in an environment string.
        return "\0" + value[1:]
    if value.startswith("/"):
        return value
    raise ValueError("NOTIFY_SOCKET must be absolute or abstract")


class SystemdWatchdog:
    """Main-loop keep-alive sender configured entirely by systemd's env."""

    def __init__(self, address: str | None = None,
                 interval_s: float = 0.0) -> None:
        self._address = address
        self._interval_s = interval_s
        self._socket: socket.socket | None = None
        self._task: asyncio.Task | None = None
        self._send_failure_logged = False

    @property
    def enabled(self) -> bool:
        return self._address is not None and self._interval_s > 0

    @classmethod
    def from_environment(
            cls, *, unset_environment: bool = True) -> "SystemdWatchdog":
        """Build from ``NOTIFY_SOCKET``/``WATCHDOG_*`` or return a no-op.

        Consuming the variables prevents supervised children such as
        llama-server from inheriting the service notification capability.
        """
        notify_socket = os.environ.get("NOTIFY_SOCKET", "")
        watchdog_usec = os.environ.get("WATCHDOG_USEC", "")
        watchdog_pid = os.environ.get("WATCHDOG_PID", "")

        if unset_environment:
            for name in ("NOTIFY_SOCKET", "WATCHDOG_USEC", "WATCHDOG_PID"):
                os.environ.pop(name, None)

        try:
            if not notify_socket or not watchdog_usec:
                return cls()
            usec = int(watchdog_usec, 10)
            if usec <= 0:
                return cls()
            if watchdog_pid and int(watchdog_pid, 10) != os.getpid():
                return cls()
            address = _notification_address(notify_socket)
        except (TypeError, ValueError):
            log.warning("ignoring malformed systemd watchdog environment")
            return cls()

        # systemd recommends pinging at half the configured timeout.  There is
        # intentionally no background thread: event-loop progress is the
        # health condition this service watchdog measures.
        return cls(address, usec / 2_000_000.0)

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        try:
            notify_socket = self._open_socket()
        except OSError as exc:
            log.warning("cannot connect to systemd notification socket: %s", exc)
            return

        self._socket = notify_socket
        # Send immediately so startup never consumes a full watchdog interval.
        await self._send()
        self._task = asyncio.create_task(
            self._run(), name="systemd-watchdog")

    def _open_socket(self) -> socket.socket:
        notify_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            notify_socket.setblocking(False)
            notify_socket.connect(self._address)
            return notify_socket
        except BaseException:
            notify_socket.close()
            raise

    async def close(self) -> None:
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(self._interval_s)
            await self._send()

    async def _send(self) -> None:
        notify_socket = self._socket
        if notify_socket is None:
            return
        try:
            await asyncio.get_running_loop().sock_sendall(
                notify_socket, _WATCHDOG_MESSAGE)
            self._send_failure_logged = False
        except OSError as exc:
            # Notification failure is not an application exception. If it
            # persists, systemd's own watchdog deadline remains authoritative.
            if not self._send_failure_logged:
                log.warning("systemd watchdog notification failed: %s", exc)
                self._send_failure_logged = True
