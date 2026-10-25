"""The deep check ("what could break"): picking areas, what each area's agent sees, the run's time rules, failures,
the cache, the second look with evidence windows, and how its findings join the rest. No real Claude calls: a fake
`claude` answers every call."""

import json
import re
import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

import prr.claude
from prr import analyze, cli, deepcheck, diffparse, findings, judge, ledger, merge
from prr.cluster import CHARS_PER_TOKEN
from prr.config import Config
from prr.models import Analysis, Finding
from prr.util import read_json, write_json
from tests.test_merge import make_pr_dir

# --------------------------------------------------------------------------------------
# A fake `claude -p`
# --------------------------------------------------------------------------------------


def deep(path: str, anchor: str, line: int, title: str, **kw) -> dict:
    """One deep-check finding as the model returns it."""
    return {"path": path, "title": title, "category": "correctness", "severity": "major", "side": "RIGHT",
            "anchor": anchor, "line": line, "start_line": None, "impact": "Totals are off by one.",
            "fix": "Read the old value as 1.", "suggestion": None, "why": "Old rows keep the old value.",
            "evidence": f"{path}:{line}", "confidence": 70,
            "scenario": ["A saved row has x = 1.", "The new code reads it and adds 2.", "The total shows 3, not 2."],
            "question": "Can rows saved before this PR still have x = 1?",
            "evidence_refs": [{"path": path, "line": line}], **kw}


class FakeClaude:
    """Stands in for `claude -p --output-format json`. Deep-check areas answer by their title, one entry per call:
    a list of findings, or "refuse", "malformed", "error" or "hang" (sleeps until killed). The second look scores
    every finding 8 ("keep"). Nothing leaves the machine."""

    ANNOTATE_RISK = "high"

    def __init__(self, monkeypatch, answers: dict[str, list] | None = None):
        self.answers = answers or {}
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.before_area: dict[str, object] = {}   # title → called before the area answers
        self.on_judge = None                       # called with the judge prompt
        monkeypatch.setattr(prr.claude, "subprocess", types.SimpleNamespace(
            run=self.run, TimeoutExpired=subprocess.TimeoutExpired))

    def area_calls(self) -> list[dict]:
        return [c for c in self.calls if c["kind"] == "deepcheck"]

    @staticmethod
    def reply(argv, out, cost, **over) -> subprocess.CompletedProcess:
        data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost,
                "duration_ms": 10, "structured_output": out, **over}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")

    def run(self, argv, *, input, cwd, env, capture_output, text, timeout):
        props = json.loads(argv[argv.index("--json-schema") + 1]).get("properties", {})
        model = argv[argv.index("--model") + 1]
        if "scores" in props:
            ids = re.findall(r"^### (f-[0-9a-f]{12})$", input, re.M)
            with self.lock:
                self.calls.append({"kind": "judge", "ids": ids, "prompt": input})
            if self.on_judge:
                self.on_judge(input)
            return self.reply(argv, {"scores": [{"id": i, "score": 8, "verdict": "keep", "reason": "The cited code shows it."}
                                                for i in ids]}, 0.01)
        if "files" in props:      # the first pass: every file in the batch, high risk, no findings
            paths = re.findall(r"^### (.+)$", input.split("## Batch", 1)[-1], re.M)
            return self.reply(argv, {"files": [{"path": p, "summary": f"Changes {p}.", "risk": self.ANNOTATE_RISK,
                                                "look_for": [], "notes": [], "changed_symbols": [], "findings": []}
                                               for p in paths], "flow_edges": []}, 0.02)
        if "blockers" in props:
            return self.reply(argv, {"blockers": []}, 0.01)
        if "chapters" in props:
            return self.reply(argv, {"summary": {"tldr": "t", "why": "w", "how": "h", "risk": "high", "risk_reasons": []},
                                     "chapters": [{"id": "core", "title": "Core logic", "goal": "g",
                                                   "files": [{"path": "src/core.py", "why_now": "first"}]}],
                                     "flows": [], "impact": [], "questions": [], "test_files": [], "docs_files": []}, 0.1)
        title = re.search(r"^## The area to check: (.+)$", input, re.M).group(1)
        with self.lock:
            todo = self.answers.setdefault(title, [[]])
            act = todo.pop(0) if len(todo) > 1 else todo[0]
            self.calls.append({"kind": "deepcheck", "title": title, "timeout": timeout, "model": model,
                               "prompt": input, "at": time.monotonic()})
        hook = self.before_area.get(title)
        if hook:
            hook()
        if act == "hang":
            time.sleep(timeout)
            raise subprocess.TimeoutExpired(argv, timeout)
        if act == "refuse":
            return self.reply(argv, None, 0.3, result="I can't help with that.")
        if act == "error":
            return self.reply(argv, None, 0.3, subtype="error_max_turns", is_error=True)
        if act == "malformed":
            return self.reply(argv, {"findings": [{"title": "no anchor, no path"}]}, 0.4)
        return self.reply(argv, {"findings": act}, 0.5)


# One area per file, each with a line from the diff to put a finding on.
AREAS = {"core": ("Core logic", "src/core.py", "line 5 changed", 5),
         "rename": ("Renamed module", "src/new_name.py", "keep 10 edited", 10),
         "crlf": ("Windows text", "crlf.txt", "TWO", 2),
         "noeol": ("Last line", "noeol.txt", "B", 2)}


def setup_pr(tmp_path, tricky_repo, areas=("core", "rename", "crlf")) -> tuple[Path, dict]:
    """A PR folder after the summary: every file noted as high risk, one chapter per area, no findings yet."""
    pr = tmp_path / "pr"
    pr.mkdir()
    ctx = make_pr_dir(pr, tricky_repo["diff"])
    ctx["pr"].update(merge_base_sha=tricky_repo["base"], head_sha=tricky_repo["head"])
    ctx.update(repo_root=str(tricky_repo["root"]), worktree=str(tricky_repo["root"]))
    write_json(pr / "context.json", ctx)
    (pr / "diff.patch").write_text(tricky_repo["diff"])
    paths = [f["path"] for f in ctx["files"] if f["hunks"] and not f["binary"]]
    write_json(pr / "clusters.json", {"clusters": [{"id": "c1", "items": [
        {"path": p, "part": 1, "parts": 1, "key": f"k{i}"} for i, p in enumerate(paths)]}]})
    for i, p in enumerate(paths):
        write_json(pr / "partials" / f"k{i}.json", {"annotation": {
            "path": p, "summary": f"Changes {p}.", "risk": "high", "look_for": [], "changed_symbols": [], "notes": [],
            "findings": []}, "blockers": [], "flow_edges": []})
    write_json(pr / "synthesis.json", {
        "summary": {"tldr": "t", "why": "w", "how": "h", "risk": "high", "risk_reasons": []},
        "chapters": [{"id": a, "title": AREAS[a][0], "goal": f"Check {AREAS[a][0].lower()}.",
                      "files": [{"path": AREAS[a][1], "why_now": ""}]} for a in areas],
        "flows": [], "impact": [], "questions": []})
    return pr, ctx


def answer(area: str, title: str = "") -> dict:
    _, path, anchor, line = AREAS[area]
    return deep(path, anchor, line, title or f"Problem in {area}")


def start(pr: Path) -> analyze.Progress:
    progress = analyze.Progress(pr, None, lambda m: None)
    progress.update("Summary written", status="synthesizing")
    return progress


def run_deepcheck(pr: Path, ctx: dict, cfg: Config | None = None, progress: analyze.Progress | None = None,
                  force: bool = False) -> analyze.Progress:
    progress = progress or start(pr)
    diffs = {f.path: f for f in diffparse.parse((pr / "diff.patch").read_text())}
    deepcheck.run(pr, ctx, cfg or Config(), diffs, progress, force=force)
    return progress


def by_title(pr: Path) -> dict[str, dict]:
    return {f["title"]: f for f in merge.build_analysis(pr)["findings"]}


# --------------------------------------------------------------------------------------
# Picking the areas
# --------------------------------------------------------------------------------------


def ctx_of(*paths: str, sensitive: tuple[str, ...] = ()) -> dict:
    return {"files": [{"path": p, "category": "core", "generated": False, "binary": False, "hunks": [{"id": "h1"}],
                       "sensitive": p in sensitive, "base_blob": "b0", "head_blob": "h0"} for p in paths]}


def chapter(cid: str, *paths: str) -> dict:
    return {"id": cid, "title": cid.title(), "goal": f"goal {cid}", "files": [{"path": p, "why_now": ""} for p in paths]}


def test_areas_are_ranked_merged_and_capped():
    ctx = ctx_of("a.py", "b.py", "c.py", "d.py", "e.py", "f.py", "g.py", sensitive=("g.py",))
    risk = {"a.py": "high", "b.py": "high", "c.py": "medium", "d.py": "low", "e.py": "high", "f.py": "medium",
            "g.py": "low"}
    synthesis = {"chapters": [chapter("quiet", "d.py"),         # nothing risky: never checked
                              chapter("one", "a.py", "b.py"),   # 3 + 3
                              chapter("two", "./b.py", "c.py"),  # shares b.py, half of its files: joins "one"
                              chapter("three", "e.py"),          # 3
                              chapter("four", "f.py"),           # 1, plus a shown finding
                              chapter("five", "g.py")],          # sensitive path: 2
                 "flows": [{"id": "pay", "title": "Pay flow", "narrative": "money moves", "nodes": [
                     {"id": "1", "label": "x", "kind": "entry", "path": "e.py"}]},
                           {"id": "calm", "title": "Calm flow", "narrative": "", "nodes": [
                     {"id": "1", "label": "x", "kind": "entry", "path": "f.py"}]}]}  # no high-risk file: not an area
    placed = [{"path": "f.py", "low_confidence": False}, {"path": "d.py", "low_confidence": True}]

    chosen, skipped = deepcheck.pick_targets(placed, synthesis, ctx, risk=risk, max_targets=3)
    assert [(t.id, t.score) for t in chosen] == [("chapter:one+chapter:two", 7), ("chapter:three+flow:pay", 3),
                                                 ("chapter:four", 2)]
    assert chosen[0].files == ["a.py", "b.py", "c.py"] and chosen[0].title == "One + Two"
    assert chosen[1].narrative == "money moves" and chosen[1].goal == "goal three" and chosen[1].high
    assert [(t.id, t.high) for t in skipped] == [("chapter:five", False)]

    _, skipped = deepcheck.pick_targets(placed, synthesis, ctx, risk=risk, max_targets=1)
    assert [(t.id, t.high) for t in skipped] == [("chapter:three+flow:pay", True), ("chapter:four", False),
                                                 ("chapter:five", False)]

    twice = {"chapters": [chapter("same", "a.py"), chapter("same", "e.py")]}   # the model reused a chapter id
    chosen, _ = deepcheck.pick_targets([], twice, ctx, risk=risk)
    assert [t.id for t in chosen] == ["chapter:same", "chapter:same-2"]


def test_without_chapters_the_file_groups_are_the_areas():
    ctx = ctx_of("a.py", "d.py", "g.py", "docs.md", sensitive=("g.py",))
    ctx["files"][3]["category"] = "docs"
    risk = {"a.py": "high", "d.py": "low", "g.py": "low", "docs.md": "high"}
    clusters = {"clusters": [{"id": "c1", "items": [{"path": "g.py"}]},
                             {"id": "c2", "items": [{"path": "a.py"}, {"path": "d.py"}, {"path": "a.py"}]},
                             {"id": "c3", "items": [{"path": "docs.md"}]}]}   # docs are never an area
    for synthesis in (None, {"chapters": [], "flows": []}):
        chosen, skipped = deepcheck.pick_targets([], synthesis, ctx, risk=risk, clusters=clusters)
        assert [(t.id, t.files, t.score) for t in chosen] == [("group:c2", ["a.py", "d.py"], 3),
                                                              ("group:c1", ["g.py"], 2)]
        assert chosen[0].title == "Group 2: a.py, d.py" and skipped == []


def test_skipped_risky_areas_are_named_in_the_warnings(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch)
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core", "rename", "crlf"))
    cfg = Config()
    cfg.analysis.deepcheck_max_targets = 2
    run_deepcheck(pr, ctx, cfg)
    assert len(fake.area_calls()) == 2
    a = merge.build_analysis(pr)
    assert a["warnings"] == ['Deep check skipped "Windows text": it checks at most 2 areas per PR, riskiest first.']
    run = read_json(pr / deepcheck.FILE)["run"]
    assert [(t["title"], t["status"]) for t in run["targets"]] == [
        ("Core logic", "nothing-found"), ("Renamed module", "nothing-found"), ("Windows text", "skipped")]
    assert (a["progress"]["deepcheck_total"], a["progress"]["deepcheck_done"]) == (2, 2)


# --------------------------------------------------------------------------------------
# What each area's agent sees
# --------------------------------------------------------------------------------------


def test_the_diff_is_cut_to_the_budget_riskiest_files_first(tricky_repo):
    diffs = {f.path: f for f in diffparse.parse(tricky_repo["diff"])}
    files = ["src/new_name.py", "crlf.txt", "src/core.py"]
    risk = {"src/core.py": "high", "crlf.txt": "medium", "src/new_name.py": "low"}
    size = {(p, h.id): len(diffparse.render_for_llm(diffs[p], {h.id})) for p in files for h in diffs[p].hunks}

    # room for core.py's two biggest hunks only
    budget = (size["src/core.py", "h2"] + size["src/core.py", "h1"] + 5) / CHARS_PER_TOKEN
    text, left = deepcheck.diff_section(files, diffs, risk, budget)
    assert "R5 +line 5 changed" in text and "R21 +inserted A" in text and "L36 -line 36" not in text
    assert "### crlf.txt" not in text and "### src/new_name.py" not in text
    assert left == ["`src/new_name.py` (whole file)", "`crlf.txt` (whole file)", "`src/core.py` (hunks h3)"]

    # all of core.py, then crlf.txt (medium) before new_name.py (low)
    budget = (sum(v for (p, _), v in size.items() if p in ("src/core.py", "crlf.txt")) + 5) / CHARS_PER_TOKEN
    text, left = deepcheck.diff_section(files, diffs, risk, budget)
    assert left == ["`src/new_name.py` (whole file)"]
    assert text.index("### crlf.txt") < text.index("### src/core.py")   # shown in the area's file order

    text, left = deepcheck.diff_section(files, diffs, risk)            # the real budget (30k tokens) fits it all
    assert left == [] and "### src/new_name.py" in text


def test_the_area_prompt(tmp_path, tricky_repo):
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core",))
    t = deepcheck.Target("chapter:core", "Core logic", ["src/core.py"], goal="Know the core change.",
                         narrative="Input goes to `run()`, then to the database.")
    diffs = {f.path: f for f in diffparse.parse(tricky_repo["diff"])}
    view = {f["path"]: f for f in merge.build_analysis(pr)["files"]}
    already = [{"severity": "major", "path": "src/core.py", "line": 5, "title": "Line 5 can be empty"}]
    text = deepcheck.target_prompt(ctx, t, view, diffs, {"src/core.py": "high"}, already, "the retry logic",
                                   "Use tabs.")
    assert analyze.style_rules() in text and "10th-class" in text
    assert re.findall(r"\{\{([A-Z_]+)\}\}", text) == []
    assert "example values" in text and "yes/no question" in text
    for part in ("## The area to check: Core logic", "Goal: Know the core change.", "then to the database",
                 "the retry logic", "Use tabs.", "R5 +line 5 changed", "- [major] src/core.py:5 Line 5 can be empty",
                 "→ src/core.py", "Changes src/core.py."):
        assert part in text, part


# --------------------------------------------------------------------------------------
# Placement and twins
# --------------------------------------------------------------------------------------


def lines(tricky_repo, path="src/core.py") -> findings.FileLines:
    return findings.FileLines(next(f for f in diffparse.parse(tricky_repo["diff"]) if f.path == path))


def test_placement_keeps_the_scenario_question_evidence_and_sources(tricky_repo):
    fl = lines(tricky_repo)
    raw = deep("src/core.py", "line 5 changed", 40, "Old rows break", sources=["deep-check"],
               evidence_refs=[{"path": "src/core.py", "line": 5}, {"path": "src/x.py"}, {"path": "a", "line": 1},
                              {"path": "b", "line": 2}, {"path": "c", "line": 3}, {"path": "d", "line": 4}],
               scenario=[f"step {i}" for i in range(1, 9)] + [" "])
    f = findings.place(raw, "src/core.py", fl)
    assert (f["line"], f["anchored"]) == (5, True)
    assert f["scenario"] == [f"step {i}" for i in range(1, 7)]                  # at most 6 steps
    assert f["question"] == "Can rows saved before this PR still have x = 1?"
    assert f["evidence_refs"] == [{"path": "src/core.py", "line": 5}, {"path": "a", "line": 1},
                                  {"path": "b", "line": 2}, {"path": "c", "line": 3}]   # malformed dropped, at most 4
    assert f["sources"] == ["deep-check"]
    plain = findings.place({"title": "t", "category": "correctness", "anchor": "line 5 changed"}, "src/core.py", fl)
    assert (plain["scenario"], plain["question"], plain["evidence_refs"], plain["sources"]) == ([], None, [], ["annotate"])
    rule = findings.place({"title": "t", "category": "security", "anchor": "line 5 changed", "source": "rule:x"},
                          "src/core.py", fl)
    assert rule["sources"] == ["blockers"]
    for g in (f, plain, rule):  # the analysis.json shape keeps every field
        shaped = Finding.model_validate({**g, "judge_score": None, "judge_reason": None, "low_confidence": False,
                                         "second_look": "pending", "status": "new", "also_flagged_by": [],
                                         "comment_body": ""}).model_dump(mode="json")
        assert {k: shaped[k] for k in findings.DEEP_FIELDS + ("sources",)} == {
            k: g[k] for k in findings.DEEP_FIELDS + ("sources",)}


def test_twins_merge_their_sources_and_keep_the_deep_check_text(tricky_repo):
    fl = lines(tricky_repo)
    first = findings.place({"title": "Missing null check on result", "category": "correctness", "severity": "major",
                            "anchor": "inserted A", "confidence": 90}, "src/core.py", fl)
    second = findings.place({**deep("src/core.py", "inserted B", 22, "Missing null check on result value"),
                             "severity": "minor", "sources": ["deep-check"]}, "src/core.py", fl)
    kept, removed = findings.dedupe([second, first])
    assert removed == 1 and len(kept) == 1
    k = kept[0]
    assert k["id"] == first["id"] and k["severity"] == "major"                     # the more severe one stays
    assert k["sources"] == ["annotate", "deep-check"]
    assert (k["scenario"], k["question"], k["evidence_refs"]) == (second["scenario"], second["question"],
                                                                 second["evidence_refs"])
    assert first["sources"] == ["annotate"] and first["scenario"] == []            # inputs are not changed

    worse = {**second, "severity": "blocker"}
    kept, _ = findings.dedupe([first, worse])
    assert kept[0]["id"] == worse["id"] and kept[0]["sources"] == ["annotate", "deep-check"]
    assert kept[0]["scenario"] == second["scenario"]


# --------------------------------------------------------------------------------------
# The cache
# --------------------------------------------------------------------------------------


def test_cache_key_changes_with_model_focus_prompt_and_code(monkeypatch):
    ctx = ctx_of("a.py", "b.py")
    t = deepcheck.Target("chapter:x", "X", ["a.py", "b.py"])
    key = deepcheck.cache_key(t, ctx, "opus", None)
    assert deepcheck.cache_key(deepcheck.Target("other", "Y", ["b.py", "a.py"]), ctx, "opus", None) == key
    assert deepcheck.cache_key(t, ctx, "sonnet", None) != key
    assert deepcheck.cache_key(t, ctx, "opus", "retries") != key
    ctx["files"][1]["head_blob"] = "h1"
    assert deepcheck.cache_key(t, ctx, "opus", None) != key
    ctx["files"][1]["head_blob"] = "h0"
    monkeypatch.setattr(deepcheck, "PROMPT_VERSION", "2")
    assert deepcheck.cache_key(t, ctx, "opus", None) != key


def test_a_model_change_misses_the_cache(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch, {"Core logic": [[answer("core")]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core",))
    run_deepcheck(pr, ctx)
    assert len(fake.area_calls()) == 1 and "Problem in core" in by_title(pr)

    progress = run_deepcheck(pr, ctx)                 # the next run: same code, same model → from the cache
    assert len(fake.area_calls()) == 1 and "Problem in core" in by_title(pr)
    assert read_json(pr / deepcheck.FILE)["run"]["targets"][0]["cached"]
    assert read_json(pr / "progress.json")["run_id"] == progress.ledger.run_id

    cfg = Config()
    cfg.models.deepcheck = "sonnet"
    run_deepcheck(pr, ctx, cfg)
    assert [c["model"] for c in fake.area_calls()] == ["opus", "sonnet"]
    run_deepcheck(pr, ctx, cfg, force=True)           # --force asks again
    assert len(fake.area_calls()) == 3


# --------------------------------------------------------------------------------------
# Failures, retries and the time budget
# --------------------------------------------------------------------------------------


def test_one_failed_area_retries_once_and_the_others_still_merge(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch, {"Core logic": [[answer("core")]],
                                    "Renamed module": ["error", [answer("rename")]],   # fails, then the retry works
                                    "Windows text": ["error", "error"]})              # fails twice
    pr, ctx = setup_pr(tmp_path, tricky_repo)
    run_deepcheck(pr, ctx)
    tries = [c["title"] for c in fake.area_calls()]
    assert sorted(tries) == ["Core logic", "Renamed module", "Renamed module", "Windows text", "Windows text"]
    found = by_title(pr)
    assert {"Problem in core", "Problem in rename"} <= set(found)
    [warning] = merge.build_analysis(pr)["warnings"]
    assert warning.startswith('Deep check couldn\'t finish "Windows text" (area chapter:crlf, model opus, try 2, ')
    assert "error_max_turns" in warning
    rec = {t["title"]: t for t in read_json(pr / deepcheck.FILE)["run"]["targets"]}
    assert (rec["Windows text"]["status"], rec["Windows text"]["attempts"]) == ("failed", 2)
    assert (rec["Renamed module"]["status"], rec["Renamed module"]["attempts"]) == ("checked", 2)


def test_refusal_malformed_output_and_nothing_found_read_differently(tmp_path, tricky_repo, monkeypatch):
    FakeClaude(monkeypatch, {"Core logic": ["refuse"], "Renamed module": ["malformed"], "Windows text": [[]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo)
    run_deepcheck(pr, ctx)
    warnings = merge.build_analysis(pr)["warnings"]
    assert len(warnings) == 2
    assert any('"Core logic"' in w and "the model gave no result" in w for w in warnings)
    assert any('"Renamed module"' in w and "malformed output" in w for w in warnings)
    rec = {t["title"]: t for t in read_json(pr / deepcheck.FILE)["run"]["targets"]}
    assert rec["Windows text"]["status"] == "nothing-found"                  # checked, nothing found: no warning
    outcomes = sorted(x["outcome"] for x in ledger.read(pr) if x["purpose"] == "deepcheck")
    assert outcomes == ["malformed", "malformed", "ok", "refused", "refused"]
    assert read_json(pr / "progress.json")["deepcheck_done"] == 3


def test_no_start_after_the_budget_and_a_stop_after_the_grace_period(tmp_path, tricky_repo, monkeypatch):
    monkeypatch.setattr(deepcheck, "GRACE_S", 0.4)
    monkeypatch.setattr(deepcheck, "JUDGE_WAIT_S", 2.0)
    fake = FakeClaude(monkeypatch, {t: ["hang"] for t in ("Core logic", "Renamed module", "Windows text")})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core", "rename", "crlf", "noeol"))
    cfg = Config()
    cfg.analysis.deepcheck_budget_s = 0.3
    cfg.analysis.deepcheck_target_timeout_s = 30
    began = time.monotonic()
    run_deepcheck(pr, ctx, cfg)
    assert time.monotonic() - began < 3
    calls = fake.area_calls()
    assert sorted(c["title"] for c in calls) == ["Core logic", "Renamed module", "Windows text"]  # 3 at a time, no retry
    assert all(c["timeout"] <= 0.75 for c in calls)        # each `claude` is killed at budget + grace, not after 30 s
    rec = {t["title"]: t for t in read_json(pr / deepcheck.FILE)["run"]["targets"]}
    assert [rec[t]["status"] for t in ("Core logic", "Renamed module", "Windows text", "Last line")] == [
        "stopped", "stopped", "stopped", "skipped"]
    warnings = merge.build_analysis(pr)["warnings"]
    assert sum('Deep check stopped "' in w and "it ran past the time budget" in w for w in warnings) == 3
    assert 'Deep check skipped "Last line": the time budget ran out before it could start.' in warnings
    stops = [x for x in ledger.read(pr) if x["purpose"] == "deepcheck"]
    assert len(stops) == 3 and all(x["outcome"] == "timeout" and x["cost_usd"] is None for x in stops)


def test_a_retry_needs_a_full_area_time_left_in_the_budget(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch, {"Core logic": ["error", [answer("core")]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core",))
    cfg = Config()
    cfg.analysis.deepcheck_budget_s = 100
    cfg.analysis.deepcheck_target_timeout_s = 600         # the budget can never cover a second try
    run_deepcheck(pr, ctx, cfg)
    assert len(fake.area_calls()) == 1
    [warning] = merge.build_analysis(pr)["warnings"]
    assert "try 1" in warning and "Problem in core" not in by_title(pr)


# --------------------------------------------------------------------------------------
# The second look, and showing findings as each area finishes
# --------------------------------------------------------------------------------------


def test_the_judge_gets_code_windows_at_each_cited_spot(tmp_path, tricky_repo, monkeypatch):
    root = tricky_repo["root"]
    (tmp_path / "outside.txt").write_text("SECRET-VALUE\n")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    f = {**findings.place(deep("src/core.py", "line 5 changed", 5, "Old rows break"), "src/core.py", None),
         "evidence_refs": [{"path": "src/core.py", "line": 5}, {"path": "src/missing.py", "line": 3},
                           {"path": "src/core.py", "line": 999}, {"path": "../outside.txt", "line": 1},
                           {"path": "link.txt", "line": 1}]}
    prompts: list[str] = []

    def scoring(prompt, schema, **kw):
        prompts.append(prompt)
        return {"scores": [{"id": f["id"], "score": 8, "verdict": "keep", "reason": "ok"}]}, None

    monkeypatch.setattr(judge, "run_structured", scoring)
    cache = judge.run([f], {}, Config(), root, {}, lambda m: None)
    assert cache[f["id"]]["score"] == 8
    [prompt] = prompts
    assert "How it could happen:\n1. A saved row has x = 1.\n2. The new code reads it and adds 2." in prompt
    assert "Question for the reviewer: Can rows saved before this PR still have x = 1?" in prompt
    window = prompt.split("`src/core.py:5`\n```\n", 1)[1].split("```", 1)[0]
    assert ">>     5  line 5 changed" in window
    assert "       1  line 1" in window and "      13  line 13" in window and "line 14" not in window
    found = prompt.split("\n---\n", 1)[1]                   # the findings, after the instructions
    assert found.count("evidence not found") == 4           # missing file, past the end, outside the worktree (x2)
    assert "SECRET-VALUE" not in prompt
    assert "Findings from the deep check" in prompt and "evidence not found" in (Path(judge.__file__).parent
                                                                                  / "prompts/judge.md").read_text()

    plain = {**f, "scenario": [], "question": None, "evidence_refs": []}       # other findings: as before
    assert "How it could happen" not in judge._render(plain, {}, root)


def test_each_area_shows_pending_then_its_second_look(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch, {"Core logic": [[answer("core")]], "Renamed module": [[answer("rename")]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core", "rename"))
    seen: list[tuple[str, str]] = []

    def at_judge(prompt):  # what analysis.json and the app showed while the second look ran
        for f in read_json(pr / "analysis.json")["findings"]:
            if f["title"] in prompt:
                seen.append((f["title"], f["second_look"]))

    fake.on_judge = at_judge
    run_deepcheck(pr, ctx)
    assert sorted(seen) == [("Problem in core", "pending"), ("Problem in rename", "pending")]
    final = {f["title"]: f for f in read_json(pr / "analysis.json")["findings"]}
    for t in ("Problem in core", "Problem in rename"):
        assert (final[t]["second_look"], final[t]["judge_score"], final[t]["sources"]) == ("judge", 8, ["deep-check"])
        assert final[t]["scenario"] and final[t]["question"] and final[t]["evidence_refs"]
    assert [len(c["ids"]) for c in fake.calls if c["kind"] == "judge"] == [1, 1]   # each area judges only its own


def test_parallel_second_looks_never_lose_each_others_scores(tmp_path, tricky_repo, monkeypatch):
    """Area A is judged, then pauses before saving; area B is judged and saves; A resumes. Both are kept."""
    fake = FakeClaude(monkeypatch, {"Core logic": [[answer("core", "A problem")]],
                                    "Renamed module": [[answer("rename", "B problem")]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core", "rename"))
    write_json(pr / "judge.json", {"f-earlier00000": {"score": 9, "verdict": "keep", "reason": "older"}})
    a_judged, b_saved = threading.Event(), threading.Event()
    snapshots: dict[str, set[str]] = {}
    real_run, real_merge = judge.run, deepcheck.JudgeStore.merge

    def judge_run(found, diffs, cfg, cwd, cache, echo, ledger=None, pr=None):
        who = "A" if found[0]["title"] == "A problem" else "B"
        snapshots[who] = set(cache)
        out = real_run(found, diffs, cfg, cwd, cache, echo, ledger=ledger, pr=pr)
        if who == "A":
            a_judged.set()
        return out

    def merge_scores(self, scores):
        if set(scores) & ids["A"]:
            assert b_saved.wait(5), "B never saved"           # A pauses after judging, until B has written
            real_merge(self, scores)
            return
        real_merge(self, scores)
        b_saved.set()

    fake.before_area["Renamed module"] = lambda: a_judged.wait(5)   # B starts only after A was judged
    monkeypatch.setattr(deepcheck.judge, "run", judge_run)
    monkeypatch.setattr(deepcheck.JudgeStore, "merge", merge_scores)
    fls = merge.file_lines(pr)
    ids = {who: {findings.place({**answer(area, f"{who} problem"), "sources": ["deep-check"]}, AREAS[area][1],
                                fls.get(AREAS[area][1]))["id"]}
           for who, area in (("A", "core"), ("B", "rename"))}
    run_deepcheck(pr, ctx)
    assert b_saved.is_set() and not (snapshots["A"] & ids["B"])     # A judged before B's scores existed
    saved = read_json(pr / "judge.json")
    assert ids["A"] <= set(saved) and ids["B"] <= set(saved) and "f-earlier00000" in saved
    assert {f["title"]: f["second_look"] for f in by_title(pr).values()} == {"A problem": "judge", "B problem": "judge"}


def test_a_new_run_hides_the_last_runs_deep_check_until_its_own_runs(tmp_path, tricky_repo, monkeypatch):
    FakeClaude(monkeypatch, {"Core logic": ["error", "error"], "Renamed module": [[answer("rename")]]})
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core", "rename"))
    run_deepcheck(pr, ctx)
    assert "Problem in rename" in by_title(pr) and len(merge.build_analysis(pr)["warnings"]) == 1
    start(pr)                                           # a new analysis run starts: progress.json has a new run id
    assert "Problem in rename" not in by_title(pr) and merge.build_analysis(pr)["warnings"] == []


# --------------------------------------------------------------------------------------
# The whole analysis
# --------------------------------------------------------------------------------------


def test_analyze_goes_deepchecking_then_complete(tmp_path, tricky_repo, monkeypatch, capsys):
    from prr import ci, github

    fake = FakeClaude(monkeypatch, {"Core logic": [[answer("core")]]})
    statuses: list[str] = []
    fake.before_area["Core logic"] = lambda: statuses.append(read_json(pr / "progress.json")["status"])
    monkeypatch.setattr(ci, "fetch", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(github, "existing_comments", lambda *a: [])
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=())
    for name in ("clusters.json", "synthesis.json"):
        (pr / name).unlink()

    a = analyze.analyze(pr, Config(), echo=lambda m: None)
    assert statuses == ["deepchecking"]
    assert a["status"] == "complete" and Analysis.model_validate(a)
    f = next(f for f in a["findings"] if f["title"] == "Problem in core")
    assert (f["sources"], f["second_look"], f["line"]) == (["deep-check"], "judge", 5)
    assert f["scenario"][0] == "A saved row has x = 1." and f["evidence_refs"] == [{"path": "src/core.py", "line": 5}]
    assert a["warnings"] == []

    before = read_json(pr / deepcheck.SNAPSHOT_FILE)   # what eval calls "deep check off"
    assert isinstance(before, list) and "Problem in core" not in {x["title"] for x in before}
    assert all(Finding.model_validate(x).model_dump(mode="json") == x for x in before)

    calls = [x for x in ledger.read(pr) if x["purpose"] == "deepcheck"]
    assert [(x["target"], x["cost_usd"], x["model"]) for x in calls] == [("chapter:core", 0.5, "opus")]
    prog = read_json(pr / "progress.json")
    assert (prog["deepcheck_total"], prog["deepcheck_done"], prog["deepcheck_cost_usd"]) == (1, 1, 0.5)
    assert prog["deepcheck_targets"] == [{"id": "chapter:core", "title": "Core logic", "status": "checked",
                                          "cost_usd": 0.5}]
    assert a["progress"]["deepcheck_cost_usd"] == 0.5 and a["progress"]["cost_usd"] > 0.5

    capsys.readouterr()
    cli._print_summary(a, pr)
    out = capsys.readouterr().out
    assert "Deep check: 1 of 1 risky areas done, 1 of the findings above came from it." in out
    assert re.search(r"Cost of this analysis: ≈\$\d+\.\d\d, of which the deep check ≈\$0\.50\.", out)


def test_turned_off_it_asks_nothing_but_still_saves_the_before_snapshot(tmp_path, tricky_repo, monkeypatch):
    fake = FakeClaude(monkeypatch)
    pr, ctx = setup_pr(tmp_path, tricky_repo)
    cfg = Config()
    cfg.analysis.deepcheck = False
    run_deepcheck(pr, ctx, cfg)
    assert fake.calls == [] and read_json(pr / deepcheck.SNAPSHOT_FILE) == []
    progress = read_json(pr / "progress.json")
    assert progress["status"] == "synthesizing" and "turned off" in progress["message"]
    assert read_json(pr / deepcheck.FILE)["run"]["enabled"] is False

    (pr / deepcheck.SNAPSHOT_FILE).unlink()           # analyze skips it when no file group could be read
    progress = start(pr)
    diffs = {f.path: f for f in diffparse.parse((pr / "diff.patch").read_text())}
    deepcheck.run(pr, ctx, Config(), diffs, progress, skip="Claude could not read any group of files")
    assert fake.calls == [] and read_json(pr / deepcheck.SNAPSHOT_FILE) == []
    assert read_json(pr / "progress.json")["message"] == "Deep check skipped: Claude could not read any group of files"


def test_deep_check_settings_are_read_from_config(tmp_path, monkeypatch):
    from prr import config
    (tmp_path / "config.toml").write_text('[models]\ndeepcheck = "sonnet"\n\n[analysis]\ndeepcheck = false\n'
                                          "deepcheck_max_targets = 2\ndeepcheck_target_timeout_s = 300\n"
                                          "deepcheck_budget_s = 450\nconcurrency = 3\n")
    monkeypatch.setattr(config, "REPO_DIR", tmp_path)
    cfg = config.load_config()
    assert cfg.models.deepcheck == "sonnet" and cfg.analysis.deepcheck is False
    assert (cfg.analysis.deepcheck_max_targets, cfg.analysis.deepcheck_target_timeout_s,
            cfg.analysis.deepcheck_budget_s, cfg.analysis.concurrency) == (2, 300, 450, 3)
    (tmp_path / "config.toml").write_text('[analysis]\ndeepcheck = "false"\n')
    assert config.load_config().analysis.deepcheck is False
    defaults = Config()
    assert (defaults.models.deepcheck, defaults.analysis.deepcheck, defaults.analysis.deepcheck_max_targets,
            defaults.analysis.deepcheck_target_timeout_s, defaults.analysis.deepcheck_budget_s) == ("opus", True, 4, 600, 900)


def test_the_repo_config_lists_every_deep_check_key():
    text = (Path(__file__).resolve().parents[1] / "config.toml").read_text()
    for key in ('deepcheck = "opus"', "deepcheck = true", "deepcheck_max_targets = 4",
                "deepcheck_target_timeout_s = 600", "deepcheck_budget_s = 900"):
        assert key in text, key


def test_the_cli_summary_without_a_deep_check(tmp_path, capsys):
    a = {"pr": {"number": 1, "title": "t"}, "summary": None, "chapters": [], "files": [], "findings": [],
         "repair_log": [], "progress": {"status": "complete", "cost_usd": 1.25}, "warnings": ["Deep check skipped x."]}
    cli._print_summary(a, tmp_path)
    out = capsys.readouterr().out
    assert "Deep check:" not in out and "Cost of this analysis: ≈$1.25, of which the deep check ≈$0.00." in out
    assert "Warning: Deep check skipped x." in out
