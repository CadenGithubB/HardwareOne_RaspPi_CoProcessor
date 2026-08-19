"""The CM5 as a remote source in the firmware's LLM model registry.

The ESP32 owns a model registry in which its on-device engine and this host are
two symmetric *sources*.  Selecting ``cm5:<model>`` on any surface (web, OLED,
G2, CLI, BLE app) routes the whole conversation here.  The firmware is the UART
**server** and cannot call us and block, so it pushes an EVT frame and waits for
us to come back with authenticated commands — the same shape as the shipped
power/fan control planes.

Firmware -> CM5 EVT payloads::

    llm_select <escaped-model-name>
    llm_ask    <session> <maxTokens> <tempX100> <toppX100> <escaped-prompt>
    llm_cancel <session>

CM5 -> firmware authenticated commands::

    cm5 llm models <gen> <idx> <count> <sizeMB> <name>
    cm5 llm ready  <gen> <name>
    cm5 llm push   <session> <seq> <escaped-text>
    cm5 llm end    <session> <ok|error|stopped> [tokens] [tokPerSecX10]

``<gen>`` is a catalog generation counter this module owns; the firmware resets
its table whenever it changes, so a shrinking catalog cannot leave stale rows.
``<session>`` is minted by the firmware per generation and echoed back on every
push and end.  The firmware consumes all four with an intrinsic that runs
*before* cmd_exec, so a push never takes the command lock and never enters the
durable command audit.

History deliberately stays in :class:`~.llm.client.LlmClient`: the firmware
sends no history, because this host applies its own chat template and system
prompt (see that module's docstring).  The prompt arrives raw and unframed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from .link.session import CommandTimeout, LinkClosed

log = logging.getLogger("cm5_llm")

# Firmware caps (System_LLMCm5.h / System_LLMBackend.h; change both together).
MAX_MODELS = 8                  # CM5_LLM_MAX_MODELS
MAX_NAME_CHARS = 31             # LLM_MODEL_NAME_LEN - 1, after tokCopy()
STALL_MS = 45_000               # CM5_LLM_STALL_MS

# CM5_LLM_MAX_PROMPT is 700, but the firmware bounds the ESCAPED form against
# twice that before sending. Escaping only grows a string, so the longest raw
# prompt it can actually emit is 1400 chars — one with no whitespace at all.
# Guarding at 700 here would silently clip prompts the device legitimately
# sent; this bound only exists to catch the two sides drifting apart.
MAX_PROMPT_CHARS = 1400

# The firmware decodes one push into `char decoded[256]` and silently truncates
# past that, so a chunk's DECODED UTF-8 form must fit with room for the NUL.
# 200 is the shipped EvenAI part size and sits comfortably under the real cap.
_PUSH_MAX_BYTES = 200

# Flush policy, mirroring pipeline.py's validated EvenAI cadence. Per-token
# pushes are not viable: the firmware admits at most one line per loop lap and
# every command here serializes behind one asyncio.Lock.
_STREAM_OPEN_MIN = 30
_STREAM_FLUSH_CHARS = 140
_SENTENCE_ENDS = (". ", "! ", "? ")

# The firmware abandons a generation STALL_MS after the last APPLIED push (an
# ask counts as one). A cold model can spend longer than that in prefill before
# the first token exists, so a long silence is filled with empty pushes — the
# firmware documents an empty tail as a legal no-op, and it still stamps the
# stall clock and consumes a seq. Half the budget leaves room for one retry.
_KEEPALIVE_S = STALL_MS / 1000.0 / 2

_EVENT_QUEUE_SIZE = 16
_CANCEL_MEMORY = 32

# Rejections that mean "the firmware already moved on", not "the host is
# broken": it abandoned the turn on its stall timer, the wearer dismissed the
# card, or a new login epoch fenced the old generation. These are logged at
# INFO because they are a normal race, not a fault.
_BENIGN_REJECTIONS = (
    "stale session",
    "session epoch mismatch",
    "no generation in flight",
)


class Cm5LlmProtocolError(ValueError):
    """A recognized ``llm_`` event is malformed."""


# ---------------------------------------------------------------------------
# Escaping
# ---------------------------------------------------------------------------
# Mirror of cm5LlmEscape/cm5LlmUnescape in the firmware's System_LLMCm5.cpp.
# The firmware trims every inbound line before dispatch, and streamed deltas
# carry their inter-word space at exactly the chunk boundary — so rather than
# protect only the edges, ALL whitespace is escaped and no chunk can be damaged
# by trimming or re-tokenizing on either side.

_ESCAPE_OUT = {
    "\\": "\\\\",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    " ": "\\s",
}
_ESCAPE_IN = {"n": "\n", "r": "\r", "t": "\t", "s": " ", "\\": "\\"}


def escape(text: str) -> str:
    """Encode one payload for the wire."""
    return "".join(_ESCAPE_OUT.get(ch, ch) for ch in text)


def unescape(text: str) -> str:
    """Decode one wire payload.

    An unknown escape yields the character itself rather than dropping it, so a
    newer peer adding an escape degrades to readable text instead of a hole. A
    trailing lone backslash is literal, exactly as the firmware's decoder
    treats ``i + 1 >= inLen``.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        out.append(_ESCAPE_IN.get(text[i + 1], text[i + 1]))
        i += 2
    return "".join(out)


# VT and FF are the one gap in the shared escape table: both sides pass them
# through raw, and a raw one landing at a chunk edge would be eaten by the
# firmware's line trim. They never occur in chat-model prose; normalizing them
# to the closest escapable character keeps that theoretical case lossless on
# the wire instead of silently dropping a byte.
_WIRE_SAFE = str.maketrans({"\v": "\n", "\f": "\n"})


# ---------------------------------------------------------------------------
# Inbound events
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LlmSelect:
    name: str


@dataclass(frozen=True)
class LlmAsk:
    session: int
    max_tokens: int
    temperature: float
    top_p: float
    prompt: str


@dataclass(frozen=True)
class LlmCancel:
    session: int


def _positive_int(token: str, what: str) -> int:
    if not token.isascii() or not token.isdecimal():
        raise Cm5LlmProtocolError(f"{what} must be a decimal integer")
    value = int(token, 10)
    if not 0 < value <= 0xFFFFFFFF:
        raise Cm5LlmProtocolError(f"{what} is out of range")
    return value


def _bounded_int(token: str, what: str, low: int, high: int) -> int:
    if not token.isascii() or not token.isdecimal():
        raise Cm5LlmProtocolError(f"{what} must be a decimal integer")
    value = int(token, 10)
    # The firmware clamps generation params centrally before it emits the ask,
    # so an out-of-range value here means the two sides disagree. Clamp rather
    # than reject: a working answer with a nudged temperature beats no answer.
    if value < low or value > high:
        log.warning("llm_ask %s=%d outside %d..%d — clamping", what, value, low, high)
        return max(low, min(high, value))
    return value


def parse_llm_event(payload: bytes):
    """Strictly parse one ``llm_`` EVT, or return ``None`` for another subsystem.

    Decoding is UTF-8 with ``errors="replace"``, NOT the strict ASCII the
    generic event path uses: a prompt typed on a phone keyboard routinely
    carries a curly apostrophe, and one UnicodeDecodeError would lose the whole
    ask with no error and no timeout on either side.
    """
    if not payload.startswith((b"llm_select", b"llm_ask", b"llm_cancel")):
        return None
    text = payload.decode("utf-8", errors="replace").strip()
    # `split(None, n)` collapses runs and strips the head; every payload tail is
    # fully escaped, so the remainder is taken raw rather than re-tokenized.
    tokens = text.split(None, 5)
    verb = tokens[0] if tokens else ""

    if verb == "llm_select":
        if len(tokens) < 2:
            raise Cm5LlmProtocolError("usage: llm_select <name>")
        name = unescape(tokens[1])
        if not name:
            raise Cm5LlmProtocolError("llm_select carried an empty name")
        return LlmSelect(name)

    if verb == "llm_cancel":
        if len(tokens) < 2:
            raise Cm5LlmProtocolError("usage: llm_cancel <session>")
        return LlmCancel(_positive_int(tokens[1], "session"))

    if verb == "llm_ask":
        if len(tokens) < 6:
            raise Cm5LlmProtocolError(
                "usage: llm_ask <session> <maxTokens> <tempX100> <toppX100> <prompt>")
        session = _positive_int(tokens[1], "session")
        max_tokens = _bounded_int(tokens[2], "maxTokens", 1, 4096)
        temp_x100 = _bounded_int(tokens[3], "tempX100", 0, 200)
        topp_x100 = _bounded_int(tokens[4], "toppX100", 1, 100)
        prompt = unescape(tokens[5])
        if not prompt:
            raise Cm5LlmProtocolError("llm_ask carried an empty prompt")
        if len(prompt) > MAX_PROMPT_CHARS:
            # The firmware bounds the escaped form, so this only trips if the
            # two caps drift apart. Truncating beats refusing the turn.
            log.warning("llm_ask prompt is %d chars — truncating to %d",
                        len(prompt), MAX_PROMPT_CHARS)
            prompt = prompt[:MAX_PROMPT_CHARS]
        return LlmAsk(session, max_tokens, temp_x100 / 100.0,
                      topp_x100 / 100.0, prompt)

    raise Cm5LlmProtocolError(f"unknown llm event verb {verb!r}")


# ---------------------------------------------------------------------------
# Highest percentage the loader may display. The last ~4% of wall time is
# KV/compute-buffer allocation plus warmup decode, which no counter on this
# system observes: MEASURED cold on the CM5 2026-08-18, residency reached 100%
# of the GGUF at 45.1s of a 47.1s load. Parking at 96 while that finishes is
# truthful; showing 100 and then appearing to hang is not.
_LOAD_PCT_CAP = 96


# Model catalog
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cm5Model:
    name: str
    path: str
    size_mb: int


def sanitize_model_name(stem: str) -> str:
    """Reduce a filename to a name the firmware can store and echo back.

    The firmware stores catalog names with ``tokCopy`` — no unescape, stops at
    whitespace — and escapes the stored name again when it emits ``llm_select``.
    Restricting names to a single escape-free token makes that round trip an
    identity instead of a double-escaped mess, and it reads better on a 19-column
    OLED row.
    """
    out = []
    for ch in stem:
        out.append(ch if (ch.isascii() and (ch.isalnum() or ch in "._+-")) else "_")
    name = "".join(out).strip("_") or "model"
    return name[:MAX_NAME_CHARS]


def discover_models(model_dir: str, *, active_path: str = "") -> list[Cm5Model]:
    """Enumerate servable GGUFs, newest-config-first, deduped and capped.

    ``active_path`` is always included even when it sits outside ``model_dir``,
    so the model this daemon is actually serving can never be missing from the
    picker.
    """
    paths: list[Path] = []
    if active_path:
        candidate = Path(os.path.expanduser(active_path))
        if candidate.is_file():
            paths.append(candidate)
    if model_dir:
        directory = Path(os.path.expanduser(model_dir))
        try:
            paths.extend(sorted(p for p in directory.glob("*.gguf") if p.is_file()))
        except OSError as exc:
            log.error("cannot enumerate %s: %s", directory, exc)

    models: list[Cm5Model] = []
    seen_paths: set[str] = set()
    used_names: set[str] = set()
    dropped = 0
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        try:
            size_mb = max(1, path.stat().st_size // (1024 * 1024))
        except OSError as exc:
            log.warning("skipping %s: %s", path, exc)
            continue
        name = sanitize_model_name(path.stem)
        if name.casefold() in used_names:
            # Truncation to the firmware's 31 chars can collide. A numbered
            # suffix keeps both rows selectable instead of shadowing one.
            base = name[:MAX_NAME_CHARS - 2]
            for n in range(2, 10):
                name = f"{base}_{n}"
                if name.casefold() not in used_names:
                    break
            else:
                log.warning("skipping %s: name collides after truncation", path)
                continue
        if len(models) >= MAX_MODELS:
            dropped += 1
            continue
        used_names.add(name.casefold())
        models.append(Cm5Model(name, str(path), size_mb))
    if dropped:
        log.warning("%d model(s) beyond the firmware's cap of %d were not "
                    "published", dropped, MAX_MODELS)
    return models


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


def _flush_point(acc: str, sent: int, *, opened: bool) -> int | None:
    """Absolute index to cut the next pushed chunk at, or None to accumulate.

    Cuts land BEFORE the boundary space so the glue travels at the front of the
    next chunk and the firmware's concatenation is byte-exact.  Verbatim policy
    from pipeline.py's EvenAI stream: wait for a sentence end past
    ``_STREAM_OPEN_MIN`` pending chars to open, flush at every sentence end
    once open, and force a cut at the last word boundary after a run of
    ``_STREAM_FLUSH_CHARS`` with no sentence end.
    """
    pending = acc[sent:]
    best = -1
    for mark in _SENTENCE_ENDS:
        idx = pending.rfind(mark)
        if idx >= 0:
            best = max(best, idx + len(mark) - 1)   # index of the boundary space
    if best >= 0 and (opened or best >= _STREAM_OPEN_MIN):
        return sent + best
    if len(pending) >= _STREAM_FLUSH_CHARS:
        sp = pending.rfind(" ", 0, _STREAM_FLUSH_CHARS)
        return sent + (sp if sp > 0 else _STREAM_FLUSH_CHARS)
    return None


def split_for_push(text: str, limit: int = _PUSH_MAX_BYTES) -> list[str]:
    """Split on codepoint boundaries so every part's UTF-8 form fits ``limit``.

    Unlike deliver.chunk_text this preserves the text exactly — no whitespace
    collapsing — because the firmware appends the decoded parts verbatim and the
    reassembled answer has to be byte-identical to what the model produced.
    """
    if not text:
        return []
    if len(text.encode("utf-8")) <= limit:
        return [text]
    parts: list[str] = []
    current = ""
    for ch in text:
        candidate = current + ch
        if len(candidate.encode("utf-8")) > limit:
            if not current:
                raise ValueError(f"push limit {limit} is smaller than one character")
            parts.append(current)
            current = ch
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


@dataclass
class _Generation:
    session: int
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    seq: int = 0
    deltas: int = 0
    first_delta_t: float | None = None
    last_delta_t: float | None = None
    last_push_t: float = field(default_factory=time.monotonic)
    # True once a chunk carrying real text has gone out. Keepalives deliberately
    # do not open the stream — they consume a seq without producing a paint.
    opened: bool = False
    aborted: bool = False


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class Cm5LlmService:
    """Nonblocking EVT consumer and single-generation worker for the LLM bridge.

    Construction is cheap and happens with the rest of the control plane, before
    any model loads: the catalog is filesystem-derived, so the picker populates
    while llama-server is still warming.  :meth:`attach` binds the model plane
    once it is up and healthy; until then an ask is answered immediately with a
    terminal ``end … error`` rather than left to time out on the device.
    """

    def __init__(self, session, cfg, *, cm5_presence=None) -> None:
        self._session = session
        self._cfg = cfg
        # A generation makes this host slow for seconds at a time. Holding a
        # named share of the BUSY lease widens it from 15s to 75s, which is
        # what stops a momentarily slow link from starving the 5s heartbeat
        # and making the firmware abandon the answer mid-stream.
        self._cm5_presence = cm5_presence
        self._enabled = cfg.serve_firmware and cfg.engine != "none"
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=_EVENT_QUEUE_SIZE)
        self._client = None
        self._supervisor = None
        self._models: list[Cm5Model] = []
        self._by_name: dict[str, Cm5Model] = {}
        self._catalog_ready = False
        self._published = False
        # Never publish generation 0: a device that rebooted under us is sitting
        # at 0, and an equal generation does not reset its table.
        self._generation = 0
        self._active_name: str | None = None
        self._current: _Generation | None = None
        # Cancels can arrive before their ask is dequeued; remember a bounded
        # number so a pre-cancelled turn is never started.
        self._cancelled: OrderedDict[int, None] = OrderedDict()
        # None = not yet probed. False latches when the device build predates
        # the registry, so a missing feature costs one log line, not one per row.
        self._supported: bool | None = None
        self._closed = False

    # -- loop-thread entry point ------------------------------------------

    def submit_event(self, payload: bytes) -> bool:
        """Parse/enqueue on the Session event thread; never perform I/O here."""
        if not self._enabled:
            return False
        try:
            request = parse_llm_event(payload)
        except Cm5LlmProtocolError as exc:
            log.warning("rejected LLM event: %s", exc)
            return True
        if request is None:
            return False

        if isinstance(request, LlmCancel):
            self._remember_cancel(request.session)
            current = self._current
            if current is not None and current.session == request.session:
                current.cancel.set()
            else:
                log.info("llm_cancel %d arrived before/after its generation",
                         request.session)
            return True

        # A select restarts llama-server and a new ask supersedes: the firmware
        # only mints a new session once it considers the old one done, so an
        # in-flight generation here is one it has already abandoned.
        current = self._current
        if current is not None:
            log.info("superseding in-flight generation %d", current.session)
            current.cancel.set()
        self._enqueue(request)
        return True

    def _remember_cancel(self, session: int) -> None:
        self._cancelled[session] = None
        self._cancelled.move_to_end(session)
        while len(self._cancelled) > _CANCEL_MEMORY:
            self._cancelled.popitem(last=False)

    def _enqueue(self, request) -> None:
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            log.error("LLM event queue full; dropped %s", type(request).__name__)
            if isinstance(request, LlmAsk):
                # Never leave the device waiting on a turn we threw away.
                self._remember_cancel(request.session)

    # -- lifecycle ---------------------------------------------------------

    async def attach(self, client, supervisor) -> None:
        """Bind the model plane once llama-server is up and /health is green."""
        self._client = client
        self._supervisor = supervisor
        if not self._enabled or client is None:
            return
        changed = self._refresh_catalog()
        self._active_name = self._name_for_path(self._cfg.model)
        # run() normally wins this race and has already published for this
        # epoch; re-sending an unchanged catalog would just cost eight lines on
        # a link that admits one per loop lap.
        if changed or not self._published:
            await self._publish_catalog()
        await self._send_ready()

    async def run(self) -> None:
        """Publish the catalog for this login epoch, then serve requests."""
        if not self._enabled:
            return
        self._refresh_catalog()
        await self._publish_catalog()
        # Re-announce on reconnect: the worker is recreated per link epoch, and
        # a device that rebooted under us has an empty registry.
        if self._client is not None and self._active_name:
            await self._send_ready()

        while True:
            request = await self._queue.get()
            try:
                if isinstance(request, LlmSelect):
                    await self._do_select(request)
                elif isinstance(request, LlmAsk):
                    await self._do_ask(request)
            except (LinkClosed, asyncio.CancelledError):
                raise
            except Exception:
                log.exception("LLM request failed unexpectedly")
            finally:
                self._queue.task_done()

    def link_reset(self) -> None:
        """Abandon epoch-bound work before a supervisor reconnect.

        The firmware fences a live generation on the named UART session epoch
        that started it, so a push replayed into the replacement login would be
        rejected anyway — and replaying it would weaken that boundary.
        """
        current = self._current
        if current is not None:
            current.cancel.set()
        self._current = None
        self._supported = None
        self._published = False
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            else:
                self._queue.task_done()

    async def close(self) -> None:
        self._closed = True
        current = self._current
        if current is not None:
            current.cancel.set()

    # -- catalog -----------------------------------------------------------

    def _model_dir(self) -> str:
        if self._cfg.model_dir:
            return self._cfg.model_dir
        # Default to wherever the configured checkpoint lives: the common case
        # is one directory of GGUFs, and that needs no new config to work.
        return os.path.dirname(os.path.expanduser(self._cfg.model)) if self._cfg.model else ""

    def _refresh_catalog(self) -> bool:
        models = discover_models(self._model_dir(), active_path=self._cfg.model)
        if self._catalog_ready and models == self._models:
            return False
        self._catalog_ready = True
        self._models = models
        self._by_name = {m.name.casefold(): m for m in models}
        self._generation = (self._generation + 1) & 0xFFFFFFFF or 1
        return True

    def _name_for_path(self, path: str) -> str | None:
        if not path:
            return None
        target = os.path.abspath(os.path.expanduser(path))
        for model in self._models:
            if os.path.abspath(model.path) == target:
                return model.name
        return None

    async def _publish_catalog(self) -> None:
        count = len(self._models)
        if not count:
            log.warning("no GGUF models found in %r — the device picker will be "
                        "empty", self._model_dir() or "(unset)")
        for idx, model in enumerate(self._models):
            line = (f"cm5 llm models {self._generation} {idx} {count} "
                    f"{model.size_mb} {escape(model.name)}")
            if not await self._send(line):
                return          # unsupported or link trouble; stop the burst
        self._published = True
        if count:
            log.info("published %d model(s) to the device registry (gen=%d)",
                     count, self._generation)

    async def _report_load_progress(self) -> None:
        """Push measured load progress at 1 Hz while llama-server populates.

        The percentage is resident-bytes/model-bytes from /proc — see
        LlamaServerSupervisor.load_residency for why that is the only signal
        that moves during the load, and for the on-device measurements behind
        these constants.

        Two rules keep it honest:
          * RATCHET. Residency can dip (measured: it fell 39,469,056 ->
            39,124,992 early in a run), and a bar that goes backwards reads as
            a fault. Never emit less than the last value sent.
          * CAP at 96. Residency reaches 100% of the GGUF ~2s before the server
            answers /health, because KV/compute-buffer allocation and warmup
            decode follow and nothing observes them. Measured: 45.1s of 47.1s,
            so the unobserved tail is 4.3%. Sitting at 96 while that finishes is
            truthful; showing 100 and then hanging is not.
        """
        last_sent = -1
        try:
            while True:
                await asyncio.sleep(1.0)
                sample = self._supervisor.load_residency() if self._supervisor else None
                if sample is None:
                    continue
                resident, total = sample
                if total <= 0:
                    continue
                pct = int(resident * 100 / total)
                if pct > _LOAD_PCT_CAP:
                    pct = _LOAD_PCT_CAP
                if pct <= last_sent:
                    continue
                last_sent = pct
                await self._send(f"cm5 llm loading {self._generation} {pct}")
        except asyncio.CancelledError:
            raise
        except LinkClosed:
            # The link died mid-load. Nothing to report to and nothing to fix
            # here; the select path already handles the failure.
            return
        except Exception:
            # Progress is cosmetic. It must never be the reason a model switch
            # fails, so swallow anything else and let the load run silently.
            log.exception("load-progress reporter stopped early")

    async def _send_ready(self) -> None:
        if not self._active_name:
            return
        await self._send(
            f"cm5 llm ready {self._generation} {escape(self._active_name)}")

    async def _report_actual_model(self) -> None:
        """Clear the device's LOADING state with the truth after a failed select.

        The protocol has no "select failed" verb by design — the firmware treats
        this host as authoritative about what it is serving and adopts whatever
        ``ready`` reports.  Re-publishing the catalog also drops a phantom row,
        so the surfaces stop offering a model that is not there.
        """
        self._refresh_catalog()
        await self._publish_catalog()
        if self._active_name:
            await self._send_ready()
        else:
            log.error("no model is being served, so the device will stay in "
                      "LOADING until one is — check llm.model / llm.model_dir")

    # -- select ------------------------------------------------------------

    async def _do_select(self, request: LlmSelect) -> None:
        self._refresh_catalog()
        model = self._by_name.get(request.name.casefold())
        if model is None:
            log.error("llm_select %r: no such model on this host", request.name)
            await self._report_actual_model()
            return
        if self._client is None:
            log.error("llm_select %r: the model plane is not up", request.name)
            await self._report_actual_model()
            return
        if model.name == self._active_name and await self._healthy():
            await self._send_ready()          # idempotent re-selection
            return
        if self._supervisor is None:
            # No supervised child to restart: either external-server mode
            # (llm.server_bin unset, someone else owns its lifetime) or a fake
            # engine. Either way the loaded model is the only one on offer.
            log.error("llm_select %r ignored: this daemon does not control "
                      "which model is loaded", request.name)
            await self._report_actual_model()
            return

        log.info("switching llama-server to %s", model.path)
        # Report measured progress while switch_model() blocks. It has to run
        # concurrently: switch_model does not return until the child is healthy,
        # which on a cold CM5 load is ~47s of silence.
        reporter = asyncio.create_task(
            self._report_load_progress(), name="llm-load-progress")
        try:
            switched = await self._supervisor.switch_model(model.path)
        except Exception as exc:
            log.error("model switch to %s failed: %s", model.path, exc)
            switched = False
        finally:
            reporter.cancel()
            # A cancelled reporter must not leave the device stuck at whatever
            # percentage it last saw; ready/actual-model below is what clears
            # LOADING, and this line makes the bar honest in between.
            with contextlib.suppress(asyncio.CancelledError):
                await reporter
        if switched:
            self._active_name = model.name
            # A different model is a different voice; carrying the old turns
            # over would bias its first answer with another model's words.
            self._client.clear_history()
        else:
            self._active_name = self._name_for_path(self._cfg.model)
        await self._report_actual_model()

    async def _healthy(self) -> bool:
        if self._client is None:
            return False
        if self._supervisor is None:
            return True
        try:
            return await self._supervisor.healthy()
        except Exception as exc:
            log.warning("health probe failed: %s", exc)
            return False

    # -- generation --------------------------------------------------------

    async def _do_ask(self, request: LlmAsk) -> None:
        if request.session in self._cancelled:
            log.info("llm_ask %d was cancelled before it started", request.session)
            await self._send_end(request.session, "stopped")
            return
        if self._client is None:
            log.error("llm_ask %d rejected: the model plane is not up",
                      request.session)
            await self._send_end(request.session, "error")
            return

        gen = _Generation(request.session)
        self._current = gen
        status = "ok"
        presence_token = None
        if self._cm5_presence is not None:
            presence_token = await self._cm5_presence.acquire_busy(
                f"llm:{self._active_name or 'generate'}")
        stream = self._client.ask_stream(
            request.prompt,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            top_p=request.top_p,
        )
        acc = ""
        sent = 0
        try:
            async for delta in stream:
                if gen.cancel.is_set():
                    status = "stopped"
                    break
                now = time.monotonic()
                if gen.first_delta_t is None:
                    gen.first_delta_t = now
                gen.last_delta_t = now
                gen.deltas += 1
                acc += delta.translate(_WIRE_SAFE)
                while not gen.aborted:
                    cut = _flush_point(acc, sent, opened=gen.opened)
                    if cut is None:
                        break
                    await self._push(gen, acc[sent:cut])
                    sent = cut
                if gen.aborted:
                    break
                if now - gen.last_push_t >= _KEEPALIVE_S:
                    # Prefill on a cold model can outlast the device's stall
                    # timer before a single token exists. An empty push is a
                    # documented no-op there that still stamps the clock.
                    await self._keepalive(gen)
            if status == "ok" and not gen.aborted:
                await self._push(gen, acc[sent:])
            if gen.aborted:
                # Every rejection path already logged its reason; the device has
                # stopped accepting this turn, so report it as a failed one.
                status = "error"
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception as exc:
            log.error("generation %d failed: %s", gen.session, exc)
            status = "error"
        finally:
            # aclose() throws GeneratorExit at the yield, so an abandoned turn
            # closes the HTTP stream AND never commits a partial answer to
            # history — the same rule native EvenAI delivery follows.
            await stream.aclose()
            self._current = None
            if presence_token is not None:
                # Released before the terminal `end`, which is the last thing
                # this turn owes the device — and never awaited, since this
                # finally can run under TaskGroup cancellation.
                self._cm5_presence.release_busy(presence_token)
            try:
                await self._send_end(gen.session, status,
                                     tokens=gen.deltas, tps=self._tps(gen),
                                     gen=gen)
            except (LinkClosed, asyncio.CancelledError):
                raise
            except Exception as exc:
                log.error("could not close out generation %d: %s",
                          gen.session, exc)

    @staticmethod
    def _tps(gen: _Generation) -> float:
        if (gen.first_delta_t is None or gen.last_delta_t is None or
                gen.deltas < 2):
            return 0.0
        span = gen.last_delta_t - gen.first_delta_t
        return (gen.deltas - 1) / span if span > 0 else 0.0

    async def _push(self, gen: _Generation, text: str) -> bool:
        """Send one delta group as one or more seq'd chunks.

        Returns False once the device has stopped accepting this generation.
        An empty group is a no-op here; the keepalive path sends the deliberate
        empty chunk.
        """
        for part in split_for_push(text):
            if not await self._push_chunk(gen, part):
                return False
            gen.opened = True
        return True

    async def _keepalive(self, gen: _Generation) -> bool:
        return await self._push_chunk(gen, "")

    async def _push_chunk(self, gen: _Generation, part: str) -> bool:
        line = f"cm5 llm push {gen.session} {gen.seq} {escape(part)}"
        if not await self._send(line, retry_idempotent=True, gen=gen):
            gen.aborted = True
            return False
        gen.seq += 1
        gen.last_push_t = time.monotonic()
        return True

    async def _send_end(self, session: int, status: str, *,
                        tokens: int = 0, tps: float = 0.0,
                        gen: _Generation | None = None) -> None:
        """Terminate the turn on the device. A lost end is the worst failure
        mode here: every surface shows a hung answer until the firmware's
        stall timer eventually abandons it."""
        await self._send(
            f"cm5 llm end {session} {status} {tokens} {int(tps * 10)}", gen=gen)

    # -- transport ---------------------------------------------------------

    async def _send(self, line: str, *, retry_idempotent: bool = False,
                    gen: _Generation | None = None) -> bool:
        if self._supported is False or self._closed:
            return False
        try:
            reply = await self._session.command(
                line,
                timeout=self._cfg.uart_timeout_s,
                expect="status",
                # Session's own replay re-logs-in first, which would transplant
                # an epoch-bound line into a new epoch; and a blind replay of a
                # push would duplicate answer text, because pushes are appends.
                replay=False,
                auth_replay=False,
            )
        except LinkClosed:
            raise
        except CommandTimeout:
            if not retry_idempotent:
                log.error("timed out sending %r", _brief(line))
                return False
            # Pushes are idempotent by seq on the device — an already-applied
            # seq is accepted and ignored — so replaying exactly this line is
            # safe and recovers a dropped reply without duplicating text.
            log.info("timeout on %r — replaying the same seq once", _brief(line))
            try:
                reply = await self._session.command(
                    line, timeout=self._cfg.uart_timeout_s, expect="status",
                    replay=False, auth_replay=False)
            except LinkClosed:
                raise
            except Exception as exc:
                log.error("replay of %r failed: %s", _brief(line), exc)
                return False
        except Exception as exc:
            log.error("could not send %r: %s", _brief(line), exc)
            return False

        if reply.ok:
            self._supported = True
            return True
        text = reply.text
        if "Unknown command" in text:
            if self._supported is None:
                log.warning("this device build has no CM5 LLM registry — not "
                            "offering the host as a model source")
            self._supported = False
            return False
        if any(reason in text for reason in _BENIGN_REJECTIONS):
            if ("session epoch mismatch" in text and gen is not None and
                    not gen.cancel.is_set()):
                # Not a cancel and not our stall: the firmware decided the CM5
                # went away mid-answer, which it only does when the presence
                # lease goes stale (cm5LlmTick's second wedge check). Name the
                # cause rather than filing it as routine, because the shape of
                # this failure — one slow command starving a sibling actor —
                # reads like a link fault and is not one.
                log.warning(
                    "device abandoned generation %d mid-answer (%s). This is "
                    "almost always a stale CM5 presence lease: the LLM bridge "
                    "shares one Session command lock with the 5s heartbeat, so "
                    "any command blocking longer than the firmware's 15s lease "
                    "starves it and the firmware concludes the host is gone. "
                    "Check llm.uart_timeout_s and the preceding log for a "
                    "command timeout.", gen.session, text)
                return False
            log.info("device already closed this turn (%r): %s",
                     _brief(line), text)
            return False
        log.error("device rejected %r: %s", _brief(line), text)
        return False


def _brief(line: str) -> str:
    return line if len(line) < 60 else line[:57] + "..."
