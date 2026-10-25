"""Understand this PR: a guided walk, step by step, for a reviewer who doesn't know the repo. Each step explains what
the code did before, what changes, why, where it connects, and the words a newcomer needs. It explains; it doesn't
judge.

**Steps** (`build_steps(analysis, ctx)`, pure and deterministic) follow the reading order: chapters, then each
chapter's files.
- One step per changed file. A big file (over SPLIT_LINES changed lines) is split at hunk boundaries into "part" steps
  of about SPLIT_LINES changed lines each; a single huge hunk stays one step.
- A test file attaches to the step of the code it tests (`tests_for`, from prr/classify.py `pair_tests`) as one of its
  `tests` ("Tested by"), on every part of a split file. A test with no such step is a step of its own.
- Generated, lock, vendored and binary files, and files with no text change (a pure rename or mode change), are not
  steps: they are listed as `skim` on their chapter's first step. A chapter with nothing else lists them on the walk's
  first step.
- At most MAX_STEPS steps. Past that, a chapter's low-risk files (and files without notes) become one "Other changes
  in this chapter" step (`kind: "group"`, at the chapter's end), in the chapter that saves the most steps first. If
  that is not enough, medium-risk files join their chapter's group, then high-risk ones.
- A step's id is "und-" + sha1(path | hunk range | head blob) (a deleted file: its base blob; a group: its paths and
  blobs), so it stays the same across re-runs until that code changes.

**A step's text** (`explain`) is made only when the reviewer opens the step, never during the analysis: one
structured call (`[models] understand`, Read/Grep/Glob inside the worktree only, at most MAX_TURNS turns and TIMEOUT_S
seconds) given the step's diff, the file at the merge-base around each hunk (`git show <merge_base>:<path>`, never the
head), the PR's title, description and commit subjects, the chapter, the file's summary and notes, the callers
impact.json found, and the reader's level ("new": explain the repo's ideas and terms; "familiar": skip background).
Its cost goes to calls.jsonl with purpose `understand`. The output is tidied, not rejected:
- a reason without a source counts as missing (`why_missing`), so the text never gives a reason nobody stated;
- `connects` keeps up to 4 places in files that exist; `terms` up to 4, and none for "familiar";
- `look_at` keeps up to 3 lines that are in the step's diff (none for a group).

It is cached in <pr_dir>/understand/<stepId>-<level>.json under a key of the prompt version, the model, the head
blobs, the merge base, the step's hunks and the level: any change makes it again. The server holds one lock per step
and level, so two requests for the same text make one call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .analyze import MAX_COMMITS, _commits, _pr_header, style_rules
from .claude import run_structured
from .config import PROMPTS_DIR, Config
from .diffparse import FileDiff, Hunk, render_for_llm
from .gitgh import git
from .ledger import Ledger
from .models import UnderstandOut, UnderstandStep, llm_schema
from .util import now_iso, read_json, write_json

PROMPT_VERSION = "1"  # bump when prompts/understand.md or what the prompt is given changes: cached texts are keyed on it
LEVELS = ("new", "familiar")
DEFAULT_LEVEL = "new"
MAX_STEPS = 40
SPLIT_LINES = 150      # a file with more changed lines than this is split at hunk boundaries
MAX_TURNS = 12
TIMEOUT_S = 180
BASE_CONTEXT = 40      # lines of the old file shown on each side of a hunk
DIFF_CHARS = 30_000    # the step's diff in the prompt; the rest is cut
BASE_CHARS = 20_000    # … and the old version around it
MAX_LINE = 400
MAX_ITEMS = 4          # connects and terms
MAX_LOOK_AT = 3
MAX_CALLERS = 8
CACHE_DIR = "understand"
ID = re.compile(r"und-[0-9a-f]{40}")
_RISK_RANK = {"high": 2, "medium": 1}  # low and unknown are 0
_SENTENCE = re.compile(r"(.+?[.!?])(?=\s+[A-Z`*_(\"']|$)", re.S)
_KIND_TEXT = {
    "file": "one changed file",
    "part": "one part of a big file (the file's other changes are other steps)",
    "group": "a few small files from one chapter, shown together. Leave `look_at` empty",
}
LEVEL_TEXT = {
    "new": ("The reader is new to this area of the repo. Explain the repo's ideas this code relies on: what its main "
            "names stand for, and the job this code does for the rest of the app. Fill `terms` with up to 4 words "
            "they need."),
    "familiar": ("The reader knows this area of the repo. Skip background and be brief: one or two short sentences "
                 "for `before` and for `change`. Leave `terms` empty."),
}


# --------------------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------------------


def step_id(path: str, hunks: list[dict], blob: str | None) -> str:
    span = ",".join(f"{h['old_start']},{h['old_lines']},{h['new_start']},{h['new_lines']}" for h in hunks)
    return "und-" + hashlib.sha1(f"{path}|{span}|{blob or ''}".encode("utf-8")).hexdigest()


def _span(h: dict) -> dict:
    return {k: h[k] for k in ("old_start", "old_lines", "new_start", "new_lines")}


def _blob(f: dict) -> str:
    return f.get("head_blob") or f.get("base_blob") or ""


def _exact(fd: FileDiff | None, h: dict) -> Hunk | None:
    """The parsed hunk with this range, or None."""
    want = (h["old_start"], h["old_lines"], h["new_start"], h["new_lines"])
    return next((x for x in fd.hunks if (x.old_start, x.old_lines, x.new_start, x.new_lines) == want), None) \
        if fd else None


def changed_lines(path: str, h: dict, diffs: dict[str, FileDiff] | None) -> int:
    """Added and deleted lines in the hunk (its size, when the diff itself isn't at hand)."""
    x = _exact((diffs or {}).get(path), h)
    return sum(1 for ln in x.lines if ln.kind != " ") if x else max(h["old_lines"], h["new_lines"])


def split(path: str, hunks: list[dict], diffs: dict[str, FileDiff] | None, limit: int = SPLIT_LINES) -> list[list[dict]]:
    """The file's hunks as one group, or, past `limit` changed lines, as consecutive groups of at most about `limit`."""
    sizes = [changed_lines(path, h, diffs) for h in hunks]
    if sum(sizes) <= limit:
        return [hunks]
    parts: list[list[dict]] = [[]]
    size = 0
    for h, n in zip(hunks, sizes):
        if parts[-1] and size + n > limit:
            parts.append([])
            size = 0
        parts[-1].append(h)
        size += n
    return parts


def role(summary: str | None) -> str | None:
    """The first sentence of the file summary (what the file is and does), or None."""
    text = " ".join((summary or "").strip().split("\n\n", 1)[0].split())
    if not text:
        return None
    m = _SENTENCE.match(text)
    out = m.group(1) if m else text
    return out if len(out) <= 200 else out[:199].rstrip() + "…"


def is_skim(f: dict) -> bool:
    """Generated, lock and vendored files (category generated), binary files, and files with no text change."""
    return bool(f.get("generated") or f.get("binary") or not f.get("hunks"))


@dataclass
class _Unit:
    """One file's steps in its chapter."""
    path: str
    file: dict
    pos: int
    parts: list[list[dict]]
    tests: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return _RISK_RANK.get(self.file.get("risk") or "", 0)


@dataclass
class _Chapter:
    id: str
    title: str
    goal: str | None
    units: list[_Unit] = field(default_factory=list)
    grouped: list[_Unit] = field(default_factory=list)
    skim: list[str] = field(default_factory=list)

    def count(self) -> int:
        return sum(len(u.parts) for u in self.units) + (1 if self.grouped else 0)


def _cap(chapters: list[_Chapter], max_steps: int) -> None:
    """Group files into "Other changes in this chapter" until at most max_steps steps are left: low-risk files first,
    in the chapter that saves the most steps (on a tie, the later chapter), then medium-risk and high-risk files."""
    total = sum(c.count() for c in chapters)
    for level in (0, 1, 2):
        while total > max_steps:
            best, saves = None, 0
            for c in chapters:
                todo = [u for u in c.units if u.rank <= level]
                n = sum(len(u.parts) for u in todo) - (0 if c.grouped else 1)
                if todo and n > 0 and n >= saves:
                    best, saves = c, n
            if best is None:
                break
            best.grouped = sorted(best.grouped + [u for u in best.units if u.rank <= level], key=lambda u: u.pos)
            best.units = [u for u in best.units if u.rank > level]
            total -= saves


def _step(c: _Chapter, *, sid: str, path: str, hunks: list[dict], role_text: str | None, tests: list[str], kind: str,
          files: list[str]) -> dict:
    return {"id": sid, "chapter_id": c.id, "chapter_title": c.title, "chapter_goal": c.goal, "path": path,
            "hunks": hunks, "role": role_text, "tests": list(dict.fromkeys(tests)), "skim": [], "order": 0,
            "kind": kind, "files": files}


def build_steps(analysis: dict, ctx: dict | None = None, *, diffs: dict[str, FileDiff] | None = None,
                max_steps: int = MAX_STEPS) -> list[dict]:
    """The walk's steps, in reading order (see the module docstring). `analysis` is analysis.json (its chapters and
    files); `ctx` (context.json) fills in file facts the analysis lacks. `diffs` (path → parsed diff) makes the
    changed-line counts that split big files exact; without it hunk sizes stand in."""
    files = {f["path"]: f for f in (ctx or {}).get("files") or []}
    for f in analysis.get("files") or []:
        files[f["path"]] = {**files.get(f["path"], {}), **f}
    seen: set[str] = set()
    chapters: list[_Chapter] = []
    for i, ch in enumerate(analysis.get("chapters") or [], 1):
        c = _Chapter(str(ch.get("id") or f"ch{i}"), ch.get("title") or f"Part {i}", (ch.get("goal") or "").strip() or None)
        for cf in ch.get("files") or []:
            p = cf.get("path")
            if p not in files or p in seen:
                continue
            seen.add(p)
            f = files[p]
            if is_skim(f):
                c.skim.append(p)
            else:
                c.units.append(_Unit(p, f, len(c.units), split(p, [_span(h) for h in f["hunks"]], diffs)))
        chapters.append(c)

    units = {u.path: u for c in chapters for u in c.units}
    for c in chapters:  # tests join the step of the code they test
        kept = []
        for u in c.units:
            host = units.get(u.file.get("tests_for") or "")
            if host is not None and host is not u and not host.file.get("tests_for"):
                host.tests.append(u.path)
            else:
                kept.append(u)
        c.units = kept
    _cap(chapters, max_steps)

    steps: list[dict] = []
    elsewhere: list[str] = []  # skim files of chapters with no step
    for c in chapters:
        first = len(steps)
        for u in c.units:
            for hunks in u.parts:
                steps.append(_step(c, sid=step_id(u.path, hunks, _blob(u.file)), path=u.path, hunks=hunks,
                                   role_text=role(u.file.get("summary")), tests=u.tests,
                                   kind="part" if len(u.parts) > 1 else "file", files=[u.path]))
        if c.grouped:
            paths = [u.path for u in c.grouped]
            sid = step_id("group:" + "\n".join(paths), [], ",".join(_blob(u.file) for u in c.grouped))
            steps.append(_step(c, sid=sid, path=paths[0], hunks=[], role_text=None,
                               tests=[t for u in c.grouped for t in u.tests], kind="group", files=paths))
        if len(steps) > first:
            steps[first]["skim"] = list(c.skim)
        else:
            elsewhere += c.skim
    if steps and elsewhere:
        steps[0]["skim"] = list(dict.fromkeys(steps[0]["skim"] + elsewhere))
    for i, s in enumerate(steps):
        s["order"] = i
    return [UnderstandStep.model_validate(s).model_dump(mode="json") for s in steps]


# --------------------------------------------------------------------------------------
# State: the reader's level and the steps marked "Got it"
# --------------------------------------------------------------------------------------


def level_of(state: dict | None) -> str:
    level = (state or {}).get("understand_level")
    return level if level in LEVELS else DEFAULT_LEVEL


def understood(state: dict | None) -> dict[str, str]:
    """state.understood_steps ({stepId: when}), without anything malformed."""
    raw = (state or {}).get("understood_steps")
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)} if isinstance(raw, dict) else {}


def mark(state: dict, sid: str, done: bool, now: str) -> dict[str, str]:
    """Mark a step understood (keeping the first time) or not, in state.json `state`. Returns the marks."""
    marks = understood(state)
    if done:
        marks.setdefault(sid, now)
    else:
        marks.pop(sid, None)
    state["understood_steps"] = marks
    return dict(marks)


# --------------------------------------------------------------------------------------
# What the call is given
# --------------------------------------------------------------------------------------


def _step_hunks(step: dict, files: dict[str, dict]) -> dict[str, list[dict]]:
    """path → the hunks the step covers: its own, or for a group, all of each file's."""
    if step["kind"] != "group":
        return {step["path"]: [_span(h) for h in step["hunks"]]}
    return {p: [_span(h) for h in (files.get(p) or {}).get("hunks") or []] for p in step["files"]}


def base_excerpt(repo_root: str | Path, merge_base: str, path: str, hunks: list[dict],
                 context: int = BASE_CONTEXT) -> str | None:
    """The file at the merge-base around each hunk (`context` lines each side), with line numbers; None when it can't
    be read. Always `git show <merge_base>:<path>`, never the worktree, which holds the PR's head."""
    if not merge_base:
        return None
    text = git("show", f"{merge_base}:{path}", cwd=repo_root, check=False)
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if not lines:
        return None
    windows = sorted((max(1, h["old_start"] - context), min(len(lines), h["old_start"] + max(h["old_lines"], 1) - 1 + context))
                     for h in hunks)
    merged: list[list[int]] = []
    for lo, hi in windows:
        if merged and lo <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], hi)
        elif lo <= hi:
            merged.append([lo, hi])
    width = len(str(len(lines)))
    out: list[str] = []
    for lo, hi in merged:
        if out:
            out.append("…")
        for n in range(lo, hi + 1):
            t = lines[n - 1].rstrip("\r")
            out.append(f"{n:>{width}}  {t if len(t) <= MAX_LINE else t[:MAX_LINE] + ' …[cut]'}")
    return "\n".join(out) or None


def _cut(text: str, left: int) -> tuple[str, int]:
    """`text` cut to `left` characters, and what is left after it."""
    if len(text) <= left:
        return text, left - len(text)
    return (text[:left].rstrip() + "\n…[cut to save space]") if left > 0 else "(left out to save space)", 0


def _in_hunks(n: dict, side: str, hunks: list[dict]) -> bool:
    start, count = ("new_start", "new_lines") if side == "RIGHT" else ("old_start", "old_lines")
    return any(h[start] <= n["end"] and n["start"] < h[start] + max(h[count], 1) for h in hunks)


def _callers(impact: dict | None, paths: list[str]) -> list[str]:
    out = []
    for s in (impact or {}).get("symbols") or []:
        if s.get("defined_in") not in paths or s.get("too_common") or not s.get("count"):
            continue
        refs = ", ".join(f"{r['path']}:{r['line']}" + (f" `{' '.join(str(r['text']).split())[:100]}`" if r.get("text") else "")
                         for r in (s.get("outside_refs") or [])[:3])
        out.append(f"- `{s['symbol']}` (defined in {s['defined_in']}): used in {s['count']} places, e.g. {refs}\n")
    return out[:MAX_CALLERS]


def template(level: str) -> str:
    text = (PROMPTS_DIR / "understand.md").read_text()
    return text.replace("{{STYLE}}", style_rules()).replace("{{LEVEL}}", LEVEL_TEXT[level])


def prompt(step: dict, level: str, *, analysis: dict, ctx: dict, impact: dict | None = None,
           diffs: dict[str, FileDiff] | None = None) -> str:
    """The whole prompt for one step: the rules, the PR, the chapter, then per file its summary, notes, tests, the
    step's diff and the file at the merge-base around it."""
    files = {f["path"]: f for f in ctx["files"]}
    view = {f["path"]: f for f in analysis.get("files") or []}
    pr = ctx["pr"]
    base = pr.get("merge_base_sha") or ""
    out = [template(level), "\n---\n", _pr_header(ctx), _commits(ctx, MAX_COMMITS),
           f"\n## This step: {_KIND_TEXT[step['kind']]}\n",
           f'Chapter of the reading order: "{step["chapter_title"]}"'
           + (f". Its goal: {step['chapter_goal']}" if step.get("chapter_goal") else "") + "\n"]
    diff_left, base_left = DIFF_CHARS, BASE_CHARS
    for path, hunks in _step_hunks(step, files).items():
        f, v = files.get(path) or {}, view.get(path) or {}
        meta = [x for x in (f.get("status"), f.get("category"),
                            f"+{f.get('additions', 0)}/-{f.get('deletions', 0)}") if x]
        if f.get("old_path"):
            meta.append(f"renamed from {f['old_path']}")
        out.append(f"\n### {path}\n({', '.join(meta)})\n")
        if v.get("summary"):
            out.append(f"What a first read of the whole file says: {' '.join(v['summary'][:600].split())}\n")
        notes = [n for n in v.get("notes") or [] if n.get("kind") in ("explain", "flow")
                 and (step["kind"] != "part" or _in_hunks(n, n["side"], hunks))]
        if notes:
            out.append("Notes from that read:\n" + "".join(
                f"- {n['side'][0]}{n['start']}" + (f"-{n['end']}" if n["end"] != n["start"] else "")
                + f": {' '.join(n['text'].split())}\n" for n in notes[:8]))
        fd = (diffs or {}).get(path)
        ids = {x.id for h in hunks for x in [_exact(fd, h)] if x}
        diff = render_for_llm(fd, ids) if fd and ids else "(no text diff here: Read the file)"
        diff, diff_left = _cut(diff, diff_left)
        out.append(f"\nThe diff for this step:\n```\n{diff}\n```\n")
        if f.get("status") == "added" or not f.get("base_blob"):
            out.append("\nThis file is new in this PR, so it has no old version.\n")
            continue
        old = base_excerpt(ctx["repo_root"], base, f.get("old_path") or path, hunks)
        if old is None:
            out.append("\n(The old version of this file couldn't be read.)\n")
            continue
        old, base_left = _cut(old, base_left)
        out.append(f"\nThe old version around the change ({f.get('old_path') or path} at the PR's base, commit "
                   f"{base[:10]}; line numbers are the old file's):\n```\n{old}\n```\n")
    if step.get("tests"):
        out.append("\n## Tested by\n" + "".join(f"- {t}\n" for t in step["tests"]))
    callers = _callers(impact, step["files"])
    if callers:
        out.append("\n## Code outside this PR that uses the changed names\n" + "".join(callers))
    return "".join(out)


# --------------------------------------------------------------------------------------
# The call, its tidy-up and the cache
# --------------------------------------------------------------------------------------


def cache_path(pr_dir: Path, sid: str, level: str) -> Path:
    return pr_dir / CACHE_DIR / f"{sid}-{level}.json"


def cache_key(step: dict, level: str, ctx: dict, model: str) -> str:
    """sha256 of the prompt version, the model, the level, the merge base, and each file's head blob with the hunks
    the step covers."""
    files = {f["path"]: f for f in ctx["files"]}
    parts = [PROMPT_VERSION, model, level, ctx["pr"].get("merge_base_sha") or ""]
    parts += [f"{p}:{_blob(files.get(p) or {})}:{json.dumps(hunks, sort_keys=True)}"
              for p, hunks in _step_hunks(step, files).items()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]


def _rel(raw: str, root: str) -> str | None:
    """A path the model gave, relative to the worktree, or None when it points outside it."""
    p = re.sub(r":\d+$", "", raw.strip().strip("`").strip())
    if p.startswith(root + os.sep):
        p = p[len(root) + 1:]
    while p.startswith("./"):
        p = p[2:]
    if not p or p.startswith("/") or ".." in p.split("/"):
        return None
    return p


def _visible(step: dict, diffs: dict[str, FileDiff] | None) -> set[tuple[str, int]]:
    """(side, line) of every line the step's diff shows."""
    if step["kind"] == "group":
        return set()
    fd = (diffs or {}).get(step["path"])
    out: set[tuple[str, int]] = set()
    for h in step["hunks"]:
        x = _exact(fd, h)
        if x is not None:
            out |= {("RIGHT", ln.new) for ln in x.lines if ln.kind != "-" and ln.new is not None}
            out |= {("LEFT", ln.old) for ln in x.lines if ln.kind != "+" and ln.old is not None}
        else:
            out |= {("RIGHT", n) for n in range(h["new_start"], h["new_start"] + h["new_lines"])}
            out |= {("LEFT", n) for n in range(h["old_start"], h["old_start"] + h["old_lines"])}
    return out


def tidy(out: dict, step: dict, level: str, *, ctx: dict, diffs: dict[str, FileDiff] | None = None) -> dict:
    """The model's answer, kept within its limits (see the module docstring)."""
    why = " ".join((out.get("why") or "").split()) or None
    source = " ".join((out.get("why_source") or "").split()) or None
    stated = why is not None and source is not None and not out.get("why_missing")
    root = os.path.realpath(ctx["worktree"])
    known = {f["path"] for f in ctx["files"]}
    connects: list[dict] = []
    for c in out.get("connects") or []:
        p, text = _rel(c["path"], root), " ".join(c["text"].split())
        real = os.path.realpath(os.path.join(root, p)) if p else ""
        exists = bool(p) and (p in known or (real.startswith(root + os.sep) and os.path.isfile(real)))
        if exists and text and all((x["path"], x["line"]) != (p, c["line"]) for x in connects):
            connects.append({"path": p, "line": max(1, c["line"]), "text": text})
    terms = [] if level == "familiar" else [
        {"term": " ".join(t["term"].split()), "meaning": " ".join(t["meaning"].split())}
        for t in out.get("terms") or [] if t["term"].strip() and t["meaning"].strip()]
    visible = _visible(step, diffs)
    look: list[tuple[str, int]] = []
    for x in out.get("look_at") or []:
        k = (x["side"], x["line"])
        if k in visible and k not in look:
            look.append(k)
    return {"before": out["before"].strip(), "change": out["change"].strip(), "why": why if stated else None,
            "why_source": source if stated else None, "why_missing": not stated, "connects": connects[:MAX_ITEMS],
            "terms": terms[:MAX_ITEMS], "look_at": [{"side": s, "line": n} for s, n in look[:MAX_LOOK_AT]]}


def _reply(step: dict, level: str, text: dict, *, cached: bool, cost: Any) -> dict:
    return {"id": step["id"], "level": level, **text, "cached": cached,
            "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None}


def explain(pr_dir: Path, step: dict, level: str, *, analysis: dict, ctx: dict, cfg: Config,
            ledger: Ledger | None = None, diffs: dict[str, FileDiff] | None = None) -> dict:
    """The step's text for `level`: from the cache when its key matches, else one Claude call, then cached.
    `cost_usd` is what making the text cost (on a cache hit, `cached` is true and nothing new was spent).
    Raises ClaudeError or ValueError when the call fails; nothing is cached then. The caller holds the step's lock."""
    if level not in LEVELS:
        raise ValueError("level must be new or familiar")
    model = cfg.models.understand
    path, key = cache_path(pr_dir, step["id"], level), cache_key(step, level, ctx, model)
    saved = read_json(path)
    if isinstance(saved, dict) and saved.get("key") == key and isinstance(saved.get("explanation"), dict):
        return _reply(step, level, saved["explanation"], cached=True, cost=saved.get("cost_usd"))
    text = prompt(step, level, analysis=analysis, ctx=ctx, impact=read_json(pr_dir / "impact.json"), diffs=diffs)
    raw, stats = run_structured(text, llm_schema(UnderstandOut), cwd=Path(ctx["worktree"]), model=model,
                                max_turns=MAX_TURNS, timeout=TIMEOUT_S, ledger=ledger or Ledger(pr_dir),
                                purpose="understand", target=f"{step['id']}:{level}",
                                validate=UnderstandOut.model_validate)
    out = tidy(UnderstandOut.model_validate(raw).model_dump(mode="json"), step, level, ctx=ctx, diffs=diffs)
    write_json(path, {"key": key, "model": model, "at": now_iso(), "cost_usd": stats.cost_usd, "explanation": out})
    return _reply(step, level, out, cached=False, cost=stats.cost_usd)
