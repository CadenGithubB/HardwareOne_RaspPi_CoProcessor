# HardwareOne — Raspberry Pi CM5 co-processor

The CM5-side companion to the HardwareOne firmware running on an ESP32.
One long-lived user daemon speaks the UART link to the ESP32, receives voice and
prompts from it, runs speech-to-text and LLM generation on this host, and
returns answers to the device's own display surfaces. Two narrowly privileged
system services own host power and the fan curve.

The ESP32 firmware itself lives in a **separate repository** and defines every wire
protocol this daemon speaks.

## Layout

| Path | What lives there |
| --- | --- |
| [`ai-service/`](ai-service/) | the daemon — Python package, tests, tools, and the privileged host-control units |
| [`ai-service/hw1_ai_service/`](ai-service/hw1_ai_service/) | the package itself: link, audio, STT, LLM, pipeline, control planes |
| [`ai-service/tests/`](ai-service/tests/) | the full suite — runs on any POSIX machine with no hardware and no models |
| [`ai-service/tools/`](ai-service/tools/README.md) | operator probes and benchmarks, grouped into `link/`, `stt/`, `llm/`, and `openclaw/` by what they investigate |
| [`ai-service/systemd/`](ai-service/systemd/) | units, the two privileged helper daemons, their sudo policy, and installers |
| [`docs/`](docs/) | architecture, deployment paths, and the investigation runbooks |
| [`docs/investigations/`](docs/investigations/README.md) | how to diagnose this setup on whatever hardware you have |
| [`deploy.sh`](deploy.sh) | gated sync from a dev machine to a device |
| [`ai-service/setup.sh`](ai-service/setup.sh) | one RPi-console first-time setup guide (core or core + OpenClaw) |

## Start here

**Reading the design:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the
program-level design — process model, link layer, engines, pipeline.

**Standing up a device:** get the tree onto a Pi 5 or CM5 (`./deploy.sh`, or a
clone on the device), then run the one setup guide *on* the device:

```bash
cd ~/hw1-ai-service
./setup.sh --dry-run                       # print the selected plan
./setup.sh                                  # choose core or core + OpenClaw
```

The guide runs as the normal console/SSH administrator. Core mode installs the
STT + local LLM + ESP32 daemon. The OpenClaw profile runs the same core phase,
then elevates only for the root-owned OpenClaw installer; its Gateway is
loopback-only and outbound networking is blocked unless explicitly opted out.
Non-interactive OpenClaw setup does not add the login account to the vault;
use `--vault-operator <user>` to opt into direct human vault access.
For an RPi-only agent experiment, choose `OpenClaw only` or pass
`--mode openclaw-only`; that path does not touch UART, STT, or ESP32 setup.
The legacy `bootstrap.sh` and `openclaw/bootstrap_openclaw.sh` remain available
for advanced, phase-specific re-runs.

It is re-runnable and overwrites no credentials or tuned configuration. Core
mode stops with TODOs for missing credentials/model work; the OpenClaw profile
offers only its pinned, resumable downloads and requires confirmation (or
explicit `--yes`).

**Working on the code**, with no Pi and no models:

```bash
cd ai-service
./run_checks.sh          # compile, lint, and the full suite against fakes
```

The tests drive a fake firmware over a pty and fake engines, so the whole suite
is a laptop-only loop.

**Investigating a problem:** [`docs/investigations/`](docs/investigations/README.md)
holds the runbooks — link triage, capture parity, STT and LLM benchmarking,
render timing — plus `uart-baud-test/`, a harness that measures what link rate
your particular board pair actually sustains.

## Conventions

- Paths and hosts come from `$CM5_HOST` / `$CM5_USER` and the profile variables
  in the investigation runbooks. No account, hostname, or checkout location is
  baked into tracked text.
- Session-specific investigation records are kept locally and gitignored: they
  describe one rig and are not useful to anyone else. What is reusable from them
  was distilled into `docs/investigations/`.
