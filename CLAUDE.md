# Project rules

Telegram bot that edits clips into 9:16 Reels/Shorts with video-use (`vendor/video-use`,
a git submodule — never copy or edit it; wrap it from `reelbot/`).

- **No paid LLM API.** The agent is `claude -p` (default) or `codex exec` (backup), logged in
  with the owner's own accounts, behind `reelbot/agent/AgentBackend`, chosen by
  `AGENT_BACKEND`. The bot serves only `ALLOWED_USER_IDS` and refuses to start if it's empty.
- **Follow video-use's SKILL.md Hard Rules.** The Telegram Approve button is the strategy
  confirmation (Rule 11). All job outputs go in `jobs/<id>/edit/` (Rule 12).
- **Headless steps:** PLAN (inventory, transcribe, pack, strategy → `edit/plan.json`),
  EXECUTE (EDL → 720p preview → capped self-eval), REVISE (fresh session from project.md +
  edl.json + feedback), FINAL (deterministic 1080x1920 re-render, no LLM call).
- **LEAN_MODE=true (default):** ≤10 timeline_view self-eval checks, no animation overlays,
  revisions start a fresh agent session. Renderer features (transitions, speed/zoom,
  b-roll, images, texts, music, blur; see `reelbot/edl.py`) are allowed in lean mode.
- **Output:** 1080x1920 @ 30fps, ≤180 s, landscape cropped to 9:16, bold-overlay
  captions, texts, images and PiP kept out of the bottom 20% and right 15%. No cut may land
  inside a word, and no transition may overlap speech. Subtitles are always the last layer.
- **Log per agent call:** backend, turns, tokens, cost (when the CLI reports it), wall time.
- **Telegram limits:** ≤20 MB download / ≤50 MB upload on the hosted Bot API. Reply clearly
  when a file is too big, and re-encode outgoing files to fit. An optional local Bot API
  server is set with `TELEGRAM_API_BASE_URL`.
- Keep it small: Python 3.11+, uv, python-telegram-bot, SQLite, one asyncio worker.
  Commit only `.env.example`. Never commit `jobs/`, `.env` or media files.
- Tests must mock the agent backend; never call the claude/codex CLIs from tests.
  Run `uv run pytest`.
