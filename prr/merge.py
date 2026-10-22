"""context.json + partials/*.json + synthesis.json + impact.json → analysis.json.

The model's output is repaired, not rejected: a bad anchor is clamped, moved or folded into the
file summary; an unknown path is matched by basename or dropped; a file the synthesis forgot is
still placed in a chapter. Everything that was repaired is listed in `repair_log`.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import anchors, blockers, bots, ci, diffparse, filekinds, findings, judge
from .classify import CATEGORY_ORDER, CATEGORY_TITLES
from .models import Analysis
from .util import read_json

_LEVELS = {"low": 0, "medium": 1, "high": 2}
_WHY_NOW = {
    "schema": "Read this first. Other files depend on these definitions.",
    "types": "Shows the data shapes that the next files use.",
    "core": "This is where the main behavior changes.",
    "api": "This is where the new code is called or exposed.",
    "ui": "Shows what users will see.",
    "config": "Settings that turn the change on or control it.",
    "infra": "Changes how the code is built, tested or shipped.",
    "test": "Shows which behavior the tests check.",
    "docs": "Explains the change in words.",
    "generated": "Made by a tool, not by hand. Skim it.",
}
_GOALS = {
    "schema": "Learn the data formats that the rest of the code uses.",
    "types": "Know the data shapes before you read the logic.",
    "core": "Understand the main change and the cases where it could fail.",
    "api": "See how the change is used and who calls it.",
    "ui": "Check what users will see and do.",
    "config": "Check how the change is set up and turned on.",
    "infra": "Check the effect on build, CI and deploys.",
    "test": "Make sure the risky parts have tests.",
    "docs": "Make sure the docs match the code.",
    "generated": "Only skim. Check these were made by a tool and not edited by hand.",
}


class PathResolver:
    def __init__(self, paths: list[str]):
        self.paths = set(paths)
        self.by_base: dict[str, list[str]] = defaultdict(list)
        for p in paths:
            self.by_base[posixpath.basename(p)].append(p)

    def __call__(self, raw: str | None) -> str | None:
        if not raw:
            return None
        p = raw.strip().strip("`")
        for prefix in ("./", "a/", "b/", "/"):
            if p.startswith(prefix) and p not in self.paths:
                p = p[len(prefix):]
        if p in self.paths:
            return p
        hits = self.by_base.get(posixpath.basename(p), [])
        if len(hits) == 1:
            return hits[0]
        tail = [q for q in self.paths if q.endswith("/" + p)]
        return tail[0] if len(tail) == 1 else None


def _fix_note(file_ctx: dict, note: dict) -> dict | None:
    side, start, end = note.get("side"), int(note.get("start") or 0), int(note.get("end") or 0)
    if start > end:
        start, end = end, start
    for s in ([side] if side in ("LEFT", "RIGHT") else []) + [x for x in ("RIGHT", "LEFT") if x != side]:
        h_end, h_start = anchors.hunk_for(file_ctx, s, end), anchors.hunk_for(file_ctx, s, start)
        if h_end is None and h_start is None:
            continue
        h = h_end or h_start
        lo = h["new_start"] if s == "RIGHT" else h["old_start"]
        hi = lo + (h["new_lines"] if s == "RIGHT" else h["old_lines"]) - 1
        return {**note, "side": s, "start": max(start, lo), "end": min(end, hi) if h_end is None else end}
    return None


_LINES_CACHE: dict[str, Any] = {}


def file_lines(pr_dir: Path) -> dict[str, findings.FileLines]:
    """Every file's commentable diff lines, parsed once per diff.patch version (this runs on every page poll)."""
    patch = pr_dir / "diff.patch"
    if not patch.exists():
        return {}
    key = f"{patch}:{patch.stat().st_mtime_ns}"
    if _LINES_CACHE.get("key") != key:
        parsed = diffparse.parse(patch.read_bytes().decode("utf-8", errors="replace"))
        _LINES_CACHE.update(key=key, lines={fd.path: findings.FileLines(fd) for fd in parsed})
    return _LINES_CACHE["lines"]


def _keys_by_path(pr_dir: Path) -> dict[str, list[str]]:
    manifest = read_json(pr_dir / "clusters.json") or {"clusters": []}
    out: dict[str, list[str]] = defaultdict(list)
    for c in manifest["clusters"]:
        for it in c["items"]:
            out[it["path"]].append(it["key"])
    return out


def placed_findings(pr_dir: Path, ctx: dict) -> list[dict]:
    """The reviewer's findings plus the blocker rules' proposals, placed on diff lines and de-duplicated."""
    fls = file_lines(pr_dir)
    raw: list[tuple[str, dict]] = []
    for path, keys in _keys_by_path(pr_dir).items():
        for k in keys:
            part = read_json(pr_dir / "partials" / f"{k}.json")
            if not part:
                continue
            ann = part["annotation"]
            items = ann.get("findings")
            if items is None:  # an analysis from before findings had titles, fixes and anchors
                items = [findings.from_legacy(old, fls.get(path)) for old in ann.get("suggestions", [])]
            raw += [(path, x) for x in items]
            raw += [(b["path"], blockers.to_raw_finding(b, b.get("problem"))) for b in part.get("blockers", [])]
    placed = [findings.place(x, path, fls.get(path)) for path, x in raw]
    kept, _ = findings.dedupe(placed)
    return kept


def _carry_over(r: dict, fls: dict[str, findings.FileLines]) -> dict:
    """An earlier finding that is still open but wasn't found again this round."""
    raw = {"title": r["title"], "category": r.get("category") or "correctness", "severity": r.get("severity") or "minor",
           "side": r["side"], "anchor": r["anchor"], "line": r["line"], "impact": "", "fix": "",
           "why": f"Found in the previous review round and not fixed yet: {r['how']}.", "evidence": "", "confidence": None}
    f = findings.place(raw, r["path"], fls.get(r["path"]))
    f["id"] = r["id"]
    return f


def build_findings(pr_dir: Path, ctx: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(findings, other tools' findings, re-checked earlier problems) for the analysis."""
    fls = file_lines(pr_dir)
    placed = placed_findings(pr_dir, ctx)
    scores = read_json(pr_dir / "judge.json") or {}
    saved_bots = read_json(pr_dir / "bots.json") or {}
    verdicts = saved_bots.get("verdicts", {})
    bot_found = [{**b, "verdict": (verdicts.get(str(b["comment_id"])) or {}).get("verdict"),
                  "verdict_reason": (verdicts.get(str(b["comment_id"])) or {}).get("reason")}
                 for b in saved_bots.get("found", [])]
    results = (read_json(pr_dir / "recheck.json") or {}).get("results", {})
    current = {f["id"] for f in placed}
    placed += [_carry_over(r, fls) for rid, r in results.items()
               if r["kind"] == "finding" and r["status"] == "still-open" and rid not in current]
    for f in placed:
        v = scores.get(f["id"])
        f["judge_score"] = v["score"] if v else None
        f["judge_reason"] = v["reason"] if v else None
        f["low_confidence"] = judge.is_low(f, v)
        f["status"] = "still-open" if f["id"] in results and results[f["id"]]["status"] == "still-open" else "new"
        f["also_flagged_by"] = sorted({b["bot"] for b in bot_found if bots.same_spot(f, b)})
        f["comment_body"] = findings.comment_body(f)
    placed.sort(key=lambda f: (f["low_confidence"], findings.SEVERITY_RANK.get(f["severity"], 9), f["path"], f["line"]))
    rechecked = [{"kind": r["kind"], "title": r["title"], "path": r["path"], "line": r.get("line"),
                  "status": r["status"], "how": r["how"], "author": r.get("author"), "html_url": r.get("html_url")}
                 for r in results.values() if r["kind"] == "comment" or r["status"] == "fixed"]
    return placed, bot_found, rechecked


def default_chapters(ctx: dict) -> list[dict]:
    by_cat: dict[str, list[str]] = defaultdict(list)
    files = {f["path"]: f for f in ctx["files"]}
    for p in ctx["order"]:
        by_cat[files[p]["category"]].append(p)
    chapters = []
    for cat in CATEGORY_ORDER:
        if not by_cat.get(cat):
            continue
        chapters.append({
            "id": cat, "title": CATEGORY_TITLES[cat], "goal": _GOALS[cat],
            "files": [{"path": p, "why_now": (f"Tests `{files[p]['tests_for']}`." if files[p]["tests_for"] else _WHY_NOW[cat])}
                      for p in by_cat[cat]],
        })
    return chapters


def _chapters(ctx: dict, synthesis: dict | None, resolve: PathResolver, log: list[str]) -> list[dict]:
    if not synthesis:
        return default_chapters(ctx)
    files = {f["path"]: f for f in ctx["files"]}
    seen: set[str] = set()
    chapters = []
    for i, ch in enumerate(synthesis.get("chapters", [])):
        entries = []
        for cf in ch.get("files", []):
            p = resolve(cf.get("path"))
            if p is None:
                log.append(f"chapter '{ch.get('title')}': removed {cf.get('path')!r}, which is not in this PR")
                continue
            if p in seen:
                continue
            seen.add(p)
            entries.append({"path": p, "why_now": cf.get("why_now", "")})
        if entries:
            chapters.append({"id": ch.get("id") or f"ch{i + 1}", "title": ch.get("title", f"Part {i + 1}"),
                             "goal": ch.get("goal", ""), "files": entries})
    missing = [p for p in ctx["order"] if p not in seen and not files[p]["generated"]]
    if missing:
        log.append(f"{len(missing)} files were not put in any chapter; added them to 'Other changes'")
        chapters.append({"id": "other", "title": "Other changes", "goal": "Files that were not put in any group above.",
                         "files": [{"path": p, "why_now": _WHY_NOW[files[p]["category"]]} for p in missing]})
    skim = [p for p in ctx["order"] if p not in seen and files[p]["generated"]]
    if skim:
        chapters.append({"id": "generated", "title": CATEGORY_TITLES["generated"], "goal": _GOALS["generated"],
                         "files": [{"path": p, "why_now": _WHY_NOW["generated"]} for p in skim]})
    return chapters


def _deterministic_impact(ctx: dict, impact_data: dict | None) -> list[dict]:
    items = []
    sensitive = [f for f in ctx["files"] if f["sensitive"]]
    if sensitive:
        hot = [f for f in sensitive if any(k in f["path"] for k in (".github/workflows", ".claude", "CLAUDE.md", ".mcp.json"))]
        items.append({
            "kind": "sensitive", "severity": "high" if hot else "medium",
            "title": f"Changes {len(sensitive)} file{'s' if len(sensitive) != 1 else ''} that need{'' if len(sensitive) != 1 else 's'} extra care",
            "detail": "These files control CI, agent settings, login, passwords and secrets, packaging or containers. "
                      "A mistake here can affect security. Read them closely:\n\n"
                      + "\n".join(f"- `{f['path']}` ({f['status']})" for f in sensitive[:25]),
            "refs": [{"path": f["path"], "line": None, "at": "head"} for f in sensitive[:25]],
        })
    if impact_data:
        used = [s for s in impact_data.get("symbols", []) if s["count"] and not s["too_common"]]
        if used:
            rows = "\n".join(f"| `{s['symbol']}` | `{s['defined_in']}` | {s['count']} |" for s in used[:20])
            refs = [{"path": r["path"], "line": r["line"], "at": "head"} for s in used[:20] for r in s["outside_refs"][:1]]
            items.append({
                "kind": "callers", "severity": "medium" if len(used) > 5 else "low",
                "title": f"{len(used)} changed name{'s are' if len(used) != 1 else ' is'} used in files outside this PR",
                "detail": f"Found with {impact_data.get('engine', 'git grep')}. Other files use code that this PR changed, "
                          "but those files are not in the PR. Check that they still work.\n\n"
                          "| Name | Defined in | Uses outside the PR |\n|---|---|---|\n" + rows,
                "refs": refs,
            })
    return items


def build_analysis(pr_dir: Path) -> dict[str, Any]:
    ctx = read_json(pr_dir / "context.json")
    if ctx is None:
        raise FileNotFoundError(f"{pr_dir}/context.json is missing; run `prr fetch` first")
    progress = read_json(pr_dir / "progress.json") or {"status": "fetched"}
    manifest = read_json(pr_dir / "clusters.json") or {"clusters": []}
    synthesis = read_json(pr_dir / "synthesis.json")
    impact_data = read_json(pr_dir / "impact.json")
    log: list[str] = []
    resolve = PathResolver([f["path"] for f in ctx["files"]])

    keys_by_path: dict[str, list[str]] = defaultdict(list)
    for c in manifest["clusters"]:
        for it in c["items"]:
            keys_by_path[it["path"]].append(it["key"])

    # Test/docs flags: language rules first, then files Claude listed that the rules did not catch.
    roots = ctx.get("test_roots", [])
    claude_tests = {p for p in map(resolve, (synthesis or {}).get("test_files", [])) if p}
    claude_docs = {p for p in map(resolve, (synthesis or {}).get("docs_files", [])) if p}

    files_out = []
    for f in ctx["files"]:
        entry = {k: f[k] for k in ("path", "old_path", "status", "additions", "deletions", "binary", "category",
                                   "generated", "sensitive", "minutes", "base_blob", "head_blob", "tests_for",
                                   "tested_by", "hunks")}
        entry.update({"annotated": False, "risk": None, "summary": None, "look_for": [], "notes": [], "changed_symbols": []})
        kind, why = filekinds.kind(f["path"], roots)
        if filekinds.AGENT_FILES.search(f["path"]):
            pass  # AI-agent instructions are never hidden, whatever Claude listed
        elif kind is None and f["path"] in claude_tests:
            kind, why = "test", "Claude marked it as a test file"
        elif kind is None and f["path"] in claude_docs:
            kind, why = "docs", "Claude marked it as a docs file"
        entry.update({"is_test": kind == "test", "is_docs": kind == "docs", "kind_reason": why})
        parts = [read_json(pr_dir / "partials" / f"{k}.json") for k in keys_by_path.get(f["path"], [])]
        parts = [p["annotation"] for p in parts if p]
        if parts:
            orphans: list[str] = []
            entry["annotated"] = True
            entry["summary"] = "\n\n".join(dict.fromkeys(p["summary"] for p in parts if p.get("summary")))
            entry["risk"] = max((p["risk"] for p in parts), key=lambda r: _LEVELS.get(r, 0))
            entry["look_for"] = list(dict.fromkeys(x for p in parts for x in p.get("look_for", [])))[:6]
            entry["changed_symbols"] = list(dict.fromkeys(x for p in parts for x in p.get("changed_symbols", [])))
            for p in parts:
                for n in p.get("notes", []):
                    fixed = _fix_note(f, n)
                    if fixed:
                        entry["notes"].append(fixed)
                    else:
                        orphans.append(n.get("text", ""))
            if orphans:
                log.append(f"{f['path']}: moved {len(orphans)} note(s) that pointed outside the diff into the file summary")
                entry["summary"] = (entry["summary"] or "") + "\n\n**More notes:**\n" + "\n".join(f"- {t}" for t in orphans)
            entry["notes"].sort(key=lambda n: (n["side"] != "RIGHT", n["start"]))
        files_out.append(entry)

    impact = []
    for item in (synthesis or {}).get("impact", []):
        refs = [{**r, "path": resolve(r.get("path")) or r.get("path")} for r in item.get("refs", []) if r.get("path")]
        impact.append({**item, "refs": refs})
    impact += _deterministic_impact(ctx, impact_data)

    flows = []
    for fl in (synthesis or {}).get("flows", []):
        nodes = [{**n, "path": resolve(n.get("path")) or n.get("path")} for n in fl.get("nodes", [])]
        ids = {n["id"] for n in nodes}
        edges = [e for e in fl.get("edges", []) if e.get("source") in ids and e.get("target") in ids]
        if nodes:
            flows.append({**fl, "nodes": nodes, "edges": edges})

    questions = [{**q, "refs": [{**r, "path": resolve(r.get("path")) or r.get("path")} for r in q.get("refs", []) if r.get("path")]}
                 for q in (synthesis or {}).get("questions", [])]

    found, bot_found, rechecked = build_findings(pr_dir, ctx)
    analysis = {
        "status": progress.get("status", "fetched"),
        "focus": progress.get("focus"),
        "pr": ctx["pr"],
        "summary": (synthesis or {}).get("summary"),
        "files": files_out,
        "chapters": _chapters(ctx, synthesis, resolve, log),
        "flows": flows,
        "impact": impact,
        "questions": questions,
        "findings": found,
        "bot_findings": bot_found,
        "ci": ci.public(read_json(pr_dir / "ci.json")),
        "rechecked": rechecked,
        "progress": {k: progress.get(k) for k in ("status", "clusters_total", "clusters_done", "clusters_failed",
                                                   "message", "started_at", "finished_at", "log") if k in progress},
        "repair_log": ctx.get("warnings", []) + log,
    }
    return Analysis.model_validate(analysis).model_dump(mode="json")
