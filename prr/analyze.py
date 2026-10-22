"""`prr analyze`: parallel per-cluster annotation → deterministic impact → one synthesis call → analysis.json.

Progress goes to progress.json after every step, so the web app fills in while this runs.
Annotations are cached per file by (path, blobs, prompt version, focus), so a re-run after the PR
changes only re-annotates files that changed.
"""

from __future__ import annotations

import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from . import blockers, bots, ci, cluster, diffparse, filekinds, github, impact, judge, merge, recheck, rubrics
from .claude import ClaudeError, run_structured
from .config import GUIDELINES_FILE, PROMPTS_DIR, Config
from .models import AnnotateOut, SynthesisOut, llm_schema

SECOND_LOOK = """**This is a second, closer read.** A first review of these files found no problems. That can be right, but
it is also what a review that didn't read closely looks like. Read each file in full again and look for real
problems. Do not invent findings: if the files are clean, return no findings again.

"""
from .util import now_iso, read_json, write_json

PROMPT_VERSION = "4"  # bump when prompts change: cached file notes are keyed on it
MAX_BODY_CHARS = 8000
MAX_FILE_MAP_LINES = 300


class AnalyzeError(RuntimeError):
    pass


def guidelines() -> str:
    try:
        text = GUIDELINES_FILE.read_text()
    except FileNotFoundError:
        return "(none)"
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip() or "(none)"


def style_rules() -> str:
    """Plain-language writing rules shared by every prompt (prompts/style.md)."""
    return (PROMPTS_DIR / "style.md").read_text().strip()


def _template(name: str, focus: str | None) -> str:
    text = (PROMPTS_DIR / name).read_text()
    return (text.replace("{{STYLE}}", style_rules())
                .replace("{{FOCUS}}", focus or "(none given; do a balanced review)")
                .replace("{{GUIDELINES}}", guidelines()))


def _pr_header(ctx: dict) -> str:
    pr = ctx["pr"]
    body = pr["body"].strip() or "(no description)"
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS] + "\n…[description truncated]"
    return (f"# Pull request #{pr['number']}: {pr['title']}\n"
            f"Author: {pr['author']} · {pr['base_ref']} ← {pr['head_ref']} · +{pr['additions']}/-{pr['deletions']} "
            f"in {pr['changed_files']} files\n\n## Description\n{body}\n")


def _file_map(ctx: dict, highlight: set[str]) -> str:
    lines = []
    for f in ctx["files"]:
        mark = "→ " if f["path"] in highlight else "  "
        lines.append(f"{mark}{f['path']} [{f['category']}, {f['status']}, +{f['additions']}/-{f['deletions']}]")
    if len(lines) > MAX_FILE_MAP_LINES:
        keep = [ln for ln in lines if ln.startswith("→")]
        lines = keep + [f"  … and {len(ctx['files']) - len(keep)} more files"]
    return "\n".join(lines)


def annotate_prompt(ctx: dict, c: cluster.Cluster, idx: int, total: int, focus: str | None, conventions: str = "") -> str:
    head = (_template("annotate.md", focus).replace("{{RUBRICS}}", rubrics.rubrics_for(c.paths))
            .replace("{{CONVENTIONS}}", conventions or "(The repo has no CLAUDE.md or AGENTS.md.)"))
    return head + cluster_files(ctx, c, idx, total)


def cluster_files(ctx: dict, c: cluster.Cluster, idx: int, total: int) -> str:
    """The PR header, the whole-PR file map and this batch's diffs (with R/L line tags)."""
    files = {f["path"]: f for f in ctx["files"]}
    parts = ["\n---\n", _pr_header(ctx),
             "\n## All changed files (→ marks this batch; others are context only)\n", _file_map(ctx, set(c.paths)),
             f"\n\n## Batch {idx} of {total}: review exactly these {len(c.items)} file(s)\n"]
    for it in c.items:
        f = files[it.path]
        meta = [f["status"], f["category"], f"+{f['additions']}/-{f['deletions']}"]
        if f["old_path"]:
            meta.append(f"renamed from {f['old_path']}")
        if f["tests_for"]:
            meta.append(f"tests {f['tests_for']}")
        if it.parts > 1:
            meta.append(f"part {it.part}/{it.parts}")
        parts.append(f"\n### {it.path}\n({', '.join(meta)})\n```\n{it.text}\n```\n")
    return "".join(parts)


def synthesis_prompt(ctx: dict, analysis: dict, impact_data: dict, edges: list[dict], focus: str | None) -> str:
    pr = ctx["pr"]
    out = [_template("synthesize.md", focus), "\n---\n", _pr_header(ctx)]
    if pr["commits"]:
        out.append("\n## Commits\n" + "\n".join(f"- {c['headline']}" for c in pr["commits"][:60]) + "\n")
    out.append("\n## Files (dependency-first default order) with their annotations\n")
    generated = []
    by_path = {f["path"]: f for f in analysis["files"]}
    ctx_files = {f["path"]: f for f in ctx["files"]}
    for p in ctx["order"]:
        f = by_path[p]
        if f["generated"]:
            generated.append(p)
            continue
        meta = [f["category"], f["status"], f"+{f['additions']}/-{f['deletions']}"]
        if f["risk"]:
            meta.append(f"risk {f['risk']}")
        if f["old_path"]:
            meta.append(f"renamed from {f['old_path']}")
        if f["tests_for"]:
            meta.append(f"tests {f['tests_for']}")
        if f["is_test"] or f["is_docs"]:
            meta.append("test file" if f["is_test"] else "docs file")
        if ctx_files[p]["imports"]:
            meta.append("imports " + ", ".join(ctx_files[p]["imports"][:6]))
        out.append(f"- `{p}` [{' · '.join(meta)}]\n")
        if f["summary"]:
            out.append(f"  {f['summary'][:400].replace(chr(10), ' ')}\n")
        if f["changed_symbols"]:
            out.append(f"  symbols: {', '.join(f['changed_symbols'][:12])}\n")
        if not f["annotated"]:
            out.append("  (not annotated)\n")
    if generated:
        shown = ", ".join(generated[:40]) + (f", … +{len(generated) - 40}" if len(generated) > 40 else "")
        out.append(f"\nGenerated / vendored / lock files ({len(generated)}): {shown}\n")
    if edges:
        out.append("\n## Calls and data hand-offs found in the file notes\n")
        out += [f"- {e['source']} → {e['target']}" + (f" ({e['label']})" if e.get("label") else "") + "\n" for e in edges[:100]]
    syms = [s for s in impact_data.get("symbols", []) if s["count"] or s["too_common"]]
    if syms:
        out.append(f"\n## Code outside this PR that uses the changed names (found with {impact_data['engine']})\n")
        for s in syms[:30]:
            if s["too_common"]:
                out.append(f"- `{s['symbol']}` (defined in {s['defined_in']}): name too common to search for\n")
                continue
            refs = ", ".join(f"{r['path']}:{r['line']}" for r in s["outside_refs"][:4])
            out.append(f"- `{s['symbol']}` (defined in {s['defined_in']}): used in {s['count']} places, e.g. {refs}\n")
    found = analysis.get("findings", [])
    if found:
        counts = {s: sum(1 for f in found if f["severity"] == s and not f["low_confidence"]) for s in ("blocker", "major", "minor", "nit")}
        out.append(f"\n## Findings so far\nBlockers: {counts['blocker']}, must fix: {counts['major']}, should fix: "
                   f"{counts['minor']}, could improve: {counts['nit']}\n")
        for f in [f for f in found if f["severity"] in ("blocker", "major") and not f["low_confidence"]][:15]:
            out.append(f"- [{f['severity']}] {f['path']}:{f['line']} {f['title']}\n")
    ci_info = analysis.get("ci") or {}
    failing = [c for c in ci_info.get("checks", []) if c["state"] == "fail"]
    if failing:
        out.append("\n## Failing CI checks\n")
        for c in failing:
            ex = c.get("explanation") or {}
            out.append(f"- {c['name']}: {ex.get('summary') or c.get('summary') or 'failed'}"
                       + (f" (caused by this PR: {ex['caused_by_pr']})" if ex.get("caused_by_pr") else "") + "\n")
    sensitive = [f["path"] for f in ctx["files"] if f["sensitive"]]
    if sensitive:
        out.append("\n## Files that need extra care (security, CI, secrets, packaging)\n" + "\n".join(f"- {p}" for p in sensitive[:40]) + "\n")
    return "".join(out)


class Progress:
    def __init__(self, pr_dir: Path, focus: str | None, echo: Callable[[str], None]):
        self.path = pr_dir / "progress.json"
        self.lock = threading.Lock()
        self.echo = echo
        self.data: dict[str, Any] = {"status": "annotating", "focus": focus, "clusters_total": 0, "clusters_done": 0,
                                     "clusters_failed": 0, "message": "", "started_at": now_iso(),
                                     "finished_at": None, "log": [], "cost_usd": 0.0}

    def update(self, msg: str | None = None, **fields: Any) -> None:
        with self.lock:
            self.data.update(fields)
            if msg:
                self.data["message"] = msg
                self.data["log"] = (self.data["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-100:]
                self.echo(msg)
            write_json(self.path, self.data)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def analyze(pr_dir: Path, cfg: Config, focus: str | None = None, force: bool = False,
            echo: Callable[[str], None] = print) -> dict:
    ctx = read_json(pr_dir / "context.json")
    if ctx is None:
        raise AnalyzeError("context.json missing; run `prr fetch` first")
    pid_file = pr_dir / "analyze.pid"
    other = read_json(pid_file)
    if other and other.get("pid") != os.getpid() and _pid_alive(other["pid"]):
        raise AnalyzeError(f"another analysis is already running for this PR (pid {other['pid']})")
    write_json(pid_file, {"pid": os.getpid(), "started_at": now_iso()})
    try:
        return _analyze(pr_dir, ctx, cfg, focus, force, echo)
    finally:
        pid_file.unlink(missing_ok=True)


def _analyze(pr_dir: Path, ctx: dict, cfg: Config, focus: str | None, force: bool, echo: Callable[[str], None]) -> dict:
    worktree = Path(ctx["worktree"])
    diffs = {f.path: f for f in diffparse.parse((pr_dir / "diff.patch").read_bytes().decode("utf-8", errors="replace"))}
    files_ctx = {f["path"]: f for f in ctx["files"]}
    clusters = cluster.build(ctx, diffs, cfg.analysis.cluster_tokens, cfg.analysis.cluster_max_files)
    keys = {(c.id, it.path, it.part): cluster.cache_key(files_ctx[it.path], it, PROMPT_VERSION, focus)
            for c in clusters for it in c.items}
    write_json(pr_dir / "clusters.json", {"prompt_version": PROMPT_VERSION, "clusters": [
        {"id": c.id, "tokens": c.tokens,
         "items": [{"path": it.path, "part": it.part, "parts": it.parts, "key": keys[(c.id, it.path, it.part)]} for it in c.items]}
        for c in clusters]})
    partials = pr_dir / "partials"
    partials.mkdir(exist_ok=True)

    def is_cached(c: cluster.Cluster) -> bool:
        return all((partials / f"{keys[(c.id, it.path, it.part)]}.json").exists() for it in c.items)

    pending = [c for c in clusters if force or not is_cached(c)]
    progress = Progress(pr_dir, focus, echo)
    skipped = sum(1 for f in ctx["files"] if f["generated"] or f["binary"])
    progress.update(
        f"Reading {len(ctx['files'])} files in {len(clusters)} groups "
        f"({len(clusters) - len(pending)} already done, {skipped} generated or binary files skipped)",
        status="annotating", clusters_total=len(clusters), clusters_done=len(clusters) - len(pending),
    )
    if force:
        (pr_dir / "synthesis.json").unlink(missing_ok=True)

    conventions = rubrics.repo_conventions(Path(ctx["repo_root"]), ctx["pr"]["merge_base_sha"])
    blocker_head = (PROMPTS_DIR / "blockers.md").read_text().replace("{{STYLE}}", style_rules())

    def structured(prompt: str, schema: type, model: str, *, tools: list[str] | None = None, turns: int = 40,
                   timeout: int | None = None) -> tuple[dict, float]:
        last_err: Exception | None = None
        for _ in range(cfg.analysis.retries + 1):
            try:
                raw, stats = run_structured(prompt, llm_schema(schema), cwd=worktree, model=model, tools=tools,
                                            timeout=timeout or cfg.analysis.annotate_timeout_s, max_turns=turns)
                return schema.model_validate(raw).model_dump(mode="json"), stats.cost_usd or 0.0
            except (ClaudeError, ValueError) as e:
                last_err = e
        raise ClaudeError(str(last_err))

    def run_cluster(c: cluster.Cluster) -> float:
        idx = clusters.index(c) + 1
        docs_only = all(files_ctx[p]["category"] == "docs" for p in c.paths)
        model = cfg.models.docs if docs_only else cfg.models.annotate
        prompt = annotate_prompt(ctx, c, idx, len(clusters), focus, conventions)
        try:
            out, cost = structured(prompt, AnnotateOut, model)
        except ClaudeError as e:
            progress.update(f"Group {idx}: Claude could not read it: {str(e)[:200]}")
            raise
        roots = ctx.get("test_roots", [])
        code_files = [p for p in c.paths if filekinds.kind(p, roots)[0] is None
                      and files_ctx[p]["category"] not in ("docs", "test", "generated")]
        second_pass = False
        if code_files and not any(a["findings"] for a in out["files"]):
            # Zero findings is also what a review that didn't read closely looks like: read once more.
            try:
                again, more = structured(SECOND_LOOK + prompt, AnnotateOut, model)
                cost += more
                found = {a["path"]: a["findings"] for a in again["files"]}
                for a in out["files"]:
                    a["findings"] = found.get(a["path"], [])
                second_pass = True
            except ClaudeError as e:
                progress.update(f"Group {idx}: the closer second read failed: {str(e)[:150]}")
        proposed: list[dict] = []
        if code_files:
            try:
                bo, more = structured(blocker_head + cluster_files(ctx, c, idx, len(clusters)), blockers.BlockersOut,
                                      cfg.models.blockers, tools=[], turns=3, timeout=600)
                cost += more
                proposed = bo["blockers"]
            except ClaudeError as e:
                progress.update(f"Group {idx}: the blocker check failed: {str(e)[:150]}")
        resolve = merge.PathResolver(c.paths)
        anns = {}
        for a in out["files"]:
            p = resolve(a["path"])
            if p:
                anns[p] = a
        for b in proposed:
            b["path"] = resolve(b["path"]) or b["path"]
            # Code must confirm the rule's key parts before it counts as a blocker (stored with the proposal).
            b["problem"] = blockers.corroborate(b, diffs.get(b["path"]), blockers.head_text(worktree, b["path"]))
        proposed = [b for b in proposed if b["path"] in code_files]  # no blockers in tests, docs or generated files
        for it in c.items:
            a = anns.get(it.path)
            if a is None:
                progress.update(f"Group {idx}: Claude returned no notes for {it.path}")
                continue
            a["path"] = it.path
            edges = [e for e in out["flow_edges"] if e["source"].startswith(it.path) or e["target"].startswith(it.path)]
            if it is c.items[0]:
                edges += [e for e in out["flow_edges"] if not any(e["source"].startswith(p) or e["target"].startswith(p) for p in c.paths)]
            write_json(partials / f"{keys[(c.id, it.path, it.part)]}.json",
                       {"annotation": a, "blockers": [b for b in proposed if b["path"] == it.path], "flow_edges": edges,
                        "cluster": c.id, "model": model, "second_pass": second_pass, "at": now_iso()})
        return cost

    with ThreadPoolExecutor(max_workers=max(1, cfg.analysis.concurrency)) as pool:
        futures = {pool.submit(run_cluster, c): c for c in pending}
        for fut in as_completed(futures):
            c = futures[fut]
            try:
                cost = fut.result()
                progress.update(f"Group {clusters.index(c) + 1} of {len(clusters)} done ({len(c.items)} files)",
                                clusters_done=progress.data["clusters_done"] + 1,
                                cost_usd=round(progress.data["cost_usd"] + cost, 4))
            except Exception as e:  # noqa: BLE001 — one failed batch must not sink the review
                progress.update(f"Group {clusters.index(c) + 1} failed, so its files have no notes: {str(e)[:200]}",
                                clusters_failed=progress.data["clusters_failed"] + 1)

    # Second look at every finding (cheap model, can only veto).
    progress.update("Taking a second look at every finding")
    placed = merge.placed_findings(pr_dir, ctx)
    verdicts = judge.run(placed, diffs, cfg, worktree, read_json(pr_dir / "judge.json") or {}, progress.update)
    write_json(pr_dir / "judge.json", verdicts)

    # CI results, other review tools' comments, and re-checking earlier problems run side by side.
    pr = ctx["pr"]
    previous = read_json(pr_dir / "history.json") or {}

    def do_ci() -> None:
        try:
            info = ci.fetch(pr["owner"], pr["repo"], pr["head_sha"])
            failing = sum(1 for c in info["checks"] if c["state"] == "fail")
            progress.update(f"Read {len(info['checks'])} CI checks ({failing} failing)")
            write_json(pr_dir / "ci.json", ci.explain_failures(info, ctx, cfg, worktree, progress.update))
        except Exception as e:  # noqa: BLE001 — CI is extra information; the review goes on without it
            progress.update(f"Could not read the CI checks: {str(e)[:150]}")

    def do_comments() -> None:
        try:
            comments = github.existing_comments(pr["owner"], pr["repo"], pr["number"])
        except Exception as e:  # noqa: BLE001
            progress.update(f"Could not read the PR's existing comments: {str(e)[:150]}")
            return
        found = bots.from_comments(comments)
        saved = read_json(pr_dir / "bots.json") or {}
        verdict_cache = bots.check(found, cfg, worktree, saved.get("verdicts", {}), progress.update) if found else {}
        write_json(pr_dir / "bots.json", {"found": found, "verdicts": verdict_cache})
        if found:
            progress.update(f"Checked {len(found)} comments from other review tools")
        items = []
        if previous.get("head_sha") and previous["head_sha"] != pr["head_sha"]:
            items += previous.get("items", [])
        items += [e for c in comments if c.get("line") is None and not c.get("in_reply_to_id")
                  for e in [recheck.comment_entry(c)] if e]
        if items:
            saved_checks = (read_json(pr_dir / "recheck.json") or {}).get("cache", {})
            results = recheck.run(items, ctx, worktree, cfg, saved_checks, progress.update)  # updates saved_checks
            write_json(pr_dir / "recheck.json", {"cache": saved_checks, "results": results,
                                                 "previous_head": previous.get("head_sha")})
            fixed = sum(1 for r in results.values() if r["status"] == "fixed")
            progress.update(f"Re-checked {len(results)} earlier problems: {fixed} fixed")

    with ThreadPoolExecutor(max_workers=2) as side:
        list(side.map(lambda fn: fn(), [do_ci, do_comments]))

    partial_view = merge.build_analysis(pr_dir)
    progress.update("Looking for other code that uses the changed names")
    symbols = {f["path"]: f["changed_symbols"] for f in partial_view["files"] if f["changed_symbols"]}
    impact_data = impact.compute(Path(ctx["repo_root"]), ctx["pr"]["head_sha"], set(files_ctx), symbols)
    write_json(pr_dir / "impact.json", impact_data)

    edges: list[dict] = []
    seen_edges: set[tuple[str, str]] = set()
    for c in clusters:
        for it in c.items:
            part = read_json(partials / f"{keys[(c.id, it.path, it.part)]}.json")
            for e in (part or {}).get("flow_edges", []):
                if (e["source"], e["target"]) not in seen_edges:
                    seen_edges.add((e["source"], e["target"]))
                    edges.append(e)

    progress.update("Writing the summary, reading order, data flows and impact", status="synthesizing")
    prompt = synthesis_prompt(ctx, partial_view, impact_data, edges, focus)
    for attempt in range(cfg.analysis.retries + 1):
        try:
            raw, stats = run_structured(prompt, llm_schema(SynthesisOut), cwd=worktree, model=cfg.models.synthesize,
                                        timeout=cfg.analysis.synthesize_timeout_s, max_turns=30)
            write_json(pr_dir / "synthesis.json", SynthesisOut.model_validate(raw).model_dump(mode="json"))
            progress.update("Summary written", cost_usd=round(progress.data["cost_usd"] + (stats.cost_usd or 0), 4))
            break
        except (ClaudeError, ValueError) as e:
            progress.update(f"Summary try {attempt + 1} failed: {str(e)[:200]}")
    else:
        progress.update("Could not write the summary. Showing the file notes in the default order")

    all_failed = clusters and progress.data["clusters_failed"] == len(clusters)
    progress.update(f"Analysis {'failed' if all_failed else 'complete'} (≈${progress.data['cost_usd']:.2f})",
                    status="failed" if all_failed else "complete", finished_at=now_iso())
    analysis = merge.build_analysis(pr_dir)
    write_json(pr_dir / "analysis.json", analysis)
    _save_history(pr_dir, ctx, analysis)
    return analysis


def _save_history(pr_dir: Path, ctx: dict, analysis: dict) -> None:
    """What the next review round re-checks: this round's open findings, with the code around them."""
    worktree = Path(ctx["worktree"])
    items = []
    for f in analysis["findings"]:
        if f["low_confidence"] or f["severity"] == "nit":
            continue
        try:
            lines = (worktree / f["path"]).read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        items.append(recheck.history_entry(f, lines))
    write_json(pr_dir / "history.json", {"head_sha": ctx["pr"]["head_sha"], "saved_at": now_iso(), "items": items})
