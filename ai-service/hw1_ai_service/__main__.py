"""CLI entry: hw1-ai-service {probe|ask|chat|daemon}.

  probe          open link, log in, run `uartlink status` — first-light check
  ask            one full voice exchange (record on XIAO -> STT -> LLM -> display)
  chat "text"    text-only exchange (no mic)
  daemon         hold the session, serve the control socket, run jobs forever
                 (supervises link reconnect: close -> backoff -> reopen -> re-login)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys

from . import config as config_mod
from . import log as log_mod
from . import mem
from .cm5_presence import Cm5Presence, Cm5PresenceMode
from .cm5_time import Cm5Time
from .fan import FanController
from .jobs import ManualTrigger, route_link_event
from .link.session import CommandTimeout, LinkClosed, LoginFailed, Session
from .link.transport import SerialTransport
from .pipeline import VoicePipeline, abort_evenai_best_effort
from .power import PowerController
from .systemd_watchdog import SystemdWatchdog

log = logging.getLogger("main")

_G2_CONFIG_TIMEOUT_S = 5.0


def _cancel_marker_interval_arg(raw: str) -> float:
    """Argparse type for the daemon-only, non-gating wearer-test marker."""
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError("must be finite")
    if value == 0:
        return 0.0
    if not (0.05 <= value <= 10.0):
        raise argparse.ArgumentTypeError(
            "must be 0 (disabled) or between 0.05 and 10 seconds")
    return value


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser separately so daemon-only diagnostics are tested."""
    parser = argparse.ArgumentParser(prog="hw1-ai-service")
    parser.add_argument("-c", "--config", default=None, help="config YAML path")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("probe")
    sub.add_parser("ask")
    chat = sub.add_parser("chat")
    chat.add_argument("text")
    daemon = sub.add_parser("daemon")
    daemon.add_argument(
        "--evenai-cancel-marker-interval-s",
        type=_cancel_marker_interval_arg,
        default=0.0,
        metavar="SECONDS",
        help=("diagnostic only: repeatedly log TAP NOW during natural EvenAI "
              "cancellation windows; adds no artificial pause or wire mutation"),
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()

    log_mod.setup(args.verbose)
    cfg = config_mod.load(args.config)
    try:
        for msg in mem.preflight(cfg):    # RAM budget: warn/strict per config
            (log.warning if "WARNING" in msg else log.info)(msg)
    except RuntimeError as exc:
        if args.cmd == "daemon" and (cfg.power.enabled or cfg.fan.enabled):
            # Strict AI sizing must not remove an explicitly enabled power
            # or fan control plane. Run control-only rather than risk OOM.
            log.error("%s — continuing daemon in control-only mode", exc)
            cfg.stt.engine = "none"
            cfg.llm.engine = "none"
        else:
            log.error("%s", exc)
            sys.exit(2)
    try:
        asyncio.run(_run(args, cfg))
    except KeyboardInterrupt:
        sys.exit(130)


def _build_live_gate(args, cfg):
    """Gate E construction, fail-soft: any problem returns (None, None) and
    the daemon runs the batch-only pipeline it always had."""
    if args.cmd != "daemon" or not cfg.stt.live_enabled or not cfg.stt.live_model_dir:
        return None, None
    if cfg.stt.engine == "none":
        # Control-only / strict-RAM-degraded daemon: batch STT was disabled to
        # avoid model-load OOM, so warming a live streaming model would defeat
        # exactly that guard. No STT means no transcription at all.
        log.info("live STT skipped — stt.engine is 'none'")
        return None, None
    if cfg.link.baud < 921600:
        log.warning("live STT requires link.baud >= 921600 (got %d) — "
                    "batch path only", cfg.link.baud)
        return None, None
    from .audio.live import LivePcmInbox
    from .stt.live_gate import LiveSttGate, fresh_controller_id
    try:
        inbox = LivePcmInbox(fresh_controller_id())
        gate = LiveSttGate(cfg.stt, inbox)
    except Exception as exc:
        log.error("live STT gate unavailable (%s) — batch path only", exc)
        return None, None
    return inbox, gate


async def _run(args, cfg) -> None:
    # Start before UART login: a powered-off/resetting XIAO can legitimately
    # consume the login retry window, but the Python control loop is healthy
    # and must continue satisfying the systemd watchdog during that wait.
    watchdog = SystemdWatchdog.from_environment()
    await watchdog.start()
    transport = None
    live_gate = None
    try:
        user, password = config_mod.read_credentials(cfg.link.credentials_file)
        # The frame sink must own LIVE dispatch before the port opens (BEGIN
        # must not race admission) and is immutable for the transport's life.
        live_inbox, live_gate = _build_live_gate(args, cfg)
        transport = SerialTransport(
            cfg.link.port, cfg.link.baud, frame_sink=live_inbox)
        transport.open()
        session = Session(transport, user, password)
        await session.login()
        if args.cmd == "probe":
            rep = await session.command("uartlink status", expect="auto", timeout=20)
            print(rep.text)
            return
        await _run_pipeline(args, cfg, transport, session, live_gate)
    finally:
        if live_gate is not None:
            await live_gate.close()
        if transport is not None:
            transport.close()
        await watchdog.close()


async def _run_pipeline(args, cfg, transport: SerialTransport, session: Session,
                        live_gate=None) -> None:
    if args.cmd == "daemon":
        await _run_daemon(
            cfg, transport, session, live_gate=live_gate,
            cancel_marker_interval_s=args.evenai_cancel_marker_interval_s)
        return

    from .stt import create_engine

    # One-shots can opt into the same automatic profile policy, but need no EVT
    # worker.  Everything is None-tolerant so partial model startup still tears
    # down a llama-server child (review finding).
    llm_client = supervisor = pipeline = power = None
    power_active = False
    try:
        if cfg.power.enabled:
            power = PowerController(session, cfg.power)
            await power.start()
        stt_engine = (await asyncio.to_thread(
            create_engine, cfg.stt.engine, cfg.stt.model)
            if args.cmd == "ask" else None)
        llm_client, supervisor = await _make_llm(cfg)
        pipeline = VoicePipeline(session, stt_engine, llm_client, cfg,
                                 power_activity=power)
        if power is not None:
            await power.activity_started()
            power_active = True
        if args.cmd == "ask":
            print(await pipeline.run_ask())
        elif args.cmd == "chat":
            print(await pipeline.run_chat(args.text))
    finally:
        if power is not None and power_active:
            await power.activity_finished()
        if pipeline is not None:
            await pipeline.close()
        if llm_client is not None:
            await llm_client.close()
        if supervisor is not None:
            await supervisor.stop()
        if power is not None:
            await power.close()


async def _run_daemon(
        cfg, transport: SerialTransport, session: Session, *,
        live_gate=None, cancel_marker_interval_s: float = 0.0) -> None:
    """Bring up the finite host control planes before any heavyweight model.

    ``_LazyDaemonPipeline`` performs STT/LLM construction inside the supervised
    task group. Consequently the Session event pump and power/fan workers are
    live during model loading and remain live if model initialization fails.
    """
    trigger = ManualTrigger()
    power = PowerController(session, cfg.power)
    fan = FanController(session, cfg.fan)
    cm5_presence = Cm5Presence(session)
    cm5_time = Cm5Time(session)
    pipeline = _LazyDaemonPipeline(
        session, cfg, power, live_gate=live_gate,
        cancel_marker_interval_s=cancel_marker_interval_s,
        cm5_presence=cm5_presence)
    if cancel_marker_interval_s > 0:
        log.warning(
            "EvenAI cancellation TAP-NOW markers enabled every %.3fs "
            "(diagnostic log I/O only; no artificial pause or wire command)",
            cancel_marker_interval_s)
    try:
        await trigger.serve_socket(cfg.service.socket_path)
        session.on_event = lambda payload: route_link_event(
            payload, trigger, session, power, fan)
        await power.start()
        await _daemon_supervised(
            pipeline,
            trigger,
            transport,
            session,
            power,
            cfg.deliver.g2_stream_speed,
            live_gate=live_gate, fan=fan,
            cm5_presence=cm5_presence,
            cm5_time=cm5_time,
        )
    finally:
        session.on_event = None
        await trigger.close()
        await pipeline.close()
        await fan.close()
        await power.close()


async def _daemon_supervised(pipeline, trigger: ManualTrigger,
                             transport: SerialTransport, session: Session,
                             power: PowerController,
                             g2_stream_speed: int, *, live_gate=None,
                             fan: FanController | None = None,
                             cm5_presence: Cm5Presence | None = None,
                             cm5_time: Cm5Time | None = None) -> None:
    """Run the daemon loop + the idle event pump, recovering the link when it
    dies: close -> backoff -> reopen -> re-login (the ARCHITECTURE §3
    reconnect story). The pump makes idle-time link death (and idle-time EVT
    pushes) visible; either task raising LinkClosed cancels its sibling and
    triggers the reconnect."""
    backoff = 1.0
    # This is a daemon-start policy, not a state reconciler. If the first
    # attempt loses the serial link, retain it through that reconnect; after a
    # completed success/error/timeout, do not mutate G2 state again until the
    # daemon itself restarts.
    g2_config_pending = g2_stream_speed != 0
    while True:
        try:
            if g2_config_pending:
                await _submit_g2_stream_speed(session, g2_stream_speed)
                g2_config_pending = False
            reboot_generation = getattr(session, "reboot_generation", None)
            async with asyncio.TaskGroup() as tg:
                tg.create_task(pipeline.daemon(trigger))
                tg.create_task(session.pump_events())
                tg.create_task(power.run())
                if fan is not None:
                    tg.create_task(fan.run())
                if cm5_presence is not None:
                    tg.create_task(cm5_presence.run())
                if cm5_time is not None:
                    tg.create_task(cm5_time.run())
                if (reboot_generation is not None and
                        callable(getattr(
                            session, "wait_for_reboot_after", None))):
                    tg.create_task(_fail_link_on_new_reboot(
                        session, reboot_generation))
            return
        except* LinkClosed:
            log.warning("link lost — reconnecting in %.0fs", backoff)
            if live_gate is not None:
                live_gate.link_reset()
            if fan is not None:
                # A new UART login creates a new authenticated epoch. Host-fan
                # callbacks are request-epoch-bound, so abandon the old finite
                # records instead of replaying them into the new session.
                fan.link_reset()
            if cm5_presence is not None:
                cm5_presence.link_reset()
            if cm5_time is not None:
                cm5_time.link_reset()
            transport.close()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            try:
                transport.open()
                await session.login()
                # A repaired serial link is not a host boot/resume. Replaying
                # only pending request callbacks avoids falsely clearing the
                # firmware's low-power wake latch with an id=0 awake report.
                power.replay_pending_callbacks()
                log.info("link re-established")
                backoff = 1.0
            except Exception as exc:
                log.error("reconnect failed: %s — will retry", exc)
                transport.close()


async def _fail_link_on_new_reboot(session: Session,
                                   generation: int) -> None:
    """Turn a new reboot hint into supervised reconnect/re-probation.

    A hint already present when the task group starts belongs to
    ``pipeline.daemon``'s startup probation. Only a newer generation tears
    down the running group, which also wakes an idle ``next_job()`` wait.
    """
    await session.wait_for_reboot_after(generation)
    raise LinkClosed("device reboot suspected")


async def _submit_g2_stream_speed(session: Session, speed: int) -> bool:
    """Best-effort daemon initialization of EvenAI CONFIG field 2.

    The firmware command returns after the XIAO builds and writes the BLE
    envelope; the G2 CONFIG echo/COMM_RSP arrives asynchronously. Therefore a
    successful return means "submitted", not "confirmed applied". Replaying
    after a timeout is safe because writing the same numeric setting is
    idempotent.
    """
    if speed == 0:
        log.info("automatic G2 EvenAI streamSpeed submission disabled")
        return False

    command = f"g2aiconfig - {speed} -"
    try:
        reply = await session.command(
            command,
            expect="status",
            timeout=_G2_CONFIG_TIMEOUT_S,
            replay=True,
        )
    except (CommandTimeout, LoginFailed) as exc:
        log.warning(
            "G2 EvenAI streamSpeed=%d submission failed; daemon continues: %s",
            speed,
            exc,
        )
        return False

    if not reply.ok:
        log.warning(
            "G2 EvenAI streamSpeed=%d was not submitted; daemon continues: %s",
            speed,
            reply.text,
        )
        return False

    log.info(
        "G2 EvenAI streamSpeed=%d submitted (XIAO accepted BLE write; "
        "G2 CONFIG echo not observed on this channel)",
        speed,
    )
    return True


class _LazyDaemonPipeline:
    """Model plane that cannot take down the already-running control plane."""

    def __init__(self, session: Session, cfg, power: PowerController, *,
                 live_gate=None, cancel_marker_interval_s: float = 0.0,
                 cm5_presence: Cm5Presence | None = None) -> None:
        self._session = session
        self._cfg = cfg
        self._power = power
        self._live_gate = live_gate
        self._cancel_marker_interval_s = cancel_marker_interval_s
        self._cm5_presence = cm5_presence
        self._pipeline: VoicePipeline | None = None
        self._llm_client = None
        self._supervisor = None
        self._stt_load_task: asyncio.Task | None = None
        self._initialization_failed = False

    async def daemon(self, source: ManualTrigger) -> None:
        if self._pipeline is None and not self._initialization_failed:
            await self._initialize()
        if self._pipeline is not None:
            await self._pipeline.daemon(source)
            return

        if self._cm5_presence is not None:
            self._cm5_presence.set_mode_nowait(Cm5PresenceMode.DEGRADED)
        log.error("AI pipeline unavailable; UART host controls remain active")
        while True:
            job = await source.next_job()
            log.warning("dropping %s job because AI initialization failed", job.kind)
            if job.kind == "evenai" and job.exchange is not None:
                try:
                    await abort_evenai_best_effort(
                        self._session, job.exchange, "pipeline_unavailable")
                finally:
                    source.evenai_done(job.exchange.exchange_id)

    async def _initialize(self) -> None:
        from .stt import create_engine

        try:
            # Model constructors perform synchronous file I/O and allocation.
            # Off-loop loading is what lets the UART pump/power worker service
            # the firmware's bounded retry window during a slow model load.
            if self._stt_load_task is None:
                self._stt_load_task = asyncio.create_task(
                    asyncio.to_thread(
                        create_engine, self._cfg.stt.engine, self._cfg.stt.model),
                    name="stt-model-load",
                )
            # to_thread work cannot be canceled. Shield and retain this single
            # task so a UART flap reuses it instead of launching overlapping
            # native model loads and risking OOM.
            stt_engine = await asyncio.shield(self._stt_load_task)
            self._llm_client, self._supervisor = await _make_llm(self._cfg)
            self._pipeline = VoicePipeline(
                self._session, stt_engine, self._llm_client, self._cfg,
                power_activity=self._power, live_gate=self._live_gate,
                cancel_marker_interval_s=self._cancel_marker_interval_s,
                cm5_presence=(self._cm5_presence
                              if stt_engine is not None else None))
            if stt_engine is None and self._cm5_presence is not None:
                self._cm5_presence.set_mode_nowait(Cm5PresenceMode.DEGRADED)
            if self._live_gate is not None:
                # Warm the streaming worker only after the batch models are in
                # (sequenced loads — never two native model loads racing RAM).
                self._live_gate.start()
        except asyncio.CancelledError:
            await self._close_model_resources()
            raise
        except Exception:
            log.exception("AI initialization failed")
            self._initialization_failed = True
            if self._cm5_presence is not None:
                self._cm5_presence.set_mode_nowait(Cm5PresenceMode.DEGRADED)
            await self._close_model_resources()

    async def _close_model_resources(self) -> None:
        if self._llm_client is not None:
            await self._llm_client.close()
            self._llm_client = None
        if self._supervisor is not None:
            await self._supervisor.stop()
            self._supervisor = None

    async def close(self) -> None:
        if self._pipeline is not None:
            await self._pipeline.close()
            self._pipeline = None
        await self._close_model_resources()
        if self._stt_load_task is not None:
            try:
                await asyncio.shield(self._stt_load_task)
            except Exception:
                pass
            self._stt_load_task = None


async def _make_llm(cfg):
    if cfg.llm.engine == "none":
        return None, None                  # STT-only mode: ask delivers transcripts
    if cfg.llm.engine == "fake":
        from .llm.fake import FakeLlm
        return FakeLlm(), None
    from .llm.client import LlmClient
    from .llm.server import LlamaServerSupervisor
    supervisor = LlamaServerSupervisor(cfg.llm)
    try:
        await supervisor.start()
    except BaseException:
        await supervisor.stop()
        raise
    return LlmClient(cfg.llm, supervisor.base_url), supervisor


if __name__ == "__main__":
    main()
