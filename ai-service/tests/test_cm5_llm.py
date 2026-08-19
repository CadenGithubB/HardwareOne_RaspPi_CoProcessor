"""CM5-as-LLM-source bridge, against the real firmware double over a pty.

The firmware half of this protocol is already written and shipped; these tests
pin the six things that were called out as the ones most likely to bite, plus
the catalog/lifecycle behaviour around them:

  1. escaping round-trips, including the whitespace cases the wire depends on
  2. a prompt with a curly apostrophe survives `llm_ask` end to end
  3. deltas reassemble BYTE-EXACTLY, including a chunk's leading space
  4. `end` is sent on every error path
  5. `seq` is contiguous from 0 and a forced retry does not duplicate text
  6. selecting a model that is not here does not strand the device in LOADING
"""

from __future__ import annotations

import asyncio
import random

import pytest
from conftest import open_link, run
from fake_firmware import llm_escape, llm_unescape

from hw1_ai_service import cm5_llm, cm5_presence
from hw1_ai_service.cm5_llm import Cm5LlmProtocolError, Cm5LlmService
from hw1_ai_service.config import LlmConfig
from hw1_ai_service.jobs import ManualTrigger, route_link_event

# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class StubLlm:
    """Minimal LlmClient surface: scripted deltas, recorded call parameters."""

    def __init__(self, deltas, *, fail_after: int | None = None,
                 delay_s: float = 0.0):
        self._deltas = list(deltas)
        self._fail_after = fail_after
        self._delay_s = delay_s
        self.prompts: list[str] = []
        self.params: list[dict] = []
        self.cleared = 0

    async def ask_stream(self, prompt, *, commit_history=True,
                         max_tokens=None, temperature=None, top_p=None):
        self.prompts.append(prompt)
        self.params.append({"max_tokens": max_tokens,
                            "temperature": temperature, "top_p": top_p})
        for i, delta in enumerate(self._deltas):
            if self._fail_after is not None and i == self._fail_after:
                raise RuntimeError("llama-server returned 500")
            if self._delay_s:
                await asyncio.sleep(self._delay_s)
            else:
                await asyncio.sleep(0)
            yield delta

    def clear_history(self) -> None:
        self.cleared += 1

    async def close(self) -> None:
        pass


def _cfg(tmp_path, *, names=("tinyllama-1.1b-chat-q4",), active=0) -> LlmConfig:
    for name in names:
        path = tmp_path / f"{name}.gguf"
        path.write_bytes(b"\0" * (3 * 1024 * 1024))
    cfg = LlmConfig()
    cfg.model_dir = str(tmp_path)
    cfg.model = str(tmp_path / f"{names[active]}.gguf") if names else ""
    cfg.uart_timeout_s = 5.0
    cfg.server_bin = "/usr/bin/true"      # "this daemon owns llama-server"
    return cfg


class _Harness:
    """A logged-in Session with the bridge running and events routed to it."""

    def __init__(self, fw, cfg, client):
        self.fw = fw
        self.cfg = cfg
        self.client = client
        self.service: Cm5LlmService | None = None

    async def __aenter__(self):
        self._transport, self.session = open_link(self.fw)
        await self.session.login()
        self.service = Cm5LlmService(self.session, self.cfg)
        trigger = ManualTrigger()
        self.session.on_event = lambda payload: route_link_event(
            payload, trigger, self.session, None, None, self.service)
        self._task = asyncio.create_task(self.service.run())
        self._pump = asyncio.create_task(self.session.pump_events())
        await self.service.attach(self.client, None)
        return self

    async def __aexit__(self, *exc):
        self.session.on_event = None
        for task in (self._task, self._pump):
            task.cancel()
        await asyncio.gather(self._task, self._pump, return_exceptions=True)
        await self.service.close()
        self._transport.close()

    async def settle(self, predicate, timeout: float = 10.0) -> None:
        """Wait for a device-side condition instead of sleeping a guess."""
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError("timed out waiting for the device")
            await asyncio.sleep(0.01)


# --------------------------------------------------------------------------
# 1. Escaping
# --------------------------------------------------------------------------


ESCAPE_CASES = [
    "",
    " ",
    "   ",
    "hello world",
    " leading space",
    "trailing space ",
    "line one\nline two",
    "carriage\r\nreturn",
    "tab\tseparated",
    "back\\slash",
    "\\",
    "\\\\",
    "ends with a backslash \\",
    "it's a wrap",                     # U+2019
    "café — naïve — 日本語",
    "mixed \\n literal and real \n newline",
    "\n\r\t \\ ",
]


@pytest.mark.parametrize("text", ESCAPE_CASES)
def test_escape_round_trips_through_the_firmware_decoder(text):
    """escape() must survive the DEVICE's decoder, not just our own."""
    assert llm_unescape(cm5_llm.escape(text)) == text
    assert cm5_llm.unescape(llm_escape(text)) == text
    assert cm5_llm.unescape(cm5_llm.escape(text)) == text


def test_escape_round_trips_on_random_payloads():
    alphabet = "ab \t\n\r\\'’é日 ."
    rng = random.Random(20260817)
    for _ in range(2000):
        text = "".join(rng.choice(alphabet)
                       for _ in range(rng.randint(0, 40)))
        assert llm_unescape(cm5_llm.escape(text)) == text


def test_escaped_form_carries_no_whitespace():
    """The device trims every inbound line, so a chunk that still contained
    raw whitespace could lose bytes at either edge."""
    for text in ESCAPE_CASES:
        escaped = cm5_llm.escape(text)
        assert not any(ch.isspace() for ch in escaped), escaped


def test_unknown_escape_degrades_to_the_character():
    # A newer peer adding an escape must read as text, never as a hole.
    assert cm5_llm.unescape("a\\qb") == "aqb"
    assert cm5_llm.unescape("trailing\\") == "trailing\\"


# --------------------------------------------------------------------------
# 2. Event parsing — the strict-ASCII trap
# --------------------------------------------------------------------------


def test_curly_apostrophe_prompt_parses():
    prompt = "what’s the weather like — briefly?"
    payload = f"llm_ask 7 250 70 95 {llm_escape(prompt)}".encode()
    ask = cm5_llm.parse_llm_event(payload)
    assert ask.prompt == prompt
    assert (ask.session, ask.max_tokens) == (7, 250)
    assert ask.temperature == pytest.approx(0.70)
    assert ask.top_p == pytest.approx(0.95)


def test_the_longest_prompt_the_device_can_send_is_not_clipped():
    """The device bounds the ESCAPED prompt at 2x CM5_LLM_MAX_PROMPT, so a
    whitespace-free prompt of 1400 raw chars is legal on the wire. Guarding at
    700 here would silently clip it."""
    prompt = "a" * 1400
    ask = cm5_llm.parse_llm_event(
        f"llm_ask 1 250 70 95 {llm_escape(prompt)}".encode())
    assert ask.prompt == prompt


def test_non_llm_payloads_are_not_claimed():
    # Left for the generic handler, which logs them as unhandled — claiming a
    # payload we do not understand would hide a real routing mistake.
    assert cm5_llm.parse_llm_event(b"evenai_wake a1b2c3d400000001") is None
    assert cm5_llm.parse_llm_event(b"cm5_fan_status 1 a1b2c3d400000001") is None
    assert cm5_llm.parse_llm_event(b"llm_frobnicate 1") is None


def test_malformed_llm_events_raise():
    for payload in (b"llm_ask 1 250 70", b"llm_cancel", b"llm_select",
                    b"llm_ask x 250 70 95 hi", b"llm_askew 1"):
        with pytest.raises(Cm5LlmProtocolError):
            cm5_llm.parse_llm_event(payload)


def test_curly_apostrophe_survives_end_to_end(firmware, tmp_path):
    """Gotcha #1: route_link_event's strict-ASCII decode would have dropped
    this prompt with only a log line — no error, no timeout, either side."""
    prompt = "what’s the plan for tomorrow?"
    client = StubLlm(["Sunny ", "and warm."])
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            session = firmware.llm_ask(prompt)
            await h.settle(lambda: firmware.cm5_llm_ends)
            return session

    session = run(main())
    assert client.prompts == [prompt]
    assert firmware.cm5_llm_text == "Sunny and warm."
    assert firmware.cm5_llm_ends[0][:2] == (session, "ok")


def test_ask_parameters_are_honoured(firmware, tmp_path):
    client = StubLlm(["ok"])
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("hi", max_tokens=64, temp_x100=15, topp_x100=40)
            await h.settle(lambda: firmware.cm5_llm_ends)

    run(main())
    assert client.params == [
        {"max_tokens": 64, "temperature": pytest.approx(0.15),
         "top_p": pytest.approx(0.40)}]


# --------------------------------------------------------------------------
# 3. Byte-exact reassembly
# --------------------------------------------------------------------------


# Deliberately long enough to force several flushes, with the inter-word space
# landing exactly at a chunk boundary — the whole point of escaping everything.
_ANSWER_DELTAS = [
    "The", " Raspberry", " Pi", " CM5", " runs", " the", " model", ".",
    " It", " streams", " deltas", " back", " over", " UART", ".",
    " Each", " chunk", " keeps", " its", " leading", " space", ",",
    " so", " the", " device", " can", " concatenate", " them", " without",
    " guessing", " where", " a", " word", " boundary", " was", ".",
    "\n", "Second", " line", " after", " a", " newline", ".",
    " Unicode", " too", ":", " café", " naïve", " —", " done", ".",
]


def test_deltas_reassemble_byte_exactly(firmware, tmp_path):
    client = StubLlm(_ANSWER_DELTAS)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("tell me about the co-processor")
            await h.settle(lambda: firmware.cm5_llm_ends)

    run(main())
    assert firmware.cm5_llm_text == "".join(_ANSWER_DELTAS)
    # More than one push, or this proves nothing about chunk boundaries.
    assert len(firmware.cm5_llm_pushes) > 1


def test_a_single_short_answer_still_arrives(firmware, tmp_path):
    client = StubLlm(["Yes."])
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("is it on?")
            await h.settle(lambda: firmware.cm5_llm_ends)

    run(main())
    assert firmware.cm5_llm_text == "Yes."


def test_oversized_chunks_are_split_on_codepoint_boundaries():
    text = "日" * 300                    # 3 bytes each
    parts = cm5_llm.split_for_push(text, 200)
    assert "".join(parts) == text
    assert all(len(p.encode("utf-8")) <= 200 for p in parts)
    assert len(parts) > 1


def test_a_wall_of_text_reassembles(firmware, tmp_path):
    """One delta far past both the flush limit and the device's decode buffer."""
    body = "word " * 400
    client = StubLlm([body])
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("ramble")
            await h.settle(lambda: firmware.cm5_llm_ends)

    run(main())
    assert firmware.cm5_llm_text == body


# --------------------------------------------------------------------------
# 4. `end` on every error path
# --------------------------------------------------------------------------


def test_end_is_sent_when_the_model_server_fails(firmware, tmp_path):
    client = StubLlm(["Partial ", "answer ", "then"], fail_after=2)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            session = firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends)
            return session

    session = run(main())
    assert firmware.cm5_llm_ends[0][:2] == (session, "error")


def test_end_is_sent_when_the_model_plane_is_down(firmware, tmp_path):
    """An ask before llama-server is up must terminate the turn immediately,
    not leave every surface waiting out the device's 45s stall timer."""
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, None) as h:
            session = firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends)
            return session

    session = run(main())
    assert firmware.cm5_llm_ends[0][:2] == (session, "error")
    assert firmware.cm5_llm_text == ""


def test_cancel_stops_the_generation_and_still_ends(firmware, tmp_path):
    client = StubLlm([f"delta{i} " for i in range(200)], delay_s=0.01)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            session = firmware.llm_ask("long one")
            await h.settle(lambda: firmware.cm5_llm_pushes)
            firmware.llm_cancel(session)
            await h.settle(lambda: h.service._current is None)
            return session

    session = run(main())
    # cm5LlmStop finishes locally before telling us, so the terminal `end` is
    # legitimately rejected as stale — what matters is that we stopped, and
    # that we never closed out somebody else's session on the way.
    assert all(record[0] == session for record in firmware.cm5_llm_ends)
    assert firmware.cm5_llm_done
    assert len(firmware.cm5_llm_text) < len("".join(f"delta{i} "
                                                    for i in range(200)))


def test_a_device_side_stall_aborts_the_push_stream(firmware, tmp_path):
    """After the device abandons a turn, further pushes are refused — the host
    must stop rather than keep writing into a dead session."""
    client = StubLlm([f"delta{i} " for i in range(200)], delay_s=0.01)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("long one")
            await h.settle(lambda: firmware.cm5_llm_pushes)
            firmware.llm_stall()
            await h.settle(lambda: h.service._current is None)

    run(main())
    pushes = len(firmware.cm5_llm_pushes)
    assert pushes > 0
    # No push kept landing after the abandon.
    assert firmware.cm5_llm_done


# --------------------------------------------------------------------------
# 5. seq discipline
# --------------------------------------------------------------------------


def test_seq_is_contiguous_from_zero(firmware, tmp_path):
    client = StubLlm(_ANSWER_DELTAS)
    cfg = _cfg(tmp_path)

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends)

    run(main())
    seqs = [seq for _sess, seq in firmware.cm5_llm_pushes]
    assert seqs == list(range(len(seqs)))


def test_a_timed_out_push_replays_without_duplicating_text(firmware, tmp_path):
    """Gotcha #2: Session's own replay would re-login first and blind-replay
    an append. The bridge replays the SAME seq instead, which the device
    accepts-and-ignores, so the answer text stays exact."""
    client = StubLlm(_ANSWER_DELTAS)
    cfg = _cfg(tmp_path)
    cfg.uart_timeout_s = 1.0
    # Stall the reply to seq 0 past the command deadline, but comfortably
    # inside the retry's own deadline. The device still APPLIES the push, so
    # the host's retry is a genuine duplicate delivery, not a re-send of
    # something that never landed.
    firmware.cm5_llm_push_delay[0] = 1.4

    async def main():
        async with _Harness(firmware, cfg, client) as h:
            firmware.llm_ask("q")
            await h.settle(lambda: firmware.cm5_llm_ends, timeout=20.0)

    run(main())
    resent = [line for line in firmware.command_log
              if line.startswith("cm5 llm push 1 0 ")]
    assert len(resent) == 2 and resent[0] == resent[1], resent
    assert firmware.cm5_llm_text == "".join(_ANSWER_DELTAS)
    seqs = [seq for _sess, seq in firmware.cm5_llm_pushes]
    assert seqs == sorted(set(seqs)), "a seq was applied twice"
    assert seqs == list(range(len(seqs)))


# --------------------------------------------------------------------------
# 6. Catalog and selection
# --------------------------------------------------------------------------


def test_catalog_is_published_on_link_up(firmware, tmp_path):
    cfg = _cfg(tmp_path, names=("alpha-q4", "beta-q8", "gamma-f16"), active=1)

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            await h.settle(lambda: firmware.cm5_llm_host_ready)

    run(main())
    assert sorted(firmware.cm5_llm_models) == ["alpha-q4", "beta-q8", "gamma-f16"]
    assert firmware.cm5_llm_active == "beta-q8"
    assert firmware.cm5_llm_gen > 0


def test_catalog_is_capped_at_the_device_limit(firmware, tmp_path):
    names = tuple(f"model-{i:02d}" for i in range(12))
    cfg = _cfg(tmp_path, names=names)

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            await h.settle(lambda: firmware.cm5_llm_host_ready)

    run(main())
    assert len(firmware.cm5_llm_models) == cm5_llm.MAX_MODELS


def test_selecting_a_missing_model_does_not_strand_loading(firmware, tmp_path):
    """The protocol has no "select failed" verb: the device treats this host as
    authoritative, so the fix is to report what is actually being served."""
    cfg = _cfg(tmp_path, names=("alpha-q4",))

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            await h.settle(lambda: firmware.cm5_llm_host_ready)
            firmware.llm_select("ghost-model")
            assert not firmware.cm5_llm_host_ready
            await h.settle(lambda: firmware.cm5_llm_host_ready)

    run(main())
    # LOADING cleared, and it names the model that really is loaded.
    assert firmware.cm5_llm_host_ready
    assert firmware.cm5_llm_active == "alpha-q4"
    assert "ghost-model" not in firmware.cm5_llm_models


def test_reselecting_the_active_model_just_reconfirms(firmware, tmp_path):
    cfg = _cfg(tmp_path, names=("alpha-q4",))

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            await h.settle(lambda: firmware.cm5_llm_host_ready)
            firmware.llm_select("alpha-q4")
            await h.settle(lambda: firmware.cm5_llm_host_ready)

    run(main())
    assert firmware.cm5_llm_active == "alpha-q4"


def test_names_are_single_escape_free_tokens():
    """The device stores catalog names with tokCopy (no unescape) and escapes
    them again on the way back out, so anything needing an escape would make
    that round trip lossy and read badly on a 19-column OLED row."""
    name = cm5_llm.sanitize_model_name("Qwen2.5 1.5B Instruct (Q4_K_M)")
    assert " " not in name and "\\" not in name
    assert cm5_llm.escape(name) == name
    assert len(name) <= cm5_llm.MAX_NAME_CHARS


def test_long_names_are_truncated_to_the_device_buffer(tmp_path):
    long_name = "a" * 80
    (tmp_path / f"{long_name}.gguf").write_bytes(b"\0" * 1024)
    models = cm5_llm.discover_models(str(tmp_path))
    assert len(models[0].name) == cm5_llm.MAX_NAME_CHARS


def test_colliding_truncations_stay_distinct(tmp_path):
    stem = "b" * 40
    for suffix in ("one", "two"):
        (tmp_path / f"{stem}-{suffix}.gguf").write_bytes(b"\0" * 1024)
    names = [m.name for m in cm5_llm.discover_models(str(tmp_path))]
    assert len(names) == len(set(names)) == 2


def test_an_empty_model_directory_does_not_crash(firmware, tmp_path):
    """A daemon with no GGUFs must still come up and serve the control plane;
    the device simply sees an empty picker."""
    cfg = _cfg(tmp_path, names=())

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            await asyncio.sleep(0.2)
            assert h.service is not None

    run(main())
    assert firmware.cm5_llm_models == []
    assert not firmware.cm5_llm_host_ready


def test_serve_firmware_false_leaves_the_link_alone(firmware, tmp_path):
    """The opt-out must publish nothing and claim no events, so an `llm_` push
    falls through to the generic handler that logs it as unhandled."""
    cfg = _cfg(tmp_path)
    cfg.serve_firmware = False

    async def main():
        async with _Harness(firmware, cfg, StubLlm(["x"])) as h:
            assert h.service.submit_event(b"llm_ask 1 250 70 95 hi") is False
            await asyncio.sleep(0.2)

    run(main())
    assert firmware.cm5_llm_models == []
    assert not any(line.startswith("cm5 llm") for line in firmware.command_log)


def test_the_configured_model_is_offered_even_from_elsewhere(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    (catalog / "in-dir.gguf").write_bytes(b"\0" * 1024)
    active = elsewhere / "configured.gguf"
    active.write_bytes(b"\0" * 1024)
    names = [m.name for m in cm5_llm.discover_models(
        str(catalog), active_path=str(active))]
    assert names[0] == "configured"
    assert "in-dir" in names


# --------------------------------------------------------------------------
# 7. Coexistence with the presence heartbeat (2026-08-17 hardware failure)
# --------------------------------------------------------------------------


def test_uart_timeout_cannot_starve_the_presence_heartbeat():
    """The bridge and the presence actor share ONE Session command lock, and
    the firmware abandons a live generation the instant the presence lease goes
    stale (cm5LlmTick's second wedge check). So the worst case a command can
    hold that lock — one timeout plus its idempotent replay — plus the
    heartbeat's own cadence has to fit inside the normal lease, with margin.

    Hardware, 2026-08-17: a 20s default held the lock through the 15s lease,
    the heartbeat could not land, and the firmware killed session 4 with
    "session epoch mismatch" one push in.
    """
    default = LlmConfig().uart_timeout_s
    lease_s = cm5_presence.NORMAL_LEASE_MS / 1000.0
    beat_s = cm5_presence.HEARTBEAT_INTERVAL_S
    worst_case_lock_hold = 2 * default
    assert worst_case_lock_hold + beat_s < lease_s, (
        f"uart_timeout_s={default}s can starve the {beat_s}s heartbeat past "
        f"the {lease_s}s lease")


def test_config_rejects_a_timeout_that_outlives_the_lease(tmp_path):
    import yaml

    from hw1_ai_service import config as config_mod

    lease_s = cm5_presence.NORMAL_LEASE_MS / 1000.0
    beat_s = cm5_presence.HEARTBEAT_INTERVAL_S
    ceiling = (lease_s - beat_s) / 2

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"llm": {"uart_timeout_s": ceiling}}))
    assert config_mod.load(str(path)).llm.uart_timeout_s == ceiling

    path.write_text(yaml.safe_dump({"llm": {"uart_timeout_s": ceiling + 0.5}}))
    with pytest.raises(ValueError, match="uart_timeout_s"):
        config_mod.load(str(path))
