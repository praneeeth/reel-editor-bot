# reel-editor-bot

A private Telegram bot that turns your raw clips into a vertical Instagram Reel / YouTube Short.
Send clips and an instruction ("cut the ums, punchy 60s reel, bold captions"). The bot plans
the edit and waits for your approval. It then sends a 720p preview, takes revisions, and
delivers a 1080x1920 @ 30fps export (max 180 s) with burned-in bold captions placed clear of
the Instagram/YouTube UI.

The editing is done by [video-use](https://github.com/browser-use/video-use) (MIT), driven by
a headless coding agent that runs on **your own** Claude Pro plan (`claude -p`) or ChatGPT plan
(`codex exec`). No paid LLM API is used. Your subscription must not power a product for other
people, so the bot answers only the Telegram user IDs in `ALLOWED_USER_IDS`.

## Architecture

```
Telegram ─► bot.py (handlers, ALLOWED_USER_IDS gate) ─► SQLite job state (states.py)
                                   │
                         worker.py: one asyncio queue, one job step at a time
                                   │
pipeline.py  PLAN    ffprobe + Scribe transcription (cached) + pack  → agent writes edit/plan.json
             EXECUTE agent: plan → edl.json → 720p preview → ≤10 timeline_view self-checks
             REVISE  FRESH agent session: project.md + edl.json + your feedback → new preview
             FINAL   no LLM: render.py re-renders the approved EDL at 1080x1920
agent/  AgentBackend: claude -p | codex exec (AGENT_BACKEND), logs turns/tokens/cost per step
render.py  video-use's helpers/render.py + 9:16 centre-crop + safe-zone ASS captions
```

video-use lives at `vendor/video-use` as a git submodule. At startup the bot links it into
`~/.claude/skills/video-use` or `~/.codex/skills/video-use`, as video-use's `install.md`
describes.

## Run on my laptop

### 1. Install the tools

**macOS** (Terminal):

```bash
brew install ffmpeg git
curl -LsSf https://astral.sh/uv/install.sh | sh
curl -fsSL https://claude.ai/install.sh | bash      # Claude Code
claude                                              # run once, /login with your Pro account, then /exit
npm install -g @openai/codex && codex login         # optional backup backend
```

**Windows** (PowerShell):

```powershell
winget install Gyan.FFmpeg Git.Git
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
irm https://claude.ai/install.ps1 | iex             # Claude Code (uses Git Bash from Git for Windows)
claude                                              # run once, /login with your Pro account, then /exit
npm install -g @openai/codex; codex login           # optional backup backend
```

Open a new terminal afterwards so `ffmpeg`, `uv` and `claude` are on your PATH. Check with
`ffmpeg -version`, `uv --version` and `claude --version`.

### 2. Get the code

```bash
git clone --recurse-submodules https://github.com/praneeeth/reel-editor-bot.git
cd reel-editor-bot
uv sync
```

(If you cloned without `--recurse-submodules`, run `git submodule update --init`.)

### 3. Get your keys

1. **Bot token:** in Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`,
   pick a name and username, then copy the token.
2. **Your user ID:** message [@userinfobot](https://t.me/userinfobot) and copy the number it
   replies with.
3. **ElevenLabs key** (for Scribe transcription): sign up at elevenlabs.io and create a key at
   <https://elevenlabs.io/app/settings/api-keys>.

### 4. Fill in `.env`

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

Set `TELEGRAM_BOT_TOKEN`, `ALLOWED_USER_IDS` (your ID; separate several with commas) and
`ELEVENLABS_API_KEY`. Leave `AGENT_BACKEND=claude` and `LEAN_MODE=true` as they are. To use
Codex instead, set `AGENT_BACKEND=codex`.

### 5. Start the bot

```bash
uv run python -m reelbot
```

Leave it running. The first start registers the video-use skill. On Windows without Developer
Mode, it creates a directory junction instead of a symlink.

### 6. Send a test clip

1. Record a 30–60 s talking clip on your phone. Say a few "um"s and leave some pauses.
2. In Telegram, open your bot and send `/start`.
3. Attach the clip with 📎 → **File**, not Gallery, so Telegram doesn't compress it. The file
   must be 20 MB or less. See the local Bot API server section below if you need more.
4. Send `cut the ums, punchy 30s reel, bold captions`.
5. Wait for the plan (a few minutes), then tap **Approve**. You'll get a 720p preview.
6. Reply `yellow captions` to test a revision, then tap **Final** for the 1080x1920 file.

`/status` shows progress, agent turns and estimated API cost. `/cancel` stops the current
job, and `/new` starts a fresh one. Job folders are deleted 24 h after delivery.

Every agent call's backend, turns, tokens, `total_cost_usd` (Claude only) and wall time are
stored in `jobs/reelbot.sqlite3`. Print them with:

```bash
uv run python -m reelbot metrics
```

To generate a synthetic test clip with fillers and pauses, install espeak-ng (macOS:
`brew install espeak-ng`), then run
`uv run python scripts/make_test_clip.py test_clip --no-transcript` and send
`test_clip/clip01.mp4`.

### Optional: local Bot API server (files up to 2000 MB)

1. Get `api_id`/`api_hash` at <https://my.telegram.org/apps> and put them in `.env` as
   `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`.
2. Log the bot out of the hosted API once:
   `https://api.telegram.org/bot<TOKEN>/logOut` (open it in a browser).
3. Run `docker compose --profile local-api up -d` (needs Docker Desktop).
4. Set `TELEGRAM_API_BASE_URL=http://localhost:8081` in `.env` and restart the bot.

### Tests

```bash
uv run pytest      # render tests need ffmpeg + espeak-ng; they are skipped otherwise
```
