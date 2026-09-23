"""`claude -p` backend (Claude Code logged in with a Pro/Max subscription)."""

from __future__ import annotations

import json
from pathlib import Path

from .base import AgentBackend, AgentError, AgentResult

ALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,Skill"


class ClaudeBackend(AgentBackend):
    name = "claude"
    exe = "claude"

    def available(self) -> bool:
        return self.which(self.exe) is not None

    def build_cmd(self, resume_session: str | None = None) -> list[str]:
        cmd = [self.which(self.exe) or self.exe, "-p", "--output-format", "json",
               "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED_TOOLS]
        if self.max_turns:
            cmd += ["--max-turns", str(self.max_turns)]
        if self.model:
            cmd += ["--model", self.model]
        for d in self.extra_dirs:
            cmd += ["--add-dir", str(d)]
        if resume_session:
            cmd += ["--resume", resume_session]
        return cmd

    async def run(self, prompt: str, cwd: Path, *,
                  resume_session: str | None = None) -> AgentResult:
        out = await self._exec(self.build_cmd(resume_session), prompt, cwd)
        result = self.parse(out.stdout, out.wall_s)
        if out.returncode != 0 and result.ok:
            result.ok = False
            result.error = f"exit code {out.returncode}"
        if not result.ok:
            stderr = out.stderr.strip()[-800:]
            result.error = "; ".join(x for x in (result.error, stderr) if x) or "unknown error"
            raise AgentError(f"claude -p failed: {result.error}", result)
        return result

    @staticmethod
    def parse(stdout: str, wall_s: float) -> AgentResult:
        """Parse `claude -p --output-format json` (a single JSON object)."""
        data: dict | None = None
        for line in reversed(stdout.strip().splitlines()):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
        if data is None:
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError:
                return AgentResult("claude", ok=False, wall_s=wall_s,
                                   error=f"unparseable output: {stdout[-500:]}")
        usage = data.get("usage") or {}
        subtype = data.get("subtype", "")
        ok = not data.get("is_error") and subtype in ("success", "")
        return AgentResult(
            backend="claude",
            ok=ok,
            text=data.get("result") or "",
            session_id=data.get("session_id"),
            turns=data.get("num_turns"),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_read_tokens=usage.get("cache_read_input_tokens"),
            cache_write_tokens=usage.get("cache_creation_input_tokens"),
            cost_usd=data.get("total_cost_usd"),
            wall_s=wall_s,
            error=None if ok else (subtype or "is_error"),
            raw=data,
        )
