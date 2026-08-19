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
| [`ai-service/tools/`](ai-service/tools/README.md) | operator probes and benchmarks, grouped into `link/`, `stt/`, `llm/` by what they investigate |
| [`ai-service/systemd/`](ai-service/systemd/) | units, the two privileged helper daemons, their sudo policy, and installers |
| [`docs/`](docs/) | architecture, deployment paths, and the investigation runbooks |
| [`docs/investigations/`](docs/investigations/README.md) | how to diagnose this setup on whatever hardware you have |
| [`deploy.sh`](deploy.sh) | gated sync from a dev machine to a device |

## Start here

**Reading the design:** [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) is the
program-level design — process model, link layer, engines, pipeline.

**Standing up a device:** get the tree onto a Pi 5 or CM5 (`./deploy.sh`, or a
clone on the device), then run the provisioner *on* the device:

```bash
~/hw1-ai-service/bootstrap.sh --dry-run   # print the plan
~/hw1-ai-service/bootstrap.sh             # UART, venv, config, unit, helpers
```

It is re-runnable, overwrites nothing, and stops with a TODO list rather than
inventing a credential or downloading model weights for you.

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
