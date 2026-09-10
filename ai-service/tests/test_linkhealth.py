"""The host's fault tally, and its trip to the device's CLI.

None of these counters change what the daemon does — that is the point. They
exist so a bench operator can answer "is this link damaging text, and how
often?" from the XIAO, instead of from a journal on a Pi that may have no
network. See link/health.py and cm5_linkhealth.py.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from conftest import open_link, run
from test_cm5_llm import StubLlm, _cfg, _Harness   # noqa: F401  (shared harness)

from hw1_ai_service.cm5_linkhealth import Cm5LinkHealth
from hw1_ai_service.link.health import LinkHealth, note_corrupt, note_reset


DAMAGED_REPLY = "Unknown command: he5 ***\nType 'help' for available commands"


# --------------------------------------------------------------------------
# Counters
# --------------------------------------------------------------------------


def test_fault_counters_bump_the_generation_but_volume_does_not():
    """A reporter uses fault_generation to decide "push now"; tx/rx churn
    constantly and must not trigger one."""
    h = LinkHealth()
    assert h.fault_generation == 0
    h.note_tx()
    h.note_rx_line()
    h.note_login()
    assert h.fault_generation == 0
    h.note_corrupt()
    h.note_garbage()
    h.note_timeout()
    h.note_stray()
    h.note_reset()
    assert h.fault_generation == 5


def test_the_wire_form_is_all_ints_and_stable_in_order():
    fields = LinkHealth().fields()
    assert list(fields) == ["garbage", "corrupt", "timeouts", "strays",
                            "logins", "resets", "tx", "rx", "up"]
    assert all(isinstance(v, int) for v in fields.values())


def test_the_tolerant_accessors_ignore_a_session_without_counters():
    class _Bare:
        pass

    note_corrupt(_Bare())      # must not raise
    note_reset(_Bare())


# --------------------------------------------------------------------------
# Wiring: the transport and session actually move the numbers
# --------------------------------------------------------------------------


def test_a_login_moves_tx_rx_and_logins(firmware):
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            h = session.health
            assert h.logins == 1
            assert h.tx >= 1, "the login line was not counted"
            assert h.rx >= 1, "the login reply was not counted"
            assert h.corrupt == 0 and h.timeouts == 0
        finally:
            transport.close()

    run(main())


def test_garbage_count_still_reads_through_the_tally():
    """`garbage_count` predates link/health.py and other code still uses it."""
    h = LinkHealth()
    h.note_garbage()
    assert h.garbage == 1


# --------------------------------------------------------------------------
# The reporter, end to end against the firmware double
# --------------------------------------------------------------------------


class _Session:
    """Just enough Session for the reporter: a tally and a command sink."""

    def __init__(self, replies=None) -> None:
        self.health = LinkHealth()
        self.lines: list[str] = []
        self._replies = list(replies or [])

    async def command(self, line: str, **_kw):
        self.lines.append(line)
        if self._replies:
            return self._replies.pop(0)
        return _Reply("OK: cm5 linkhealth version=1 keys=9 unknown=0")


class _Reply:
    def __init__(self, text: str, ok: bool = True) -> None:
        self.text = text
        self.ok = ok


def test_the_pushed_line_is_the_documented_grammar():
    async def main():
        session = _Session()
        session.health.note_corrupt()
        reporter = Cm5LinkHealth(session)
        await reporter._push_once()
        return session.lines

    lines = run(main())
    assert len(lines) == 1
    head, _, tail = lines[0].partition(" 1 ")
    assert head == "cm5 linkhealth"
    fields = dict(tok.split("=", 1) for tok in tail.split())
    assert fields["corrupt"] == "1"
    assert set(fields) == {"garbage", "corrupt", "timeouts", "strays",
                           "logins", "resets", "tx", "rx", "up"}
    assert all(v.isdecimal() for v in fields.values())


def test_an_older_firmware_disables_the_reporter_once():
    async def main():
        session = _Session([_Reply("Unknown command: cm5 ***", ok=False)])
        reporter = Cm5LinkHealth(session)
        assert await reporter._push_once() is False
        assert reporter.supported is False
        assert await reporter._push_once() is False
        assert len(session.lines) == 1, "kept hammering a build without it"

    run(main())


def test_a_damaged_report_is_not_read_as_an_older_firmware():
    async def main():
        session = _Session([_Reply(DAMAGED_REPLY, ok=False)])
        reporter = Cm5LinkHealth(session)
        assert await reporter._push_once() is False
        assert reporter.supported is not False
        # ...and it counted itself.
        assert session.health.corrupt == 1

    run(main())


def test_the_device_accepts_and_stores_a_real_report(firmware):
    """Against the firmware double's `cm5 linkhealth` intrinsic."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            session.health.note_corrupt()
            session.health.note_garbage()
            reporter = Cm5LinkHealth(session)
            assert await reporter._push_once() is True
        finally:
            transport.close()

    run(main())
    assert firmware.cm5_linkhealth_reports == 1
    assert firmware.cm5_linkhealth["corrupt"] == 1
    assert firmware.cm5_linkhealth["garbage"] == 1
    assert firmware.cm5_linkhealth["logins"] == 1


def test_an_unknown_counter_still_lands_on_an_older_build(firmware):
    """Forward compatibility: the device parses by key and ignores the rest,
    so adding a counter here does not need a firmware flash."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            reply = await session.command(
                "cm5 linkhealth 1 garbage=2 something_new=7",
                expect="status")
            assert reply.ok, reply.text
            assert "unknown=1" in reply.text
        finally:
            transport.close()

    run(main())
    assert firmware.cm5_linkhealth == {"garbage": 2}


def test_a_malformed_report_is_rejected_whole(firmware):
    """Damage is what these counters measure; a damaged report must never be
    stored as truth."""
    async def main():
        transport, session = open_link(firmware)
        try:
            await session.login()
            reply = await session.command(
                "cm5 linkhealth 1 garbage=2 corrupt=notanumber",
                expect="status")
            assert not reply.ok
        finally:
            transport.close()

    run(main())
    assert firmware.cm5_linkhealth == {}
    assert firmware.cm5_linkhealth_reports == 0


def test_a_damaged_verb_during_a_turn_reaches_the_device_as_a_count(
        firmware, tmp_path):
    """The whole point, end to end: damage a real LLM push, and the number a
    human would read on the XIAO goes up."""
    client = StubLlm(["Sheep ", "sleep ", "more ", "in ", "winter."])
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.damage_next_verb(1, match="llm push")
            firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends, timeout=20.0)
            reporter = Cm5LinkHealth(h.session)
            assert await reporter._push_once() is True

    run(main())
    assert firmware.cm5_linkhealth["corrupt"] == 1, firmware.cm5_linkhealth
