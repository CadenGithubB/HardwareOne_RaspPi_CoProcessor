# Agent Notes plugin

Registers ten OpenClaw gateway tools — `note_write`, `note_append`, `note_read`,
`note_tag`, `note_search`, `note_list`, `note_folders`, `note_move`, `note_archive`,
`note_backlinks` — that let the sandboxed agent persist Markdown notes in a host
Obsidian vault (default `/Users/Shared/Openclaw Vault`, override
`AGENT_NOTES_VAULT`). The agent has no direct filesystem access; these tools are
the only path to its memory.

## Files

| File | Purpose |
|------|---------|
| `openclaw.plugin.json` | Manifest — `contracts.tools` lists the ten tools (`id: agent-notes`). |
| `index.js` | The tools + vault logic: title→path sanitization (no traversal), the auto-maintained `Index` map, propose-then-popup folders (`requireApproval` on `createFolder: true`), near-duplicate folder merge, move/archive with link rewrite, and English-plaintext normalization. |
| `deploy.sh` | Install/restore the plugin, wire `openclaw.json`, and restart the gateway. |

## Deploy

Run `./deploy.sh` on the host (see the top-level [README](../README.md)). It
installs to the user-level `~/.openclaw/extensions/agent-notes/` — so it
**survives `npm update -g openclaw`** and needs no `plugin-entry-<hash>`
re-pointing.

## Behavior notes

- A title maps to `<vault>/<title>.md`; `..`, control chars, and excess depth are
  stripped, and the resolved path is confined to the vault (a traversal title is
  rejected). Titles that already end in `.md` have that extension stripped so
  files are never written as `name.md.md`.
- **Durable** notes (everything outside `log/` and `archive/`) are auto-listed in
  the maintained map at the bottom of the `Index` note; the top of `Index` is the
  agent's own curated memory. `log/` is ephemeral; `archive/` is soft-deleted.
- New topic folders must be **proposed**, then retried with `createFolder: true`,
  which raises a system consent popup (`requireApproval`). `createFolder: true` is
  honored only after a prior proposal, so the agent can't sprawl the vault.
- `note_folders` supports search-first filing; skill guidance forbids shoehorning
  into vaguely related folders (e.g. `reference/work` = employment).
- `note_move` / `note_archive` rewrite `[[wikilinks]]` across the vault (full path,
  and unique basename links) so the graph stays intact.
