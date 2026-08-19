# HardwareOne CM5 fan controller

This service controls the CM5 carrier's dedicated four-wire fan through the
Raspberry Pi kernel `pwm-fan` driver. The ESP32 does not drive the fan pins; its
`cm5 fan` bridge connects to this service's Unix socket.

The controller commands PWM duty (`0..255`) and reports tachometer RPM as
telemetry. It does not claim that a PWM value is a particular RPM: that mapping
depends on the individual fan and load.

## Install

On the CM5, run the installer as root and name the unprivileged account that
runs `hw1-ai-service`:

```sh
sudo ./systemd/install-fan-controller.sh <service-user>
```

The installer:

- creates the system group `hw1-fan` and adds the service account;
- installs the root-owned executable at
  `/usr/local/libexec/hw1-fan-controller`;
- installs and enables the system unit `hw1-fan-controller.service`;
- creates `/etc/hw1-fan-controller.json` from the example only when no config
  already exists; and
- validates the installed configuration before restarting the service and
  prints the first hardware health result.

Reboot before starting the unprivileged bridge. A lingering per-user systemd
manager may retain its old supplementary groups across an ordinary logout and
login. Inspect startup and hardware discovery with:

```sh
systemctl status hw1-fan-controller.service
systemctl show hw1-fan-controller.service --property=StatusText --value
journalctl -u hw1-fan-controller.service
```

`READY=1` means the process, socket, and first discovery attempt completed; it
does not turn `health=unavailable` or `health=io_error` into a healthy fan.
Resolve either result and bench-check PWM/tach behavior before setting
`fan.enabled: true` in the unprivileged service.

## Modes and safety

- `auto` applies the configured temperature/PWM step curve with downward
  hysteresis.
- `quiet` applies `quiet_pwm`, after a bounded start boost when needed.
- `max` applies PWM 255.

Every boot starts in `auto`; Quiet is deliberately not persisted. High
temperature or a sustained zero tach reading at non-zero PWM overrides the
requested mode and forces effective `max`. A missing tach input is reported as
`tach_unavailable` but cannot be used for stall detection.

While running, the daemon drives `pwm1` directly and leaves the kernel's
`step_wise` governor in charge of the thermal zone. Raspberry Pi kernels ship
`step_wise` as the only governor, and disabling the zone would forfeit its
critical trip, so coexistence is deliberate. The kernel writes the cooling
device only when a trip target changes, and the poll loop re-reads the real
duty every tick and re-asserts, which bounds any kernel override to one poll
interval. A duty that does not stick is retried once, because losing that race
is far more likely than failing hardware; a second failure is treated as I/O
failure as before.

Because the policy is therefore always `step_wise`, writing it cannot hand the
fan back — it would strand `pwm1` at the last commanded duty until some trip
target happened to change. Every release path instead bounces the zone
(`mode` `disabled` then `enabled`) so the thermal core re-evaluates and resumes
control, always ending on a verified `enabled`. Graceful shutdown does this
through the controller's verified handle, or attempts verified PWM 255 if the
bounce fails; if neither action can be confirmed it logs a critical fault. The
zone is unmonitored only between those two adjacent writes; dying there is
healed by the next start's restore. The unit independently
attempts `--restore-kernel` discovery before every start and after every stop,
including after a watchdog `SIGKILL`; that best-effort guard requires the
unique sysfs topology to remain discoverable. It never writes `pwm1_enable`.

## Configuration

Edit `/etc/hw1-fan-controller.json`, then validate and restart:

```sh
sudo /usr/local/libexec/hw1-fan-controller \
  --config /etc/hw1-fan-controller.json --check-config
sudo systemctl restart hw1-fan-controller.service
```

Unknown keys, ambiguous JSON types, unsafe PWM/temperature bounds, unordered
curves, and non-root-writable production configurations are rejected. Curve
temperatures are millidegrees Celsius. Use a measured `quiet_pwm` that reliably
keeps the installed fan rotating; the example's 75 is only a starting point for
hardware validation.

## Socket protocol

The root:`hw1-fan` socket is fixed at
`/run/hw1-fan-controller/control.sock`. One ASCII command is accepted per
connection:

```text
status
mode auto
mode quiet
mode max
```

Terminate the command with a newline or close the write side. The one-line JSON
reply always includes `ok`, `code`, `requested_mode`, `effective_mode`,
`temp_mc`, `target_pwm`, `pwm`, `rpm`, and `health`.

## Local tests

From `cm5/ai-service`:

```sh
./run_checks.sh --fast
```

The focused fake-sysfs coverage is in `tests/test_fan_daemon.py`; no CM5 or
root privileges are required for it.
