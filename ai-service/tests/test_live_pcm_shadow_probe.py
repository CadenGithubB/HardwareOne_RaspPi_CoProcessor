"""Phase-2B recorder-shadow validation stays standalone and fail-closed."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import serial

from conftest import run
from fake_firmware import make_wav
from hw1_ai_service.link.transport import SerialTransport


_PROBE_PATH = Path(__file__).parents[1] / "tools" / "live_pcm_shadow_probe.py"
_PROBE_SPEC = importlib.util.spec_from_file_location(
    "live_pcm_shadow_probe", _PROBE_PATH)
assert _PROBE_SPEC is not None and _PROBE_SPEC.loader is not None
probe = importlib.util.module_from_spec(_PROBE_SPEC)
_PROBE_SPEC.loader.exec_module(probe)


CONTROLLER = 0xC0DEC0DE00000001
EXCHANGE = 0xA1B2C3D400000001
BEGIN_EXCHANGE = 0xA1B2C3D400000002


def _args(**overrides):
    values = {
        "config": "unused.yaml",
        "expected_source": "pdm",
        "record_seconds": 0.05,
        "controller_id": CONTROLLER,
        "exchange_id": EXCHANGE,
        "max_queue_bytes": 16 * 1024,
        "max_queue_frames": 32,
        "fault": probe.FAULT_NONE,
        "fault_after_ms": 0,
        "output_dir": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _native_args(**overrides):
    values = {
        "config": "unused.yaml",
        "expected_source": "g2",
        "controller_id": CONTROLLER,
        "wake_timeout": 2.0,
        "capture_timeout": 3.0,
        "max_queue_bytes": 16 * 1024,
        "max_queue_frames": 32,
        "output_dir": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _after_native_arm(firmware, action):
    def worker():
        deadline = time.monotonic() + 2.0
        while not firmware.live_shadow_armed:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)
        action()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class _RecordingSttObserver:
    def __init__(self, text="what is the capital of france", *, valid=True):
        self.text = text
        self.valid = valid
        self.begin = None
        self.pcm = bytearray()
        self.ended = False
        self.aborted = None

    def on_begin(self, begin):
        self.begin = dict(begin)

    def offer_pcm(self, pcm):
        self.pcm.extend(pcm)
        return True

    def end_input(self):
        self.ended = True

    def abort(self, reason):
        self.aborted = reason

    def wait(self, _timeout):
        return {
            "valid": self.valid,
            "valid_empty": self.valid and not self.text,
            "done": True,
            "text": self.text,
            "failure_reasons": [] if self.valid else ["fake_stt_failure"],
            "stream": {"end_to_final_seconds": 0.4},
            "queue": {"capacity_chunks": 8, "capacity_ms": 1024},
        }


class _FakeLiveWorker(_RecordingSttObserver):
    def __init__(self, factory, *, update_interval_s, queue_chunks,
                 text_queue_events):
        super().__init__()
        self.factory = factory
        self.update_interval_s = update_interval_s
        self.queue_chunks = queue_chunks
        self.text_queue_events = text_queue_events
        self.start_timeout = None

    def start(self, timeout):
        self.start_timeout = timeout


def _patch_probe_config(monkeypatch, firmware):
    cfg = SimpleNamespace(
        link=SimpleNamespace(
            port=firmware._slave_path,
            baud=921_600,
            credentials_file="unused.credentials",
        ),
        audio=SimpleNamespace(vad_max_seconds=3.0),
    )
    monkeypatch.setattr(probe.config_mod, "load", lambda _: cfg)
    monkeypatch.setattr(
        probe.config_mod, "read_credentials",
        lambda _: (firmware.user, firmware.password))

    # macOS pseudo-TTYs do not accept the production custom-baud ioctl.  Keep
    # the configured baud admission check but use a conventional pty rate.
    def pty_transport(port, baud, *, frame_sink):
        ser = serial.Serial(
            port, 115_200, timeout=0.05, write_timeout=2.0)
        return SerialTransport(serial_obj=ser, frame_sink=frame_sink)

    monkeypatch.setattr(probe, "SerialTransport", pty_transport)


def _assert_no_production_pipeline_commands(firmware):
    assert not any(command.startswith("g2evenai ")
                   for command in firmware.command_log)
    assert not any(command.startswith("oledtext")
                   for command in firmware.command_log)


def test_native_cli_is_g2_only_and_defers_capture_timeout_to_config():
    args = probe.build_parser().parse_args(["native"])
    assert args.expected_source == "g2"
    assert args.capture_timeout is None
    with pytest.raises(SystemExit):
        probe.build_parser().parse_args(
            ["native", "--expected-source", "pdm"])


def test_native_stt_cli_defaults_to_bounded_one_second_shadow():
    args = probe.build_parser().parse_args([
        "native-stt",
        "--model-dir", "/tmp/model",
        "--expected-text", "what time is it",
        "--output-dir", "/tmp/evidence",
    ])
    assert args.expected_source == "g2"
    assert args.model_arch == "medium-streaming"
    assert args.update_interval == 1.0
    assert args.stt_queue_chunks == 8
    assert args.stt_soft_final_target == 0.8
    assert args.stt_final_timeout == 2.0


def test_native_stt_gate_requires_performance_before_opening_uart(
        firmware, monkeypatch):
    _patch_probe_config(monkeypatch, firmware)
    monkeypatch.setattr(probe, "performance_governors", lambda: ["powersave"])
    args = _native_args(
        allow_non_performance=False,
        expected_text="what time is it",
        stt_final_timeout=2.0,
        stt_soft_final_target=0.8,
        model_dir="/fake/model",
        model_arch="medium-streaming",
        update_interval=1.0,
        stt_queue_chunks=8,
        stt_text_queue_events=64,
        model_startup_timeout=120.0,
    )

    with pytest.raises(RuntimeError, match="requires every CPU governor"):
        run(probe.run_native_stt_probe(args))

    assert firmware.command_log == []


def test_native_stt_runner_constructs_worker_with_canonical_defaults(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)
    monkeypatch.setattr(probe, "performance_governors", lambda: ["performance"])
    factory = object()
    monkeypatch.setattr(probe, "exact_moonshine_factory", lambda *_: factory)
    made = []

    def make_worker(*args, **kwargs):
        worker = _FakeLiveWorker(*args, **kwargs)
        made.append(worker)
        return worker

    monkeypatch.setattr(probe, "LiveMoonshineWorker", make_worker)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))
    args = _native_args(
        allow_non_performance=False,
        expected_text="what is the capital of france",
        stt_final_timeout=2.0,
        stt_soft_final_target=0.8,
        model_dir="/fake/model",
        model_arch="medium-streaming",
        update_interval=1.0,
        stt_queue_chunks=8,
        stt_text_queue_events=64,
        model_startup_timeout=120.0,
    )

    result = run(probe.run_native_stt_probe(args))
    trigger.join(timeout=1)

    assert result["ok"], result
    assert len(made) == 1
    assert made[0].factory is factory
    assert made[0].update_interval_s == 1.0
    assert made[0].queue_chunks == 8
    assert made[0].text_queue_events == 64
    assert made[0].start_timeout == 120.0


@pytest.mark.parametrize("source", ["pdm", "g2"])
def test_owned_shadow_matches_finalized_wav_and_cleans_exact_id(
        firmware, monkeypatch, source):
    firmware.mic_source = source
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args(expected_source=source)))

    assert result["ok"], result
    assert result["stt_started"] is False
    assert result["mic"]["source"] == source
    assert result["live"]["terminal"]["kind"] == "end"
    assert result["live"]["terminal"]["valid"] is True
    assert result["parity"] == {
        "pcm_equal": True,
        "receiver_matches_wav": True,
        "terminal_matches_wav": True,
    }
    assert result["live"]["bytes"] == result["wav"]["pcm_bytes"]
    assert result["live"]["crc32"] == result["wav"]["crc32"]
    assert firmware.live_controller_id is None
    assert not firmware.live_shadow_armed
    assert f"rec_{EXCHANGE:016x}.wav" in firmware.deleted
    assert (
        f"liveaudio shadow 1 {CONTROLLER:016x} on {EXCHANGE:016x}"
        in firmware.command_log)
    assert f"micrecord startid {EXCHANGE:016x}" in firmware.command_log
    assert f"micrecord stopid {EXCHANGE:016x}" in firmware.command_log
    assert (f'micdeleteid {EXCHANGE:016x} "rec_{EXCHANGE:016x}.wav"'
            in firmware.command_log)
    _assert_no_production_pipeline_commands(firmware)


def test_actual_mic_source_mismatch_fails_before_recording_and_disarms(
        firmware, monkeypatch):
    firmware.mic_source = "pdm"
    _patch_probe_config(monkeypatch, firmware)

    with pytest.raises(RuntimeError, match="active source='pdm', expected='g2'"):
        run(probe.run_owned_probe(_args(expected_source="g2")))

    assert f"micrecord startid {EXCHANGE:016x}" not in firmware.command_log
    assert not any(command.startswith("liveaudio shadow ")
                   for command in firmware.command_log)
    assert firmware.live_controller_id is None
    assert not firmware.live_shadow_armed
    _assert_no_production_pipeline_commands(firmware)


def test_corrupt_live_terminal_fails_gate_but_wav_remains_authoritative(
        firmware, monkeypatch):
    firmware.live_shadow_terminal_crc_xor = 1
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args()))

    assert not result["ok"]
    assert result["wav"]["canonical"] is True
    # Invalidating a terminal deliberately purges any queued PCM that the
    # consumer has not taken yet, so byte-for-byte consumer parity may depend
    # on scheduling. Receiver accounting is durable and proves every PCM
    # frame itself matched the finalized WAV before the corrupt terminal.
    assert result["parity"]["receiver_matches_wav"] is True, result
    assert result["live"]["terminal"]["valid"] is False
    assert str(result["live"]["terminal"]["reason"]).startswith("end_crc32:")
    assert firmware.live_controller_id is None
    assert not firmware.live_shadow_armed
    assert f"rec_{EXCHANGE:016x}.wav" in firmware.deleted
    _assert_no_production_pipeline_commands(firmware)


def test_missing_live_frame_fails_gate_then_quiesces_before_wav_fetch(
        firmware, monkeypatch):
    firmware.live_shadow_drop_frame_index = 1
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args()))

    assert not result["ok"]
    assert result["wav"]["canonical"] is True
    assert result["parity"]["pcm_equal"] is False
    assert result["parity"]["receiver_matches_wav"] is False
    assert result["live"]["terminal"]["valid"] is False
    assert str(result["live"]["terminal"]["reason"]).startswith("wire_seq:")
    assert result["quiescence"]["active"] == "0"
    assert result["quiescence"]["exchange"] == "-"
    status_index = max(
        i for i, command in enumerate(firmware.command_log)
        if command == "liveaudio status")
    fetch_index = next(
        i for i, command in enumerate(firmware.command_log)
        if command.startswith("voicefetch "))
    assert status_index < fetch_index
    assert f"rec_{EXCHANGE:016x}.wav" in firmware.deleted
    _assert_no_production_pipeline_commands(firmware)


@pytest.mark.parametrize("fault,expected_reason", [
    (probe.FAULT_HOST_OVERFLOW, "pcm_queue_overflow"),
    (probe.FAULT_HOST_GAP, "wire_seq:3!=2"),
])
def test_expected_host_receiver_fault_keeps_canonical_wav_and_device_end(
        firmware, monkeypatch, fault, expected_reason):
    _patch_probe_config(monkeypatch, firmware)
    # Frame-level host faults ignore the control-fault delay, even when it is
    # longer than this deliberately short fake recording.
    result = run(probe.run_owned_probe(_args(
        fault=fault, fault_after_ms=250)))

    assert result["ok"], result
    assert result["fault"]["injected"] is True
    assert result["fault"]["expected_outcome"] is True
    assert result["fault"]["fallback_wav_prefix"] is True
    assert result["wav"]["canonical"] is True
    reason = str(result["live"]["terminal"]["reason"])
    assert reason == expected_reason
    assert "state=end" in result["quiescence"]["text"]
    assert f"rec_{EXCHANGE:016x}.wav" in firmware.deleted
    _assert_no_production_pipeline_commands(firmware)


def test_expected_host_abort_validates_prefix_and_keeps_canonical_wav(
        firmware, monkeypatch):
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args(fault=probe.FAULT_HOST_ABORT)))

    assert result["ok"], result
    assert result["fault"]["injected"] is True
    assert result["fault"]["expected_outcome"] is True
    assert result["live"]["terminal"]["kind"] == "abort"
    assert (result["live"]["terminal"]["reason"] ==
            probe.protocol.LIVE_ABORT_REASON_HOST_REQUEST)
    assert result["wav"]["canonical"] is True
    assert result["fault"]["fallback_wav_prefix"] is True
    _assert_no_production_pipeline_commands(firmware)


def test_self_consistent_corrupt_abort_prefix_cannot_pass_wav_fallback_gate(
        firmware, monkeypatch):
    firmware.live_shadow_pcm_xor = 0x01
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args(fault=probe.FAULT_HOST_ABORT)))

    assert not result["ok"], result
    assert result["live"]["terminal"]["kind"] == "abort"
    assert result["fault"]["terminal_prefix_matches_wav"] is False
    assert result["wav"]["canonical"] is True


def test_expected_lease_expiry_releases_expired_lease_after_wav_fallback(
        firmware, monkeypatch):
    firmware.live_lease_ttl_s = 0.5
    firmware.live_renew_direct = False
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args(
        fault=probe.FAULT_LEASE_EXPIRE, record_seconds=0.7)))

    assert result["ok"], result
    assert result["fault"]["injected"] is True
    assert result["live"]["terminal"]["kind"] == "abort"
    assert (result["live"]["terminal"]["reason"] ==
            probe.protocol.LIVE_ABORT_REASON_LEASE_EXPIRED)
    assert result["wav"]["canonical"] is True
    assert result["fault"]["fallback_wav_prefix"] is True
    assert f"liveaudio release 1 {CONTROLLER:016x}" in firmware.command_log
    assert firmware.live_controller_id is None
    _assert_no_production_pipeline_commands(firmware)


def test_lease_expiry_rejects_window_shorter_than_late_injection_plus_ttl(
        firmware, monkeypatch):
    firmware.live_lease_ttl_s = 0.2
    firmware.live_renew_direct = False
    _patch_probe_config(monkeypatch, firmware)

    with pytest.raises(RuntimeError, match="recording window must extend"):
        run(probe.run_owned_probe(_args(
            fault=probe.FAULT_LEASE_EXPIRE,
            fault_after_ms=100,
            record_seconds=0.15)))

    assert f"micrecord startid {EXCHANGE:016x}" not in firmware.command_log


def test_host_gap_rejects_stale_prior_end_status_for_another_exchange(
        firmware, monkeypatch):
    firmware.live_last_terminal = "end"
    firmware.live_last_exchange = 0xDEADBEEF00000001
    firmware.live_last_sent = len(firmware._wav_pcm(firmware.wav_bytes)) // 2
    firmware.live_last_crc32 = probe.protocol.crc32_ieee(
        firmware._wav_pcm(firmware.wav_bytes))
    firmware.live_last_terminal_sent = True
    firmware.live_shadow_suppress_last_update = True
    _patch_probe_config(monkeypatch, firmware)

    result = run(probe.run_owned_probe(_args(fault=probe.FAULT_HOST_GAP)))

    assert not result["ok"], result
    assert result["fault"]["device_end_matches_wav"] is False
    assert result["wav"]["canonical"] is True


def test_native_wake_correlates_firmware_id_end_status_path_and_cleanup(
        firmware, monkeypatch, tmp_path):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    result = run(probe.run_native_probe(_native_args(
        output_dir=str(tmp_path))))
    trigger.join(timeout=1)

    assert result["ok"], result
    assert result["mode"] == "native_recorder_shadow_smoke"
    assert result["exchange_id"] == f"{EXCHANGE:016x}"
    assert result["begin"]["exchange_id"] == f"{EXCHANGE:016x}"
    assert result["begin"]["controller_id"] == f"{CONTROLLER:016x}"
    assert result["native"]["active"]["exchange_id"] == f"{EXCHANGE:016x}"
    assert result["native"]["active"]["uart_epoch"] == 1
    assert result["device_path"].endswith(f"rec_{EXCHANGE:016x}.wav")
    assert result["native"]["mic_autostop"] == result["device_path"]
    assert result["live"]["terminal"]["kind"] == "end"
    assert result["live"]["terminal"]["valid"] is True
    assert result["live"]["terminal"]["reason"] == 0
    assert result["live"]["terminal"]["dropped_samples"] == 0
    assert result["live"]["status_matches_terminal"] is True
    assert result["live"]["samples"] > result["wav"]["samples"]
    assert result["wav"]["canonical"] is True
    assert result["parity"] == {
        "applicable": False,
        "reason": "native_capture_trim_enabled",
        "pcm_equal": None,
    }
    assert result["stt_started"] is False
    assert result["llm_started"] is False
    assert result["ask_sent"] is False
    assert result["reply_sent"] is False
    assert result["cleanup_order"] == [
        "shadow_off", "lease_release", "voicefetch",
        "micdeleteid", "g2evenai_exitid",
    ]
    assert result["cleanup"] == {
        "wav_deleted": True,
        "evenai_exited": True,
        "shadow_disarmed": True,
        "lease_released": True,
    }
    assert firmware.native_emit_order.index("live_begin") < \
        firmware.native_emit_order.index("evenai_wake")
    assert firmware.native_emit_order.index("live_terminal") < \
        firmware.native_emit_order.index("mic_autostop")
    texts = [entry["text"] for entry in result["native"]["events"]]
    assert f"evenai_wake {EXCHANGE:016x}" in texts
    assert any(text == f"mic_autostop {EXCHANGE:016x} {result['device_path']}"
               for text in texts)
    assert f"evenai_cancel {EXCHANGE:016x} host_exit" in texts
    assert f"rec_{EXCHANGE:016x}.wav" in firmware.deleted
    assert not firmware.live_shadow_armed
    assert firmware.live_controller_id is None
    assert firmware.evenai_active is False

    commands = firmware.command_log
    assert commands.index(f"liveaudio shadow 1 {CONTROLLER:016x} off") < \
        commands.index(f"liveaudio release 1 {CONTROLLER:016x}") < \
        next(i for i, cmd in enumerate(commands) if cmd.startswith("voicefetch ")) < \
        commands.index(
            f'micdeleteid {EXCHANGE:016x} "rec_{EXCHANGE:016x}.wav"') < \
        commands.index(f"g2evenai exitid {EXCHANGE:016x}")
    assert not any(cmd.startswith((
        "micrecord start", "g2evenai ask", "g2evenai reply"))
        for cmd in commands)
    saved_live = Path(result["local_paths"]["live_pcm"]).read_bytes()
    assert saved_live
    assert saved_live != firmware._wav_pcm(firmware.wav_bytes)
    assert Path(result["local_paths"]["wav"]).read_bytes() == firmware.wav_bytes


def test_native_live_stt_observer_receives_full_untrimmed_pcm_and_gates_words(
        firmware, monkeypatch, tmp_path):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)
    observer = _RecordingSttObserver()
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    result = run(probe.run_native_probe(
        _native_args(
            expected_text="What is the capital of France?",
            stt_final_timeout=2.0,
            stt_soft_final_target=0.8,
            output_dir=str(tmp_path)),
        pcm_observer=observer))
    trigger.join(timeout=1)

    assert result["ok"], result
    assert result["mode"] == "native_live_stt_shadow"
    assert result["stt_started"] is True
    assert result["llm_started"] is False
    assert result["ask_sent"] is False
    assert result["reply_sent"] is False
    assert observer.begin["exchange_id"] == f"{EXCHANGE:016x}"
    assert observer.ended is True
    assert observer.aborted is None
    assert bytes(observer.pcm) == Path(
        result["local_paths"]["live_pcm"]).read_bytes()
    assert result["streaming_stt"]["valid"] is True
    assert result["streaming_stt"]["accuracy"]["word_errors"] == 0
    assert result["streaming_stt"]["accuracy"]["exact_words"] is True
    assert result["streaming_stt"]["final_policy"] == {
        "soft_target_seconds": 0.8,
        "hard_timeout_seconds": 2.0,
        "soft_target_met": True,
    }
    assert not any(command.startswith((
        "g2evenai ask", "g2evenai reply", "oledtext"))
        for command in firmware.command_log)


def test_native_live_stt_mismatch_fails_model_gate_but_keeps_exact_cleanup(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)
    observer = _RecordingSttObserver(text="yeah")
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    result = run(probe.run_native_probe(
        _native_args(
            expected_text="what is the capital of france",
            stt_final_timeout=2.0,
            stt_soft_final_target=0.8),
        pcm_observer=observer))
    trigger.join(timeout=1)

    assert result["ok"] is False
    assert result["streaming_stt"]["valid"] is True
    assert result["streaming_stt"]["accuracy"]["exact_words"] is False
    assert result["cleanup"]["wav_deleted"] is True
    assert result["cleanup"]["evenai_exited"] is True
    assert firmware.evenai_active is False
    assert firmware.live_controller_id is None


def test_native_rejects_active_pdm_before_shadow_arm(firmware, monkeypatch):
    firmware.mic_source = "pdm"
    _patch_probe_config(monkeypatch, firmware)

    with pytest.raises(RuntimeError, match="active source='pdm', expected='g2'"):
        run(probe.run_native_probe(_native_args()))

    assert not any(command.endswith(" on native")
                   for command in firmware.command_log)
    assert not any(command.startswith("voicefetch ")
                   for command in firmware.command_log)


def test_native_rejects_foreign_live_stream_before_ready_or_arm(
        firmware, monkeypatch):
    firmware.live_exchange_id = BEGIN_EXCHANGE
    _patch_probe_config(monkeypatch, firmware)

    with pytest.raises(RuntimeError, match="preflight is not idle/bulk-free"):
        run(probe.run_native_probe(_native_args()))

    assert not any(command.startswith("liveaudio ready ")
                   for command in firmware.command_log)
    assert not any(command.endswith(" on native")
                   for command in firmware.command_log)


def test_native_rejects_active_uart_epoch_mismatch_before_fetch(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.evenai_uart_epoch = 2
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    with pytest.raises(RuntimeError, match="does not match wake/login epoch"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert not any(command.startswith("voicefetch ")
                   for command in firmware.command_log)
    # A candidate from a different login epoch is never used to mutate the
    # current EvenAI card, even though recorder cleanup remains exact-ID-only.
    assert f"g2evenai exitid {EXCHANGE:016x}" not in firmware.command_log


def test_native_rejects_zero_length_live_terminal_before_fetch(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.wav_bytes = make_wav(seconds=0)
    firmware.native_trim_padding_samples = 0
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    with pytest.raises(RuntimeError, match="did not end with exact count/CRC"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert not any(command.startswith("voicefetch ")
                   for command in firmware.command_log)


def test_native_rejects_canonical_zero_pcm_wav_after_nonempty_live(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.wav_bytes = make_wav(seconds=0)
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    with pytest.raises(RuntimeError, match="canonical WAV contains no PCM"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert any(command.startswith("voicefetch ")
               for command in firmware.command_log)


def test_native_rejects_foreign_prebegin_frame_even_when_good_stream_ends(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.native_prebegin_foreign_exchange_id = f"{BEGIN_EXCHANGE:016x}"
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    with pytest.raises(RuntimeError, match="live inbox recorded"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert not any(command.startswith("voicefetch ")
                   for command in firmware.command_log)


def test_native_wake_begin_id_mismatch_cleans_both_exact_candidates(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.native_begin_exchange_id = f"{BEGIN_EXCHANGE:016x}"
    firmware.vad_auto_stop_after = None
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    with pytest.raises(RuntimeError, match="identity candidates disagree"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert f"g2evenai exitid {EXCHANGE:016x}" in firmware.command_log
    assert (f"micrecord stopid {BEGIN_EXCHANGE:016x} discard"
            in firmware.command_log)
    assert "g2evenai exit" not in firmware.command_log
    assert "micrecord stop" not in firmware.command_log


def test_native_both_signals_lost_adopts_same_epoch_status_for_exact_cleanup(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.vad_auto_stop_after = None
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(
            exchange_id=f"{EXCHANGE:016x}", push=False, start_shadow=False))

    loop_errors: list[dict] = []

    async def scenario():
        asyncio.get_running_loop().set_exception_handler(
            lambda _loop, context: loop_errors.append(context))
        with pytest.raises(RuntimeError, match="evenai_wake|LIVE_BEGIN"):
            await probe.run_native_probe(_native_args(wake_timeout=1.0))
        # Give task finalizers a loop turn; an unretrieved sibling timeout used
        # to surface here as "Task exception was never retrieved".
        await asyncio.sleep(0)

    run(scenario())
    trigger.join(timeout=1)

    assert loop_errors == []
    assert f"g2evenai exitid {EXCHANGE:016x}" in firmware.command_log
    assert f"micrecord stopid {EXCHANGE:016x} discard" in firmware.command_log
    assert "g2evenai exit" not in firmware.command_log
    assert "micrecord stop" not in firmware.command_log


def test_native_replacement_wake_same_epoch_exits_replacement_and_discards_all(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.vad_auto_stop_after = None
    _patch_probe_config(monkeypatch, firmware)

    def trigger_replacement():
        firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}")
        deadline = time.monotonic() + 1.0
        while firmware.command_log.count("g2evenai status") < 2:
            if time.monotonic() >= deadline:
                return
            time.sleep(0.005)
        time.sleep(0.05)
        with firmware._lock:
            firmware.evenai_exchange_id = f"{BEGIN_EXCHANGE:016x}"
            firmware._recording_owner = f"{BEGIN_EXCHANGE:016x}"
        firmware.push_event(f"evenai_wake {BEGIN_EXCHANGE:016x}")

    trigger = _after_native_arm(firmware, trigger_replacement)
    with pytest.raises(RuntimeError, match="replacement|superseded|owner mismatch"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert f"g2evenai exitid {BEGIN_EXCHANGE:016x}" in firmware.command_log
    assert f"micrecord stopid {EXCHANGE:016x} discard" in firmware.command_log
    assert (f"micrecord stopid {BEGIN_EXCHANGE:016x} discard"
            in firmware.command_log)
    assert "g2evenai exit" not in firmware.command_log
    assert "micrecord stop" not in firmware.command_log
    assert not any(command.startswith("voicefetch ")
                   for command in firmware.command_log)


def test_native_accepts_wake_before_begin_and_autostop_before_end(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.native_wake_before_begin = True
    firmware.native_autostop_before_end = True
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    result = run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert result["ok"], result
    assert firmware.native_emit_order.index("evenai_wake") < \
        firmware.native_emit_order.index("live_begin")
    assert firmware.native_emit_order.index("mic_autostop") < \
        firmware.native_emit_order.index("live_terminal")
    assert result["parity"]["applicable"] is False


def test_native_ignores_foreign_cancel_tombstone_before_new_wake(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)
    foreign = "deadbeef00000002"

    def trigger_wake():
        firmware.push_event(f"evenai_cancel {foreign} dismiss")
        firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}")

    trigger = _after_native_arm(firmware, trigger_wake)
    result = run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert result["ok"], result
    assert any(entry["text"] == f"evenai_cancel {foreign} dismiss"
               for entry in result["native"]["events"])


def test_native_rejects_same_id_cancel_tombstone_before_wake(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    _patch_probe_config(monkeypatch, firmware)

    def trigger_wake():
        firmware.push_event(f"evenai_cancel {EXCHANGE:016x} dismiss")
        firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}")

    trigger = _after_native_arm(firmware, trigger_wake)
    with pytest.raises(RuntimeError, match="already cancelled before wake"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert not any(cmd.startswith("voicefetch ")
                   for cmd in firmware.command_log)
    assert not any(cmd.startswith(("g2evenai ask", "g2evenai reply"))
                   for cmd in firmware.command_log)


def test_native_fails_closed_on_lost_cancel_discard_status(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.vad_auto_stop_after = None
    _patch_probe_config(monkeypatch, firmware)

    def trigger_and_discard():
        firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}")
        deadline = time.monotonic() + 1.0
        while firmware.command_log.count("g2evenai status") < 2:
            if time.monotonic() >= deadline:
                break
            time.sleep(0.005)
        firmware.dismiss_evenai("dismiss", push=False)

    trigger = _after_native_arm(firmware, trigger_and_discard)
    with pytest.raises(RuntimeError, match="discarded"):
        run(probe.run_native_probe(_native_args()))
    trigger.join(timeout=1)

    assert not any(cmd.startswith("voicefetch ")
                   for cmd in firmware.command_log)


def test_native_stopped_without_mic_autostop_fails_after_short_grace(
        firmware, monkeypatch):
    firmware.mic_source = "g2"
    firmware.push_mic_autostop = False
    _patch_probe_config(monkeypatch, firmware)
    trigger = _after_native_arm(
        firmware,
        lambda: firmware.begin_wake_capture(exchange_id=f"{EXCHANGE:016x}"))

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="stopped without exact mic_autostop"):
        run(probe.run_native_probe(_native_args(capture_timeout=6.0)))
    elapsed = time.monotonic() - started
    trigger.join(timeout=1)

    assert elapsed < 4.0
    assert not any(cmd.startswith("voicefetch ")
                   for cmd in firmware.command_log)


@pytest.mark.parametrize("missing", ["begin", "wake"])
def test_native_missing_identity_half_uses_only_candidate_exact_cleanup(
        firmware, monkeypatch, missing):
    firmware.mic_source = "g2"
    firmware.vad_auto_stop_after = None
    _patch_probe_config(monkeypatch, firmware)

    if missing == "begin":
        action = lambda: firmware.begin_wake_capture(
            exchange_id=f"{EXCHANGE:016x}", start_shadow=False)
        error = "LIVE_BEGIN"
    else:
        action = lambda: firmware.begin_wake_capture(
            exchange_id=f"{EXCHANGE:016x}", push=False)
        error = "evenai_wake"
    trigger = _after_native_arm(firmware, action)

    with pytest.raises(RuntimeError, match=error):
        run(probe.run_native_probe(_native_args(wake_timeout=1.0)))
    trigger.join(timeout=1)

    assert f"g2evenai exitid {EXCHANGE:016x}" in firmware.command_log
    assert f"micrecord stopid {EXCHANGE:016x} discard" in firmware.command_log
    assert "micrecord stop" not in firmware.command_log
    assert not any(cmd.startswith(("g2evenai ask", "g2evenai reply"))
                   for cmd in firmware.command_log)


def test_result_artifacts_include_exact_ids_and_self_path(
        firmware, monkeypatch, tmp_path):
    _patch_probe_config(monkeypatch, firmware)
    result = run(probe.run_owned_probe(_args(output_dir=str(tmp_path))))

    assert result["ok"], result
    assert Path(result["local_paths"]["live_pcm"]).read_bytes()
    assert Path(result["local_paths"]["wav"]).read_bytes() == firmware.wav_bytes
    result_path = Path(result["local_paths"]["result"])
    text = result_path.read_text(encoding="utf-8")
    assert str(result_path) in text
    assert f'"exchange_id": "{EXCHANGE:016x}"' in text
