import json
import os
import subprocess
import sys

from prr import chat_tools
from prr.config import Config
from prr.util import read_json
from tests.test_merge import make_pr_dir


def cfg(can_post=True, can_approve=False) -> Config:
    c = Config()
    c.chat.can_post, c.chat.can_approve = can_post, can_approve
    return c


def names(c: Config) -> list[str]:
    return [t["name"] for t in chat_tools.tool_list(c)]


def test_switches_decide_which_tools_exist():
    assert names(cfg(can_post=False)) == ["add_pending_comment"]
    assert names(cfg()) == ["add_pending_comment", "post_comment", "post_review"]
    review = next(t for t in chat_tools.tool_list(cfg()) if t["name"] == "post_review")
    assert review["inputSchema"]["properties"]["event"]["enum"] == ["COMMENT"]
    review = next(t for t in chat_tools.tool_list(cfg(can_approve=True)) if t["name"] == "post_review")
    assert "APPROVE" in review["inputSchema"]["properties"]["event"]["enum"]


def test_pending_and_posted_comments(tmp_path, tricky_repo, monkeypatch):
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    make_pr_dir(tmp_path, tricky_repo["diff"])
    c = cfg()

    text, err = chat_tools.call_tool(tmp_path, c, "add_pending_comment", {"path": "src/core.py", "line": 21, "body": "check this"})
    assert not err and "not on GitHub yet" in text
    assert [d["source"] for d in read_json(tmp_path / "state.json")["drafts"]] == ["chat"]

    text, err = chat_tools.call_tool(tmp_path, c, "post_comment", {"path": "src/core.py", "start_line": 21, "line": 22, "body": "x"})
    assert not err and "https://github.com/dry-run/" in text
    assert read_json(tmp_path / "state.json")["submitted"][-1]["source"] == "chat"

    text, err = chat_tools.call_tool(tmp_path, c, "post_comment", {"path": "src/core.py", "line": 15, "body": "x"})
    assert err and "outside the diff" in text and "pending" in text   # off-diff lines can't be posted

    text, err = chat_tools.call_tool(tmp_path, c, "post_review", {"body": "Looks good", "include_pending": True})
    assert not err and "1 line comments" in text
    assert read_json(tmp_path / "state.json")["drafts"] == []           # the pending comment went with it


def test_refusals(tmp_path, tricky_repo, monkeypatch):
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    make_pr_dir(tmp_path, tricky_repo["diff"])
    text, err = chat_tools.call_tool(tmp_path, cfg(), "post_review", {"body": "ok", "event": "APPROVE"})
    assert err and "turned off" in text
    text, err = chat_tools.call_tool(tmp_path, cfg(can_post=False), "post_comment", {"path": "src/core.py", "line": 21, "body": "x"})
    assert err and "turned off" in text
    text, err = chat_tools.call_tool(tmp_path, cfg(), "post_comment", {"path": "nope.py", "line": 1, "body": "x"})
    assert err and "not part of this PR" in text


def test_mcp_server_speaks_the_protocol(tmp_path, tricky_repo):
    make_pr_dir(tmp_path, tricky_repo["diff"])
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "add_pending_comment", "arguments": {"path": "src/core.py", "line": 5, "body": "hi"}}},
    ]
    proc = subprocess.run([sys.executable, "-P", "-m", "prr", "_mcp", str(tmp_path)], input="".join(json.dumps(m) + "\n" for m in msgs),
                          capture_output=True, text=True, timeout=60, env={**os.environ, "PRR_GITHUB_DRY_RUN": "1"})
    replies = [json.loads(line) for line in proc.stdout.splitlines()]
    assert [r["id"] for r in replies] == [1, 2, 3]                       # no reply to the notification
    assert replies[0]["result"]["capabilities"] == {"tools": {}}
    assert "post_comment" in [t["name"] for t in replies[1]["result"]["tools"]]
    assert replies[2]["result"]["isError"] is False and "pending comment" in replies[2]["result"]["content"][0]["text"]
