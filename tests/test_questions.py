"""Questions for the author (category `intent`): "Why was this removed?" on a deleted line, "Why this special case?" on
a new line. They are placed like any finding, always minor, scored by the second look on "is the change real and
unexplained?", asked as they are in guided review, and never re-checked as open problems. No real Claude calls."""

import json
import re

from prr import analyze, ci, findings, github, judge, merge, stops
from prr.claude import ClaudeError
from prr.config import Config
from prr.util import read_json, write_json
from tests.test_deepcheck import FakeClaude, setup_pr
from tests.test_findings import lines_for


def ask(anchor: str, side: str, title: str, **kw) -> dict:
    """A question for the author as the model returns it."""
    return {"title": title, "category": "intent", "severity": "major", "side": side, "anchor": anchor, "line": 0,
            "start_line": None, "impact": f"This changes `{anchor}`. Was that on purpose? Could you say why?",
            "fix": "Say why in the PR description.", "suggestion": None, "why": "The description doesn't say.",
            "evidence": "src/core.py:34 `line 34`", "confidence": 80, **kw}


BUG = {"title": "Inserted lines skip the check", "category": "correctness", "severity": "major", "side": "RIGHT",
       "anchor": "inserted A", "line": 21, "start_line": None, "impact": "i", "fix": "f", "suggestion": None,
       "why": "w", "evidence": "diff only", "confidence": 90}


def test_a_question_on_a_deleted_line_is_placed_left_and_always_minor(tricky_repo):
    _, fl = lines_for(tricky_repo)
    f = findings.place(ask("line 34", "LEFT", "Why was line 34 removed?"), "src/core.py", fl)
    assert (f["side"], f["line"], f["anchored"], f["severity"]) == ("LEFT", 34, True, "minor")
    assert f["one_click"] is False and f["category"] == "intent"
    assert findings.comment_body(f) == "This changes `line 34`. Was that on purpose? Could you say why?"
    new = findings.place(ask("inserted B", "RIGHT", "Why add line B?", severity="nit"), "src/core.py", fl)
    assert (new["side"], new["line"], new["severity"]) == ("RIGHT", 22, "minor")
    assert findings.comment_body({**new, "impact": ""}) == "Why add line B?"   # no text: the question itself
    bug = findings.place(BUG, "src/core.py", fl)
    assert findings.comment_body(bug).startswith("**Inserted lines skip the check**\n\n*Must fix · correctness*")


def test_at_most_three_questions_per_batch_the_surest_first():
    files = [{"findings": [ask("a", "LEFT", "Q1", confidence=40), BUG, ask("b", "LEFT", "Q2", confidence=90)]},
             {"findings": [ask("c", "RIGHT", "Q3", confidence=70), ask("d", "RIGHT", "Q4", confidence=60)]}]
    assert analyze.keep_questions(files) == 1
    assert [f["title"] for f in files[0]["findings"]] == ["Inserted lines skip the check", "Q2"]
    assert [f["title"] for f in files[1]["findings"]] == ["Q3", "Q4"]


def _judged(prompt: str) -> list[str]:
    return re.findall(r"^### (f-[0-9a-f]{12})$", prompt, re.M)


def test_the_second_look_checks_questions_against_the_prs_text(tricky_repo, monkeypatch):
    _, fl = lines_for(tricky_repo)
    q = findings.place(ask("line 34", "LEFT", "Why was line 34 removed?"), "src/core.py", fl)
    bug = findings.place(BUG, "src/core.py", fl)
    pr = {"title": "Tidy the core module", "body": "Moves helpers around.",
          "commits": [{"headline": "Move helpers"}, {"headline": "Drop the old flag"}]}
    prompts: list[str] = []

    def scoring(prompt, schema, **kw):
        prompts.append(prompt)
        return {"scores": [{"id": i, "score": 8, "verdict": "keep", "reason": "ok"} for i in _judged(prompt)]}, None

    monkeypatch.setattr(judge, "run_structured", scoring)
    cache = judge.run([bug, q], {}, Config(), None, {}, lambda m: None, pr=pr)
    by_id = {tuple(_judged(p)): p for p in prompts}
    assert set(by_id) == {(bug["id"],), (q["id"],)}                 # questions go in their own batch
    asked = by_id[(q["id"],)]
    for part in ("## Questions for the author", "Is the change real?", "## The pull request (for the questions",
                 "Title: Tidy the core module", "Moves helpers around.", "- Drop the old flag",
                 "Kind: a question for the author, not a problem report", "File: src/core.py, line 34 (LEFT)"):
        assert part in asked, part
    assert "## The pull request (for the questions" not in by_id[(bug["id"],)]
    key = judge.pr_key(pr)
    assert cache[q["id"]] == {"score": 8, "verdict": "keep", "reason": "ok", "pr": key} and "pr" not in cache[bug["id"]]

    judge.run([bug, q], {}, Config(), None, cache, lambda m: None, pr=pr)
    assert len(prompts) == 2                                         # nothing changed: nothing asked again
    judge.run([bug, q], {}, Config(), None, cache, lambda m: None, pr={**pr, "body": "Removes line 34: unused."})
    assert len(prompts) == 3 and _judged(prompts[-1]) == [q["id"]]   # a new description: the question is scored again
    assert "Removes line 34: unused." in prompts[-1] and cache[q["id"]]["pr"] != key


def _analysis_with(pr_dir, ctx, raw: list[dict]) -> dict:
    items = read_json(pr_dir / "clusters.json")["clusters"][0]["items"]
    key = next(it["key"] for it in items if it["path"] == "src/core.py")
    write_json(pr_dir / "partials" / f"{key}.json", {"annotation": {
        "path": "src/core.py", "summary": "s", "risk": "high", "look_for": [], "changed_symbols": [], "notes": [],
        "findings": raw}, "blockers": [], "flow_edges": []})
    return merge.build_analysis(pr_dir)


def test_questions_flow_into_the_analysis_and_guided_review(tmp_path, tricky_repo):
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=("core",))
    write_json(pr / "progress.json", {"status": "complete"})
    nit = {**BUG, "title": "Name could be clearer", "category": "maintainability", "severity": "nit",
           "anchor": "inserted B"}
    a = _analysis_with(pr, ctx, [ask("line 34", "LEFT", "Why was line 34 removed?"), BUG, nit])
    q = next(f for f in a["findings"] if f["category"] == "intent")
    assert (q["side"], q["line"], q["anchored"], q["severity"]) == ("LEFT", 34, True, "minor")
    assert q["comment_body"] == "This changes `line 34`. Was that on purpose? Could you say why?"

    by_kind = {s["finding_id"]: s for s in a["stops"] if s["kind"] == "finding"}
    stop = by_kind[q["id"]]
    assert stop["question"] == "Why was line 34 removed?"            # the title, as it is
    assert (stop["side"], stop["line"], stop["severity"], stop["category"]) == ("LEFT", 34, "minor", "intent")
    assert stop["comment_body"] == q["comment_body"]
    bug = next(f for f in a["findings"] if f["title"] == BUG["title"])
    assert by_kind[bug["id"]]["question"] == "Is this a real problem? Inserted lines skip the check"

    # It ranks with minor findings: past a cap of 2, the nit goes first, then the question.
    prio = {s["finding_id"]: s["more"] for s in stops.build_stops(a, max_stops=2) if s["kind"] == "finding"}
    nit_id = next(f["id"] for f in a["findings"] if f["title"] == "Name could be clearer")
    assert (prio[bug["id"]], prio[q["id"]], prio[nit_id]) == (False, False, True)
    deep_ask = {**q, "sources": ["deep-check"], "severity": "major"}  # whoever found it, it stays a minor's priority
    assert stops._finding_prio(deep_ask) == 5 and stops._finding_prio({**BUG, "sources": ["deep-check"]}) == 1


class QuestionsClaude(FakeClaude):
    """The first pass returns what `notes` says for src/core.py (the first read, then any closer second read)."""

    def __init__(self, monkeypatch, notes: list[list[dict]]):
        super().__init__(monkeypatch)
        self.notes = notes
        self.annotate_prompts: list[str] = []
        self.judge_prompts: list[str] = []
        self.on_judge = self.judge_prompts.append

    def run(self, argv, *, input, cwd, env, capture_output, text, timeout):
        props = json.loads(argv[argv.index("--json-schema") + 1]).get("properties", {})
        if "files" not in props:
            return super().run(argv, input=input, cwd=cwd, env=env, capture_output=capture_output, text=text,
                               timeout=timeout)
        self.annotate_prompts.append(input)
        found = self.notes.pop(0) if len(self.notes) > 1 else self.notes[0]
        paths = re.findall(r"^### (.+)$", input.split("## Batch", 1)[-1], re.M)
        return self.reply(argv, {"files": [{"path": p, "summary": "s", "risk": "high", "look_for": [], "notes": [],
                                            "changed_symbols": [], "findings": found if p == "src/core.py" else []}
                                           for p in paths], "flow_edges": []}, 0.02)


def _analyze(tmp_path, tricky_repo, monkeypatch, notes):
    fake = QuestionsClaude(monkeypatch, notes)
    monkeypatch.setattr(ci, "fetch", lambda *a: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(github, "existing_comments", lambda *a: [])
    pr, ctx = setup_pr(tmp_path, tricky_repo, areas=())
    ctx["pr"].update(title="Tidy the core module", body="Moves the helpers into one place.",
                     commits=[{"sha": "1", "headline": "Move helpers", "author": "a"}])
    write_json(pr / "context.json", ctx)
    for name in ("clusters.json", "synthesis.json"):
        (pr / name).unlink()
    cfg = Config()
    cfg.analysis.deepcheck = False
    return fake, pr, analyze.analyze(pr, cfg, echo=lambda m: None)


def test_annotate_gets_the_description_and_commits_and_questions_reach_the_review(tmp_path, tricky_repo, monkeypatch):
    many = [ask("line 34", "LEFT", "Why was line 34 removed?", confidence=95),
            ask("line 5", "LEFT", "Why was line 5 replaced?", confidence=50),
            ask("inserted A", "RIGHT", "Why add line A?", confidence=90),
            ask("inserted B", "RIGHT", "Is the second insert meant to stay?", confidence=85), BUG]
    fake, pr, a = _analyze(tmp_path, tricky_repo, monkeypatch, [many])
    [prompt] = [p for p in fake.annotate_prompts if "### src/core.py" in p]   # a real finding: no second read
    for part in ("## Description\nMoves the helpers into one place.", "## Commits\n- Move helpers",
                 "## Questions for the author", "`intent`", "side` LEFT"):
        assert part in prompt, part
    asks = {f["title"]: f for f in a["findings"] if f["category"] == "intent"}
    assert set(asks) == {"Why was line 34 removed?", "Why add line A?", "Is the second insert meant to stay?"}   # 3 kept, surest first
    assert asks["Why was line 34 removed?"]["side"] == "LEFT"
    judged = [p for p in fake.judge_prompts if "Kind: a question for the author" in p]
    assert judged and all("Moves the helpers into one place." in p and "- Move helpers" in p for p in judged)
    assert "Why was line 34 removed?" in {s["question"] for s in a["stops"]}
    history = read_json(pr / "history.json")
    assert [x["title"] for x in history["items"]] == [BUG["title"]]   # questions are not re-checked next round


def test_a_batch_with_only_questions_still_gets_its_closer_second_read(tmp_path, tricky_repo, monkeypatch):
    first = [ask("line 34", "LEFT", "Why was line 34 removed?")]
    fake, pr, a = _analyze(tmp_path, tricky_repo, monkeypatch, [first, [BUG]])
    core = [p for p in fake.annotate_prompts if "### src/core.py" in p]
    assert len(core) == 2 and core[1].startswith("**This is a second, closer read.**")
    titles = {f["title"] for f in a["findings"]}
    assert {"Why was line 34 removed?", BUG["title"]} <= titles     # the first read's question is kept


def test_a_failed_judge_never_hides_a_question(tricky_repo, monkeypatch):
    _, fl = lines_for(tricky_repo)
    q = findings.place(ask("line 34", "LEFT", "Why was line 34 removed?"), "src/core.py", fl)

    def failing(prompt, schema, **kw):
        raise ClaudeError("timed out")

    monkeypatch.setattr(judge, "run_structured", failing)
    cache = judge.run([q], {}, Config(), None, {}, lambda m: None, pr={"title": "t", "body": "", "commits": []})
    assert judge.failed(cache[q["id"]]) and not judge.is_low(q, cache[q["id"]])
