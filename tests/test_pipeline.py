"""Pipeline + worker with a mocked agent backend and a real ffmpeg render."""

import asyncio
import json
import shutil

import pytest

from conftest import needs_media
from fakes import FakeBackend, FakeNotifier
from reelbot.config import Settings
from reelbot.db import DB
from reelbot.media import probe
from reelbot.pipeline import Pipeline
from reelbot.states import Event, JobState
from reelbot.worker import Worker, cleanup_old_jobs

pytestmark = needs_media


@pytest.fixture
def env(tmp_path, short_clip_dir):
    settings = Settings(jobs_dir=tmp_path / "jobs", lean_mode=True, self_eval_cap=25)
    db = DB(":memory:")
    job = db.create_job(user_id=7, chat_id=7)
    d = settings.jobs_dir / job.id
    shutil.copytree(short_clip_dir, d)  # clip + pre-seeded transcript cache
    db.add_clip(job.id, str(d / "clip01.mp4"))
    return settings, db, job


async def test_full_flow_plan_execute_revise_final(env):
    settings, db, job = env
    backend, notifier = FakeBackend(), FakeNotifier()
    p = Pipeline(settings, db, backend, notifier)

    db.transition(job.id, Event.INSTRUCTION, instruction="cut the ums, punchy reel")
    await p.run(job.id)
    assert db.get(job.id).state == JobState.AWAITING_APPROVAL
    edit = settings.jobs_dir / job.id / "edit"
    assert (edit / "inventory.json").exists() and (edit / "takes_packed.md").exists()
    statuses = [e[1] for e in notifier.events if e[0] == "status"]
    assert "Checking clips…" in statuses
    assert "Transcribing…" not in statuses  # fixture transcript is cached (Hard Rule 9)
    inv = json.loads((edit / "inventory.json").read_text())
    assert inv["clips"][0]["spoken_words"] == 17 and inv["images"] == [] and inv["music"] == []
    plan_prompt = backend.calls[0]["prompt"]
    assert "cut the ums, punchy reel" in plan_prompt and "SKILL.md" in plan_prompt
    assert "At most 2 timeline_view" in plan_prompt  # lean plan: tiny visual budget

    db.transition(job.id, Event.APPROVE)
    await p.run(job.id)
    assert db.get(job.id).state == JobState.PREVIEW_READY
    kind, preview, summary = notifier.events[-1]
    assert kind == "preview" and summary == "EXECUTE ok"
    assert (probe(preview)["width"], probe(preview)["height"]) == (720, 1280)
    assert "At most 10 timeline_view" in backend.calls[1]["prompt"]  # LEAN caps 25 -> 10

    db.transition(job.id, Event.FEEDBACK, feedback="drop the intro, yellow captions")
    await p.run(job.id)
    job_now = db.get(job.id)
    assert job_now.state == JobState.PREVIEW_READY and job_now.revision == 1
    rev = backend.calls[2]
    assert rev["resume"] is None  # LEAN: fresh session
    assert "FRESH session" in rev["prompt"] and "drop the intro, yellow captions" in rev["prompt"]
    assert '"ranges"' in rev["prompt"]  # current EDL is inlined
    assert (edit / "history" / "edl_v0.json").exists()
    assert "YELLOW" not in (edit / "captions.ass").read_text()  # colour lives in the style
    assert "&H0000E6FF" in (edit / "captions.ass").read_text()   # yellow in ASS BGR

    db.transition(job.id, Event.FINAL)
    await p.run(job.id)
    assert db.get(job.id).state == JobState.DELIVERED
    kind, final, summary = notifier.events[-1]
    assert kind == "final" and summary.startswith("1080x1920")
    assert len(backend.calls) == 3  # FINAL made no agent call

    steps = [(m["step"], m["backend"]) for m in db.metrics_for(job.id)]
    assert ("plan", "fake") in steps and ("execute", "fake") in steps
    assert ("render_preview", "local") in steps and ("render_final", "local") in steps
    agent = [m for m in db.metrics_for(job.id) if m["backend"] == "fake"]
    assert all(m["turns"] == 5 and m["cost_usd"] == 0.01 for m in agent)


async def test_non_lean_revision_resumes_session(env):
    settings, db, job = env
    settings.lean_mode = False
    backend, notifier = FakeBackend(), FakeNotifier()
    p = Pipeline(settings, db, backend, notifier)
    for ev, kw in [(Event.INSTRUCTION, {"instruction": "x"}), (Event.APPROVE, {}),
                   (Event.FEEDBACK, {"feedback": "shorter"})]:
        db.transition(job.id, ev, **kw)
        await p.run(job.id)
    assert backend.calls[2]["resume"] == "sess-2"
    assert "FRESH session" not in backend.calls[2]["prompt"]


async def test_invalid_edl_falls_back_to_approval(env):
    settings, db, job = env
    notifier = FakeNotifier()
    p = Pipeline(settings, db, FakeBackend(bad_edl=True), notifier)
    db.transition(job.id, Event.INSTRUCTION, instruction="x")
    await p.run(job.id)
    db.transition(job.id, Event.APPROVE)
    await p.run(job.id)
    j = db.get(job.id)
    assert j.state == JobState.AWAITING_APPROVAL
    assert "cuts inside the word" in j.error
    assert notifier.kinds()[-1] == "error"


async def test_agent_failure_is_logged_and_reported(env):
    settings, db, job = env
    notifier = FakeNotifier()
    p = Pipeline(settings, db, FakeBackend(fail_step="PLAN"), notifier)
    db.transition(job.id, Event.INSTRUCTION, instruction="x")
    await p.run(job.id)
    assert db.get(job.id).state == JobState.FAILED
    assert "scripted failure" in notifier.events[-1][1]
    (m,) = [m for m in db.metrics_for(job.id) if m["step"] == "plan"]
    assert m["ok"] == 0 and m["turns"] == 5


async def test_missing_transcript_without_key_fails_clearly(env):
    settings, db, job = env
    shutil.rmtree(settings.jobs_dir / job.id / "edit" / "transcripts")
    notifier = FakeNotifier()
    db.transition(job.id, Event.INSTRUCTION, instruction="x")
    await Pipeline(settings, db, FakeBackend(), notifier).run(job.id)
    assert db.get(job.id).state == JobState.FAILED
    assert "ELEVENLABS_API_KEY" in notifier.events[-1][1]


async def test_worker_runs_one_job_at_a_time_and_cancels(env):
    settings, db, job = env
    order = []

    class SlowPipeline:
        async def run(self, job_id):
            order.append(("start", job_id))
            db.transition(job_id, Event.START)
            await asyncio.sleep(0.3)
            order.append(("end", job_id))

    job2 = db.create_job(user_id=8, chat_id=8)
    db.add_clip(job2.id, "x")
    w = Worker(db, SlowPipeline())
    runner = asyncio.create_task(w.run_forever())
    db.transition(job.id, Event.INSTRUCTION, instruction="a")
    db.transition(job2.id, Event.INSTRUCTION, instruction="b")
    await w.enqueue(job.id)
    await w.enqueue(job2.id)
    await asyncio.sleep(0.05)
    assert w.position(job.id) == 0 and w.position(job2.id) == 1
    await asyncio.sleep(0.4)
    assert order[:2] == [("start", job.id), ("end", job.id)]
    assert w.current_job == job2.id
    db.transition(job2.id, Event.CANCEL)
    assert w.cancel(job2.id)
    await asyncio.sleep(0.05)
    assert ("end", job2.id) not in order and w.current_job is None
    runner.cancel()


def test_cleanup_deletes_delivered_job_folders(env):
    settings, db, job = env
    for ev in (Event.INSTRUCTION, Event.START, Event.DONE, Event.APPROVE, Event.START,
               Event.DONE, Event.FINAL, Event.START, Event.DONE):
        db.transition(job.id, ev)
    assert cleanup_old_jobs(db, settings.jobs_dir, hours=24) == []
    db.update(job.id, delivered_at=1.0)
    assert cleanup_old_jobs(db, settings.jobs_dir, hours=24) == [job.id]
    assert not (settings.jobs_dir / job.id).exists()


async def test_assets_reach_the_prompts(env):
    from PIL import Image

    from reelbot.media import add_note

    settings, db, job = env
    d = settings.jobs_dir / job.id
    (d / "assets").mkdir()
    Image.new("RGBA", (100, 50), (0, 0, 0, 0)).save(d / "assets" / "image01.png")
    add_note(d, d / "assets" / "image01.png", "logo")
    backend = FakeBackend()
    db.transition(job.id, Event.INSTRUCTION, instruction="add my logo")
    await Pipeline(settings, db, backend, FakeNotifier()).run(job.id)
    prompt = backend.calls[0]["prompt"]
    assert "image01.png" in prompt and '"note": "logo"' in prompt and '"transparent": true' in prompt
    assert "At most 4 timeline_view" in prompt  # images get a bigger look budget
    db.transition(job.id, Event.APPROVE)
    await Pipeline(settings, db, backend, FakeNotifier()).run(job.id)
    assert '"images"' in backend.calls[1]["prompt"] and "transition_in" in backend.calls[1]["prompt"]
