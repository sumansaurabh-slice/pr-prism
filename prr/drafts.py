"""Pending (not yet sent) review comments: making one, and claiming them for a post.

Both ways of posting a review (the app's Submit and the chat's post_review) use the same three steps, so a comment
is never sent twice and never lost:
- claim_for_post marks the drafts this post will send with `posting` = its post id, under the state lock. Drafts
  another post already holds, and outdated ones, are skipped.
- finish_post, after GitHub accepted the review, removes only the claimed drafts, records the review, remembers the
  reviewed commit for a submitted review (prr/since.py) and saves a problem label per posted comment for `prr eval`
  (prr/labels.py).
- release, on any failure, takes the marks off so the drafts are pending again.
"""

from __future__ import annotations

import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Callable, NamedTuple

from . import anchors, labels
from .state import StateStore
from .util import now_iso

SOURCES = ("user", "suggestion", "chat")


def new_draft(ctx: dict, path: str, side: str, line: int, body: str, *, start_line: int | None = None,
              start_side: str | None = None, source: str = "user", suggestion_id: str | None = None,
              origin_id: str | None = None, generated_body: str | None = None) -> dict:
    """A draft dict for state.json. Raises ValueError when the input can't be a comment on this PR.
    `origin_id` is the guided-review stop it was made from (null otherwise): its eval label follows the comment.
    A "suggestion" or "chat" draft keeps `generated_body`, the text PR Prism wrote (`body` unless given), so its eval
    label can say whether the reviewer rewrote it (labels.draft_writer); a "user" draft has it only when given."""
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
    source = source if source in SOURCES else "user"
    return {
        "id": uuid.uuid4().hex[:12], "path": path, "side": side, "line": line, "start_line": start,
        "start_side": start_side, "body": body, "source": source,
        "generated_body": generated_body if generated_body is not None else body if source != "user" else None,
        "suggestion_id": suggestion_id, "origin_id": origin_id,
        "anchored": anchors.check(fc, side, line, start, start_side) is None,
        "outdated": False, "commit_id": ctx["pr"]["head_sha"], "created_at": now_iso(),
    }


# A claim this old belongs to a post that died (GitHub calls time out after 2 minutes), so the drafts are free again.
CLAIM_EXPIRY_S = 10 * 60
# When the review server starts, a claim older than GitHub's timeout can only belong to a post that died with it.
STARTUP_EXPIRY_S = 3 * 60


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


def finish_post(store: StateStore, post_id: str, record: dict,
                labels_for: Callable[[], list[dict]] | None = None,
                reviewed: Callable[[], dict] | None = None) -> str | None:
    """GitHub has the review: remove the drafts this post claimed (and only those) and record the review. Only then
    make and save the eval labels (`labels_for`: a problem label per posted comment), so nothing that goes wrong there
    can leave posted drafts behind to be sent again. Returns labels.SAVE_FAILED when the labels couldn't be saved.
    `reviewed` is given for a submitted review (not a lone comment): it returns the fields saved for "since your
    review" (prr/since.py `on_review`: reviewed_head, reviewed_base, reviewed_stop_ids)."""
    def apply(s: dict) -> None:
        s["drafts"] = [d for d in s["drafts"] if not (post_id and d.get("posting") == post_id)]
        s["submitted"].append(record)

    store.update(apply)
    if reviewed is not None:
        try:
            fields = reviewed()
            store.update(lambda s: s.update(fields))
        except Exception as e:  # noqa: BLE001 — the review is on GitHub; only "since your review" is lost
            print(f"[prr] could not save the reviewed commit: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
    if labels_for is None:
        return None
    try:
        rows = labels_for()
    except Exception as e:  # noqa: BLE001 — the review is on GitHub; only its eval labels are lost
        print(f"[prr] could not make the eval labels for this post: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
        return labels.SAVE_FAILED
    return labels.save(store.path.parent, rows)


def release(store: StateStore, post_id: str) -> None:
    """The post failed: its drafts are pending again, unchanged."""
    def apply(s: dict) -> None:
        for d in s["drafts"]:
            if post_id and d.get("posting") == post_id:
                _unmark(d)

    store.update(apply)


def release_stale(store: StateStore, older_than_s: float, now: float | None = None) -> int:
    """Give back drafts claimed more than `older_than_s` ago. Returns how many."""
    t = time.time() if now is None else now

    def apply(s: dict) -> int:
        freed = 0
        for d in s["drafts"]:
            if d.get("posting"):
                try:
                    since = datetime.fromisoformat(d.get("posting_at") or "").timestamp()
                except ValueError:
                    since = 0.0
                if t - since >= older_than_s:
                    _unmark(d)
                    freed += 1
        return freed

    if not any(d.get("posting") for d in store.load()["drafts"]):
        return 0
    return store.update(apply)
