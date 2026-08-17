"""Best-effort background work: coroutines that must run, but must not sit on
the critical path.

Safe for link commands because Session.command serializes on an asyncio.Lock
with FIFO waiters — a backgrounded command queues behind whatever the exchange
does next rather than interleaving on the wire, and it acquires the lock ahead
of any command submitted after it. What this does NOT give you is a result:
nothing awaits these, so only use it for work whose failure is a log line and
not a broken exchange.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("bg")

# asyncio holds only a WEAK reference to a running task, so without a strong ref
# here the GC can collect a live task mid-flight.
_TASKS: set[asyncio.Task] = set()


def fire_and_forget(coro, *, what: str = "background task") -> None:
    task = asyncio.create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    task.add_done_callback(lambda t: _report(t, what))


def _report(task: asyncio.Task, what: str) -> None:
    """Retrieve the exception so a failure is a named warning here rather than
    asyncio's anonymous "Task exception was never retrieved" at GC time."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.warning("%s failed: %s", what, exc)


async def drain(timeout: float = 5.0) -> None:
    """Wait out any in-flight background work. Shutdown only — the point of
    fire_and_forget is that the exchange does not wait."""
    pending = [t for t in _TASKS if not t.done()]
    if pending:
        await asyncio.wait(pending, timeout=timeout)
