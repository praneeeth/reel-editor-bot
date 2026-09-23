"""Single asyncio worker: runs one job step at a time, in FIFO order."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from .db import DB
from .pipeline import Pipeline
from .states import QUEUED, RUNNING, STEP_FOR_QUEUED, JobState

log = logging.getLogger(__name__)

# running state -> the queued state that re-runs it
RESUME_STATE = {running: queued for queued, (running, _) in STEP_FOR_QUEUED.items()}


class Worker:
    def __init__(self, db: DB, pipeline: Pipeline):
        self.db = db
        self.pipeline = pipeline
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: list[str] = []
        self.current_job: str | None = None
        self._current_task: asyncio.Task | None = None

    async def enqueue(self, job_id: str) -> int:
        """Queue a job whose state is one of the *_QUEUED states. Returns queue position."""
        self._pending.append(job_id)
        await self.queue.put(job_id)
        return self.position(job_id)

    def position(self, job_id: str) -> int:
        """0 = running now, 1 = next, ... ; -1 = not queued."""
        if self.current_job == job_id:
            return 0
        try:
            return self._pending.index(job_id) + 1
        except ValueError:
            return -1

    def cancel(self, job_id: str) -> bool:
        """Stop the running step if it belongs to job_id (queued ones are skipped later)."""
        if self.current_job == job_id and self._current_task and not self._current_task.done():
            self._current_task.cancel()
            return True
        return False

    async def recover(self) -> None:
        """After a restart: re-queue queued jobs and re-run steps that were interrupted.

        Every step can safely run again from its inputs (transcripts are cached, the agent
        rewrites its own outputs, FINAL is deterministic), so an interrupted step goes back
        to its queued state and the user is told it is resuming.
        """
        for job in self.db.jobs_in_states(RUNNING):
            queued = RESUME_STATE[job.state]
            self.db.update(job.id, state=queued, error=None)
            log.info("resuming %s after restart (%s -> %s)", job.id, job.state, queued)
            notifier = getattr(self.pipeline, "notifier", None)
            if notifier is not None:
                try:
                    await notifier.status(self.db.get(job.id),
                                          "The bot restarted mid-step – resuming your job…")
                except Exception:  # noqa: BLE001 - telling the user is best effort
                    log.warning("could not notify %s about resuming", job.id)
        for job in self.db.jobs_in_states(QUEUED):
            self._pending.append(job.id)
            self.queue.put_nowait(job.id)

    async def run_forever(self) -> None:
        while True:
            job_id = await self.queue.get()
            if job_id in self._pending:
                self._pending.remove(job_id)
            job = self.db.get(job_id)
            if job is None or job.state not in QUEUED:
                self.queue.task_done()
                continue
            self.current_job = job_id
            self._current_task = asyncio.create_task(self.pipeline.run(job_id))
            try:
                await self._current_task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise  # the worker itself is shutting down
                log.info("step for %s was cancelled", job_id)
            except Exception:  # noqa: BLE001 - the worker must never die
                log.exception("unexpected error running %s", job_id)
            finally:
                self.current_job = None
                self._current_task = None
                self.queue.task_done()


def cleanup_old_jobs(db: DB, jobs_dir: Path, hours: int) -> list[str]:
    """Delete job folders 24h (configurable) after final delivery / cancel / failure."""
    removed = []
    for job in db.jobs_to_clean(hours * 3600):
        shutil.rmtree(jobs_dir / job.id, ignore_errors=True)
        db.update(job.id, cleaned=1)
        removed.append(job.id)
    return removed


async def cleanup_loop(db: DB, jobs_dir: Path, hours: int, every_s: int = 3600) -> None:
    while True:
        try:
            removed = cleanup_old_jobs(db, jobs_dir, hours)
            if removed:
                log.info("cleaned up %d job folder(s): %s", len(removed), ", ".join(removed))
        except Exception:  # noqa: BLE001
            log.exception("cleanup failed")
        await asyncio.sleep(every_s)


__all__ = ["Worker", "cleanup_loop", "cleanup_old_jobs", "JobState"]
