"""Headless coding-agent backends (claude -p / codex exec) behind one interface."""

from .base import AgentBackend, AgentError, AgentResult
from .claude import ClaudeBackend
from .codex import CodexBackend


def make_backend(name: str, **kwargs) -> AgentBackend:
    if name == "claude":
        return ClaudeBackend(**kwargs)
    if name == "codex":
        return CodexBackend(**kwargs)
    raise ValueError(f"unknown AGENT_BACKEND {name!r}")


__all__ = ["AgentBackend", "AgentError", "AgentResult", "ClaudeBackend", "CodexBackend",
           "make_backend"]
