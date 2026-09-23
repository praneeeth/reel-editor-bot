from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


class AgentError(RuntimeError):
    def __init__(self, message: str, result: "AgentResult | None" = None):
        super().__init__(message)
        self.result = result


@dataclass
class AgentResult:
    """One headless agent call, with whatever usage the CLI reported."""

    backend: str
    ok: bool
    text: str = ""
    session_id: str | None = None
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    cost_usd: float | None = None
    wall_s: float = 0.0
    error: str | None = None
    raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ProcOutput:
    returncode: int
    stdout: str
    stderr: str
    wall_s: float


class AgentBackend(ABC):
    """Runs one prompt to completion in ``cwd`` and returns usage metrics.

    ``resume_session`` continues an earlier session (only used when
    LEAN_MODE=false; lean revisions always start fresh).
    """

    name: str = "base"

    def __init__(self, *, model: str = "", timeout_s: int = 1800, max_turns: int = 60,
                 extra_dirs: list[Path] | None = None, env: dict[str, str] | None = None):
        self.model = model
        self.timeout_s = timeout_s
        self.max_turns = max_turns
        self.extra_dirs = extra_dirs or []
        self.env = env or {}

    @abstractmethod
    async def run(self, prompt: str, cwd: Path, *,
                  resume_session: str | None = None) -> AgentResult: ...

    @abstractmethod
    def available(self) -> bool: ...

    # ---- subprocess plumbing shared by the CLI backends ---------------------------
    @staticmethod
    def which(exe: str) -> str | None:
        return shutil.which(exe)

    async def _exec(self, cmd: list[str], prompt: str, cwd: Path) -> ProcOutput:
        """Run ``cmd`` with the prompt on stdin (avoids Windows' 8k command-line limit).

        Kills the whole process tree on timeout or cancellation (/cancel).
        """
        env = {**os.environ, **self.env, "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONIOENCODING": "utf-8"}
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        t0 = time.monotonic()
        log.info("agent exec: %s (cwd=%s)", " ".join(cmd[:3]), cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd), env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, **kwargs,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), self.timeout_s)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            _kill_tree(proc)
            await proc.wait()
            if isinstance(exc, asyncio.TimeoutError):
                raise AgentError(f"{self.name} timed out after {self.timeout_s}s") from None
            raise
        return ProcOutput(proc.returncode or 0, out.decode("utf-8", "replace"),
                          err.decode("utf-8", "replace"), time.monotonic() - t0)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True, check=False)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()
