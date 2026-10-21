"""Where a review comment may live on GitHub, and moving drafts when the PR head changes.

GitHub accepts an inline review comment only on a line inside one of the PR diff's hunks:
RIGHT = new-file line numbers (added or context lines), LEFT = old-file line numbers.
A multi-line comment (start_line..line) must sit in a single hunk. It may start on a deleted line
(start_side LEFT) and end on an added or unchanged line (side RIGHT), as GitHub allows; otherwise
both ends are on the same side and start_line < line.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import diffparse
from .gitgh import git
from .state import StateStore


def _contains(h: dict, side: str, line: int) -> bool:
    if side == "RIGHT":
        return h["new_lines"] > 0 and h["new_start"] <= line < h["new_start"] + h["new_lines"]
    return h["old_lines"] > 0 and h["old_start"] <= line < h["old_start"] + h["old_lines"]


def hunk_for(file_ctx: dict, side: str, line: int) -> dict | None:
    return next((h for h in file_ctx["hunks"] if _contains(h, side, line)), None)


def check(file_ctx: dict | None, side: str, line: int, start_line: int | None, start_side: str | None = None) -> str | None:
    """None if GitHub will accept this anchor, else the reason it won't."""
    if file_ctx is None:
        return "file is not part of this PR"
    if file_ctx["binary"]:
        return "binary files can't take inline comments"
    if side not in ("LEFT", "RIGHT"):
        return f"invalid side {side!r}"
    h = hunk_for(file_ctx, side, line)
    if h is None:
        return f"{side} line {line} is outside the diff"
    if start_line is not None:
        start_side = start_side or side
        if start_side not in ("LEFT", "RIGHT"):
            return f"invalid start_side {start_side!r}"
        if start_side == side and start_line >= line:
            return "start_line must be before line"
        if start_side == "RIGHT" and side == "LEFT":
            return "a range can't start on a new line and end on an old (deleted) line"
        if hunk_for(file_ctx, start_side, start_line) is not h:
            return "a multi-line range must stay within one hunk"
    return None


def permalink(pr: dict, file_ctx: dict | None, path: str, side: str, line: int, start_line: int | None) -> str:
    if side == "LEFT":
        sha, p = pr["merge_base_sha"], (file_ctx or {}).get("old_path") or path
    else:
        sha, p = pr["head_sha"], path
    frag = f"#L{start_line}-L{line}" if start_line else f"#L{line}"
    return f"https://github.com/{pr['owner']}/{pr['repo']}/blob/{sha}/{p}{frag}"


# --------------------------------------------------------------------------------------
# Remapping drafts across a PR update
# --------------------------------------------------------------------------------------


def map_line(hunks: list[diffparse.Hunk], line: int) -> int | None:
    """Follow an old-commit line through an -U0 diff to the new commit; None if that line itself changed."""
    offset = 0
    for h in hunks:
        if h.old_lines == 0:  # pure insertion after old line h.old_start
            if line > h.old_start:
                offset += h.new_lines
                continue
            break
        if line < h.old_start:
            break
        if line < h.old_start + h.old_lines:
            return None
        offset += h.new_lines - h.old_lines
    return line + offset


def remap_state(pr_dir: Path, root: Path, old_head: str, new_head: str, new_files: list[diffparse.FileDiff]) -> dict[str, int] | None:
    by_path = {f.path: f for f in new_files}
    cache: dict[str, list[diffparse.Hunk]] = {}

    def hunks_between(path: str) -> list[diffparse.Hunk]:
        if path not in cache:
            out = git("diff", "--no-ext-diff", "--no-textconv", "-U0", "--src-prefix=a/", "--dst-prefix=b/",
                      old_head, new_head, "--", path, cwd=root, check=False)
            parsed = diffparse.parse(out)
            cache[path] = parsed[0].hunks if parsed else []
        return cache[path]

    def apply(state: dict[str, Any]) -> dict[str, int] | None:
        drafts = state["drafts"]
        if not drafts:
            return None
        stats = {"remapped": 0, "outdated": 0}
        for d in drafts:
            if d.get("outdated"):
                continue
            line, start = d["line"], d.get("start_line")
            if d["side"] == "RIGHT":
                line = map_line(hunks_between(d["path"]), line)
            if start is not None and (d.get("start_side") or d["side"]) == "RIGHT":
                start = map_line(hunks_between(d["path"]), start)
            fd = by_path.get(d["path"])
            file_ctx = None if fd is None else {"binary": fd.binary, "hunks": fd.hunk_infos()}
            if line is None or (d.get("start_line") is not None and start is None) or fd is None:
                d["outdated"] = True
                stats["outdated"] += 1
                continue
            d["line"], d["start_line"], d["commit_id"] = line, start, new_head
            d["anchored"] = check(file_ctx, d["side"], line, start, d.get("start_side")) is None
            stats["remapped"] += 1
        return stats

    return StateStore(pr_dir).update(apply)
