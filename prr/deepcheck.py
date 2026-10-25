"""Deep check ("what could break"): after the summary, an Opus agent questions the riskiest areas of the PR.

- `pick_targets` ranks the summary's chapters, plus flows that touch high-risk files, by risk. Areas that share most
  of their files are checked as one, and only the riskiest few are kept. Without chapters, the file groups are used.
- Each area is one structured call with Read/Grep/Glob inside the worktree. A finding must give a concrete scenario
  with example values, one yes/no question for the reviewer, and the spots it read (`evidence_refs`).
- Areas run 3 at a time inside a time budget. A failed area retries once if the budget still has a full area's time
  left. Nothing starts after the budget, and areas still running 60 s later are stopped (the `claude` process is
  killed). Every failure, refusal, malformed output and skipped area is named in `analysis.warnings`. A valid empty
  result is "checked, nothing found".
- As each area finishes, its findings are placed and de-duplicated, shown with "second look pending", scored by the
  second look (just those findings) and shown again. Scores from areas that finish together merge into judge.json
  under one lock.

Files in the PR folder:
- deepcheck.json: `cache` (area results by prompt version, focus, model and the area's file blobs) and `run`, this
  analysis run's areas. An area's status is waiting | running | checked | nothing-found | failed | stopped |
  skipped; checked and nothing-found both count as checked, and an area past the cap is skipped with `capped`.
- placed_pre_deepcheck.json: the findings just before the deep check merged, shaped like analysis.json findings.
  Eval's "deep check off" view comes from it.
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import findings, judge, ledger, rubrics
from .claude import ClaudeError, run_structured
from .cluster import CHARS_PER_TOKEN
from .config import Config
from .diffparse import FileDiff, render_for_llm
from .models import DeepCheckOut, Finding, llm_schema
from .util import now_iso, read_json, write_json

PROMPT_VERSION = "1"    # bump when prompts/deepcheck.md changes: cached results are keyed on it
SOURCE = "deep-check"
FILE = "deepcheck.json"
SNAPSHOT_FILE = "placed_pre_deepcheck.json"
PARALLEL = 3            # areas checked at the same time
MAX_TURNS = 60
GRACE_S = 60.0          # areas still running this long after the budget are stopped
JUDGE_WAIT_S = 120.0    # then, how long to wait for second looks already under way
DIFF_TOKENS = 30_000    # the diff shown per area; the rest is listed by path
RISK_POINTS = {"high": 3, "medium": 1}
RISK_ORDER = {"high": 0, "medium": 1, "low": 2}
SENSITIVE_POINTS = 2
MERGE_OVERLAP = 0.5     # two areas sharing this share of the smaller one's files are checked as one
SKIP_CATEGORIES = ("docs", "test", "generated")
CHECKED = ("checked", "nothing-found")
DONE = (*CHECKED, "failed", "stopped", "skipped")
MAX_CACHE = 60          # cached area results kept in deepcheck.json


# --------------------------------------------------------------------------------------
# Picking the areas
# --------------------------------------------------------------------------------------


@dataclass
class Target:
    id: str
    title: str
    files: list[str]
    goal: str = ""
    narrative: str = ""
    score: int = 0
    high: bool = False      # has a high-risk file


def _score(t: Target, risk: dict[str, str | None], sensitive: set[str], shown: Counter) -> Target:
    """High-risk file 3, medium 1, sensitive path +2, each finding shown on it +1."""
    t.score = sum(RISK_POINTS.get(risk.get(p) or "", 0) + (SENSITIVE_POINTS if p in sensitive else 0) + shown[p]
                  for p in t.files)
    t.high = any(risk.get(p) == "high" for p in t.files)
    return t


def _overlap(a: list[str], b: list[str]) -> float:
    sa, sb = set(a), set(b)
    return len(sa & sb) / min(len(sa), len(sb)) if sa and sb else 0.0


def _merge_overlapping(targets: list[Target]) -> list[Target]:
    """Riskiest first: an area sharing at least half of the smaller area's files joins the earlier one."""
    kept: list[Target] = []
    for t in targets:
        twin = next((k for k in kept if _overlap(k.files, t.files) >= MERGE_OVERLAP), None)
        if twin is None:
            kept.append(t)
            continue
        twin.id = f"{twin.id}+{t.id}"
        twin.title = f"{twin.title} + {t.title}"
        twin.files += [p for p in t.files if p not in twin.files]
        twin.goal = " ".join(x for x in (twin.goal, t.goal) if x)
        twin.narrative = "\n\n".join(x for x in (twin.narrative, t.narrative) if x)
    return kept


def _from_synthesis(synthesis: dict, resolve: Any, eligible: set[str], risk: dict[str, str | None]) -> list[Target]:
    """One area per chapter, plus one per flow that touches a high-risk file."""
    out = []
    for i, ch in enumerate(synthesis.get("chapters") or [], 1):
        files = _paths([cf.get("path") for cf in ch.get("files") or []], resolve, eligible)
        if files:
            out.append(Target(f"chapter:{ch.get('id') or i}", ch.get("title") or f"Part {i}", files,
                              goal=ch.get("goal") or ""))
    for i, fl in enumerate(synthesis.get("flows") or [], 1):
        files = _paths([n.get("path") for n in fl.get("nodes") or []], resolve, eligible)
        if any(risk.get(p) == "high" for p in files):
            out.append(Target(f"flow:{fl.get('id') or i}", fl.get("title") or f"Flow {i}", files,
                              narrative=fl.get("narrative") or ""))
    return out


def _from_clusters(manifest: dict, eligible: set[str]) -> list[Target]:
    """Without chapters (the summary failed): one area per group of files the first pass read together."""
    out = []
    for i, c in enumerate(manifest.get("clusters") or [], 1):
        files = list(dict.fromkeys(it["path"] for it in c.get("items") or [] if it.get("path") in eligible))
        if files:
            names = ", ".join(p.rsplit("/", 1)[-1] for p in files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            out.append(Target(f"group:{c.get('id') or i}", f"Group {i}: {names}", files))
    return out


def _paths(raw: list[Any], resolve: Any, eligible: set[str]) -> list[str]:
    return list(dict.fromkeys(p for p in map(resolve, raw) if p in eligible))


def _rank(targets: list[Target]) -> list[Target]:
    return sorted((t for t in targets if t.score > 0), key=lambda t: -t.score)


def pick_targets(placed: list[dict], synthesis: dict | None, ctx: dict, *, risk: dict[str, str | None],
                 clusters: dict | None = None, max_targets: int = 4) -> tuple[list[Target], list[Target]]:
    """(the areas to check, riskiest first; the risky areas past the cap). Areas with nothing risky are left out.

    `placed` are the findings so far (annotate and blockers, after the first second look); `risk` is each file's
    risk from the file notes; `clusters` is clusters.json, used when the summary has no chapters."""
    from .merge import PathResolver

    resolve = PathResolver([f["path"] for f in ctx["files"]])
    eligible = {f["path"] for f in ctx["files"]
                if f["category"] not in SKIP_CATEGORIES and not f["generated"] and not f["binary"] and f["hunks"]}
    sensitive = {f["path"] for f in ctx["files"] if f["sensitive"]}
    shown = Counter(f["path"] for f in placed if not f.get("low_confidence"))
    targets = _from_synthesis(synthesis or {}, resolve, eligible, risk) if (synthesis or {}).get("chapters") else []
    if not targets:
        targets = _from_clusters(clusters or {}, eligible)
    ranked = _rank([_score(t, risk, sensitive, shown) for t in targets])
    merged = _rank([_score(t, risk, sensitive, shown) for t in _merge_overlapping(ranked)])
    return merged[:max_targets], merged[max_targets:]


# --------------------------------------------------------------------------------------
# What one area's agent is given
# --------------------------------------------------------------------------------------


def _pick_hunks(files: list[str], diffs: dict[str, FileDiff], risk: dict[str, str | None],
                budget_tokens: int) -> dict[str, set[str]]:
    """Whole hunks, riskiest files first and then the biggest hunks, while they fit in about `budget_tokens`."""
    hunks = []
    for i, p in enumerate(files):
        fd = diffs.get(p)
        for h in (fd.hunks if fd and not fd.binary else []):
            size = len(render_for_llm(fd, {h.id}))
            hunks.append((RISK_ORDER.get(risk.get(p) or "", 3), -size, i, h.id, p, size))
    budget, used = budget_tokens * CHARS_PER_TOKEN, 0
    chosen: dict[str, set[str]] = {}
    for *_, hid, p, size in sorted(hunks):
        if used + size <= budget:
            chosen.setdefault(p, set()).add(hid)
            used += size
    return chosen


def diff_section(files: list[str], diffs: dict[str, FileDiff], risk: dict[str, str | None],
                 budget_tokens: int = DIFF_TOKENS) -> tuple[str, list[str]]:
    """The area's diff with R/L line tags, cut to about `budget_tokens` (see _pick_hunks). Returns the diff, in the
    area's file order, and what was left out, by path, so the agent can Read it."""
    chosen = _pick_hunks(files, diffs, risk, budget_tokens)
    out, left = [], []
    for p in files:
        fd, ids = diffs.get(p), chosen.get(p, set())
        if fd is None or not ids:
            left.append(f"`{p}` (whole file)")
            continue
        out.append(f"\n### {p}\n({fd.status}, +{fd.additions}/-{fd.deletions})\n```\n{render_for_llm(fd, ids)}\n```\n")
        missing = [h.id for h in fd.hunks if h.id not in ids]
        if missing:
            left.append(f"`{p}` (hunks {', '.join(missing)})")
    return "".join(out), left


def target_prompt(ctx: dict, t: Target, files_view: dict[str, dict], diffs: dict[str, FileDiff],
                  risk: dict[str, str | None], already: list[dict], focus: str | None, conventions: str = "") -> str:
    from .analyze import _file_map, _pr_header, _template

    head = (_template("deepcheck.md", focus)
            .replace("{{CONVENTIONS}}", conventions or "(The repo has no CLAUDE.md or AGENTS.md.)"))
    parts = [head, "\n---\n", _pr_header(ctx), f"\n## The area to check: {t.title}\n"]
    if t.goal:
        parts.append(f"Goal: {t.goal}\n")
    if t.narrative:
        parts.append(f"\nHow data moves here:\n{t.narrative}\n")
    parts.append("\n## Files in this area\n")
    for p in t.files:
        f = files_view.get(p) or {}
        meta = [x for x in (f.get("category"), f.get("status"), f.get("risk") and f"risk {f['risk']}") if x]
        parts.append(f"- `{p}` [{' · '.join(meta)}]\n")
        if f.get("summary"):
            parts.append(f"  {f['summary'][:400].replace(chr(10), ' ')}\n")
    diff, left = diff_section(t.files, diffs, risk)
    parts.append("\n## The diff for this area\n" + (diff or "(no text diff)\n"))
    if left:
        parts.append("\n## Not shown above (Read these when you need them)\n" + "".join(f"- {x}\n" for x in left))
    if already:
        parts.append("\n## Already reported in this area (don't repeat these)\n"
                     + "".join(f"- [{f['severity']}] {f['path']}:{f['line']} {f['title']}\n" for f in already[:20]))
    parts.append("\n## All changed files in this PR (→ marks this area; the others are context)\n"
                 + _file_map(ctx, set(t.files)) + "\n")
    return "".join(parts)


def cache_key(t: Target, ctx: dict, model: str, focus: str | None) -> str:
    """sha256 of the prompt version, the focus, the model and the area's files with their base and head blobs."""
    files = {f["path"]: f for f in ctx["files"]}
    blobs = sorted(f"{p}:{files[p].get('base_blob') or ''}:{files[p].get('head_blob') or ''}" for p in t.files if p in files)
    raw = "|".join([PROMPT_VERSION, focus or "", model, *blobs])
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------------------
# What merge.py reads: this run's results and warnings
# --------------------------------------------------------------------------------------


def _current(pr_dir: Path) -> dict | None:
    """deepcheck.json when its run is the PR's current analysis run (progress.json), else None: a new run hides the
    last run's deep-check findings until its own deep check has them."""
    data = read_json(pr_dir / FILE) or {}
    run = data.get("run") or {}
    run_id = (read_json(pr_dir / "progress.json") or {}).get("run_id")
    return data if run_id and run.get("run_id") == run_id else None


def found(pr_dir: Path) -> list[tuple[str, dict]]:
    """(path, raw finding) for every area this run has checked, in the order they finished."""
    data = _current(pr_dir)
    if data is None:
        return []
    cache = data.get("cache") or {}
    done = sorted((t for t in data["run"].get("targets", []) if t.get("status") in CHECKED and t.get("key")),
                  key=lambda t: t.get("finished_seq") or 0)
    return [(f["path"], {**f, "source": "claude", "sources": [SOURCE]})
            for t in done for f in (cache.get(t["key"]) or {}).get("findings", [])]


def warnings(pr_dir: Path) -> list[str]:
    data = _current(pr_dir)
    return list(data["run"].get("warnings", [])) if data else []


def write_snapshot(pr_dir: Path, ctx: dict) -> list[dict]:
    """placed_pre_deepcheck.json: the findings before the deep check merges, in analysis.json's finding shape."""
    from .merge import build_findings

    out = [Finding.model_validate(f).model_dump(mode="json") for f in build_findings(pr_dir, ctx)[0]]
    write_json(pr_dir / SNAPSHOT_FILE, out)
    return out


# --------------------------------------------------------------------------------------
# Running the areas
# --------------------------------------------------------------------------------------


class JudgeStore:
    """judge.json while areas finish in parallel. Each area's scores merge into one in-memory cache, and the file is
    written from that merged cache, both under one lock: one area's scores never overwrite another's."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.cache: dict = read_json(path) or {}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.cache)

    def merge(self, scores: dict) -> None:
        with self.lock:
            self.cache.update(scores)
            write_json(self.path, self.cache)


def _took(seconds: float) -> str:
    s = int(round(seconds))
    return f"{s // 60}m {s % 60}s" if s >= 60 else f"{s}s"


def _reason(e: Exception, limit: float) -> str:
    """Plain words for why one try failed: refusal, malformed output, time-out and errors each read differently."""
    if isinstance(e, ValueError):
        return f"malformed output ({str(e).splitlines()[0][:120]})"
    if getattr(e, "outcome", "") == "refused":
        return "the model gave no result (no structured output)"
    if getattr(e, "outcome", "") == "timeout":
        return f"timed out after {_took(limit)}"
    return str(e)[:200]


class _Run:
    """One analysis run's deep check: its areas, the shared caches, and the lock every write goes through."""

    def __init__(self, pr_dir: Path, ctx: dict, cfg: Config, diffs: dict[str, FileDiff], progress: Any,
                 focus: str | None, force: bool, view: dict):
        self.pr_dir, self.ctx, self.cfg, self.diffs, self.progress = pr_dir, ctx, cfg, diffs, progress
        self.focus, self.force = focus, force
        self.worktree = Path(ctx["worktree"])
        self.model = cfg.models.deepcheck
        self.files_view = {f["path"]: f for f in view["files"]}
        self.risk = {f["path"]: f["risk"] for f in view["files"]}
        self.shown = [f for f in view["findings"] if not f["low_confidence"]]
        self.conventions = ""  # the repo's CLAUDE.md, set once areas are picked
        self.lock = threading.Lock()
        saved = read_json(pr_dir / FILE) or {}
        self.cache: dict = saved.get("cache") if isinstance(saved.get("cache"), dict) else {}
        self.run: dict = {"run_id": progress.ledger.run_id, "enabled": cfg.analysis.deepcheck, "model": self.model,
                          "started_at": now_iso(), "finished_at": None, "targets": [], "warnings": []}
        self.recs: dict[str, dict] = {}
        self.judged = JudgeStore(pr_dir / "judge.json")
        self.closed = False
        self.seq = 0
        self.timeout = float(cfg.analysis.deepcheck_target_timeout_s)
        self.deadline = time.monotonic() + cfg.analysis.deepcheck_budget_s
        self.hard = self.deadline + GRACE_S

    # ---- state ----

    def save(self) -> None:
        """deepcheck.json (call with the lock held, or before any area runs)."""
        keep = sorted(self.cache.items(), key=lambda kv: kv[1].get("at") or "", reverse=True)[:MAX_CACHE]
        self.cache = dict(keep)
        write_json(self.pr_dir / FILE, {"cache": self.cache, "run": self.run})

    def warn(self, text: str) -> None:
        self.run["warnings"].append(text)

    def start(self, chosen: list[Target], skipped: list[Target]) -> None:
        for t, capped in [(t, False) for t in chosen] + [(t, True) for t in skipped]:
            rec = {"id": t.id, "title": t.title, "files": t.files, "score": t.score, "high": t.high,
                   "status": "skipped" if capped else "waiting", "reason": "over the area limit" if capped else None,
                   "capped": capped, "attempts": 0, "duration_ms": None, "cost_usd": 0.0, "findings": 0, "key": None,
                   "cached": False}
            self.recs[t.id] = rec
            self.run["targets"].append(rec)
        cap = self.cfg.analysis.deepcheck_max_targets
        for t in skipped:
            if t.high:
                self.warn(f'Deep check skipped "{t.title}": it checks at most {cap} areas per PR, riskiest first.')
        self.save()

    def write_partial(self) -> None:
        """analysis.json as it stands now (call with the lock held)."""
        from .merge import build_analysis

        write_json(self.pr_dir / "analysis.json", build_analysis(self.pr_dir))

    def report(self, msg: str) -> None:
        """progress.json: areas done, and the deep check's cost so far, in total and per area."""
        lines = [x for x in ledger.read(self.pr_dir)
                 if x.get("run_id") == self.run["run_id"] and x.get("purpose") == "deepcheck"]
        with self.lock:
            for rec in self.recs.values():
                rec["cost_usd"] = round(sum(x.get("cost_usd") or 0.0 for x in lines if x.get("target") == rec["id"]), 4)
            chosen = [r for r in self.recs.values() if not r["capped"]]
            done = sum(1 for r in chosen if r["status"] in DONE)
            per_area = [{k: r[k] for k in ("id", "title", "status", "cost_usd")} for r in chosen]
        self.progress.update(msg, deepcheck_done=done,
                             deepcheck_cost_usd=round(sum(x.get("cost_usd") or 0.0 for x in lines), 4),
                             deepcheck_targets=per_area)

    # ---- one area ----

    def check(self, t: Target) -> None:
        """Ask, place, show as pending, second look, show again. Runs in a worker thread; never raises."""
        rec = self.recs[t.id]
        try:
            key = cache_key(t, self.ctx, self.model, self.focus)
            hit = None if self.force else self.cache.get(key)
            result = hit["findings"] if hit else self.ask(t, rec)
            if result is not None:
                self.finish(t, rec, key, result, cached=hit is not None)
        except Exception as e:  # noqa: BLE001 — a bug in one area must not sink the others
            self.fail(t, rec, "failed", f"PR Prism hit an error: {str(e)[:200]}")

    def ask(self, t: Target, rec: dict) -> list[dict] | None:
        """The area's findings, or None when it failed, was stopped or never started (each named in warnings)."""
        already = [f for f in self.shown if f["path"] in t.files]
        prompt = target_prompt(self.ctx, t, self.files_view, self.diffs, self.risk, already, self.focus,
                               self.conventions)
        last: tuple[int, str, float, bool] | None = None
        for attempt in (1, 2):
            now = time.monotonic()
            if now >= self.deadline or (attempt == 2 and self.deadline - now < self.timeout):
                break  # no start, and no retry, once the budget can't cover it
            out, last = self.attempt(t, rec, prompt, attempt, min(self.timeout, self.hard - now))
            if out is not None:
                return out
        if last is None:
            self.fail(t, rec, "skipped", "the time budget ran out before it could start",
                      f'Deep check skipped "{t.title}": the time budget ran out before it could start.')
            return None
        attempt, reason, took, stopped = last
        where = f"area {t.id}, model {self.model}, try {attempt}, {_took(took)}"
        if stopped:
            self.fail(t, rec, "stopped", reason, f'Deep check stopped "{t.title}" ({where}): it ran past the time budget.')
        else:
            self.fail(t, rec, "failed", reason, f'Deep check couldn\'t finish "{t.title}" ({where}): {reason}.')
        return None

    def attempt(self, t: Target, rec: dict, prompt: str, attempt: int,
                limit: float) -> tuple[list[dict] | None, tuple[int, str, float, bool]]:
        with self.lock:
            rec.update(status="running", attempts=attempt)
        started = time.monotonic()
        try:
            raw, _ = run_structured(prompt, llm_schema(DeepCheckOut), cwd=self.worktree, model=self.model,
                                    max_turns=MAX_TURNS, timeout=limit, ledger=self.progress.ledger,
                                    purpose="deepcheck", target=t.id, validate=DeepCheckOut.model_validate)
            out = DeepCheckOut.model_validate(raw).model_dump(mode="json")["findings"]
            with self.lock:
                rec["duration_ms"] = int((time.monotonic() - started) * 1000)
            return out, (attempt, "", 0.0, False)
        except (ClaudeError, ValueError) as e:
            took = time.monotonic() - started
            stopped = getattr(e, "outcome", "") == "timeout" and limit < self.timeout  # cut short by the budget
            reason = _reason(e, limit)
            with self.lock:
                rec["duration_ms"] = int(took * 1000)
            self.report(f'Deep check: "{t.title}" try {attempt} failed: {reason}')
            return None, (attempt, reason, took, stopped)

    def fail(self, t: Target, rec: dict, status: str, reason: str, warning: str | None = None) -> None:
        with self.lock:
            if self.closed:
                return
            rec.update(status=status, reason=reason)
            self.warn(warning or f'Deep check couldn\'t finish "{t.title}" (area {t.id}, model {self.model}): {reason}.')
            self.save()
        self.report(warning or f'Deep check: "{t.title}" failed: {reason}')

    def finish(self, t: Target, rec: dict, key: str, result: list[dict], *, cached: bool) -> None:
        from .merge import PathResolver, file_lines, placed_findings

        resolve = PathResolver([f["path"] for f in self.ctx["files"]])
        kept = [{**f, "path": p} for f in result for p in [resolve(f.get("path"))] if p]
        fls = file_lines(self.pr_dir)
        mine = {findings.place({**f, "sources": [SOURCE]}, f["path"], fls.get(f["path"]))["id"] for f in kept}
        with self.lock:
            if self.closed:
                return
            if not cached:
                self.cache[key] = {"target": t.id, "model": self.model, "findings": kept, "at": now_iso()}
            self.seq += 1
            rec.update(status="checked" if kept else "nothing-found", key=key, findings=len(kept), cached=cached,
                       finished_seq=self.seq, reason=None)
            self.save()
            pending = [f for f in placed_findings(self.pr_dir, self.ctx) if f["id"] in mine]
            self.write_partial()  # these findings show as "second look pending"
        dropped = len(result) - len(kept)
        self.report(f'Deep check: "{t.title}" ' + (f"found {len(kept)} possible problem(s)" if kept
                                                   else "checked, nothing found")
                    + (" (from earlier results)" if cached else "")
                    + (f"; left out {dropped} that pointed outside the changed files" if dropped else ""))
        if pending:
            scores = judge.run(pending, self.diffs, self.cfg, self.worktree, self.judged.snapshot(),
                               self.progress.update, ledger=self.progress.ledger)
            self.judged.merge({f["id"]: scores[f["id"]] for f in pending if f["id"] in scores})
        with self.lock:
            if self.closed:
                return
            rec["judged"] = True
            self.write_partial()  # now with their second look

    def close(self) -> None:
        """Name every area that never finished, and stop listening to late workers."""
        with self.lock:
            for rec in self.recs.values():
                if rec["status"] == "waiting":
                    rec.update(status="skipped", reason="the time budget ran out before it could start")
                    self.warn(f'Deep check skipped "{rec["title"]}": the time budget ran out before it could start.')
                elif rec["status"] == "running":
                    rec.update(status="stopped", reason="ran past the time budget")
                    self.warn(f'Deep check stopped "{rec["title"]}" (area {rec["id"]}, model {self.model}, '
                              f'try {rec["attempts"]}): it ran past the time budget.')
            self.closed = True
            self.run["finished_at"] = now_iso()
            self.save()


def run(pr_dir: Path, ctx: dict, cfg: Config, diffs: dict[str, FileDiff], progress: Any,
        focus: str | None = None, force: bool = False) -> None:
    """The deep check stage of `prr analyze`, after the summary. Never raises: a problem becomes a warning.

    `progress` is analyze.Progress (its `update` and its `ledger`)."""
    try:
        _run(pr_dir, ctx, cfg, diffs, progress, focus, force)
    except Exception as e:  # noqa: BLE001 — the deep check adds to the review; it must not sink it
        msg = f"The deep check stopped with an error: {str(e)[:200]}"
        data = read_json(pr_dir / FILE) or {}
        run_state = data.get("run") or {}
        if run_state.get("run_id") == progress.ledger.run_id:
            run_state.setdefault("warnings", []).append(msg)
            write_json(pr_dir / FILE, data)
        progress.update(msg)


def _run(pr_dir: Path, ctx: dict, cfg: Config, diffs: dict[str, FileDiff], progress: Any, focus: str | None,
         force: bool) -> None:
    from .merge import build_analysis

    if cfg.analysis.deepcheck:
        progress.update("Deep check: picking the riskiest areas", status="deepchecking", deepcheck_total=0,
                        deepcheck_done=0, deepcheck_cost_usd=0.0)
    view = build_analysis(pr_dir)
    r = _Run(pr_dir, ctx, cfg, diffs, progress, focus, force, view)
    r.save()                          # this run has no deep-check findings yet
    write_snapshot(pr_dir, ctx)       # "deep check off", for eval
    if not cfg.analysis.deepcheck:
        progress.update("Deep check is off ([analysis] deepcheck = false)")
        return
    chosen, skipped = pick_targets(view["findings"], read_json(pr_dir / "synthesis.json"), ctx, risk=r.risk,
                                   clusters=read_json(pr_dir / "clusters.json"),
                                   max_targets=cfg.analysis.deepcheck_max_targets)
    r.start(chosen, skipped)
    r.conventions = rubrics.repo_conventions(Path(ctx["repo_root"]), ctx["pr"]["merge_base_sha"]) if chosen else ""
    if not chosen:
        r.close()
        r.report("Deep check: no risky areas to check")
        return
    progress.update(f"Deep check: asking what could break in {len(chosen)} area{'s' if len(chosen) != 1 else ''}",
                    deepcheck_total=len(chosen))
    pool = ThreadPoolExecutor(max_workers=PARALLEL)
    futures = [pool.submit(r.check, t) for t in chosen]
    wait(futures, timeout=max(0.0, r.hard + JUDGE_WAIT_S - time.monotonic()))
    pool.shutdown(wait=False, cancel_futures=True)
    r.close()
    checked = sum(1 for rec in r.recs.values() if rec["status"] in CHECKED)
    r.report(f"Deep check done: {checked} of {len(chosen)} areas checked")
