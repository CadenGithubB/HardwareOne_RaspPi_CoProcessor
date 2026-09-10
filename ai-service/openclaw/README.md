# OpenClaw on the 16 GB CM5

This directory installs a separate, notes-only OpenClaw service on a Raspberry
Pi 5 or Compute Module 5. The recommended one-surface entry point is
`../setup.sh`, which installs either the core HardwareOne service or the core
plus this agent. This directory remains the advanced OpenClaw-only path. It is
deliberately not folded into
`hw1-ai-service`: OpenClaw has locked Gateway and model accounts, separate
systemd units, private state, and an Obsidian-compatible Markdown vault.

The Pi does not need the Obsidian desktop application. The vault is an ordinary
Markdown directory with Obsidian frontmatter, links, and an auto-maintained
`Index.md`; Obsidian can open it later on another machine after a deliberate
sync/export design is chosen. The vendored skill is already Linux-safe: its
macOS launchd/config deployment script is not used, and the Gateway supplies
the vault path through `AGENT_NOTES_VAULT`.

The result is agentic in a narrow sense. The local model can choose among ten
typed `note_*` tools and can recall their output in a later session. It cannot
run a shell, browse, send messages, access the UART, administer the Pi, or use a
generic filesystem tool.

## Security architecture

```text
SSH tunnel
    |
    v
loopback-only OpenClaw Gateway  (locked user: openclaw or hw1-openclaw-agent)
    |                            agent sees only note_* schemas
    +--> bearer-authenticated loopback llama-server
    |       (supervised unit; locked user: openclaw-model; one request at a time)
    |
    +--> root-owned agent-notes plugin
             |
             v
       /srv/hw1-openclaw-vaults/main  (group: openclaw-notes)

isolated benchmark             (locked user: openclaw-bench)
    +--> separate state, model port, workspace, and disposable vault
    +--> cannot traverse the production vault
```

The boundaries are intentionally redundant:

- the Gateway identity (`openclaw` by default, or `hw1-openclaw-agent` when a
  human login already owns the name), `openclaw-model`, `openclaw-bench`, and
  `_openclaw-build` are
  locked system users
  with `nologin` shells and no sudo, device, UART, or other admin-capable
  supplementary groups. Only the Gateway identity joins `openclaw-notes`; the
  Gateway and model share only a narrowly scoped model-API credential group.
- Node, OpenClaw, the plugin, the skill, configuration, wrappers, and systemd
  unit are installed root-owned. `npm ci` runs as `_openclaw-build`, never as
  root or the production service account.
- Model/server inputs must be non-set-id, non-group/world-writable, and
  non-replaceable by either service UID. The installer never executes a custom
  server as root. llama.cpp runs eagerly as `openclaw-model`, which cannot
  traverse the vault; the Gateway unit cannot see the model or server files.
  A root-owned readiness helper proves the exact listener PID, executable,
  cgroup, argv, UID, loopback bind, health, and bearer enforcement before the
  Gateway is allowed to start.
- Setup parses the diagnostic JSON instead of trusting command exit codes. It
  requires the exact ten-tool plugin contract, the eligible pinned skill, zero
  critical security findings, and a successful live Gateway deep probe.
- The unit is enabled only as part of the live gate. If health, listener,
  security-audit, model/tool, or cross-session-memory validation fails, cleanup
  disables and stops it so an unvalidated configuration cannot return at boot.
- The Gateway binds to loopback, uses a generated token, disables discovery and
  its terminal, and has no Tailscale exposure. The default systemd policy denies
  all IP traffic except localhost.
- Both services have no capabilities and use `NoNewPrivileges`, a closed device
  policy, private temporary/device namespaces, protected kernel/control-group
  state, a strict system filesystem, explicit writable paths, and separate 11
  GiB model / 2 GiB Gateway hard memory ceilings.
- OpenClaw's generic container sandbox is off. That avoids container overhead
  on the CM5, and it is not the boundary for this trusted plugin anyway: plugin
  code runs in the Gateway process and performs the vault I/O. The OS account,
  systemd confinement, root-owned code, and tool allowlist are the boundary.
- Production replacement-style actions require an approval: every complete
  `note_write` (new or existing), moving a note, archiving a note, or creating a
  previously proposed folder. Gating every full write removes an existence-check
  race; `note_append` remains the additive creation/update path. Archive is a
  recoverable move, not a hard delete.
- Agent mutations are scoped to the dedicated `reference/`, `projects/`, `log/`,
  and `archive/` roots. Top-level vault files are not writable through the
  agent tools, apart from the gateway-managed `Index` metadata.
- The benchmark has a different UID and no vault group. Its approval bypass is
  accepted only for a real disposable vault below
  `/var/lib/hw1-openclaw-bench/` with a private, benchmark-owned sentinel file.

This is a small blast radius, not a proof that model output is trustworthy. A
bug in the root-owned plugin still runs with the production service account's
vault access, and a human added to `openclaw-notes` can edit the vault directly.

## Exact model tool surface

The production and benchmark configurations explicitly allow these tools and no
others:

| Tool | Capability |
| --- | --- |
| `note_write` | Create a note or replace its complete body. Every production call requires approval. |
| `note_append` | Add a timestamped entry and optionally merge tags. |
| `note_read` | Read a full note or one heading, with an optional character cap. |
| `note_tag` | Add or remove YAML-frontmatter tags without changing the body. |
| `note_search` | Ranked text/tag search with folder, date, exclusion, and result-count filters. |
| `note_list` | List note ids, sizes, modification times, and tags. |
| `note_folders` | List durable topic folders, counts, and sample note ids. |
| `note_move` | Rename/move a note and rewrite matching wikilinks; production approval required. |
| `note_archive` | Move a note under `archive/`, add the `archived` tag, rewrite links, and remove it from the Index map; production approval required. |
| `note_backlinks` | Find notes that wikilink to a given note. |

An explicit allowlist means that installing another OpenClaw plugin does not
silently expose its tools to the model. Do not replace it with `"*"` or add
`exec`, `process`, generic filesystem, browser, web, messaging, node, cron, or
elevated tools without treating that as a new security design.

## Persistent memory and its limits

The vault is ordinary Markdown that Obsidian can open. Durable facts live below
`reference/`, ongoing state below `projects/`, short-lived journal entries below
`log/`, and soft-deleted notes below `archive/`. The plugin maintains the lower
map in `Index.md`; curated text above that map remains editable. YAML
frontmatter holds `created`, `updated`, and `tags`.

The HardwareOne plugin snapshot enforces the following bounds:

| Bound | Value / behavior |
| --- | --- |
| Production vault quota | 512 MiB, enforced before atomic writes. |
| Benchmark vault quota | 64 MiB. |
| One note | At most 5,000,000 UTF-8 bytes after normalization. |
| Title | At most 240 input characters; unsafe characters and `.md` suffixes are normalized. |
| Folder depth | At most three path levels; deeper input is folded into the note name. |
| `note_read` return | At most 200,000 characters; callers should normally request much less. |
| Search results | 25 by default, 100 maximum. Search is a full vault scan, so latency grows with vault size. |
| Content type | Markdown/plain text only. Emoji and CJK characters are removed by this plugin snapshot; binary attachments are not agent memory. |
| Deletion | No hard-delete tool. `note_archive` is the supported removal path. |

Resolved note paths and every existing parent component are checked for symlinks
and confinement beneath the configured vault. Writes use a private temporary
file and atomic rename. Mutations are serialized inside one Gateway process.
Those checks protect the host boundary; they do not make the facts in a note
correct.

This installer creates the vault; it does not install the Obsidian desktop app
or choose a sync product. With `--operator <user>`, that human account can open
the path through SSHFS or a separately configured sync service running as the
human—not as `openclaw`. That option is a trust decision, not a sandbox: a
malicious operator with concurrent rename/symlink access could race POSIX
pathname checks and use the Gateway as a confused deputy. Do not add an
untrusted human or sync daemon; omit `--operator` for the strongest boundary.
Keep the Pi copy dedicated to this agent. Synced edits
are immediately eligible for `note_search`/`note_read` and must therefore be
treated as untrusted prompt input. The plugin ignores dotfiles, so Obsidian's
`.obsidian/` metadata is not agent memory.

### Treat every note as untrusted model input

`note_read` and `note_search` return vault text to the model. A Markdown note can
therefore contain instructions that act like prompt injection, whether inserted
maliciously, copied from the web, synced from another machine, or written by a
confused earlier agent. The plugin's hook-security settings are not a Markdown
content sanitizer.

Practical rules:

- Do not point this service at a general-purpose or publicly synced vault. Use a
  dedicated vault and review any imported material first.
- Do not store passwords, tokens, private keys, or recovery codes in notes. The
  model can read them, and benchmark/session evidence may retain tool results.
- Keep backups or versioned snapshots outside the service account's write scope.
  Approval protects selected mutations; it does not authenticate note content.
- Treat `Index` and personal notes as private. Do not surface them into shared or
  group conversations.
- Review unexpected new notes, appends, and tag changes. Those actions do not all
  require approval, although the model still has no host, network, or messaging
  tool with which to extend the compromise.
- Leave outbound networking disabled unless the use case genuinely requires it.
  `--allow-network` removes the unit's egress restriction and materially changes
  the threat model.

## Reproducible inputs

| Input | Pin |
| --- | --- |
| Node.js | `24.20.0`, arm64 archive SHA-256 pinned in `bootstrap_openclaw.sh` (the upstream byte count is intentionally not hard-coded because Node's release index is authoritative). |
| OpenClaw | `2026.9.2`, npm registry tarball pinned by its published SHA-512 SRI integrity value. |
| Agent Notes bundle | `1.4.0-hw1.5`, installed from the reviewed vendored snapshot and verified by `vendor/agent-notes/SHA256SUMS`. |
| Default GGUF | `/opt/models/LFM2-8B-A1B-UD-Q3_K_XL.gguf`, 3,676,339,264 bytes, SHA-256 `d10253b60d9699c4936a024fded42cba4581dc3640182146cba95fe57c143ac6`. The RPi-only bootstrap downloads and verifies it when absent. |
| llama.cpp | `/opt/llama.cpp/build/bin/llama-server` from exact tag `b10516` (commit `b95502b`), built by this RPi-only bootstrap and required to support `--jinja`. The benchmark also hashes the built binary. |

The notes source reported version 1.4.0 but came from a dirty working tree rather
than a matching upstream release tag. `vendor/agent-notes/SOURCE.md` records that
provenance and the HardwareOne compatibility/security changes. Do not replace
the installed bundle with a mutable user extension or run its upstream macOS
deployment script.

Custom `--model` or `--server-bin` paths are allowed but are not covered by
those default pins; the installer warns and the benchmark records their
SHA-256. They must live outside `/home`, `/root`, `/run/user`, `/tmp`,
`/var/tmp`, and `/dev`, which the hardened unit hides; `/opt` is the recommended
durable location. Record those identities with every custom performance claim.

## Install or reconcile the Pi

Prerequisites are Debian arm64 on a Pi 5/CM5, working DNS/HTTPS for the pinned
downloads, and a known-good SSH login. The installer provisions the default GGUF
and builds the pinned llama.cpp server when they are absent. The 16 GiB SKU is
the intended target; the installer warns below 12 GiB.

For a normal first install, use the single console guide:

```bash
cd ~/hw1-ai-service
./setup.sh --dry-run                 # choose/print a profile
./setup.sh                            # choose core or core + OpenClaw
```

Choose `OpenClaw only` (or use `./setup.sh --mode openclaw-only`) when the
ESP32/UART side is intentionally out of scope.

The advanced OpenClaw-only path below is useful when the core service is already
installed or when reconciling only the agent:

```bash
cd ~/hw1-ai-service/openclaw
sudo ./bootstrap_openclaw.sh --dry-run \
  --agent-user hw1-openclaw-agent \
  --model /opt/models/LFM2-8B-A1B-UD-Q3_K_XL.gguf \
  --with-host-hardening --with-firewall
```

Then run the same command without `--dry-run`:

```bash
sudo ./bootstrap_openclaw.sh --yes \
  --agent-user hw1-openclaw-agent \
  --model /opt/models/LFM2-8B-A1B-UD-Q3_K_XL.gguf \
  --with-host-hardening --with-firewall
```

`--operator` is explicit opt-in to trusted direct vault access; omit it if the
human account should not edit notes on the Pi. Reconciliation requires the
vault group to contain exactly the selected Gateway identity plus that one
operator, so remove an old operator explicitly before changing the option. Log
out and back in after adding the group. Host hardening enables unattended
upgrades and a fail2ban SSH jail on every effective sshd port. The firewall
option resolves and allows those same ports before enabling UFW, but
the installer deliberately does not change SSH authentication. Move to
keys-only SSH separately, only after proving a second key-authenticated login.

The default vault is `/srv/hw1-openclaw-vaults/main`. A custom vault must be one
dedicated direct child of `/srv/hw1-openclaw-vaults/`; the root-owned parent
prevents vault-group members from replacing that path. The installer refuses an
existing unmanaged directory unless `--adopt-vault` is supplied after reviewing
the path and taking a backup. Adoption changes only the vault root's group/mode;
it never recursively takes ownership of existing content, and existing managed
subdirectories must already satisfy the expected group and mode.

The script is intended to be re-run to reconcile the pinned release, configs,
permissions, service, and validation gates. Use `--no-start` only as a staging
mode: it permits a not-yet-installed model/server and therefore skips their
identity, `--jinja`, service-UID access, missing model/server unit-path verification,
live RPC, and memory checks. It
explicitly stops and disables any previously installed unit. A normal
reconciliation quiesces and disables the old unit before replacing managed
inputs, then starts the new unit before its live gates, ensuring those
gates exercise the newly installed runtime and config. Do not use
`--allow-network` merely to make installation downloads work: that flag changes
the installed Gateway's runtime egress policy, not the installer's HTTPS access.

The normal start path deliberately performs two live model turns. The first
writes a unique `log/openclaw-install-smoke-*` note; a fresh session then reads
its secret value without seeing it in the prompt. This can take several minutes
on the CM5. The note remains in the vault, while root-only diagnostic and CLI
JSON is retained under `/etc/hw1-openclaw/verification/` as the activation audit
trail. `--no-start` skips this live gate and the live deep-security probe.

## Operate and inspect

The root-owned wrapper accepts ordinary stable OpenClaw CLI subcommands, then
drops to the locked production account with the exact production environment:

```bash
sudo hw1-openclaw --version
sudo hw1-openclaw config validate --json
sudo hw1-openclaw gateway status --require-rpc --json
sudo hw1-openclaw plugins inspect agent-notes --runtime --json
sudo hw1-openclaw plugins doctor
sudo hw1-openclaw skills info notes --json --agent main
sudo hw1-openclaw security audit --deep --json
```

Service diagnostics remain standard systemd operations:

```bash
sudo systemctl status hw1-openclaw.service
sudo systemctl status hw1-openclaw-model.service
sudo journalctl -u hw1-openclaw.service -b --no-pager
sudo journalctl -u hw1-openclaw-model.service -b --no-pager
sudo ss -ltnp 'sport = :18789'
sudo ss -ltnp 'sport = :18080'
```

Both listeners must remain on loopback. Reach the Control UI through SSH,
not a LAN bind or firewall rule:

```bash
# Workstation terminal (leave running):
ssh -N -L 18789:127.0.0.1:18789 <pi-host>

# Separate Pi shell:
sudo hw1-openclaw dashboard --no-open
```

Open the one-time URL printed by the second command through the tunnel; it also
handles the browser's first device pairing. The generated Gateway token remains
in `/etc/hw1-openclaw/secrets.env`; the separate llama bearer lives in
`/etc/hw1-openclaw-model/model.env`. Both are root-created and narrowly
group-readable. Treat them as secrets even though the listeners are local.

## Benchmark the agent-memory path

Installation also provides the root-owned `hw1-openclaw-benchmark` wrapper. It
creates a private run, confirms that `openclaw-bench` cannot access the
production vault, starts a separate model server, and runs three fresh embedded
agent sessions against a disposable vault. The installer renders the selected
production `--vault` path into this wrapper, including when it is nondefault:

```bash
sudo hw1-openclaw-benchmark
```

See [the benchmark tool README](../tools/openclaw/README.md) for options and
[the investigation runbook](../../docs/investigations/openclaw-agent.md) for
measurement and interpretation. A full run is expected to be slow: it drives an
8B-class quantized model on four CPU threads through multiple model/tool loops.
