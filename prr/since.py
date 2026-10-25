"""Since your last review (E4): what changed after the commit you last reviewed.

- **The reviewed commit.** When you submit a review (the app's Submit, or the chat's post_review; a lone chat
  post_comment doesn't count), drafts.finish_post saves `reviewed_head` (the analyzed head), `reviewed_base` (its merge
  base) and `reviewed_stop_ids` (the guided-review stops present then) in state.json, and the commit is kept as
  refs/pr-prism/<n>/reviewed in your repo, fetched by sha if needed. `prr clean` keeps that ref.
- **changes()**, for GET /api/since-review: `git diff <reviewed_head> <head> -- <the PR's current files>`, the
  interdiff. When the merge base moved, a note says some changes may come from the base branch. When the reviewed
  commit is gone (a force-push) and can't be fetched, a note says so and the files are the whole PR. Any other git
  failure: ok = false and a note, and the app disables its toggle.
- **is_new_stop()**: a stop is new when its lines meet an interdiff hunk (same path, new-side lines, with git's 3 lines
  of context). There is no id rule: a re-run can re-anchor a stop without any code change.
- **Your earlier comments**: outdated ones, and ones whose lines meet the interdiff, are re-checked by the analysis on
  the new head (prr/recheck.py, counted against `[analysis] recheck_max`); the others are "line unchanged".
- **estimate()**: how long and how much the last complete analysis run took (calls.jsonl), for "Re-read the PR".
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import cost, diffparse, ledger
from .fetch import DIFF_ARGS
from .gitgh import CommandError, PRRef, find_remote, git, git_ok, run, viewer_login
from .state import StateStore
from .supervise import pid_alive
from .util import read_json

SHA = re.compile(r"^[0-9a-f]{7,64}$")
FETCH_TIMEOUT_S = 60
GONE = "The commit you reviewed ({short}) is no longer on GitHub, likely after a force-push, so this shows the whole PR."
BASE_MOVED = "{base} moved; some changes may come from {base}"
FAILED = "Couldn't compute changes since your review: {reason}"
LIMIT = "{n} not checked (limit; raise `recheck_max`)"
NOT_YET = "Not checked on this commit yet. Re-read the PR to check it."
STATUSES = ("fixed", "still-open", "line-unchanged", "checking", "not-checked")


def _log(msg: str) -> None:
    print(f"[prr] {msg}", file=sys.stderr, flush=True)


def ref_name(number: int) -> str:
    return f"refs/pr-prism/{number}/reviewed"


# --------------------------------------------------------------------------------------
# The reviewed commit
# --------------------------------------------------------------------------------------


def have_commit(root: Path, sha: str) -> bool:
    return git_ok("cat-file", "-e", f"{sha}^{{commit}}", cwd=root)


def fetch_commit(root: Path, pr: dict, sha: str) -> bool:
    """Fetch one commit by sha from the PR's repo (read-only). False when it can't be fetched (gone after a
    force-push, or GitHub unreachable)."""
    remote = find_remote(root, PRRef(pr["owner"], pr["repo"], pr["number"]))
    try:
        run(["git", "fetch", "--no-tags", "--quiet", remote, sha], cwd=root, timeout=FETCH_TIMEOUT_S,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    except (CommandError, OSError, subprocess.TimeoutExpired):
        return False
    return have_commit(root, sha)


def keep_commit(root: Path, pr: dict, sha: str) -> None:
    """Keep `sha` as refs/pr-prism/<n>/reviewed, so git never drops it; fetched by sha first if the repo lacks it."""
    if not have_commit(root, sha) and not fetch_commit(root, pr, sha):
        raise CommandError(["git", "fetch"], 1, "", f"commit {sha[:10]} is not in this repo and couldn't be fetched")
    git("update-ref", ref_name(pr["number"]), sha, cwd=root)


def stops(analysis: Any) -> list[dict]:
    """analysis.stops (guided review), read defensively: [] while the analysis has none."""
    found = analysis.get("stops") if isinstance(analysis, dict) else None
    return [s for s in found if isinstance(s, dict)] if isinstance(found, list) else []


def stop_ids(pr_dir: Path) -> list[str]:
    from . import merge

    try:
        analysis = merge.build_analysis(pr_dir)
    except Exception as e:  # noqa: BLE001 — the review is already on GitHub; only the stop list is lost
        _log(f"could not read the guided-review stops: {type(e).__name__}: {e}")
        return []
    return [str(s["id"]) for s in stops(analysis) if s.get("id")]


def on_review(pr_dir: Path, ctx: dict) -> dict:
    """What drafts.finish_post saves when a review is submitted: the analyzed head and merge base, and the stop ids
    present now. Also keeps the commit as refs/pr-prism/<n>/reviewed; a failure there is logged, never raised (the
    review is already on GitHub)."""
    pr = ctx["pr"]
    try:
        keep_commit(Path(ctx["repo_root"]), pr, pr["head_sha"])
    except Exception as e:  # noqa: BLE001
        _log(f"could not keep the reviewed commit as {ref_name(pr['number'])}: {e}")
    return {"reviewed_head": pr["head_sha"], "reviewed_base": pr.get("merge_base_sha"),
            "reviewed_stop_ids": stop_ids(pr_dir)}


# --------------------------------------------------------------------------------------
# The interdiff
# --------------------------------------------------------------------------------------


def _hunk(h: Any) -> dict:
    get = h.get if isinstance(h, dict) else lambda k: getattr(h, k)
    return {k: get(k) for k in ("old_start", "old_lines", "new_start", "new_lines")}


def _reason(e: Exception) -> str:
    if isinstance(e, CommandError):
        lines = [ln.strip() for ln in (e.stderr or e.stdout or "").splitlines() if ln.strip()]
        if lines:
            return lines[-1][:300]
    return str(e)[:300] or type(e).__name__


def _whole_pr(pr_dir: Path, ctx: dict) -> dict:
    """The fallback when the reviewed commit is gone: every file and hunk of the PR, with its patches."""
    try:
        patches = {f.path: f.patch for f in diffparse.parse((pr_dir / "diff.patch").read_bytes().decode(
            "utf-8", errors="replace"))}
    except OSError:
        patches = {}
    return {"files": [{"path": f["path"], "hunks": [_hunk(h) for h in f.get("hunks", [])]} for f in ctx["files"]],
            "patch": {f["path"]: patches[f["path"]] for f in ctx["files"] if f["path"] in patches}, "full_pr": True}


def _base_moved(root: Path, pr: dict, state: dict, reviewed: str) -> bool:
    before = state.get("reviewed_base")
    if before:
        return before != pr.get("merge_base_sha")
    # An older state.json without it: the base moved if the current merge base isn't part of the reviewed commit.
    base = pr.get("merge_base_sha") or ""
    return bool(SHA.match(base)) and not git_ok("merge-base", "--is-ancestor", base, reviewed, cwd=root)


def changes(pr_dir: Path, ctx: dict, state: dict, tried: set[str] | None = None) -> dict:
    """{reviewed_head, head, files: [{path, hunks: [{old_start, old_lines, new_start, new_lines}]}], patch: {path:
    unified diff}, note, ok, full_pr}. No reviewed head, or the same head: no files. `tried` holds the shas already
    fetched once (the server keeps it, so a gone commit isn't fetched on every poll)."""
    pr = ctx["pr"]
    reviewed, head = state.get("reviewed_head"), pr["head_sha"]
    out: dict[str, Any] = {"reviewed_head": reviewed, "head": head, "files": [], "patch": {}, "note": None, "ok": True,
                           "full_pr": False}
    if not reviewed or reviewed == head:
        return out
    if not isinstance(reviewed, str) or not SHA.match(reviewed):
        return {**out, "ok": False, "note": FAILED.format(reason="the saved commit id is not valid")}
    tried = set() if tried is None else tried
    try:
        root = Path(ctx["repo_root"])
        if not have_commit(root, reviewed):
            first = reviewed not in tried
            tried.add(reviewed)
            if not (first and fetch_commit(root, pr, reviewed)):
                return {**out, **_whole_pr(pr_dir, ctx), "note": GONE.format(short=reviewed[:7])}
        paths = sorted({p for f in ctx["files"] for p in (f["path"], f.get("old_path")) if p})
        if not paths:  # no pathspec would mean every file in the repo
            return out
        text = git("--literal-pathspecs", *DIFF_ARGS, reviewed, head, "--", *paths, cwd=root)
        moved = _base_moved(root, pr, state, reviewed)
    except (CommandError, OSError, KeyError) as e:
        return {**out, "ok": False, "note": FAILED.format(reason=_reason(e))}
    files = diffparse.parse(text)
    base = pr.get("base_ref") or "main"
    return {**out, "files": [{"path": f.path, "hunks": [_hunk(h) for h in f.hunks]} for f in files],
            "patch": {f.path: f.patch for f in files}, "note": BASE_MOVED.format(base=base) if moved else None}


def touches(files: list[dict], path: str, lo: int, hi: int) -> bool:
    """Do lines lo..hi (new side) of `path` meet a hunk of the interdiff?"""
    for f in files:
        if f.get("path") != path:
            continue
        for h in f.get("hunks") or []:
            start = h["new_start"]
            end = start + max(h["new_lines"], 1) - 1
            if start <= hi and lo <= end:
                return True
    return False


def _first_int(d: dict, *keys: str) -> int | None:
    for k in keys:
        v = d.get(k)
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


def is_new_stop(stop: dict, interdiff_files: list[dict]) -> bool:
    """A stop is new when its line range meets an interdiff hunk, by path and new-side lines. Its id doesn't count:
    a re-run can re-anchor a stop without any code change. A stop with no path or line, or on a deleted line, isn't."""
    path = stop.get("path")
    if not isinstance(path, str) or not path or (stop.get("side") or "RIGHT") != "RIGHT":
        return False
    lo = _first_int(stop, "start_line", "start", "line")
    hi = _first_int(stop, "end_line", "end", "line")
    if lo is None and hi is None:
        return False
    lo, hi = lo or hi, hi or lo
    return touches(interdiff_files, path, min(lo, hi), max(lo, hi))


# --------------------------------------------------------------------------------------
# Your earlier comments
# --------------------------------------------------------------------------------------


def mine(comments: list[dict], viewer: str | None, submitted: list[dict]) -> list[dict]:
    """Your earlier comments on the PR (thread starters, not replies): written by you, or posted by PR Prism for you."""
    me = (viewer or "").lower()
    ids = {r.get("review_id") for r in submitted if r.get("review_id")}
    return [c for c in comments if not c.get("in_reply_to_id") and not c.get("is_bot") and (
        (me and str(c.get("user") or "").lower() == me) or c.get("id") in ids or c.get("review_id") in ids)]


def comment_lines(c: dict) -> tuple[int, int] | None:
    """The new-side lines a comment covers now, or None: outdated, or on a deleted line."""
    line = c.get("line")
    if not isinstance(line, int) or (c.get("side") or "RIGHT") != "RIGHT":
        return None
    start = c.get("start_line")
    return (start if isinstance(start, int) and 0 < start <= line else line), line


def in_interdiff(c: dict, files: list[dict]) -> bool:
    span = comment_lines(c)
    return span is not None and touches(files, c["path"], *span)


def recheck_results(pr_dir: Path, head: str) -> dict:
    """The re-check results of the analysis of `head` (recheck.json), or {} when they are from another commit."""
    saved = read_json(pr_dir / "recheck.json") or {}
    return (saved.get("results") or {}) if saved.get("head_sha") == head else {}


def comment_statuses(comments: list[dict], files: list[dict], results: dict, running: bool) -> list[dict]:
    """[{id, path, line, status, note}] for your earlier comments. Outdated ones, and ones the interdiff touches, take
    the re-check result: fixed, still-open, not-checked (over recheck_max, or no result for this commit yet), or
    checking while an analysis runs. The rest are line-unchanged."""
    out = []
    for c in comments:
        row = {"id": c["id"], "path": c["path"], "line": c.get("line"), "status": "line-unchanged", "note": None}
        if c.get("line") is None or in_interdiff(c, files):
            r = results.get(f"c-{c['id']}")
            if running:
                row["status"] = "checking"
            elif r is None:
                row.update(status="not-checked", note=NOT_YET)
            elif r.get("not_checked"):
                row["status"] = "not-checked"
                row["limit"] = True
            else:
                row.update(status=r["status"], note=r.get("how"))
        out.append(row)
    over = sum(1 for r in out if r.pop("limit", False))
    for r in out:
        if r["status"] == "not-checked" and r["note"] is None:
            r["note"] = LIMIT.format(n=over)
    return out


def for_recheck(pr_dir: Path, ctx: dict, comments: list[dict]) -> tuple[set[str], list[dict]]:
    """For the analysis's re-check (prr/analyze.py): the re-check ids ("c-<id>") of your own earlier comments, which go
    first within recheck_max, and an item for each of them that isn't outdated but whose lines the new commits
    touched. Nothing before your first submitted review, or when the head hasn't moved since."""
    from .recheck import comment_entry

    state = StateStore(pr_dir).load()
    reviewed = state.get("reviewed_head")
    if not reviewed or reviewed == ctx["pr"]["head_sha"]:
        return set(), []
    try:
        viewer = viewer_login()
    except Exception:  # noqa: BLE001 — fall back to the comments PR Prism posted
        viewer = None
    yours = mine(comments, viewer, state.get("submitted") or [])
    diff = changes(pr_dir, ctx, state)
    files = diff["files"] if diff["ok"] else []
    items = []
    for c in yours:
        entry = comment_entry(c) if in_interdiff(c, files) else None
        if entry:
            items.append({**entry, "line": c["line"], "changed_nearby": True})
    return {f"c-{c['id']}" for c in yours}, items


# --------------------------------------------------------------------------------------
# Re-reading the PR
# --------------------------------------------------------------------------------------


def _time(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def estimate(pr_dir: Path) -> dict | None:
    """{minutes, usd} of the last complete analysis run, from calls.jsonl, or None when no run has completed. A run is
    complete when progress.json says so (the latest run), or, for older runs, when its summary call succeeded."""
    runs: dict[str, dict] = {}
    for x in ledger.read(pr_dir):
        rid = x.get("run_id")
        if not x.get("purpose") or not isinstance(rid, str) or rid == cost.BEFORE_LEDGER:
            continue
        r = runs.setdefault(rid, {"usd": 0.0, "start": None, "end": None, "done": False})
        spent = x.get("cost_usd")
        r["usd"] += spent if isinstance(spent, (int, float)) else 0.0
        end = _time(x.get("at"))
        if end is not None:
            took = x.get("duration_ms")
            start = end - (took / 1000 if isinstance(took, (int, float)) else 0)
            r["start"] = start if r["start"] is None else min(r["start"], start)
            r["end"] = end if r["end"] is None else max(r["end"], end)
        if x.get("purpose") == "synthesis" and x.get("outcome") == "ok":
            r["done"] = True
    prog = read_json(pr_dir / "progress.json") or {}
    latest = runs.get(prog.get("run_id"))
    if latest is not None:
        latest["done"] = prog.get("status") == "complete"
        began, ended = _time(prog.get("started_at")), _time(prog.get("finished_at"))
        if began is not None and ended is not None:
            latest["start"], latest["end"] = began, ended
    done = [rid for rid, r in runs.items() if r["done"] and r["start"] is not None]
    if not done:
        return None
    r = runs[max(done)]  # run ids sort by start time
    return {"minutes": max(1, round((r["end"] - r["start"]) / 60)), "usd": round(r["usd"], 2)}


def analysis_running(pr_dir: Path) -> bool:
    """An analysis (or a re-read, which holds analyze.pid from its fetch on) is working on this PR right now."""
    pid = (read_json(pr_dir / "analyze.pid") or {}).get("pid")
    return isinstance(pid, int) and pid_alive(pid)


def start_reread(pr_dir: Path, ctx: dict) -> subprocess.Popen:
    """Run `prr _reanalyze <PR>` (fetch, then analyze) in the background, in its own session like the server's
    supervisor, so it outlives a server restart. Its output goes to analyze.log."""
    pr = ctx["pr"]
    url = f"https://github.com/{pr['owner']}/{pr['repo']}/pull/{pr['number']}"
    with open(pr_dir / "analyze.log", "a") as log:
        return subprocess.Popen([sys.executable, "-P", "-m", "prr", "--repo", str(ctx["repo_root"]), "_reanalyze", url],
                                stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd=pr_dir)
