"""Wire grammar for one correlated native EvenAI exchange.

Keep every spelling here.  The firmware and host are being upgraded together;
isolating the provisional grammar prevents protocol churn from leaking through
the pipeline.

The exchange ID is also the recorder owner.  It is a 64-bit hexadecimal value
whose high and low uint32 halves are non-zero (boot nonce + boot-local counter).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .link import protocol

_ID_RE = re.compile(r"^[0-9A-Fa-f]{16}$")
_REASON_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class EvenAiProtocolError(ValueError):
    pass


def exchange_id(raw: str) -> str:
    if not _ID_RE.fullmatch(raw):
        raise EvenAiProtocolError("exchange ID must be exactly 16 hexadecimal digits")
    value = raw.lower()
    if int(value[:8], 16) == 0 or int(value[8:], 16) == 0:
        raise EvenAiProtocolError("exchange ID nonce and counter must be non-zero")
    return value


@dataclass(frozen=True)
class WakeEvent:
    exchange_id: str


@dataclass(frozen=True)
class CancelEvent:
    exchange_id: str
    reason: str


@dataclass(frozen=True)
class MicAutostopEvent:
    exchange_id: str
    path: str


@dataclass(frozen=True)
class TimingEvent:
    """Device-side stage stamps (2026-08-10 firmware). Absolute device millis;
    anchor against the wake event's arrival to derive host-clock deltas.
    owner is '-' for manual (non-exchange) recordings. samples is the CAPTURED
    (pre-trim) count — correct for wall-vs-sample skew math, NOT file length.
    degraded mirrors the delivered-rate watchdog latch sealed at finalize."""
    exchange_id: str | None   # None when the recording had no exchange owner
    stamps_ms: dict[str, int]  # wake/claim/firstpcm/vadend/closed/preroll
    samples: int
    rate: int
    degraded: bool


@dataclass(frozen=True)
class StreamCompleteEvent:
    """Glasses reported the streamed reply fully painted (was debug-only)."""
    exchange_id: str


EvenAiEvent = (WakeEvent | CancelEvent | MicAutostopEvent | TimingEvent |
               StreamCompleteEvent)

_TIMING_KEYS = ("wake_ms", "claim_ms", "firstpcm_ms", "vadend_ms", "closed_ms",
                "preroll_ms")


def parse_event(text: str) -> EvenAiEvent | None:
    """Strictly parse recognized events; return None for unrelated events."""
    tokens = text.split()
    if not tokens:
        return None
    name = tokens[0]
    if name not in ("evenai_wake", "evenai_cancel", "mic_autostop",
                    "evenai_timing", "evenai_stream_complete"):
        return None
    if name == "evenai_wake":
        if len(tokens) != 2:
            raise EvenAiProtocolError("evenai_wake requires one exchange ID")
        return WakeEvent(exchange_id(tokens[1]))
    if name == "evenai_cancel":
        if len(tokens) != 3:
            raise EvenAiProtocolError("evenai_cancel requires ID and reason")
        reason = tokens[2]
        if not _REASON_RE.fullmatch(reason):
            raise EvenAiProtocolError("invalid cancellation reason")
        return CancelEvent(exchange_id(tokens[1]), reason)
    if name == "evenai_stream_complete":
        if len(tokens) != 2:
            raise EvenAiProtocolError(
                "evenai_stream_complete requires one exchange ID")
        return StreamCompleteEvent(exchange_id(tokens[1]))
    if name == "evenai_timing":
        # evenai_timing <owner|-> k=v ... — tolerate unknown extra tokens so a
        # future firmware can append fields without breaking this parser.
        if len(tokens) < 2:
            raise EvenAiProtocolError("evenai_timing requires an owner token")
        owner = None if tokens[1] == "-" else exchange_id(tokens[1])
        kv: dict[str, int] = {}
        for tok in tokens[2:]:
            if "=" not in tok:
                continue
            key, _, val = tok.partition("=")
            if not val.isdigit():
                continue
            kv[key] = int(val)
        stamps = {k: kv[k] for k in _TIMING_KEYS if k in kv}
        return TimingEvent(
            exchange_id=owner,
            stamps_ms=stamps,
            samples=kv.get("samples", 0),
            rate=kv.get("rate", 0),
            degraded=bool(kv.get("degraded", 0)),
        )

    if len(tokens) != 3:
        raise EvenAiProtocolError("mic_autostop requires ID and path")
    path = tokens[2]
    if not path.startswith("/") or len(path) > 255 or any(c in path for c in "\r\n\0"):
        raise EvenAiProtocolError("invalid mic_autostop path")
    return MicAutostopEvent(exchange_id(tokens[1]), path)


def ask_command(eid: str, text: str) -> str:
    return f"g2evenai askid {exchange_id(eid)} {text}"


def reply_command(eid: str, text: str) -> str:
    return f"g2evenai replyid {exchange_id(eid)} {text}"


def replypart_command(eid: str, text: str) -> str:
    return f"g2evenai replypartid {exchange_id(eid)} {text}"


def replyend_command(eid: str) -> str:
    return f"g2evenai replyendid {exchange_id(eid)}"


def exit_command(eid: str) -> str:
    return f"g2evenai exitid {exchange_id(eid)}"


def mic_status_command(eid: str) -> str:
    return f"micrecord statusid {exchange_id(eid)}"


def mic_stop_command(eid: str) -> str:
    return f"micrecord stopid {exchange_id(eid)}"


def mic_delete_command(eid: str, filename: str) -> str:
    return f"micdeleteid {exchange_id(eid)} {protocol.quote_path(filename)}"
