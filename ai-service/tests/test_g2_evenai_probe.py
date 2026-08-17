from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


_TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "g2_evenai_probe.py"
_SPEC = importlib.util.spec_from_file_location("g2_evenai_probe", _TOOL_PATH)
assert _SPEC is not None and _SPEC.loader is not None
probe = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = probe
_SPEC.loader.exec_module(probe)

EXCHANGE_ID_1 = "1234567800000001"
EXCHANGE_ID_2 = "1234567800000002"
EXCHANGE_ID_3 = "1234567800000003"


class G2EvenAIProbeTests(unittest.TestCase):
    def test_cli_defaults_avoid_the_right_censored_render_window(self):
        parser = probe.build_parser()
        render = parser.parse_args(["render-ab"])
        speed = parser.parse_args(["speed-ab"])

        self.assertEqual(render.render_wait_ms, 20_000)
        self.assertEqual(speed.speeds, (80, 40, 80))
        self.assertEqual(speed.reply_text, "Probe complete")

    def test_speed_parser_rejects_zero_and_accepts_reversal(self):
        self.assertEqual(probe.parse_speeds("80,40,80"), (80, 40, 80))
        with self.assertRaises(argparse.ArgumentTypeError):
            probe.parse_speeds("80,0,80")

    def test_restore_no_longer_sends_a_config_packet(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True,
                    text=f"EvenAI session: active id={EXCHANGE_ID_1} arm=L gen=7",
                )
            return types.SimpleNamespace(ok=True, text="OK")

        with mock.patch.object(probe, "send", side_effect=fake_send):
            with redirect_stdout(io.StringIO()):
                asyncio.run(probe.restore(object()))

        self.assertEqual(
            sent,
            [
                "g2evenai status",
                f"g2evenai exitid {EXCHANGE_ID_1}",
                f"micrecord stopid {EXCHANGE_ID_1} discard",
            ],
        )

    def test_active_exchange_status_parser_is_strict_and_normalizes_hex(self):
        self.assertEqual(
            probe.parse_active_exchange_id(
                "OK: EvenAI session: active id=ABCDEF1200000009 arm=R gen=4"
            ),
            "abcdef1200000009",
        )
        self.assertIsNone(
            probe.parse_active_exchange_id(
                "EvenAI session: idle id=- arm=- gen=0 (hb=0, idle=0ms)"
            )
        )
        for malformed in (
            "OK",
            "EvenAI session: active id=1234 arm=L gen=1",
            "EvenAI session: active id=0000000000000001 arm=L gen=1",
            "EvenAI session: active id=1234567800000000 arm=L gen=1",
            f"EvenAI session: idle id={EXCHANGE_ID_1} arm=- gen=0",
        ):
            with self.subTest(malformed=malformed), self.assertRaises(RuntimeError):
                probe.parse_active_exchange_id(malformed)

    def test_stale_cleanup_does_not_exit_a_new_exchange(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            return types.SimpleNamespace(
                ok=True,
                text=f"EvenAI session: active id={EXCHANGE_ID_2} arm=R gen=8",
            )

        output = io.StringIO()
        with (
            mock.patch.object(probe, "send", side_effect=fake_send),
            redirect_stdout(output),
        ):
            exited = asyncio.run(
                probe.exit_active_evenai(object(), expected_id=EXCHANGE_ID_1)
            )

        self.assertFalse(exited)
        self.assertEqual(
            sent,
            [
                "g2evenai status",
                f"micrecord stopid {EXCHANGE_ID_1} discard",
            ],
        )
        self.assertIn("cleanup skipped", output.getvalue())

    def test_inactive_exchange_still_discards_expected_capture(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True,
                    text="EvenAI session: idle id=- arm=- gen=0",
                )
            return types.SimpleNamespace(ok=True, text="OK")

        with mock.patch.object(probe, "send", side_effect=fake_send):
            with redirect_stdout(io.StringIO()):
                exited = asyncio.run(
                    probe.exit_active_evenai(object(), expected_id=EXCHANGE_ID_1)
                )

        self.assertFalse(exited)
        self.assertEqual(
            sent,
            [
                "g2evenai status",
                f"micrecord stopid {EXCHANGE_ID_1} discard",
            ],
        )

    def test_capture_cleanup_does_not_mask_tagged_exit_error(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True,
                    text=f"EvenAI session: active id={EXCHANGE_ID_1} arm=L gen=7",
                )
            if line.startswith("g2evenai exitid "):
                raise RuntimeError("tagged exit failed")
            return types.SimpleNamespace(ok=True, text="OK")

        with mock.patch.object(probe, "send", side_effect=fake_send):
            with (
                redirect_stdout(io.StringIO()),
                self.assertRaisesRegex(RuntimeError, "tagged exit failed"),
            ):
                asyncio.run(
                    probe.exit_active_evenai(object(), expected_id=EXCHANGE_ID_1)
                )

        self.assertEqual(
            sent,
            [
                "g2evenai status",
                f"g2evenai exitid {EXCHANGE_ID_1}",
                f"micrecord stopid {EXCHANGE_ID_1} discard",
            ],
        )

    def test_logged_reply_parser_accepts_historic_and_tagged_records(self):
        legacy = "[225] [CMD] cm5@uart: g2evenai reply Probe complete -> OK"
        tagged = (
            f"[226] [CMD] cm5@uart: g2evenai replyid {EXCHANGE_ID_1} "
            "Probe complete -> OK"
        )

        self.assertEqual(
            probe._CMD_REPLY_RE.search(legacy).group("text"), "Probe complete"
        )
        self.assertEqual(
            probe._CMD_REPLY_RE.search(tagged).group("text"), "Probe complete"
        )
        self.assertIsNone(
            probe._CMD_REPLY_RE.search(
                "[227] [CMD] cm5@uart: g2evenai replyid bad Probe complete -> OK"
            )
        )

    def test_marker_counter_distinguishes_completion_from_command_echoes(self):
        content = "\n".join(
            (
                "[1] EvenAI CONFIG magic=1",
                "[2] EvenAI ASK magic=2",
                "[3] EvenAI REPLY magic=3",
                "[4] EvenAI EVENT type=STREAM_COMPLETE(2)",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "capture.log"
            path.write_text(content, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                counts = probe.print_protocol_markers(path)

        self.assertEqual(counts["EvenAI CONFIG"], 1)
        self.assertEqual(counts["EvenAI ASK"], 1)
        self.assertEqual(counts["EvenAI REPLY"], 1)
        self.assertEqual(counts["STREAM_COMPLETE"], 1)

    def test_speed_matrix_pairs_each_magic_and_condition(self):
        content = "\n".join(
            (
                "[100] [G2-R] EvenAI CONFIG magic=201 (9 B)",
                "[100] [G2-R]   body f13 bytes(2)=[10 50]",
                "[150] [G2-R] EvenAI ASK magic=209 (7 B)",
                "[175] [G2-R] EvenAI ANALYSE magic=210 (7 B)",
                "[190] [G2-R] TX env total=41 seq=0x01 len=33 1/1 sid=0x07 flag=0x20",
                "[200] [G2-R] EvenAI REPLY magic=210 (7 B)",
                "[225] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                "[1200] [G2-R] EvenAI EVENT type=STREAM_COMPLETE(2)",
                "[1300] [G2-R] EvenAI CTRL status=EXIT(3) magic=211",
                "[2000] [G2-R] EvenAI CONFIG magic=202 (9 B)",
                "[2000] [G2-R]   body f13 bytes(2)=[10 28]",
                "[2050] [G2-R] EvenAI ASK magic=211 (7 B)",
                "[2075] [G2-R] EvenAI ANALYSE magic=212 (7 B)",
                "[2090] [G2-R] TX env total=41 seq=0x02 len=33 1/1 sid=0x07 flag=0x20",
                "[2100] [G2-R] EvenAI REPLY magic=212 (7 B)",
                f"[2125] [CMD] cm5@uart: g2evenai replyid {EXCHANGE_ID_2} "
                "Probe complete -> OK",
                "[2650] [G2-R] EvenAI EVENT type=STREAM_COMPLETE(2)",
                "[2700] [G2-R] EvenAI CTRL status=EXIT(3) magic=213",
                "[3000] [G2-R] EvenAI CONFIG magic=203 (9 B)",
                "[3000] [G2-R]   body f13 bytes(2)=[10 50]",
                "[3050] [G2-R] EvenAI ASK magic=213 (7 B)",
                "[3075] [G2-R] EvenAI ANALYSE magic=214 (7 B)",
                "[3090] [G2-R] TX env total=41 seq=0x03 len=33 1/1 sid=0x07 flag=0x20",
                "[3100] [G2-R] EvenAI REPLY magic=214 (7 B)",
                "[3125] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                "[4100] [G2-R] EvenAI EVENT type=STREAM_COMPLETE(2)",
                "[4200] [G2-R] EvenAI CTRL status=EXIT(3) magic=215",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "speed.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path,
                (
                    probe.SpeedRequest(80, 201, "Probe complete"),
                    probe.SpeedRequest(40, 202, "Probe complete"),
                    probe.SpeedRequest(80, 203, "Probe complete"),
                ),
            )

        self.assertTrue(evidence.valid)
        self.assertEqual(
            [trial.response_to_completion_ms for trial in evidence.trials],
            [1000, 550, 1000],
        )
        self.assertEqual(
            [trial.tx_to_completion_ms for trial in evidence.trials],
            [1010, 560, 1010],
        )
        self.assertEqual(
            [trial.logged_reply_bytes for trial in evidence.trials],
            [14, 14, 14],
        )

    def test_speed_matrix_rejects_cross_boundary_completion_distribution(self):
        content = "\n".join(
            (
                "[100] EvenAI CONFIG magic=201",
                "[100] body f13 bytes(2)=[10 50]",
                "[150] EvenAI ASK magic=209",
                "[200] EvenAI REPLY magic=210",
                "[300] STREAM_COMPLETE",
                "[350] STREAM_COMPLETE",
                "[400] EvenAI CTRL status=EXIT(3)",
                "[500] EvenAI CONFIG magic=202",
                "[500] body f13 bytes(2)=[10 28]",
                "[550] EvenAI ASK magic=210",
                "[600] EvenAI REPLY magic=211",
                "[700] EvenAI CTRL status=EXIT(3)",
                "[750] STREAM_COMPLETE",
                "[800] EvenAI CONFIG magic=203",
                "[800] body f13 bytes(2)=[10 50]",
                "[850] EvenAI ASK magic=211",
                "[900] EvenAI REPLY magic=212",
                "[1000] STREAM_COMPLETE",
                "[1100] EvenAI CTRL status=EXIT(3)",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad-speed.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path,
                (
                    probe.SpeedRequest(80, 201),
                    probe.SpeedRequest(40, 202),
                    probe.SpeedRequest(80, 203),
                ),
            )

        self.assertFalse(evidence.valid)
        self.assertIn("got 2", " ".join(evidence.trials[0].issues))
        self.assertIn("got 0", " ".join(evidence.trials[1].issues))
        self.assertIn("after EXIT", " ".join(evidence.trials[1].issues))

    def test_speed_matrix_ignores_stale_prior_exit_before_its_ask(self):
        content = "\n".join(
            (
                "[100] EvenAI CONFIG magic=201",
                "[100] body f13 bytes(2)=[10 50]",
                "[125] EvenAI CTRL status=EXIT(3) magic=190",
                "[150] EvenAI ASK magic=202",
                "[175] EvenAI ANALYSE magic=203",
                "[190] TX env total=41 seq=0x01 len=33 1/1 sid=0x07 flag=0x20",
                "[200] EvenAI REPLY magic=203",
                "[300] STREAM_COMPLETE",
                "[400] EvenAI CTRL status=EXIT(3) magic=204",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale-exit.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path, (probe.SpeedRequest(80, 201),)
            )

        self.assertTrue(evidence.valid)
        self.assertEqual(evidence.trials[0].response_to_completion_ms, 100)
        self.assertEqual(evidence.trials[0].tx_to_completion_ms, 110)

    def test_speed_matrix_rejects_missing_echo_and_comm_rsp(self):
        content = "\n".join(
            (
                "[100] EvenAI COMM_RSP magic=201 errorCode=7",
                "[200] STREAM_COMPLETE",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "rejected-speed.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path, (probe.SpeedRequest(80, 201),)
            )

        self.assertFalse(evidence.valid)
        issues = " ".join(evidence.trials[0].issues)
        self.assertIn("got 0", issues)
        self.assertIn("COMM_RSP", issues)

    def test_speed_matrix_ignores_stale_prior_tx_and_uses_nearest_reply_tx(self):
        content = "\n".join(
            (
                "[50] TX env total=57 seq=0x01 len=49 1/1 sid=0x07 flag=0x20",
                "[100] EvenAI CONFIG magic=201",
                "[100] body f13 bytes(2)=[10 50]",
                "[150] EvenAI ASK magic=202",
                "[160] TX env total=41 seq=0x02 len=33 1/1 sid=0x07 flag=0x20",
                "[175] EvenAI ANALYSE magic=203",
                "[180] TX env total=19 seq=0x03 len=11 1/1 sid=0x07 flag=0x20",
                "[190] TX env total=41 seq=0x04 len=33 1/1 sid=0x07 flag=0x20",
                "[200] EvenAI REPLY magic=204",
                "[225] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                "[300] STREAM_COMPLETE",
                "[400] EvenAI CTRL status=EXIT(3) magic=205",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stale-tx.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path, (probe.SpeedRequest(80, 201, "Probe complete"),)
            )

        self.assertTrue(evidence.valid)
        self.assertEqual(evidence.trials[0].reply_tx_ms, 190)
        self.assertEqual(evidence.trials[0].reply_tx_total, 41)
        self.assertEqual(evidence.trials[0].tx_to_completion_ms, 110)
        printed = io.StringIO()
        with redirect_stdout(printed):
            probe.print_speed_matrix(evidence)
        self.assertIn("REPLY-TX(ms/B)", printed.getvalue())
        self.assertIn("190/41", printed.getvalue())
        self.assertIn("110 ms", printed.getvalue())

    def test_speed_matrix_warns_on_plugin_silence_and_carries_dead_state(self):
        content = "\n".join(
            (
                "[100] EvenAI CONFIG magic=201",
                "[100] body f13 bytes(2)=[10 50]",
                "[150] EvenAI ASK magic=202",
                "[175] EvenAI ANALYSE magic=203",
                "[190] TX env total=41 seq=0x01 len=33 1/1 sid=0x07 flag=0x20",
                "[200] EvenAI REPLY magic=204",
                "[225] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                "[300] STREAM_COMPLETE",
                "[325] LEFT temple plugin silent — 3 heartbeats unacked",
                '[326] [g2-status-TX] {"s":"connected","L":"dead","R":"up"}',
                "[400] EvenAI CTRL status=EXIT(3) magic=205",
                "[500] EvenAI CONFIG magic=206",
                "[500] body f13 bytes(2)=[10 28]",
                "[550] EvenAI ASK magic=207",
                "[575] EvenAI ANALYSE magic=208",
                "[590] TX env total=41 seq=0x02 len=33 1/1 sid=0x07 flag=0x20",
                "[600] EvenAI REPLY magic=209",
                "[625] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                "[700] STREAM_COMPLETE",
                "[800] EvenAI CTRL status=EXIT(3) magic=210",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plugin-silent.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path,
                (
                    probe.SpeedRequest(80, 201, "Probe complete"),
                    probe.SpeedRequest(40, 206, "Probe complete"),
                ),
            )

        self.assertTrue(evidence.valid)
        self.assertFalse(evidence.clean)
        for trial in evidence.trials:
            self.assertIn("LEFT temple plugin", " ".join(trial.warnings))

    def test_speed_matrix_rejects_missing_or_extra_analyse(self):
        for analyses in ((), ("[175] EvenAI ANALYSE magic=203", "[176] EvenAI ANALYSE magic=204")):
            with self.subTest(count=len(analyses)):
                content = "\n".join(
                    (
                        "[100] EvenAI CONFIG magic=201",
                        "[100] body f13 bytes(2)=[10 50]",
                        "[150] EvenAI ASK magic=202",
                        *analyses,
                        "[190] TX env total=41 seq=0x01 len=33 1/1 sid=0x07 flag=0x20",
                        "[200] EvenAI REPLY magic=205",
                        "[225] [CMD] cm5@uart: g2evenai reply Probe complete -> OK",
                        "[300] STREAM_COMPLETE",
                        "[400] EvenAI CTRL status=EXIT(3) magic=206",
                    )
                )
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "bad-analyse.log"
                    path.write_text(content, encoding="utf-8")
                    evidence = probe.analyze_speed_matrix(
                        path, (probe.SpeedRequest(80, 201, "Probe complete"),)
                    )

                self.assertFalse(evidence.valid)
                self.assertIn(
                    f"got {len(analyses)}", " ".join(evidence.trials[0].issues)
                )

    def test_speed_matrix_rejects_wrong_logged_reply_text(self):
        content = "\n".join(
            (
                "[100] EvenAI CONFIG magic=201",
                "[100] body f13 bytes(2)=[10 50]",
                "[150] EvenAI ASK magic=202",
                "[175] EvenAI ANALYSE magic=203",
                "[190] TX env total=41 seq=0x01 len=33 1/1 sid=0x07 flag=0x20",
                "[200] EvenAI REPLY magic=204",
                "[225] [CMD] cm5@uart: g2evenai reply Probe completf -> OK",
                "[300] STREAM_COMPLETE",
                "[400] EvenAI CTRL status=EXIT(3) magic=205",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wrong-text.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_speed_matrix(
                path, (probe.SpeedRequest(80, 201, "Probe complete"),)
            )

        self.assertFalse(evidence.valid)
        self.assertIn("logged reply text mismatch", " ".join(evidence.trials[0].issues))
        self.assertEqual(evidence.trials[0].logged_reply_bytes, 14)

    def test_render_matrix_pairs_sessions_instead_of_aggregate_counts(self):
        content = "\n".join(
            (
                "[100] EvenAI ASK magic=201",
                "[200] EvenAI REPLY magic=202",
                "[500] STREAM_COMPLETE",
                "[600] EvenAI CTRL status=EXIT(3)",
                "[700] EvenAI ASK magic=203",
                "[800] EvenAI REPLY magic=204",
                "[900] EvenAI REPLY magic=205",
                "[1000] EvenAI REPLY magic=206",
                "[1300] STREAM_COMPLETE",
                "[1400] EvenAI CTRL status=EXIT(3)",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "render.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_render_matrix(path, ("one-shot", "two-parts"))

        self.assertTrue(evidence.valid)
        self.assertEqual(
            [trial.final_response_to_completion_ms for trial in evidence.trials],
            [300, 300],
        )

    def test_render_matrix_rejects_late_cross_session_completion(self):
        content = "\n".join(
            (
                "[100] EvenAI ASK magic=201",
                "[200] EvenAI REPLY magic=202",
                "[300] EvenAI CTRL status=EXIT(3)",
                "[350] STREAM_COMPLETE",
                "[400] EvenAI ASK magic=203",
                "[500] EvenAI REPLY magic=204",
                "[600] EvenAI REPLY magic=205",
                "[700] EvenAI REPLY magic=206",
                "[800] STREAM_COMPLETE",
                "[900] EvenAI CTRL status=EXIT(3)",
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "late-render.log"
            path.write_text(content, encoding="utf-8")
            evidence = probe.analyze_render_matrix(path, ("one-shot", "two-parts"))

        self.assertFalse(evidence.valid)
        self.assertIn("after EXIT", " ".join(evidence.trials[0].issues))

    def test_health_guard_rejects_one_dead_temple(self):
        async def fake_send(_session, _line, **_kwargs):
            return types.SimpleNamespace(ok=True, text="OK: state=connected L=up R=down")

        with mock.patch.object(probe, "send", side_effect=fake_send):
            with self.assertRaisesRegex(RuntimeError, "state=connected"):
                asyncio.run(probe.require_healthy_temples(object()))

    def test_health_guard_rejects_connecting_with_both_temples_up(self):
        async def fake_send(_session, _line, **_kwargs):
            return types.SimpleNamespace(
                ok=True, text="OK: state=connecting L=up R=up"
            )

        with mock.patch.object(probe, "send", side_effect=fake_send):
            with self.assertRaisesRegex(RuntimeError, "state=connected"):
                asyncio.run(probe.require_healthy_temples(object()))

    def test_reconnect_waits_past_connecting_with_both_temples_up(self):
        statuses = iter(
            (
                "OK: state=connecting L=up R=up",
                "OK: state=connected L=up R=up",
            )
        )
        status_calls = 0

        async def fake_send(_session, line, **_kwargs):
            nonlocal status_calls
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True,
                    text="EvenAI session: idle id=- arm=- gen=0",
                )
            if line == "g2status":
                status_calls += 1
                return types.SimpleNamespace(ok=True, text=next(statuses))
            return types.SimpleNamespace(ok=True, text="OK")

        with (
            mock.patch.object(probe, "send", side_effect=fake_send),
            mock.patch.object(probe.asyncio, "sleep", new=mock.AsyncMock()),
        ):
            asyncio.run(probe.reconnect_g2(object()))

        self.assertEqual(status_calls, 2)

    def test_speed_ab_sends_only_field_two_and_uses_fresh_wakes(self):
        sent: list[str] = []
        wakes: list[str] = []
        next_magic = iter((201, 202, 203))
        wake_ids = iter((EXCHANGE_ID_1, EXCHANGE_ID_2, EXCHANGE_ID_3))
        current_id: str | None = None

        async def fake_send(_session, line, **_kwargs):
            nonlocal current_id
            sent.append(line)
            if line.startswith("g2aiconfig"):
                return types.SimpleNamespace(
                    ok=True, text=f"OK: aiconfig sent — magic={next(next_magic)}"
                )
            if line == "g2evenai status":
                text = (
                    f"EvenAI session: active id={current_id} arm=L gen=1"
                    if current_id is not None
                    else "EvenAI session: idle id=- arm=- gen=0"
                )
                return types.SimpleNamespace(ok=True, text=text)
            if line.startswith("g2evenai exitid "):
                current_id = None
            return types.SimpleNamespace(ok=True, text="OK")

        async def fake_wake(_session, *, attempt=""):
            nonlocal current_id
            wakes.append(attempt)
            current_id = next(wake_ids)
            return current_id

        async def no_sleep(_delay):
            return None

        async def fake_fetch(_session, _device_path, _output):
            return 1

        args = types.SimpleNamespace(
            question="Ready.",
            reply_text="Probe complete",
            speeds=(80, 40, 80),
            ask_settle_ms=2_000,
            render_wait_ms=3_500,
            device_log=None,
            output="/tmp/fake-speed-ab.log",
        )

        patches = (
            mock.patch.object(probe, "reconnect_g2", new=mock.AsyncMock()),
            mock.patch.object(
                probe,
                "start_protocol_log",
                new=mock.AsyncMock(return_value=("/device/log", True)),
            ),
            mock.patch.object(probe, "wait_for_native_wake", side_effect=fake_wake),
            mock.patch.object(probe, "require_healthy_temples", new=mock.AsyncMock()),
            mock.patch.object(probe, "send", side_effect=fake_send),
            mock.patch.object(probe.asyncio, "sleep", side_effect=no_sleep),
            mock.patch.object(probe, "fetch_file", side_effect=fake_fetch),
            mock.patch.object(
                probe,
                "print_protocol_markers",
                return_value={marker: (3 if marker in {
                                  "EvenAI CONFIG", "STREAM_COMPLETE"} else 0)
                for marker in probe._PROTOCOL_MARKERS},
            ),
            mock.patch.object(
                probe,
                "analyze_speed_matrix",
                return_value=probe.SpeedMatrixEvidence(
                    tuple(
                        probe.SpeedEvidence(
                            request=probe.SpeedRequest(
                                speed, magic, "Probe complete"
                            ),
                            echoed_speed=speed,
                            logged_reply_bytes=14,
                            reply_tx_ms=1,
                            reply_tx_total=41,
                            reply_response_ms=2,
                            completion_ms=3,
                            tx_to_completion_ms=2,
                            response_to_completion_ms=1,
                            issues=(),
                        )
                        for speed, magic in ((80, 201), (40, 202), (80, 203))
                    ),
                    (),
                ),
            ),
        )
        with (
            patches[0], patches[1], patches[2], patches[3], patches[4],
            patches[5], patches[6], patches[7], patches[8],
        ):
            with redirect_stdout(io.StringIO()):
                asyncio.run(probe.speed_ab(object(), args))

        configs = [line for line in sent if line.startswith("g2aiconfig")]
        self.assertEqual(
            configs,
            ["g2aiconfig - 80 -", "g2aiconfig - 40 -", "g2aiconfig - 80 -"],
        )
        self.assertEqual(wakes, ["streamSpeed=80", "streamSpeed=40", "streamSpeed=80"])
        self.assertEqual(
            [line for line in sent if line.startswith("g2evenai askid ")],
            [
                f"g2evenai askid {EXCHANGE_ID_1} Ready.",
                f"g2evenai askid {EXCHANGE_ID_2} Ready.",
                f"g2evenai askid {EXCHANGE_ID_3} Ready.",
            ],
        )
        self.assertEqual(
            [line for line in sent if line.startswith("g2evenai replyid ")],
            [
                f"g2evenai replyid {EXCHANGE_ID_1} Probe complete",
                f"g2evenai replyid {EXCHANGE_ID_2} Probe complete",
                f"g2evenai replyid {EXCHANGE_ID_3} Probe complete",
            ],
        )
        self.assertEqual(
            [line for line in sent if line.startswith("g2evenai exitid ")],
            [
                f"g2evenai exitid {EXCHANGE_ID_1}",
                f"g2evenai exitid {EXCHANGE_ID_2}",
                f"g2evenai exitid {EXCHANGE_ID_3}",
            ],
        )
        self.assertNotIn(probe.CONFIGS["80"], sent)
        self.assertIn("log stop", sent)

    def test_speed_ab_warns_and_closes_log_when_trial_aborts(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            if line.startswith("g2aiconfig"):
                return types.SimpleNamespace(ok=True, text="OK magic=201")
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True, text="EvenAI session: idle id=- arm=- gen=0"
                )
            return types.SimpleNamespace(ok=True, text="OK")

        async def abort_wake(_session, *, attempt=""):
            raise KeyboardInterrupt(attempt)

        args = types.SimpleNamespace(
            question="Ready.",
            reply_text="Probe complete",
            speeds=(80,),
            ask_settle_ms=2_000,
            render_wait_ms=3_500,
            device_log=None,
            output="/tmp/unused-speed-ab.log",
        )
        output = io.StringIO()
        with (
            mock.patch.object(probe, "reconnect_g2", new=mock.AsyncMock()),
            mock.patch.object(
                probe,
                "start_protocol_log",
                new=mock.AsyncMock(return_value=("/device/log", True)),
            ),
            mock.patch.object(probe, "require_healthy_temples", new=mock.AsyncMock()),
            mock.patch.object(probe, "wait_for_native_wake", side_effect=abort_wake),
            mock.patch.object(probe, "send", side_effect=fake_send),
            mock.patch.object(probe.asyncio, "sleep", new=mock.AsyncMock()),
            redirect_stdout(output),
            self.assertRaises(KeyboardInterrupt),
        ):
            asyncio.run(probe.speed_ab(object(), args))

        self.assertIn("log stop", sent)
        self.assertIn("RESET-STATE WARNING", output.getvalue())

    def test_speed_ab_refuses_analysis_when_log_did_not_close(self):
        sent: list[str] = []

        async def fake_send(_session, line, **_kwargs):
            sent.append(line)
            if line.startswith("g2aiconfig"):
                return types.SimpleNamespace(ok=True, text="OK magic=201")
            if line == "g2evenai status":
                return types.SimpleNamespace(
                    ok=True,
                    text=f"EvenAI session: active id={EXCHANGE_ID_1} arm=L gen=1",
                )
            return types.SimpleNamespace(ok=True, text="OK")

        args = types.SimpleNamespace(
            question="Ready.",
            reply_text="Probe complete",
            speeds=(80,),
            ask_settle_ms=0,
            render_wait_ms=0,
            device_log=None,
            output="/tmp/must-not-be-fetched.log",
        )
        fetch = mock.AsyncMock()
        with (
            mock.patch.object(probe, "reconnect_g2", new=mock.AsyncMock()),
            mock.patch.object(
                probe,
                "start_protocol_log",
                new=mock.AsyncMock(return_value=("/device/log", True)),
            ),
            mock.patch.object(probe, "require_healthy_temples", new=mock.AsyncMock()),
            mock.patch.object(
                probe,
                "wait_for_native_wake",
                new=mock.AsyncMock(return_value=EXCHANGE_ID_1),
            ),
            mock.patch.object(probe, "stop_protocol_log", new=mock.AsyncMock(return_value=False)),
            mock.patch.object(probe, "send", side_effect=fake_send),
            mock.patch.object(probe.asyncio, "sleep", new=mock.AsyncMock()),
            mock.patch.object(probe, "fetch_file", new=fetch),
            redirect_stdout(io.StringIO()),
            self.assertRaisesRegex(RuntimeError, "refusing to fetch/analyze"),
        ):
            asyncio.run(probe.speed_ab(object(), args))

        fetch.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
