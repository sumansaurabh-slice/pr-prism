"""labels.jsonl: what the reviewer said is a real problem (or not), so `prr eval` can score the analysis against it.

The file lives at <cache>/<owner>__<repo>/labels.jsonl, beside the PR folders, so `prr clean` never touches it. It is
an append-only log: every write adds lines, nothing rewrites it, and readers compact it (below).

Where labels come from:
- posted: each comment the app's Submit or the chat's post_comment / post_review sent to GitHub (a problem);
- imported: `prr labels import` reads the root comments already on past PRs (problems);
- verdict: guided review's answers, real → problem, not → not-problem (and nothing for "not sure");
- a pending comment made from a "Real problem" answer and deleted before it was posted appends `retracted`.
Dismissing a finding is not a label: it can mean "true, but not worth a comment".

One line: {v, id, pr, commit_id, merge_base_sha, path, side, line, kind, text, author, source, origin_id,
gh_comment_id, finding_id, stop_kind, split, at}. `v` is the format version. `stop_kind` is the guided-review stop a
verdict answered (finding, risk-note, bot, ci, look_for), or null. `split` is train or holdout: held-out PRs are kept
out of prompt design and reported on their own.

Compaction, so one issue counts once:
1. A line's key is its origin_id (the stop it came from) when present, else its gh_comment_id; a line with both links
   them, so a verdict, the comment posted from it and a later import of that comment share one key.
2. The latest line per key wins, and `retracted` removes the key.
3. Problem labels on the same (pr, commit_id, path, side) at most one line apart are one label (the latest), which
   catches an import of a comment that was posted from the app.

The review server and the chat's MCP process both append, so writes take labels.lock (waiting at most 5 s). The file
is created 0600, like state.json.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import findings, github
from .gitgh import CommandError, PRRef, find_remote, gh_json, git, git_ok
from .util import now_iso

VERSION = 1
LABELS_FILE = "labels.jsonl"
LOCK_FILE = "labels.lock"
LOCK_TIMEOUT_S = 5.0
KINDS = ("problem", "not-problem", "retracted")
SOURCES = ("verdict", "posted", "imported")
SPLITS = ("train", "holdout")
NO_EXACT_LINE = ("ci", "look_for")  # verdicts on these stops are kept, but they have no exact line to find
SAVE_FAILED = "Couldn't save this answer for eval"
_PR_FOLDER = re.compile(r"pr-\d+")


def repo_dir(cache_dir: Path, owner: str, repo: str) -> Path:
    """The folder that holds one repo's PR folders and its labels.jsonl."""
    return cache_dir / f"{owner}__{repo}"


def repo_dir_for(pr_dir: Path) -> Path | None:
    """The repo folder of a PR folder in the cache (<cache>/<owner>__<repo>/pr-<n>), or None for any other folder
    (an eval run's copy, a test's temp folder): only real PR folders get labels."""
    if _PR_FOLDER.fullmatch(pr_dir.name) and "__" in pr_dir.parent.name:
        return pr_dir.parent
    return None


# --------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------


def new_label(*, pr: int, kind: str, source: str, commit_id: str | None = None, merge_base_sha: str | None = None,
              path: str | None = None, side: str | None = None, line: int | None = None, text: str = "",
              author: str | None = None, origin_id: str | None = None, gh_comment_id: int | None = None,
              finding_id: str | None = None, stop_kind: str | None = None, split: str = "train",
              at: str | None = None) -> dict[str, Any]:
    """One label line. Raises ValueError for an unknown kind, source or split."""
    if kind not in KINDS:
        raise ValueError(f"unknown label kind {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown label source {source!r}")
    if split not in SPLITS:
        raise ValueError(f"split must be train or holdout, not {split!r}")
    return {"v": VERSION, "id": uuid.uuid4().hex[:12], "pr": int(pr), "commit_id": commit_id,
            "merge_base_sha": merge_base_sha, "path": path, "side": side if side in ("LEFT", "RIGHT") else None,
            "line": line if isinstance(line, int) else None, "kind": kind, "text": text or "", "author": author,
            "source": source, "origin_id": origin_id, "gh_comment_id": gh_comment_id, "finding_id": finding_id,
            "stop_kind": stop_kind, "split": split, "at": at or now_iso()}


@contextmanager
def _locked(folder: Path, timeout: float) -> Iterator[None]:
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(folder / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"{LOCK_FILE} stayed busy for {timeout:.0f} s") from None
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write(folder: Path, labels: list[dict[str, Any]], timeout: float = LOCK_TIMEOUT_S) -> None:
    """Append label lines (from new_label) to the repo folder's labels.jsonl, in one write under labels.lock.
    Raises TimeoutError when the lock stays busy, OSError when the disk refuses."""
    if not labels:
        return
    data = "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in labels).encode()
    with _locked(folder, timeout):
        fd = os.open(folder / LABELS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
        finally:
            os.close(fd)


def append(folder: Path, *, pr: int, kind: str, source: str, **fields: Any) -> dict[str, Any]:
    """Add one label to the repo folder's labels.jsonl and return it. Fields as in new_label. Verdicts use
    kind="problem" (real) or "not-problem" (not) with source="verdict", origin_id=<stop id> and stop_kind."""
    label = new_label(pr=pr, kind=kind, source=source, **fields)
    write(folder, [label])
    return label


def save(pr_dir: Path, labels: list[dict[str, Any]]) -> str | None:
    """Append labels for the PR in `pr_dir`, never raising: a failed write is logged and returns SAVE_FAILED for the
    app's toast (what was posted or answered still counts; only eval misses it)."""
    folder = repo_dir_for(pr_dir)
    if not labels or folder is None:
        return None
    try:
        write(folder, labels)
    except (OSError, TimeoutError) as e:
        print(f"[prr] could not save {len(labels)} label(s) to {folder / LABELS_FILE}: {e}", file=sys.stderr, flush=True)
        return SAVE_FAILED
    return None


def for_posted(pr: dict, comments: list[dict], *, commit_id: str | None, author: str | None) -> list[dict[str, Any]]:
    """Problem labels for comments just posted to GitHub. Each comment: {path, line, side, body, gh_comment_id,
    origin_id?}. The merge base is known only when they went on the analyzed head."""
    merge_base = pr.get("merge_base_sha") if commit_id and commit_id == pr.get("head_sha") else None
    return [new_label(pr=pr["number"], kind="problem", source="posted", commit_id=commit_id, merge_base_sha=merge_base,
                      path=c.get("path"), side=c.get("side") or "RIGHT", line=c.get("line"), text=c.get("body") or "",
                      author=author, origin_id=c.get("origin_id"), gh_comment_id=c.get("gh_comment_id"))
            for c in comments]


def posted_drafts(drafts: list[dict], gh_comments: list[dict]) -> list[dict]:
    """The comments a review just posted, from the drafts it sent, with GitHub's comment ids where GitHub returned
    them (`gh_comments`: the review's comments as GitHub lists them). A draft that went into the review body, outside
    the diff, has no comment id of its own."""
    left = list(gh_comments)
    out = []
    for d in drafts:
        if d.get("outdated"):
            continue
        body = (d.get("body") or "").strip()
        hit = next((c for c in left if c.get("path") == d.get("path") and (c.get("body") or "").strip() == body
                    and c.get("line") in (d.get("line"), None)), None) if d.get("anchored") else None
        if hit:
            left.remove(hit)
        out.append({"path": d.get("path"), "line": d.get("line"), "side": d.get("side"), "body": d.get("body") or "",
                    "origin_id": d.get("origin_id"), "gh_comment_id": hit.get("id") if hit else None})
    return out


def retracted(pr: dict, draft: dict) -> list[dict[str, Any]]:
    """A pending comment made from a "Real problem" answer was deleted before it was posted: take the answer back."""
    if not draft.get("origin_id"):
        return []
    return [new_label(pr=pr["number"], kind="retracted", source="verdict", origin_id=draft["origin_id"],
                      commit_id=draft.get("commit_id"), path=draft.get("path"), side=draft.get("side"),
                      line=draft.get("line"))]


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def read(folder: Path) -> list[dict[str, Any]]:
    """Every line of the repo folder's labels.jsonl, oldest first (a damaged line is skipped)."""
    try:
        text = (folder / LABELS_FILE).read_text(errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("kind") in KINDS and isinstance(obj.get("pr"), int):
            out.append(obj)
    return out


def load(folder: Path) -> list[dict[str, Any]]:
    """The compacted labels: one per issue."""
    return compact(read(folder))


def _keys(entries: list[dict]) -> list[str]:
    alias = {e["gh_comment_id"]: f"o:{e['origin_id']}" for e in entries
             if e.get("origin_id") and e.get("gh_comment_id") is not None}
    keys = []
    for n, e in enumerate(entries):
        if e.get("origin_id"):
            keys.append(f"o:{e['origin_id']}")
        elif e.get("gh_comment_id") is not None:
            keys.append(alias.get(e["gh_comment_id"], f"c:{e['gh_comment_id']}"))
        else:
            keys.append(f"i:{e.get('id') or n}")
    return keys


def _same_place(a: dict, b: dict) -> bool:
    if not (isinstance(a.get("line"), int) and isinstance(b.get("line"), int)):
        return False
    return all(a.get(k) == b.get(k) for k in ("pr", "commit_id", "path", "side")) and abs(a["line"] - b["line"]) <= 1


_CARRY = ("origin_id", "gh_comment_id", "finding_id", "stop_kind", "merge_base_sha", "author")


def _carry(newer: dict, older: dict) -> dict:
    """The newer line, with what only the older one knew (an import of a posted comment keeps the stop it came from)."""
    return {**newer, **{k: older[k] for k in _CARRY if newer.get(k) in (None, "") and older.get(k) not in (None, "")}}


def compact(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One label per issue (see the module docstring), oldest first. `entries` is in file order."""
    latest: dict[str, dict] = {}
    for key, e in zip(_keys(entries), entries):
        older = latest.pop(key, None)  # re-inserted, so the dict ends up ordered by each key's latest line
        latest[key] = _carry(e, older) if older else dict(e)
    alive = [e for e in latest.values() if e["kind"] != "retracted"]
    kept: list[dict] = []
    for e in reversed(alive):  # newest first: the latest line at a place wins
        at = next((i for i, k in enumerate(kept) if e["kind"] == k["kind"] == "problem" and _same_place(e, k)), None)
        if at is None:
            kept.append(e)
        else:
            kept[at] = _carry(kept[at], e)
    return list(reversed(kept))


def countable(label: dict) -> bool:
    """Counts toward recall: a problem at a code line. CI and look_for answers have no exact line, so they don't."""
    return (label["kind"] == "problem" and bool(label.get("path")) and isinstance(label.get("line"), int)
            and label.get("stop_kind") not in NO_EXACT_LINE)


def match_label(label: dict, finding: dict) -> bool:
    """The finding is about the labelled issue: the app's own "same issue" rule (findings.near_same), comparing the
    finding's title and impact with the label's text."""
    return findings.near_same(finding["path"], finding["line"], f"{finding['title']} {finding.get('impact') or ''}",
                              label.get("path") or "", label.get("line"), label.get("text") or "")


# --------------------------------------------------------------------------------------
# `prr labels import`: comments already on past PRs (read-only on GitHub)
# --------------------------------------------------------------------------------------

REVIEWERS = ("me", "all")
_META_FIELDS = "number,state,baseRefName,baseRefOid"


def _have_commit(root: Path, sha: str) -> bool:
    return git_ok("cat-file", "-e", f"{sha}^{{commit}}", cwd=root)


def merge_base_for(root: Path, remote: str, ref: PRRef, meta: dict, commit: str) -> str | None:
    """Where the PR's diff starts for `commit` (the fetch.py rule): the PR's recorded base commit (baseRefOid) for a
    merged or closed PR, whose head is already in the base branch; `git merge-base <commit> <base>` for an open one,
    asking GitHub's compare API when git can't tell. Fetches the commits it needs (fetching only reads from GitHub).
    None when nothing works."""
    if meta.get("state") != "OPEN":
        return meta.get("baseRefOid")
    base_ref = f"refs/pr-prism/{ref.number}/base"
    try:
        if not _have_commit(root, commit):
            git("fetch", "--no-tags", "--quiet", remote, commit, cwd=root)
        git("fetch", "--no-tags", "--quiet", "--force", remote, f"+refs/heads/{meta['baseRefName']}:{base_ref}", cwd=root)
        return git("merge-base", commit, base_ref, cwd=root).strip()
    except CommandError:
        pass
    try:
        data = gh_json("api", f"repos/{ref.slug}/compare/{meta['baseRefName']}...{commit}",
                       "--jq", "{sha: .merge_base_commit.sha}")
        return (data or {}).get("sha") or None
    except CommandError:
        return None


def _where(c: dict) -> tuple[str | None, int | None]:
    """(commit, line) the comment was written on: GitHub's original_commit_id / original_line, which stay put when the
    PR moves on (an outdated comment has line null). A comment without an original line uses its current one."""
    if isinstance(c.get("original_line"), int):
        return c.get("original_commit_id") or c.get("commit_id"), c["original_line"]
    return c.get("commit_id") or c.get("original_commit_id"), c.get("line") if isinstance(c.get("line"), int) else None


def import_comments(ref: PRRef, root: Path, folder: Path, *, reviewers: str = "me", viewer: str | None = None,
                    split: str = "train") -> dict[str, Any]:
    """The root review comments already on a PR, as problem labels in `folder`'s labels.jsonl: yours
    (`reviewers="me"`, with `viewer` your GitHub login) or every human reviewer's (`"all"`; bot accounts never).
    Only reads from GitHub. A comment imported before with the same place, text and split is not added again.
    Returns a summary for the command's output."""
    if reviewers not in REVIEWERS:
        raise ValueError("--reviewers must be me or all")
    if reviewers == "me" and not viewer:
        raise ValueError("importing your own comments needs your GitHub login")
    me = (viewer or "").lower()
    meta = gh_json("pr", "view", str(ref.number), "--repo", ref.slug, "--json", _META_FIELDS)
    comments = [c for c in github.existing_comments(ref.owner, ref.repo, ref.number)
                if not c.get("in_reply_to_id") and not c.get("is_bot")
                and (reviewers == "all" or (c.get("user") or "").lower() == me)]
    remote = find_remote(root, ref)
    bases: dict[str, str | None] = {}
    seen = {(x.get("gh_comment_id"), x.get("commit_id"), x.get("line"), x.get("text"), x.get("split"))
            for x in read(folder) if x.get("source") == "imported"}
    rows, again = [], 0
    for c in comments:
        commit, line = _where(c)
        if commit and commit not in bases:
            bases[commit] = merge_base_for(root, remote, ref, meta, commit)
        if (c["id"], commit, line, c.get("body") or "", split) in seen:
            again += 1
            continue
        rows.append(new_label(pr=ref.number, kind="problem", source="imported", commit_id=commit,
                              merge_base_sha=bases.get(commit) if commit else None, path=c.get("path"),
                              side=c.get("side") or "RIGHT", line=line, text=c.get("body") or "", author=c.get("user"),
                              gh_comment_id=c["id"], split=split))
    write(folder, rows)
    return {"pr": ref.number, "imported": len(rows), "already_imported": again,
            "outdated": sum(1 for c in comments if c.get("line") is None), "commits": len(bases),
            "no_merge_base": sum(1 for b in bases.values() if b is None), "reviewers": reviewers, "split": split}
