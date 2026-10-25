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
    assert names(cfg(can_post=False)) == ["approve", "add_pending_comment"]
    assert names(cfg()) == ["approve", "add_pending_comment", "post_comment", "post_review"]
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


def test_approval_rules(tmp_path, tricky_repo, monkeypatch):
    from prr import policy
    from prr.util import write_json

    ctx = make_pr_dir(tmp_path, tricky_repo["diff"])
    (tmp_path / "wt").mkdir()
    ctx["worktree"], ctx["pr"]["author_association"] = str(tmp_path / "wt"), "MEMBER"
    write_json(tmp_path / "context.json", ctx)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    def decide(tool: str, inp: dict | None = None, **kw) -> str:
        return policy.decide(tool, inp or {}, pr_dir=tmp_path, thread_id="t1", **kw)["decision"]

    # read-only connector lookups run without asking (on a teammate's PR); actions that change things are asked about
    assert decide("mcp__claude_ai_Atlassian__getJiraIssue") == "allow"
    assert decide("mcp__claude_ai_Slack__slack_read_channel") == "allow"
    assert decide("mcp__claude_ai_Atlassian__createJiraIssue") == "ask"
    assert decide("mcp__claude_ai_Slack__slack_send_message") == "ask"
    assert decide("Bash", {"command": "ls"}) == "ask"                      # the approval tool can't see the shell's folder
    assert decide("Bash", {"command": "awk 'NR==5{print}' a.py | head"}, cwd=ctx["worktree"]) == "allow"   # the hook can
    assert policy.approval_keys("Bash", {"command": "npm test -- --watch"}, sc=policy.scope(tmp_path),
                                cwd=ctx["worktree"]) == ["Bash:npm test"]

    lookup = {"tool_name": "mcp__claude_ai_Atlassian__getJiraIssue", "input": {"issueIdOrKey": "ATI-1"}}
    assert chat_tools.approve(tmp_path, lookup)["behavior"] == "allow"

    # nobody to ask (no running app) → denied, with a reason Claude can relay
    run = {"tool_name": "Bash", "input": {"command": "touch x"}}
    denied = chat_tools.approve(tmp_path, run)
    assert denied["behavior"] == "deny" and "not running" in denied["message"]

    # "Always allow" remembered for this chat → allowed without asking
    from prr.util import write_json
    write_json(tmp_path / "state.json", {"threads": [{"id": "t1", "always_allow": ["Bash:touch x"]}]})
    monkeypatch.setenv("PRR_THREAD_ID", "t1")
    monkeypatch.setattr(policy, "turn_causes", lambda pr_dir, tid: [])     # nothing marked this turn
    assert chat_tools.approve(tmp_path, run)["behavior"] == "allow"
    # …but only for that command, not every shell command
    assert chat_tools.approve(tmp_path, {"tool_name": "Bash", "input": {"command": "rm -rf build"}})["behavior"] == "deny"
