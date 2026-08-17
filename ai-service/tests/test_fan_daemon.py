from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import sys
from pathlib import Path

import pytest


CONTROLLER_PATH = (
    Path(__file__).resolve().parent.parent / "systemd" / "hw1-fan-controller"
)
LOADER = importlib.machinery.SourceFileLoader("hw1_fan_controller", str(CONTROLLER_PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
fan = importlib.util.module_from_spec(SPEC)
# dataclasses resolves postponed annotation metadata through sys.modules while
# the extensionless service source is executing.
sys.modules[LOADER.name] = fan
LOADER.exec_module(fan)


def _write(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="ascii")


def _record_writes(monkeypatch) -> list[tuple[Path, str]]:
    """Observe verified sysfs writes while still performing them.

    A tmp_path sysfs cannot emulate the kernel re-asserting the cooling device,
    so handing the fan back is asserted through the write sequence itself.
    """
    calls: list[tuple[Path, str]] = []
    real_write_verified = fan._write_verified

    def recorder(path, value):
        calls.append((path, value))
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", recorder)
    return calls


def _assert_bounced(calls: list[tuple[Path, str]], zone: Path) -> None:
    """The zone must be bounced, and must be left enabled."""
    mode_writes = [value for path, value in calls if path == zone / "mode"]
    assert mode_writes == ["disabled", "enabled"], mode_writes


def _make_sysfs(tmp_path: Path, *, tach: bool = True):
    hwmon_root = tmp_path / "sys/class/hwmon"
    thermal_root = tmp_path / "sys/class/thermal"
    hwmon = hwmon_root / "hwmon17"
    cooling = thermal_root / "cooling_device23"
    zone = thermal_root / "thermal_zone9"

    _write(hwmon / "name", "pwmfan\n")
    _write(hwmon / "pwm1", "0\n")
    # The daemon must never use this attribute as an auto/manual selector.
    _write(hwmon / "pwm1_enable", "1\n")
    if tach:
        _write(hwmon / "fan1_input", "0\n")

    _write(cooling / "type", "pwm-fan\n")
    _write(cooling / "max_state", "4\n")
    _write(cooling / "cur_state", "0\n")

    _write(zone / "type", "cpu-thermal\n")
    _write(zone / "temp", "45000\n")
    _write(zone / "policy", "step_wise\n")
    # Raspberry Pi kernels ship step_wise as the only governor. The daemon
    # coexists with it and hands the fan back by bouncing this mode attribute.
    _write(zone / "mode", "enabled\n")
    _write(zone / "available_policies", "step_wise\n")
    # One cooling device can appear in several trip mappings. Discovery must
    # deduplicate those links to one bound thermal zone.
    (zone / "cdev0").symlink_to(cooling)
    (zone / "cdev1").symlink_to(cooling)

    roots = fan.SysfsRoots(hwmon=hwmon_root, thermal=thermal_root)
    return roots, hwmon, cooling, zone


def _config(**overrides):
    raw = {
        "socket_path": "/run/hw1-fan-controller/control.sock",
        "poll_interval_s": 1.0,
        "rediscover_interval_s": 1.0,
        "quiet_pwm": 75,
        "start_boost_pwm": 255,
        "start_boost_s": 0.5,
        "stall_timeout_s": 2.0,
        "safety_temp_mc": 80000,
        "safety_hysteresis_mc": 5000,
        "curve_hysteresis_mc": 5000,
        "curve": [
            {"temp_mc": 50000, "pwm": 75},
            {"temp_mc": 60000, "pwm": 125},
            {"temp_mc": 70000, "pwm": 200},
        ],
    }
    raw.update(overrides)
    return fan.config_from_mapping(raw)


def _schema(reply: dict) -> None:
    assert set(reply) == {
        "ok",
        "code",
        "requested_mode",
        "effective_mode",
        "temp_mc",
        "target_pwm",
        "pwm",
        "rpm",
        "health",
    }
    assert type(reply["ok"]) is bool
    assert reply["requested_mode"] in fan.MODES
    assert reply["effective_mode"] in fan.MODES
    assert reply["temp_mc"] is None or type(reply["temp_mc"]) is int
    assert type(reply["target_pwm"]) is int
    assert 0 <= reply["target_pwm"] <= 255
    assert type(reply["pwm"]) is int
    assert 0 <= reply["pwm"] <= 255
    assert reply["rpm"] is None or (
        type(reply["rpm"]) is int and reply["rpm"] >= 0
    )
    assert reply["health"] in fan.HEALTH_TOKENS


def test_discovery_uses_names_and_bound_links_not_hwmon_numbers(tmp_path):
    roots, hwmon, cooling, zone = _make_sysfs(tmp_path)

    found = fan.discover_hardware(roots)

    assert found.hwmon == hwmon
    assert found.cooling_device == cooling
    assert found.thermal_zone == zone
    assert found.pwm_path == hwmon / "pwm1"
    assert found.rpm_path == hwmon / "fan1_input"


def test_discovery_fails_closed_for_duplicate_or_unbound_fans(tmp_path):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    duplicate = roots.hwmon / "hwmon88"
    _write(duplicate / "name", "pwmfan\n")
    _write(duplicate / "pwm1", "0\n")
    with pytest.raises(fan.DiscoveryError, match="expected one pwmfan"):
        fan.discover_hardware(roots)

    for child in duplicate.iterdir():
        child.unlink()
    duplicate.rmdir()
    (zone / "cdev0").unlink()
    (zone / "cdev1").unlink()
    assert hwmon.exists()
    with pytest.raises(fan.DiscoveryError, match="bound to one thermal zone"):
        fan.discover_hardware(roots)


def test_graceful_restore_uses_verified_handle_if_topology_later_ambiguous(
        tmp_path, monkeypatch):
    roots, _hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    # The daemon coexists with step_wise rather than displacing it, so the
    # governor must still own the zone while the fan is under control.
    assert (zone / "policy").read_text() == "step_wise\n"

    duplicate = roots.hwmon / "hwmon88"
    _write(duplicate / "name", "pwmfan\n")
    _write(duplicate / "pwm1", "0\n")
    with pytest.raises(fan.DiscoveryError, match="expected one pwmfan"):
        fan.restore_kernel_policy(roots)

    calls = _record_writes(monkeypatch)
    controller.restore()
    assert (zone / "policy").read_text() == "step_wise"
    # Rediscovery is now ambiguous, so the bounce must run off the cached
    # handle the daemon captured at attach time.
    _assert_bounced(calls, zone)


def test_graceful_restore_failure_leaves_verified_max_pwm(tmp_path, monkeypatch):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    assert (zone / "policy").read_text() == "step_wise\n"
    assert (hwmon / "pwm1").read_text().strip() == "0"
    real_write_verified = fan._write_verified

    def reject_handback(path, value):
        if path == zone / "mode":
            raise fan.FanIOError("injected zone bounce failure")
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", reject_handback)
    controller.restore()

    # The kernel could not be made to re-assert, so the only safe fixed duty
    # must be left behind instead of a low one the governor will not correct.
    assert (hwmon / "pwm1").read_text() == "255"


@pytest.mark.parametrize(
    "raw, message",
    [
        ({"surprise": 1}, "unknown configuration key"),
        ({"quiet_pwm": True}, "quiet_pwm must be an integer"),
        (
            {
                "curve": [
                    {"temp_mc": 60000, "pwm": 100},
                    {"temp_mc": 50000, "pwm": 120},
                ]
            },
            "strictly increasing",
        ),
        (
            {
                "start_boost_pwm": 100,
                "curve": [{"temp_mc": 50000, "pwm": 125}],
            },
            "at least every normal setpoint",
        ),
    ],
)
def test_configuration_validation_is_strict(raw, message):
    with pytest.raises(fan.ConfigError, match=message):
        fan.config_from_mapping(raw)


def test_controller_refuses_socket_paths_outside_its_runtime_directory():
    with pytest.raises(fan.ConfigError, match="socket_path must be"):
        fan.config_from_mapping({"socket_path": "/home/cm5/.config/fan.sock"})


def test_auto_curve_has_downward_hysteresis(tmp_path):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)

    first = controller.tick(now=0.0)
    assert first["requested_mode"] == "auto"
    assert first["target_pwm"] == 0
    # Coexistence, not displacement: the governor keeps the zone (and with it
    # the zone's critical trip) while the daemon drives pwm1.
    assert (zone / "policy").read_text() == "step_wise\n"

    _write(zone / "temp", "61000\n")
    _write(hwmon / "fan1_input", "2000\n")
    up = controller.tick(now=1.0)
    assert up["target_pwm"] == 125

    _write(zone / "temp", "57000\n")
    held = controller.tick(now=2.0)
    assert held["target_pwm"] == 125

    _write(zone / "temp", "54000\n")
    down = controller.tick(now=3.0)
    assert down["target_pwm"] == 75


def test_quiet_boosts_then_settles_and_never_touches_pwm_enable(tmp_path):
    roots, hwmon, _cooling, _zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    controller.set_requested_mode("quiet")

    boost = controller.tick(now=1.0)
    assert boost["health"] == "boosting"
    assert boost["target_pwm"] == 255
    assert boost["pwm"] == 255

    _write(hwmon / "fan1_input", "2400\n")
    settled = controller.tick(now=1.6)
    assert settled["requested_mode"] == "quiet"
    assert settled["effective_mode"] == "quiet"
    assert settled["health"] == "ok"
    assert settled["target_pwm"] == 75
    assert (hwmon / "pwm1_enable").read_text() == "1\n"


def test_temperature_and_stall_safety_force_effective_max(tmp_path):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    controller.set_requested_mode("quiet")

    controller.tick(now=1.0)  # starts the boost and zero-RPM timers
    stalled = controller.tick(now=3.6)
    assert stalled["requested_mode"] == "quiet"
    assert stalled["effective_mode"] == "max"
    assert stalled["health"] == "safety_stall"
    assert stalled["target_pwm"] == 255

    _write(hwmon / "fan1_input", "2500\n")
    recovered = controller.tick(now=4.0)
    assert recovered["effective_mode"] == "quiet"
    assert recovered["health"] == "ok"

    _write(zone / "temp", "81000\n")
    hot = controller.tick(now=5.0)
    assert hot["requested_mode"] == "quiet"
    assert hot["effective_mode"] == "max"
    assert hot["health"] == "safety_temp"
    assert hot["pwm"] == 255

    _write(zone / "temp", "77000\n")
    held = controller.tick(now=6.0)
    assert held["health"] == "safety_temp"
    _write(zone / "temp", "75000\n")
    cleared = controller.tick(now=7.0)
    assert cleared["effective_mode"] == "quiet"


def test_missing_tach_is_telemetry_degradation_not_stall(tmp_path):
    roots, hwmon, _cooling, _zone = _make_sysfs(tmp_path, tach=False)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    controller.set_requested_mode("quiet")

    boost = controller.tick(now=1.0)
    assert boost["health"] == "boosting"
    assert boost["target_pwm"] == 255
    degraded = controller.tick(now=1.6)
    assert degraded["health"] == "tach_unavailable"
    assert degraded["rpm"] is None
    assert degraded["target_pwm"] == 75
    assert (hwmon / "pwm1_enable").read_text() == "1\n"


def test_restore_kernel_is_independent_and_verified(tmp_path, monkeypatch):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    # A duty this daemon commanded, which the governor will not correct on its
    # own because no trip target has changed.
    _write(hwmon / "pwm1", "200\n")

    calls = _record_writes(monkeypatch)
    fan.restore_kernel_policy(roots)

    assert (zone / "policy").read_text() == "step_wise"
    assert (zone / "mode").read_text() == "enabled"
    _assert_bounced(calls, zone)


def test_attach_failure_after_discovery_returns_fan_to_kernel(
        tmp_path, monkeypatch):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    # Discovery only proves pwm1 exists, so an unreadable duty fails the
    # activation transaction after the topology was already accepted.
    _write(hwmon / "pwm1", "999\n")

    calls = _record_writes(monkeypatch)
    failed = controller.tick(now=0.0)

    assert failed["health"] == "unavailable"
    assert (zone / "policy").read_text() == "step_wise"
    # It is safe to leave PWM alone only because the kernel was made to
    # re-assert control of the fan.
    _assert_bounced(calls, zone)


def test_attach_rollback_failure_leaves_max_pwm(tmp_path, monkeypatch):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    _write(hwmon / "pwm1", "999\n")
    real_write_verified = fan._write_verified

    def fail_activation_and_handback(path, value):
        if path == zone / "mode":
            raise fan.FanIOError("injected zone bounce failure")
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", fail_activation_and_handback)

    failed = controller.tick(now=0.0)

    assert failed["health"] == "unavailable"
    # Neither the daemon nor the kernel can be trusted with the fan now, so the
    # only safe fixed command must be left behind.
    assert (hwmon / "pwm1").read_text() == "255"


def test_socket_grammar_and_response_schema(tmp_path):
    roots, hwmon, _cooling, _zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    _write(hwmon / "fan1_input", "2000\n")

    assert fan._decode_request(b"status") == "status"
    assert fan._decode_request(b"mode quiet") == "mode quiet"
    assert fan._decode_request(b"mode quiet ") is None
    assert fan._decode_request(b"mode\tmax") is None
    assert fan._decode_request(b"status\r") is None
    assert fan._decode_request(b"\xff") is None

    reply = controller.command("mode max")
    _schema(reply)
    assert reply["ok"] is True
    assert reply["requested_mode"] == "max"
    assert reply["effective_mode"] == "max"
    assert reply["pwm"] == 255

    invalid = controller.command("mode 255")
    _schema(invalid)
    assert invalid["ok"] is False
    assert invalid["code"] == "invalid_command"

    encoded = json.dumps(reply, separators=(",", ":"), sort_keys=True)
    assert json.loads(encoded) == reply


def test_io_error_attempts_max_and_reports_fixed_schema(tmp_path):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    _write(zone / "temp", "not-an-integer\n")

    failed = controller.tick(now=1.0)

    _schema(failed)
    assert failed["ok"] is False
    assert failed["code"] == "io_error"
    assert failed["health"] == "io_error"
    assert failed["effective_mode"] == "max"
    assert failed["target_pwm"] == 255
    assert (hwmon / "pwm1").read_text() == "255"


def _warmed_controller(tmp_path):
    """Attached, fan already spinning, and one curve step below target."""
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    # A live tach keeps this off the start-boost path, so the commanded duty is
    # the curve value itself and the assertions stay about the write, not boost.
    _write(hwmon / "fan1_input", "2000\n")
    _write(zone / "temp", "70000\n")
    return controller, hwmon


def test_lost_race_against_governor_retries_rather_than_failing(
        tmp_path, monkeypatch):
    controller, hwmon = _warmed_controller(tmp_path)
    real_write_verified = fan._write_verified
    stomped: list[str] = []

    def stomp_first_write(path, value):
        if path == hwmon / "pwm1" and not stomped:
            stomped.append(value)
            # The governor still owns this cooling device: emulate it winning
            # the race by overwriting the duty between write and readback.
            path.write_text("125", encoding="ascii")
            return None
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", stomp_first_write)
    result = controller.tick(now=1.0)

    assert stomped, "the governor race was never exercised"
    assert result["health"] == "ok"
    assert result["pwm"] == result["target_pwm"] == 200
    assert (hwmon / "pwm1").read_text() == "200"


def test_persistently_unretained_pwm_still_reaches_the_fail_safe(
        tmp_path, monkeypatch):
    controller, hwmon = _warmed_controller(tmp_path)
    real_write_verified = fan._write_verified

    def always_stomp(path, value):
        if path == hwmon / "pwm1" and value != str(fan.MAX_PWM):
            path.write_text("125", encoding="ascii")
            return None
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", always_stomp)
    failed = controller.tick(now=1.0)

    # Retrying is bounded: a duty that never sticks is real hardware trouble.
    assert failed["health"] == "io_error"
    assert failed["effective_mode"] == "max"
    assert (hwmon / "pwm1").read_text() == "255"


def test_mode_failure_does_not_later_apply_a_terminally_failed_request(tmp_path):
    roots = fan.SysfsRoots(
        hwmon=tmp_path / "sys/class/hwmon",
        thermal=tmp_path / "sys/class/thermal",
    )
    controller = fan.FanController(_config(), roots=roots, clock=lambda: 0.0)

    failed = controller.command("mode quiet")

    assert failed["ok"] is False
    assert failed["requested_mode"] == "auto"
    live_roots, hwmon, _cooling, _zone = _make_sysfs(tmp_path)
    assert live_roots == roots
    _write(hwmon / "fan1_input", "2000\n")
    recovered = controller.tick(now=2.0)
    assert recovered["ok"] is True
    assert recovered["requested_mode"] == "auto"
    assert recovered["target_pwm"] == 0


def test_failed_max_fallback_restores_kernel_governor(tmp_path, monkeypatch):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    real_write_verified = fan._write_verified

    def reject_max(path, value):
        if path == hwmon / "pwm1" and value == "255":
            raise fan.FanIOError("injected max failure")
        return real_write_verified(path, value)

    monkeypatch.setattr(fan, "_write_verified", reject_max)
    _write(zone / "temp", "not-an-integer\n")

    failed = controller.tick(now=1.0)

    assert failed["health"] == "io_error"
    assert (zone / "policy").read_text() == "step_wise"


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("temp", "-1\n"),
        ("rpm", "100001\n"),
    ],
)
def test_bridge_numeric_limits_fail_safe(tmp_path, attribute, value):
    roots, hwmon, _cooling, zone = _make_sysfs(tmp_path)
    controller = fan.FanController(_config(), roots=roots)
    controller.tick(now=0.0)
    if attribute == "temp":
        _write(zone / "temp", value)
    else:
        _write(hwmon / "fan1_input", value)

    failed = controller.tick(now=1.0)

    _schema(failed)
    assert failed["health"] == "io_error"
    assert failed["effective_mode"] == "max"
    assert failed["temp_mc"] is None
    assert failed["rpm"] is None
