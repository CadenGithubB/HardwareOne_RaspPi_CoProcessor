"""Answer delivery: render CM5 text on the XIAO's display surfaces.

P0 targets the two commands that exist today (plan §Gap C0). Review-
corrected realities baked in here:

  - Over the UART wire, oledtext's failure reply is a BARE "ERROR" — the
    descriptive "OLED display not running..." text goes to the broadcast/
    audit sink and never reaches this channel. So the oledstart recovery
    triggers on ANY oledtext failure (once per delivery), not on matching
    text that cannot arrive.
  - oledtext and g2notify REPLACE the whole screen/lens per call. Chunks
    are paced with a dwell so a multi-chunk answer is actually readable;
    single-chunk answers (the norm for short voice replies) pay nothing.
  - g2notify only honors durations 1..599 (validated at config load).

P3 replaces this with llmpush streaming; this stays as the fallback path.
"""

from __future__ import annotations

import asyncio
import logging

from .config import DeliverConfig
from .link.session import Session

log = logging.getLogger("deliver")


def chunk_text(text: str, limit: int) -> list[str]:
    """Whitespace-aware chunking; every chunk's UTF-8 form fits `limit`."""
    if limit < 4:
        raise ValueError(f"chunk limit {limit} is too small (min 4 bytes)")
    text = " ".join(text.split())  # collapse newlines/runs — display lines wrap on-device
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for word in text.split(" "):
        candidate = f"{current} {word}".strip()
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        # A single over-long "word" gets hard-split on a codepoint boundary.
        while len(word.encode("utf-8")) > limit:
            cut = limit
            while cut > 0 and len(word[:cut].encode("utf-8")) > limit:
                cut -= 1
            if cut == 0:
                # limit smaller than one codepoint — unreachable with the
                # config floor, but never spin (review finding).
                raise ValueError(f"chunk limit {limit} smaller than one character")
            chunks.append(word[:cut])
            word = word[cut:]
        current = word
    if current:
        chunks.append(current)
    return chunks


async def deliver(session: Session, cfg: DeliverConfig, text: str) -> bool:
    """Send the answer to every configured target. True if any succeeded."""
    delivered = False
    oledstart_tried = False
    for i, chunk in enumerate(chunk_text(text, cfg.chunk_bytes)):
        if i:
            # Each command replaces the whole display — give the previous
            # chunk time to be read before overwriting it.
            await asyncio.sleep(cfg.chunk_dwell_s)
        for target in cfg.targets:
            if target == "oled":
                ok, oledstart_tried = await _deliver_oled(
                    session, cfg, chunk, oledstart_tried)
                delivered |= ok
            elif target == "g2":
                delivered |= await _deliver_g2(session, cfg, chunk)
            else:
                log.warning("unknown deliver target %r", target)
    return delivered


async def _deliver_oled(session: Session, cfg: DeliverConfig, chunk: str,
                        oledstart_tried: bool) -> tuple[bool, bool]:
    rep = await session.command(f"oledtext {chunk}", expect="status")
    if rep.ok:
        return True, oledstart_tried
    # The wire reply for OLED-not-running is a bare "ERROR" — no
    # distinguishing text ever reaches this channel, so recovery triggers
    # on any failure, once per delivery.
    if cfg.allow_oledstart and not oledstart_tried:
        log.info("oledtext failed (%s) — attempting oledstart once", rep.text)
        start = await session.command("oledstart", expect="status", timeout=90)
        if start.ok:
            rep = await session.command(f"oledtext {chunk}", expect="status")
            if rep.ok:
                return True, True
        return False, True
    log.warning("oledtext failed: %s", rep.text)
    return False, oledstart_tried


async def _deliver_g2(session: Session, cfg: DeliverConfig, chunk: str) -> bool:
    rep = await session.command(f"g2notify {cfg.g2_seconds} {chunk}", expect="status")
    if not rep.ok:
        log.warning("g2notify failed (G2 connected?): %s", rep.text)
    return rep.ok
