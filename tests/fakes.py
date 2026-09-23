"""Test doubles: a scripted AgentBackend and a recording Notifier."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import clean_edl
from reelbot.agent import AgentBackend, AgentError, AgentResult


class FakeBackend(AgentBackend):
    """Plays the agent's part by writing the files each step is expected to produce."""

    name = "fake"

    def __init__(self, *, bad_edl: bool = False, fail_step: str | None = None):
        super().__init__()
        self.calls: list[dict] = []
        self.bad_edl = bad_edl
        self.fail_step = fail_step

    def available(self) -> bool:
        return True

    async def run(self, prompt, cwd: Path, *, resume_session=None) -> AgentResult:
        step = prompt.split("STEP = ", 1)[1].split()[0].rstrip(".")
        self.calls.append({"step": step, "prompt": prompt, "resume": resume_session})
        res = AgentResult("fake", ok=True, text=f"{step}_DONE", session_id=f"sess-{len(self.calls)}",
                          turns=5, input_tokens=100, output_tokens=40, cost_usd=0.01, wall_s=0.1)
        if step == self.fail_step:
            res.ok = False
            raise AgentError("scripted failure", res)
        edit = cwd / "edit"
        if step == "PLAN":
            (edit / "plan.json").write_text(json.dumps({
                "summary": "Cut the fillers, keep all three sentences.",
                "estimated_duration_s": 7.0,
                "beats": [{"beat": "HOOK", "quote": "Hey everyone"}],
                "assumptions": ["white captions"]}))
        elif step in ("EXECUTE", "REVISE"):
            edl = clean_edl(cwd, cwd / "clip01.mp4")
            if self.bad_edl:
                edl["ranges"][0]["end"] -= 0.3  # lands inside a word
            if step == "REVISE":
                edl["captions"] = {"color": "yellow"}
                edl["ranges"] = edl["ranges"][1:]
            (edit / "edl.json").write_text(json.dumps(edl))
            (edit / "result.json").write_text(json.dumps({"summary": f"{step} ok", "issues": []}))
        return res


class FakeNotifier:
    def __init__(self):
        self.events: list[tuple] = []

    async def status(self, job, text):
        self.events.append(("status", text))

    async def plan(self, job, plan):
        self.events.append(("plan", plan["summary"]))

    async def preview(self, job, path, summary):
        assert path.exists()
        self.events.append(("preview", path, summary))

    async def final(self, job, path, summary):
        assert path.exists()
        self.events.append(("final", path, summary))

    async def error(self, job, text):
        self.events.append(("error", text))

    def kinds(self):
        return [e[0] for e in self.events if e[0] != "status"]
