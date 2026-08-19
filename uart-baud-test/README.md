# UART Baud-Rate Reliability Sweep — CM5 ↔ XIAO ESP32-S3

Answers one question: **what is the highest UART baud rate this particular
CM5 + XIAO hardware combination can sustain reliably, for an extended period,
with essentially zero data corruption?**

Part of the investigation set in
[`docs/investigations/`](../docs/investigations/README.md) — the one you run
rather than follow.

The Raspberry Pi CM5 is the test controller (`uart_baud_test.py`). The XIAO
runs a dedicated test firmware (`firmware/`). For every requested rate the
tool configures both sides, re-synchronizes, then runs CRC32-framed,
sequence-numbered, byte-for-byte-verified stress traffic in three phases:

| Phase    | Direction        | What it proves |
|----------|------------------|----------------|
| `echo`   | full-duplex echo | CM5 streams frames while a reader thread verifies the echoes — both directions loaded simultaneously, coupled through the far side's parse-and-reply path |
| `sink`   | CM5 → XIAO only  | XIAO regenerates the expected pattern from (pattern, seq) and verifies; isolates the CM5→XIAO leg |
| `gen`    | XIAO → CM5 only  | XIAO streams generated frames at line rate; isolates the XIAO→CM5 leg |
| `duplex` | both, independent | XIAO generates at line rate **while** the CM5 floods sink frames — simultaneous uncoupled streams (the shape of real bulk-stream + command traffic), with errors still attributed per direction |

Nothing is graded on "the port opened". A rate the Linux driver silently
clamps is reported `UNSUPPORTED`, not `FAIL` (see *Baud-rate limits* below).

## Quick start

```bash
# 1. Flash the XIAO (once):
cd firmware && idf.py set-target esp32s3 && idf.py -p /dev/ttyACM0 flash

# 2. On the CM5, run the standard suite (~2.5 min for 12 rates × 10 s):
python3 uart_baud_test.py

# Development smoke test (echo only, 2 s per rate):
python3 uart_baud_test.py --quick

# Long soak of the interesting candidates:
python3 uart_baud_test.py --baud-rates 2000000,2500000,3000000 --duration 60
```

## Wiring / UART pins

Defaults match the hardwareone CM5↔XIAO carrier (short PCB trace, 33 Ω series
resistors, no flow-control lines). Everything is configurable for other
board revisions — nothing is hard-coded.

| Signal | CM5 (RP1)                  | XIAO ESP32-S3            |
|--------|----------------------------|--------------------------|
| CM5 TX → XIAO RX | GPIO4 (uart2 TXD) | GPIO44 (D7)              |
| CM5 RX ← XIAO TX | GPIO5 (uart2 RXD) | GPIO43 (D6)              |
| GND    | GND                        | GND                      |

- Both sides are 3.3 V logic. No RTS/CTS — the protocol is designed around
  bounded in-flight data instead of hardware flow control.
- CM5 device: `/dev/ttyAMA2`, created by the **`uart2-pi5`** overlay
  (plain `uart2` is the Pi 4 overlay and does nothing on a Pi 5/CM5):

  ```
  echo 'dtoverlay=uart2-pi5' | sudo tee -a /boot/firmware/config.txt && sudo reboot
  ```

- Different wiring? Change the CM5 side with `--port /dev/ttyAMA0` (enable the
  matching overlay/dtparam and make sure the Linux serial console is *disabled*
  on that UART), and the XIAO side via `idf.py menuconfig` → *UART Baud Test*
  (UART number, TX/RX GPIOs, safe baud, ring sizes).
- Make sure nothing else holds the port: `fuser -v /dev/ttyAMA2`
  (stop `hw1-ai-service` first if it's installed).
- Add your user to `dialout` if you get permission errors.

## Building / flashing the XIAO firmware

The firmware is a standalone ESP-IDF project (tested with IDF v5.5). It
**replaces** whatever is on the XIAO — reflash your normal firmware when done.

```bash
cd firmware
idf.py set-target esp32s3        # once
idf.py menuconfig                # optional: pins/UART under "UART Baud Test"
idf.py -p /dev/ttyACM0 flash monitor
```

- The XIAO's USB-C port is the console (USB-Serial-JTAG): `idf.py monitor`
  shows every baud switch, watchdog revert, and phase there — very useful when
  debugging a failing rate. The test UART (UART0, GPIO43/44) is exclusively
  the test link.
- If flashing fails to start, hold the XIAO's BOOT button while plugging in.
- Other ESP32 chips work too (`idf.py set-target esp32s2` etc.); pins and the
  UART number are menuconfig options, and the firmware reports its chip and
  its UART's maximum baud to the controller at startup.

## Running the CM5 test

```bash
python3 uart_baud_test.py                        # standard suite, JSON auto-saved
python3 uart_baud_test.py --quick                # 2 s echo-only per rate
python3 uart_baud_test.py --duration 30          # heavier soak
python3 uart_baud_test.py --bytes 10000000       # budget by bytes instead of time
python3 uart_baud_test.py --baud-rates 2000000,2500000,3000000,3500000
python3 uart_baud_test.py --output results.json --csv results.csv
python3 uart_baud_test.py --phases echo          # full-duplex only
python3 uart_baud_test.py --lockstep             # one frame in flight (debug aid)
python3 uart_baud_test.py --stop-bits 2          # extra margin at marginal rates
python3 uart_baud_test.py --self-test            # protocol unit checks, no HW
```

No hardware handy? `sim_xiao.py` is a software stand-in for the firmware
(same protocol over a pty): `python3 sim_xiao.py --run-suite --quick` runs the
whole controller against it in-process, and `--deaf-above 2500000` injects a
dead rate to exercise the recovery path. It validates tool logic, not
electrical reality.

Needs `pyserial` (`sudo apt install python3-serial`). All options:
`python3 uart_baud_test.py --help`.

Defaults: `--port /dev/ttyAMA2`, 10 s per rate split 60/20/20 across
echo/sink/gen, 1024-byte payloads, 8 KB echo window, all five patterns
cycling per frame:

- incrementing bytes (start offset varies per frame)
- xorshift32 pseudo-random keyed by the frame's sequence number (known seed,
  fully reproducible, and every frame is independently derivable — a dropped
  frame can't desynchronize verification)
- alternating 0x55/0xAA (worst-case bit toggling)
- all 0x00, all 0xFF (worst-case DC levels)

Every frame: `A5 5A | type | pattern | seq(u32) | len(u16) | payload | CRC32`.
Corruption → CRC mismatch; drops/dups → sequence gaps/dups; garbage →
counted resync bytes. The ESP32 additionally reports hardware framing,
parity, FIFO-overflow, and ring-buffer-full events from its UART ISR.

## How baud-rate synchronization works

All negotiation happens at a **safe control baud** (115200, `--safe-baud`);
only the stress traffic runs at the rate under test:

1. *Preflight (CM5 only):* set the requested rate via `termios2`/`BOTHER`,
   read it back, restore safe baud. If the kernel clamped it (e.g. 4 M → 3 M
   on the PL011), the rate is reported `UNSUPPORTED` and the XIAO is never
   disturbed. (`--ignore-preflight` overrides.)
2. At safe baud: `SET_BAUD(rate)` → XIAO validates it against its UART's
   real capability (rejects > 5 Mbaud on ESP32-S3 → `UNSUPPORTED`), ACKs,
   drains its TX FIFO, waits a fixed 40 ms, switches.
3. CM5 waits out the switch delay, switches, flushes, settles (`--settle`),
   then sends nonce-checked `PING`s at the new rate. Only after a verified
   `PONG` does stress traffic start.
4. After the phases: stats are fetched at the test rate and `SET_BAUD(safe)`
   returns both sides to the control channel before the next rate.

**Recovery is automatic and cannot strand the link.** The firmware keeps two
watchdogs: if no *CRC-valid* frame arrives within 3 s of a baud switch, or the
link goes silent for 8 s mid-test, it reverts to the safe baud on its own. The
CM5 mirrors this: on any sync failure it drops to safe baud and pings until
the XIAO's watchdog brings it back (worst case ~14 s). A total loss at 3 Mbaud
therefore cannot prevent 3.5/4/5 Mbaud from being tested. Garbage received at
a wrong rate can't fake a valid frame — feeding the watchdog requires a
correct CRC32.

## Interpreting results

```
Baud     Result  Duration  TX Bytes   RX Bytes   Errors  Timeouts  Throughput  Notes
115200   PASS    10.2 s    ...        ...        0       0         21.8 KB/s
2000000  PASS    10.1 s    ...        ...        0       0         372.1 KB/s
3000000  MARGINAL 10.3 s   ...        ...        12      3         540.2 KB/s  12 error events, byte error rate 3.1e-05
3500000  UNSUPPORTED  -    -          -          -       -         -           kernel clamped to 3000000
```

- **PASS** — every phase completed with *zero* errors of any kind on either
  side (no CRC failures, no sequence gaps/dups, no payload mismatches, no
  timeouts, no hardware framing/overrun events, no resync bytes).
- **MARGINAL** — completed, but with a nonzero byte error rate below
  `--marginal-threshold` (default 1e-4). Real intermittent corruption: do not
  ship at this rate; investigate or add margin (`--stop-bits 2`, slower rate).
- **FAIL** — sync never succeeded at a configurable rate, a phase died, or
  the error rate crossed the threshold.
- **UNSUPPORTED** — the rate could not even be configured: the CM5 kernel
  clamped/refused it, or the XIAO's UART rejected it. Not a communication
  failure.
- **Throughput** is verified payload bytes per second (sum of both verified
  directions, framing overhead excluded — with 1024-byte payloads the frame
  overhead is 14/1038 ≈ 1.3%). Theoretical one-way line rate at 8N1 is
  baud/10 bytes/s; the echo phase moves traffic both ways at once.
- The JSON (always written; `--output` names it) contains per-phase, per-side
  counters — the XIAO's stats attribute errors to the CM5→XIAO leg, the CM5's
  own counters to the XIAO→CM5 leg, so you can see *which direction* is
  failing. `--csv` adds a flat per-rate summary for comparing PCB revisions.
- Timestamps and the full test configuration are embedded in the JSON, so a
  result file is self-describing when compared across hardware revisions.
- For the "extended period / essentially zero corruption" question: take the
  top PASS rates from a standard sweep, then soak them —
  `--baud-rates 2000000,2500000,3000000 --duration 300`. A rate is only
  trustworthy at the duration you actually tested.

## Adding baud rates

`--baud-rates` takes any list; the sweep continues cleanly past failures.
To change the default suite, edit `DEFAULT_RATES` in `uart_baud_test.py`.
Rates above 5 Mbaud are **not** offered because the ESP32-S3 UART hard-caps at
5 Mbaud (and the CM5's PL011 at ~3 Mbaud) — there is no configuration of this
stack where they can work; on other hardware just pass them and the tool will
grade them honestly.

## Baud-rate limits of this stack (why some rates report UNSUPPORTED)

**CM5 / RP1 PL011.** The Pi 5-family UARTs sit in the RP1 southbridge
(`arm,pl011`-compatible). A PL011's maximum rate is `uartclk / 16`; RP1's UART
clock is ~48 MHz, so the ceiling is **3.0 Mbaud**. The Linux serial core
clamps higher requests and writes the clamped value back into the termios —
exactly what the preflight readback detects. `init_uart_clock` in config.txt
is a Pi 4-era knob and does not apply to RP1. Divisor accuracy at 48 MHz
(PL011 has a 6-bit fractional divider) is excellent at the rates that matter:
2 M and 3 M are exact, 2.5 M is −0.26%, 921600 is +0.16%.

**Arbitrary rates on Linux.** Python's `termios` module only knows the classic
`Bnnn` constants. The tool bypasses that entirely with the `TCGETS2`/`TCSETS2`
ioctls + `BOTHER` (see `set_custom_baud()`), which is the correct mechanism
for arbitrary rates on modern kernels, and verifies every set with a readback.

**ESP32-S3 UART.** Hard maximum 5 Mbaud. This firmware clocks the UART from
APB (80 MHz) with a 4-bit fractional divisor — 1 M / 2 M / 2.5 M / 4 M / 5 M
are exact, 3 M is −0.08%, 3.5 M is +0.08%. (Note: the main hardwareone
firmware's Arduino HAL uses the 40 MHz XTAL instead; its divisor errors differ
slightly. This is one more reason a PASS here should be confirmed with your
real application traffic.)

**Combined clock-error budget.** 8N1 tolerates roughly ±2% total mismatch
between the two ends; every configurable pair above is ≤0.35% combined, so at
≤3 Mbaud the deciding factors are signal integrity and software keep-up, not
divisor error — which is precisely what the stress phases measure.

**Framing.** 8N1 is fixed (10 wire bits per byte). `--stop-bits 2` adds
margin at the cost of ~9% throughput; the safe control baud always runs 8N1.

**USB-serial adapters.** If you ever run the controller through an FTDI/CP210x
dongle instead of the PL011, its own rate table and latency behavior apply
(e.g. FTDI does 3 MBaud but not 2.5 M exactly; CP2102 tops out at 1 M, or
2 M for CP2102N). The readback + sync design still reports honestly.

**Expected outcome on this stack:** everything through 3 Mbaud is
configurable on both sides; 3.5 M / 4 M / 5 M will report
`UNSUPPORTED (kernel clamped to ~3 M)` on the CM5. The real question a sweep
answers is whether 2.5 M and 3 M hold up under sustained full-duplex load on
your PCB revision.

## Troubleshooting

**Complete failure at one particular baud (sync never succeeds).**
Expected for anything the PL011 can't generate (>3 M) — but that should
surface as `UNSUPPORTED` in preflight, not FAIL. A configurable rate that
never syncs usually means combined clock error or signal integrity. Watch
`idf.py monitor`: if the XIAO logs `synced at ...` but the CM5 still fails,
the XIAO→CM5 leg is the problem (and vice versa). Try `--stop-bits 2`,
`--lockstep`, and check the actual rates in the JSON
(`kernel_readback`, `esp_actual_baud`).

**Intermittent corruption (MARGINAL).**
The JSON tells you the direction: `phases.*.xiao.rx_crc_err`/`rx_mismatch_*`
= CM5→XIAO leg; the CM5-side `crc_err_frames`/`mismatch_*` = XIAO→CM5 leg.
Hardware `hw_frame_err` with clean CRCs elsewhere points at edges/ringing
(probe the trace, check grounding); errors that appear only in `echo` but not
`sink`/`gen` point at simultaneous-switching noise. Compare `--patterns alt`
vs `--patterns zero` — a pattern-dependent failure is signal integrity, not
clocking. Longer `--duration` makes rare events statistically visible.

**Linux refuses or clamps a baud rate (UNSUPPORTED).**
`termios2 set failed` = the driver rejected the ioctl outright.
`kernel clamped to N` = the serial core limited the request to the UART
clock's ceiling; the tool reports the achievable value. There is no config.txt
workaround for RP1's UART clock. Rates needing >3 M on this link would require
different hardware (e.g. USB or a different bridge), not different settings.

**Synchronization failures / sweep aborts with "safe-baud contact lost".**
The firmware always returns to 115200 within 8 s of silence, so a stuck link
usually means the firmware isn't running (check the USB console), the wrong
port is selected, another process grabbed the tty (`fuser -v /dev/ttyAMA2`),
or `--safe-baud` doesn't match `CONFIG_BAUDTEST_SAFE_BAUD`. Power-cycle the
XIAO and rerun; the tool re-handshakes from scratch each run.

**RX overruns (`hw_fifo_ovf` / `hw_buffer_full` on the XIAO).**
The ESP32 ring buffer (32 KB default) absorbed too much un-consumed data.
Should not happen with the default 8 KB window; if you raised `--window`,
keep it well below the RX ring (the tool assumes window ≪ ring). On the CM5
side, overruns show up as resync bytes/CRC errors instead — the PL011 has DMA
on RP1, but a heavily loaded CM5 can still stall the reader; keep the box
otherwise idle for authoritative runs.

**Unexpectedly low throughput at a PASSing rate.**
Throughput counts *verified payload*, so expect ~line-rate × 98.7% minus
protocol turnarounds. Low numbers with zero errors usually mean: payload too
small (`--payload-size 4096` amortizes framing + Python overhead), window too
small for the bandwidth-delay product (raise `--window`), or `--lockstep`
mode (one frame in flight is deliberately slow). At 3 Mbaud the echo phase
needs the CM5 Python process to sustain ~600 KB/s both directions — a busy
CPU shows up here first. `gen` phase throughput is the cleanest measure of
raw one-way capacity.

## Files

```
uart_baud_test.py       CM5 controller (Python 3 + pyserial)
baudtest_proto.py       shared protocol: framing, CRC, patterns, structs
sim_xiao.py             software XIAO for developing the tool without hardware
firmware/               standalone ESP-IDF project for the XIAO
  main/uart_test_proto.h   C mirror of baudtest_proto.py (keep in sync;
                           PROTO_VERSION is checked at runtime)
  main/uart_baud_test_main.c
uart_baud_results_*.json auto-saved machine-readable results
```
