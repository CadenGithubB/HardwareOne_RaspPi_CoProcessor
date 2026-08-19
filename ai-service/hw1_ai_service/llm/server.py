"""LlamaServerSupervisor: llama-server as a supervised child process.

Chosen shape per the plan §2: crash isolation from the STT/link side,
/health for readiness, mmap'd weights warm across restarts. The child dies
with us (start_new_session=False + explicit terminate on stop), and an
unexpected exit triggers backoff restart.
"""

from __future__ import annotations

import asyncio
import os
import logging

import httpx

from ..config import LlmConfig

log = logging.getLogger("llm.server")


_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")


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

    @property
    def model_path(self) -> str:
        return self._cfg.model

    def load_residency(self) -> tuple[int, int] | None:
        """(resident_bytes, model_bytes) for the loading child, or None.

        This is THE measurable signal for load progress, and it is an OS
        measurement rather than anything llama.cpp reports. llama.cpp maps the
        GGUF with mmap(MAP_POPULATE) (src/llama-mmap.cpp), so the entire load is
        one blocking syscall that emits nothing: the loader's own progress
        callback fires afterwards, over a pointer-assignment loop with no I/O,
        and races 0->100% in well under a second. Its stderr dot-printer is
        installed only when no callback is set, and the server always sets one.
        Residency is the only thing that moves during the wait.

        MEASURED on the CM5, cold, 2026-08-18: resident climbs near-linearly at
        ~82 MB/s, reaching the GGUF's size at 45.1s of a 47.1s total wall time
        (the remaining ~2s is KV/compute-buffer allocation plus warmup, which
        nothing observes). Residency overshoots the file size at the end -- it
        peaked at 107% -- because those buffers and the binary itself are
        resident too, so callers must clamp.

        /proc/<pid>/io read_bytes is deliberately NOT used: it is blind to mmap
        population on a warm cache (measured flat at 4 MB while 3.7 GB became
        resident) and undercounts by ~43% even when cold. Residency is correct
        in both regimes, so a max() of the two would only ever return residency.
        """
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return None
        try:
            size = os.path.getsize(self._cfg.model)
        except OSError:
            return None
        try:
            with open(f"/proc/{proc.pid}/statm", "r") as fh:
                resident_pages = int(fh.read().split()[1])
        except (OSError, IndexError, ValueError):
            # The child can exit between the liveness check and the read; a
            # missing /proc entry is that race, not an error worth logging.
            return None
        return resident_pages * _PAGE_SIZE, size

    async def healthy(self, timeout: float = 2.0) -> bool:
        """One bounded /health probe. False on any transport or status failure."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(f"{self.base_url}/health")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False

    async def switch_model(self, path: str) -> bool:
        """Restart the child on a different GGUF; True once it is serving it.

        A failed switch restores the previous model and starts it again, so a
        bad pick degrades to "the old model still works" rather than "the LLM
        is gone". The caller is expected to report whatever ends up loaded —
        the firmware treats this host as authoritative about that.
        """
        if not self._cfg.server_bin:
            raise RuntimeError(
                "external-server mode cannot switch models (llm.server_bin is unset)")
        previous = self._cfg.model
        if path == previous and await self.healthy():
            return True
        await self.stop()
        self._cfg.model = path
        try:
            await self.start()
            return True
        except Exception as exc:
            log.error("model switch to %s failed: %s — restoring %s",
                      path, exc, previous)
            await self.stop()
            self._cfg.model = previous
            try:
                await self.start()
            except Exception as restore_exc:
                log.error("could not restore %s either: %s",
                          previous, restore_exc)
            return False

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
