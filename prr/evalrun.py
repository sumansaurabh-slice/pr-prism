"""`prr eval`: replay the analysis on past PRs, at the commit you commented on, and score it against your labels.

For each distinct (PR, commit) that has labels (prr/labels.py):
1. Prepare it like `prr fetch` does (fetch.prepare), on the pinned commit, fetched into refs/pr-prism/eval/<sha>, in
   <cache>/_eval/<eval id>/pr-<n>-<sha>/. A commit that can't be fetched (force-pushed away) is skipped and listed.
2. Run the analysis `runs` times (3 by default), each in its own folder so no run reuses another's cached work, with
   mode="eval": no CI, no other tools' comments, no re-checks, no history.
3. Score every run in four views: deep check off or on (off = placed_pre_deepcheck.json, the findings before the deep
   check merged; on = analysis.json) × confidence floor on or off (judge.is_low with floor=50 or None). Per view, per
   PR and in total: recall over countable problem labels, problems found but hidden by the second look, findings that
   match not-problem labels, unlabelled findings (listed, not counted as wrong), cost and time, each as the mean and
   min-max across runs; plus the pairs that were skipped.
4. On the held-out PRs, check the Wave 2 gate (D21): deep check on beats off by 25+ points of recall, on finds 40%+,
   the deep check adds at most $6 per PR, and at most 10 unlabelled findings per PR.

"Per PR" means per PR at one commit (one analysis). The report goes to <cache>/<owner>__<repo>/eval-<date>.json (0600).
Eval folders are removed at the end unless `keep`; the refs stay, so a commit that is later force-pushed away can still
be replayed. Nothing here writes to GitHub: the only GitHub calls are `gh pr view` and git fetches.
"""

from __future__ import annotations

import re
import shutil
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Callable

from . import analyze, fetch, judge, labels, ledger
from .config import Config
from .gitgh import CommandError, PRRef, find_remote, git, git_ok
from .util import now_iso, read_json, write_json

PRE_DEEPCHECK = "placed_pre_deepcheck.json"  # written by the deep check (R1) before its findings merge
FLOOR = 50                                   # "floor on": findings the reviewer was under 50% sure of count as hidden
DEFAULT_RUN_USD = 5.0                        # the estimate per analysis when this repo has no earlier analysis cost
VIEWS: dict[str, dict[str, Any]] = {
    "off+floor": {"deepcheck": False, "floor": FLOOR, "title": "Deep check off, confidence floor on (before)"},
    "off": {"deepcheck": False, "floor": None, "title": "Deep check off, no floor"},
    "on+floor": {"deepcheck": True, "floor": FLOOR, "title": "Deep check on, confidence floor on"},
    "on": {"deepcheck": True, "floor": None, "title": "Deep check on, no floor (now)"},
}
GATE_ON, GATE_OFF = "on", "off"  # the gate isolates the deep check: both sides use today's rule (no floor)
GATE = {"gain_points": 25.0, "on_recall": 0.40, "added_usd": 6.0, "unlabelled": 10.0}
_SHA = re.compile(r"[0-9a-f]{40}")
_METRICS = ("recall", "found", "countable", "hidden", "not_problem", "unlabelled_per_pr", "judge_hidden_rate",
            "cost_usd_per_pr", "deepcheck_usd_per_pr", "time_s_per_pr")


class Skip(Exception):
    """This (PR, commit) can't be replayed; the message says why, in plain words."""


def _log(msg: str) -> None:
    print(f"[prr] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------------------
# What to replay
# --------------------------------------------------------------------------------------


def pairs_from(all_labels: list[dict], prs: list[int] | None = None, split: str = "all") -> list[dict]:
    """The distinct (PR, commit) pairs that have labels, oldest PR first. A PR is held out when any of its labels is
    (labels import sets the split per PR). Labels without a commit can't be replayed and are left out."""
    held = {x["pr"] for x in all_labels if x.get("split") == "holdout"}
    groups: dict[tuple[int, str], list[dict]] = {}
    for x in all_labels:
        if x.get("commit_id") and (not prs or x["pr"] in prs):
            groups.setdefault((x["pr"], x["commit_id"]), []).append(x)
    out = []
    for (pr, commit), rows in sorted(groups.items()):
        pr_split = "holdout" if pr in held else "train"
        if split != "all" and pr_split != split:
            continue
        bases = Counter(x["merge_base_sha"] for x in rows if x.get("merge_base_sha"))
        out.append({"pr": pr, "commit_id": commit, "merge_base_sha": bases.most_common(1)[0][0] if bases else None,
                    "split": pr_split, "labels": rows})
    return out


def label_counts(rows: list[dict]) -> dict[str, int]:
    return {"problems": sum(1 for x in rows if x["kind"] == "problem"), "countable": sum(map(labels.countable, rows)),
            "not_problem": sum(1 for x in rows if x["kind"] == "not-problem")}


def last_analysis_usd(repo_folder: Path) -> float | None:
    """What this repo's most recent finished analysis cost (progress.json), for the estimate."""
    best: tuple[str, float] | None = None
    for p in repo_folder.glob("pr-*/progress.json"):
        prog = read_json(p) or {}
        spent = prog.get("cost_usd")
        if prog.get("status") == "complete" and isinstance(spent, (int, float)) and spent > 0:
            key = (prog.get("finished_at") or "", float(spent))
            best = key if best is None or key > best else best
    return best[1] if best else None


# --------------------------------------------------------------------------------------
# Preparing a pinned commit
# --------------------------------------------------------------------------------------


def _pin(root: Path, remote: str, sha: str) -> None:
    """Keep `sha` at refs/pr-prism/eval/<sha>, fetching it first when the repo doesn't have it."""
    ref = f"refs/pr-prism/eval/{sha}"
    if git_ok("cat-file", "-e", f"{sha}^{{commit}}", cwd=root):
        git("update-ref", ref, sha, cwd=root)
        return
    try:
        git("fetch", "--no-tags", "--quiet", remote, f"+{sha}:{ref}", cwd=root)
    except CommandError as e:
        _log(f"git fetch of {sha[:10]} failed: {str(e)[:300]}")
        raise Skip(f"couldn't fetch commit {sha[:10]}; it may have been force-pushed away") from e


def _meta_at(meta: dict, sha: str) -> dict:
    """The PR as it was at `sha`: commits up to it, and no +/- counts when GitHub's are for a later head."""
    out = dict(meta)
    if meta.get("headRefOid") != sha:
        out.update(additions=None, deletions=None, changedFiles=None)
        oids = [c.get("oid") for c in meta.get("commits") or []]
        if sha in oids:
            out["commits"] = (meta.get("commits") or [])[: oids.index(sha) + 1]
    return out


def prepare_pair(pair: dict, meta: dict, *, root: Path, remote: str, ref: PRRef, folder: Path) -> dict:
    """Fetch and pin the pair's commit and its merge base, then build its inputs with fetch.prepare. Raises Skip."""
    sha = pair["commit_id"]
    if not _SHA.fullmatch(sha or ""):
        raise Skip(f"{sha!r} is not a full commit id")
    _pin(root, remote, sha)
    base = pair.get("merge_base_sha") or labels.merge_base_for(root, remote, ref, meta, sha)
    if not base or not _SHA.fullmatch(base):
        raise Skip("couldn't work out where the PR's diff starts (its merge base)")
    if not git_ok("cat-file", "-e", f"{base}^{{commit}}", cwd=root):
        try:
            git("fetch", "--no-tags", "--quiet", remote, base, cwd=root)
        except CommandError as e:
            raise Skip(f"couldn't fetch the PR's base commit {base[:10]}") from e
    try:
        ctx, _ = fetch.prepare(ref, sha, base, _meta_at(meta, sha), root=root, d=folder)
    except CommandError as e:
        raise Skip(f"couldn't check out commit {sha[:10]}: {str(e)[:160]}") from e
    return ctx


def remove_folder(root: Path, folder: Path) -> None:
    """An eval folder: its worktree (git worktree remove) and everything else."""
    for wt in folder.glob("**/worktree"):
        if wt.is_dir():
            git("worktree", "remove", "--force", str(wt), cwd=root, check=False)
    shutil.rmtree(folder, ignore_errors=True)
    git("worktree", "prune", cwd=root, check=False)


# --------------------------------------------------------------------------------------
# Scoring one run
# --------------------------------------------------------------------------------------


def hidden(f: dict, verdicts: dict, floor: int | None) -> bool:
    """Hidden in this view. A finding the second look never scored keeps what the analysis said (low_confidence)."""
    v = verdicts.get(f["id"])
    if v is None and f.get("low_confidence"):
        return True
    return judge.is_low(f, v, floor=floor)


def _brief(f: dict) -> dict:
    return {"id": f["id"], "path": f["path"], "line": f["line"], "severity": f.get("severity"), "title": f["title"],
            "sources": f.get("sources") or [f.get("source") or "claude"]}


def score(pair_labels: list[dict], found: list[dict], verdicts: dict, floor: int | None) -> dict[str, Any]:
    """One view of one run: what it found of the labelled problems, and what else it showed."""
    shown = [f for f in found if not hidden(f, verdicts, floor)]
    held_back = [f for f in found if hidden(f, verdicts, floor)]
    problems = [x for x in pair_labels if labels.countable(x)]
    status = {}
    for x in problems:
        if any(labels.match_label(x, f) for f in shown):
            status[x["id"]] = "found"
        elif any(labels.match_label(x, f) for f in held_back):
            status[x["id"]] = "hidden"  # found, but hidden by the second look (or the floor)
        else:
            status[x["id"]] = "missed"
    rejected = [x for x in pair_labels if x["kind"] == "not-problem"]
    placed = [x for x in pair_labels if x.get("path") and isinstance(x.get("line"), int)]
    flagged = [f for f in shown if any(labels.match_label(x, f) for x in rejected)]
    unlabelled = [f for f in shown if not any(labels.match_label(x, f) for x in placed)]
    found_n = sum(1 for s in status.values() if s == "found")
    return {
        "countable": len(problems), "found": found_n, "hidden": sum(1 for s in status.values() if s == "hidden"),
        "missed": sum(1 for s in status.values() if s == "missed"),
        "recall": found_n / len(problems) if problems else None,
        "not_problem": len(flagged), "not_problem_findings": [_brief(f) for f in flagged],
        "unlabelled": len(unlabelled), "unlabelled_findings": [_brief(f) for f in unlabelled],
        "shown": len(shown), "judge_hidden": len(held_back), "findings": len(found), "labels": status,
    }


def _span_s(lines: list[dict]) -> float:
    """Wall time covered by these calls (they may overlap): first start to last end."""
    spans = []
    for x in lines:
        try:
            end = datetime.fromisoformat(x["at"])
        except (KeyError, TypeError, ValueError):
            continue
        spans.append((end - timedelta(milliseconds=x.get("duration_ms") or 0), end))
    return (max(e for _, e in spans) - min(s for s, _ in spans)).total_seconds() if spans else 0.0


def score_run(run_dir: Path, pair_labels: list[dict], elapsed_s: float) -> dict[str, Any]:
    """All four views of one finished run, from its folder, plus its cost and time (from calls.jsonl)."""
    final = (read_json(run_dir / "analysis.json") or {}).get("findings") or []
    before = read_json(run_dir / PRE_DEEPCHECK)
    ran = isinstance(before, list)
    verdicts = read_json(run_dir / "judge.json") or {}
    calls = [x for x in ledger.read(run_dir) if x.get("purpose")]
    total = sum(x.get("cost_usd") or 0.0 for x in calls)
    deep = [x for x in calls if x["purpose"] == "deepcheck"]
    deep_usd, deep_s = sum(x.get("cost_usd") or 0.0 for x in deep), _span_s(deep)
    views = {}
    for vid, v in VIEWS.items():
        views[vid] = score(pair_labels, final if v["deepcheck"] or not ran else before, verdicts, v["floor"])
        views[vid]["cost_usd"] = round(total if v["deepcheck"] else total - deep_usd, 4)
        views[vid]["time_s"] = round(elapsed_s if v["deepcheck"] else max(0.0, elapsed_s - deep_s), 1)
        views[vid]["deepcheck_usd"] = round(deep_usd if v["deepcheck"] else 0.0, 4)
    return {"status": "complete", "deepcheck_ran": ran, "cost_usd": round(total, 4), "deepcheck_usd": round(deep_usd, 4),
            "time_s": round(elapsed_s, 1), "deepcheck_time_s": round(deep_s, 1),
            "unknown_cost": any(x.get("cost_usd") is None for x in calls), "views": views}


# --------------------------------------------------------------------------------------
# Adding up: per run, then mean and min-max across runs
# --------------------------------------------------------------------------------------


def combine(pairs: list[dict], vid: str, i: int) -> dict[str, Any] | None:
    """Run i of a group of pairs in one view: counts are summed, per-PR numbers averaged over the pairs."""
    rs = [p["runs"][i]["views"][vid] for p in pairs if i < len(p["runs"]) and p["runs"][i]["status"] == "complete"]
    if not rs:
        return None
    countable, found_n = sum(r["countable"] for r in rs), sum(r["found"] for r in rs)
    every = sum(r["findings"] for r in rs)
    return {"recall": found_n / countable if countable else None, "found": found_n, "countable": countable,
            "hidden": sum(r["hidden"] for r in rs), "not_problem": sum(r["not_problem"] for r in rs),
            "unlabelled_per_pr": mean(r["unlabelled"] for r in rs),
            "judge_hidden_rate": sum(r["judge_hidden"] for r in rs) / every if every else None,
            "cost_usd_per_pr": mean(r["cost_usd"] for r in rs), "deepcheck_usd_per_pr": mean(r["deepcheck_usd"] for r in rs),
            "time_s_per_pr": mean(r["time_s"] for r in rs)}


def spread(values: list[float | None]) -> dict[str, float] | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return {"mean": round(mean(vals), 4), "min": round(min(vals), 4), "max": round(max(vals), 4), "runs": len(vals)}


def summarize(pairs: list[dict], runs: int) -> dict[str, dict]:
    """Per view: each metric's mean and min-max across runs."""
    out = {}
    for vid in VIEWS:
        per_run = [combine(pairs, vid, i) for i in range(runs)]
        out[vid] = {m: spread([r[m] if r else None for r in per_run]) for m in _METRICS}
    return out


def gate(pairs: list[dict], runs: int) -> dict[str, Any]:
    """The Wave 2 gate (D21) on the held-out pairs, from the mean of the runs."""
    held = [p for p in pairs if p["split"] == "holdout" and any(r["status"] == "complete" for r in p["runs"])]
    if not held:
        return {"split": "holdout", "pass": None, "prs": 0, "bars": [],
                "note": "No held-out PRs were scored. Import some with `prr labels import --split holdout <pr…>`."}
    gains, on_recall, added, unlabelled = [], [], [], []
    for i in range(runs):
        on, off = combine(held, GATE_ON, i), combine(held, GATE_OFF, i)
        if not on or not off:
            continue
        if on["recall"] is not None and off["recall"] is not None:
            gains.append((on["recall"] - off["recall"]) * 100)
            on_recall.append(on["recall"])
        added.append(on["deepcheck_usd_per_pr"])
        unlabelled.append(on["unlabelled_per_pr"])

    def bar(text: str, values: list[float], ok: Callable[[float], bool], shown: Callable[[float], str]) -> dict:
        value = round(mean(values), 4) if values else None
        return {"bar": text, "value": value, "shown": shown(value) if value is not None else "no data",
                "pass": bool(value is not None and ok(value))}

    bars = [
        bar("Deep check on finds at least 25 points more of your problems than off", gains,
            lambda v: v >= GATE["gain_points"], lambda v: f"{v:+.0f} points"),
        bar("Deep check on finds at least 40% of your problems", on_recall,
            lambda v: v >= GATE["on_recall"], lambda v: f"{v:.0%}"),
        bar("The deep check adds at most $6 per PR", added, lambda v: v <= GATE["added_usd"], lambda v: f"${v:.2f}"),
        bar("At most 10 unlabelled findings per PR", unlabelled,
            lambda v: v <= GATE["unlabelled"], lambda v: f"{v:.1f}"),
    ]
    out: dict[str, Any] = {"split": "holdout", "pass": all(b["pass"] for b in bars), "prs": len(held),
                           "runs": runs, "views": {"on": GATE_ON, "off": GATE_OFF}, "bars": bars}
    if not any(r.get("deepcheck_ran") for p in held for r in p["runs"]):
        out["note"] = "The deep check did not run, so deep check on and off are the same findings."
    return out


# --------------------------------------------------------------------------------------
# The whole eval
# --------------------------------------------------------------------------------------


def _report_path(repo_folder: Path) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path, n = repo_folder / f"eval-{day}.json", 1
    while path.exists():
        n += 1
        path = repo_folder / f"eval-{day}-{n}.json"
    return path


def run_eval(cfg: Config, root: Path, owner: str, repo: str, *, prs: list[int] | None = None, runs: int = 3,
             split: str = "all", max_usd: float | None = None, keep: bool = False,
             confirm: Callable[[str], bool] = lambda text: False, out: Callable[[str], None] = print) -> dict | None:
    """Replay and score. `confirm` sees the cost estimate and says whether to go on. Returns the report (also written
    next to labels.jsonl), or None when there was nothing to run or the answer was no."""
    repo_folder = labels.repo_dir(cfg.cache_dir, owner, repo)
    pairs = pairs_from(labels.load(repo_folder), prs, split)
    if not pairs:
        out(f"No labelled PRs to replay for {owner}/{repo}" + (f" in the {split} split" if split != "all" else "")
            + ". Import your comments with `prr labels import <pr…>`.")
        return None
    last = last_analysis_usd(repo_folder)
    per_run = last if last is not None else DEFAULT_RUN_USD
    estimate = len(pairs) * runs * per_run
    basis = (f"this repo's last analysis cost ${per_run:.2f}" if last is not None
             else f"no earlier analysis of this repo, so ${per_run:.2f} each is assumed")
    text = (f"This replays {_plural(len(pairs), 'labelled PR commit')} × {_plural(runs, 'run')}: about ${estimate:.2f}"
            f" ({basis}).")
    if max_usd is not None:
        text += f" It stops starting new runs once ${max_usd:.2f} is spent."
    if not confirm(text):
        out("Not run.")
        return None

    eval_id = ledger.new_run_id()
    base = cfg.cache_dir / "_eval" / eval_id
    remote = None
    report: dict[str, Any] = {
        "v": 1, "repo": f"{owner}/{repo}", "eval_id": eval_id, "started_at": now_iso(), "finished_at": None,
        "status": "running", "stop_reason": None, "runs_per_pair": runs, "split": split, "prs_asked": prs or [],
        "floor": FLOOR, "views": {k: v["title"] for k, v in VIEWS.items()}, "estimate_usd": round(estimate, 2),
        "max_usd": max_usd, "spent_usd": 0.0, "pairs": [], "skipped": [], "notes": [],
    }
    metas: dict[int, dict] = {}
    spent, started = 0.0, time.monotonic()
    try:
        for pair in pairs:
            row = {k: pair[k] for k in ("pr", "commit_id", "merge_base_sha", "split")}
            row.update(labels=label_counts(pair["labels"]), runs=[], status="skipped", reason=None)
            report["pairs"].append(row)
            if report["stop_reason"]:
                row["reason"] = report["stop_reason"]
                continue
            ref = PRRef(owner, repo, pair["pr"])
            folder = base / f"pr-{pair['pr']}-{pair['commit_id']}"
            try:
                if pair["pr"] not in metas:
                    metas[pair["pr"]] = fetch.read_meta(ref)
                remote = remote or find_remote(root, ref)
                ctx = prepare_pair(pair, metas[pair["pr"]], root=root, remote=remote, ref=ref, folder=folder)
            except (Skip, CommandError) as e:
                row["reason"] = str(e) if isinstance(e, Skip) else f"couldn't read the PR from GitHub: {str(e)[:160]}"
                out(f"Skipped #{pair['pr']} at {pair['commit_id'][:7]}: {row['reason']}")
                continue
            row["merge_base_sha"] = ctx["pr"]["merge_base_sha"]
            row["status"] = "complete"
            for i in range(1, runs + 1):
                if max_usd is not None and spent >= max_usd:
                    report["stop_reason"] = f"reached --max-usd ${max_usd:.2f} after spending ${spent:.2f}"
                    row["status"] = "partial" if row["runs"] else "skipped"
                    row["reason"] = report["stop_reason"]
                    break
                result = _one_run(folder / f"run-{i}", ctx, cfg, pair, i, runs)
                spent += result.get("cost_usd") or 0.0
                row["runs"].append(result)
            if row["runs"] and all(r["status"] != "complete" for r in row["runs"]):
                row["status"], row["reason"] = "failed", "every run of the analysis failed"
        report["status"] = "stopped" if report["stop_reason"] else "complete"
    except KeyboardInterrupt:
        report["status"], report["stop_reason"] = "aborted", "stopped with Ctrl-C"
        for row in report["pairs"]:
            if row["status"] == "complete" and len(row["runs"]) < runs:
                row["status"] = "partial" if row["runs"] else "skipped"
            row["reason"] = row["reason"] or (report["stop_reason"] if row["status"] != "complete" else None)
    finally:
        # every call the runs made, including one stopped half-way (its folder is about to go)
        calls = [x for p in base.glob("*/run-*") for x in ledger.read(p) if x.get("purpose")]
        report["spent_usd"] = round(max(spent, sum(x.get("cost_usd") or 0.0 for x in calls)), 4)
        report["time_s"] = round(time.monotonic() - started, 1)
        report["finished_at"] = now_iso()
        if keep:
            report["folder"] = str(base)
        elif base.exists():
            remove_folder(root, base)

    _add_totals(report, runs)
    path = _report_path(repo_folder)
    write_json(path, report)  # written through a private temp file: 0600, like state.json
    path.chmod(0o600)
    report["path"] = str(path)
    for line in summary_lines(report):
        out(line)
    return report


def _one_run(run_dir: Path, ctx: dict, cfg: Config, pair: dict, i: int, runs: int) -> dict[str, Any]:
    """One fresh analysis of the pinned commit, scored. A failed run is recorded, not raised."""
    run_dir.mkdir(parents=True, exist_ok=True)
    pr_folder = run_dir.parent
    for name in ("context.json", "diff.patch"):
        shutil.copy2(pr_folder / name, run_dir / name)
    tag = f"#{pair['pr']} {pair['commit_id'][:7]} run {i}/{runs}"
    t0 = time.monotonic()
    try:
        result = analyze._analyze(run_dir, ctx, cfg, None, True, lambda m: _log(f"{tag}: {m}"), mode="eval")
    except Exception as e:  # noqa: BLE001 — one failed run is a row in the report, not the end of the eval
        spent = sum(x.get("cost_usd") or 0.0 for x in ledger.read(run_dir) if x.get("purpose"))
        _log(f"{tag}: the analysis failed: {str(e)[:200]}")
        return {"status": "failed", "reason": str(e)[:300], "cost_usd": round(spent, 4), "views": {}}
    elapsed = time.monotonic() - t0
    if result.get("status") == "failed":
        spent = sum(x.get("cost_usd") or 0.0 for x in ledger.read(run_dir) if x.get("purpose"))
        return {"status": "failed", "reason": "every group of files failed", "cost_usd": round(spent, 4), "views": {}}
    return score_run(run_dir, pair["labels"], elapsed)


def _add_totals(report: dict, runs: int) -> None:
    pairs = report["pairs"]
    report["skipped"] = [{"pr": p["pr"], "commit_id": p["commit_id"], "reason": p["reason"]}
                         for p in pairs if p["status"] == "skipped"]
    for row in pairs:
        row["summary"] = summarize([row], runs)
    by_pr: dict[int, list[dict]] = {}
    for row in pairs:
        by_pr.setdefault(row["pr"], []).append(row)
    report["prs"] = [{"pr": n, "split": rows[0]["split"], "commits": len(rows), "summary": summarize(rows, runs)}
                     for n, rows in by_pr.items()]
    report["total"] = {"all": summarize(pairs, runs)}
    for name in ("train", "holdout"):
        group = [p for p in pairs if p["split"] == name and p["runs"]]
        if group:
            report["total"][name] = summarize(group, runs)
    report["gate"] = gate(pairs, runs)
    done = [r for p in pairs for r in p["runs"] if r["status"] == "complete"]
    without = sum(1 for r in done if not r.get("deepcheck_ran"))
    if without:
        report["notes"].append(f"Deep check not run in {without} of {len(done)} runs: there, \"deep check off\" is the "
                               "final set of findings, so off and on are the same.")
    if any(r.get("unknown_cost") for r in done):
        report["notes"].append("Some calls' cost is unknown (a timeout or a crash), so the costs are lower bounds.")


# --------------------------------------------------------------------------------------
# The printed summary
# --------------------------------------------------------------------------------------


def _pct(s: dict | None) -> str:
    if not s:
        return "n/a"
    lo, hi = s["min"] * 100, s["max"] * 100
    return f"{s['mean']:.0%}" + (f" ({lo:.0f}-{hi:.0f}%)" if s["runs"] > 1 else "")


def _num(s: dict | None, fmt: str = "{:.1f}") -> str:
    if not s:
        return "n/a"
    text = fmt.format(s["mean"])
    return text + (f" ({fmt.format(s['min'])}-{fmt.format(s['max'])})" if s["runs"] > 1 and s["min"] != s["max"] else "")


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    return f"{minutes // 60} h {minutes % 60} min" if minutes >= 60 else f"{minutes} min"


def _minutes(s: dict | None) -> dict | None:
    return {**s, **{k: s[k] / 60 for k in ("mean", "min", "max")}} if s else None


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def view_line(s: dict) -> str:
    """One view's numbers in words."""
    if not s.get("countable"):
        return "no runs finished"
    return (f"found {s['found']['mean']:.1f} of {s['countable']['mean']:.0f} problems, {_pct(s['recall'])}"
            f" · {_num(s['hidden'])} more hidden by the second look"
            f" · {_num(s['not_problem'])} you said aren't problems"
            f" · {_num(s['unlabelled_per_pr'])} unlabelled per PR"
            f" · {_num(s['cost_usd_per_pr'], '${:.2f}')} and {_num(_minutes(s['time_s_per_pr']))} min per PR")


def summary_lines(report: dict) -> list[str]:
    scored = [p for p in report["pairs"] if p["runs"]]
    lines = [f"\n=== Eval of {report['repo']} ===",
             f"{_plural(len(scored), 'labelled PR commit')} × {_plural(report['runs_per_pair'], 'run')} · "
             f"${report['spent_usd']:.2f} spent · {_duration(report.get('time_s') or 0)}"
             + ("" if report["status"] == "complete" else f" · {report['status']}: {report['stop_reason']}")]
    lines += report["notes"]
    for split, views in report.get("total", {}).items():
        lines.append(f"\n{'All PRs' if split == 'all' else split.capitalize() + ' PRs'} (mean of runs, min-max):")
        for vid, title in report["views"].items():
            lines.append(f"  {title}: {view_line(views[vid])}")
    if scored:
        lines.append("Unlabelled findings are listed in the report; they are not counted as wrong.")
    if scored:
        lines.append("\nPer PR commit, deep check on → off (no floor):")
        for p in scored:
            on, off = p["summary"]["on"], p["summary"]["off"]
            lines.append(f"  #{p['pr']} {p['commit_id'][:7]} ({p['split']}): {_pct(on['recall'])} → {_pct(off['recall'])}"
                         f" of {p['labels']['countable']} problems" + ("" if p["status"] == "complete"
                                                                         else f" ({p['status']}: {p['reason']})"))
    if report["skipped"]:
        lines.append("\nSkipped:")
        lines += [f"  #{s['pr']} {s['commit_id'][:7]}: {s['reason']}" for s in report["skipped"]]
    g = report.get("gate") or {}
    if g.get("bars"):
        verdict = "PASS" if g["pass"] else "FAIL"
        lines.append(f"\nWave 2 gate, held-out PRs ({g['prs']}), mean of {g['runs']} runs: {verdict}")
        lines += [f"  {'pass' if b['pass'] else 'FAIL'}  {b['bar']}: {b['shown']}" for b in g["bars"]]
    if g.get("note"):
        lines.append(("\n" if not g.get("bars") else "  ") + g["note"])
    if report.get("folder"):
        lines.append(f"\nEval folders kept in {report['folder']}")
    lines.append(f"\nReport: {report.get('path', '')}")
    return lines
