#!/usr/bin/env python3
"""Exercise synthetic live-pcm-v1 transport without STT or pipeline changes.

The probe installs the direct reader-thread inbox *before* opening/login and
advertising host readiness, renews the short lease while the asynchronous
firmware producer runs, validates every offset and CRC, and compares the result
with the deterministic synthetic sample pattern.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import secrets
import sys
import time

from hw1_ai_service import config as config_mod
from hw1_ai_service import log as log_mod
from hw1_ai_service.audio.live import (
    LivePcmChunk,
    LivePcmInbox,
    LiveStreamTerminal,
    synthetic_pcm,
)
from hw1_ai_service.link import protocol
from hw1_ai_service.link.session import Session
from hw1_ai_service.link.transport import SerialTransport


log = logging.getLogger("tools.live_pcm_transport_probe")
CAPABILITY_TOKEN = "live-pcm-v1 synthetic=1"
MIN_BAUD = 921_600


def _fresh_id() -> int:
    high = secrets.randbits(32) or 1
    low = secrets.randbits(32) or 1
    return (high << 32) | low


def _id_arg(raw: str) -> int:
    try:
        value = int(raw, 16)
        protocol.live_id_hex(value)
        return value
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be 16 hex digits with nonzero high and low halves") from exc


def _duration_arg(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if not (1 <= value <= 60_000):
        raise argparse.ArgumentTypeError("must be between 1 and 60000 ms")
    return value


def _collect(inbox: LivePcmInbox, exchange_id: int,
             duration_ms: int) -> tuple[bytes, LiveStreamTerminal, dict]:
    stream = inbox.next_stream(timeout=3.0)
    if stream.exchange_id != exchange_id:
        stream.invalidate(
            f"probe_expected_exchange:{exchange_id:016x}")
    pcm = bytearray()
    deadline = time.monotonic() + max(8.0, duration_ms / 1000.0 + 5.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            stream.invalidate("probe_deadline")
            remaining = 0.1
        item = stream.next_item(timeout=remaining)
        if isinstance(item, LiveStreamTerminal):
            return bytes(pcm), item, stream.snapshot()
        assert isinstance(item, LivePcmChunk)
        pcm.extend(item.pcm)


async def _renew_lease(session: Session, controller_id: int,
                       stop: asyncio.Event, errors: list[str],
                       timing: protocol.LiveLeaseTiming,
                       session_epoch: int) -> None:
    command = f"liveaudio ready 1 {controller_id:016x}"
    interval_s = timing.renew_ms / 1000.0
    loop = asyncio.get_running_loop()
    next_send = loop.time() + interval_s
    while True:
        try:
            await asyncio.wait_for(
                stop.wait(), max(0.05, next_send - loop.time()))
            return
        except asyncio.TimeoutError:
            pass
        sent_at = loop.time()
        try:
            reply = await session.command(
                command, expect="status", timeout=5.0, replay=True)
            if not reply.ok:
                errors.append(f"lease renewal rejected: {reply.text}")
                return
            parsed = protocol.parse_live_ready(
                reply.text, expected_controller=controller_id)
            renewed_timing = protocol.live_lease_timing_from_ready(parsed)
            if parsed.session_epoch != session_epoch:
                errors.append("lease renewal changed session epoch")
                return
            if renewed_timing != timing:
                errors.append("lease renewal timing contract changed")
                return
            next_send = sent_at + interval_s
        except Exception as exc:
            errors.append(f"lease renewal failed: {type(exc).__name__}: {exc}")
            return


async def _best_effort_command(session: Session, command: str) -> None:
    try:
        await session.command(
            command, expect="status", timeout=5.0, replay=False)
    except Exception as exc:
        log.warning("best-effort %r failed: %s", command, exc)


async def run_probe(args) -> dict:
    cfg = config_mod.load(args.config)
    if cfg.link.baud < MIN_BAUD:
        raise RuntimeError(
            f"live-pcm-v1 requires link.baud >= {MIN_BAUD}; got {cfg.link.baud}")
    user, password = config_mod.read_credentials(cfg.link.credentials_file)
    controller_id = args.controller_id or _fresh_id()
    exchange_id = args.exchange_id or _fresh_id()
    inbox = LivePcmInbox(controller_id)
    transport = SerialTransport(
        cfg.link.port, cfg.link.baud, frame_sink=inbox)
    session = Session(transport, user, password)
    lease_stop = asyncio.Event()
    lease_errors: list[str] = []
    renew_task: asyncio.Task | None = None
    started = time.monotonic()
    synth_started = False
    lease_released = False
    try:
        transport.open()
        await session.login()
        capabilities = await session.command(
            "liveaudio capabilities", expect="status", timeout=5.0)
        try:
            capability_fields = protocol.parse_live_capabilities(
                capabilities.text) if capabilities.ok else {}
        except ValueError as exc:
            raise RuntimeError(
                f"malformed liveaudio capabilities: {exc}: "
                f"{capabilities.text}") from exc
        if capability_fields.get("synthetic") != "1":
            raise RuntimeError(
                f"required capability {CAPABILITY_TOKEN!r} unavailable: "
                f"{capabilities.text}")
        ready = await session.command(
            f"liveaudio ready 1 {controller_id:016x}",
            expect="status", timeout=5.0, replay=True)
        if not ready.ok:
            raise RuntimeError(f"live ready rejected: {ready.text}")
        try:
            ready_fields = protocol.parse_live_ready(
                ready.text, expected_controller=controller_id)
            lease_timing = protocol.live_lease_timing_from_ready(ready_fields)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid live ready contract: {exc}: {ready.text}") from exc
        renew_task = asyncio.create_task(
            _renew_lease(
                session, controller_id, lease_stop, lease_errors,
                lease_timing, ready_fields.session_epoch),
            name="live-pcm-lease-renew")
        synth = await session.command(
            f"liveaudio synth 1 {controller_id:016x} "
            f"{exchange_id:016x} {args.duration_ms}",
            expect="status", timeout=5.0, replay=False)
        if not synth.ok:
            raise RuntimeError(f"synthetic stream rejected: {synth.text}")
        synth_started = True
        pcm, terminal, stream_snapshot = await asyncio.to_thread(
            _collect, inbox, exchange_id, args.duration_ms)

        # Freeze renewal/cleanup outcomes before computing the gate result.
        # A ``return`` inside the try would evaluate ``ok`` before finally
        # awaited an in-flight renewal, allowing a late renewal failure to
        # leave ok=true with a non-empty lease_errors list.
        lease_stop.set()
        if renew_task is not None:
            await renew_task
            renew_task = None
        release = await session.command(
            f"liveaudio release 1 {controller_id:016x}",
            expect="status", timeout=5.0, replay=False)
        if release.ok:
            lease_released = True
        else:
            lease_errors.append(f"lease release rejected: {release.text}")

        expected_samples = (16_000 * args.duration_ms) // 1000
        expected = synthetic_pcm(exchange_id, expected_samples)
        pattern_ok = pcm == expected
        ok = terminal.valid and pattern_ok and not lease_errors
        return {
            "schema": 1,
            "ok": ok,
            "capability": CAPABILITY_TOKEN,
            "controller_id": f"{controller_id:016x}",
            "exchange_id": f"{exchange_id:016x}",
            "duration_ms": args.duration_ms,
            "expected_samples": expected_samples,
            "received_samples": len(pcm) // 2,
            "received_bytes": len(pcm),
            "pattern_ok": pattern_ok,
            "terminal": {
                "kind": terminal.kind,
                "valid": terminal.valid,
                "reason": terminal.reason,
                "total_samples": terminal.total_samples,
                "crc32": f"{terminal.pcm_crc32:08x}",
                "dropped_samples": terminal.dropped_samples,
            },
            "stream": stream_snapshot,
            "inbox": inbox.snapshot(),
            "lease_errors": lease_errors,
            "wall_seconds": time.monotonic() - started,
        }
    except BaseException:
        if synth_started:
            await _best_effort_command(
                session,
                f"liveaudio abort 1 {controller_id:016x} {exchange_id:016x}")
        raise
    finally:
        lease_stop.set()
        if renew_task is not None:
            await renew_task
        if not lease_released:
            await _best_effort_command(
                session, f"liveaudio release 1 {controller_id:016x}")
        transport.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate synthetic live-pcm-v1 transport; does not run STT")
    parser.add_argument("-c", "--config", default=None,
                        help="hw1-ai-service config YAML")
    parser.add_argument("--duration-ms", type=_duration_arg, default=2048)
    parser.add_argument("--controller-id", type=_id_arg, default=None,
                        help="optional reproducible 16-hex lease controller ID")
    parser.add_argument("--exchange-id", type=_id_arg, default=None,
                        help="optional reproducible 16-hex synthetic exchange ID")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_mod.setup(args.verbose)
    try:
        result = asyncio.run(run_probe(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({
            "schema": 1,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }, separators=(",", ":")))
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
