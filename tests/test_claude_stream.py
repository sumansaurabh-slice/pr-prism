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
