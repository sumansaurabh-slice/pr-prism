"""Understand this PR (prr/understand.py): the steps, a step's text, its cache and its call, and the level and "Got it"
state. No real Claude calls: a fake `claude` answers every call. Nothing reaches GitHub."""

import copy
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import prr.claude
from prr import archive, diffparse, ledger, understand
from prr.config import Config
from prr.server import Guard, create_app
from prr.state import StateStore, empty_state
from prr.util import read_json, write_json, session_cookie
from tests.test_stops import write_pr

# --------------------------------------------------------------------------------------
# build_steps, on small analyses
# --------------------------------------------------------------------------------------


def hunk(old_start: int, old_lines: int, new_start: int, new_lines: int) -> dict:
    return {"id": "h", "old_start": old_start, "old_lines": old_lines, "new_start": new_start, "new_lines": new_lines,
            "header": ""}


def f(path: str, risk: str | None = "low", *, hunks=None, tests_for=None, generated=False, binary=False,
      summary=None, blob=None) -> dict:
    return {"path": path, "risk": risk, "hunks": hunks if hunks is not None else [hunk(1, 5, 1, 6)],
            "tests_for": tests_for, "generated": generated, "binary": binary,
            "summary": summary if summary is not None else f"Handles {path}. It changes a little.",
            "head_blob": blob or f"blob-{path}", "base_blob": "base", "status": "modified"}


def chapter(cid: str, *paths: str, goal: str = "Learn it.") -> dict:
    return {"id": cid, "title": cid.title(), "goal": goal, "files": [{"path": p, "why_now": ""} for p in paths]}


def walk(files: list[dict], chapters: list[dict], **kw) -> list[dict]:
    return understand.build_steps({"files": files, "chapters": chapters}, {"files": []}, **kw)


def test_steps_follow_the_reading_order():
    files = [f("b.py"), f("a.py", summary="Stores each loan, e.g. one book. It also counts fees.\n\n**More notes:**"),
             f("c.py", summary="")]
    steps = walk(files, [chapter("two", "c.py", "a.py"), chapter("one", "b.py", goal="  ")])
    assert [(s["chapter_id"], s["path"], s["order"]) for s in steps] == [("two", "c.py", 0), ("two", "a.py", 1),
                                                                         ("one", "b.py", 2)]
    a = steps[1]
    assert a["role"] == "Stores each loan, e.g. one book." and steps[0]["role"] is None
    assert (a["kind"], a["files"], a["tests"], a["skim"]) == ("file", ["a.py"], [], [])
    assert a["hunks"] == [{"old_start": 1, "old_lines": 5, "new_start": 1, "new_lines": 6}]
    assert (a["chapter_title"], a["chapter_goal"], steps[2]["chapter_goal"]) == ("Two", "Learn it.", None)
    assert all(understand.ID.fullmatch(s["id"]) for s in steps)


def test_tests_attach_to_the_code_they_test():
    files = [f("src/fees.py"), f("tests/test_fees.py", tests_for="src/fees.py"),
             f("tests/test_more_fees.py", tests_for="src/fees.py"), f("tests/test_orphan.py"),
             f("gen/api_pb2.py", generated=True), f("tests/test_api.py", tests_for="gen/api_pb2.py")]
    steps = walk(files, [chapter("tests", "tests/test_fees.py", "tests/test_orphan.py", "tests/test_api.py"),
                         chapter("core", "src/fees.py", "gen/api_pb2.py", "tests/test_more_fees.py")])
    # a paired test is no step of its own, whatever chapter it is in; one without a step to join is a step
    assert [(s["path"], s["tests"]) for s in steps] == [
        ("tests/test_orphan.py", []), ("tests/test_api.py", []),
        ("src/fees.py", ["tests/test_fees.py", "tests/test_more_fees.py"])]


def test_skim_files_are_listed_on_their_chapters_first_step():
    files = [f("a.py"), f("b.py"), f("package-lock.json", generated=True), f("logo.png", binary=True, hunks=[]),
             f("moved.py", hunks=[]), f("c.py"), f("vendor/lib.js", generated=True)]
    steps = walk(files, [chapter("one", "a.py", "package-lock.json", "b.py", "logo.png"),
                         chapter("two", "moved.py", "c.py"), chapter("generated", "vendor/lib.js")])
    assert [(s["path"], s["skim"]) for s in steps] == [
        ("a.py", ["package-lock.json", "logo.png", "vendor/lib.js"]),   # a chapter with no step: on the first step
        ("b.py", []), ("c.py", ["moved.py"])]                           # a pure rename or mode change: skim too
    assert walk([f("yarn.lock", generated=True)], [chapter("one", "yarn.lock")]) == []


def big_diff(path: str, hunks: int = 3, context: int = 60) -> str:
    """A diff whose hunks are mostly unchanged lines: 1 line out and 1 in per hunk."""
    out = [f"diff --git a/{path} b/{path}", "index 1111111..2222222 100644", f"--- a/{path}", f"+++ b/{path}"]
    for i in range(hunks):
        start = 1 + i * 200
        out.append(f"@@ -{start},{context + 1} +{start},{context + 1} @@")
        out += [f" keep {start + j}" for j in range(context)] + [f"-old {i}", f"+new {i}"]
    return "\n".join(out) + "\n"


def test_big_files_split_at_hunk_boundaries():
    hunks = [hunk(1, 80, 1, 80), hunk(200, 80, 200, 80), hunk(400, 10, 400, 10)]
    files = [f("big.py", hunks=hunks), f("tests/test_big.py", tests_for="big.py"),
             f("huge.py", hunks=[hunk(0, 0, 1, 400)]), f("edge.py", hunks=[hunk(1, 75, 1, 75), hunk(90, 75, 90, 75)])]
    steps = walk(files, [chapter("c", "big.py", "tests/test_big.py", "huge.py", "edge.py")])
    big = [s for s in steps if s["path"] == "big.py"]
    assert [s["kind"] for s in big] == ["part", "part"]
    assert [[h["old_start"] for h in s["hunks"]] for s in big] == [[1], [200, 400]]   # ≤ 150 changed lines each
    assert all(s["tests"] == ["tests/test_big.py"] and s["role"] == "Handles big.py." for s in big)
    assert len({s["id"] for s in big}) == 2
    assert [(s["path"], s["kind"]) for s in steps[2:]] == [("huge.py", "file"), ("edge.py", "file")]  # one hunk; 150

    fd = diffparse.parse(big_diff("ctx.py"))[0]
    padded = [f("ctx.py", hunks=fd.hunk_infos())]
    assert [s["kind"] for s in walk(padded, [chapter("c", "ctx.py")])] == ["part", "part"]       # hunk sizes: 3 × 61
    assert [s["kind"] for s in walk(padded, [chapter("c", "ctx.py")], diffs={"ctx.py": fd})] == ["file"]  # 3 × 2


def capped_pr() -> tuple[list[dict], list[dict]]:
    """48 steps: chapter a has 2 high-risk and 20 low-risk files, chapter b 1 medium and 25 unrated ones."""
    a = [f(f"a/high{i}.py", "high") for i in range(2)] + [f(f"a/low{i:02}.py") for i in range(20)]
    b = [f("b/med.py", "medium")] + [f(f"b/low{i:02}.py", None) for i in range(25)]
    test = f("tests/test_low00.py", tests_for="b/low00.py")
    return a + b + [test], [chapter("a", *[x["path"] for x in a]),
                            chapter("b", *[x["path"] for x in b], "tests/test_low00.py")]


def test_past_the_cap_low_risk_files_of_a_chapter_become_one_step():
    files, chapters = capped_pr()
    steps = walk(files, chapters)
    assert len(steps) == 24 and len(walk(files, chapters, max_steps=48)) == 48
    *alone, group = [s for s in steps if s["chapter_id"] == "b"]   # b saves the most, so only b is grouped
    assert [s["path"] for s in alone] == ["b/med.py"]
    assert group["kind"] == "group" and group["order"] == 23 and group["hunks"] == [] and group["role"] is None
    assert group["files"] == [f"b/low{i:02}.py" for i in range(25)] and group["path"] == "b/low00.py"
    assert group["tests"] == ["tests/test_low00.py"] and understand.ID.fullmatch(group["id"])
    assert all(s["kind"] == "file" for s in steps if s["chapter_id"] == "a")


def test_a_tight_cap_groups_medium_then_high_risk_files_too():
    files, chapters = capped_pr()
    five = walk(files, chapters, max_steps=5)
    assert [(s["chapter_id"], s["kind"], s["path"]) for s in five] == [
        ("a", "file", "a/high0.py"), ("a", "file", "a/high1.py"), ("a", "group", "a/low00.py"),
        ("b", "file", "b/med.py"), ("b", "group", "b/low00.py")]
    two = walk(files, chapters, max_steps=2)
    assert [s["kind"] for s in two] == ["group", "group"]
    assert two[0]["files"][:3] == ["a/high0.py", "a/high1.py", "a/low00.py"]      # still in reading order
    assert two[1]["files"][:2] == ["b/med.py", "b/low00.py"] and len(two[1]["files"]) == 26


def test_step_ids_are_stable_and_follow_the_code():
    files, chapters = capped_pr()
    first = walk(files, chapters)
    assert [s["id"] for s in walk(copy.deepcopy(files), copy.deepcopy(chapters))] == [s["id"] for s in first]
    expected = "und-" + hashlib.sha1(b"a/high0.py|1,5,1,6|blob-a/high0.py").hexdigest()
    assert first[0]["id"] == expected
    files[0]["head_blob"] = "edited"
    files[1]["hunks"] = [hunk(1, 5, 1, 7)]
    again = walk(files, chapters)
    changed = [a["path"] for a, b in zip(first, again) if a["id"] != b["id"]]
    assert changed == ["a/high0.py", "a/high1.py"]
    files[-2]["head_blob"] = "edited"                                # a grouped file changed: so did its group
    assert walk(files, chapters)[-1]["id"] != again[-1]["id"]


# --------------------------------------------------------------------------------------
# The app, with a fake `claude -p`
# --------------------------------------------------------------------------------------

PORT, TOKEN = 40133, "understand-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
FIELDS = {"id", "level", "before", "change", "why", "why_source", "why_missing", "connects", "terms", "look_at",
          "cached", "cost_usd"}


def answer(**over) -> dict:
    return {"before": "`core.py` listed forty numbered lines.", "change": "Line 5 now says it changed.",
            "why": "Members with very late books stopped coming back.", "why_source": "PR description",
            "why_missing": False,
            "connects": [{"path": "src/new_name.py", "line": 3, "text": "Reads   the list."},
                         {"path": "nowhere/made_up.py", "line": 1, "text": "A file that isn't there."},
                         {"path": "../outside.py", "line": 1, "text": "Outside the PR's code."},
                         {"path": "./crlf.txt", "line": 2, "text": "Also reads it."},
                         {"path": "gone.txt", "line": 1, "text": "Deleted by the PR."},
                         {"path": "noeol.txt:2", "line": 2, "text": "Line in the path."},
                         {"path": "with space.md", "line": 1, "text": "A fifth one."}],
            "terms": [{"term": f"term {i}", "meaning": "What it means."} for i in range(6)],
            "look_at": [{"side": "RIGHT", "line": 5}, {"side": "RIGHT", "line": 999}, {"side": "LEFT", "line": 5},
                        {"side": "RIGHT", "line": 5}, {"side": "RIGHT", "line": 19}, {"side": "RIGHT", "line": 20}],
            **over}


class FakeClaude:
    """Stands in for `claude -p --output-format json`: records each call and answers with `answer`, or fails with
    `fail` ("error" or "malformed"). With `gate`, each call waits for it first."""

    def __init__(self, monkeypatch):
        self.calls: list[dict] = []
        self.lock = threading.Lock()
        self.answer = answer()
        self.fail: str | None = None
        self.gate: threading.Event | None = None
        monkeypatch.setattr(prr.claude, "subprocess", SimpleNamespace(run=self.run,
                                                                      TimeoutExpired=subprocess.TimeoutExpired))

    def run(self, argv, *, input, cwd, env, capture_output, text, timeout):
        with self.lock:
            self.calls.append({"argv": argv, "prompt": input, "cwd": cwd, "timeout": timeout})
        if self.gate is not None:
            self.gate.wait(10)
        data = {"type": "result", "subtype": "success", "is_error": False, "total_cost_usd": 0.07, "duration_ms": 10,
                "structured_output": self.answer}
        if self.fail == "error":
            data.update(subtype="error_max_turns", is_error=True, structured_output=None)
        elif self.fail == "malformed":
            data["structured_output"] = {"before": "no other fields"}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")


NOTES = [{"side": "RIGHT", "start": 19, "end": 20, "kind": "explain", "text": "Two lines go in before line 21."},
         {"side": "RIGHT", "start": 5, "end": 5, "kind": "risk", "text": "RISK NOTE TEXT"}]


@pytest.fixture()
def pr(tmp_path, tricky_repo, monkeypatch):
    """The tricky repo's PR with file notes, a PR description, a commit and a caller in impact.json, and the app."""
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    pr_dir = tmp_path / "o__r" / "pr-1"
    pr_dir.mkdir(parents=True)
    write_pr(pr_dir, tricky_repo["diff"], notes=NOTES)
    ctx = read_json(pr_dir / "context.json")
    ctx["pr"].update(merge_base_sha=tricky_repo["base"], head_sha=tricky_repo["head"], title="Cap the late fee",
                     body="Members with very late books stopped coming back, so we cap the fee.",
                     commits=[{"sha": "c1", "headline": "Cap the fee at five dollars", "author": "a"}])
    ctx.update(repo_root=str(tricky_repo["root"]), worktree=str(tricky_repo["root"]))
    write_json(pr_dir / "context.json", ctx)
    write_json(pr_dir / "impact.json", {"engine": "git grep", "symbols": [
        {"symbol": "late_fee", "defined_in": "src/core.py", "count": 1, "too_common": False,
         "outside_refs": [{"path": "billing/invoice.py", "line": 42, "text": "total += late_fee(loan)"}]}]})
    fake = FakeClaude(monkeypatch)
    client = TestClient(Guard(create_app(pr_dir, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set(session_cookie(PORT), TOKEN)
    return SimpleNamespace(client=client, dir=pr_dir, ctx=ctx, fake=fake, repo=tricky_repo)


def steps_of(pr) -> dict[str, dict]:
    r = pr.client.get("/api/understand")
    assert r.status_code == 200, r.text
    return {s["path"]: s for s in r.json()["steps"]}


def explain(pr, sid: str, level: str = "new"):
    return pr.client.post(f"/api/understand/steps/{sid}", json={"level": level}, headers=ORIGIN)


def test_the_walk_is_listed_without_calling_claude(pr):
    body = pr.client.get("/api/understand").json()
    assert set(body) == {"steps", "level", "understood"} and (body["level"], body["understood"]) == ("new", {})
    assert [s["path"] for s in body["steps"]] == ["café.txt", "crlf.txt", "gone.txt", "noeol.txt", "src/core.py",
                                                  "src/new_name.py", "with space.md"]
    first = body["steps"][0]
    assert first["skim"] == ["logo.png", "run.sh"] and all(s["skim"] == [] for s in body["steps"][1:])
    assert (first["chapter_id"], first["chapter_title"]) == ("core", "Main logic")
    core = body["steps"][4]
    assert core["role"] == "Core." and len(core["hunks"]) == 3 and core["kind"] == "file"
    assert pr.fake.calls == []


def test_a_step_gets_its_text_from_one_scoped_call(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    r = explain(pr, sid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert set(out) == FIELDS
    assert (out["id"], out["level"], out["cached"], out["cost_usd"]) == (sid, "new", False, 0.07)
    assert out["why"] == "Members with very late books stopped coming back."
    assert (out["why_source"], out["why_missing"]) == ("PR description", False)
    # only places that exist in the PR's code, at most 4; terms at most 4; look_at only lines in this step's diff
    assert out["connects"] == [{"path": "src/new_name.py", "line": 3, "text": "Reads the list."},
                               {"path": "crlf.txt", "line": 2, "text": "Also reads it."},
                               {"path": "gone.txt", "line": 1, "text": "Deleted by the PR."},
                               {"path": "noeol.txt", "line": 2, "text": "Line in the path."}]
    assert [t["term"] for t in out["terms"]] == ["term 0", "term 1", "term 2", "term 3"]
    assert out["look_at"] == [{"side": "RIGHT", "line": 5}, {"side": "LEFT", "line": 5}, {"side": "RIGHT", "line": 19}]

    [call] = pr.fake.calls
    argv = call["argv"]
    root = os.path.realpath(pr.repo["root"])
    assert argv[argv.index("--model") + 1] == "sonnet" and argv[argv.index("--max-turns") + 1] == "12"
    assert argv[argv.index("--tools") + 1] == "Read,Grep,Glob"
    assert f"Read(/{root}/**)" in argv and argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert call["timeout"] == 180 and Path(call["cwd"]) == Path(pr.ctx["worktree"])
    assert set(json.loads(argv[argv.index("--json-schema") + 1])["required"]) == FIELDS - {"id", "level", "cached",
                                                                                          "cost_usd"}
    [line] = [x for x in ledger.read(pr.dir) if x.get("purpose")]
    assert {k: line[k] for k in ("purpose", "model", "target", "cost_usd", "outcome", "run_id")} == {
        "purpose": "understand", "model": "sonnet", "target": f"{sid}:new", "cost_usd": 0.07, "outcome": "ok",
        "run_id": None}
    assert read_json(pr.dir / "understand" / f"{sid}-new.json")["explanation"]["before"] == out["before"]


def test_the_prompt_has_the_pr_the_chapter_notes_and_callers(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    explain(pr, sid)
    prompt = pr.fake.calls[0]["prompt"]
    for text in ("Cap the late fee", "Members with very late books stopped coming back", "Cap the fee at five dollars",
                 '"Main logic"', "Understand the main change", "What a first read of the whole file says: Core.",
                 "R19-20: Two lines go in before line 21.", "billing/invoice.py:42 `total += late_fee(loan)`",
                 understand.LEVEL_TEXT["new"], "R22 +inserted B", "Explain, don't judge", "why_missing"):
        assert text in prompt, text
    assert "RISK NOTE TEXT" not in prompt and "{{" not in prompt     # notes that judge stay out
    explain(pr, sid, "familiar")
    assert understand.LEVEL_TEXT["familiar"] in pr.fake.calls[1]["prompt"]


def test_the_old_code_is_read_at_the_merge_base_never_the_head(pr, tmp_path):
    other = tmp_path / "worktree"                 # the worktree holds the head; here it holds something else too
    (other / "src").mkdir(parents=True)
    (other / "src/core.py").write_text("HEAD ONLY MARKER\n")
    pr.ctx["worktree"] = str(other)
    write_json(pr.dir / "context.json", pr.ctx)
    steps = steps_of(pr)
    explain(pr, steps["src/core.py"]["id"])
    explain(pr, steps["src/new_name.py"]["id"])
    explain(pr, steps["café.txt"]["id"])
    core, renamed, added = (c["prompt"] for c in pr.fake.calls)
    assert all("HEAD ONLY MARKER" not in p for p in (core, renamed, added))
    old = core.split("The old version around the change (src/core.py at the PR's base, commit "
                     f"{pr.repo['base'][:10]}", 1)[1].split("```", 2)[1]
    assert " 5  line 5\n" in old and "34  line 34\n" in old and "40  line 40" in old  # ±40 lines: the whole file
    assert "changed" not in old and "inserted" not in old
    old = renamed.split("(src/old_name.py at the PR's base", 1)[1].split("```", 2)[1]  # a rename: its old path
    assert "10  keep 10\n" in old and "edited" not in old
    assert "This file is new in this PR, so it has no old version." in added
    assert Path(pr.fake.calls[0]["cwd"]) == other


def test_the_base_excerpt_shows_only_lines_near_each_hunk(tricky_repo):
    old = understand.base_excerpt(tricky_repo["root"], tricky_repo["base"], "src/core.py",
                                  [hunk(5, 1, 5, 1), hunk(30, 2, 30, 2)], context=2)
    assert old.splitlines() == [" 3  line 3", " 4  line 4", " 5  line 5", " 6  line 6", " 7  line 7", "…",
                                "28  line 28", "29  line 29", "30  line 30", "31  line 31", "32  line 32", "33  line 33"]
    assert understand.base_excerpt(tricky_repo["root"], tricky_repo["base"], "café.txt", [hunk(0, 0, 1, 1)]) is None


def test_the_cache_answers_again_until_the_level_the_code_or_the_key_changes(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    assert explain(pr, sid).json()["cached"] is False
    again = explain(pr, sid).json()
    assert (again["cached"], again["cost_usd"], len(pr.fake.calls)) == (True, 0.07, 1)
    familiar = explain(pr, sid, "familiar").json()
    assert (familiar["cached"], familiar["terms"], len(pr.fake.calls)) == (False, [], 2)   # no terms when familiar

    cache = pr.dir / "understand" / f"{sid}-new.json"
    write_json(cache, {**read_json(cache), "key": "from an older prompt"})
    assert explain(pr, sid).json()["cached"] is False and len(pr.fake.calls) == 3

    for f_ in pr.ctx["files"]:                      # new commits change the file: a new step, and a new text
        if f_["path"] == "src/core.py":
            f_["head_blob"] = "0" * 40
    write_json(pr.dir / "context.json", pr.ctx)
    new_sid = steps_of(pr)["src/core.py"]["id"]
    assert new_sid != sid and explain(pr, sid).status_code == 404
    assert explain(pr, new_sid).json()["cached"] is False and len(pr.fake.calls) == 4


def test_the_cache_key_follows_prompt_model_base_blob_hunks_and_level(monkeypatch):
    ctx = {"pr": {"merge_base_sha": "m1"}, "files": [{"path": "a.py", "head_blob": "h1", "base_blob": "b1"}]}
    step = {"kind": "file", "path": "a.py", "hunks": [hunk(1, 2, 1, 3)], "files": ["a.py"]}
    key = understand.cache_key(step, "new", ctx, "sonnet")
    assert understand.cache_key(copy.deepcopy(step), "new", copy.deepcopy(ctx), "sonnet") == key
    assert understand.cache_key(step, "familiar", ctx, "sonnet") != key
    assert understand.cache_key(step, "new", ctx, "opus") != key
    assert understand.cache_key({**step, "hunks": [hunk(1, 2, 1, 4)]}, "new", ctx, "sonnet") != key
    assert understand.cache_key(step, "new", {**ctx, "pr": {"merge_base_sha": "m2"}}, "sonnet") != key
    assert understand.cache_key(step, "new", {**ctx, "files": [{"path": "a.py", "head_blob": "h2"}]}, "sonnet") != key
    monkeypatch.setattr(understand, "PROMPT_VERSION", understand.PROMPT_VERSION + "-next")
    assert understand.cache_key(step, "new", ctx, "sonnet") != key


def test_a_reason_nobody_stated_is_shown_as_missing(pr):
    steps = steps_of(pr)
    pr.fake.answer = answer(why=None, why_source=None, why_missing=True)
    assert explain(pr, steps["crlf.txt"]["id"]).json()["why_missing"] is True
    pr.fake.answer = answer(why_source=None)                  # a reason with no source counts as missing
    out = explain(pr, steps["noeol.txt"]["id"]).json()
    assert (out["why"], out["why_source"], out["why_missing"]) == (None, None, True)
    pr.fake.answer = answer(why_missing=True)
    assert explain(pr, steps["gone.txt"]["id"]).json()["why"] is None


def test_a_failed_call_is_a_502_and_nothing_is_cached(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    pr.fake.fail = "error"
    r = explain(pr, sid)
    assert r.status_code == 502 and "error_max_turns" in r.json()["detail"]
    pr.fake.fail = "malformed"
    assert explain(pr, sid).status_code == 502
    assert not (pr.dir / "understand" / f"{sid}-new.json").exists()
    assert [x["outcome"] for x in ledger.read(pr.dir) if x.get("purpose")] == ["error", "malformed"]
    pr.fake.fail = None
    assert explain(pr, sid).status_code == 200 and len(pr.fake.calls) == 3   # Try again works


def wait_for(cond, seconds: float = 5.0) -> None:
    deadline = time.monotonic() + seconds
    while not cond():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_two_requests_for_the_same_step_and_level_make_one_call(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    pr.fake.gate = threading.Event()
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(explain(pr, sid).json())) for _ in range(2)]
    for t in threads:
        t.start()
    wait_for(lambda: len(pr.fake.calls) >= 1)
    time.sleep(0.3)                                  # the second request is waiting for the first, not calling
    assert len(pr.fake.calls) == 1
    pr.fake.gate.set()
    for t in threads:
        t.join(10)
    assert sorted(r["cached"] for r in results) == [False, True] and len(pr.fake.calls) == 1


def test_another_level_of_the_same_step_does_not_wait(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    pr.fake.gate = threading.Event()
    threads = [threading.Thread(target=explain, args=(pr, sid, level)) for level in ("new", "familiar")]
    for t in threads:
        t.start()
    wait_for(lambda: len(pr.fake.calls) == 2)        # both calls run at once
    pr.fake.gate.set()
    for t in threads:
        t.join(10)


def test_unknown_steps_and_levels(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    assert explain(pr, "und-" + "0" * 40).status_code == 404
    assert explain(pr, "not-a-step").status_code == 404
    assert explain(pr, sid, "expert").status_code == 400
    assert pr.client.put("/api/understand/level", json={"level": "expert"}, headers=ORIGIN).status_code == 400
    assert pr.fake.calls == []


# --------------------------------------------------------------------------------------
# State: the level and "Got it", kept by `prr clean` and restored
# --------------------------------------------------------------------------------------


def test_the_level_is_remembered(pr):
    state = pr.client.get("/api/state").json()
    assert (state["understand_level"], state["understood_steps"]) == ("new", {})
    r = pr.client.put("/api/understand/level", json={"level": "familiar"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json() == {"level": "familiar"}
    assert pr.client.get("/api/understand").json()["level"] == "familiar"
    assert StateStore(pr.dir).load()["understand_level"] == "familiar"


def test_got_it_marks_a_step_and_can_be_taken_back(pr):
    sid = steps_of(pr)["src/core.py"]["id"]
    url = f"/api/understand/steps/{sid}/understood"
    first = pr.client.post(url, json={"understood": True}, headers=ORIGIN).json()["understood"]
    assert list(first) == [sid] and re.fullmatch(r"\d{4}-\d\d-\d\dT.*", first[sid])
    assert pr.client.post(url, json={"understood": True}, headers=ORIGIN).json()["understood"] == first  # first time kept
    assert pr.client.get("/api/understand").json()["understood"] == first
    assert StateStore(pr.dir).load()["understood_steps"] == first
    assert pr.client.post(url, json={"understood": False}, headers=ORIGIN).json() == {"understood": {}}
    gone = "/api/understand/steps/und-" + "0" * 40 + "/understood"
    assert pr.client.post(gone, json={"understood": True}, headers=ORIGIN).status_code == 404
    assert pr.client.post(gone, json={"understood": False}, headers=ORIGIN).json() == {"understood": {}}


def test_clean_keeps_the_level_and_got_it_marks_and_restore_brings_them_back(tmp_path):
    assert {"understand_level", "understood_steps"} <= set(archive.WORK_FIELDS)
    cfg = Config(cache_dir=tmp_path / "cache")
    old = tmp_path / "old"
    write_json(old / "state.json", empty_state())
    assert archive.save(cfg, "o", "r", 5, old) is None                # the default level alone is no work
    for extra in ({"understand_level": "familiar"}, {"understood_steps": {"und-" + "1" * 40: "2026-10-25T10:00:00+00:00"}}):
        write_json(old / "state.json", {**empty_state(), **extra})
        assert archive.save(cfg, "o", "r", 5, old) is not None
    fresh = tmp_path / "fresh"
    archive.restore(cfg, "o", "r", 5, fresh, "h")
    back = StateStore(fresh).load()
    assert back["understood_steps"] == {"und-" + "1" * 40: "2026-10-25T10:00:00+00:00"}
    assert back["understand_level"] == "new"


# --------------------------------------------------------------------------------------
# Settings and the prompt
# --------------------------------------------------------------------------------------


def test_the_model_setting(tmp_path, monkeypatch):
    from prr import config
    assert Config().models.understand == "sonnet"
    assert 'understand = "sonnet"' in (Path(__file__).resolve().parents[1] / "config.toml").read_text()
    (tmp_path / "config.toml").write_text('[models]\nunderstand = "haiku"\n')
    monkeypatch.setattr(config, "REPO_DIR", tmp_path)
    assert config.load_config().models.understand == "haiku"


def test_the_prompt_follows_the_plain_language_rules():
    raw = (understand.PROMPTS_DIR / "understand.md").read_text()
    for level in understand.LEVELS:
        text = understand.template(level)
        assert understand.style_rules() in text and understand.LEVEL_TEXT[level] in text and "{{" not in text
    assert "—" not in raw and "#" + "1" not in raw
    for banned in ("leverage", "seamless", "robust", "delve", "crucial", "comprehensive", "this ensures"):
        assert banned not in raw.lower(), banned
    for rule in ("Explain, don't judge", "Never invent a reason", "why_missing", "why_source", "at most 3 sentences",
                 "at most 4"):
        assert rule in raw, rule


def test_ask_about_a_step_without_lines_still_gives_the_chat_its_explanation():
    """An Understand step's chip with no line range (a grouped step) keeps its diff and explanation in the prompt."""
    from prr.server import ChipIn, _compose_prompt
    step = ChipIn(path="src/a.py", text="Before: it read the cache.\nWhat changes: it reads the database.")
    prompt = _compose_prompt("why?", [step])
    assert "the whole file `src/a.py`" in prompt and "it reads the database" in prompt and prompt.endswith("why?")
    assert _compose_prompt("x", [ChipIn(path="src/b.py")]) == "Context: the whole file `src/b.py` (read it as needed).\n\nx"
