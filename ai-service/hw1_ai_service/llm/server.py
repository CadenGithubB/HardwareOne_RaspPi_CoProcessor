"""LlamaServerSupervisor: llama-server as a supervised child process.

Chosen shape per the plan §2: crash isolation from the STT/link side,
/health for readiness, mmap'd weights warm across restarts. The child dies
with us (start_new_session=False + explicit terminate on stop), and an
unexpected exit triggers backoff restart.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from ..config import LlmConfig

log = logging.getLogger("llm.server")


class LlamaServerSupervisor:
    def __init__(self, cfg: LlmConfig):
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._monitor: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stopping = False

    @property
    def base_url(self) -> str:
        return f"http://{self._cfg.host}:{self._cfg.port}"

    async def start(self) -> None:
        if not self._cfg.server_bin:
            # External-server mode: someone else runs llama-server; just probe.
            await self._wait_healthy(self._cfg.startup_timeout_s)
            log.info("using external llama-server at %s", self.base_url)
            return
        self._stopping = False
        await self._spawn()
        self._monitor = asyncio.create_task(self._monitor_main(), name="llama-monitor")

    async def stop(self) -> None:
        self._stopping = True
        for task_attr in ("_monitor", "_stderr_task"):
            task = getattr(self, task_attr)
            if task is not None:
                task.cancel()
                setattr(self, task_attr, None)
        proc = self._proc
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self._proc = None

    # -- internals ---------------------------------------------------------

    def _args(self) -> list[str]:
        cfg = self._cfg
        return [
            cfg.server_bin,
            "--model", cfg.model,
            "--host", cfg.host, "--port", str(cfg.port),
            "-t", "4", "-c", "2048",
            "--cache-reuse", "256",
            "--parallel", "1",
            *cfg.extra_args,
        ]

    async def _spawn(self) -> None:
        args = self._args()
        log.info("spawning llama-server: %s", " ".join(args))
        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        # Keep a strong reference — asyncio holds tasks weakly, and a GC'd
        # pump means the child's stderr pipe fills and blocks generation.
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        self._stderr_task = asyncio.create_task(
            self._pump_stderr(self._proc), name="llama-stderr")
        try:
            await self._wait_healthy(self._cfg.startup_timeout_s)
        except BaseException:
            # Startup failed or was cancelled (Ctrl-C during model load):
            # never orphan the child — it would hold RAM + the port and
            # poison the next start's health probe (review finding).
            proc = self._proc
            if proc is not None and proc.returncode is None:
                proc.kill()
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()), 5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            raise
        log.info("llama-server healthy at %s", self.base_url)

    async def _pump_stderr(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            log.debug("llama-server: %s", raw.decode(errors="replace").rstrip())

    async def _wait_healthy(self, timeout: float) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        async with httpx.AsyncClient(timeout=2.0) as client:
            while True:
                if self._proc is not None and self._proc.returncode is not None:
                    raise RuntimeError(
                        f"llama-server exited during startup "
                        f"(rc={self._proc.returncode}) — check model path/RAM")
                try:
                    resp = await client.get(f"{self.base_url}/health")
                    if resp.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                if asyncio.get_running_loop().time() > deadline:
                    raise RuntimeError(
                        f"llama-server not healthy within {timeout:.0f}s at "
                        f"{self.base_url}")
                await asyncio.sleep(1.0)

    async def _monitor_main(self) -> None:
        backoff = 2.0
        consec_fast_deaths = 0
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            spawned_at = asyncio.get_running_loop().time()
            rc = await proc.wait()
            if self._stopping:
                return
            # Fast repeated deaths are the OOM-kill signature on a too-small
            # Pi: stop thrashing the machine and let the pipeline degrade to
            # transcript-only delivery instead (graceful small-RAM behavior).
            if asyncio.get_running_loop().time() - spawned_at < 60.0:
                consec_fast_deaths += 1
            else:
                consec_fast_deaths = 0
            if consec_fast_deaths >= 5:
                log.error(
                    "llama-server died %d times in quick succession (rc=%s) — "
                    "giving up on the LLM for this run. Likely out of RAM: try "
                    "a smaller GGUF, or llm.engine: none for STT-only mode.",
                    consec_fast_deaths, rc)
                return
            log.warning("llama-server exited rc=%s — restarting in %.0fs", rc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)
            try:
                await self._spawn()
                backoff = 2.0
            except Exception as exc:
                log.error("llama-server restart failed: %s", exc)
