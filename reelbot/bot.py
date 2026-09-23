"""Telegram handlers, notifier and application wiring."""

from __future__ import annotations

import asyncio
import html
import logging
import shutil
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application, ApplicationBuilder, ApplicationHandlerStop, CallbackQueryHandler,
    CommandHandler, ContextTypes, MessageHandler, TypeHandler, filters,
)

from .agent import make_backend
from .config import MAX_OUTPUT_SECONDS, Settings
from .db import DB, Job
from .media import VIDEO_EXTS, probe
from .pipeline import Pipeline, job_dir
from .skill import ensure_skill_registered
from .states import QUEUED, RUNNING, Event, InvalidTransition, JobState
from .worker import Worker, cleanup_loop

log = logging.getLogger(__name__)

MAX_CLIPS = 10
LOCAL_API_CONTAINER_DIR = "/var/lib/telegram-bot-api"

START_TEXT = (
    "Hi! I turn your clips into a vertical Reel / Short (1080x1920, max 3 min, bold captions).\n\n"
    "1. Send one or more clips. Send them as a <b>File</b> (📎 → File) so Telegram doesn't "
    "compress them.\n"
    "2. Then tell me what you want, e.g. <i>cut the ums, punchy 60s reel, bold captions</i>.\n"
    "3. I reply with a plan → tap <b>Approve</b> → I send a 720p preview.\n"
    "4. Reply with changes (e.g. <i>trim the intro, yellow captions</i>) or tap <b>Final</b> for "
    "the full-quality export.\n\n"
    "/new – start a new job · /status – what I'm doing · /cancel – stop the current job"
)

STATE_LABEL = {
    JobState.COLLECTING: "collecting clips – send more clips or your instructions",
    JobState.PLAN_QUEUED: "queued for planning",
    JobState.PLANNING: "planning",
    JobState.AWAITING_APPROVAL: "waiting for you to approve the plan",
    JobState.AWAITING_PLAN_FEEDBACK: "waiting for your changes to the plan",
    JobState.EXECUTE_QUEUED: "queued for editing",
    JobState.EXECUTING: "editing + rendering the preview",
    JobState.PREVIEW_READY: "preview sent – reply with changes or tap Final",
    JobState.REVISE_QUEUED: "queued for revision",
    JobState.REVISING: "revising",
    JobState.FINAL_QUEUED: "queued for the final render",
    JobState.FINALIZING: "rendering the final video",
    JobState.DELIVERED: "delivered",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
}


def _mb(n: int | None) -> str:
    return f"{(n or 0) / 1024 / 1024:.1f} MB"


# ---------------------------------------------------------------- notifier ----------


class TelegramNotifier:
    """Sends pipeline progress and results to the job's chat."""

    def __init__(self, app_or_bot, settings: Settings, db: DB):
        self._src = app_or_bot
        self.settings = settings
        self.db = db
        self._status_state: dict[str, JobState] = {}

    @property
    def bot(self):
        return getattr(self._src, "bot", self._src)

    async def status(self, job: Job, text: str) -> None:
        msg_text = f"⏳ {text}"
        try:
            if job.status_message_id and self._status_state.get(job.id) == job.state:
                await self.bot.edit_message_text(msg_text, chat_id=job.chat_id,
                                                 message_id=job.status_message_id)
                return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        msg = await self.bot.send_message(job.chat_id, msg_text)
        self._status_state[job.id] = job.state
        self.db.update(job.id, status_message_id=msg.message_id)

    async def plan(self, job: Job, plan: dict) -> None:
        est = plan.get("estimated_duration_s")
        lines = [f"<b>Edit plan</b>{f' (~{float(est):.0f}s)' if est else ''}",
                 html.escape(str(plan.get("summary", "")))]
        beats = plan.get("beats") or []
        if beats:
            lines.append("")
            for b in beats[:12]:
                q = str(b.get("quote", ""))[:60]
                lines.append(f"• <b>{html.escape(str(b.get('beat', '')))}</b> "
                             f"{html.escape(q)}")
        if plan.get("assumptions"):
            lines += ["", "<i>Assumed:</i> " + html.escape("; ".join(map(str,
                                                                          plan["assumptions"])))]
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{job.id}"),
            InlineKeyboardButton("✏️ Change", callback_data=f"change:{job.id}"),
        ]])
        await self.bot.send_message(job.chat_id, "\n".join(lines)[:4000], parse_mode="HTML",
                                    reply_markup=kb)

    async def preview(self, job: Job, path: Path, summary: str) -> None:
        info = probe(path)
        caption = (f"Preview v{job.revision + 1} · {info['duration']:.1f}s (720p)\n"
                   f"{summary}\n\nReply with changes, or tap Final for the 1080x1920 export.")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🎬 Final", callback_data=f"final:{job.id}"),
            InlineKeyboardButton("✏️ Change", callback_data=f"tweak:{job.id}"),
        ]])
        with path.open("rb") as f:
            await self.bot.send_video(job.chat_id, video=f, caption=caption[:1024],
                                      width=info["width"], height=info["height"],
                                      duration=int(info["duration"]), supports_streaming=True,
                                      reply_markup=kb)

    async def final(self, job: Job, path: Path, summary: str) -> None:
        with path.open("rb") as f:
            await self.bot.send_document(
                job.chat_id, document=f, filename=f"reel_{job.id}.mp4",
                caption=f"✅ Final reel · {summary}\nSent as a file so Telegram keeps full "
                        f"quality. Send /new for the next one.")

    async def error(self, job: Job, text: str) -> None:
        await self.bot.send_message(job.chat_id, f"⚠️ {text}"[:4000])


# ---------------------------------------------------------------- handlers ----------


class Handlers:
    def __init__(self, settings: Settings, db: DB, worker: Worker):
        self.settings = settings
        self.db = db
        self.worker = worker

    def register(self, app: Application) -> None:
        app.add_handler(TypeHandler(Update, self.gate), group=-1)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.start))
        app.add_handler(CommandHandler("new", self.new))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("cancel", self.cancel))
        app.add_handler(MessageHandler(
            filters.VIDEO | filters.Document.ALL | filters.VIDEO_NOTE, self.media))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text))
        app.add_handler(CallbackQueryHandler(self.button))

    # -- access control: the bot runs on the owner's personal subscription ----------
    async def gate(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None or user.id not in self.settings.allowed_user_ids:
            log.warning("rejected update from user %s", user.id if user else None)
            if update.effective_message is not None:
                await update.effective_message.reply_text(
                    "Sorry, this is a private bot.")
            elif update.callback_query is not None:
                await update.callback_query.answer("Private bot.")
            raise ApplicationHandlerStop

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await update.effective_message.reply_html(START_TEXT)

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user, chat = update.effective_user, update.effective_chat
        old = self.db.active_job(user.id)
        note = ""
        if old:
            self._cancel(old)
            note = " (cancelled the previous one)"
        self.db.create_job(user.id, chat.id)
        await update.effective_message.reply_text(
            f"New job started{note}. Send your clips as a File, then your instructions.")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        job = self.db.active_job(update.effective_user.id) or \
            self.db.latest_job(update.effective_user.id)
        if job is None:
            await update.effective_message.reply_text("No jobs yet. Send a clip to start.")
            return
        lines = [f"Job {job.id}: {STATE_LABEL[job.state]}", f"Clips: {len(job.clips)}"]
        if job.state in QUEUED:
            pos = self.worker.position(job.id)
            lines.append(f"Queue position: {pos}" if pos > 0 else "Starting…")
        if job.error and job.state in (JobState.FAILED, JobState.AWAITING_APPROVAL,
                                       JobState.PREVIEW_READY):
            lines.append(f"Last error: {job.error[:300]}")
        metrics = self.db.metrics_for(job.id)
        agent = [m for m in metrics if m["backend"] != "local"]
        if metrics:
            turns = sum(m["turns"] or 0 for m in agent)
            cost = sum(m["cost_usd"] or 0 for m in agent)
            wall = sum(m["wall_s"] for m in metrics)
            lines.append(f"Agent calls: {len(agent)} · turns: {turns} · "
                         f"est. API cost: ${cost:.2f} · total time: {wall:.0f}s")
        await update.effective_message.reply_text("\n".join(lines))

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        job = self.db.active_job(update.effective_user.id)
        if job is None:
            await update.effective_message.reply_text("Nothing to cancel.")
            return
        self._cancel(job)
        await update.effective_message.reply_text("Cancelled. Send /new to start again.")

    def _cancel(self, job: Job) -> None:
        self.db.transition(job.id, Event.CANCEL)
        self.worker.cancel(job.id)

    # -- clips -----------------------------------------------------------------------
    async def media(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        user, chat = update.effective_user, update.effective_chat
        as_video = msg.video or msg.video_note
        tg = as_video or msg.document
        name = getattr(tg, "file_name", None) or ""
        ext = Path(name).suffix.lower() if name else ".mp4"
        if msg.document and not ((msg.document.mime_type or "").startswith("video/")
                                 or ext in VIDEO_EXTS):
            await msg.reply_text("That doesn't look like a video. Send an .mp4 / .mov file.")
            return
        if ext not in VIDEO_EXTS:
            ext = ".mp4"

        limit = self.settings.download_limit
        if tg.file_size and tg.file_size > limit:
            await msg.reply_text(self._too_big(tg.file_size))
            return

        job = self.db.active_job(user.id)
        if job is None:
            job = self.db.create_job(user.id, chat.id)
        if job.state != JobState.COLLECTING:
            await msg.reply_text(
                f"Your current job is {STATE_LABEL[job.state]}. Send /new to start a new job "
                "with these clips.")
            return
        if len(job.clips) >= MAX_CLIPS:
            await msg.reply_text(f"Max {MAX_CLIPS} clips per job. Now send your instructions.")
            return

        d = job_dir(self.settings, job)
        d.mkdir(parents=True, exist_ok=True)
        dest = d / f"clip{len(job.clips) + 1:02d}{ext}"
        try:
            await self._download(context, tg.file_id, dest)
        except BadRequest as e:
            if "too big" in str(e).lower():
                await msg.reply_text(self._too_big(tg.file_size))
                return
            raise
        try:
            info = await asyncio.to_thread(probe, dest)
            if not info["has_video"]:
                raise ValueError("no video stream")
        except Exception:  # noqa: BLE001
            dest.unlink(missing_ok=True)
            await msg.reply_text("I couldn't read that file as a video. Try another file.")
            return
        job = self.db.add_clip(job.id, str(dest))
        text = (f"Got clip {len(job.clips)} ({_mb(dest.stat().st_size)}, "
                f"{info['duration']:.1f}s, {info['width']}x{info['height']}). "
                "Send more clips, or tell me how to edit them.")
        if as_video:
            text += ("\nTip: Telegram compressed this one. For best quality send clips as a "
                     "File (📎 → File).")
        await msg.reply_text(text)

    def _too_big(self, size: int | None) -> str:
        if self.settings.local_bot_api:
            return (f"That file is {_mb(size)} – over the 2000 MB limit of the local Bot API "
                    "server. Please trim or compress it.")
        return (f"That file is {_mb(size)}. Telegram only lets bots download files up to "
                "20 MB. Trim or compress it (or split it into shorter clips) and send it "
                "again as a File. (The bot owner can lift this limit by running the local Bot "
                "API server – see README.)")

    async def _download(self, context: ContextTypes.DEFAULT_TYPE, file_id: str,
                        dest: Path) -> None:
        tg_file = await context.bot.get_file(file_id)
        if self.settings.local_bot_api:
            src = Path(tg_file.file_path)
            if not src.exists() and tg_file.file_path.startswith(LOCAL_API_CONTAINER_DIR):
                rel = tg_file.file_path[len(LOCAL_API_CONTAINER_DIR):].lstrip("/")
                src = self.settings.telegram_api_data_dir / rel
            await asyncio.to_thread(shutil.copyfile, src, dest)
        else:
            await tg_file.download_to_drive(dest)

    # -- instructions / feedback ------------------------------------------------------
    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        text = (msg.text or "").strip()
        job = self.db.active_job(update.effective_user.id)
        if job is None:
            await msg.reply_text("Send me one or more clips first (as a File), then tell me "
                                 "how to edit them.")
            return
        if job.state == JobState.COLLECTING:
            if not job.clips:
                await msg.reply_text("Send me at least one clip first, then your instructions.")
                return
            job = self.db.transition(job.id, Event.INSTRUCTION, instruction=text)
            await self._queued(msg, job, "Got it – planning your edit")
        elif job.state in (JobState.AWAITING_APPROVAL, JobState.AWAITING_PLAN_FEEDBACK):
            job = self.db.transition(job.id, Event.FEEDBACK, feedback=text)
            await self._queued(msg, job, "Re-planning with your changes")
        elif job.state == JobState.PREVIEW_READY:
            job = self.db.transition(job.id, Event.FEEDBACK, feedback=text)
            await self._queued(msg, job, "Making those changes")
        else:
            await msg.reply_text(f"I'm busy: {STATE_LABEL[job.state]}. "
                                 "/status to check, /cancel to stop.")

    async def _queued(self, msg, job: Job, what: str) -> None:
        pos = await self.worker.enqueue(job.id)
        busy = self.worker.current_job not in (None, job.id)
        await msg.reply_text(f"{what}… (queue position {pos})" if busy or pos > 1
                             else f"{what}…")

    # -- inline buttons -----------------------------------------------------------------
    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        q = update.callback_query
        action, _, job_id = (q.data or "").partition(":")
        job = self.db.get(job_id)
        if job is None or job.user_id != update.effective_user.id:
            await q.answer("Unknown job.")
            return
        try:
            if action == "approve":
                job = self.db.transition(job.id, Event.APPROVE)
                await q.answer("Approved")
                await self._strip_buttons(q)
                await self._queued(q.message, job, "Approved – editing")
            elif action == "change":
                job = self.db.transition(job.id, Event.CHANGE)
                await q.answer()
                await self._strip_buttons(q)
                await q.message.reply_text("What should change in the plan? Reply in one "
                                           "message.")
            elif action == "final":
                job = self.db.transition(job.id, Event.FINAL)
                await q.answer("Rendering final")
                await self._strip_buttons(q)
                await self._queued(q.message, job, "Rendering the full-quality 1080x1920 "
                                                   "export")
            elif action == "tweak":
                await q.answer()
                if job.state == JobState.PREVIEW_READY:
                    await q.message.reply_text("Reply with what to change, e.g. "
                                               "“trim the intro, yellow captions”.")
            else:
                await q.answer()
        except InvalidTransition:
            await q.answer("That button is no longer active.")

    @staticmethod
    async def _strip_buttons(q) -> None:
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except TelegramError:
            pass


# ---------------------------------------------------------------- wiring --------------


def build_application(settings: Settings) -> Application:
    if not settings.bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set (see .env.example)")
    if not settings.allowed_user_ids:
        raise SystemExit("ALLOWED_USER_IDS is empty – refusing to start a bot that would run "
                         "your personal Claude/Codex subscription for anyone.")
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} not found on PATH")

    builder = (ApplicationBuilder().token(settings.bot_token)
               .read_timeout(60).write_timeout(600).media_write_timeout(600)
               .connect_timeout(30).pool_timeout(30))
    if settings.local_bot_api:
        base = settings.telegram_api_base_url
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot") \
                         .local_mode(True)
    app = builder.build()

    db = DB(settings.db_path)
    backend = make_backend(
        settings.agent_backend, model=settings.agent_model, timeout_s=settings.agent_timeout_s,
        max_turns=settings.agent_max_turns,
        extra_dirs=[Path(__file__).resolve().parent.parent / "vendor" / "video-use"],
        env={"ELEVENLABS_API_KEY": settings.elevenlabs_api_key} if settings.elevenlabs_api_key
        else {},
    )
    if not backend.available():
        log.error("%s CLI not found on PATH – jobs will fail until it is installed",
                  settings.agent_backend)
    try:
        ensure_skill_registered(settings.agent_backend)
    except Exception:  # noqa: BLE001
        log.exception("could not register the video-use skill")

    notifier = TelegramNotifier(app, settings, db)
    pipeline = Pipeline(settings, db, backend, notifier)
    worker = Worker(db, pipeline)
    Handlers(settings, db, worker).register(app)

    async def post_init(application: Application) -> None:
        worker.recover()
        application.create_task(worker.run_forever())
        application.create_task(cleanup_loop(db, settings.jobs_dir, settings.cleanup_after_hours))
        log.info("bot ready: backend=%s lean=%s local_api=%s max=%ss", settings.agent_backend,
                 settings.lean_mode, settings.local_bot_api, MAX_OUTPUT_SECONDS)

    app.post_init = post_init
    app.bot_data.update(db=db, worker=worker)
    return app


def run(settings: Settings) -> None:
    settings.jobs_dir.mkdir(parents=True, exist_ok=True)
    app = build_application(settings)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


__all__ = ["Handlers", "TelegramNotifier", "build_application", "run", "RUNNING"]
