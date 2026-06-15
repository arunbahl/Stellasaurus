"""Asyncio task supervisor: keeps long-running tasks alive with backoff and runs
periodic jobs. One event loop owns everything in Phase 1 (DESIGN §5 concurrency
note: the workload is I/O-bound; no threads/processes needed yet)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from stellasaurus.common.logging import get_logger

_log = get_logger("background.scheduler")


class TaskSupervisor:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task[None]] = []

    def supervise(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        """Run ``factory()`` forever, restarting with capped backoff on failure."""
        self._tasks.append(asyncio.create_task(self._run_forever(name, factory), name=name))

    def run_periodic(
        self, name: str, interval_s: float, job: Callable[[], Awaitable[None]]
    ) -> None:
        """Run ``job()`` immediately, then every ``interval_s`` seconds."""
        coro = self._run_periodic(name, interval_s, job)
        self._tasks.append(asyncio.create_task(coro, name=name))

    async def _run_forever(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        backoff = 1.0
        while True:
            try:
                await factory()
                _log.warning("task_exited_restarting", task=name)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("task_crashed", task=name, error=str(exc), backoff_s=backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _run_periodic(
        self, name: str, interval_s: float, job: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _log.warning("periodic_job_failed", task=name, error=str(exc))
            await asyncio.sleep(interval_s)

    async def cancel_all(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
