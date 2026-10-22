"""Publishing a review to GitHub, and reading the comments already on the PR. All calls go through `gh`."""

from __future__ import annotations

import json
import os
from typing import Any

from . import anchors
from .gitgh import CommandError, gh, gh_json

EVENTS = ("COMMENT", "APPROVE", "REQUEST_CHANGES", "PENDING")


class ReviewError(RuntimeError):
    pass


def build_payload(ctx: dict, drafts: list[dict], body: str, event: str, commit_id: str) -> tuple[dict, list[dict]]:
    """The exact JSON for POST /repos/{o}/{r}/pulls/{n}/reviews, plus the drafts that had to go in the body."""
    if event not in EVENTS:
        raise ReviewError(f"unknown event {event!r}")
    files = {f["path"]: f for f in ctx["files"]}
    comments, unanchored = [], []
    for d in drafts:
        if d.get("outdated") or not d.get("anchored"):
            unanchored.append(d)
            continue
        c: dict[str, Any] = {"path": d["path"], "body": d["body"], "line": d["line"], "side": d["side"]}
        if d.get("start_line"):
            c["start_line"], c["start_side"] = d["start_line"], d.get("start_side") or d["side"]
        comments.append(c)
    full_body = body.strip()
    if unanchored:
        lines = ["", "---", "**Comments on lines outside the diff:**", ""]
        for d in unanchored:
            # A range that starts on a deleted line has numbers from two files; link to its last line only.
            start = d.get("start_line") if not d.get("start_side") else None
            link = anchors.permalink(ctx["pr"], files.get(d["path"]), d["path"], d["side"], d["line"], start)
            loc = f"{d['path']}#L{start}-L{d['line']}" if start else f"{d['path']}#L{d['line']}"
            quoted = d["body"].strip().replace("\n", "\n  ")
            lines.append(f"- [`{loc}`]({link}){' (outdated)' if d.get('outdated') else ''}:\n  {quoted}")
        full_body = (full_body + "\n" + "\n".join(lines)).strip()
    payload: dict[str, Any] = {"commit_id": commit_id, "body": full_body, "comments": comments}
    if event != "PENDING":
        payload["event"] = event
    if event in ("COMMENT", "REQUEST_CHANGES") and not full_body and not comments:
        raise ReviewError("a comment-only or request-changes review needs a body or at least one comment")
    return payload, unanchored


def _dry_run() -> bool:
    """PRR_GITHUB_DRY_RUN=1 (tests) makes every post a no-op that returns a fake link."""
    return os.environ.get("PRR_GITHUB_DRY_RUN") == "1"


def _post(endpoint: str, payload: dict, what: str) -> dict:
    if _dry_run():
        return {"id": 0, "html_url": f"https://github.com/dry-run/{endpoint}"}
    try:
        out = gh("api", "-X", "POST", endpoint, "--input", "-", input=json.dumps(payload))
    except CommandError as e:
        detail = e.stdout.strip() or e.stderr.strip()
        try:
            err = json.loads(detail)
            msgs = [err.get("message", "")] + [str(x.get("message", x)) if isinstance(x, dict) else str(x) for x in err.get("errors", [])]
            detail = "; ".join(m for m in msgs if m)
        except (json.JSONDecodeError, AttributeError):
            pass
        raise ReviewError(f"GitHub rejected the {what}: {detail or e}") from e
    return json.loads(out)


def submit(owner: str, repo: str, number: int, payload: dict) -> dict:
    return _post(f"repos/{owner}/{repo}/pulls/{number}/reviews", payload, "review")


def post_comment(owner: str, repo: str, number: int, comment: dict) -> dict:
    """One inline review comment, posted right away (needs commit_id, path, line, side, body)."""
    return _post(f"repos/{owner}/{repo}/pulls/{number}/comments", comment, "comment")


def existing_comments(owner: str, repo: str, number: int) -> list[dict]:
    out = gh("api", "--paginate", f"repos/{owner}/{repo}/pulls/{number}/comments?per_page=100", "--jq", ".[]")
    comments = []
    for line in out.splitlines():
        if not line.strip():
            continue
        c = json.loads(line)
        user = c.get("user") or {}
        login = user.get("login", "")
        comments.append({
            "id": c["id"], "path": c["path"], "line": c.get("line"), "side": c.get("side"),
            "start_line": c.get("start_line"), "body": c.get("body", ""), "user": login,
            "created_at": c.get("created_at", ""), "html_url": c.get("html_url", ""), "in_reply_to_id": c.get("in_reply_to_id"),
            "is_bot": user.get("type") == "Bot" or login.endswith("[bot]"),
            "commit_id": c.get("commit_id"), "original_commit_id": c.get("original_commit_id"),
            "original_line": c.get("original_line"), "diff_hunk": c.get("diff_hunk", ""),
        })
    return comments


def current_head(owner: str, repo: str, number: int) -> str:
    return gh_json("pr", "view", str(number), "--repo", f"{owner}/{repo}", "--json", "headRefOid")["headRefOid"]
