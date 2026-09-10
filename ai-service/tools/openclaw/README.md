# OpenClaw agent-memory benchmark

This is an end-to-end correctness and wall-latency benchmark for the isolated
OpenClaw + Agent Notes + local llama-server path. Each run also performs a
direct streaming model probe and records TTFT, end-to-end latency, completion
token count, and decode tokens/second separately from agent/tool latency. It
never points OpenClaw at the production Obsidian vault.

Run it only after [`bootstrap_openclaw.sh`](../../openclaw/README.md) has
installed the pinned runtime, benchmark configuration, dedicated identities,
and root-owned harness assets:

```bash
sudo hw1-openclaw-benchmark
```

The installed wrapper is the supported entry point. It holds the benchmark
lock, validates paths and ownership, checks for competing services, creates the
evidence directory, drops all agent work to `openclaw-bench`, and restores what
it stopped. Do not run `openclaw_memory_probe.py` as root or repoint its vault
arguments at real notes.

## Files

| File | Purpose |
| --- | --- |
| `benchmark_openclaw.sh` | Root/operator wrapper: exclusive lock, contention policy, private run directory, host evidence, privilege drop, cleanup, and input hashes. Installed as `hw1-openclaw-benchmark`. |
| `openclaw_memory_probe.py` | Runs one benchmark llama-server, invokes three fresh `openclaw agent --local` sessions per repeat, parses exact session transcripts, and grades vault state. |
| `openclaw_memory_cases.json` | Hash-stable prompts, fixture data, expected tool calls, and expected final replies for schema version 1. |

## What one repeat proves

Every repeat starts with a new private state directory and an empty disposable
vault. The grader requires the exact calls in the table; an extra call, retry,
missing result, malformed result, or different order is a benchmark failure.

| Case | Required sequence | Decisive assertion |
| --- | --- | --- |
| `write_read` | `note_search` → `note_folders` → `note_write` → `note_read` | The note body/tags and maintained Index are correct, no folder proposal was created, and the final reply is exactly `WRITE_READ_OK`. |
| `cross_session_recall` | `note_search` → `note_read` in a brand-new session | The search preview deliberately omits the answer. The agent must read the persistent note and reply exactly `delta-4829`; read-only access must not change the vault. |
| `archive_verify` | `note_archive` → `note_search` in another new session | The source disappears, the archive retains body/tags plus `archived`, the Index drops the note, excluded search returns zero, and the final reply is exactly `ARCHIVE_OK`. |

The transcript supplies proof that the agent actually selected each tool and
received one matching successful result. The disposable vault and its hashes
are canonical proof of side effects. A final answer alone is never enough.

## Options

Start with one repeat as a functional smoke:

```bash
systemctl --user stop hw1-ai-service.service
sudo hw1-openclaw-benchmark
```

`hw1-ai-service` is a user unit, so the wrapper detects its executable process
rather than querying a nonexistent system unit. A short stability window also
catches its configured automatic restart before measurement begins.

Use repeated fresh scenarios for an agent-reliability result:

```bash
sudo hw1-openclaw-benchmark --repeats 3
```

The direct model probe can be repeated independently, or disabled when a run
should contain only the agent lifecycle:

```bash
sudo hw1-openclaw-benchmark --model-probes 5 --model-max-tokens 64
sudo hw1-openclaw-benchmark --model-probes 0
```

The default per-case deadline is 600 seconds. A slower model can be given more
time, up to the harness limit, without converting a timed-out observation into
a completed latency:

```bash
sudo hw1-openclaw-benchmark --repeats 3 --case-timeout 900
```

The wrapper always stops the managed production OpenClaw unit for isolation and
restores it afterward. By default it also refuses other competing
AI/OpenClaw/model processes so the latency has an interpretable owner.
`--allow-contention` permits those other processes only for the deliberately
different question of how badly the shared CM5 degrades under overlap:

```bash
sudo hw1-openclaw-benchmark --allow-contention
```

Even when the functional probe passes, contention marks the summary
`FAIL/TAINTED` and the wrapper exits nonzero. Never compare that result to a
clean baseline. Run `sudo hw1-openclaw-benchmark --help` for the installed
wrapper's exact limits and any additional diagnostic options.

## Isolation contract

The wrapper and probe fail closed unless all of these remain true:

- the agent process runs as the locked `openclaw-bench` UID;
- its state, home, workspace, config copy, sessions, and vault are private and
  below the current run directory in `/var/lib/hw1-openclaw-bench/`;
- the production vault is not readable or traversable by that UID;
- the disposable vault is a real directory, not a symlink, and contains the
  benchmark-owned private `.hw1-openclaw-benchmark-vault` sentinel;
- `AGENT_NOTES_DESTRUCTIVE_APPROVALS=0` is accompanied by the validated
  benchmark root; the same bypass is rejected for production paths;
- the benchmark uses its own model port and `--local` embedded agent sessions,
  never the production Gateway process or session store.

An OpenClaw agent id is not isolation. The notes plugin captures its vault path
when its process loads, so two agents in one Gateway would still share the same
vault and Index.

## Results and evidence

Runs are retained below:

```text
/var/lib/hw1-openclaw-bench/results/<UTC timestamp>-<random>/
```

`/var/lib/hw1-openclaw-bench/results/latest` is an atomically replaced,
root-owned pointer to the newest run. The wrapper directory is root-owned and
contains a private `probe/` child owned by `openclaw-bench`; this prevents the
untrusted benchmark runtime from planting paths later written by root. Each
result keeps partial artifacts even on failure, including:

- `probe/run.json`, the overall status, identities/hashes, model startup time, and
  repeat summaries;
- `run-metadata.env`, `summary.md`, the llama-server log, and wrapper
  preflight/host evidence;
- `telemetry.tsv` samples temperature, throttle flags, core voltage, ARM clock,
  available memory, swap use, llama/Node RSS, and load every two seconds;
- `probe/repeat-NN/repeat.json` plus the final disposable vault and state;
- one directory per case. `case.json` and before/after vault manifests survive
  failures; prompts and partial stdout/stderr survive when invocation reached
  them. A successful parsed case additionally has copied session JSONL,
  normalized calls/results, assertions, final text, and wall time;
- `input-sha256.txt` for the probe, cases, configs, runtimes, server, and model;
  `probe/run.json`, each `case.json`, and the vault manifests also retain the hashes
  used by the grader.

A case deadline is reported as right-censored. The elapsed observation window
is not a completion latency and must not be averaged with successful cases.
OpenClaw's tool summary is useful for call count/tool time. The direct model
probe in `run.json` is the source for model TTFT and decode throughput; agent
turn wall time includes prompt construction, tool calls, filesystem work, and
model passes.

The harness intentionally retains the disposable archived note. Retention makes
the grade auditable and avoids a recursive-delete path anywhere near production
data. Prune old result directories only as a separate, explicit admin action.

## Expected speed

One repeat contains eight successful tool calls across three one-shot embedded
agent sessions. Each tool decision and final reply can require another local
model pass. With the default 8B-class quantized model, four CPU threads, 16k
context, thinking disabled, and parallelism one, several minutes per repeat is
plausible. Model startup is recorded separately from turn wall time.

Use [`docs/investigations/openclaw-agent.md`](../../../docs/investigations/openclaw-agent.md)
for the measurement procedure, validity checks, and report template.
