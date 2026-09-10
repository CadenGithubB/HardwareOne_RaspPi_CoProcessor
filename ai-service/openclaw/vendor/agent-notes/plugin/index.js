import { definePluginEntry } from "openclaw/plugin-sdk/plugin-entry";
import { jsonResult } from "openclaw/plugin-sdk/tool-results";
import { randomUUID } from "node:crypto";
import { promises as fs } from "node:fs";
import { lstatSync, realpathSync } from "node:fs";
import * as path from "node:path";

const vaultRaw = process.env.AGENT_NOTES_VAULT;
if (!vaultRaw || !path.isAbsolute(vaultRaw) || path.resolve(vaultRaw) === path.parse(path.resolve(vaultRaw)).root) {
  throw new Error("AGENT_NOTES_VAULT must be an absolute, non-root path");
}
const VAULT = path.resolve(vaultRaw);
const MAX_BYTES = 5_000_000;
const MAX_VAULT_BYTES_DEFAULT = 536_870_912;
const maxVaultBytesRaw = Number(process.env.AGENT_NOTES_MAX_VAULT_BYTES ?? MAX_VAULT_BYTES_DEFAULT);
const MAX_VAULT_BYTES = Number.isSafeInteger(maxVaultBytesRaw) && maxVaultBytesRaw >= MAX_BYTES
  ? maxVaultBytesRaw
  : MAX_VAULT_BYTES_DEFAULT;
// Production may opt into a vault-only Unix group for an Obsidian operator.
// The default remains owner-only, which is also what the isolated benchmark uses.
const NOTE_FILE_MODE = process.env.AGENT_NOTES_SHARED_GROUP === "1" ? 0o660 : 0o600;
// Production defaults to approval for destructive mutations. The benchmark
// harness may disable only these prompts inside its separate UID/state/vault.
const DESTRUCTIVE_APPROVALS = process.env.AGENT_NOTES_DESTRUCTIVE_APPROVALS !== "0";
if (!DESTRUCTIVE_APPROVALS) {
  const benchRootRaw = process.env.AGENT_NOTES_BENCHMARK_ROOT;
  if (!benchRootRaw || !path.isAbsolute(benchRootRaw)) {
    throw new Error("destructive-approval bypass requires AGENT_NOTES_BENCHMARK_ROOT");
  }
  const benchRoot = realpathSync(benchRootRaw);
  const vaultReal = realpathSync(VAULT);
  const rel = path.relative(benchRoot, vaultReal);
  if (!benchRoot.startsWith("/var/lib/hw1-openclaw-bench/") || rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel)) {
    throw new Error("destructive-approval bypass is restricted to an isolated hw1-openclaw-bench vault");
  }
  const sentinel = lstatSync(path.join(vaultReal, ".hw1-openclaw-benchmark-vault"));
  if (!sentinel.isFile() || sentinel.isSymbolicLink() || sentinel.uid !== process.getuid() || (sentinel.mode & 0o077) !== 0) {
    throw new Error("invalid benchmark-vault sentinel ownership or mode");
  }
}
const MAX_DEPTH = 3;
const INDEX_NOTE = "Index";
const PENDING_NOTE = "Pending-Folders";
const ARCHIVE_BASE = "archive";
const KNOWN_BASES = new Set(["reference", "projects", "log", ARCHIVE_BASE]);
// The Gateway operates a dedicated agent vault. Keep all model-created notes
// below these roots; top-level files remain reserved for Index/Pending metadata.
const WRITE_BASES = new Set(["reference", "projects", "log", ARCHIVE_BASE]);
const MAP_START = "<!-- notes-map: auto-maintained, write your curated memory above this line -->";
const MAP_END = "<!-- /notes-map -->";
const INDEX_SCAFFOLD = `# ${INDEX_NOTE}\n\n_Curated long-term memory — write distilled facts above. The map below maintains itself._\n\n`;
const SEARCH_LIMIT_DEFAULT = 25;
const SEARCH_LIMIT_MAX = 100;
const READ_CHARS_MAX = 200_000;
const TITLE_MAX_CHARS = 240;

function stripEmoji(s) { return String(s).replace(/(?:\p{Extended_Pictographic}|[\u{1F1E6}-\u{1F1FF}])(?:[\u{1F3FB}-\u{1F3FF}\uFE0F\u200D])* ?/gu, ""); }
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]/;
function toEnglish(s) {
  const map = { "，":",","。":".","！":"!","？":"?","：":":","；":";","（":"(","）":")","、":",","．":".","　":" " };
  const text = String(s).replace(/[，。！？：；（）、．　]/g, (c) => map[c] ?? "").replace(/[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uff00-\uffef]/g, "");
  return { text, stripped: CJK.test(String(s)) };
}
const clean1 = (s) => toEnglish(stripEmoji(s)).text;

// ---- Agent input guards (structured errors the model can act on) ----
function err(error, extra = {}) { return { ok: false, error, ...extra }; }
function requireString(val, name, { allowEmpty = false } = {}) {
  if (typeof val !== "string") return err(`'${name}' is required and must be a string (got ${val === null ? "null" : typeof val}). Pass plain text, not an object/array.`);
  if (!allowEmpty && !val.trim()) return err(`'${name}' must be a non-empty string.`);
  return null;
}
/** Only literal JSON boolean true counts — string "false"/"true" from the model must not coerce. */
function parseCreateFolder(val) {
  if (val == null || val === false) return { ok: true, value: false };
  if (val === true) return { ok: true, value: true };
  return err(`'createFolder' must be boolean true or false (got ${typeof val}: ${JSON.stringify(val)}). Use true only AFTER proposing the folder (triggers a system consent popup).`);
}
/** Strip control chars and Index map fences so agent text cannot break vault bookkeeping. */
function scrubAgentText(raw) {
  let text = String(raw ?? "").replace(/\u0000/g, "");
  const mapMarkersStripped = text.includes(MAP_START) || text.includes(MAP_END);
  if (mapMarkersStripped) text = text.split(MAP_START).join("").split(MAP_END).join("");
  return { text, mapMarkersStripped };
}
function normalizeNoteBody(text) {
  // If the agent pastes a YAML fence at the top, keep it as visible markdown — never confuse Obsidian/readers.
  let body = String(text);
  if (body.startsWith("---\n") || body.startsWith("---\r\n") || body === "---") {
    body = "— — —" + body.slice(3);
  }
  return body;
}
async function safeCall(fn) {
  try { return await fn(); }
  catch (e) {
    const msg = e?.message ? String(e.message) : "internal error";
    const perm = /EACCES|EPERM/i.test(msg);
    return err(perm ? `permission denied: ${msg}` : `internal error: ${msg}`, {
      hint: perm
        ? "The openclaw user cannot write that vault path. Fix ownership/permissions on AGENT_NOTES_VAULT (and subfolders). Prefer titles under reference/, projects/, or log/ — not the vault root. Omit .md from titles."
        : "Retry with a simpler title/content (omit .md). If it persists, check the vault path and disk permissions.",
    });
  }
}

/** Agents often pass "Note.md"; we store ids without the extension. Strip every trailing .md. */
function stripMdExt(s) { return String(s).replace(/(\.md)+$/gi, ""); }
function cleanSeg(s) {
  const cleaned = stripMdExt(clean1(s).replace(/[\\:`<>|\u0000-\u001f]/g, " ").replace(/\.{2,}/g, " ").replace(/\s+/g, " ").trim());
  if (cleaned.startsWith(".")) throw new Error("note title segments cannot begin with '.'");
  return cleaned;
}
function cleanSegs(title) {
  return String(title).split("/").map(cleanSeg).filter(Boolean);
}
function safeRelPath(title) {
  let parts = String(title).split("/").map(cleanSeg).filter(Boolean);
  if (parts.length > MAX_DEPTH) parts = [...parts.slice(0, MAX_DEPTH - 1), parts.slice(MAX_DEPTH - 1).join("-")];
  if (parts.length === 0) parts.push("Untitled");
  parts[parts.length - 1] = parts[parts.length - 1].slice(0, 100) || "Untitled";
  // Guard: never embed ".md" inside the id; fileForId adds the real extension once.
  parts = parts.map((p) => stripMdExt(p) || "Untitled");
  return parts.join("/") + ".md";
}
function fileForId(id) {
  const root = path.resolve(VAULT);
  const cleanId = stripMdExt(String(id).replace(/\\/g, "/"));
  const p = path.resolve(root, cleanId + ".md");
  // Require path.sep after root so "/vault-evil" cannot prefix-match "/vault".
  if (p !== root && !p.startsWith(root + path.sep)) throw new Error("invalid note title: resolved path escaped the vault");
  return p;
}
const idFromTitle = (title) => stripMdExt(safeRelPath(title));
const fileForTitle = (title) => fileForId(idFromTitle(title));
function writableAgentId(id) {
  const first = String(id).split("/")[0].toLowerCase();
  return WRITE_BASES.has(first);
}
function writableScopeError(id) {
  return err("agent write scope is limited to reference/, projects/, log/, or archive/", {
    note: id,
    hint: "Use one of the dedicated agent-vault folders; Index and Pending-Folders are gateway-managed.",
  });
}

// Reject symlinks in every existing path component, not only at the final note
// path. Without this check, a host-created `vault/reference -> /elsewhere`
// symlink could redirect otherwise sanitized note operations outside the vault.
async function assertSafeVaultPath(candidate) {
  const root = path.resolve(VAULT);
  const absolute = path.resolve(candidate);
  const rel = path.relative(root, absolute);
  if (rel === ".." || rel.startsWith(".." + path.sep) || path.isAbsolute(rel)) {
    throw new Error("invalid note path: resolved path escaped the vault");
  }
  for (const target of [root, absolute]) {
    const parsed = path.parse(target);
    let current = parsed.root;
    const parts = target.slice(parsed.root.length).split(path.sep).filter(Boolean);
    for (const part of parts) {
      current = path.join(current, part);
      let stat;
      try { stat = await fs.lstat(current); }
      catch (e) {
        if (e?.code === "ENOENT") break;
        throw e;
      }
      if (stat.isSymbolicLink()) throw new Error(`refusing symlinked vault path component: ${current}`);
    }
  }
  return absolute;
}
/** Prefer the canonical `<id>.md`; fall back to legacy `<id>.md.md` from older double-extension bugs. */
async function resolveNoteFile(id) {
  const cleanId = stripMdExt(String(id));
  const primary = fileForId(cleanId);
  await assertSafeVaultPath(primary);
  if (await exists(primary)) return primary;
  const root = path.resolve(VAULT);
  const legacy = path.resolve(root, cleanId + ".md.md");
  await assertSafeVaultPath(legacy);
  if (legacy.startsWith(root + path.sep) && (await exists(legacy))) return legacy;
  return primary;
}
async function exists(p) { try { await fs.access(p); return true; } catch { return false; } }
async function ensureDir(file) {
  await assertSafeVaultPath(file);
  await fs.mkdir(path.dirname(file), { recursive: true });
  await assertSafeVaultPath(file);
}
// Serialize every mutating op through one in-process queue, so concurrent tool calls (parallel
// tool_use in a turn, or a second session) can't interleave a read-modify-write and lose an update.
let opTail = Promise.resolve();
function withLock(fn) { const r = opTail.then(fn); opTail = r.then(() => {}, () => {}); return r; }
// Atomic write (temp file + rename) so a concurrent reader never sees a half-written note.
async function atomicWrite(file, data) {
  await assertSafeVaultPath(file);
  const usage = await vaultUsageBytes();
  let previousBytes = 0;
  try { previousBytes = (await fs.stat(file)).size; } catch {}
  const projected = usage - previousBytes + Buffer.byteLength(String(data), "utf8");
  if (projected > MAX_VAULT_BYTES) {
    throw new Error(`vault quota exceeded: projected ${projected} bytes, limit ${MAX_VAULT_BYTES}`);
  }
  const tmp = `${file}.tmp.${process.pid}.${randomUUID()}`;
  try {
    await fs.writeFile(tmp, data, { encoding: "utf8", flag: "wx", mode: NOTE_FILE_MODE });
    await fs.rename(tmp, file);
  } catch (e) {
    try { await fs.unlink(tmp); } catch {}
    throw e;
  }
}
// A symlink sitting at a note path could redirect a read/write outside the vault.
// The agent can't create symlinks (it only writes regular files), so this is
// defense in depth against a host-side compromise. lstat does NOT follow the link.
async function isSymlink(p) { try { return (await fs.lstat(p)).isSymbolicLink(); } catch { return false; } }

async function listNotes() {
  await assertSafeVaultPath(VAULT);
  const out = [];
  const seen = new Map(); // id -> { full, id }  (prefer canonical .md over legacy .md.md)
  async function walk(dir, rel) {
    let ents = []; try { ents = await fs.readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      if (e.name.startsWith(".") || e.isSymbolicLink()) continue;   // skip dotfiles + symlinks (no vault escape)
      const full = path.join(dir, e.name), idPath = rel ? `${rel}/${e.name}` : e.name;
      if (e.isDirectory()) await walk(full, idPath);
      else if (/\.md$/i.test(e.name)) {
        const legacyDouble = /\.md\.md$/i.test(e.name);
        const id = idPath.replace(/(\.md)+$/gi, "");
        const prev = seen.get(id);
        if (!prev || (prev.legacyDouble && !legacyDouble)) seen.set(id, { full, id, legacyDouble });
      }
    }
  }
  await fs.mkdir(VAULT, { recursive: true });
  await assertSafeVaultPath(VAULT);
  await walk(VAULT, "");
  for (const v of seen.values()) out.push({ full: v.full, id: v.id });
  return out;
}
async function vaultUsageBytes() {
  let total = 0;
  for (const note of await listNotes()) {
    try { total += (await fs.stat(note.full)).size; } catch {}
  }
  return total;
}
async function listFolders() {
  await assertSafeVaultPath(VAULT);
  const out = [];
  async function walk(dir, rel) {
    let ents = []; try { ents = await fs.readdir(dir, { withFileTypes: true }); } catch { return; }
    for (const e of ents) {
      if (!e.isDirectory() || e.isSymbolicLink() || e.name.startsWith(".")) continue;
      const id = rel ? `${rel}/${e.name}` : e.name;
      out.push(id); await walk(path.join(dir, e.name), id);
    }
  }
  await walk(path.resolve(VAULT), ""); return out;
}
const stamp = () => new Date().toISOString();
// Local YYYY-MM-DD for the created/updated frontmatter — the one date format Obsidian
// Properties/Dataview always parse as a real (sortable) Date, and unambiguous to read.
const today = () => { const d = new Date(), p = (n) => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };
const base = (t) => clean1(String(t).split("/").pop());
const str = (description) => ({ type: "string", description });
const RESERVED = new Set([INDEX_NOTE.toLowerCase(), PENDING_NOTE.toLowerCase()]);
// Reserved-name and log/archive checks are case-insensitive: the default vault sits on a
// case-insensitive filesystem (macOS/APFS), where "index" and "Index" resolve to the same file.
const isDurable = (id) => {
  const low = String(id).toLowerCase();
  return !RESERVED.has(low) && !low.startsWith("log/") && !low.startsWith(ARCHIVE_BASE + "/");
};
const isReservedId = (id) => RESERVED.has(String(id).toLowerCase());

// ---- Obsidian tags (YAML frontmatter) ----
function sanitizeTag(t) {
  let s = clean1(String(t).trim().replace(/^#+/, "").replace(/['"]/g, ""));
  s = s.replace(/\s+/g, "-").replace(/[^A-Za-z0-9_/-]/g, "").replace(/-{2,}/g, "-").replace(/\/{2,}/g, "/").replace(/^[-/]+|[-/]+$/g, "");
  return s && /[A-Za-z_]/.test(s) ? s.slice(0, 100) : null;   // Obsidian tags need a non-numeric char
}
function sanitizeTags(arr) {
  const out = [], seen = new Set();
  for (const t of Array.isArray(arr) ? arr : []) { const c = sanitizeTag(t); if (c && !seen.has(c.toLowerCase())) { seen.add(c.toLowerCase()); out.push(c); } }
  return out;
}
function splitFront(content) {                                  // -> { front, body }, front="" if no YAML frontmatter
  const s = String(content);
  if (!s.startsWith("---\n")) return { front: "", body: s };
  const lines = s.split("\n");
  let end = -1;
  for (let i = 1; i < lines.length; i++) if (lines[i] === "---") { end = i; break; }
  if (end === -1) return { front: "", body: s };
  return { front: lines.slice(1, end).join("\n") + "\n", body: lines.slice(end + 1).join("\n").replace(/^\n/, "") };
}
function parseTags(front) {
  if (!front) return [];
  const lines = String(front).split("\n"), out = [];
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^tags:\s*(.*)$/i); if (!m) continue;
    const rest = m[1].trim();
    if (rest.startsWith("[")) rest.replace(/^\[|\]$/g, "").split(",").forEach((t) => out.push(t));
    else if (rest) rest.split(/[,\s]+/).forEach((t) => out.push(t));
    else for (let j = i + 1; j < lines.length; j++) { const b = lines[j].match(/^\s*-\s+(.*)$/); if (!b) break; out.push(b[1]); }
    break;
  }
  return sanitizeTags(out);
}
function frontScalar(front, key) {                              // read a single-line scalar frontmatter value (e.g. created/updated)
  if (!front) return null;
  const m = String(front).match(new RegExp(`^${key}:[ \\t]*(.+?)[ \\t]*$`, "mi"));
  return m ? (m[1].replace(/^['"]|['"]$/g, "").trim() || null) : null;
}
function frontOther(front) {                                    // keep human-added frontmatter keys except the ones we manage (tags/created/updated)
  if (!front) return "";
  const lines = String(front).split("\n"), keep = [];
  for (let i = 0; i < lines.length; i++) {
    if (/^(tags|created|updated):\s*(.*)$/i.test(lines[i])) { if (/^tags:\s*$/i.test(lines[i])) while (i + 1 < lines.length && /^\s*-\s+/.test(lines[i + 1])) i++; continue; }
    keep.push(lines[i]);
  }
  return keep.join("\n").trim();
}
function renderFront({ created, updated, tags, other }) {
  const parts = [];
  if (created) parts.push(`created: ${created}`);
  if (updated) parts.push(`updated: ${updated}`);
  if (other && other.trim()) parts.push(other.trim());
  if (tags && tags.length) parts.push(`tags: [${tags.join(", ")}]`);
  return parts.length ? `---\n${parts.join("\n")}\n---\n` : "";
}

function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"); }
function replaceWikiLinks(text, fromId, toId) {
  // [[id]], [[id|display]], [[id#heading]], [[id#heading|display]]
  const re = new RegExp(`\\[\\[${escapeRe(fromId)}(#[^|\\]]*)?(\\|[^\\]]*)?\\]\\]`, "g");
  return String(text).replace(re, (_, hash, disp) => `[[${toId}${hash || ""}${disp || ""}]]`);
}
function extractHeadingSection(body, heading) {
  const want = String(heading).replace(/^#+\s*/, "").trim().toLowerCase();
  if (!want) return null;
  const lines = String(body).split("\n");
  let start = -1, level = 0;
  for (let i = 0; i < lines.length; i++) {
    const m = lines[i].match(/^(#{1,6})\s+(.+?)\s*$/);
    if (m && m[2].trim().toLowerCase() === want) { start = i; level = m[1].length; break; }
  }
  if (start === -1) return null;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    const m = lines[i].match(/^(#{1,6})\s+/);
    if (m && m[1].length <= level) { end = i; break; }
  }
  return lines.slice(start, end).join("\n");
}
function parseDateYmd(s) {
  const m = String(s ?? "").trim().match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? m[0] : null;
}
function folderPrefix(raw) {
  return String(raw ?? "").split("/").map((s) => clean1(s).replace(/[\\:\u0000-\u001f]/g, " ").replace(/\.{2,}/g, " ").replace(/\s+/g, " ").trim()).filter(Boolean).join("/");
}
function shouldExclude(noteId, excludes) {
  const low = String(noteId).toLowerCase();
  for (const ex of excludes) {
    const e = String(ex).toLowerCase().replace(/\/+$/, "");
    if (!e) continue;
    if (low === e || low.startsWith(e + "/")) return true;
  }
  return false;
}

// ---- guardrail #2: canonicalize (reuse near-duplicate folders) ----
const norm = (s) => String(s).toLowerCase().replace(/[^a-z0-9/]/g, "");
function leafMatch(a, b) { if (a === b) return true; for (const s of ["s", "es", "ing"]) if (a === b + s || b === a + s) return true; return false; }
function canonicalMatch(folderId, existing) {
  const tn = norm(folderId), tp = tn.split("/").slice(0, -1).join("/"), tl = tn.split("/").pop();
  for (const f of existing) {
    const nn = norm(f); if (nn === tn) return f;
    const np = nn.split("/").slice(0, -1).join("/"), nl = nn.split("/").pop();
    if (tp === np && tl.length >= 4 && nl.length >= 4 && leafMatch(tl, nl)) return f;
  }
  return null;
}

// ---- guardrail #1 + #4: proposal ledger (propose once; createFolder + consent popup creates) ----
async function readPending() { try { return await fs.readFile(await resolveNoteFile(PENDING_NOTE), "utf8"); } catch { return ""; } }
async function pendingSet() { const set = new Set(); for (const m of (await readPending()).matchAll(/^- `([^`]+)`/gm)) set.add(m[1]); return set; }
async function addPending(folder, noteId) {
  let cur = await readPending(); if (cur.includes("`" + folder + "`")) return;
  if (!cur) cur = `# Pending-Folders\n\n_Folders the agent proposed. Creating one raises a system consent popup when the agent retries with createFolder:true (you do not need to type yes in chat). Delete a line to clear a proposal._\n\n`;
  await ensureDir(fileForId(PENDING_NOTE));
  await atomicWrite(fileForId(PENDING_NOTE), cur + `- \`${folder}\` — proposed for [[${noteId}]] (${stamp()})\n`, "utf8");
}
async function clearPending(folder) {
  const cur = await readPending(); if (!cur) return;
  const next = cur.split("\n").filter((l) => !l.startsWith("- `" + folder + "`")).join("\n");
  if (next !== cur) await atomicWrite(fileForId(PENDING_NOTE), next, "utf8");
}

async function resolveTarget(title, createFolder) {
  const id = idFromTitle(title), segs = id.split("/");
  const folderSegs = segs.slice(0, -1), noteName = segs[segs.length - 1];
  if (folderSegs.length === 0 || (folderSegs.length === 1 && KNOWN_BASES.has(folderSegs[0]))) return { id, file: fileForId(id) };
  const folderId = folderSegs.join("/");
  const folderPath = path.resolve(VAULT, folderId);
  await assertSafeVaultPath(folderPath);
  if (await exists(folderPath)) return { id, file: fileForId(id) };           // folder exists → file in
  const pending = await pendingSet();
  if (createFolder && pending.has(folderId)) return { id, file: fileForId(id), created: folderId }; // #1: approved & proposed → create
  const match = canonicalMatch(folderId, await listFolders());                                     // #2: near-duplicate → reuse
  if (match) { const newId = match + "/" + noteName; return { id: newId, file: fileForId(newId), useExisting: { matched: match, from: folderId } }; }
  const known = KNOWN_BASES.has(segs[0]);
  const foldPath = folderSegs.slice(known ? 1 : 0).join("-");
  const collapsedLeaf = (foldPath ? foldPath + "-" + noteName : noteName).slice(0, 100);
  const collapsedId = (known ? segs[0] + "/" : "") + collapsedLeaf;                  // else collapse + ask-once
  return { id: collapsedId, file: fileForId(collapsedId), proposedFolder: folderId, firstAsk: !pending.has(folderId) };
}

function blurb(text) {
  for (const raw of splitFront(String(text)).body.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#") || line.startsWith("<!--") || /^_updated /.test(line)) continue;
    return line.replace(/[*_`[\]]/g, "").slice(0, 100);
  }
  return "";
}
async function rebuildIndex() {
  const all = await listNotes();
  const idSet = new Set(all.map((n) => n.id)), baseSet = new Map();
  for (const n of all) { const b = n.id.split("/").pop(); if (!baseSet.has(b)) baseSet.set(b, []); baseSet.get(b).push(n.id); }
  const notes = all.filter((n) => isDurable(n.id)).sort((a, b) => (a.id < b.id ? -1 : 1));
  const groups = new Map(), tagIndex = new Map(), danglers = [], seenDang = new Set();
  for (const n of notes) {
    const i = n.id.lastIndexOf("/"); const folder = i === -1 ? "(top level)" : n.id.slice(0, i);
    let text = ""; try { text = await fs.readFile(n.full, "utf8"); } catch {}
    const { front, body } = splitFront(text);
    const tags = parseTags(front);
    for (const t of tags) { if (!tagIndex.has(t)) tagIndex.set(t, []); tagIndex.get(t).push(n.id); }
    const b = blurb(text), tg = tags.length ? "  " + tags.map((t) => "#" + t).join(" ") : "";
    if (!groups.has(folder)) groups.set(folder, []);
    groups.get(folder).push(`- [[${n.id}]]${b ? " — " + b : ""}${tg}`);
    for (const m of body.matchAll(/\[\[([^\]]+)\]\]/g)) {                 // #3a: flag links to notes that don't exist
      const target = m[1].split("|")[0].split("#")[0].trim();
      if (!target) continue;
      const found = target.includes("/") ? idSet.has(target) : (baseSet.get(target)?.length > 0);
      const key = n.id + " -> " + target;
      if (!found && !seenDang.has(key)) { seenDang.add(key); danglers.push({ from: n.id, to: target }); }
    }
  }
  const keys = [...groups.keys()].sort();
  const folderPart = keys.map((f) => `### ${f}\n${groups.get(f).join("\n")}`).join("\n\n") || "_(none yet)_";
  const tagKeys = [...tagIndex.keys()].sort();
  const tagPart = tagKeys.length ? `\n\n### Tags\n${tagKeys.map((t) => `- #${t} — ${tagIndex.get(t).map((id) => `[[${id}]]`).join(", ")}`).join("\n")}` : "";
  const dangPart = danglers.length ? `\n\n### ⚠ Dangling links (create the note, or remove the link)\n${danglers.slice(0, 50).map((d) => `- [[${d.from}]] → \`${d.to}\``).join("\n")}` : "";
  const block = `${MAP_START}\n\n## Notes map (${notes.length})\n\n${folderPart}${tagPart}${dangPart}\n\n${MAP_END}\n`;
  const idxFile = fileForId(INDEX_NOTE);
  await assertSafeVaultPath(idxFile);
  let cur = ""; try { cur = await fs.readFile(idxFile, "utf8"); } catch {}
  const s = cur.indexOf(MAP_START), e = cur.indexOf(MAP_END);
  let next;
  if (s !== -1 && e !== -1 && e > s) next = cur.slice(0, s) + block + cur.slice(e + MAP_END.length).replace(/^\n/, "");
  else if (cur.trim()) next = cur.replace(/\s*$/, "\n\n") + block;
  else next = INDEX_SCAFFOLD + block;
  await ensureDir(idxFile); await atomicWrite(idxFile, next, "utf8");
}

async function tryRebuildIndex() {
  try { await rebuildIndex(); return null; }
  catch (e) { return e?.message ? String(e.message) : "Index rebuild failed"; }
}
function withIndexResult(out, indexErr) {
  if (!indexErr) return out;
  out.indexRebuildError = indexErr;
  out.hint = `${out.hint ? out.hint + " " : ""}Note was saved, but Index map rebuild failed — check vault permissions on Index.md.`;
  return out;
}

// The Index note is editable, but only its curated region (above MAP_START). Routing EVERY write
// that resolves to the Index through here means an ordinary note_write/note_append can never wipe
// the auto-map — and, with the case-insensitive reserved check below, neither can a lowercase
// "index" (the same file as "Index" on a case-insensitive vault like macOS/APFS).
async function curatedWrite(content, append) {
  const idxFile = fileForId(INDEX_NOTE);
  await assertSafeVaultPath(idxFile);
  if (await isSymlink(idxFile)) return err("refusing to write through a symlinked note path");
  const scrubbed = scrubAgentText(content);
  const { text: cleaned, stripped } = toEnglish(stripEmoji(scrubbed.text));
  const text = normalizeNoteBody(cleaned);
  let cur = ""; try { cur = await fs.readFile(idxFile, "utf8"); } catch {}
  const s = cur.indexOf(MAP_START);
  const curated = s !== -1 ? cur.slice(0, s) : cur, mapBlock = s !== -1 ? cur.slice(s) : "";
  const head = curated.trim() ? curated : INDEX_SCAFFOLD;
  const newCurated = append ? head.replace(/\s*$/, "\n\n") + text + "\n\n" : INDEX_SCAFFOLD + text + "\n\n";
  // Refuse if curated region somehow still contains map fences (defense in depth).
  if (newCurated.includes(MAP_START) || newCurated.includes(MAP_END)) {
    return err("refusing Index write: content would break the auto-maintained Notes map. Remove any notes-map HTML comments and retry.");
  }
  await ensureDir(idxFile);
  await atomicWrite(idxFile, newCurated + mapBlock, "utf8");
  let indexErr = null;
  if (!mapBlock) indexErr = await tryRebuildIndex();
  return withIndexResult({ ok: true, savedAs: INDEX_NOTE, region: "curated", indexed: false, nonEnglishRemoved: stripped || undefined,
    mapMarkersStripped: scrubbed.mapMarkersStripped || undefined,
    note: append ? "Folded into Index's curated memory (above the auto-map); the map is untouched."
                 : "Replaced Index's curated memory (above the auto-map); the map is untouched." }, indexErr);
}

const doWrite = (title, content, createFolder, append, tags) => withLock(() => safeCall(() => doWriteImpl(title, content, createFolder, append, tags)));
async function doWriteImpl(title, content, createFolder, append, tags) {
  const tErr = requireString(title, "title"); if (tErr) return tErr;
  const cErr = requireString(content, "content", { allowEmpty: true }); if (cErr) return cErr;
  if (title.length > TITLE_MAX_CHARS) return err(`'title' is too long (${title.length} chars; max ${TITLE_MAX_CHARS}). Use a shorter path like reference/<topic>.`);
  if (/\.md(\/|$)/i.test(title) || /\.md$/i.test(title.trim())) {
    // Soft signal — we strip it, but tell the agent so it stops appending .md.
  }
  const cf = parseCreateFolder(createFolder); if (!cf.ok) return cf;
  if (tags !== undefined && !Array.isArray(tags)) return err("'tags' must be an array of strings when provided, e.g. [\"homelab\",\"networking\"].");

  const scrubbed = scrubAgentText(content);
  if (Buffer.byteLength(scrubbed.text, "utf8") > MAX_BYTES) {
    return err(`content too large (${Buffer.byteLength(scrubbed.text, "utf8")} bytes; max ${MAX_BYTES}). Shorten the note or split into multiple notes.`);
  }

  const r = await resolveTarget(title, cf.value);
  const rlow = r.id.toLowerCase();
  if (rlow === PENDING_NOTE.toLowerCase()) return err(`"${PENDING_NOTE}" is a gateway-managed ledger; folders are approved via the system consent popup on createFolder:true (or edit Pending-Folders in Obsidian). Don't write it directly.`);
  if (rlow === INDEX_NOTE.toLowerCase()) return curatedWrite(scrubbed.text, append);
  if (!writableAgentId(r.id)) return writableScopeError(r.id);
  if (await isSymlink(r.file)) return err("refusing to write through a symlinked note path");
  await ensureDir(r.file);
  const { text: cleaned, stripped } = toEnglish(stripEmoji(scrubbed.text));
  const text = normalizeNoteBody(cleaned);
  const existingFile = await resolveNoteFile(r.id);
  const existedBefore = !append && (await exists(existingFile));
  let prev = ""; try { prev = await fs.readFile(existingFile, "utf8"); } catch {}
  if (append) {
    const nextBytes = Buffer.byteLength(prev, "utf8") + Buffer.byteLength(text, "utf8") + 64;
    if (nextBytes > MAX_BYTES) return err(`append would exceed ${MAX_BYTES} bytes (note is ${Buffer.byteLength(prev, "utf8")} bytes, append is ${Buffer.byteLength(text, "utf8")}). Start a new note or shorten the append.`);
  }
  const { front, body: prevBody } = splitFront(prev);
  const noteTags = tags !== undefined ? (append ? sanitizeTags([...parseTags(front), ...tags]) : sanitizeTags(tags)) : parseTags(front);
  const fm = renderFront({ created: frontScalar(front, "created") || today(), updated: today(), tags: noteTags, other: frontOther(front) });
  if (append) { const heading = prevBody.trim() ? "" : `# ${base(r.id)}\n`; const sep = prevBody && !prevBody.endsWith("\n") ? "\n" : ""; await atomicWrite(r.file, fm + prevBody + sep + `${heading}\n## ${stamp()}\n\n${text}\n`, "utf8"); }
  else await atomicWrite(r.file, fm + `# ${base(r.id)}\n\n${text}\n`, "utf8");
  // Migrate off legacy double-extension files if we just wrote the canonical path.
  if (existingFile !== r.file && (await exists(existingFile))) { try { await fs.unlink(existingFile); } catch {} }
  if (r.created) await clearPending(r.created);
  if (r.proposedFolder && r.firstAsk) await addPending(r.proposedFolder, r.id);
  const indexErr = isDurable(r.id) ? await tryRebuildIndex() : null;
  const out = { ok: true, savedAs: r.id, indexed: isDurable(r.id), folders: (await listFolders()).filter((f) => !f.startsWith("log") && !f.startsWith(ARCHIVE_BASE)), nonEnglishRemoved: stripped || undefined };
  if (/\.md$/i.test(String(title).trim()) || /\.md\//i.test(title)) out.omittedMdExtension = "Titles should not include .md — it was stripped. Use savedAs going forward.";
  if (scrubbed.mapMarkersStripped) out.mapMarkersStripped = true;
  if (existedBefore) out.overwrote = r.id;
  if (idFromTitle(title) !== r.id || cleanSegs(title).join("/") !== r.id) {
    out.titleResolved = `Requested title was normalized/routed to "${r.id}". Always use savedAs for later reads/moves.`;
  }
  if (cleanSegs(title).length > MAX_DEPTH) out.pathTruncated = `Title deeper than ${MAX_DEPTH} folders; folded into the note name and saved as "${r.id}".`;
  if (noteTags.length) out.tags = noteTags;
  if (r.useExisting) out.reusedFolder = `Filed into existing "${r.useExisting.matched}" (matched your "${r.useExisting.from}") — reused instead of duplicating.`;
  if (r.created) out.folderCreated = r.created;
  if (r.proposedFolder && r.firstAsk) out.folderApprovalNeeded = { proposed: r.proposedFolder, savedTo: r.id, action: `Folder "${r.proposedFolder}" doesn't exist. Saved to "${r.id}". Retry the same path with createFolder:true (boolean). The human gets a system consent popup — do NOT ask them to type yes in chat.` };
  if (r.proposedFolder && !r.firstAsk) out.folderPending = `Already proposed "${r.proposedFolder}". Retry with createFolder:true to raise the consent popup again if needed. Do not re-ask in chat.`;
  return withIndexResult(out, indexErr);
}

async function rewriteLinksAcrossVault(fromId, toId, uniqueLeaf) {
  const all = await listNotes();
  const leaf = fromId.split("/").pop();
  const rewriteLeaf = !!uniqueLeaf && leaf && leaf !== fromId;
  let updated = 0;
  for (const n of all) {
    if (n.id === fromId || n.id === toId) continue;
    if (await isSymlink(n.full)) continue;
    let text; try { text = await fs.readFile(n.full, "utf8"); } catch { continue; }
    let next = replaceWikiLinks(text, fromId, toId);
    if (rewriteLeaf) next = replaceWikiLinks(next, leaf, toId);
    if (next !== text) { await atomicWrite(n.full, next, "utf8"); updated++; }
  }
  // Curated Index region (above the map) may also link to the moved note.
  const idxFile = fileForId(INDEX_NOTE);
  if (!(await isSymlink(idxFile)) && (await exists(idxFile))) {
    let cur = ""; try { cur = await fs.readFile(idxFile, "utf8"); } catch {}
    const s = cur.indexOf(MAP_START);
    if (s !== -1) {
      let curated = cur.slice(0, s), mapBlock = cur.slice(s);
      let nextCurated = replaceWikiLinks(curated, fromId, toId);
      if (rewriteLeaf) nextCurated = replaceWikiLinks(nextCurated, leaf, toId);
      if (nextCurated !== curated) { await atomicWrite(idxFile, nextCurated + mapBlock, "utf8"); updated++; }
    }
  }
  return updated;
}

async function leafIsUnique(noteId) {
  const leaf = noteId.split("/").pop();
  if (!leaf || leaf === noteId) return false;
  let count = 0;
  for (const n of await listNotes()) {
    if (n.id.split("/").pop() === leaf) {
      count++;
      if (count > 1) return false;
    }
  }
  return count === 1;
}

async function removeEmptyParents(filePath) {
  let dir = path.dirname(filePath);
  const root = path.resolve(VAULT);
  while (dir.startsWith(root + path.sep) && dir !== root) {
    let ents = []; try { ents = await fs.readdir(dir); } catch { break; }
    if (ents.length) break;
    try { await fs.rmdir(dir); } catch { break; }
    dir = path.dirname(dir);
  }
}

const doMove = (fromTitle, toTitle, createFolder) => withLock(() => safeCall(() => doMoveImpl(fromTitle, toTitle, createFolder)));
async function doMoveImpl(fromTitle, toTitle, createFolder) {
  const fErr = requireString(fromTitle, "from"); if (fErr) return fErr;
  const tErr = requireString(toTitle, "to"); if (tErr) return tErr;
  const cf = parseCreateFolder(createFolder); if (!cf.ok) return cf;

  const fromId = idFromTitle(fromTitle);
  if (isReservedId(fromId)) return err("system note; cannot move", { note: fromId, hint: "Index and Pending-Folders are gateway-managed." });
  if (!writableAgentId(fromId)) return writableScopeError(fromId);
  const fromFile = await resolveNoteFile(fromId);
  if (await isSymlink(fromFile)) return err("refusing to read through a symlinked note path");
  let prev; try { prev = await fs.readFile(fromFile, "utf8"); } catch { return err("note not found", { note: fromId, hint: "Use note_search or note_list; titles are sanitized — use a prior savedAs id. Omit .md from titles." }); }

  const r = await resolveTarget(toTitle, cf.value);
  if (isReservedId(r.id)) return err("cannot move onto a system note", { note: r.id });
  if (!writableAgentId(r.id)) return writableScopeError(r.id);
  if (r.id === fromId) return { ok: true, from: fromId, savedAs: r.id, moved: false, note: "source and destination are the same" };
  if (await exists(await resolveNoteFile(r.id))) return err(`destination already exists: "${r.id}"`, { hint: "Choose a different 'to' title, or note_archive the destination first." });
  if (await isSymlink(r.file)) return err("refusing to write through a symlinked note path");

  // Destination still needs approval — refuse to move onto a collapsed pending path.
  if (r.proposedFolder) {
    if (r.firstAsk) await addPending(r.proposedFolder, fromId);
    return err("destination folder needs approval before move", {
      folderApprovalNeeded: {
        proposed: r.proposedFolder,
        action: `Retry note_move with createFolder:true (boolean). The human gets a system consent popup — do NOT ask them to type yes in chat.`,
      },
      folderPending: r.firstAsk ? undefined : `Already proposed "${r.proposedFolder}". Retry with createFolder:true for the consent popup.`,
    });
  }

  const { front, body } = splitFront(prev);
  const fm = renderFront({ created: frontScalar(front, "created") || today(), updated: today(), tags: parseTags(front), other: frontOther(front) });
  const uniqueLeaf = await leafIsUnique(fromId);
  await ensureDir(r.file);
  await atomicWrite(r.file, fm + body, "utf8");
  await fs.unlink(fromFile);
  await removeEmptyParents(fromFile);
  if (r.created) await clearPending(r.created);
  const linksUpdated = await rewriteLinksAcrossVault(fromId, r.id, uniqueLeaf);
  const indexErr = await tryRebuildIndex();
  const out = { ok: true, from: fromId, savedAs: r.id, moved: true, linksUpdated, indexed: isDurable(r.id), folders: (await listFolders()).filter((f) => !f.startsWith("log") && !f.startsWith(ARCHIVE_BASE)) };
  if (r.useExisting) out.reusedFolder = `Filed into existing "${r.useExisting.matched}" (matched your "${r.useExisting.from}") — reused instead of duplicating.`;
  if (r.created) out.folderCreated = r.created;
  return withIndexResult(out, indexErr);
}

const doArchive = (title) => withLock(() => safeCall(() => doArchiveImpl(title)));
async function doArchiveImpl(title) {
  const tErr = requireString(title, "title"); if (tErr) return tErr;
  const fromId = idFromTitle(title);
  if (isReservedId(fromId)) return err("system note; cannot archive", { note: fromId });
  if (!writableAgentId(fromId)) return writableScopeError(fromId);
  if (String(fromId).toLowerCase().startsWith(ARCHIVE_BASE + "/")) return err("note is already archived", { note: fromId });
  const fromFile = await resolveNoteFile(fromId);
  if (await isSymlink(fromFile)) return err("refusing to read through a symlinked note path");
  let prev; try { prev = await fs.readFile(fromFile, "utf8"); } catch { return err("note not found", { note: fromId, hint: "Use note_search or note_list; titles are sanitized — use a prior savedAs id. Omit .md from titles." }); }

  // Flatten under archive/ so deep originals stay within MAX_DEPTH (archive/<flat-id>).
  const flat = fromId.replace(/\//g, "-").slice(0, 100) || "Untitled";
  let destId = `${ARCHIVE_BASE}/${flat}`, n = 2;
  while (await exists(await resolveNoteFile(destId))) {
    if (n > 9999) return err("archive name collision limit exceeded", { note: fromId });
    const suffix = `-${n}`;
    destId = `${ARCHIVE_BASE}/${flat.slice(0, 100 - suffix.length)}${suffix}`;
    n++;
  }
  const destFile = fileForId(destId);
  if (await isSymlink(destFile)) return err("refusing to write through a symlinked note path");

  const { front, body } = splitFront(prev);
  const tags = sanitizeTags([...parseTags(front), "archived"]);
  const fm = renderFront({ created: frontScalar(front, "created") || today(), updated: today(), tags, other: frontOther(front) });
  const uniqueLeaf = await leafIsUnique(fromId);
  await ensureDir(destFile);
  await atomicWrite(destFile, fm + body, "utf8");
  await fs.unlink(fromFile);
  await removeEmptyParents(fromFile);
  const linksUpdated = await rewriteLinksAcrossVault(fromId, destId, uniqueLeaf);
  const indexErr = await tryRebuildIndex();
  return withIndexResult({ ok: true, from: fromId, savedAs: destId, archived: true, linksUpdated, tags, indexed: false }, indexErr);
}

async function findBacklinks(title) {
  const tErr = requireString(title, "title"); if (tErr) return tErr;
  const targetId = idFromTitle(title);
  const leaf = targetId.split("/").pop();
  const all = await listNotes();
  const baseSet = new Map();
  for (const n of all) { const b = n.id.split("/").pop(); if (!baseSet.has(b)) baseSet.set(b, []); baseSet.get(b).push(n.id); }
  const leafResolvesHere = leaf && (baseSet.get(leaf) || []).includes(targetId);
  const backlinks = [];
  for (const n of all) {
    if (isReservedId(n.id)) continue;
    if (n.id === targetId) continue;
    let text = ""; try { text = await fs.readFile(n.full, "utf8"); } catch { continue; }
    const body = splitFront(text).body;
    const hits = [];
    for (const m of body.matchAll(/\[\[([^\]]+)\]\]/g)) {
      const raw = m[1].split("|")[0].split("#")[0].trim();
      if (!raw) continue;
      const matchFull = raw === targetId;
      const matchLeaf = !raw.includes("/") && raw === leaf && leafResolvesHere;
      if (matchFull || matchLeaf) hits.push(m[0]);
    }
    if (hits.length) backlinks.push({ note: n.id, count: hits.length, examples: hits.slice(0, 3) });
  }
  backlinks.sort((a, b) => b.count - a.count || (a.note < b.note ? -1 : 1));
  return { ok: true, note: targetId, count: backlinks.length, backlinks };
}

async function readNote({ title, maxChars, heading }) {
  const tErr = requireString(title, "title"); if (tErr) return { found: false, ...tErr, content: "" };
  const id = idFromTitle(title);
  let f;
  try { f = await resolveNoteFile(id); }
  catch (e) { return { found: false, title: id, content: "", error: e.message, hint: "Use a relative note title with '/' folders only — no absolute paths. Omit .md." }; }
  if (await isSymlink(f)) return { found: false, title: id, content: "", error: "refusing to read through a symlinked note path" };
  let full; try { full = await fs.readFile(f, "utf8"); } catch {
    return { found: false, title: id, content: "", error: "note not found", hint: "Title was sanitized to this id. Use note_search / note_list, or the savedAs from a prior write. Omit .md from titles." };
  }
  const { front, body } = splitFront(full);
  let content = full;
  let section;
  if (heading != null && heading !== "") {
    const hErr = requireString(heading, "heading"); if (hErr) return { found: true, title: id, content: "", ...hErr };
    const extracted = extractHeadingSection(body, heading);
    if (extracted == null) return { found: true, title: id, content: "", sectionMissing: true, heading: String(heading), error: `heading not found: "${String(heading).replace(/^#+\s*/, "").trim()}"`, hint: "Pass the heading text without '#'. Use note_read without heading to see available sections." };
    content = (front ? `---\n${front}---\n` : "") + extracted;
    section = String(heading).replace(/^#+\s*/, "").trim();
  }
  const limitRaw = maxChars == null || maxChars === "" ? null : Number(maxChars);
  if (maxChars != null && maxChars !== "" && !Number.isFinite(limitRaw)) {
    return { found: true, title: id, content: "", error: "'maxChars' must be a positive number." };
  }
  const limit = Number.isFinite(limitRaw) && limitRaw > 0 ? Math.min(Math.floor(limitRaw), READ_CHARS_MAX) : null;
  const out = { found: true, title: id, content, bytes: Buffer.byteLength(content, "utf8") };
  if (section) out.section = section;
  if (limit != null && content.length > limit) {
    out.content = content.slice(0, limit);
    out.truncated = true;
    out.totalChars = content.length;
    out.hint = "Content truncated. Re-read with a larger maxChars, or pass heading to fetch one section.";
  }
  return out;
}

function approvalRequest(title, description) {
  return {
    requireApproval: {
      title,
      description,
      severity: "warning",
      allowedDecisions: ["allow-once", "deny"],
      timeoutMs: 120_000,
    },
  };
}

async function notesApprovalHook(event) {
  const gated = new Set(["note_write", "note_append", "note_move"]);
  if (gated.has(event.toolName) && event.params?.createFolder === true) {
    const rawTitle = event.toolName === "note_move" ? event.params?.to : event.params?.title;
    if (typeof rawTitle === "string" && rawTitle.trim()) {
      const id = idFromTitle(rawTitle);
      const segs = id.split("/");
      const folderSegs = segs.slice(0, -1);
      // Writing directly under a known base does not create a topic folder.
      if (folderSegs.length > 0 && !(folderSegs.length === 1 && KNOWN_BASES.has(folderSegs[0]))) {
        const folderId = folderSegs.join("/");
        const folderPath = path.resolve(VAULT, folderId);
        await assertSafeVaultPath(folderPath);
        if (!(await exists(folderPath))) {
          const pending = await pendingSet();
          if (pending.has(folderId)) {
            return approvalRequest(
              "Create vault folder",
              `Create "${folderId}/" in the Obsidian vault and file the note as "${id}"? Deny keeps the note collapsed without creating the folder.`,
            );
          }
        }
      }
    }
  }

  if (DESTRUCTIVE_APPROVALS && event.toolName === "note_write") {
    const rawTitle = event.params?.title;
    if (typeof rawTitle === "string" && rawTitle.trim()) {
      const target = await resolveTarget(rawTitle, false);
      return approvalRequest(
        "Write complete vault note",
        `Create or replace the complete note "${target.id}"? Use note_append for an additive update. Deny leaves durable memory untouched.`,
      );
    }
  }
  if (DESTRUCTIVE_APPROVALS && event.toolName === "note_move") {
    return approvalRequest(
      "Move vault note",
      `Move "${String(event.params?.from ?? "")}" to "${String(event.params?.to ?? "")}" and rewrite matching wikilinks?`,
    );
  }
  if (DESTRUCTIVE_APPROVALS && event.toolName === "note_archive") {
    return approvalRequest(
      "Archive vault note",
      `Move "${String(event.params?.title ?? "")}" into archive/ and rewrite matching wikilinks?`,
    );
  }
}

const tool = (def) => def;

export default definePluginEntry({
  id: "agent-notes",
  name: "Agent Notes",
  description: "Persistent markdown notes in a host Obsidian vault. Search (note_search) and survey folders (note_folders) before filing; reuse a folder only on a clear fit — reference/work is employment, not agent scratch. New folders: propose once, then createFolder:true raises a system consent popup. Durable notes auto-listed in Index. Tags, move/archive, backlinks supported. English plain text only.",
  register(api) {
    const tools = [
    tool({ name: "note_write", description: "Create or overwrite a note. BEFORE calling: note_search + note_folders. New folder: write desired path once (proposal), then retry with createFolder:true — human gets a system consent popup (do not ask them to type yes). Omit .md. English only.",
      parameters: { type: "object", properties: { title: str("Note title; '/' makes folders, e.g. reference/homelab/router. Omit .md."), content: str("Full markdown body, English only, no emoji."), createFolder: { type: "boolean", description: "Set true to create a previously proposed folder. Triggers a system consent popup. JSON boolean only. Honored only if proposed first." }, tags: { type: "array", items: { type: "string" }, description: "Obsidian tags (no '#'; spaces become '-'). Replaces the note's tags; omit to keep existing." } }, required: ["title", "content"] },
      execute: ({ title, content, createFolder, tags }) => doWrite(title, content, createFolder, false, tags) }),
    tool({ name: "note_append", description: "Append a timestamped entry. Prefer note_search hit first. New folders: propose then createFolder:true (system popup). Do not use reference/work for non-employment topics.",
      parameters: { type: "object", properties: { title: str("Note title; '/' makes folders. Omit .md."), content: str("Markdown to append, English only, no emoji."), createFolder: { type: "boolean", description: "Set true to create a previously proposed folder (system consent popup). JSON boolean only." }, tags: { type: "array", items: { type: "string" }, description: "Obsidian tags to add (no '#'); merged with existing." } }, required: ["title", "content"] },
      execute: ({ title, content, createFolder, tags }) => doWrite(title, content, createFolder, true, tags) }),
    tool({ name: "note_read", description: "Read a note by title. Optional 'heading' returns only that markdown section (plus frontmatter). Optional 'maxChars' truncates long notes (sets truncated:true). On miss returns {found:false, error, hint}.",
      parameters: { type: "object", properties: { title: str("Note title to read (include its folder, e.g. 'reference/homelab/router')."), heading: str("Optional heading text to extract (without '#'), e.g. 'Next steps'."), maxChars: { type: "number", description: "Optional max characters to return; when truncated, truncated:true and totalChars are set." } }, required: ["title"] },
      execute: (args) => safeCall(() => readNote(args)) }),
    tool({ name: "note_tag", description: "Add and/or remove Obsidian tags on an existing note's frontmatter, without touching its body. Tags power Obsidian's tag pane and note_search's 'tag' filter. Use the note's savedAs id.",
      parameters: { type: "object", properties: { title: str("Note title/id to tag (include its folder)."), add: { type: "array", items: { type: "string" }, description: "Tags to add (no '#'; spaces become '-')." }, remove: { type: "array", items: { type: "string" }, description: "Tags to remove." } }, required: ["title"] },
      execute: ({ title, add, remove }) => withLock(() => safeCall(async () => {
        const tErr = requireString(title, "title"); if (tErr) return tErr;
        if (add !== undefined && !Array.isArray(add)) return err("'add' must be an array of strings when provided.");
        if (remove !== undefined && !Array.isArray(remove)) return err("'remove' must be an array of strings when provided.");
        const id = idFromTitle(title);
        if (RESERVED.has(id.toLowerCase())) return err("system note; not taggable", { note: id });
        if (!writableAgentId(id)) return writableScopeError(id);
        const f = await resolveNoteFile(id);
        if (await isSymlink(f)) return err("refusing to write through a symlinked note path");
        let prev; try { prev = await fs.readFile(f, "utf8"); } catch { return err("note not found", { note: id, hint: "Use a prior savedAs id from note_write/note_list. Omit .md from titles." }); }
        const { front, body } = splitFront(prev);
        const rem = new Set(sanitizeTags(remove || []).map((t) => t.toLowerCase()));
        const next = sanitizeTags([...parseTags(front), ...(add || [])]).filter((t) => !rem.has(t.toLowerCase()));
        const canonical = fileForId(id);
        await atomicWrite(canonical, renderFront({ created: frontScalar(front, "created") || today(), updated: today(), tags: next, other: frontOther(front) }) + body, "utf8");
        if (f !== canonical && (await exists(f))) { try { await fs.unlink(f); } catch {} }
        const indexErr = isDurable(id) ? await tryRebuildIndex() : null;
        return withIndexResult({ ok: true, note: id, tags: next }, indexErr);
      })) }),
    tool({ name: "note_search", description: "Full-text and/or tag search across notes; returns the best matches first (ranked). Optional filters: folder, updatedAfter (YYYY-MM-DD), exclude (folder prefixes), limit (default 25, max 100). Provide query and/or tag.",
      parameters: { type: "object", properties: {
        query: str("Words to search for (optional if 'tag' given)."),
        tag: str("Obsidian tag to filter by, e.g. 'homelab' (optional)."),
        folder: str("Only notes under this folder prefix, e.g. 'reference/homelab'."),
        updatedAfter: str("Only notes with frontmatter updated on/after this YYYY-MM-DD date."),
        exclude: { type: "array", items: { type: "string" }, description: "Folder prefixes to skip, e.g. ['log','archive']." },
        limit: { type: "number", description: `Max matches to return (default ${SEARCH_LIMIT_DEFAULT}, max ${SEARCH_LIMIT_MAX}).` },
      }, required: [] },
      execute: (args) => safeCall(async () => {
        const { query, tag, folder, updatedAfter, exclude, limit } = args ?? {};
        if (query != null && typeof query !== "string") return err("'query' must be a string when provided.");
        if (tag != null && typeof tag !== "string") return err("'tag' must be a string when provided.");
        if (folder != null && typeof folder !== "string") return err("'folder' must be a string when provided.");
        if (exclude !== undefined && !Array.isArray(exclude)) return err("'exclude' must be an array of folder prefix strings when provided.");
        const rawq = String(query ?? "").trim(), want = sanitizeTag(tag ?? "");
        if (!rawq && !want) return err("provide 'query' text or a 'tag'", { query: query ?? "", count: 0, matches: [] });
        const after = updatedAfter != null && String(updatedAfter).trim() !== "" ? parseDateYmd(updatedAfter) : null;
        if (updatedAfter != null && String(updatedAfter).trim() !== "" && !after) return err("updatedAfter must be YYYY-MM-DD", { hint: "Example: 2026-07-01" });
        const folderPre = folder ? folderPrefix(folder) : "";
        const excludes = Array.isArray(exclude) ? exclude.map((x) => folderPrefix(x)).filter(Boolean) : [];
        const limRaw = limit == null || limit === "" ? SEARCH_LIMIT_DEFAULT : Number(limit);
        if (limit != null && limit !== "" && !Number.isFinite(limRaw)) return err("'limit' must be a positive number.");
        const LIMIT = Number.isFinite(limRaw) && limRaw > 0 ? Math.min(Math.floor(limRaw), SEARCH_LIMIT_MAX) : SEARCH_LIMIT_DEFAULT;
        const wl = want ? want.toLowerCase() : "", phrase = rawq.toLowerCase();
        const terms = [...new Set(phrase.split(/\s+/).filter(Boolean))], scored = [];
        for (const n of await listNotes()) {
          if (RESERVED.has(n.id.toLowerCase())) continue;                 // skip the Index/Pending meta-notes
          if (folderPre) {
            const low = n.id.toLowerCase(), fp = folderPre.toLowerCase();
            if (!(low === fp || low.startsWith(fp + "/"))) continue;
          }
          if (shouldExclude(n.id, excludes)) continue;
          let full = ""; try { full = await fs.readFile(n.full, "utf8"); } catch { continue; }
          const { front, body } = splitFront(full);
          const tags = parseTags(front);
          if (want && !tags.some((x) => { const xl = x.toLowerCase(); return xl === wl || xl.startsWith(wl + "/"); })) continue;
          const upd = frontScalar(front, "updated") || "";
          if (after && (!upd || upd < after)) continue;
          if (!terms.length) { scored.push({ note: n.id, tags: tags.length ? tags : undefined, updated: upd || undefined, _s: 0, _u: upd }); continue; }
          const hay = body.toLowerCase(), idl = n.id.toLowerCase(), tagl = tags.map((t) => t.toLowerCase());
          let matched = 0, occ = 0, titleHits = 0, tagHits = 0, firstIdx = -1;
          for (const term of terms) {
            let idx = hay.indexOf(term); if (idx !== -1) matched++;
            while (idx !== -1) { occ++; if (firstIdx === -1 || idx < firstIdx) firstIdx = idx; idx = hay.indexOf(term, idx + term.length); }
            if (idl.includes(term)) titleHits++;
            if (tagl.some((t) => t.includes(term))) tagHits++;
          }
          if (!matched && !titleHits && !tagHits) continue;
          const phraseHit = terms.length > 1 && hay.includes(phrase) ? 1 : 0;
          const _s = phraseHit * 1000 + titleHits * 50 + matched * 20 + tagHits * 15 + Math.min(occ, 20);
          const snippet = firstIdx >= 0 ? "…" + body.slice(Math.max(0, firstIdx - 60), firstIdx + 90).replace(/\s+/g, " ").trim() + "…" : undefined;
          scored.push({ note: n.id, snippet, tags: tags.length ? tags : undefined, updated: upd || undefined, _s, _u: upd });
        }
        scored.sort((a, b) => b._s - a._s || (b._u < a._u ? -1 : b._u > a._u ? 1 : (a.note < b.note ? -1 : 1)));
        const truncated = scored.length > LIMIT;
        const matches = scored.slice(0, LIMIT).map(({ _s, _u, ...m }) => m);
        const out = { ok: true, query: query ?? "", tag: want || undefined, folder: folderPre || undefined, updatedAfter: after || undefined, exclude: excludes.length ? excludes : undefined, count: matches.length, matches };
        if (truncated) out.truncated = true;
        return out;
      }) }),
    tool({ name: "note_list", description: "List every note (all folders) with size, last-modified time, and tags.",
      parameters: { type: "object", properties: {} },
      execute: () => safeCall(async () => { const notes = []; for (const n of await listNotes()) { try { const st = await fs.stat(n.full); let tags = []; try { tags = parseTags(splitFront(await fs.readFile(n.full, "utf8")).front); } catch {} notes.push({ note: n.id, bytes: st.size, modified: st.mtime.toISOString(), tags: tags.length ? tags : undefined }); } catch {} } notes.sort((a, b) => (a.modified < b.modified ? 1 : -1)); return { ok: true, count: notes.length, notes }; }) }),
    tool({ name: "note_folders", description: "List topic folders with how many notes each holds (and up to 5 example note ids). Call this BEFORE filing a new durable note to find a real fit — do not shoehorn into a vaguely related folder (e.g. reference/work is employment, not agent tasks). Optional 'prefix' limits to a subtree (e.g. 'reference').",
      parameters: { type: "object", properties: { prefix: str("Optional folder prefix filter, e.g. 'reference' or 'reference/homelab'.") } },
      execute: ({ prefix }) => safeCall(async () => {
        const pre = prefix ? folderPrefix(prefix) : "";
        const folders = (await listFolders()).filter((f) => {
          if (f.startsWith("log") || f.startsWith(ARCHIVE_BASE)) return false;
          if (!pre) return true;
          const fl = f.toLowerCase(), pl = pre.toLowerCase();
          return fl === pl || fl.startsWith(pl + "/");
        });
        const notes = await listNotes();
        const byFolder = new Map();
        for (const n of notes) {
          if (isReservedId(n.id) || !isDurable(n.id)) continue;
          const i = n.id.lastIndexOf("/");
          const folder = i === -1 ? "(top level)" : n.id.slice(0, i);
          if (!byFolder.has(folder)) byFolder.set(folder, []);
          byFolder.get(folder).push(n.id);
        }
        const rows = folders.map((f) => {
          const ids = byFolder.get(f) || [];
          return { folder: f, noteCount: ids.length, examples: ids.slice(0, 5) };
        }).sort((a, b) => a.folder < b.folder ? -1 : 1);
        // Include top-level durable notes bucket for visibility
        if (!pre || pre.toLowerCase() === "reference" || pre === "") {
          const top = byFolder.get("(top level)") || [];
          if (top.length && !rows.some((r) => r.folder === "(top level)")) {
            rows.unshift({ folder: "(top level)", noteCount: top.length, examples: top.slice(0, 5) });
          }
        }
        return { ok: true, prefix: pre || undefined, count: rows.length, folders: rows,
          hint: "Reuse a folder only on a clear subject match. If none fit, write the desired path (proposal), then retry with createFolder:true for a system consent popup. reference/work = employment, not agent scratch." };
      }) }),
    tool({ name: "note_move", description: "Rename/move a note; rewrites [[wikilinks]]. New destination folders: propose then createFolder:true (system consent popup).",
      parameters: { type: "object", properties: { from: str("Current note title/id."), to: str("New note title/id (may include folders)."), createFolder: { type: "boolean", description: "Set true to create a previously proposed destination folder (system consent popup). JSON boolean only." } }, required: ["from", "to"] },
      execute: ({ from, to, createFolder }) => doMove(from, to, createFolder) }),
    tool({ name: "note_archive", description: "Soft-delete: move a note under archive/ (flattened name), tag it 'archived', rewrite [[wikilinks]], and drop it from the Index map. Prefer this over inventing a delete. Cannot archive system notes.",
      parameters: { type: "object", properties: { title: str("Note title/id to archive.") }, required: ["title"] },
      execute: ({ title }) => doArchive(title) }),
    tool({ name: "note_backlinks", description: "List notes that [[link]] to the given note (full path or unique basename). Returns each linking note with hit count and example link texts.",
      parameters: { type: "object", properties: { title: str("Note title/id to find backlinks for.") }, required: ["title"] },
      execute: ({ title }) => safeCall(() => findBacklinks(title)) }),
    ];

    for (const t of tools) {
      api.registerTool({
        name: t.name,
        description: t.description,
        parameters: t.parameters,
        // OpenClaw otherwise prepares every before_tool_call hook in a model
        // batch before executing any tool. Sequential execution keeps approval
        // checks and mutation/read ordering inside one serialized timeline.
        executionMode: "sequential",
        async execute(_id, params) {
          // OpenClaw's low-level registerTool contract expects an AgentToolResult.
          // The upstream v1.4 source returns domain objects here; wrapping them
          // keeps both the model-visible JSON and structured details intact.
          return jsonResult(await t.execute(params ?? {}));
        },
      });
    }

    api.on("before_tool_call", notesApprovalHook);
  },
});
