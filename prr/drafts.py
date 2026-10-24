"""Pending (not yet sent) review comments: making one, and claiming them for a post.

Both ways of posting a review (the app's Submit and the chat's post_review) use the same three steps, so a comment
is never sent twice and never lost:
- claim_for_post marks the drafts this post will send with `posting` = its post id, under the state lock. Drafts
  another post already holds, and outdated ones, are skipped.
- finish_post, after GitHub accepted the review, removes only the claimed drafts and records the review.
- release, on any failure, takes the marks off so the drafts are pending again.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import NamedTuple

from . import anchors
from .state import StateStore
from .util import now_iso

SOURCES = ("user", "suggestion", "chat")


def new_draft(ctx: dict, path: str, side: str, line: int, body: str, *, start_line: int | None = None,
              start_side: str | None = None, source: str = "user", suggestion_id: str | None = None) -> dict:
    """A draft dict for state.json. Raises ValueError when the input can't be a comment on this PR."""
    fc = next((f for f in ctx["files"] if f["path"] == path), None)
    if fc is None:
        raise ValueError(f"{path} is not part of this PR")
    if side not in ("LEFT", "RIGHT"):
        raise ValueError("side must be LEFT or RIGHT")
    if not body.strip():
        raise ValueError("the comment is empty")
    start_side = start_side if start_side in ("LEFT", "RIGHT") and start_side != side else None
    start = start_line
    if start is not None and start_side is None:
        if start == line:
            start = None
        elif start > line:
            start, line = line, start
    start_side = start_side if start is not None else None
    return {
        "id": uuid.uuid4().hex[:12], "path": path, "side": side, "line": line, "start_line": start,
        "start_side": start_side, "body": body, "source": source if source in SOURCES else "user",
        "suggestion_id": suggestion_id, "anchored": anchors.check(fc, side, line, start, start_side) is None,
        "outdated": False, "commit_id": ctx["pr"]["head_sha"], "created_at": now_iso(),
    }


# A claim this old belongs to a post that died (GitHub calls time out after 2 minutes), so the drafts are free again.
CLAIM_EXPIRY_S = 10 * 60


class Claim(NamedTuple):
    drafts: list[dict]  # what this post sends; each is marked posting=<post id> in state.json
    busy: int           # skipped: another post is sending them right now
    outdated: int       # skipped: from an older commit; they stay pending until re-placed or deleted


def is_posting(d: dict, now: float | None = None) -> bool:
    """True while a post that is still alive holds this draft (an expired claim doesn't count)."""
    if not d.get("posting"):
        return False
    try:
        since = datetime.fromisoformat(d.get("posting_at") or "").timestamp()
    except ValueError:
        return False
    return (time.time() if now is None else now) - since < CLAIM_EXPIRY_S


def postable(drafts: list[dict], now: float | None = None) -> list[dict]:
    """The drafts a post started now would send: not outdated and not held by another post."""
    return [d for d in drafts if not d.get("outdated") and not is_posting(d, now)]


def _unmark(d: dict) -> None:
    d.pop("posting", None)
    d.pop("posting_at", None)


def claim_for_post(store: StateStore, post_id: str, now: float | None = None) -> Claim:
    """Claim every postable draft for this post, in one update. Expired claims are released here first."""
    t = time.time() if now is None else now
    at = datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")

    def apply(s: dict) -> Claim:
        claimed, busy, outdated = [], 0, 0
        for d in s["drafts"]:
            if d.get("posting") and not is_posting(d, t):
                _unmark(d)
            if d.get("outdated"):
                outdated += 1
            elif d.get("posting"):
                busy += 1
            else:
                d["posting"], d["posting_at"] = post_id, at
                claimed.append(dict(d))
        return Claim(claimed, busy, outdated)

    return store.update(apply)


def finish_post(store: StateStore, post_id: str, record: dict) -> None:
    """GitHub has the review: remove the drafts this post claimed (and only those) and record the review."""
    def apply(s: dict) -> None:
        s["drafts"] = [d for d in s["drafts"] if not (post_id and d.get("posting") == post_id)]
        s["submitted"].append(record)

    store.update(apply)


def release(store: StateStore, post_id: str) -> None:
    """The post failed: its drafts are pending again, unchanged."""
    def apply(s: dict) -> None:
        for d in s["drafts"]:
            if post_id and d.get("posting") == post_id:
                _unmark(d)

    store.update(apply)
