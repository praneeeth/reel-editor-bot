"""Prompts for the headless PLAN / EXECUTE / REVISE agent calls.

video-use is written for an interactive conversation (ask -> confirm ->
execute -> iterate). The bot splits it into separate headless calls; the
Telegram [Approve] button stands in for Hard Rule 11's strategy confirmation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import VIDEO_USE_DIR
from .config import MAX_OUTPUT_SECONDS


def _p(path: Path | str) -> str:
    # Forward slashes work in bash (incl. Git Bash on Windows) and in Python.
    return Path(path).as_posix()


def _common(job_dir: Path, *, lean: bool, eval_cap: int) -> str:
    py = _p(sys.executable)
    skill = _p(VIDEO_USE_DIR)
    lean_rules = (
        f"- LEAN MODE: no animation overlays (no Remotion/HyperFrames/Manim/PIL cards; "
        f"'overlays' must be []). At most {eval_cap} timeline_view screenshot checks in this "
        f"call. Keep the number of tool calls low; don't re-read files you already have.\n"
        if lean else
        f"- Animation overlays are allowed if the request calls for them (Hard Rule 10). "
        f"At most {eval_cap} timeline_view checks per self-eval pass.\n"
    )
    return f"""You are running HEADLESS as one step of a Telegram bot that edits clips into vertical
Instagram Reels / YouTube Shorts. There is no human in this session: never ask questions or wait
for confirmation - make the most reasonable choice and note it.

Use the video-use skill: read {skill}/SKILL.md and obey its Hard Rules.
- Helpers live in {skill}/helpers/ - run them with `"{py}" {skill}/helpers/<name>.py ...`.
- videos_dir (your working directory) = {_p(job_dir)}; every output goes in {_p(job_dir)}/edit/
  (Hard Rule 12 - never write inside {skill}).
- Transcripts are already cached in edit/transcripts/ and packed in edit/takes_packed.md.
  NEVER re-transcribe (Hard Rule 9).
- Delivery spec (fixed, handled by the bot's renderer): 1080x1920 @ 30fps, total length
  <= {MAX_OUTPUT_SECONDS:.0f}s, landscape sources are centre-cropped to 9:16, bold-overlay
  captions (2-word UPPERCASE) are burned in automatically and kept clear of the platform UI
  (bottom 20%, right 15%). Do not call video-use's render.py directly - render with:
    "{py}" -m reelbot.render edit/edl.json -o edit/preview.mp4 --preview{' --no-overlays' if lean else ''}
  It validates the EDL (<= {MAX_OUTPUT_SECONDS:.0f}s, no cut inside a word) and prints what to fix.
{lean_rules}"""


EDL_SCHEMA = """edit/edl.json follows the SKILL.md "EDL format" (use absolute source paths from
edit/inventory.json, source names = file stems), plus an optional "captions" object:
  "captions": {"enabled": true, "color": "white" | "yellow" | "#RRGGBB", "font_size": 88}
font_size is in 1080x1920 pixels (48-130). Use "grade": "none" unless a grade was requested."""


def plan_prompt(job_dir: Path, instruction: str, *, lean: bool, eval_cap: int,
                previous_plan: dict | None = None, feedback: str | None = None) -> str:
    inventory = (job_dir / "edit" / "inventory.json").read_text()
    redo = ""
    if previous_plan and feedback:
        redo = f"""
This is a RE-PLAN. The user rejected the previous plan:
{json.dumps(previous_plan, indent=2)}
User's requested changes: \"\"\"{feedback}\"\"\"
"""
    return f"""{_common(job_dir, lean=lean, eval_cap=min(eval_cap, 2))}
STEP = PLAN (video-use process steps 1-4: inventory, pre-scan, propose strategy).
Do NOT write edl.json and do NOT render anything in this step.

User's request: \"\"\"{instruction}\"\"\"
{redo}
Source inventory (edit/inventory.json):
{inventory}

Read edit/takes_packed.md, pre-scan for slips/fillers, decide the structure, then write
edit/plan.json exactly in this shape:
{{
  "summary": "4-8 plain-English sentences: shape, take choices, cut direction, captions, length",
  "estimated_duration_s": 42.0,
  "beats": [{{"source": "clip01", "start": 1.23, "end": 4.56, "beat": "HOOK", "quote": "..."}}],
  "captions": {{"enabled": true, "color": "white", "font_size": 88}},
  "assumptions": ["anything you assumed instead of asking"]
}}
Beats must use word-boundary times from the transcript; total must be <= {MAX_OUTPUT_SECONDS:.0f}s.
Then append a "## Session 1 - <date>" section (Strategy / Decisions / Outstanding) to
edit/project.md. Reply with one line: PLAN_DONE <estimated seconds>.
"""


def execute_prompt(job_dir: Path, *, lean: bool, eval_cap: int) -> str:
    plan = (job_dir / "edit" / "plan.json").read_text()
    return f"""{_common(job_dir, lean=lean, eval_cap=eval_cap)}
STEP = EXECUTE. The user APPROVED this plan in Telegram (this satisfies Hard Rule 11):
{plan}

1. Turn the plan into edit/edl.json. Snap every cut to word boundaries from
   edit/transcripts/*.json (Hard Rule 6) and pad edges 30-200 ms (Hard Rule 7).
   {EDL_SCHEMA}
2. Render the 720p preview with the reelbot.render command above. If it rejects the EDL, fix
   edl.json and re-run.
3. Self-eval (process step 7) on edit/preview.mp4: run timeline_view on the RENDERED preview
   around cut boundaries, write PNGs to edit/verify/, look at them; ffprobe the duration.
   Budget: at most {eval_cap} timeline_view calls and at most 3 render passes in total.
   Flag remaining issues instead of looping.
4. Append what you did to edit/project.md.
5. Write edit/result.json: {{"summary": "1-3 sentences for the user", "duration_s": 0.0,
   "issues": ["anything the self-eval could not fix"]}}
Reply with one line: EXECUTE_DONE.
"""


def revise_prompt(job_dir: Path, feedback: str, *, lean: bool, eval_cap: int,
                  resumed: bool = False) -> str:
    edit = job_dir / "edit"
    task = f"""STEP = REVISE. The user watched the preview and asked for changes:
\"\"\"{feedback}\"\"\"

1. Edit edit/edl.json to apply the feedback (re-cut from edit/takes_packed.md and
   edit/transcripts/*.json if needed; keep Hard Rules 6/7). Caption colour/size live in
   the "captions" object. {EDL_SCHEMA}
2. Re-render the preview with the reelbot.render command and self-eval it:
   at most {eval_cap} timeline_view calls, at most 3 render passes.
3. Append a short "Revision" entry to edit/project.md.
4. Write edit/result.json: {{"summary": "what changed, 1-3 sentences", "duration_s": 0.0,
   "issues": []}}
Reply with one line: REVISE_DONE.
"""
    if resumed:  # non-lean: the session already has the full history
        return task
    project = (edit / "project.md").read_text() if (edit / "project.md").exists() else ""
    if len(project) > 4000:
        project = "...\n" + project[-4000:]
    edl = (edit / "edl.json").read_text()
    return f"""{_common(job_dir, lean=lean, eval_cap=eval_cap)}
This is a FRESH session. Summary of the project so far (edit/project.md):
{project or "(empty)"}

Current edit/edl.json (approved and previewed):
{edl}

{task}"""
