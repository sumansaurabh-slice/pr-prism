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
gh_comment_id, finding_id, stop_kind, split, written_by, written_by_from, at}. `v` is the format version. `stop_kind`
is the guided-review stop a verdict answered (finding, risk_note, bot, ci, look_for), or null. `side` and `line` are
GitHub's: a comment on a deleted line is LEFT, with the old file's line number. `split` names a set of PRs: train (the
default) or any other name (SPLIT_NAME), such as holdout or holdout2. PRs outside train are kept out of prompt design,
reported on their own, and each such split gets its own gate in `prr eval`. A PR is in one split as a whole (pr_splits).

Who wrote the label's text, `written_by`, so eval can report recall on the reviewer's own comments: a comment that
PR Prism wrote and the reviewer only clicked through is one the analysis "finds" by itself.
- human: the reviewer wrote it;
- tool: PR Prism wrote it: a finding's ready-made comment, the chat's text or a stop's pre-filled comment, posted
  unchanged or edited only a little, or a "real" answer on a finding or risk-note stop;
- unknown: can't tell (an imported comment not in PR Prism's format, another tool's stop, a line from before this).
`written_by_from` is how it is known, surest first: set (you said so: `--written-by` or `prr labels tag`), app (from
the pending comment that was posted, or the stop that was answered), format (an imported comment's text: PR Prism's
finding format, PRISM_FORMAT, is tool, anything else unknown; the writing style is never guessed from).
A posted comment (draft_writer) is human when the reviewer typed it (a "user" draft that didn't start as generated
text), or typed more than 30% of its words (EDITED_SHARE: the posted words that don't line up, in order, with the text
PR Prism generated, the draft's `generated_body`). Removing words alone never makes it human.

Compaction, so one issue counts once:
1. A line's key is its origin_id (the stop it came from) when present, else its gh_comment_id; a line with both links
   them, so a verdict, the comment posted from it and a later import of that comment share one key.
2. The latest line per key wins, and `retracted` removes the key.
3. Problem labels on the same (pr, commit_id, path, side) at most one line apart are one label (the latest), which
   catches an import of a comment that was posted from the app.
4. Of the lines merged into one label, the surest written_by wins (the latest of equally sure ones), so an import of a
   comment posted from the app keeps what the app knew. `prr labels tag` re-tags by appending a copy of the label (same
   id) with the new written_by or split.

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
from difflib import SequenceMatcher
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
TRAIN = "train"                     # the default split; every other split name is held out
ALL = "all"                         # `prr eval --split all`: every split, so it can't name one
SPLIT_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}")
WRITERS = ("human", "tool", "unknown")
SURE = {"set": 3, "app": 2, "format": 1}  # written_by_from, surest first
EDITED_SHARE = 0.3                  # a posted PR Prism comment is human when more of its words are the reviewer's
PRISM_STOPS = ("finding", "risk_note")  # stops PR Prism raised itself: a "real" answer on one is the tool's issue
NO_EXACT_LINE = ("ci", "look_for")  # verdicts on these stops are kept, but they have no exact line to find
SAVE_FAILED = "Couldn't save this answer for eval"
SHA = re.compile(r"[0-9a-f]{40}")
_PR_FOLDER = re.compile(r"pr-\d+")
_WORD = re.compile(r"[a-z0-9]+")
# findings.comment_body: a bold title line, a blank line, then "*Must fix · correctness*" (severity · category)
_SEVERITY_WORDS = "|".join(re.escape(s) for s in (*findings.SEVERITY_TEXT.values(), "Nit"))
PRISM_FORMAT = re.compile(r"\s*\*\*[^*\s][^\n]*\*\*[ \t]*\r?\n[ \t]*\r?\n"
                          rf"\*(?:{_SEVERITY_WORDS}) · [^*\n]+\*[ \t]*(?:\r?\n|\Z)")


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


def check_split(name: str) -> str:
    """`name` when it can name a split (train, holdout, holdout2…), else ValueError."""
    if not isinstance(name, str) or not SPLIT_NAME.fullmatch(name) or name == ALL:
        raise ValueError(f"a split is named with letters, digits, '.', '_' or '-' (and isn't {ALL!r}), not {name!r}")
    return name


def check_writer(written_by: str) -> str:
    if written_by not in WRITERS:
        raise ValueError(f"written_by must be human, tool or unknown, not {written_by!r}")
    return written_by


def new_label(*, pr: int, kind: str, source: str, commit_id: str | None = None, merge_base_sha: str | None = None,
              path: str | None = None, side: str | None = None, line: int | None = None, text: str = "",
              author: str | None = None, origin_id: str | None = None, gh_comment_id: int | None = None,
              finding_id: str | None = None, stop_kind: str | None = None, split: str = TRAIN,
              written_by: str = "unknown", written_by_from: str | None = None, at: str | None = None) -> dict[str, Any]:
    """One label line. Raises ValueError for an unknown kind, source, split or writer."""
    if kind not in KINDS:
        raise ValueError(f"unknown label kind {kind!r}")
    if source not in SOURCES:
        raise ValueError(f"unknown label source {source!r}")
    check_split(split)
    check_writer(written_by)
    if written_by_from is not None and written_by_from not in SURE:
        raise ValueError(f"written_by_from must be set, app or format, not {written_by_from!r}")
    return {"v": VERSION, "id": uuid.uuid4().hex[:12], "pr": int(pr), "commit_id": commit_id,
            "merge_base_sha": merge_base_sha, "path": path, "side": side if side in ("LEFT", "RIGHT") else None,
            "line": line if isinstance(line, int) else None, "kind": kind, "text": text or "", "author": author,
            "source": source, "origin_id": origin_id, "gh_comment_id": gh_comment_id, "finding_id": finding_id,
            "stop_kind": stop_kind, "split": split, "written_by": written_by, "written_by_from": written_by_from,
            "at": at or now_iso()}


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


def append(folder: Path, *, pr: int, kind: str, source: str, commit_id: str | None = None,
           merge_base_sha: str | None = None, path: str | None = None, side: str | None = None, line: int | None = None,
           text: str = "", author: str | None = None, origin_id: str | None = None, gh_comment_id: int | None = None,
           finding_id: str | None = None, stop_kind: str | None = None, split: str = TRAIN,
           written_by: str = "unknown", written_by_from: str | None = None) -> dict[str, Any]:
    """Add one label to the repo folder's labels.jsonl and return it. A guided-review verdict is
    kind="problem" (real) or "not-problem" (not), source="verdict", origin_id=<stop id>, stop_kind=<the stop's kind>
    and finding_id when the stop is a finding. Raises ValueError, TimeoutError (lock busy) or OSError."""
    label = new_label(pr=pr, kind=kind, source=source, commit_id=commit_id, merge_base_sha=merge_base_sha, path=path,
                      side=side, line=line, text=text, author=author, origin_id=origin_id, gh_comment_id=gh_comment_id,
                      finding_id=finding_id, stop_kind=stop_kind, split=split, written_by=written_by,
                      written_by_from=written_by_from)
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
    except Exception as e:  # noqa: BLE001 — lock timeout, full disk, a body JSON can't encode: never fail the caller
        print(f"[prr] could not save {len(labels)} label(s) to {folder / LABELS_FILE}: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return SAVE_FAILED
    return None


def for_posted(pr: dict, comments: list[dict], *, commit_id: str | None, author: str | None) -> list[dict[str, Any]]:
    """Problem labels for comments just posted to GitHub. Each comment: {path, line, side, body, gh_comment_id,
    origin_id?, written_by?} (written_by from draft_writer; unknown without it). The merge base is known only when
    they went on the analyzed head."""
    merge_base = pr.get("merge_base_sha") if commit_id and commit_id == pr.get("head_sha") else None
    return [new_label(pr=pr["number"], kind="problem", source="posted", commit_id=commit_id, merge_base_sha=merge_base,
                      path=c.get("path"), side=c.get("side") or "RIGHT", line=c.get("line"), text=c.get("body") or "",
                      author=author, origin_id=c.get("origin_id"), gh_comment_id=c.get("gh_comment_id"),
                      written_by=c["written_by"] if c.get("written_by") in WRITERS else "unknown",
                      written_by_from="app" if c.get("written_by") in WRITERS else None)
            for c in comments]


def posted_drafts(drafts: list[dict], gh_comments: list[dict]) -> list[dict]:
    """The comments a review just posted, from the drafts it sent, with GitHub's comment ids where GitHub returned
    them (`gh_comments`: the review's comments as GitHub lists them). A draft that went into the review body, outside
    the diff, has no comment id of its own. Each says who wrote it (draft_writer)."""
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
                    "origin_id": d.get("origin_id"), "gh_comment_id": hit.get("id") if hit else None,
                    "written_by": draft_writer(d)})
    return out


def for_verdict(pr: dict, stop: dict, verdict: str | None, previous: str | None, *,
                author: str | None) -> list[dict[str, Any]]:
    """The label a guided-review answer makes, keyed by the stop id (origin_id): real → problem, not → not-problem.
    "Not sure" (or taking the answer back: `verdict` None) makes none, so it retracts an earlier real or not answer.
    `stop` is the stop, or for one that is gone, where it was ({id, kind, path, side, line, finding_id}). The issue
    is PR Prism's (written_by tool) when the stop is its own finding or risk note, else unknown (another tool's)."""
    kind = {"real": "problem", "not": "not-problem"}.get(verdict or "")
    if kind is None and previous not in ("real", "not"):
        return []
    where = {"pr": pr["number"], "source": "verdict", "commit_id": pr.get("head_sha"), "path": stop.get("path"),
             "side": stop.get("side"), "line": stop.get("line"), "origin_id": stop["id"],
             "finding_id": stop.get("finding_id"), "stop_kind": stop.get("kind")}
    if kind is None:
        return [new_label(kind="retracted", **where)]
    return [new_label(kind=kind, merge_base_sha=pr.get("merge_base_sha"), author=author,
                      text=stop.get("comment_body") or stop.get("question") or "",
                      written_by="tool" if stop.get("kind") in PRISM_STOPS else "unknown", written_by_from="app",
                      **where)]


def retracted(pr: dict, draft: dict) -> list[dict[str, Any]]:
    """A pending comment made from a "Real problem" answer was deleted before it was posted: take the answer back."""
    if not draft.get("origin_id"):
        return []
    return [new_label(pr=pr["number"], kind="retracted", source="verdict", origin_id=draft["origin_id"],
                      commit_id=draft.get("commit_id"), path=draft.get("path"), side=draft.get("side"),
                      line=draft.get("line"))]


# --------------------------------------------------------------------------------------
# Who wrote it
# --------------------------------------------------------------------------------------


def writer(label: dict) -> str:
    """The label's written_by; unknown for a line from before it was recorded (`prr labels tag` fills those in)."""
    return label.get("written_by") if label.get("written_by") in WRITERS else "unknown"


def text_writer(text: str) -> str:
    """An imported comment's writer from its text alone: tool when it is in PR Prism's finding format (a bold title
    line, then "*Must fix · category*"), else unknown. A short plain comment may well be the reviewer's, but the
    writing style is never taken as proof."""
    return "tool" if PRISM_FORMAT.match(text or "") else "unknown"


def typed_share(generated: str, body: str) -> float:
    """The share of `body`'s words the reviewer typed: those that don't line up, in order, with the generated text.
    0 for the text unchanged or only cut down; 1 for a body with no words that isn't the generated text."""
    got = _WORD.findall(body.lower())
    if not got:
        return 0.0 if body.strip() == generated.strip() else 1.0
    made = _WORD.findall(generated.lower())
    kept = sum(b.size for b in SequenceMatcher(None, made, got, autojunk=False).get_matching_blocks())
    return 1 - kept / len(got)


def draft_writer(draft: dict) -> str:
    """Who wrote a pending comment that is being posted. One that started as PR Prism's text (`generated_body`: a
    finding's comment, the chat's, or a stop's pre-filled one) is tool unless the reviewer typed more than
    EDITED_SHARE of its words, then human. Otherwise source "user" is human (the reviewer typed it), and a
    "suggestion" or "chat" draft made before its generated text was kept is unknown."""
    generated = draft.get("generated_body")
    if isinstance(generated, str):
        return "human" if typed_share(generated, draft.get("body") or "") > EDITED_SHARE else "tool"
    return "unknown" if draft.get("source") in ("suggestion", "chat") else "human"


def guessed_writer(label: dict) -> tuple[str, str]:
    """(written_by, written_by_from) for a label saved before written_by was: a verdict from its stop (PR Prism's own
    finding or risk note → tool, else unknown); a posted or imported comment from its format."""
    if label.get("source") == "verdict":
        return ("tool" if label.get("stop_kind") in PRISM_STOPS else "unknown"), "app"
    return text_writer(label.get("text") or ""), "format"


def _sure(label: dict) -> int:
    """How surely the label's written_by is known (SURE); -1 for a line without one."""
    return SURE.get(label.get("written_by_from") or "", 0) if label.get("written_by") in WRITERS else -1


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
    """One issue told twice: the same spot (at most a line apart), and not two different comments or stops."""
    if not (isinstance(a.get("line"), int) and isinstance(b.get("line"), int)):
        return False
    if any(a.get(k) is not None and b.get(k) is not None and a[k] != b[k] for k in ("gh_comment_id", "origin_id")):
        return False
    return all(a.get(k) == b.get(k) for k in ("pr", "commit_id", "path", "side")) and abs(a["line"] - b["line"]) <= 1


_CARRY = ("origin_id", "gh_comment_id", "finding_id", "stop_kind", "merge_base_sha", "author")


def _carry(newer: dict, older: dict) -> dict:
    """The newer line, with what only the older one knew (an import of a posted comment keeps the stop it came from)
    and the older one's written_by when that is surer (an import of a comment posted from the app keeps what the app
    knew about who wrote it)."""
    out = {**newer, **{k: older[k] for k in _CARRY if newer.get(k) in (None, "") and older.get(k) not in (None, "")}}
    if _sure(older) > _sure(newer):
        out["written_by"], out["written_by_from"] = older["written_by"], older.get("written_by_from")
    return out


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


def pr_splits(entries: list[dict]) -> dict[int, str]:
    """Each PR's split, so a PR is in one split as a whole: the latest split other than train among its labels, else
    train (a label saved from the app, such as a posted comment, is train by default)."""
    out: dict[int, str] = {}
    for x in entries:
        name = x.get("split") or TRAIN
        if name != TRAIN or x["pr"] not in out:
            out[x["pr"]] = name
    return out


def match_label(label: dict, finding: dict) -> bool:
    """The finding is about the labelled issue: the app's own "same issue" rule (findings.near_same), comparing the
    finding's title and impact with the label's text. Like that rule, it doesn't compare sides: a label on a deleted
    line (LEFT, the old file's line number) matches a finding placed on that deleted line, such as an "intent"
    question about removed code, since both use the old file's numbers."""
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
    """Where the PR's diff starts for `commit`, by the fetch.py rule: `git merge-base <commit> <base point>`, where the
    base point is the base branch for an open PR and the PR's recorded base commit (baseRefOid) for a merged or closed
    one, whose head is already in the base branch. Asks GitHub's compare API when git can't tell; a merged or closed
    PR then falls back to baseRefOid itself. Fetches the commits it needs (fetching only reads from GitHub).
    None when nothing works."""
    closed = meta.get("state") != "OPEN"
    recorded = meta.get("baseRefOid") if SHA.fullmatch(meta.get("baseRefOid") or "") else None
    if not SHA.fullmatch(commit or ""):  # only full commit ids ever reach git's command line
        return recorded if closed else None
    try:
        if not _have_commit(root, commit):
            git("fetch", "--no-tags", "--quiet", remote, commit, cwd=root)
        if closed:
            if not recorded:
                return None
            if not _have_commit(root, recorded):
                git("fetch", "--no-tags", "--quiet", remote, recorded, cwd=root)
            point = recorded
        else:
            point = f"refs/pr-prism/{ref.number}/base"
            git("fetch", "--no-tags", "--quiet", "--force", remote, f"+refs/heads/{meta['baseRefName']}:{point}", cwd=root)
        return git("merge-base", commit, point, cwd=root).strip()
    except CommandError:
        pass
    try:
        data = gh_json("api", f"repos/{ref.slug}/compare/{recorded if closed else meta['baseRefName']}...{commit}",
                       "--jq", "{sha: .merge_base_commit.sha}")
        found = (data or {}).get("sha")
        if found:
            return found
    except CommandError:
        pass
    return recorded if closed else None


def _where(c: dict) -> tuple[str | None, int | None]:
    """(commit, line) the comment was written on: GitHub's original_commit_id / original_line, which stay put when the
    PR moves on (an outdated comment has line null). A comment without an original line uses its current one."""
    if isinstance(c.get("original_line"), int):
        return c.get("original_commit_id") or c.get("commit_id"), c["original_line"]
    return c.get("commit_id") or c.get("original_commit_id"), c.get("line") if isinstance(c.get("line"), int) else None


def _side(c: dict) -> str:
    """The side the comment is on: GitHub's original_side, else its side (a comment on a deleted line is LEFT, and its
    line is the old file's). When GitHub gives neither, a diff hunk that ends on a deleted line means LEFT."""
    for key in ("original_side", "side"):
        if c.get(key) in ("LEFT", "RIGHT"):
            return c[key]
    last = (c.get("diff_hunk") or "").rstrip("\n").rsplit("\n", 1)[-1]
    return "LEFT" if last.startswith("-") else "RIGHT"


def _import_key(x: dict) -> tuple:
    return x.get("gh_comment_id"), x.get("commit_id"), x.get("line"), x.get("text"), x.get("split")


def import_comments(ref: PRRef, root: Path, folder: Path, *, reviewers: str = "me", viewer: str | None = None,
                    split: str = TRAIN, written_by: str | None = None) -> dict[str, Any]:
    """The root review comments already on a PR, as problem labels in `folder`'s labels.jsonl: yours
    (`reviewers="me"`, with `viewer` your GitHub login) or every human reviewer's (`"all"`; bot accounts never).
    Who wrote each one is `written_by` when given (set by you: e.g. human for PRs reviewed before PR Prism existed),
    else its format's guess (text_writer). Only reads from GitHub. A comment imported before with the same place,
    text and split is not added again (with `written_by`, unless it was not yet set to that).
    Returns a summary for the command's output."""
    if reviewers not in REVIEWERS:
        raise ValueError("--reviewers must be me or all")
    if reviewers == "me" and not viewer:
        raise ValueError("importing your own comments needs your GitHub login")
    check_split(split)
    if written_by is not None:
        check_writer(written_by)
    me = (viewer or "").lower()
    meta = gh_json("pr", "view", str(ref.number), "--repo", ref.slug, "--json", _META_FIELDS)
    comments = [c for c in github.existing_comments(ref.owner, ref.repo, ref.number)
                if not c.get("in_reply_to_id") and not c.get("is_bot")
                and (reviewers == "all" or (c.get("user") or "").lower() == me)]
    remote = find_remote(root, ref)
    bases: dict[str, str | None] = {}
    before = [x for x in read(folder) if x.get("source") == "imported"]
    seen = {_import_key(x) for x in before}
    told = {(*_import_key(x), x.get("written_by")) for x in before if x.get("written_by_from") == "set"}
    rows, again = [], 0
    for c in comments:
        commit, line = _where(c)
        if commit and commit not in bases:
            bases[commit] = merge_base_for(root, remote, ref, meta, commit)
        key = (c["id"], commit, line, c.get("body") or "", split)
        if (key in seen) if written_by is None else ((*key, written_by) in told):
            again += 1
            continue
        rows.append(new_label(pr=ref.number, kind="problem", source="imported", commit_id=commit,
                              merge_base_sha=bases.get(commit) if commit else None, path=c.get("path"),
                              side=_side(c), line=line, text=c.get("body") or "", author=c.get("user"),
                              gh_comment_id=c["id"], split=split,
                              written_by=written_by or text_writer(c.get("body") or ""),
                              written_by_from="set" if written_by else "format"))
    write(folder, rows)
    return {"pr": ref.number, "imported": len(rows), "already_imported": again,
            "outdated": sum(1 for c in comments if c.get("line") is None), "commits": len(bases),
            "no_merge_base": sum(1 for b in bases.values() if b is None), "reviewers": reviewers, "split": split,
            "written_by": {w: sum(1 for r in rows if r["written_by"] == w) for w in WRITERS}}


# --------------------------------------------------------------------------------------
# `prr labels tag`: re-tag the labels already in the log
# --------------------------------------------------------------------------------------


def tag(folder: Path, prs: list[int] | None = None, *, written_by: str | None = None,
        split: str | None = None) -> dict[str, Any]:
    """Re-tag the labels of `prs` (every labelled PR when None) by appending a copy of each changed label, with the
    same id, so compaction takes the copy as its latest line:
    - `written_by`: who wrote them, set by you (no later guess overrides it);
    - `split`: move the PRs to that split;
    - every label still without a written_by (saved before it existed) gets it from its format (guessed_writer).
    Setting a writer or a split needs `prs`. Returns {prs, labels, retagged, written_by: {human, tool, unknown}}."""
    if written_by is not None:
        check_writer(written_by)
    if split is not None:
        check_split(split)
    if (written_by or split) and not prs:
        raise ValueError("say which PRs to re-tag (--prs)")
    now = now_iso()
    picked, rows = [], []
    for x in load(folder):
        if prs and x["pr"] not in prs:
            continue
        picked.append(x)
        new = dict(x)
        if written_by:
            new.update(written_by=written_by, written_by_from="set")
        elif x.get("written_by") not in WRITERS:
            new["written_by"], new["written_by_from"] = guessed_writer(x)
        if split:
            new["split"] = split
        if new != x:
            rows.append({**new, "at": now})
    write(folder, rows)
    final = {x["id"]: x for x in picked} | {x["id"]: x for x in rows}
    return {"prs": sorted({x["pr"] for x in picked}), "labels": len(picked), "retagged": len(rows),
            "written_by": {w: sum(1 for x in final.values() if writer(x) == w) for w in WRITERS}}
