"""Damaged text lines must not read as missing firmware features.

The link's TEXT channel carries no integrity check — only the P2 binary frames
get a CRC — so at 2 Mbaud with no flow control a flipped bit arrives at the
firmware's parser as a real command. When the damage lands in the VERB, the
firmware answers with an ordinary unknown-command reply, which is the same
shape a build genuinely missing that command produces.

On 2026-08-25 one damaged `cm5 llm push` reached the device as `he5 llm push`
('c'->'h' and 'm'->'e' are one flipped bit each). The daemon read the reply as
"this build has no CM5 LLM registry", latched the bridge off for the life of
the process, and — because the latch also gated the terminal `end` — left the
firmware holding the turn open until its 60s stall timer fired. From the user's
chair: the question was accepted, the fan spun up, and no answer ever arrived.

These tests pin the three rules that came out of it:

  1. an echoed verb we never wrote is DAMAGE, never a capability fact
  2. capability is decided once, at the first probe — a working bridge cannot
     lose a command mid-link
  3. the terminal `end` outranks every local give-up latch
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

import pytest

from conftest import run
from test_cm5_llm import StubLlm, _cfg, _Harness   # noqa: F401  (shared harness)

from hw1_ai_service.cm5_llm import Cm5LlmService
from hw1_ai_service.cm5_presence import (
    MAX_CORRUPT_HEARTBEATS,
    NORMAL_LEASE_MS,
    BUSY_LEASE_MS,
    Cm5Presence,
)
from hw1_ai_service.cm5_time import (
    FLAG_PI_RTC_VALID,
    FLAG_PI_SYNCED,
    Cm5Time,
)
from hw1_ai_service.link import protocol
from hw1_ai_service.link.session import LinkClosed


# The exact reply the device sent on 2026-08-25.
DAMAGED_REPLY = "Unknown command: he5 ***\nType 'help' for available commands"


# --------------------------------------------------------------------------
# 1. The classifier
# --------------------------------------------------------------------------


def test_an_echoed_verb_we_never_wrote_is_damage():
    assert protocol.classify_unknown_command(
        DAMAGED_REPLY, "cm5 llm push 4 12 the") == protocol.UNKNOWN_CORRUPT


def test_a_matching_echo_is_a_missing_command():
    assert protocol.classify_unknown_command(
        "Unknown command: cm5 ***", "cm5 llm push 4 12 the"
    ) == protocol.UNKNOWN_MISSING
    assert protocol.classify_unknown_command(
        "Unknown command: cm5\nType 'help'", "cm5 heartbeat 1 9 ready"
    ) == protocol.UNKNOWN_MISSING


def test_flipped_case_is_damage_not_a_missing_command():
    """'c' -> 'C' is the 0x20 bit: exactly the damage this exists to catch.
    A casefolded compare would file it as a missing command and disable the
    feature for good."""
    assert protocol.classify_unknown_command(
        "Unknown command: CM5 ***", "cm5 llm push 4 12 the"
    ) == protocol.UNKNOWN_CORRUPT


def test_ordinary_replies_are_not_unknown_commands():
    for text in ("OK: 12", "OK", "Error: stale session", ""):
        assert protocol.classify_unknown_command(text, "cm5 llm end 4 ok") is None


def test_the_echoed_verb_is_reported_for_the_log():
    assert protocol.unknown_command_echo(DAMAGED_REPLY) == "he5"
    assert protocol.unknown_command_echo("OK: 12") is None


# --------------------------------------------------------------------------
# 2. The LLM bridge, against the firmware double over a pty
# --------------------------------------------------------------------------


_DELTAS = ["Sheep ", "sleep ", "more ", "in ", "winter."]


def test_a_damaged_push_is_replayed_and_the_answer_still_lands(firmware, tmp_path):
    """The regression: one damaged push used to kill the whole bridge."""
    client = StubLlm(_DELTAS)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.damage_next_verb(1, match="llm push")
            firmware.llm_ask("when do sheep sleep the most?")
            await h.settle(lambda: firmware.cm5_llm_ends, timeout=20.0)
            # The bridge must still be on offer: nothing was learned about
            # what this build supports.
            assert h.service._supported is not False

    run(main())
    assert firmware.cm5_llm_text == "".join(_DELTAS)
    assert firmware.cm5_llm_ends, "the turn was never terminated"
    seqs = [seq for _sess, seq in firmware.cm5_llm_pushes]
    assert seqs == list(range(len(seqs))), seqs


def test_a_turn_that_gives_up_still_terminates_on_the_device(firmware, tmp_path):
    """Both writes of a line damaged -> give up on the line, but the device
    must still be told, or every surface holds the turn for 60s."""
    client = StubLlm(_DELTAS)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.damage_next_verb(2, match="llm push")  # write AND replay
            firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends, timeout=20.0)
            assert h.service._supported is not False

    run(main())
    assert firmware.cm5_llm_ends, "the device was left hanging"
    _session, status, _tokens, _tps = firmware.cm5_llm_ends[-1]
    assert status == "error"


def test_a_damaged_end_is_rewritten_so_the_turn_still_closes(firmware, tmp_path):
    """`end` is the most expensive line to lose: without it the firmware holds
    the streaming turn open on every surface until CM5_LLM_STALL_MS (60s)."""
    client = StubLlm(_DELTAS)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.damage_next_verb(1, match="llm end")
            firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends, timeout=20.0)

    run(main())
    assert firmware.cm5_llm_ends, "the device was left hanging"
    _session, status, _tokens, _tps = firmware.cm5_llm_ends[-1]
    assert status == "ok"
    assert firmware.cm5_llm_text == "".join(_DELTAS)


# --------------------------------------------------------------------------
# 3. The LLM bridge's capability rule, at the policy chokepoint
# --------------------------------------------------------------------------


class _Reply:
    def __init__(self, text: str, ok: bool = True) -> None:
        self.text = text
        self.ok = ok


class _Session:
    """Scripted replies; records every line the bridge wrote."""

    def __init__(self, replies=None) -> None:
        self.lines: list[str] = []
        self._replies = list(replies or [])

    async def command(self, line: str, **_kw):
        self.lines.append(line)
        if self._replies:
            return self._replies.pop(0)
        return _Reply("OK")


def _service(session, tmp_path) -> Cm5LlmService:
    return Cm5LlmService(session, _cfg(tmp_path))


def test_a_matching_echo_on_the_first_probe_disables_the_bridge(tmp_path):
    """Preserved behaviour: a build with no registry fails fast."""
    session = _Session([_Reply("Unknown command: cm5 ***", ok=False)])
    svc = _service(session, tmp_path)

    async def main():
        assert await svc._send("cm5 llm models 1 0 1 3 m") is False
        assert svc._supported is False
        # Latched: nothing further is written.
        assert await svc._send("cm5 llm push 1 0 hi") is False
        assert len(session.lines) == 1

    run(main())


def test_a_working_bridge_is_never_disabled_by_a_later_unknown(tmp_path):
    """Capability is decided once. A build that has already served our lines
    cannot lose a command, so a matching echo here is a link fault."""
    session = _Session([
        _Reply("OK: 0"),                                # first probe works
        _Reply("Unknown command: cm5 ***", ok=False),   # ...then this
        _Reply("Unknown command: cm5 ***", ok=False),
    ])
    svc = _service(session, tmp_path)

    async def main():
        assert await svc._send("cm5 llm models 1 0 1 3 m") is True
        assert svc._supported is True
        assert await svc._send("cm5 llm push 1 0 hi",
                               retry_idempotent=True) is False
        assert svc._supported is True, "the bridge disabled itself"

    run(main())
    # Retried once rather than latching.
    assert session.lines.count("cm5 llm push 1 0 hi") == 2


def test_a_damaged_reply_is_retried_not_latched(tmp_path):
    session = _Session([
        _Reply(DAMAGED_REPLY, ok=False),
        _Reply("OK: 0"),
    ])
    svc = _service(session, tmp_path)

    async def main():
        assert await svc._send("cm5 llm push 1 0 hi",
                               retry_idempotent=True) is True
        assert svc._supported is True

    run(main())
    assert session.lines.count("cm5 llm push 1 0 hi") == 2


def test_the_terminal_end_outranks_the_capability_latch(tmp_path):
    """A dead turn still owes the device its `end`; without it the firmware
    holds a streaming turn open until CM5_LLM_STALL_MS."""
    session = _Session()
    svc = _service(session, tmp_path)
    svc._supported = False        # bridge given up on, for any reason

    async def main():
        await svc._send_end(4, "error")

    run(main())
    assert session.lines == ["cm5 llm end 4 error 0 0"], session.lines


def test_a_closed_bridge_writes_nothing_at_all(tmp_path):
    """`force` bypasses the capability latch, never the shutdown gate."""
    session = _Session()
    svc = _service(session, tmp_path)
    svc._closed = True

    async def main():
        await svc._send_end(4, "error")

    run(main())
    assert session.lines == []


# --------------------------------------------------------------------------
# 4. Presence — the heartbeat must not fall back to legacy on damage
# --------------------------------------------------------------------------


class _HeartbeatSession:
    def __init__(self, *, damaged: int) -> None:
        self.calls: list[str] = []
        self._damaged = damaged

    async def command(self, line: str, **_kw):
        self.calls.append(line)
        if self._damaged > 0:
            self._damaged -= 1
            return _Reply(DAMAGED_REPLY, ok=False)
        _cm5, _hb, _ver, seq, mode = line.split()
        lease = BUSY_LEASE_MS if mode == "busy" else NORMAL_LEASE_MS
        return _Reply(
            f"OK: cm5 heartbeat version=1 seq={seq} state={mode} "
            f"session_epoch=7 lease_ms={lease}")


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0)


def test_a_damaged_heartbeat_is_resent_not_read_as_legacy_firmware():
    async def main() -> None:
        session = _HeartbeatSession(damaged=1)
        presence = Cm5Presence(session, interval_s=60, legacy_reprobe_s=60)
        task = asyncio.create_task(presence.run())
        try:
            await _wait_until(lambda: presence.supported is True)
            assert len(session.calls) == 2, session.calls
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    run(main())


def test_a_run_of_damaged_heartbeats_reconnects_the_link():
    """Persistent damage is a link that is no longer carrying text; the
    supervisor's reconnect (re-open, re-login) is the broader repair."""
    async def main() -> None:
        session = _HeartbeatSession(damaged=99)
        presence = Cm5Presence(session, interval_s=60, legacy_reprobe_s=60)
        with pytest.raises(LinkClosed, match="damaged in transit"):
            await presence.run()
        assert len(session.calls) == MAX_CORRUPT_HEARTBEATS
        assert presence.supported is not False, "dropped to legacy on damage"

    run(main())


# --------------------------------------------------------------------------
# 5. Clock push
# --------------------------------------------------------------------------


class _TimeSession:
    def __init__(self, reply) -> None:
        self.calls: list[str] = []
        self._reply = reply

    def add_reboot_listener(self, listener) -> None:
        pass

    async def command(self, line: str, **_kw):
        self.calls.append(line)
        return self._reply


def test_a_damaged_clock_push_does_not_disable_the_pusher():
    async def main() -> None:
        session = _TimeSession(_Reply(DAMAGED_REPLY, ok=False))
        actor = Cm5Time(session,
                        clock_fn=lambda: 1_700_000_000.0,
                        confidence_fn=lambda: FLAG_PI_SYNCED | FLAG_PI_RTC_VALID)
        settled = await actor._push_once()
        assert settled is False, "a damaged push must be retried"
        assert actor.supported is not False, "clock push disabled by damage"

    run(main())
