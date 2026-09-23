"""Bot handlers with mocked Telegram objects and a mocked worker/backend."""

import shutil
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from conftest import needs_media
from fakes import FakeBackend, FakeNotifier
from reelbot.bot import Handlers, TelegramNotifier
from reelbot.config import Settings
from reelbot.db import DB
from reelbot.pipeline import Pipeline
from reelbot.states import Event, JobState
from reelbot.worker import Worker

OWNER = 42


@pytest.fixture
def h(tmp_path):
    settings = Settings(jobs_dir=tmp_path / "jobs", allowed_user_ids=frozenset({OWNER}))
    db = DB(":memory:")
    worker = Worker(db, Pipeline(settings, db, FakeBackend(), FakeNotifier()))
    return Handlers(settings, db, worker)


def message(text=None, video=None, document=None):
    m = MagicMock()
    m.text = text
    m.video, m.video_note, m.document = video, None, document
    m.reply_text = AsyncMock()
    m.reply_html = AsyncMock()
    return m


def update(msg=None, user=OWNER, callback=None):
    return SimpleNamespace(effective_user=SimpleNamespace(id=user),
                           effective_chat=SimpleNamespace(id=user),
                           effective_message=msg, callback_query=callback)


def context(get_file=None):
    return SimpleNamespace(bot=SimpleNamespace(get_file=get_file or AsyncMock()))


def replies(msg):
    return " ".join(str(c.args[0]) for c in msg.reply_text.call_args_list)


def doc(size, name="take.mov", mime="video/quicktime"):
    return SimpleNamespace(file_id="F1", file_size=size, file_name=name, mime_type=mime)


async def test_gate_blocks_strangers(h):
    msg = message("hi")
    with pytest.raises(ApplicationHandlerStop):
        await h.gate(update(msg, user=999), context())
    assert "private bot" in replies(msg)
    await h.gate(update(message("hi")), context())  # owner passes


async def test_start_and_empty_status(h):
    msg = message("/start")
    await h.start(update(msg), context())
    assert "File" in msg.reply_html.call_args.args[0]
    msg = message("/status")
    await h.status(update(msg), context())
    assert "No jobs yet" in replies(msg)


async def test_too_big_file_is_refused_before_download(h):
    msg = message(document=doc(35 * 1024 * 1024))
    ctx = context()
    await h.media(update(msg), ctx)
    assert "35.0 MB" in replies(msg) and "20 MB" in replies(msg)
    ctx.bot.get_file.assert_not_called()
    assert h.db.active_job(OWNER) is None


async def test_get_file_too_big_error_is_explained(h):
    msg = message(document=doc(None))
    await h.media(update(msg), context(AsyncMock(side_effect=BadRequest("File is too big"))))
    assert "20 MB" in replies(msg)


async def test_non_video_document_rejected(h):
    msg = message(document=doc(1000, name="notes.pdf", mime="application/pdf"))
    await h.media(update(msg), context())
    assert "doesn't look like a video" in replies(msg)


def _tg_file(src):
    async def download_to_drive(dest):
        shutil.copyfile(src, dest)
    return SimpleNamespace(file_path="videos/file_1.mp4", download_to_drive=download_to_drive)


@needs_media
async def test_clip_then_instruction_queues_plan(h, short_clip_dir):
    src = short_clip_dir / "clip01.mp4"
    msg = message(document=doc(src.stat().st_size, name="clip.mp4", mime="video/mp4"))
    await h.media(update(msg), context(AsyncMock(return_value=_tg_file(src))))
    job = h.db.active_job(OWNER)
    assert job.state == JobState.COLLECTING and len(job.clips) == 1
    assert "Got clip 1" in replies(msg) and "compressed" not in replies(msg)

    # sent as a (compressed) video -> tip about sending as File
    vid = SimpleNamespace(file_id="F2", file_size=src.stat().st_size)
    msg2 = message(video=vid)
    await h.media(update(msg2), context(AsyncMock(return_value=_tg_file(src))))
    assert "Got clip 2" in replies(msg2) and "as a File" in replies(msg2)
    assert (h.settings.jobs_dir / job.id / "clip02.mp4").exists()

    msg3 = message("cut the ums, punchy 60s reel, bold captions")
    await h.text(update(msg3), context())
    job = h.db.get(job.id)
    assert job.state == JobState.PLAN_QUEUED
    assert job.instruction == "cut the ums, punchy 60s reel, bold captions"
    assert h.worker.position(job.id) == 1
    assert "planning" in replies(msg3)


async def test_text_without_clips(h):
    msg = message("make a reel")
    await h.text(update(msg), context())
    assert "clips first" in replies(msg)


async def test_local_bot_api_path_is_remapped(tmp_path):
    settings = Settings(jobs_dir=tmp_path / "jobs", allowed_user_ids=frozenset({OWNER}),
                        telegram_api_base_url="http://localhost:8081",
                        telegram_api_data_dir=tmp_path / "tgdata")
    assert settings.download_limit == 2000 * 1024 * 1024
    h = Handlers(settings, DB(":memory:"), MagicMock())
    (tmp_path / "tgdata" / "TOKEN" / "documents").mkdir(parents=True)
    (tmp_path / "tgdata" / "TOKEN" / "documents" / "f.mp4").write_bytes(b"data")
    f = SimpleNamespace(file_path="/var/lib/telegram-bot-api/TOKEN/documents/f.mp4")
    dest = tmp_path / "out.mp4"
    await h._download(context(AsyncMock(return_value=f)), "F", dest)
    assert dest.read_bytes() == b"data"


def _callback(data):
    q = MagicMock()
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_reply_markup = AsyncMock()
    q.message = message()
    return q


async def _job_in(h, state_events):
    job = h.db.create_job(OWNER, OWNER)
    h.db.add_clip(job.id, "x.mp4")
    for ev, kw in state_events:
        job = h.db.transition(job.id, ev, **kw)
    return job


async def test_approve_change_and_final_buttons(h):
    job = await _job_in(h, [(Event.INSTRUCTION, {"instruction": "x"}), (Event.START, {}),
                            (Event.DONE, {})])
    q = _callback(f"change:{job.id}")
    await h.button(update(callback=q), context())
    assert h.db.get(job.id).state == JobState.AWAITING_PLAN_FEEDBACK
    msg = message("make it 30 seconds")
    await h.text(update(msg), context())
    assert h.db.get(job.id).state == JobState.PLAN_QUEUED
    assert h.db.get(job.id).feedback == "make it 30 seconds"

    for ev in (Event.START, Event.DONE):
        h.db.transition(job.id, ev)
    q = _callback(f"approve:{job.id}")
    await h.button(update(callback=q), context())
    assert h.db.get(job.id).state == JobState.EXECUTE_QUEUED
    q.edit_message_reply_markup.assert_awaited()

    # a stale button is harmless
    q = _callback(f"approve:{job.id}")
    await h.button(update(callback=q), context())
    q.answer.assert_awaited_with("That button is no longer active.")

    for ev in (Event.START, Event.DONE):
        h.db.transition(job.id, ev)
    msg = message("yellow captions")
    await h.text(update(msg), context())
    assert h.db.get(job.id).state == JobState.REVISE_QUEUED
    for ev in (Event.START, Event.DONE):
        h.db.transition(job.id, ev)
    q = _callback(f"final:{job.id}")
    await h.button(update(callback=q), context())
    assert h.db.get(job.id).state == JobState.FINAL_QUEUED


async def test_buttons_for_someone_elses_job_are_ignored(h):
    job = await _job_in(h, [(Event.INSTRUCTION, {"instruction": "x"})])
    q = _callback(f"final:{job.id}")
    await h.button(update(callback=q, user=OWNER + 1), context())
    q.answer.assert_awaited_with("Unknown job.")


async def test_cancel_and_new(h):
    job = await _job_in(h, [(Event.INSTRUCTION, {"instruction": "x"}), (Event.START, {})])
    h.worker.cancel = MagicMock()
    msg = message("/cancel")
    await h.cancel(update(msg), context())
    assert h.db.get(job.id).state == JobState.CANCELLED
    h.worker.cancel.assert_called_once_with(job.id)
    msg = message("/new")
    await h.new(update(msg), context())
    assert h.db.active_job(OWNER).state == JobState.COLLECTING


async def test_busy_message_while_running(h):
    await _job_in(h, [(Event.INSTRUCTION, {"instruction": "x"}), (Event.START, {})])
    msg = message("hello?")
    await h.text(update(msg), context())
    assert "busy" in replies(msg)


async def test_status_shows_metrics(h):
    job = await _job_in(h, [(Event.INSTRUCTION, {"instruction": "x"}), (Event.START, {}),
                            (Event.DONE, {})])
    h.db.log_metric(job.id, "plan", "claude", True, 50.0, turns=12, cost_usd=0.31)
    h.db.log_metric(job.id, "transcribe", "local", True, 4.0, turns=0)
    msg = message("/status")
    await h.status(update(msg), context())
    out = replies(msg)
    assert "approve the plan" in out and "turns: 12" in out and "$0.31" in out


async def test_notifier_edits_status_message_within_a_step(tmp_path):
    db = DB(":memory:")
    job = db.create_job(OWNER, OWNER)
    bot = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=5)),
                          edit_message_text=AsyncMock())
    n = TelegramNotifier(bot, Settings(jobs_dir=tmp_path), db)
    await n.status(job, "Transcribing…")
    job = db.get(job.id)
    await n.status(job, "Planning…")
    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_awaited_once()
    await n.plan(job, {"summary": "Do <it>", "estimated_duration_s": 40,
                       "beats": [{"beat": "HOOK", "quote": "hi"}]})
    text = bot.send_message.call_args.args[1]
    assert "&lt;it&gt;" in text and "~40s" in text
    kb = bot.send_message.call_args.kwargs["reply_markup"].inline_keyboard[0]
    assert [b.callback_data for b in kb] == [f"approve:{job.id}", f"change:{job.id}"]
