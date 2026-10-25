"""prr/policy.py: what the chat may do without a card (E1, E1b, D19, eng D2/D4/D9/D10), and the server's cards."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from prr import chat_tools, policy
from prr.util import write_json

LOOKUP = "mcp__claude_ai_Atlassian__getJiraIssue"
SLACK_SEND = "mcp__claude_ai_Slack__slack_send_message"
POST = "mcp__prprism__post_comment"
HOME = os.path.realpath(os.path.expanduser("~"))


def make_pr(tmp_path: Path, association: str | None = "MEMBER") -> Path:
    pr = tmp_path / "pr"
    wt = pr / "worktree"
    (wt / "src").mkdir(parents=True)
    (wt / "src" / "a.py").write_text("x = 1\n")
    (pr / "diff.patch").write_text("")
    (tmp_path / "clone").mkdir()
    write_json(pr / "context.json", {
        "repo_root": str(tmp_path / "clone"), "worktree": str(wt), "files": [],
        "pr": {"owner": "o", "repo": "r", "number": 1, "author": "alice", "author_association": association}})
    return pr


@pytest.fixture()
def pr(tmp_path, monkeypatch):
    for var in ("PRR_ROOTS", "PRR_PR_DIR", "PRR_THREAD_ID"):
        monkeypatch.delenv(var, raising=False)
    return make_pr(tmp_path)


def hook(tool: str, inp: dict, pr: Path, **kw) -> dict:
    """What the PreToolUse hook does for this call (in-process)."""
    return policy.resolve(tool, inp, pr_dir=pr, thread_id="t1", cwd=str(pr / "worktree"), hook=True, **kw)


def ask_tool(tool: str, inp: dict, pr: Path) -> dict:
    """What the approval tool (chat_tools.approve) does for this call."""
    os.environ["PRR_THREAD_ID"] = "t1"
    try:
        return chat_tools.approve(pr, {"tool_name": tool, "input": inp})
    finally:
        del os.environ["PRR_THREAD_ID"]


def remember(pr: Path, *keys: str) -> None:
    write_json(pr / "state.json", {"threads": [{"id": "t1", "always_allow": list(keys)}]})


# --------------------------------------------------------------------------------------
# Reads (E1, eng D4)
# --------------------------------------------------------------------------------------


def test_reads_inside_the_pr_run_and_outside_reads_ask(pr, tmp_path, review_stub):
    stub = review_stub(pr)
    wt = pr / "worktree"
    for tool, inp in [("Read", {"file_path": str(wt / "src/a.py")}), ("Read", {"file_path": str(pr / "diff.patch")}),
                      ("Grep", {"pattern": "x", "path": "src"}), ("Glob", {"pattern": "**/*.py"}),
                      ("Glob", {"pattern": "src/{a,b}.py"})]:
        assert hook(tool, inp, pr)["decision"] == "allow", (tool, inp)
    assert stub.approvals == []

    outside = [
        ("Read", {"file_path": os.path.expanduser("~/.config/gh/hosts.yml")}),
        ("Read", {"file_path": str(pr / "server.json")}),
        ("Read", {"file_path": str(wt / "../../x")}),
        ("Grep", {"pattern": "x", "path": str(tmp_path)}),
        ("Grep", {"pattern": "x", "path": "src", "glob": "../../../*"}),
        ("Glob", {"pattern": "/Users/**"}),
        ("Glob", {"pattern": "../../**"}),
        ("Glob", {"pattern": "{src,/etc}/*"}),
        ("LS", {"path": "/"}),
        ("NotebookRead", {"notebook_path": str(tmp_path / "n.ipynb")}),
    ]
    for tool, inp in outside:
        out = hook(tool, inp, pr)
        assert out["decision"] == "deny" and "said no" in out["message"], (tool, inp)
        assert stub.approvals[-1]["tool_name"] == tool and stub.approvals[-1]["reason"]["kind"] == "outside_read"
    assert len(stub.approvals) == len(outside)


def test_always_allow_for_reads_is_per_folder(pr, tmp_path, review_stub):
    stub = review_stub(pr)
    (tmp_path / "other" / "sub").mkdir(parents=True)
    read = {"file_path": str(tmp_path / "other" / "sub" / "x.txt")}
    sc = policy.scope(pr)
    assert policy.approval_keys("Read", read, sc=sc, cwd=str(pr)) == [f"Read:{os.path.realpath(tmp_path / 'other/sub')}"]

    remember(pr, f"Read:{os.path.realpath(tmp_path / 'other')}")
    assert hook("Read", read, pr)["decision"] == "allow"                       # the folder and below
    assert hook("Grep", {"pattern": "x", "path": str(tmp_path / "other")}, pr)["decision"] == "allow"
    assert stub.approvals == [] and stub.taints[-1]["kind"] == "outside_read"  # an allowed outside read marks the turn
    for sibling_or_parent in (tmp_path / "another" / "x.txt", tmp_path / "x.txt"):
        assert hook("Read", {"file_path": str(sibling_or_parent)}, pr)["decision"] == "deny"
    assert len(stub.approvals) == 2
    # a hook-approved outside read, then a post in the same turn: a card (SC-TAINT)
    assert ask_tool(POST, {"path": "src/a.py", "line": 1, "body": "x"}, pr)["behavior"] == "deny"
    assert stub.approvals[-1]["reason"]["kind"] == "marked_turn"


@pytest.mark.parametrize("path", ["/x.txt", "~/x.txt", "~/.ssh/id_rsa", "~/.config/gh/hosts.yml", "~/.aws/credentials",
                                  "~/.kube/config", "~/.docker/config.json", "~/.config/app.json", "~/.ssh/keys/k"])
def test_no_folder_key_for_root_home_or_secrets(pr, path):
    keys = policy.approval_keys("Read", {"file_path": os.path.expanduser(path)}, sc=policy.scope(pr), cwd=str(pr))
    assert keys == []
    assert policy.always_text("Read", keys) == ("", "")


def test_no_folder_key_for_the_folder_holding_the_token(pr):
    sc = policy.scope(pr)
    assert policy.approval_keys("Read", {"file_path": str(pr / "server.json")}, sc=sc, cwd=str(pr)) == []
    assert policy.approval_keys("Grep", {"pattern": "t", "path": str(pr.parent)}, sc=sc, cwd=str(pr)) == []


def test_folder_labels_are_short(pr):
    label, tip = policy.always_text("Read", [f"Read:{HOME}/code/billing"])
    assert label == "Always allow reads in ~/code/billing" and tip.endswith(f"{HOME}/code/billing")
    long = policy.show_folder(f"{HOME}/code/some/very/deep/folder/structure/here")
    assert long.startswith("~/code/") and "…" in long and len(long) <= 32
    assert policy.always_text(LOOKUP, [LOOKUP])[0] == "Always allow Atlassian: getJiraIssue in this chat"


# --------------------------------------------------------------------------------------
# Shell commands (E1, eng D2)
# --------------------------------------------------------------------------------------


def test_shell_commands_through_the_hook(pr, review_stub, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    stub = review_stub(pr)
    assert hook("Bash", {"command": "git log -- src/a.py | head"}, pr)["decision"] == "allow"
    assert stub.approvals == []

    assert hook("Bash", {"command": "cat ~/.config/gh/hosts.yml"}, pr)["decision"] == "deny"
    assert stub.approvals[-1]["reason"]["kind"] == "outside_read"
    assert hook("Bash", {"command": "jq -n env"}, pr)["decision"] == "deny"
    assert stub.approvals[-1]["reason"]["kind"] == "env"
    assert hook("Bash", {"command": "npm test"}, pr)["decision"] == "deny"
    assert stub.approvals[-1]["reason"]["kind"] == "action" and stub.approvals[-1]["cwd"] == str(pr / "worktree")

    remember(pr, "Bash:npm test")
    assert hook("Bash", {"command": "npm test -- --watch"}, pr)["decision"] == "allow"
    assert stub.taints[-1]["kind"] == "approved"                  # an allowed action marks the turn (eng D9)
    assert hook("Bash", {"command": "rm -rf build"}, pr)["decision"] == "deny"    # only that program
    assert len(stub.approvals) == 4


def test_the_approval_tool_never_auto_allows_a_shell_command(pr, review_stub, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    stub = review_stub(pr)
    out = ask_tool("Bash", {"command": "ls"}, pr)       # only the hook knows the shell's folder
    assert out["behavior"] == "deny" and stub.approvals[-1]["tool_name"] == "Bash"


def test_bypass_mode_denies_only_outside_reads_and_env_dumps(pr, review_stub):
    stub = review_stub(pr)

    def bypass(tool, inp):
        return hook(tool, inp, pr, ask_first=False)

    assert bypass("Bash", {"command": "cat /etc/passwd"})["decision"] == "deny"
    assert "outside this PR" in bypass("Bash", {"command": "cat /etc/passwd"})["message"]
    assert bypass("Bash", {"command": "printenv"})["decision"] == "deny"
    assert bypass("Bash", {"command": "npm install && rm -rf build"})["decision"] == "pass"
    assert bypass("Read", {"file_path": "/etc/hosts"})["decision"] == "deny"
    assert bypass("Read", {"file_path": str(pr / "worktree/src/a.py")})["decision"] == "allow"
    assert bypass("WebFetch", {"url": "https://example.com"})["decision"] == "pass"
    assert stub.approvals == []


# --------------------------------------------------------------------------------------
# The hook entry: `python -P -m prr.policy` (eng Section 4, D2)
# --------------------------------------------------------------------------------------


def run_hook(stdin: str, env: dict, *args: str) -> tuple[int, dict | None]:
    env = {**os.environ, **env, "PYTHONPATH": policy.PACKAGE_ROOT}
    proc = subprocess.run([sys.executable, "-P", "-m", "prr.policy", *args], input=stdin, capture_output=True,
                          text=True, env=env, timeout=60)
    return proc.returncode, (json.loads(proc.stdout) if proc.stdout.strip() else None)


def test_the_hook_fails_closed(pr):
    for stdin, env in [("not json", {"PRR_PR_DIR": str(pr)}), ('{"tool_input": {}}', {"PRR_PR_DIR": str(pr)}),
                       ('{"tool_name": "Read", "tool_input": {"file_path": "/x"}}', {"PRR_PR_DIR": ""})]:
        code, out = run_hook(stdin, env)
        assert code == 0
        decision = out["hookSpecificOutput"]
        assert decision["permissionDecision"] == "deny"
        assert decision["permissionDecisionReason"].startswith("PR Prism's check failed: ")


def test_the_hook_answers_claude_code(pr):
    env = {"PRR_PR_DIR": str(pr), "PRR_THREAD_ID": "t1"}
    read = {"tool_name": "Read", "tool_input": {"file_path": str(pr / "worktree/src/a.py")}, "cwd": str(pr / "worktree")}
    code, out = run_hook(json.dumps(read), env)
    assert code == 0 and out["hookSpecificOutput"]["permissionDecision"] == "allow"
    # an outside read with no app running: denied with a reason (and never "ask")
    code, out = run_hook(json.dumps({**read, "tool_input": {"file_path": "/etc/hosts"}}), env)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "not running" in out["hookSpecificOutput"]["permissionDecisionReason"]
    code, out = run_hook(json.dumps({**read, "tool_name": "Bash", "tool_input": {"command": "npm i"}}), env, "--bypass")
    assert code == 0 and out is None                                  # bypass mode: no say


def test_the_hook_command_and_module_stay_framework_free():
    command = policy.hook_command(True)
    assert " -P -m prr.policy" in command and command.endswith("|| exit 2")
    probe = ("import sys, prr.policy; bad = sorted(m for m in sys.modules if m.split('.')[0] in "
             "('fastapi', 'pydantic', 'starlette', 'uvicorn', 'prr.server', 'prr.models')); print(bad)")
    out = subprocess.run([sys.executable, "-P", "-c", probe], capture_output=True, text=True, timeout=60,
                         env={**os.environ, "PYTHONPATH": policy.PACKAGE_ROOT})
    assert out.returncode == 0 and out.stdout.strip() == "[]", out.stderr


def test_approve_fails_closed(pr, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bad state")

    monkeypatch.setattr(policy, "resolve", boom)
    out = chat_tools.approve(pr, {"tool_name": LOOKUP, "input": {}})
    assert out == {"behavior": "deny", "message": "PR Prism's check failed: RuntimeError: bad state; ask the reviewer"}


# --------------------------------------------------------------------------------------
# Outsiders' PRs (E1b, eng D1)
# --------------------------------------------------------------------------------------


def test_lookups_on_a_teammates_pr_run_and_mark_the_turn(pr, review_stub):
    stub = review_stub(pr)
    assert policy.trust(pr) == {"author": "alice", "association": "MEMBER", "trusted": True, "state": "trusted"}
    assert ask_tool(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)["behavior"] == "allow"
    assert ask_tool("mcp__codegraph__codegraph_explore", {"query": "x", "projectPath": "/elsewhere"}, pr)["behavior"] == "allow"
    assert stub.approvals == [] and [t["kind"] for t in stub.taints] == ["lookup", "lookup"]


@pytest.mark.parametrize("association, says", [("CONTRIBUTOR", "Outside author (@alice)"),
                                               (None, "Couldn't check the author; lookups ask first")])
def test_lookups_on_an_outsiders_pr_ask(tmp_path, monkeypatch, review_stub, association, says):
    monkeypatch.delenv("PRR_ROOTS", raising=False)
    pr = make_pr(tmp_path, association)
    stub = review_stub(pr)
    assert not policy.trusted(pr)
    assert ask_tool(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)["behavior"] == "deny"
    assert stub.approvals[-1]["reason"] == {"kind": "outside_author", "text": says,
                                            "detail": "a Jira, Slack or other lookup"}
    assert ask_tool("mcp__codegraph__codegraph_explore", {"query": "x", "projectPath": "/elsewhere"}, pr)["behavior"] == "deny"
    # codegraph on this repo (or with no projectPath) still runs
    for inp in ({"query": "x"}, {"query": "x", "projectPath": str(tmp_path / "clone")}):
        assert ask_tool("mcp__codegraph__codegraph_explore", inp, pr)["behavior"] == "allow"
    assert len(stub.approvals) == 2
    # the hook only marks lookups; it never decides them
    assert hook(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)["decision"] == "pass" and stub.taints[-1]["kind"] == "lookup"


# --------------------------------------------------------------------------------------
# The post guard and other ways out (D19, eng D9, D10)
# --------------------------------------------------------------------------------------


def test_posting_asks_only_after_outside_data_in_the_same_turn(pr, review_stub):
    stub = review_stub(pr)
    comment = {"path": "src/a.py", "line": 1, "body": "x"}
    assert ask_tool(POST, comment, pr)["behavior"] == "allow" and stub.approvals == []      # instant

    ask_tool(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)                                          # a lookup marks the turn
    assert ask_tool(POST, comment, pr)["behavior"] == "deny"
    reason = stub.approvals[-1]["reason"]
    assert reason["kind"] == "marked_turn" and "Jira/Slack/other files read this turn" in reason["text"]

    stub.new_turn("t1")                                                                      # the next turn
    assert ask_tool(POST, comment, pr)["behavior"] == "allow" and len(stub.approvals) == 1


def test_an_approved_command_marks_the_turn(pr, review_stub, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    stub = review_stub(pr)
    stub.decision = "allow"
    assert hook("Bash", {"command": "kubectl get secret x"}, pr)["decision"] == "allow"
    stub.decision = "deny"
    assert ask_tool("mcp__prprism__post_review", {"body": "x"}, pr)["behavior"] == "deny"
    assert "output from a command you approved" in stub.approvals[-1]["reason"]["text"]


def test_other_ways_out_ask_in_a_marked_turn(pr, review_stub):
    stub = review_stub(pr)
    assert hook("WebFetch", {"url": "https://x.example/?q=1"}, pr)["decision"] == "pass"    # your settings decide
    assert hook(SLACK_SEND, {"text": "hi"}, pr)["decision"] == "pass"
    assert hook(POST, {"body": "x"}, pr)["decision"] == "pass"
    hook(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)                                             # the mcp__ hook marks
    for tool in ("WebFetch", "WebSearch", SLACK_SEND, POST):
        assert hook(tool, {"url": "https://x.example", "text": "secret"}, pr)["decision"] == "deny", tool
        assert stub.approvals[-1]["reason"]["kind"] == "marked_turn"
    assert len(stub.approvals) == 4


def test_a_remembered_command_asks_again_in_a_marked_turn(pr, review_stub, monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    remember(pr, "Bash:curl")
    stub = review_stub(pr)
    cmd = {"command": "curl -s https://x.example/hook"}
    assert hook("Bash", cmd, pr)["decision"] == "allow" and not stub.approvals    # unmarked: remembered
    stub.new_turn("t1")
    hook(LOOKUP, {"issueIdOrKey": "ABC-1"}, pr)                                    # a lookup marks the turn
    stub.decision = "deny"
    assert hook("Bash", cmd, pr)["decision"] == "deny"
    assert stub.approvals[-1]["reason"]["kind"] == "marked_turn"


def test_a_turn_is_marked_when_the_server_cannot_say(pr):
    assert policy.turn_causes(pr, "t1")                      # no server.json: fails closed
    assert policy.decide(POST, {"body": "x"}, pr_dir=pr, thread_id="t1")["decision"] == "ask"


# --------------------------------------------------------------------------------------
# The server's cards (design issues 1, 8, 20, 23)
# --------------------------------------------------------------------------------------


@pytest.fixture()
def app(pr, monkeypatch):
    from prr.config import Config
    from prr.server import create_app

    return create_app(pr, Config())


def endpoint(app, path: str, method: str):
    return next(r.endpoint for r in app.routes if getattr(r, "path", "") == path and method in r.methods)


async def _card(app, tool: str, inp: dict, reason: dict | None = None) -> tuple[asyncio.Task, dict]:
    from prr.server import ApprovalIn

    queue = app.state.live_streams.setdefault("t1", asyncio.Queue())
    task = asyncio.create_task(endpoint(app, "/api/internal/approvals", "POST")(
        ApprovalIn(thread_id="t1", tool_name=tool, input=inp, reason=reason)))
    while (ev := await asyncio.wait_for(queue.get(), 5))["type"] != "approval":
        pass  # an earlier card's approval_done
    return task, ev


def _decide(app, aid: str, decision: str) -> None:
    from prr.server import DecisionIn

    endpoint(app, "/api/chat/approvals/{aid}", "POST")(aid, DecisionIn(decision=decision))


def _taint(app, kind: str, text: str) -> None:
    from prr.server import TaintIn

    endpoint(app, "/api/internal/taint", "POST")(TaintIn(thread_id="t1", kind=kind, text=text))


def _always(pr: Path) -> list[str]:
    from prr.state import StateStore

    return next((t.get("always_allow", []) for t in StateStore(pr).load()["threads"] if t["id"] == "t1"), [])


@pytest.fixture()
def thread(pr):
    write_json(pr / "state.json", {"threads": [{"id": "t1", "messages": [], "always_allow": []}]})


def test_cards_say_why_they_ask(app, pr, tmp_path, thread):
    async def go():
        task, card = await _card(app, "Read", {"file_path": str(tmp_path / "other/x.py")},
                                 {"kind": "outside_read", "text": "Outside this PR's code"})
        assert card["reason"] == {"kind": "outside_read", "text": "Outside this PR's code"} and card["causes"] == []
        assert card["always"] and card["always_text"].startswith("Always allow reads in ") and not card["guard"]
        assert card["always_title"].endswith(os.path.realpath(tmp_path / "other"))
        _decide(app, card["id"], "deny")
        assert (await task)["decision"] == "deny"

        task, card = await _card(app, LOOKUP, {"issueIdOrKey": "ABC-1"},
                                 {"kind": "outside_author", "text": "Outside author (@alice)"})
        assert card["reason"]["text"] == "Outside author (@alice)"
        assert card["always_text"] == "Always allow Atlassian: getJiraIssue in this chat"
        _decide(app, card["id"], "allow")
        assert (await task)["decision"] == "allow"
        marked = endpoint(app, "/api/internal/taint", "GET")(thread="t1")
        assert marked["marked"] and marked["causes"][-1]["kind"] == "lookup"     # any approval marks the turn

        task, card = await _card(app, POST, {"path": "src/a.py", "line": 1, "body": "**Looks** off"})
        assert card["guard"] and not card["always"] and card["body"] == "**Looks** off"
        assert card["reason"]["kind"] == "marked_turn" and card["causes"] == marked["causes"]
        _decide(app, card["id"], "always")                     # the server refuses to remember a guard card
        assert (await task)["decision"] == "allow"

    asyncio.run(go())
    assert _always(pr) == []


@pytest.mark.parametrize("tool, inp", [("WebFetch", {"url": "https://x.example"}), ("WebSearch", {"query": "x"}),
                                       (SLACK_SEND, {"text": "x"}), (POST, {"body": "x"}),
                                       ("Bash", {"command": "curl -s https://x.example"})])
def test_guard_cards_are_never_remembered(app, pr, thread, tool, inp):
    async def go():
        _taint(app, "outside_read", "~/other/x.py")
        task, card = await _card(app, tool, inp)
        assert card["guard"] and not card["always"]
        _decide(app, card["id"], "always")
        assert (await task)["decision"] == "allow"
        assert app.state.live_streams["t1"].get_nowait() == {"type": "approval_done", "id": card["id"],
                                                             "decision": "allow", "why": "answered"}

    asyncio.run(go())
    assert _always(pr) == []


def test_no_folder_is_remembered_for_home_or_secrets(app, pr, thread):
    async def go():
        for path in ("~/x.txt", "~/.ssh/id_rsa", "~/.config/gh/hosts.yml", "~/.aws/x", "~/.kube/x", "~/.docker/x", "/x"):
            task, card = await _card(app, "Read", {"file_path": os.path.expanduser(path)})
            assert not card["always"], path
            _decide(app, card["id"], "always")
            assert (await task)["decision"] == "allow"

    asyncio.run(go())
    assert _always(pr) == []


def test_a_timeout_tells_the_page(app, thread, monkeypatch):
    monkeypatch.setattr(policy, "APPROVAL_WAIT_S", 0.05)

    async def go():
        task, card = await _card(app, "Bash", {"command": "npm test"})
        answer = await task
        assert answer == {"decision": "deny", "message": "Nobody answered in time."}
        done = app.state.live_streams["t1"].get_nowait()
        assert done == {"type": "approval_done", "id": card["id"], "decision": "deny", "why": "timeout"}

    asyncio.run(go())


def test_always_allow_settles_the_cards_it_covers(app, pr, tmp_path, thread):
    async def go():
        first, a = await _card(app, "Read", {"file_path": str(tmp_path / "other/a.py")})
        second, b = await _card(app, "Read", {"file_path": str(tmp_path / "other/b.py")})
        third, c = await _card(app, "Read", {"file_path": str(tmp_path / "elsewhere/c.py")})
        _decide(app, a["id"], "always")
        assert (await first)["decision"] == "always" and (await second)["decision"] == "allow"
        events = []
        while not app.state.live_streams["t1"].empty():
            events.append(app.state.live_streams["t1"].get_nowait())
        assert {"type": "approval_done", "id": b["id"], "decision": "allow", "why": "covered"} in events
        assert not third.done()                                  # another folder still waits
        _decide(app, c["id"], "deny")
        assert (await third)["decision"] == "deny"

    asyncio.run(go())
    assert _always(pr) == [f"Read:{os.path.realpath(tmp_path / 'other')}"]


def test_the_saved_answer_keeps_one_line_per_card(pr, monkeypatch, tricky_repo):
    """After the turn each card is a one-line record in the answer (design issue 8), saved with the thread: how it
    was settled, or "closed" when it still waited as the turn ended."""
    from fastapi.testclient import TestClient

    from prr import server
    from prr.config import Config
    from prr.state import StateStore
    from tests.test_merge import make_pr_dir

    ctx = make_pr_dir(pr, tricky_repo["diff"])
    write_json(pr / "context.json", {**ctx, "worktree": str(pr / "worktree"), "repo_root": str(pr)})
    (pr / "diff.patch").write_text(tricky_repo["diff"])
    app = server.create_app(pr, Config())
    ask = endpoint(app, "/api/internal/approvals", "POST")
    waiting: list[asyncio.Task] = []

    async def card(tid: str, tool: str, inp: dict) -> str:
        waiting.append(asyncio.create_task(ask(server.ApprovalIn(thread_id=tid, tool_name=tool, input=inp))))
        while not app.state.approvals:
            await asyncio.sleep(0)
        return next(iter(app.state.approvals))

    async def fake_stream(prompt, **kw):
        _decide(app, await card(kw["thread_id"], "Bash", {"command": "npm test"}), "deny")
        assert (await waiting[0])["decision"] == "deny"
        await card(kw["thread_id"], "Read", {"file_path": "/elsewhere/x.py"})   # still waiting when the turn ends
        yield {"type": "result", "session_id": "s", "cost_usd": 0.0, "duration_ms": 1}

    monkeypatch.setattr(server, "stream_chat", fake_stream)
    client = TestClient(server.Guard(app, "tok", 40123), base_url="http://127.0.0.1:40123")
    client.cookies.set("prr_session", "tok")
    h = {"origin": "http://127.0.0.1:40123"}
    tid = client.post("/api/chat/threads", json={}, headers=h).json()["id"]
    stream = client.post(f"/api/chat/threads/{tid}/messages", json={"text": "hi"}, headers=h).text
    assert '"type": "approval_done"' in stream and '"why": "answered"' in stream
    saved = StateStore(pr).load()["threads"][0]["messages"][-1]["approvals"]
    assert [{k: v for k, v in r.items() if k != "id"} for r in saved] == [
        {"tool": "Bash", "title": "Run a command", "summary": "npm test", "decision": "deny", "why": "answered"},
        {"tool": "Read", "title": "Read a file", "summary": "/elsewhere/x.py", "decision": "deny", "why": "closed"},
    ]


def test_trust_state_for_the_page(app, pr):
    assert endpoint(app, "/api/trust", "GET")() == {"author": "alice", "association": "MEMBER", "trusted": True,
                                                    "state": "trusted"}


def test_a_new_turn_starts_unmarked(pr, monkeypatch, tricky_repo):
    """send_message clears the mark and passes the thread, PR folder and roots to the claude process."""
    from fastapi.testclient import TestClient

    from prr import server
    from prr.config import Config
    from tests.test_merge import make_pr_dir

    ctx = make_pr_dir(pr, tricky_repo["diff"])
    write_json(pr / "context.json", {**ctx, "worktree": str(pr / "worktree"), "repo_root": str(pr)})
    (pr / "diff.patch").write_text(tricky_repo["diff"])
    seen = {}

    async def fake_stream(prompt, **kw):
        seen.update(kw)
        yield {"type": "result", "session_id": "s", "cost_usd": 0.0, "duration_ms": 1}

    monkeypatch.setattr(server, "stream_chat", fake_stream)
    app = server.create_app(pr, Config())
    client = TestClient(server.Guard(app, "tok", 40123), base_url="http://127.0.0.1:40123")
    client.cookies.set("prr_session", "tok")
    h = {"origin": "http://127.0.0.1:40123"}
    tid = client.post("/api/chat/threads", json={}, headers=h).json()["id"]
    app.state.marks[tid] = [{"kind": "lookup", "text": "Jira ABC-1"}]
    assert client.get("/api/internal/taint", params={"thread": tid}).json()["marked"] is True
    client.post(f"/api/chat/threads/{tid}/messages", json={"text": "hi"}, headers=h).read()
    assert client.get("/api/internal/taint", params={"thread": tid}).json() == {"marked": False, "causes": []}
    assert seen["thread_id"] == tid and seen["pr_dir"] == pr and seen["roots"] == [pr / "worktree", pr]
    assert client.post("/api/internal/taint", json={"thread_id": tid, "kind": "nope"}, headers=h).status_code == 400


def test_doctor_warns_on_broad_allow_rules(tmp_path):
    from prr.cli import broad_allow_rules

    settings = tmp_path / "settings.json"
    write_json(settings, {"permissions": {"allow": [
        "WebFetch", "mcp__*", "Bash(*)", "Bash(git log *)", "mcp__claude_ai_Slack", "mcp__x__getY",
        "WebFetch(domain:example.com)", "Read"]}})
    assert broad_allow_rules(settings) == ["WebFetch", "mcp__*", "Bash(*)", "mcp__claude_ai_Slack"]
    assert broad_allow_rules(tmp_path / "missing.json") == []
