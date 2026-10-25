import json
import os
from pathlib import Path

from prr.claude import StreamTranslator, base_argv
from prr.gitgh import claude_env


def test_translates_stream_json_events():
    t = StreamTranslator(cwd=Path("/wt"))
    events = []
    for obj in [
        {"type": "system", "subtype": "init", "session_id": "s"},
        {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}},
        {"type": "stream_event", "event": {"type": "content_block_start", "content_block": {"type": "thinking"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hel"}}},
        {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "lo"}}},
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "text", "text": "Hello"},  # already streamed → not repeated
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/wt/pkg/a.go", "offset": 10}},
        ]}},
        {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "late"}]}},  # never streamed
        {"type": "result", "subtype": "success", "session_id": "s", "total_cost_usd": 0.01, "duration_ms": 5},
    ]:
        events += t.feed(obj)
    assert events == [
        {"type": "thinking"},
        {"type": "text", "text": "Hel"},
        {"type": "text", "text": "lo"},
        {"type": "tool", "name": "Read", "summary": "pkg/a.go (from line 10)"},
        {"type": "text", "text": "late"},
        {"type": "result", "session_id": "s", "cost_usd": 0.01, "duration_ms": 5},
    ]


def test_error_result():
    assert StreamTranslator().feed({"type": "result", "subtype": "error_max_turns", "is_error": True})[0]["type"] == "error"


def test_argv_is_locked_down():
    argv = base_argv("opus", ["Read", "Grep", "Glob"])
    for flag in ("--setting-sources", "--strict-mcp-config", "--permission-mode"):
        assert flag in argv
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert "Bash" not in " ".join(argv)


def test_nested_session_env_is_scrubbed(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "x")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    env = claude_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_SESSION_ID" not in env
    assert env["CLAUDE_CODE_USE_BEDROCK"] == "1" and env["ANTHROPIC_API_KEY"] == "k"


def _allowed(argv: list[str]) -> list[str]:
    i = argv.index("--allowedTools") + 1
    out = []
    while i < len(argv) and not argv[i].startswith("--"):
        out.append(argv[i])
        i += 1
    return out


def test_chat_is_full_claude_code_with_approvals(tmp_path):
    from prr.claude import APPROVAL_TOOL, chat_argv
    wt, pr = tmp_path / "pr" / "worktree", tmp_path / "pr"
    wt.mkdir(parents=True)
    argv = chat_argv("opus", mcp_config="{}", system_file=Path("/s.md"), add_dirs=[pr], ask_first=True,
                     session_id="sid", resume=False, roots=[wt, pr], pr_dir=pr)
    assert "--tools" not in argv and "--strict-mcp-config" not in argv     # every tool and the reviewer's connectors
    assert argv[argv.index("--setting-sources") + 1] == "user"             # never the PR's own settings or hooks
    assert argv[argv.index("--permission-prompt-tool") + 1] == APPROVAL_TOOL
    assert argv[argv.index("--permission-mode") + 1] == "default"
    allowed = _allowed(argv)
    # no bare read tools and no shell rules: reads are scoped to the roots, and the hook decides every command
    assert not {"Read", "Grep", "Glob", "LS", "NotebookRead"} & set(allowed)
    assert not any(a.startswith("Bash") for a in allowed) and "mcp__prprism" not in allowed
    assert "mcp__codegraph" not in allowed and "mcp__prprism__post_comment" not in allowed
    real = os.path.realpath(wt)
    assert f"Read(/{real}/**)" in allowed and "mcp__prprism__add_pending_comment" in allowed
    assert f"Read(/{os.path.realpath(pr)}/server.json)" in argv[argv.index("--disallowedTools") + 1:]

    hooks = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"]
    assert [h["matcher"] for h in hooks] == ["Bash|Read|Grep|Glob|LS|NotebookRead", "mcp__.*", "WebFetch|WebSearch"]
    command = hooks[0]["hooks"][0]["command"]
    assert " -P -m prr.policy" in command and "--bypass" not in command and hooks[0]["hooks"][0]["timeout"] == 960

    no_ask = chat_argv("opus", mcp_config="{}", system_file=Path("/s.md"), add_dirs=[], ask_first=False,
                       session_id="sid", resume=True, roots=[wt], pr_dir=pr)
    assert no_ask[no_ask.index("--permission-mode") + 1] == "bypassPermissions" and "--resume" in no_ask
    denied = no_ask[no_ask.index("--disallowedTools") + 1:]
    assert f"Read(/{os.path.realpath(os.path.expanduser('~/.ssh'))}/**)" in denied
    assert "--bypass" in json.loads(no_ask[no_ask.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


def test_analysis_reads_are_scoped_to_the_worktree(tmp_path):
    argv = base_argv("opus", ["Read", "Grep", "Glob"], cwd=tmp_path)
    allowed = _allowed(argv)
    assert allowed == [f"Read(/{os.path.realpath(tmp_path)}/**)"]
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
