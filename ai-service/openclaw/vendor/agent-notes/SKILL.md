---
name: notes
description: Your persistent memory — use the note_* tools to remember, recall, journal, search prior notes, or write durable facts in the host Obsidian vault across sessions.
---

# Notes (your memory)

You wake up fresh each session. Your only persistent memory is a host **Obsidian vault**, reached **only** through the `note_*` tools the OpenClaw gateway runs for you. No direct filesystem access — if it isn't written with `note_*`, it's gone when the session ends.

## Available tools

| Tool | Parameters | What it does |
| ---- | ---------- | ------------ |
| `note_write`  | `{ "title": "<t>", "content": "<md>", "tags": ["a","b"], "createFolder": true }` | Create or fully overwrite a note. Production requires a **system consent popup** for every complete write; prefer `note_append` for additive updates. After a folder proposal, `createFolder: true` also requires consent — do not ask the human to type yes. |
| `note_append` | `{ "title": "<t>", "content": "<md>", "tags": ["a"], "createFolder": true }` | Append a timestamped entry. Same folder popup rule as write. |
| `note_read`   | `{ "title": "<t>", "heading": "<h>", "maxChars": 4000 }` | Read a note. Optional `heading` returns only that section (+ frontmatter). Optional `maxChars` truncates (`truncated: true`). |
| `note_tag`    | `{ "title": "<t>", "add": ["a"], "remove": ["b"] }` | Add/remove Obsidian tags on a note's frontmatter without touching its body. |
| `note_search` | `{ "query": "<text>", "tag": "<tag>", "folder": "reference", "updatedAfter": "YYYY-MM-DD", "exclude": ["log","archive"], "limit": 25 }` | Ranked full-text and/or tag search. Give `query`, `tag`, or both. Filters are optional. |
| `note_list`   | _(none)_ | List every note with size, last-modified time, and tags. |
| `note_folders`| `{ "prefix": "reference" }` | List topic folders with note counts (and sample note ids). Use before filing a new durable note. |
| `note_move`   | `{ "from": "<t>", "to": "<t>", "createFolder": true }` | Rename/move a note; rewrites `[[wikilinks]]`. New destination folders: propose, then `createFolder: true` (consent popup). |
| `note_archive`| `{ "title": "<t>" }` | Soft-delete into `archive/` (not hard delete). Rewrites links; drops the note from the Index map. |
| `note_backlinks` | `{ "title": "<t>" }` | List notes that `[[link]]` to this note. |

Titles use `/` for folders, e.g. `"reference/networking"`, `"log/2026-06-25"`. Prefer the `savedAs` id the tools return.

## How your memory is organized

| Note / folder | What goes there |
| ------------- | --------------- |
| `Index` | Your front door. The **top** is curated long-term memory — the distilled essence, in your own words, which you keep current. The **Notes map** at the bottom is maintained for you automatically: every durable note is listed there, so you never register notes by hand. Read it first. |
| `reference/<topic>` | Durable facts, configs, how-tos, lessons. One topic per note. **This is your long-term memory.** |
| `projects/<name>` | State of ongoing work — where you left off, decisions, next steps. |
| `log/<YYYY-MM-DD>` | Raw daily journal. Ephemeral — an admin prunes old logs, so anything worth keeping must graduate to `reference/`. |
| `archive/` | Soft-deleted notes (`note_archive`). Not listed in the Index map; use only via archive/move, not as a filing target for new memory. |

## Writing durable notes well

A `reference/` note is something you'll re-read for months — make it worth re-reading:

- **Tight and factual — a sharp wiki stub, not an essay and not a telegram.** Normal readable prose is good; a short intro line plus a focused list or a few short paragraphs. Cut philosophy, marketing language, emoji, and repetition. Capture the facts and how they relate, not a narrative.
- **Durable facts only — keep volatile data out.** Record what stays true (what a thing *is*, how parts connect, how it works). Don't freeze things that change and that you can look up live — last-updated dates, version numbers, current status. Note where that data comes from instead of snapshotting it.
- **Only `[[link]]` to notes that exist.** Link to a note that's already in the vault, or one you're creating in the same pass. A `[[link]]` to a note you haven't written does **not** create it — it leaves a dangling placeholder (a phantom node in the graph). If something deserves its own note, write that note now; otherwise just describe it in prose. No promise-links to notes that don't exist, and no hub notes that link to sub-notes you haven't created.
- **One topic per note.** If it sprawls, split it.

## Topic folders

Durable notes live under `reference/`. Folders are **meaning buckets**, not junk drawers.

| Folder | What goes there | What does NOT |
| --- | --- | --- |
| `reference/<project>` | A specific project, codebase, or device | Unrelated misc |
| `reference/homelab` | Home tech: machines, network, services, monitoring | Job/employer stuff |
| `reference/openclaw` | This agent's stack: gateway, models, skills, plugins, recovery | The human's employer work |
| `reference/work` | The human's **employment / job** (employer, career, workplace) | Agent tasks, tests, homelab, random scratch |
| `reference/people` | People and contacts | Generic topics |
| `reference/preferences` | How the human likes things done; their profile | One-off facts about systems |

### Filing checklist (do this before every new durable note)

Strike the middle ground: **search first**, reuse only on a **real** fit, otherwise **propose** a new folder — never shoehorn.

1. **Search existing notes** — `note_search` with a few keywords for the topic (`folder: "reference"` and/or `exclude: ["log","archive"]`). If a good note already exists, `note_append` / `note_write` **that** note instead of making a sibling.
2. **Survey folders** — `note_folders` (or the `folders` list from a prior write, or the Index Notes map). Read the table above. Pick a folder only if the note's subject clearly belongs there.
3. **Reuse only on a clear fit.** Same project, same system, same person — yes. Vague overlap ("it's work I'm doing" → `reference/work`) — **no**. `work` means employment, not "the agent worked on it."
4. **If nothing fits** — write to `reference/<short-topic>` (or the natural path you want). The vault collapses unapproved paths and returns `folderApprovalNeeded`. **Do not ask the human to type yes.** Immediately retry with the same path and `createFolder: true` (boolean). That raises a **system consent popup**. If they deny, leave the collapsed note or archive it.
5. **Ask once in chat only if the popup cannot be shown** (tool error says approvals unavailable). Otherwise the popup is the approval.
6. **Near-duplicates** — the vault may auto-merge `network` → `networking`. Trust that only for spelling/plural variants of the **same** topic, not cross-topic filing.
7. **Batch at curation** — several loose notes that deserve a shared home → propose one folder for the group, then one `createFolder: true` retry (one popup).

Ephemeral / test / scratch material belongs in `log/<today>` (or don't file it), not under `reference/work`.

## Tagging notes

Tags are a second organizing axis on top of folders — cheap to add, and they power `note_search`'s `tag` filter and Obsidian's tag pane. Tag a durable note with what it's *about* when a folder alone won't surface it later (a topic, a project, a status). Pass `tags` on `note_write` / `note_append`, or adjust later with `note_tag`. Keep tags short and lowercase (`homelab`, `networking`, or `work/projectA` to nest); **reuse existing tags** — check the **Tags** section of the `Index` — rather than coining near-duplicates. Tags live in the note's YAML frontmatter, so Obsidian reads them as properties.

## Workflow

### 1. At session start
Prefer cheap recall first:
1. `note_search` for the topic at hand (optionally `exclude: ["log","archive"]`, or `folder: "reference"`).
2. `note_read` hits with `maxChars` (or a `heading`) so you don't dump huge notes into context.
3. If you need the big picture: `note_read` `Index` (curated memory + map), then `note_read` `log/<today>` (and yesterday) for recent context.

### 2. Writing it down — no mental notes
Follow the **Filing checklist** above before durable writes.
- Durable fact / config / lesson → `note_write` `reference/<topic>` (see *Writing durable notes well*). It appears in the Index map automatically — no manual pointer needed.
- Progress on an ongoing **project** → `note_append` `projects/<name>` (not `reference/work` unless it is literally job-related).
- "Remember this" / what happened → `note_append` `log/<today>`.
- When a fact is important enough to surface up front, add it to `Index`'s **curated** section: `note_append` `Index` folds a line into your curated memory, and `note_write` `Index` rewrites the whole curated section. **Both edit only the text above the Notes map — the map maintains itself, and neither can clobber it.** (`Index` is reserved for your memory index; don't reuse it as a topic title — file topics under `reference/`.)

### 3. Before saying "I don't remember"
`note_search` a keyword first — your past self may have written it down. Use `note_backlinks` when you have a note and want "what else points here?"

### 4. Reorganize (don't orphan)
- Wrong folder / rename → `note_move` `{ from, to }` (links rewrite for you).
- No longer needed but not wrong → `note_archive` (soft-delete). **You cannot hard-delete notes.**

### 5. Curate (heartbeats, every few days)

Copy this checklist and work it in order:

```
Curation:
- [ ] 1. note_search folder=log (and/or updatedAfter=<YYYY-MM-DD ~7 days ago>); skim recent log notes
- [ ] 2. Pick facts worth keeping → note_write / note_append reference/<topic>
- [ ] 3. Fix dangling links listed in Index (create the note, move, or remove the link)
- [ ] 4. note_move misplaced durable notes into the right folder
- [ ] 5. note_archive noise / superseded stubs (optional)
- [ ] 6. Refresh Index curated section (note_write Index) with distilled headlines
- [ ] 7. If several loose notes need a new folder, propose once (batch), wait for yes
```

Logs get pruned; `reference/` and `Index` are forever. `archive/` is out of the map on purpose.

## Common recipes

- **Remember a fact:** `note_search` + `note_folders` first; then `note_write` `reference/<topic>` only on a clear folder fit (or propose a new folder). Never dump agent/test scratch into `reference/work`.
- **Log the day:** `note_append` `log/<today>` as things happen.
- **Resume work:** `note_read` `projects/<name>` (maybe with `heading: "Next steps"`) → `note_append` it as you progress.
- **Recall:** `note_search` `<keyword>` → `note_read` the hit (with `maxChars` if large).
- **Rename:** `note_move` `{ "from": "reference/old", "to": "reference/homelab/old" }`.
- **Soft-delete:** `note_archive` `{ "title": "reference/obsolete" }`.
- **What links here:** `note_backlinks` `{ "title": "reference/homelab/router" }`.

## Constraints

- **Read tool errors.** Failures return `{ ok: false, error, hint? }` (reads use `found: false` + `error`/`hint`). Fix the args from the hint — do not retry the same bad call.
- **`createFolder` is a JSON boolean** and raises a **system consent popup**. Only `true` counts. Never pass the string `"true"` / `"false"`. Do not ask the human to type yes in chat for folder creation.
- **Trust `savedAs`.** Titles are sanitized (no `..`, control chars, backticks); the path you typed may not be the path on disk. **Never put `.md` in titles** — the plugin adds the extension; including it creates confusion (and used to create `name.md.md` files).
- **You cannot delete notes.** Use `note_archive` to soft-delete. An admin cron may also prune old `log/`; everything else is permanent unless archived. To correct a note, overwrite it with `note_write`.
- **File under `reference/`, `projects/`, or `log/`** — not the vault root. Root-level writes often hit host permission errors on shared vaults.
- **Titles map to at most three folder levels**; deeper ones fold into the note name, and a folder you haven't gotten approved yet files the note at its base. Trust the `savedAs` the result returns (and the Index map), not the exact title you typed — and if the result shows `overwrote`, you replaced an existing note.
- Don't surface `Index` or personal memory in shared/group contexts — it's private.
- Do not paste the Notes-map HTML comment fences into note bodies or Index curated text — the gateway strips them, and inventing them will not give you control of the map.
