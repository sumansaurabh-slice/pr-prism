"""The chat's own MCP server over stdio (`prr _mcp <pr_dir>`).

Comment tools for this one PR:
- add_pending_comment: add to the reviewer's pending comments in the app (nothing is sent to GitHub);
- post_comment: post one line comment to GitHub now, as the reviewer;
- post_review: post a review (a summary, optionally with all pending comments) to GitHub now.
config.toml [chat] can_post turns the two posting tools off; can_approve allows APPROVE / REQUEST_CHANGES.

And `approve`, Claude Code's permission prompt tool (`--permission-prompt-tool`): for every action that is not
on the auto-allow list, policy.py decides; when it needs the reviewer, the review server shows a card in the
browser and this waits for Allow / Always allow / Deny. Connector lookups run without asking, except on PRs from
outside authors; posting asks only in a turn that already touched outside data.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Callable

from . import anchors, github, labels, policy
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


def approve(pr_dir: Path, args: dict) -> dict:
    """Claude Code's permission prompt. Returns {"behavior": "allow", "updatedInput": …} or a deny with a message.
    policy.decide says what needs a card. Shell commands are decided by the PreToolUse hook; one that still gets
    here is asked about. Any error denies (fails closed), never a tool error."""
    tool, inp = str(args.get("tool_name", "")), args.get("input") or {}
    try:
        out = policy.resolve(tool, inp, pr_dir=pr_dir, thread_id=os.environ.get("PRR_THREAD_ID", ""))
    except Exception as e:  # noqa: BLE001 — a bug here must never let the action through
        return {"behavior": "deny", "message": policy.CHECK_FAILED.format(f"{type(e).__name__}: {e}")}
    if out["decision"] == "allow":
        return {"behavior": "allow", "updatedInput": inp}
    return {"behavior": "deny", "message": out.get("message") or policy.DENIED}


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


def _record(pr_dir: Path, html_url: str, event: str, comments: int, review_id: Any = None, post_id: str = "",
            posted: list[dict] | Callable[[], list[dict]] | None = None, author: str | None = None) -> str | None:
    """Note the post in state.json. For a review with pending comments, also remove the ones it claimed (only those).
    `posted` is each comment that went to GitHub ({path, line, side, body, gh_comment_id, origin_id?}), or a function
    that returns them (when their GitHub ids must be read first: that waits until state.json is safe). Each becomes a
    problem label for `prr eval`. Returns a warning when those labels couldn't be saved."""
    def rows() -> list[dict]:
        pr = read_json(pr_dir / "context.json")["pr"]
        comments_posted = posted() if callable(posted) else posted or []
        return labels.for_posted(pr, comments_posted, commit_id=pr["head_sha"], author=author)

    return finish_post(StateStore(pr_dir), post_id, {"review_id": review_id, "html_url": html_url, "event": event,
                                                     "comments": comments, "at": now_iso(), "source": "chat"}, rows)


def _author(res: dict) -> str | None:
    return (res.get("user") or {}).get("login") or None


def _with_warning(text: str, warning: str | None) -> str:
    return f"{text} ({warning}; the post itself is fine.)" if warning else text


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
        posted = [{"path": draft["path"], "line": draft["line"], "side": draft["side"], "body": draft["body"],
                   "gh_comment_id": res.get("id") or None}]
        warning = _record(pr_dir, res.get("html_url", ""), "COMMENT", 1, review_id=res.get("id"), posted=posted,
                          author=_author(res))
        return _with_warning(f"Posted the comment on {where} to GitHub: {res.get('html_url', '')}", warning), False

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
    sent = claim.drafts if claim else []

    def posted() -> list[dict]:
        ids = github.review_comments(pr["owner"], pr["repo"], pr["number"], res.get("id")) if sent else []
        return labels.posted_drafts(sent, ids)

    warning = _record(pr_dir, res.get("html_url", ""), event, len(payload["comments"]), review_id=res.get("id"),
                      post_id=post_id, posted=posted, author=_author(res))
    kind = {"COMMENT": "comment", "APPROVE": "approval", "REQUEST_CHANGES": "request for changes"}[event]
    text = f"Posted the review ({kind}, {len(payload['comments'])} line comments) to GitHub: {res.get('html_url', '')}"
    if claim and claim.busy:
        text += f" {claim.busy} pending comments were left out because another post is already sending them."
    if claim and claim.outdated:
        text += (f" {claim.outdated} pending comments are from an older commit and were not sent; the reviewer can "
                 "re-place or delete them in the app.")
    return _with_warning(text, warning), False


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
