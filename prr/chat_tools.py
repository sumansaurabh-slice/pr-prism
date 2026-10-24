"""The chat's own MCP server over stdio (`prr _mcp <pr_dir>`).

Comment tools for this one PR:
- add_pending_comment: add to the reviewer's pending comments in the app (nothing is sent to GitHub);
- post_comment: post one line comment to GitHub now, as the reviewer;
- post_review: post a review (a summary, optionally with all pending comments) to GitHub now.
config.toml [chat] can_post turns the two posting tools off; can_approve allows APPROVE / REQUEST_CHANGES.

And `approve`, Claude Code's permission prompt tool (`--permission-prompt-tool`): for every action that is not
on the auto-allow list, it asks the reviewer in the browser (through the review server) and waits for
Allow / Always allow / Deny. Shell commands that only read (see shellsafe.py) and connector lookups run
without asking.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from . import anchors, github, shellsafe
from .config import Config
from .drafts import claim_for_post, finish_post, new_draft, release
from .gitgh import viewer_login
from .state import StateStore
from .util import now_iso, read_json

SERVER_NAME = "prprism"  # tools appear to Claude as mcp__prprism__<tool>
PROTOCOL_VERSION = "2025-06-18"

_LINE_ARGS = {
    "path": {"type": "string", "description": "File path from the repo root, exactly as in the PR"},
    "line": {"type": "integer", "description": "The line to comment on (the last line, for a range)"},
    "side": {"type": "string", "enum": ["RIGHT", "LEFT"],
             "description": "RIGHT = new or unchanged code (default). LEFT = a deleted (red) line, with its old line number"},
    "start_line": {"type": "integer", "description": "First line, for a comment on several lines"},
    "start_side": {"type": "string", "enum": ["RIGHT", "LEFT"],
                   "description": "Only when the range starts on a deleted line and ends on a new one: LEFT"},
    "body": {"type": "string", "description": "The comment, in Markdown, written to the PR author"},
}
_COMMENT_SCHEMA = {"type": "object", "properties": _LINE_ARGS, "required": ["path", "line", "body"]}


APPROVAL_WAIT_S = 15 * 60
# Connector tools that only look things up (getJiraIssue, searchJiraIssuesUsingJql, slack_read_channel, …) run without asking.
_READ_ONLY_MCP = re.compile(r"(^|_)(get|search|list|read|fetch|lookup|query|find|view)([A-Z_]|$)")


def approval_keys(tool: str, inp: dict) -> list[str]:
    """What "Always allow in this chat" covers: a whole tool, or for shell commands each program that isn't
    read-only (`awk`, `npm test`), so the next command using only those programs doesn't ask again."""
    if tool == "Bash":
        return ["Bash:" + k for k in shellsafe.approval_keys(str(inp.get("command", "")))]
    return [tool]


def auto_allowed(tool: str, inp: dict | None = None) -> bool:
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        return len(parts) >= 3 and bool(_READ_ONLY_MCP.search(parts[2]))
    if tool == "Bash":
        return shellsafe.is_read_only(str((inp or {}).get("command", "")))
    return False


def _ask_reviewer(pr_dir: Path, tool: str, inp: dict) -> dict:
    """Ask through the review server (which shows the card in the chat) and wait for the answer."""
    info = read_json(pr_dir / "server.json") or {}
    if not info.get("port"):
        return {"decision": "deny", "message": "The review app is not running, so nobody can approve this."}
    base = f"http://127.0.0.1:{info['port']}"
    data = json.dumps({"thread_id": os.environ.get("PRR_THREAD_ID", ""), "tool_name": tool, "input": inp}).encode()
    req = urllib.request.Request(f"{base}/api/internal/approvals", data=data, method="POST", headers={
        "Content-Type": "application/json", "Cookie": f"prr_session={info['token']}", "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=APPROVAL_WAIT_S + 30) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return {"decision": "deny", "message": f"Could not ask the reviewer: {e}"}


def approve(pr_dir: Path, args: dict) -> dict:
    """Claude Code's permission prompt. Returns {"behavior": "allow", "updatedInput": …} or a deny with a message."""
    tool, inp = str(args.get("tool_name", "")), args.get("input") or {}
    allow = {"behavior": "allow", "updatedInput": inp}
    if auto_allowed(tool, inp):
        return allow
    thread_id = os.environ.get("PRR_THREAD_ID", "")
    state = StateStore(pr_dir).load()
    thread = next((t for t in state["threads"] if t["id"] == thread_id), {})
    if set(approval_keys(tool, inp)) <= set(thread.get("always_allow", [])):
        return allow
    answer = _ask_reviewer(pr_dir, tool, inp)
    if answer.get("decision") in ("allow", "always"):
        return allow
    return {"behavior": "deny", "message": answer.get("message") or
            "The reviewer said no. Don't try this again; ask them what they want instead."}


def tool_list(cfg: Config) -> list[dict]:
    tools = [{
        "name": "approve",
        "description": "Internal: Claude Code calls this to ask the reviewer for permission. Do not call it yourself.",
        "inputSchema": {"type": "object", "properties": {
            "tool_name": {"type": "string"}, "input": {"type": "object"}, "tool_use_id": {"type": "string"}},
            "required": ["tool_name", "input"]},
    }, {
        "name": "add_pending_comment",
        "description": "Add a review comment to the reviewer's pending comments in the review app. It is NOT sent to "
                       "GitHub; the reviewer sends it later with Submit review. Works on any line of a changed file.",
        "inputSchema": _COMMENT_SCHEMA,
    }]
    if cfg.chat.can_post:
        events = ["COMMENT", "APPROVE", "REQUEST_CHANGES"] if cfg.chat.can_approve else ["COMMENT"]
        tools += [{
            "name": "post_comment",
            "description": "Post one review comment on a line of this PR to GitHub right now, under the reviewer's "
                           "name. Use only when the reviewer asks in their latest message for it to be posted. The "
                           "line must be inside the diff.",
            "inputSchema": _COMMENT_SCHEMA,
        }, {
            "name": "post_review",
            "description": "Post a review on this PR to GitHub right now, under the reviewer's name: a summary text, "
                           "and optionally all of the reviewer's pending comments. Use only when the reviewer asks "
                           f"for it in their latest message. Allowed events: {', '.join(events)}.",
            "inputSchema": {"type": "object", "properties": {
                "body": {"type": "string", "description": "The review summary, in Markdown"},
                "event": {"type": "string", "enum": events, "description": "COMMENT unless the reviewer says otherwise"},
                "include_pending": {"type": "boolean",
                                    "description": "Also send all of the reviewer's pending comments in this review"},
            }, "required": ["body"]},
        }]
    return tools


def _record(pr_dir: Path, html_url: str, event: str, comments: int, review_id: Any = None, post_id: str = "") -> None:
    """Note the post in state.json. For a review with pending comments, also remove the ones it claimed (only those)."""
    finish_post(StateStore(pr_dir), post_id, {"review_id": review_id, "html_url": html_url, "event": event,
                                              "comments": comments, "at": now_iso(), "source": "chat"})


def call_tool(pr_dir: Path, cfg: Config, name: str, args: dict) -> tuple[str, bool]:
    """Run one tool. Returns (text for Claude, is_error)."""
    ctx = read_json(pr_dir / "context.json")
    if ctx is None:
        return "This PR has not been fetched.", True
    pr = ctx["pr"]
    if name == "approve":
        return json.dumps(approve(pr_dir, args)), False
    allowed = {t["name"] for t in tool_list(cfg)}
    if name not in allowed:
        return (f"The tool {name} is turned off. The reviewer can change this in config.toml under [chat].", True)

    if name in ("add_pending_comment", "post_comment"):
        try:
            draft = new_draft(ctx, args.get("path", ""), args.get("side") or "RIGHT", int(args["line"]), args.get("body", ""),
                              start_line=args.get("start_line"), start_side=args.get("start_side"), source="chat")
        except (ValueError, KeyError, TypeError) as e:
            return f"Could not make this comment: {e}", True
        where = f"{draft['path']}:{draft['start_line']}-{draft['line']}" if draft["start_line"] else f"{draft['path']}:{draft['line']}"
        if name == "add_pending_comment":
            StateStore(pr_dir).update(lambda s: s["drafts"].append(draft))
            extra = "" if draft["anchored"] else " That line is not in the diff, so it will go in the review summary as a link."
            return f"Added a pending comment on {where}. It is not on GitHub yet; the reviewer sends it with Submit review.{extra}", False
        file_ctx = next(f for f in ctx["files"] if f["path"] == draft["path"])
        reason = anchors.check(file_ctx, draft["side"], draft["line"], draft["start_line"], draft["start_side"])
        if reason:
            return f"Can't post this on GitHub: {reason}. Offer to add it as a pending comment instead.", True
        comment = {"body": draft["body"], "commit_id": pr["head_sha"], "path": draft["path"], "line": draft["line"],
                   "side": draft["side"]}
        if draft["start_line"]:
            comment["start_line"], comment["start_side"] = draft["start_line"], draft["start_side"] or draft["side"]
        try:
            res = github.post_comment(pr["owner"], pr["repo"], pr["number"], comment)
        except github.ReviewError as e:
            return str(e), True
        _record(pr_dir, res.get("html_url", ""), "COMMENT", 1, review_id=res.get("id"))
        return f"Posted the comment on {where} to GitHub: {res.get('html_url', '')}", False

    # post_review
    event = args.get("event") or "COMMENT"
    if event not in ("COMMENT", "APPROVE", "REQUEST_CHANGES"):
        return f"Unknown review event {event!r}.", True
    if event != "COMMENT" and not cfg.chat.can_approve:
        return ("Approving or requesting changes from the chat is turned off. The reviewer can do it with the Submit "
                "review button, or allow it with can_approve = true under [chat] in config.toml."), True
    if event in ("APPROVE", "REQUEST_CHANGES"):
        try:
            if viewer_login().lower() == pr["author"].lower():
                return "GitHub doesn't let the PR author approve or request changes on their own PR.", True
        except Exception:  # noqa: BLE001 — if gh can't tell, let GitHub decide
            pass
    # Pending comments are claimed first (see drafts.py): one that another post is already sending, or one added
    # while this one posts, isn't sent twice or lost. When none are left to claim, only the summary goes.
    store, post_id = StateStore(pr_dir), uuid.uuid4().hex[:12]
    claim = claim_for_post(store, post_id) if args.get("include_pending") else None
    try:
        payload, _ = github.build_payload(ctx, claim.drafts if claim else [], args.get("body", ""), event, pr["head_sha"])
        res = github.submit(pr["owner"], pr["repo"], pr["number"], payload)
    except BaseException as e:
        if claim:
            release(store, post_id)  # the claimed comments are pending again
        if isinstance(e, github.ReviewError):
            return str(e), True
        raise
    _record(pr_dir, res.get("html_url", ""), event, len(payload["comments"]), review_id=res.get("id"), post_id=post_id)
    kind = {"COMMENT": "comment", "APPROVE": "approval", "REQUEST_CHANGES": "request for changes"}[event]
    text = f"Posted the review ({kind}, {len(payload['comments'])} line comments) to GitHub: {res.get('html_url', '')}"
    if claim and claim.busy:
        text += f" {claim.busy} pending comments were left out because another post is already sending them."
    if claim and claim.outdated:
        text += (f" {claim.outdated} pending comments are from an older commit and were not sent; the reviewer can "
                 "re-place or delete them in the app.")
    return text, False


def serve_stdio(pr_dir: Path, cfg: Config) -> None:
    """Minimal MCP server: newline-delimited JSON-RPC on stdin/stdout. Tool calls run in threads, so one approval
    waiting for the reviewer doesn't block the next request."""
    lock = threading.Lock()
    workers: list[threading.Thread] = []

    def reply(msg_id: Any, result: Any = None, error: dict | None = None) -> None:
        out = {"jsonrpc": "2.0", "id": msg_id, **({"error": error} if error else {"result": result})}
        with lock:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()

    def run_tool(msg_id: Any, params: dict) -> None:
        try:
            text, is_error = call_tool(pr_dir, cfg, params.get("name", ""), params.get("arguments") or {})
        except Exception as e:  # noqa: BLE001 — report it to Claude instead of dying mid-chat
            text, is_error = f"The tool failed: {e}", True
        reply(msg_id, {"content": [{"type": "text", "text": text}], "isError": is_error})

    while True:
        raw = sys.stdin.readline()
        if not raw:
            for w in workers:  # finish the tool calls already started before exiting
                w.join()
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if msg_id is None:  # a notification, e.g. notifications/initialized
            continue
        if method == "initialize":
            reply(msg_id, {"protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                           "capabilities": {"tools": {}}, "serverInfo": {"name": "pr-prism", "version": "1"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": tool_list(cfg)})
        elif method == "tools/call":
            worker = threading.Thread(target=run_tool, args=(msg_id, params), daemon=True)
            workers.append(worker)
            worker.start()
        elif method == "ping":
            reply(msg_id, {})
        else:
            reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
