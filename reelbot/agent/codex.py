"""`codex exec` backend (Codex CLI logged in with a ChatGPT plan)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from .base import AgentBackend, AgentError, AgentResult

TOOL_ITEMS = {"command_execution", "file_change", "mcp_tool_call", "web_search"}


class CodexBackend(AgentBackend):
    name = "codex"
    exe = "codex"

    def available(self) -> bool:
        return self.which(self.exe) is not None

    def build_cmd(self, resume_session: str | None = None) -> list[str]:
        sandbox = os.environ.get("CODEX_SANDBOX", "workspace-write")
        cmd = [self.which(self.exe) or self.exe, "exec"]
        if resume_session:
            cmd += ["resume", resume_session]
        cmd += ["--json", "--skip-git-repo-check", "--sandbox", sandbox]
        if self.model:
            cmd += ["--model", self.model]
        cmd.append("-")  # read the prompt from stdin
        return cmd

    async def run(self, prompt: str, cwd: Path, *,
                  resume_session: str | None = None) -> AgentResult:
        out = await self._exec(self.build_cmd(resume_session), prompt, cwd)
        result = self.parse(out.stdout, out.wall_s)
        if out.returncode != 0:
            result.ok = False
            result.error = result.error or out.stderr.strip()[-800:] or f"exit {out.returncode}"
        if not result.ok:
            raise AgentError(f"codex exec failed: {result.error}", result)
        return result

    @staticmethod
    def parse(stdout: str, wall_s: float) -> AgentResult:
        """Parse `codex exec --json` JSONL events.

        Codex reports tokens per turn (`turn.completed.usage`) but no cost. We
        count one "turn" per tool call plus the final answer, which is roughly
        comparable to Claude's num_turns.
        """
        res = AgentResult("codex", ok=True, wall_s=wall_s, turns=0, input_tokens=0,
                          output_tokens=0, cache_read_tokens=0)
        messages: list[str] = []
        events = 0
        for line in stdout.splitlines():
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            events += 1
            typ = ev.get("type", "")
            if typ == "thread.started":
                res.session_id = ev.get("thread_id")
            elif typ == "item.completed":
                item = ev.get("item") or {}
                if item.get("type") in TOOL_ITEMS:
                    res.turns += 1
                elif item.get("type") == "agent_message" and item.get("text"):
                    messages.append(item["text"])
            elif typ == "turn.completed":
                u = ev.get("usage") or {}
                res.input_tokens += int(u.get("input_tokens") or 0)
                res.cache_read_tokens += int(u.get("cached_input_tokens") or 0)
                res.output_tokens += int(u.get("output_tokens") or 0)
                res.turns += 1
            elif typ in ("turn.failed", "error"):
                res.ok = False
                err = ev.get("error") or {}
                res.error = (err.get("message") if isinstance(err, dict) else None) \
                    or ev.get("message") or typ
        if events == 0:
            res.ok, res.error = False, f"no JSON events in output: {stdout[-300:]}"
        res.text = messages[-1] if messages else ""
        return res
