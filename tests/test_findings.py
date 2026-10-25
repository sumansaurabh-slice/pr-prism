from prr import blockers, bots, diffparse, findings, judge, merge, recheck
from prr.claude import ClaudeError
from prr.config import Config
from prr.util import write_json
from tests.test_merge import make_pr_dir


def lines_for(tricky_repo, path="src/core.py"):
    fd = next(f for f in diffparse.parse(tricky_repo["diff"]) if f.path == path)
    return fd, findings.FileLines(fd)


def raw(**kw):
    base = {"title": "T", "category": "correctness", "severity": "minor", "side": "RIGHT", "anchor": "", "line": 1,
            "impact": "", "fix": "", "suggestion": None, "why": "", "evidence": "", "confidence": 80}
    return {**base, **kw}


def test_placed_by_text_not_by_number(tricky_repo):
    _, fl = lines_for(tricky_repo)
    f = findings.place(raw(anchor="line 5 changed", line=40), "src/core.py", fl)
    assert (f["side"], f["line"], f["anchored"]) == ("RIGHT", 5, True)
    deleted = findings.place(raw(anchor="line 34", side="RIGHT", line=3), "src/core.py", fl)  # deleted line, wrong side
    assert (deleted["side"], deleted["line"]) == ("LEFT", 34)
    nowhere = findings.place(raw(anchor="nothing like this", line=15), "src/core.py", fl)
    assert not nowhere["anchored"]


def test_duplicates_from_overlapping_batches_merge(tricky_repo):
    _, fl = lines_for(tricky_repo)
    a = findings.place(raw(title="Missing null check on result", anchor="inserted A", severity="minor"), "src/core.py", fl)
    b = findings.place(raw(title="Missing null check on result value", anchor="inserted B", severity="major"), "src/core.py", fl)
    c = findings.place(raw(title="Slow loop", category="performance", anchor="inserted A"), "src/core.py", fl)
    kept, removed = findings.dedupe([a, b, c])
    assert removed == 1 and len(kept) == 2
    assert next(k for k in kept if k["category"] == "correctness")["severity"] == "major"  # the more severe one stays


def test_one_click_only_when_plausible(tricky_repo):
    _, fl = lines_for(tricky_repo)
    assert findings.one_click_ok(fl, "RIGHT", 21, 22, "inserted A2\ninserted B")
    assert not findings.one_click_ok(fl, "RIGHT", 21, 22, "something(totally {different")  # brackets don't balance alike
    assert not findings.one_click_ok(fl, "RIGHT", 5, 22, "x")                                 # spans two hunks
    assert not findings.one_click_ok(fl, "LEFT", 34, 34, "line 34")                           # deleted lines can't take one


def test_comment_body_is_ready_to_post():
    body = findings.comment_body({"title": "Race on balance", "severity": "major", "category": "concurrency",
                                  "impact": "Two requests overdraw.", "fix": "Lock the row.", "suggestion": "x = 1",
                                  "one_click": True, "why": "Because."})
    assert body.startswith("**Race on balance**") and "Must fix" in body and "```suggestion\nx = 1\n```" in body
    assert "<summary>Why</summary>" in body


def test_blockers_need_code_confirmation(tmp_path):
    diff = """diff --git a/api/users.py b/api/users.py
index 1111111111111111111111111111111111111111..2222222222222222222222222222222222222222 100644
--- a/api/users.py
+++ b/api/users.py
@@ -1,3 +1,4 @@
 def get_user(request):
-    require_auth(request)
     uid = request.args["id"]
+    cur.execute(f"SELECT * FROM users WHERE id = {uid}")
+    api_key = "sk-live-9f8e7d6c5b4a39281706f5e4d3c2b1a0"
"""
    fd = diffparse.parse(diff)[0]
    sql = {"rule_id": "sql-from-request", "path": "api/users.py"}
    assert blockers.corroborate(sql, fd, "") is None
    assert blockers.corroborate({"rule_id": "auth-check-removed"}, fd, "") is None
    assert blockers.corroborate({"rule_id": "hardcoded-secret"}, fd, "") is None
    assert "no added line" in blockers.corroborate({"rule_id": "destructive-migration"}, fd, "")
    unconfirmed = blockers.to_raw_finding({"rule_id": "destructive-migration", "title": "t", "side": "RIGHT", "anchor": "a",
                                           "line": 1, "impact": "i", "fix": "f", "evidence_quote": "q"}, "nothing")
    assert unconfirmed["severity"] == "major" and "not as a blocker" in unconfirmed["note"]


def test_second_look_hides_weak_findings_but_never_blockers():
    assert judge.is_low({"severity": "minor", "confidence": 90}, {"score": 4, "verdict": "keep"})
    assert not judge.is_low({"severity": "minor", "confidence": 90}, {"score": 6, "verdict": "keep"})  # "can't confirm" stays shown
    assert judge.is_low({"severity": "minor", "confidence": 90}, {"score": 8, "verdict": "drop"})
    assert not judge.is_low({"severity": "minor", "confidence": 90}, {"score": 8, "verdict": "keep"})
    assert not judge.is_low({"severity": "minor", "confidence": 90}, None)          # judge failed → keep
    assert not judge.is_low({"severity": "blocker", "confidence": 10}, {"score": 1, "verdict": "drop"})


def test_other_tools_comments():
    comments = [
        {"id": 1, "user": "review-tool[bot]", "is_bot": True, "path": "a.py", "line": 10, "side": "RIGHT",
         "body": "🔴 **Critical** · Security\n**SQL built from user input**\n\nUse parameters.", "html_url": "u"},
        {"id": 2, "user": "alice", "is_bot": False, "path": "a.py", "line": 3, "body": "nit"},
        {"id": 3, "user": "review-tool[bot]", "is_bot": True, "path": "a.py", "line": 10, "in_reply_to_id": 1, "body": "reply"},
    ]
    found = bots.from_comments(comments)
    assert len(found) == 1 and found[0]["severity"] == "major" and found[0]["title"] == "SQL built from user input"
    mine = {"path": "a.py", "line": 11, "title": "Query built from user input", "impact": "SQL injection"}
    assert bots.same_spot(mine, found[0]) and not bots.same_spot({**mine, "line": 30}, found[0])


def test_recheck_floor_and_fixed_files(tmp_path):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src/a.py").write_text("x = 1\nvalue = compute(raw)  # unchecked\n")
    ctx = {"pr": {"head_sha": "h2"}, "files": [{"path": "src/a.py", "status": "modified"}]}
    items = [
        {"id": "f-1", "kind": "finding", "title": "Unchecked value", "path": "src/a.py", "side": "RIGHT", "line": 2,
         "anchor": "value = compute(raw)  # unchecked", "old_window": "", "author": None, "html_url": None},
        {"id": "f-2", "kind": "finding", "title": "Gone file", "path": "src/old.py", "side": "RIGHT", "line": 1,
         "anchor": "y", "old_window": "", "author": None, "html_url": None},
    ]
    out = recheck.run(items, ctx, wt, None, {}, print)  # no model call needed for these two
    assert out["f-1"]["status"] == "still-open" and out["f-2"]["status"] == "fixed"


def test_other_tools_severity_comes_from_the_first_line():
    c = {"id": 9, "user": "tool[bot]", "is_bot": True, "path": "a.py", "line": 2, "side": "RIGHT", "html_url": "u",
         "body": "🟢 **Suggestion** · Concurrency\n**Blocking subprocess.run inside async code**\n..."}
    found = bots.from_comments([c, {**c, "id": 10}])                       # the same comment twice counts once
    assert len(found) == 1 and found[0]["severity"] == "nit" and found[0]["title"].startswith("Blocking subprocess")


def test_same_issue_posted_at_file_level_by_another_tool():
    mine = {"path": "gate.py", "line": 2196, "title": "Baseline arm also runs with the candidate's version overrides", "impact": ""}
    theirs = {"path": "gate.py", "line": 1, "title": "`--baseline-model` arm inherits `--version` overrides", "body": ""}
    other = {"path": "gate.py", "line": 1, "title": "Live gate wipes user data on any `--base-url`", "body": ""}
    assert bots.same_spot(mine, theirs) and not bots.same_spot(mine, other)


def test_rechecked_comment_titles_have_no_markdown():
    hunk = "@@ -1,2 +1,2 @@\n x = 1\n+value = compute(raw)"
    bot = {"id": 5, "user": "tool[bot]", "is_bot": True, "path": "a.py", "diff_hunk": hunk,
           "body": "🟡 **Warning** · Architecture\n**Offline gate does not reuse the runtime check**\n\n> ..."}
    human = {"id": 6, "user": "alice", "is_bot": False, "path": "a.py", "diff_hunk": hunk, "body": "**Why** is this here?"}
    assert recheck.comment_entry(bot)["title"] == "Offline gate does not reuse the runtime check"
    assert recheck.comment_entry(human)["title"] == "Why is this here?"
    assert recheck.comment_entry(bot)["anchor"] == "value = compute(raw)"


# ---- R3: the second look decides --------------------------------------------------------------


def test_confidence_alone_never_hides_a_finding():
    f = {"severity": "minor", "confidence": 35}
    keep6 = {"score": 6, "verdict": "keep", "reason": "can't confirm from the hunk"}
    assert not judge.is_low(f, keep6)                     # kept at 6/10, the reviewer only 35% sure → shown
    assert judge.is_low(f, keep6, floor=50)               # the eval's old-rule arm
    assert not judge.is_low(f, None) and judge.is_low(f, None, floor=50)
    assert judge.is_low(f, {"score": 8, "verdict": "drop", "reason": "r"})   # drop → hidden
    marker = {"failed": True, "reason": "timed out after 300s", "at": "t"}
    assert not judge.is_low({"severity": "minor", "confidence": 5}, marker)  # a failed second look never hides


def _judge_dir(tmp_path, tricky_repo, status="complete"):
    """A PR folder with three reviewer findings, a confirmed blocker and a finding carried over from the last round."""
    ctx = make_pr_dir(tmp_path, tricky_repo["diff"])
    (tmp_path / "diff.patch").write_text(tricky_repo["diff"])
    write_json(tmp_path / "clusters.json", {"clusters": [{"id": "c1", "items": [
        {"path": "src/core.py", "part": 1, "parts": 1, "key": "k1"}]}]})

    def finding(title, anchor, confidence):
        return {"title": title, "category": "correctness", "severity": "minor", "side": "RIGHT", "anchor": anchor,
                "line": 1, "impact": "i", "fix": "f", "suggestion": None, "why": "w", "evidence": "e",
                "confidence": confidence}

    blocker = {"rule_id": "hardcoded-secret", "title": "Secret in code", "side": "RIGHT", "anchor": "inserted B",
               "line": 22, "impact": "i", "fix": "f", "evidence_quote": "inserted B", "path": "src/core.py",
               "problem": None}
    write_json(tmp_path / "partials" / "k1.json", {"annotation": {
        "path": "src/core.py", "summary": "s", "risk": "high", "look_for": [], "changed_symbols": [], "notes": [],
        "findings": [finding("Shy but real", "line 5 changed", 35), finding("Rejected guess", "inserted A", 90),
                     finding("Judge broke on this", "line 9", 40)],
    }, "blockers": [blocker], "flow_edges": []})
    write_json(tmp_path / "recheck.json", {"results": {"earlier-1": {
        "id": "earlier-1", "kind": "finding", "status": "still-open", "title": "Earlier problem", "path": "src/core.py",
        "side": "RIGHT",
        "anchor": "line 12", "line": 12, "category": "correctness", "severity": "minor",
        "how": "the same line is still in the code"}}})
    write_json(tmp_path / "progress.json", {"status": status})
    return {f["title"]: f["id"] for f in merge.placed_findings(tmp_path, ctx)}


def test_second_look_labels_and_what_is_hidden(tmp_path, tricky_repo):
    ids = _judge_dir(tmp_path, tricky_repo)
    write_json(tmp_path / "judge.json", {
        ids["Shy but real"]: {"score": 6, "verdict": "keep", "reason": "plausible"},
        ids["Rejected guess"]: {"score": 2, "verdict": "drop", "reason": "the code already checks this"},
        ids["Judge broke on this"]: {"failed": True, "reason": "timed out after 300s", "at": "t"},
    })
    found = {f["title"]: f for f in merge.build_analysis(tmp_path)["findings"]}
    shy, rejected, broke = found["Shy but real"], found["Rejected guess"], found["Judge broke on this"]
    assert (shy["second_look"], shy["low_confidence"], shy["confidence"]) == ("judge", False, 35)
    assert (rejected["second_look"], rejected["low_confidence"]) == ("judge", True)
    assert (broke["second_look"], broke["low_confidence"], broke["judge_score"]) == ("none", False, None)
    assert found["Secret in code"]["second_look"] == "rule" and not found["Secret in code"]["low_confidence"]
    assert found["Earlier problem"]["second_look"] == "carried"


def test_findings_wait_for_their_second_look_while_the_run_is_going(tmp_path, tricky_repo):
    _judge_dir(tmp_path, tricky_repo, status="annotating")
    found = {f["title"]: f for f in merge.build_analysis(tmp_path)["findings"]}
    assert found["Shy but real"]["second_look"] == "pending" and not found["Shy but real"]["low_confidence"]
    write_json(tmp_path / "progress.json", {"status": "complete"})   # finished with no verdict and no marker
    found = {f["title"]: f for f in merge.build_analysis(tmp_path)["findings"]}
    assert found["Shy but real"]["second_look"] == "none"


def test_judge_failure_is_marked_and_retried_next_run(monkeypatch):
    found = [{"id": f"f{i}", "path": "a.py", "side": "RIGHT", "line": 1, "severity": "minor", "category": "c",
              "title": "t", "impact": "", "fix": "", "why": "", "evidence": ""} for i in range(3)]
    found.append({**found[0], "id": "b1", "severity": "blocker"})
    calls: list[str] = []

    def failing(prompt, schema, **kw):
        calls.append(prompt)
        raise ClaudeError("timed out after 300s")

    monkeypatch.setattr(judge, "run_structured", failing)
    cache = judge.run(found, {}, Config(), None, {}, lambda m: None)
    assert set(cache) == {"f0", "f1", "f2"}                    # blockers are never sent to the judge
    assert all(v["failed"] and "timed out" in v["reason"] and v["at"] for v in cache.values())
    assert judge.second_look(found[0], cache["f0"]) == "none"

    def scoring(prompt, schema, **kw):
        calls.append(prompt)
        return {"scores": [{"id": "f0", "score": 7, "verdict": "keep", "reason": "ok"},
                           {"id": "f1", "score": 3, "verdict": "keep", "reason": "weak"}]}, None

    monkeypatch.setattr(judge, "run_structured", scoring)
    cache = judge.run(found, {}, Config(), None, cache, lambda m: None)   # the next run tries the marked ones again
    assert len(calls) == 2
    assert cache["f0"] == {"score": 7, "verdict": "keep", "reason": "ok"} and cache["f1"]["score"] == 3
    assert cache["f2"]["failed"] and "no score" in cache["f2"]["reason"]  # left out by the judge → still marked
    assert judge.run(found[:2], {}, Config(), None, cache, lambda m: None) is cache and len(calls) == 2
