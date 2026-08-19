# Operator tools

Probes and benchmarks you run by hand against a real device. Grouped by what
they investigate, matching the runbooks in
[`docs/investigations/`](../../docs/investigations/README.md).

These are not part of the installed package and nothing imports them — they are
scripts, run from the deployed tree on the device (`~/hw1-ai-service/tools/...`)
with the service venv's interpreter.

## `link/` — the UART link and the audio transport over it

| Tool | Use |
| --- | --- |
| `g2_evenai_probe.py` | send one command to the ESP32 and print the reply; the workhorse for every by-hand link check |
| `live_pcm_transport_probe.py` | deterministic synthetic transport probe — no capture hardware involved |
| `live_pcm_shadow_probe.py` | live-vs-retained capture parity and the injected-fault matrix |
| `run_native_live_stt_gate.sh` | end-to-end native gate; drives both probes above |

Runbooks: [link-triage](../../docs/investigations/link-triage.md),
[audio-parity](../../docs/investigations/audio-parity.md),
[render-timing](../../docs/investigations/render-timing.md).

## `stt/` — recognition accuracy and streaming cadence

| Tool | Use |
| --- | --- |
| `moonshine_stream_replay.py` | replay a corpus at a paced update floor, as the live path would |
| `moonshine_stream_replay_check.py` | grade a replay's retained JSONL against the policy |
| `moonshine_gate0a_medium_slice.json` | hash-pinned corpus slice, positives plus no-speech controls |
| `vad_replay.py` | replay the device's own chunk trace through voice-activity detection |

Runbook: [stt-benchmark](../../docs/investigations/stt-benchmark.md).

## `llm/` — generation speed, serving shape, and clock headroom

| Tool | Use |
| --- | --- |
| `benchmark_llm_models.sh` | compare the configured GGUF against a pinned ladder, without touching the live config |
| `benchmark_llm_serve.sh` | serving-shape benchmark driven by a manifest |
| `llm_serve_probe.py` | the per-run probe those two drive |
| `*.tsv` | the model/OC manifests they read |
| `oc_step.sh` | one overclock rung at a time, with its verdict |

Runbook: [llm-serving](../../docs/investigations/llm-serving.md).

`oc_step.sh` needs the `hw1-oc-helper` privilege boundary installed
(`bootstrap.sh --with-oc-helper`); everything else here runs as the service
account.
