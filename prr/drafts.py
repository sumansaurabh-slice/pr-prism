"""Making a pending (not yet sent) review comment. Shared by the web API and the chat's comment tools."""

from __future__ import annotations

import uuid

from . import anchors
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
