# Vendored Agent Notes source

This directory is a reviewed snapshot of the local
`Openclaw_Obsidian_Notes_Skill` source tree taken on 2026-08-28. The source
working tree reported version 1.4.0 but contained changes beyond Git
commit `7aa249e47b38a96089c4bf462015b9edd24a0c30` (which identifies version
1.2.0), so the files are pinned by `SHA256SUMS` rather than by that commit.

The HardwareOne copy carries compatibility/security adaptations for pinned
OpenClaw 2026.9.2:

- plugin domain objects are wrapped with the stable `jsonResult` tool-result
  adapter;
- the unsupported `triggers` frontmatter field is folded into the skill
  description;
- the host/plugin compatibility range is explicit in the package metadata;
- a missing/relative/root vault path fails closed, every existing parent path
  is checked for symlinks, and atomic writes use random exclusive temp files;
- a 512 MiB total-vault quota complements the upstream per-note bound;
- archive collision suffixes reserve their filename space and stop at a
  fail-closed bound instead of looping on 100-character note ids;
- dot-prefixed title segments are rejected so agent-created hidden notes cannot
  escape vault enumeration or quota accounting;
- all note tools request OpenClaw's sequential execution mode, keeping folder
  consent checks and mutation/read ordering in one serialized timeline;
- every complete `note_write`, plus moves and archives, requires production
  approval, eliminating the new-note/overwrite existence-check race; the
  unattended bypass is restricted to the dedicated benchmark UID's private,
  sentinel-marked vault below `/var/lib/hw1-openclaw-bench/`; and
- production can opt into `0660` note files for the vault-only Obsidian group;
  isolated/default writes remain owner-only.
- agent-created notes, moves, archives, and tags are scoped to the dedicated
  `reference/`, `projects/`, `log/`, and `archive/` roots; top-level vault files
  are read-only to the agent except for the gateway-managed Index metadata.

The upstream `plugin/deploy.sh` is intentionally not included or executed. It
targets macOS/launchd, edits a mutable user config, and does not install the
skill instructions. `bootstrap_openclaw.sh` installs this snapshot root-owned
under `/opt/hw1-openclaw/agent-notes`.
