"""context.json + partials/*.json + synthesis.json + impact.json → analysis.json.

The model's output is repaired, not rejected: a bad anchor is clamped, moved or folded into the
file summary; an unknown path is matched by basename or dropped; a file the synthesis forgot is
still placed in a chapter. Everything that was repaired is listed in `repair_log`.
"""

from __future__ import annotations

import hashlib
import posixpath
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import anchors
from .classify import CATEGORY_ORDER, CATEGORY_TITLES
from .models import Analysis
from .util import read_json

_LEVELS = {"low": 0, "medium": 1, "high": 2}
_WHY_NOW = {
    "schema": "Contracts first: later files build on these definitions.",
    "types": "Data shapes used by the logic that follows.",
    "core": "Core behavior change.",
    "api": "Where the new behavior is exposed or invoked.",
    "ui": "User-facing change.",
    "config": "Settings and wiring.",
    "infra": "Build, CI or deploy change.",
    "test": "Shows which behavior the PR pins down.",
    "docs": "Documentation of the change.",
    "generated": "Generated, vendored or lock file: skim.",
}
_GOALS = {
    "schema": "Understand the contracts and data definitions everything else relies on.",
    "types": "Know the data shapes before reading the logic.",
    "core": "Understand the main behavior change and its edge cases.",
    "api": "See how the change is exposed and who calls it.",
    "ui": "Check the user-facing behavior.",
    "config": "Check how the change is configured and switched on.",
    "infra": "Check build, CI and deployment impact.",
    "test": "Confirm the risky behavior is actually tested.",
    "docs": "Make sure the docs match the behavior.",
    "generated": "Skim only: confirm these were regenerated, not hand-edited.",
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


def _fix_suggestion(file_ctx: dict, s: dict) -> dict | None:
    side, line, start = s.get("side"), s.get("line"), s.get("start_line")
    if not isinstance(line, int):
        return None
    if start is not None and start > line:
        start, line = line, start
    if start == line:
        start = None
    for cand_side in ([side] if side in ("LEFT", "RIGHT") else []) + [x for x in ("RIGHT", "LEFT") if x != side]:
        for cand_start in ([start, None] if start is not None else [None]):
            if anchors.check(file_ctx, cand_side, line, cand_start) is None:
                return {**s, "side": cand_side, "line": line, "start_line": cand_start}
    return None


def _suggestion_id(path: str, s: dict) -> str:
    return "s-" + hashlib.sha1(f"{path}|{s['side']}|{s['line']}|{s['body']}".encode()).hexdigest()[:10]


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
                log.append(f"chapter '{ch.get('title')}': dropped unknown path {cf.get('path')!r}")
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
        log.append(f"{len(missing)} files were not placed by the synthesis; added to 'Other changes'")
        chapters.append({"id": "other", "title": "Other changes", "goal": "Files the guide did not place in a chapter.",
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
            "title": f"Touches {len(sensitive)} security-sensitive path{'s' if len(sensitive) != 1 else ''}",
            "detail": "Files under CI, agent config, auth/crypto/secrets, packaging or container paths deserve a "
                      "closer read:\n\n" + "\n".join(f"- `{f['path']}` ({f['status']})" for f in sensitive[:25]),
            "refs": [{"path": f["path"], "line": None, "at": "head"} for f in sensitive[:25]],
        })
    if impact_data:
        used = [s for s in impact_data.get("symbols", []) if s["count"] and not s["too_common"]]
        if used:
            rows = "\n".join(f"| `{s['symbol']}` | `{s['defined_in']}` | {s['count']} |" for s in used[:20])
            refs = [{"path": r["path"], "line": r["line"], "at": "head"} for s in used[:20] for r in s["outside_refs"][:1]]
            items.append({
                "kind": "callers", "severity": "medium" if len(used) > 5 else "low",
                "title": f"{len(used)} changed symbol{'s are' if len(used) != 1 else ' is'} referenced outside this PR",
                "detail": f"Found with {impact_data.get('engine', 'git grep')}. These call sites are not in the diff; "
                          "check they still hold.\n\n| Symbol | Defined in | Outside refs |\n|---|---|---|\n" + rows,
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

    files_out, suggestions = [], []
    for f in ctx["files"]:
        entry = {k: f[k] for k in ("path", "old_path", "status", "additions", "deletions", "binary", "category",
                                   "generated", "sensitive", "minutes", "base_blob", "head_blob", "tests_for",
                                   "tested_by", "hunks")}
        entry.update({"annotated": False, "risk": None, "summary": None, "look_for": [], "notes": [], "changed_symbols": []})
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
                for s in p.get("suggestions", []):
                    fixed = _fix_suggestion(f, s)
                    if fixed:
                        suggestions.append({"id": _suggestion_id(f["path"], fixed), "path": f["path"], "side": fixed["side"],
                                            "line": fixed["line"], "start_line": fixed["start_line"],
                                            "severity": fixed["severity"], "body": fixed["body"]})
                    else:
                        log.append(f"{f['path']}: dropped suggestion at {s.get('side')} {s.get('line')} (outside the diff)")
            if orphans:
                log.append(f"{f['path']}: {len(orphans)} note(s) outside the diff folded into the summary")
                entry["summary"] = (entry["summary"] or "") + "\n\n**Also:**\n" + "\n".join(f"- {t}" for t in orphans)
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
        "suggestions": suggestions,
        "progress": {k: progress.get(k) for k in ("status", "clusters_total", "clusters_done", "clusters_failed",
                                                   "message", "started_at", "finished_at", "log") if k in progress},
        "repair_log": ctx.get("warnings", []) + log,
    }
    return Analysis.model_validate(analysis).model_dump(mode="json")
