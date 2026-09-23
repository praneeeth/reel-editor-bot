import asyncio
import json
import sys
import textwrap

import pytest

from reelbot.agent import AgentError, ClaudeBackend, CodexBackend, make_backend
from reelbot.skill import ensure_skill_registered

CLAUDE_JSON = {
    "type": "result", "subtype": "success", "is_error": False, "duration_ms": 61000,
    "num_turns": 14, "result": "PLAN_DONE", "session_id": "abc-123",
    "total_cost_usd": 0.4231,
    "usage": {"input_tokens": 1200, "cache_creation_input_tokens": 9000,
              "cache_read_input_tokens": 150000, "output_tokens": 4100},
}

CODEX_JSONL = "\n".join(json.dumps(e) for e in [
    {"type": "thread.started", "thread_id": "th_1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "ls"}},
    {"type": "item.completed", "item": {"type": "command_execution", "command": "cat x"}},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "PLAN_DONE"}},
    {"type": "turn.completed", "usage": {"input_tokens": 5000, "cached_input_tokens": 3000,
                                         "output_tokens": 700}},
])


def test_claude_parse_usage():
    r = ClaudeBackend.parse(json.dumps(CLAUDE_JSON), 61.0)
    assert r.ok and r.turns == 14 and r.cost_usd == 0.4231 and r.session_id == "abc-123"
    assert (r.input_tokens, r.output_tokens, r.cache_read_tokens, r.cache_write_tokens) == \
        (1200, 4100, 150000, 9000)


def test_claude_parse_error_subtype():
    r = ClaudeBackend.parse(json.dumps({**CLAUDE_JSON, "subtype": "error_max_turns",
                                        "is_error": True}), 1.0)
    assert not r.ok and r.error == "error_max_turns"


def test_codex_parse_usage():
    r = CodexBackend.parse(CODEX_JSONL, 30.0)
    assert r.ok and r.session_id == "th_1" and r.text == "PLAN_DONE"
    assert r.turns == 3 and r.input_tokens == 5000 and r.cache_read_tokens == 3000
    assert r.cost_usd is None


def test_command_lines():
    c = ClaudeBackend(model="sonnet", max_turns=40).build_cmd("sess")
    assert c[1:4] == ["-p", "--output-format", "json"]
    assert "--resume" in c and "sess" in c and "--max-turns" in c and "sonnet" in c
    x = CodexBackend().build_cmd("th_1")
    assert x[1:4] == ["exec", "resume", "th_1"] and x[-1] == "-" and "--json" in x


def test_make_backend():
    assert make_backend("claude").name == "claude"
    assert make_backend("codex").name == "codex"
    with pytest.raises(ValueError):
        make_backend("gpt")


def _fake_cli(tmp_path, body):
    script = tmp_path / "fakecli.py"
    script.write_text(textwrap.dedent(body))
    return [sys.executable, str(script)]


async def test_exec_passes_prompt_on_stdin_and_parses(tmp_path):
    b = ClaudeBackend()
    b.build_cmd = lambda resume=None: _fake_cli(tmp_path, f"""
        import sys, json
        prompt = sys.stdin.read()
        d = {CLAUDE_JSON!r}
        d["result"] = "got:" + prompt
        print(json.dumps(d))
    """)
    r = await b.run("hello agent", tmp_path)
    assert r.text == "got:hello agent" and r.turns == 14


async def test_exec_failure_raises_with_result(tmp_path):
    b = ClaudeBackend()
    b.build_cmd = lambda resume=None: _fake_cli(tmp_path, "import sys; sys.stdin.read(); "
                                                "print('boom', file=sys.stderr); sys.exit(3)")
    with pytest.raises(AgentError, match="boom"):
        await b.run("x", tmp_path)


async def test_exec_timeout_and_cancel_kill_process(tmp_path):
    body = "import sys, time; sys.stdin.read(); time.sleep(30)"
    b = ClaudeBackend(timeout_s=1)
    b.build_cmd = lambda resume=None: _fake_cli(tmp_path, body)
    with pytest.raises(AgentError, match="timed out"):
        await b.run("x", tmp_path)

    b2 = ClaudeBackend(timeout_s=60)
    b2.build_cmd = lambda resume=None: _fake_cli(tmp_path, body)
    task = asyncio.create_task(b2.run("x", tmp_path))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_skill_registration(tmp_path):
    src = tmp_path / "video-use"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: video-use\n---\n")
    link = ensure_skill_registered("claude", home=tmp_path / "home", source=src)
    assert link == tmp_path / "home" / ".claude" / "skills" / "video-use"
    assert (link / "SKILL.md").exists()
    # idempotent
    assert ensure_skill_registered("claude", home=tmp_path / "home", source=src) == link
    codex = ensure_skill_registered("codex", home=tmp_path / "home", source=src)
    assert (codex / "SKILL.md").exists() and ".codex" in str(codex)
