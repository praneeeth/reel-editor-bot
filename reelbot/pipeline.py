"""The four job steps: PLAN, EXECUTE, REVISE (agent) and FINAL (deterministic)."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Protocol

from . import VIDEO_USE_DIR
from .agent import AgentBackend, AgentError, AgentResult
from .config import MAX_OUTPUT_SECONDS, Settings
from .db import DB, Job
from .edl import EDLError, check_edl
from .media import fit_to_size, inventory, probe
from .prompts import execute_prompt, plan_prompt, revise_prompt
from .states import STEP_FOR_QUEUED, Event, JobState

log = logging.getLogger(__name__)


class StepError(RuntimeError):
    """A failure with a message that is safe and useful to show the user."""


class Notifier(Protocol):
    async def status(self, job: Job, text: str) -> None: ...
    async def plan(self, job: Job, plan: dict) -> None: ...
    async def preview(self, job: Job, path: Path, summary: str) -> None: ...
    async def final(self, job: Job, path: Path, summary: str) -> None: ...
    async def error(self, job: Job, text: str) -> None: ...


def job_dir(settings: Settings, job: Job) -> Path:
    return settings.jobs_dir / job.id


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


class Pipeline:
    def __init__(self, settings: Settings, db: DB, backend: AgentBackend, notifier: Notifier):
        self.settings = settings
        self.db = db
        self.backend = backend
        self.notifier = notifier

    # ---- entry point used by the worker ------------------------------------------
    async def run(self, job_id: str) -> None:
        job = self.db.get(job_id)
        if job is None or job.state not in STEP_FOR_QUEUED:
            return  # cancelled or already handled
        _, step = STEP_FOR_QUEUED[job.state]
        job = self.db.transition(job_id, Event.START, error=None)
        t0 = time.monotonic()
        try:
            await getattr(self, f"step_{step}")(job)
        except asyncio.CancelledError:
            log.info("job %s cancelled during %s", job_id, step)
            raise
        except Exception as exc:  # noqa: BLE001 - report every failure to the user
            log.exception("job %s failed in %s", job_id, step)
            msg = str(exc) if isinstance(exc, (StepError, AgentError, EDLError)) else \
                f"{type(exc).__name__}: {exc}"
            job = self.db.transition(job_id, Event.FAIL, error=msg[:2000])
            hint = {
                JobState.FAILED: "Send /new to start over.",
                JobState.AWAITING_APPROVAL: "Tap Approve to retry or Change to re-plan.",
                JobState.PREVIEW_READY: "The previous preview is still valid - reply with "
                                        "changes or tap Final to retry.",
            }.get(job.state, "")
            await self.notifier.error(job, f"{step.upper()} failed: {msg[:1500]}\n{hint}")
        else:
            log.info("job %s %s done in %.1fs", job_id, step, time.monotonic() - t0)

    # ---- helpers ----------------------------------------------------------------------
    async def _agent(self, job: Job, step: str, prompt: str,
                     resume: str | None = None) -> AgentResult:
        d = job_dir(self.settings, job)
        (d / "edit" / "prompts").mkdir(parents=True, exist_ok=True)
        (d / "edit" / "prompts" / f"{step}_{int(time.time())}.txt").write_text(prompt)
        try:
            res = await self.backend.run(prompt, d, resume_session=resume)
        except AgentError as e:
            r = e.result
            self._log_agent(job, step, r, ok=False, detail=str(e)[:500])
            raise
        self._log_agent(job, step, res, ok=True)
        if res.session_id:
            self.db.update(job.id, session_id=res.session_id)
        return res

    def _log_agent(self, job: Job, step: str, r: AgentResult | None, ok: bool,
                   detail: str | None = None) -> None:
        if r is None:
            self.db.log_metric(job.id, step, self.backend.name, ok, 0.0, detail=detail)
            return
        self.db.log_metric(
            job.id, step, r.backend, ok, r.wall_s, turns=r.turns, input_tokens=r.input_tokens,
            output_tokens=r.output_tokens, cache_read_tokens=r.cache_read_tokens,
            cache_write_tokens=r.cache_write_tokens, cost_usd=r.cost_usd, detail=detail,
        )
        log.info("metrics job=%s step=%s backend=%s turns=%s in=%s out=%s cache_r=%s "
                 "cost=%s wall=%.1fs", job.id, step, r.backend, r.turns, r.input_tokens,
                 r.output_tokens, r.cache_read_tokens, r.cost_usd, r.wall_s)

    async def _timed_local(self, job: Job, step: str, fn, *args, **kwargs):
        t0 = time.monotonic()
        ok = False
        try:
            out = await asyncio.to_thread(fn, *args, **kwargs)
            ok = True
            return out
        finally:
            self.db.log_metric(job.id, step, "local", ok, time.monotonic() - t0, turns=0)

    # ---- PLAN -------------------------------------------------------------------------
    async def step_plan(self, job: Job) -> None:
        d = job_dir(self.settings, job)
        edit = d / "edit"
        edit.mkdir(parents=True, exist_ok=True)

        await self.notifier.status(job, "Checking clips…")
        inv = await asyncio.to_thread(inventory, d)
        if not inv or not any(i["has_video"] for i in inv):
            raise StepError("no readable video clips in this job")
        (edit / "inventory.json").write_text(json.dumps(inv, indent=2))

        await self.notifier.status(job, "Transcribing…")
        await self._timed_local(job, "transcribe", self._transcribe_all, d, inv)
        await asyncio.to_thread(self._pack, edit)

        previous = _read_json(edit / "plan.json") if job.feedback else None
        await self.notifier.status(job, "Planning… (the agent is reading the transcript)")
        prompt = plan_prompt(d, job.instruction or "", lean=self.settings.lean_mode,
                             eval_cap=self.settings.effective_self_eval_cap,
                             previous_plan=previous, feedback=job.feedback)
        if previous:
            shutil.copy(edit / "plan.json", edit / f"plan_prev_{int(time.time())}.json")
            (edit / "plan.json").unlink()
        await self._agent(job, "plan", prompt)

        plan = _read_json(edit / "plan.json")
        if not plan or not plan.get("summary"):
            raise StepError("the agent did not write edit/plan.json with a summary")
        job = self.db.transition(job.id, Event.DONE, feedback=None)
        await self.notifier.plan(job, plan)

    def _transcribe_all(self, d: Path, inv: list[dict]) -> None:
        sys.path.insert(0, str(VIDEO_USE_DIR / "helpers"))
        import transcribe as vu_transcribe  # vendor/video-use/helpers/transcribe.py

        edit = d / "edit"
        pending = [i for i in inv if i["has_audio"]
                   and not vu_transcribe.transcript_path(edit, Path(i["path"])).exists()]
        if not pending:
            return
        key = self.settings.elevenlabs_api_key
        if not key:
            raise StepError("ELEVENLABS_API_KEY is not set, and no cached transcript exists")
        for item in pending:
            try:
                vu_transcribe.transcribe_one(Path(item["path"]), edit, key, verbose=False)
            except RuntimeError as e:
                if "silent" in str(e):
                    log.info("skipping silent clip %s", item["name"])
                    continue
                raise StepError(f"transcription failed for {item['name']}: {e}") from e

    @staticmethod
    def _pack(edit: Path) -> None:
        if not any((edit / "transcripts").glob("*.json")):
            (edit / "takes_packed.md").write_text("(no speech found in any clip)\n")
            return
        subprocess.run(
            [sys.executable, str(VIDEO_USE_DIR / "helpers" / "pack_transcripts.py"),
             "--edit-dir", str(edit)],
            check=True, capture_output=True,
        )

    # ---- EXECUTE / REVISE ----------------------------------------------------------
    async def step_execute(self, job: Job) -> None:
        d = job_dir(self.settings, job)
        await self.notifier.status(job, "Editing… (cutting, rendering the preview, self-checking)")
        await self._agent(job, "execute", execute_prompt(
            d, lean=self.settings.lean_mode, eval_cap=self.settings.effective_self_eval_cap))
        await self._finish_preview(job)

    async def step_revise(self, job: Job) -> None:
        d = job_dir(self.settings, job)
        edit = d / "edit"
        rev = job.revision + 1
        (edit / "history").mkdir(exist_ok=True)
        if (edit / "edl.json").exists():
            shutil.copy(edit / "edl.json", edit / "history" / f"edl_v{rev - 1}.json")
        job = self.db.update(job.id, revision=rev)

        resume = None if self.settings.lean_mode else job.session_id
        await self.notifier.status(job, f"Revising (v{rev})… fresh agent session"
                                   if resume is None else f"Revising (v{rev})…")
        prompt = revise_prompt(d, job.feedback or "", lean=self.settings.lean_mode,
                               eval_cap=self.settings.effective_self_eval_cap,
                               resumed=resume is not None)
        await self._agent(job, "revise", prompt, resume=resume)
        await self._finish_preview(job)

    async def _finish_preview(self, job: Job) -> None:
        d = job_dir(self.settings, job)
        edit = d / "edit"
        edl_path = edit / "edl.json"
        check_edl(edl_path, allow_overlays=not self.settings.lean_mode)  # raises EDLError

        preview = edit / "preview.mp4"
        if not preview.exists() or preview.stat().st_mtime < edl_path.stat().st_mtime:
            await self.notifier.status(job, "Rendering preview…")
            from .render import render_edl
            await self._timed_local(job, "render_preview", render_edl, edl_path, preview,
                                    preview=True, allow_overlays=not self.settings.lean_mode)
        send = await asyncio.to_thread(fit_to_size, preview, edit / "preview_send.mp4",
                                       self.settings.upload_limit)
        result = _read_json(edit / "result.json") or {}
        summary = result.get("summary") or ""
        issues = result.get("issues") or []
        if issues:
            summary += "\nSelf-check notes: " + "; ".join(map(str, issues))[:600]
        job = self.db.transition(job.id, Event.DONE, feedback=None)
        await self.notifier.preview(job, send, summary.strip())

    # ---- FINAL (no LLM) -------------------------------------------------------------
    async def step_final(self, job: Job) -> None:
        d = job_dir(self.settings, job)
        edit = d / "edit"
        await self.notifier.status(job, "Rendering final 1080x1920…")
        from .render import render_edl
        res = await self._timed_local(job, "render_final", render_edl, edit / "edl.json",
                                      edit / "final.mp4", preview=False,
                                      allow_overlays=not self.settings.lean_mode)
        if (res.width, res.height) != (1080, 1920) or res.duration > MAX_OUTPUT_SECONDS + 0.5:
            raise StepError(f"final render is {res.width}x{res.height} {res.duration:.1f}s")
        send = res.path
        if res.size_bytes > self.settings.upload_limit:
            await self.notifier.status(job, "Compressing to fit Telegram's upload limit…")
            send = await self._timed_local(job, "fit_size", fit_to_size, res.path,
                                           edit / "final_send.mp4", self.settings.upload_limit)
        info = await asyncio.to_thread(probe, send)
        job = self.db.transition(job.id, Event.DONE)
        mb = send.stat().st_size / 1e6
        await self.notifier.final(job, send, f"{info['width']}x{info['height']} · "
                                             f"{info['duration']:.1f}s · {mb:.1f} MB")
