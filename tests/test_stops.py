"""E2 guided review: building the stops (prr/stops.py) and the verdict API's side effects. Nothing reaches GitHub."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prr import diffparse, github, labels, merge, server, stops
from prr.config import Config
from prr.server import BEING_POSTED, STOP_CHANGED, Guard, create_app
from prr.state import StateStore
from prr.util import write_json, session_cookie
from tests.test_merge import make_pr_dir

# ---- small analyses for the pure function --------------------------------------------------------


def hunk(old_start: int, old_lines: int, new_start: int, new_lines: int, n: int = 1) -> dict:
    return {"id": f"h{n}", "old_start": old_start, "old_lines": old_lines, "new_start": new_start,
            "new_lines": new_lines, "header": ""}


def file(path: str, risk: str | None = "low", *, notes=(), look_for=(), hunks=None, adds: int = 5, dels: int = 2) -> dict:
    return {"path": path, "risk": risk, "notes": list(notes), "look_for": list(look_for), "additions": adds,
            "deletions": dels, "hunks": hunks if hunks is not None else [hunk(1, 60, 1, 60)]}


def finding(fid: str, path: str, line: int, severity: str = "major", **kw) -> dict:
    title = kw.pop("title", f"Problem {fid}")
    return {"id": fid, "path": path, "side": "RIGHT", "line": line, "start_line": None, "anchored": True,
            "anchor": kw.pop("anchor", f"code_{fid}()"), "severity": severity, "category": kw.pop("category", "correctness"),
            "title": title, "impact": "It breaks.", "why": "Because.", "confidence": 80, "judge_score": 7,
            "second_look": "judge", "comment_body": f"**{title}**", "low_confidence": False, "scenario": [],
            "question": None, "sources": ["annotate"], **kw}


def note(start: int, end: int | None = None, text: str = "This could break callers.", kind: str = "risk",
         side: str = "RIGHT") -> dict:
    return {"side": side, "start": start, "end": end or start, "kind": kind, "text": text}


def bot(cid: int, path: str, line: int | None, verdict: str = "agree", title: str = "Magic value") -> dict:
    return {"comment_id": cid, "bot": "lintbot[bot]", "path": path, "line": line, "side": "RIGHT" if line else None,
            "severity": "minor", "title": title, "body": title, "html_url": "u", "verdict": verdict,
            "verdict_reason": "The value is hard-coded."}


def check(name: str, caused: str | None = "yes", state: str = "fail") -> dict:
    ex = None if caused is None else {"summary": f"{name} fails on core.", "cause": "Line 5 changed.",
                                      "caused_by_pr": caused, "related": [], "fix": f"Update the {name} expectation."}
    return {"name": name, "kind": "check", "state": state, "url": None, "app": None, "summary": None, "log_tail": None,
            "explanation": ex}


def analysis(files: list[dict], findings=(), bots=(), checks=(), chapters=None, status: str = "complete") -> dict:
    return {"status": status, "files": files, "findings": list(findings), "bot_findings": list(bots),
            "ci": {"fetched_at": None, "head_sha": None, "checks": list(checks), "annotations": []} if checks else None,
            "chapters": chapters if chapters is not None else
            [{"id": "c1", "title": "All", "goal": "", "files": [{"path": f["path"], "why_now": ""} for f in files]}],
            "skipped_areas": [], "warnings": []}


def answered(*pairs: tuple[str, str]) -> dict:
    return {"stop_verdicts": {sid: {"verdict": v, "at": "t", "head_sha": "h", "anchor": {}, "opened_at": None,
                                    "answered_at": "t"} for sid, v in pairs}, "dismissed_suggestions": []}


def by_kind(out: list[dict], kind: str) -> list[dict]:
    return [s for s in out if s["kind"] == kind]


# ---- cap, priority, pinning and the overview counts ---------------------------------------------------


def wide() -> dict:
    """One stop of every priority, each in its own file, far apart."""
    fs = [file(p, "high" if p in ("note_high.py", "look.py") else "medium" if p == "note_med.py" else "low",
               notes=[note(10)] if p.startswith("note") else (), look_for=["Is the cache cleared?"] if p == "look.py" else ())
          for p in ("major.py", "deep.py", "bot.py", "minor.py", "note_high.py", "note_med.py", "look.py", "nit.py",
                    "quiet.py", "low.py")]
    return analysis(fs, findings=[
        finding("f-nit", "nit.py", 10, "nit"), finding("f-minor", "minor.py", 10, "minor"),
        finding("f-deep", "deep.py", 10, "minor", sources=["deep-check"]), finding("f-major", "major.py", 10, "major"),
        finding("f-low", "low.py", 10, "major", low_confidence=True)],
        bots=[bot(5, "bot.py", 10), bot(6, "bot.py", 40, verdict="disagree")], checks=[check("unit")])


def test_cap_keeps_the_riskiest_and_lists_the_rest_as_more_places():
    a = wide()
    out = stops.build_stops(a, max_stops=4)
    listed = {(s["kind"], s["path"]) for s in out if not s["more"] and not s["low_confidence"]}
    assert listed == {("finding", "major.py"), ("finding", "deep.py"), ("ci", None), ("bot", "bot.py")}
    more = [(s["kind"], s["path"]) for s in out if s["more"]]
    assert set(more) == {("finding", "minor.py"), ("risk_note", "note_high.py"), ("risk_note", "note_med.py"),
                         ("look_for", "look.py"), ("finding", "nit.py")}
    [low] = [s for s in out if s["low_confidence"]]
    assert low["finding_id"] == "f-low" and not low["more"]              # outside the cap and the counts
    assert len(by_kind(out, "bot")) == 1                                  # only findings Claude agrees with
    # three line stops in 60-line hunks (2 + 3 minutes each) and the check (2)
    assert a["review_summary"] == {"places": 4, "more_places": 5, "quiet_files": 2, "minutes_left": 17}


def test_priority_runs_through_all_nine_levels():
    out = stops.build_stops(wide(), max_stops=100)
    def prio(s):
        return next(n for n, (kind, path) in enumerate([
            ("finding", "major.py"), ("finding", "deep.py"), ("ci", None), ("bot", "bot.py"), ("finding", "minor.py"),
            ("risk_note", "note_high.py"), ("risk_note", "note_med.py"), ("look_for", "look.py"),
            ("finding", "nit.py")]) if (s["kind"], s["path"]) == (kind, path))
    shown = [s for s in out if not s["low_confidence"]]
    for cap in range(10):
        listed = {s["id"] for s in stops.build_stops(wide(), max_stops=cap) if not s["more"] and not s["low_confidence"]}
        assert listed == {s["id"] for s in shown if prio(s) < cap}


def test_answered_stops_stay_listed_past_the_cap():
    out = stops.build_stops(wide(), max_stops=2)
    nit = next(s for s in out if s["finding_id"] == "f-nit")
    assert nit["more"]
    a = wide()
    out = stops.build_stops(a, answered((nit["id"], "unsure")), max_stops=2)
    again = next(s for s in out if s["id"] == nit["id"])
    assert not again["more"] and not again["dropped"]
    assert a["review_summary"]["places"] == 3 and a["review_summary"]["more_places"] == 6
    assert a["review_summary"]["minutes_left"] == 10          # the answered one isn't left


def test_overview_counts_and_minutes():
    big = file("big.py", "high", notes=[note(5)], hunks=[hunk(1, 10, 1, 130)])
    a = analysis([big, file("quiet.py"), file("past_cap.py", notes=[]), file("only_low.py")],
                 findings=[finding("f1", "big.py", 100), finding("f2", "past_cap.py", 3, "nit"),
                           finding("f3", "only_low.py", 3, low_confidence=True)])
    stops.build_stops(a, max_stops=2)
    # big.py: a finding and a note, 130 changed lines in the hunk (no diff at hand: its size) → 2 + 5 each
    assert a["review_summary"] == {"places": 2, "more_places": 1, "quiet_files": 2, "minutes_left": 14}


def test_empty_analysis_has_no_stops():
    a = analysis([file("a.py"), file("b.py", "high")])
    assert stops.build_stops(a) == []
    assert a["review_summary"] == {"places": 0, "more_places": 0, "quiet_files": 2, "minutes_left": 0}


# ---- ids and order ------------------------------------------------------------------------------------


def test_ids_stay_the_same_across_a_rerun_with_a_changed_title():
    first = stops.build_stops(analysis([file("a.py")], findings=[finding("f-1", "a.py", 10, title="Old title",
                                                                         anchor="x = compute()")]))
    again = stops.build_stops(analysis([file("a.py")], findings=[finding("f-9", "a.py", 14, title="New wording",
                                                                         anchor="x  =  compute()")]))
    assert first[0]["id"] == again[0]["id"] and stops.ID.fullmatch(first[0]["id"])
    assert first[0]["finding_id"] != again[0]["finding_id"]
    other = stops.build_stops(analysis([file("a.py")], findings=[finding("f-1", "a.py", 10, category="security",
                                                                         anchor="x = compute()")]))
    assert other[0]["id"] != first[0]["id"]                               # category and anchor are the key


def test_note_and_look_for_ids_follow_their_text():
    def one(text: str, look: str) -> list[str]:
        return [s["id"] for s in stops.build_stops(analysis([file("a.py", "high", notes=[note(10, text=text)]),
                                                             file("b.py", "high", look_for=[look])]))]
    assert one("Callers break.", "Is it cached?") == one("Callers  break.", "is it   cached?")
    assert one("Callers break.", "Is it cached?") != one("Other text.", "Is it cached?")


def test_reading_order_is_chapter_then_file_then_line_with_ci_first():
    fs = [file("a.py"), file("b.py"), file("c.py")]
    chapters = [{"id": "two", "title": "", "goal": "", "files": [{"path": "c.py", "why_now": ""}, {"path": "a.py", "why_now": ""}]},
                {"id": "one", "title": "", "goal": "", "files": [{"path": "b.py", "why_now": ""}]}]
    out = stops.build_stops(analysis(fs, findings=[finding("f1", "a.py", 40), finding("f2", "a.py", 9),
                                                   finding("f3", "b.py", 1), finding("f4", "c.py", 50)],
                                     checks=[check("lint")], chapters=chapters))
    assert [(s["kind"], s["path"], s["line"], s["chapter"]) for s in out] == [
        ("ci", None, None, None), ("finding", "c.py", 50, "two"), ("finding", "a.py", 9, "two"),
        ("finding", "a.py", 40, "two"), ("finding", "b.py", 1, "one")]
    assert [s["order"] for s in out] == [0, 1, 2, 3, 4]


def test_stops_within_three_lines_merge_and_the_finding_wins():
    f = file("a.py", "high", notes=[note(12, 13, text="Callers read the old field."), note(30, text="Far away."),
                                    note(31, text="Next to the far one.")])
    out = stops.build_stops(analysis([f], findings=[finding("f1", "a.py", 10), finding("f2", "a.py", 11)],
                                     bots=[bot(5, "a.py", 9)]))
    findings_ = by_kind(out, "finding")
    assert [s["finding_id"] for s in findings_] == ["f1", "f2"]          # two findings never merge
    host = next(s for s in findings_ if "Callers read the old field." in (s["claude_view"] or ""))
    assert "lintbot[bot]" in [s for s in findings_ if "Magic value" in (s["claude_view"] or "")][0]["claude_view"]
    assert host["sources"] == ["annotate"]
    [far] = by_kind(out, "risk_note")                                    # two notes a line apart: one stop
    assert far["line"] == 30 and "Next to the far one." in far["claude_view"]
    assert not by_kind(out, "bot")


def test_four_lines_apart_stay_separate_and_a_bot_takes_in_a_note():
    f = file("a.py", "high", notes=[note(14, text="Four lines on."), note(42, text="Near the bot.")])
    out = stops.build_stops(analysis([f], findings=[finding("f1", "a.py", 10)], bots=[bot(5, "a.py", 40)]))
    assert [s["kind"] for s in out] == ["finding", "risk_note", "bot"]
    assert "Near the bot." in by_kind(out, "bot")[0]["claude_view"]


def test_an_answered_note_is_never_folded_into_a_neighbour():
    f = file("a.py", "high", notes=[note(11)])
    alone = stops.build_stops(analysis([f]))
    out = stops.build_stops(analysis([f], findings=[finding("f1", "a.py", 10)]), answered((alone[0]["id"], "real")))
    assert {s["kind"] for s in out} == {"finding", "risk_note"}


# ---- each kind's question -----------------------------------------------------------------------------


def test_each_kind_asks_its_own_question():
    fs = [file("a.py", "high", notes=[note(30, text="Is the lock held?")]), file("b.py", "high", look_for=["Is X cached?"]),
          file("c.py")]
    out = stops.build_stops(analysis(
        fs, findings=[finding("f1", "a.py", 3, title="Drops errors"),
                      finding("f2", "c.py", 3, title="Deep one", question="Can two calls race?",
                              scenario=["Two calls start.", "Both write."], sources=["deep-check"])],
        bots=[bot(5, "c.py", 50, title="Magic value.")], checks=[check("unit"), check("lint", "unclear"),
                                                                 check("docs", "no"), check("flaky", None),
                                                                 check("ok", "yes", state="pass")]))
    q = {(s["kind"], s["path"] or s["check_name"]): s for s in out}
    assert q[("finding", "a.py")]["question"] == "Is this a real problem? Drops errors"
    assert q[("finding", "c.py")]["question"] == "Can two calls race?"
    assert q[("finding", "c.py")]["scenario"] == ["Two calls start.", "Both write."]
    assert q[("risk_note", "a.py")]["question"] == "Is this safe? Is the lock held?"
    assert q[("risk_note", "a.py")]["comment_body"] == "Is the lock held?"
    assert q[("look_for", "b.py")]["question"] == "Is X cached?" and q[("look_for", "b.py")]["comment_body"] is None
    assert q[("bot", "c.py")]["question"] == "Do you agree with lintbot[bot]: Magic value?"
    assert q[("bot", "c.py")]["comment_body"] == "> lintbot[bot]: Magic value"
    assert q[("ci", "unit")]["question"] == "Is this failure caused by this PR? `unit` fails."
    assert q[("ci", "unit")]["comment_body"] == "The `unit` check fails. unit fails on core.\n\nFix: Update the unit expectation."
    assert {s["check_name"] for s in by_kind(out, "ci")} == {"unit", "lint", "flaky"}   # not "no", not passing


def test_look_for_items_only_for_high_risk_files_without_other_stops():
    fs = [file("busy.py", "high", look_for=["A?"]), file("calm.py", "high", look_for=["B?", "C?"]),
          file("medium.py", "medium", look_for=["D?"]), file("hidden.py", "high", look_for=["E?"])]
    out = stops.build_stops(analysis(fs, findings=[finding("f1", "busy.py", 3),
                                                   finding("f2", "hidden.py", 3, low_confidence=True)]))
    assert [(s["path"], s["question"]) for s in by_kind(out, "look_for")] == [
        ("calm.py", "B?"), ("calm.py", "C?"), ("hidden.py", "E?")]
    assert by_kind(out, "look_for")[0]["changed_lines"] == 7                   # the whole file's changes


def test_look_for_sits_on_the_files_first_changed_line(tricky_repo):
    diffs = {f.path: f for f in diffparse.parse(tricky_repo["diff"])}
    fd = diffs["src/core.py"]
    entry = file("src/core.py", "high", look_for=["Is line 5 right?"], hunks=fd.hunk_infos())
    [s] = stops.build_stops(analysis([entry]), diffs=diffs)
    assert (s["side"], s["line"]) == ("RIGHT", 5)
    [s] = stops.build_stops(analysis([entry]))                                  # without the diff: the hunk's start
    assert (s["side"], s["line"]) == ("RIGHT", 2)


def test_changed_lines_count_added_and_deleted_lines_in_the_hunk(tricky_repo):
    diffs = {f.path: f for f in diffparse.parse(tricky_repo["diff"])}
    entry = file("src/core.py", hunks=diffs["src/core.py"].hunk_infos())
    [s] = stops.build_stops(analysis([entry], findings=[finding("f1", "src/core.py", 5)]), diffs=diffs)
    assert s["changed_lines"] == 2                                              # "-line 5" and "+line 5 changed"


# ---- low confidence and earlier answers --------------------------------------------------------------


def test_low_confidence_findings_sit_outside_the_cap_and_an_answered_one_is_dropped():
    a = analysis([file("a.py")], findings=[finding("f1", "a.py", 10, low_confidence=True)])
    [s] = stops.build_stops(a, max_stops=0)
    assert s["low_confidence"] and not s["more"] and not s["dropped"]
    assert a["review_summary"]["places"] == 0 and a["review_summary"]["quiet_files"] == 1
    [s] = stops.build_stops(a, answered((s["id"], "not")), max_stops=0)
    assert s["dropped"] and not s["more"]                                       # "Second look dropped this"


def test_an_answer_whose_stop_is_gone_stays_as_a_dropped_stop():
    first = stops.build_stops(analysis([file("a.py")], findings=[finding("f1", "a.py", 10)]))[0]
    state = answered((first["id"], "real"))
    state["stop_verdicts"][first["id"]]["anchor"] = stops.anchor_of(first)
    state["stop_verdicts"]["stop-" + "0" * 40] = {"verdict": "not", "anchor": "garbage"}   # malformed: left out
    a = analysis([file("a.py")])
    [s] = stops.build_stops(a, state, max_stops=0)
    assert s["id"] == first["id"] and s["dropped"] and not s["more"]
    assert (s["path"], s["line"], s["question"], s["finding_id"]) == ("a.py", 10, first["question"], "f1")
    assert a["review_summary"]["places"] == 0 and a["review_summary"]["quiet_files"] == 1


def test_a_finding_dismissed_elsewhere_counts_as_done():
    a = analysis([file("a.py")], findings=[finding("f1", "a.py", 10)])
    stops.build_stops(a, {"stop_verdicts": {}, "dismissed_suggestions": ["f1"]})
    assert a["review_summary"]["minutes_left"] == 0 and a["review_summary"]["places"] == 1


def test_outside_links():
    body = ("See https://github.com/o/r/blob/h/a.py#L3, https://GitHub.com/o/r. and https://github.com/o/r2/x, "
            "[docs](https://example.com/a) and http://www.github.com/o/r/pull/1")
    assert stops.outside_links(body, "o", "r") == ["https://github.com/o/r2/x", "https://example.com/a"]


def test_the_review_summary_draft_grows_once_and_shrinks_back():
    text = stops.add_to_body("Overall fine.", "The `unit` check fails.")
    assert text == "Overall fine.\n\nThe `unit` check fails."
    assert stops.add_to_body(text, "The `unit` check fails.") == text
    assert stops.remove_from_body(text, "The `unit` check fails.") == "Overall fine."
    with pytest.raises(ValueError):
        stops.add_to_body("x" * stops.MAX_REVIEW_BODY, "more")


# ---- merge: the analysis keeps its stops ---------------------------------------------------------------


def write_pr(pr_dir, diff: str, *, findings=None, notes=None, bots=None, ci=None) -> None:
    make_pr_dir(pr_dir, diff)
    (pr_dir / "diff.patch").write_text(diff)
    write_json(pr_dir / "progress.json", {"status": "complete"})
    write_json(pr_dir / "clusters.json", {"clusters": [{"id": "c1", "items": [
        {"path": "src/core.py", "part": 1, "parts": 1, "key": "k1"},
        {"path": "src/new_name.py", "part": 1, "parts": 1, "key": "k2"}]}]})
    write_json(pr_dir / "partials" / "k1.json", {"annotation": {
        "path": "src/core.py", "summary": "Core.", "risk": "high", "look_for": ["Is line 5 right?"],
        "changed_symbols": [], "notes": notes if notes is not None else [
            {"side": "RIGHT", "start": 5, "end": 5, "kind": "risk", "text": "Line 5 changed without a test."}],
        "findings": findings if findings is not None else [
            {"title": "Inserted lines skip validation", "category": "correctness", "severity": "major", "side": "RIGHT",
             "anchor": "inserted B", "line": 22, "impact": "Bad input gets in.", "fix": "Validate it.",
             "suggestion": None, "why": "w", "evidence": "diff only", "confidence": 90}]}})
    write_json(pr_dir / "partials" / "k2.json", {"annotation": {
        "path": "src/new_name.py", "summary": "Renamed.", "risk": "high", "look_for": ["Is the rename finished?"],
        "changed_symbols": [], "notes": [], "findings": []}})
    if bots is not None:
        write_json(pr_dir / "bots.json", bots)
    if ci is not None:
        write_json(pr_dir / "ci.json", ci)


BOTS = {"found": [{"comment_id": 77, "bot": "lintbot[bot]", "path": "src/core.py", "line": 36, "side": "RIGHT",
                   "severity": "minor", "title": "Magic value", "body": "Magic value", "html_url": "u"}],
        "verdicts": {"77": {"verdict": "agree", "reason": "It is hard-coded."}}}
CI = {"fetched_at": None, "head_sha": "h", "annotations": [], "checks": [
    {"name": "unit", "kind": "check", "state": "fail", "url": None, "app": None, "summary": None, "log_tail": None,
     "explanation": {"summary": "test_core fails.", "cause": "Line 5 changed.", "caused_by_pr": "yes", "related": [],
                     "fix": "Update the expected value in test_core."}}]}


def test_build_analysis_keeps_unpinned_stops_after_validation(tmp_path, tricky_repo):
    write_pr(tmp_path, tricky_repo["diff"], bots=BOTS, ci=CI)
    a = merge.build_analysis(tmp_path)
    assert sorted(s["kind"] for s in a["stops"]) == ["bot", "ci", "finding", "look_for", "risk_note"]
    assert a["review_summary"]["places"] == 5 and a["skipped_areas"] == []
    assert merge.build_analysis(tmp_path, with_stops=False)["stops"] == []
    assert merge.build_analysis(tmp_path, max_stops=1)["review_summary"]["more_places"] == 4


# ---- the verdict API -------------------------------------------------------------------------------------

PORT = 40127
TOKEN = "stops-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


@pytest.fixture()
def pr(tmp_path, tricky_repo, monkeypatch):
    """A PR folder where the cache keeps it (<cache>/o__r/pr-1), with a finding, a risk note, a look_for item, another
    tool's finding and a failing check, and the app on it."""
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: "h")
    monkeypatch.setattr(server, "viewer_login", lambda: "me")
    pr_dir = tmp_path / "o__r" / "pr-1"
    pr_dir.mkdir(parents=True)
    write_pr(pr_dir, tricky_repo["diff"], bots=BOTS, ci=CI)
    client = TestClient(Guard(create_app(pr_dir, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set(session_cookie(PORT), TOKEN)
    return SimpleNamespace(client=client, dir=pr_dir, repo=tmp_path / "o__r", diff=tricky_repo["diff"])


def listed(pr) -> dict[str, dict]:
    r = pr.client.get("/api/analysis")
    assert r.status_code == 200, r.text
    return {s["kind"]: s for s in r.json()["stops"]}


def say(pr, sid: str, verdict: str, **extra):
    return pr.client.post(f"/api/stops/{sid}/verdict", json={"verdict": verdict, **extra}, headers=ORIGIN)


def undo(pr, sid: str):
    return pr.client.delete(f"/api/stops/{sid}/verdict", headers=ORIGIN)


def state(pr) -> dict:
    return StateStore(pr.dir).load()


def label_rows(pr) -> list[tuple]:
    return [(x["kind"], x["origin_id"], x["stop_kind"]) for x in labels.read(pr.repo)]


def test_real_on_a_finding_makes_one_comment_even_on_a_double_click(pr):
    f = listed(pr)["finding"]
    first = say(pr, f["id"], "real", opened_at="2026-10-25T10:00:00+00:00")
    assert first.status_code == 200, first.text
    again = say(pr, f["id"], "real")
    body, draft = first.json(), first.json()["draft"]
    assert again.json()["draft"]["id"] == draft["id"] and len(state(pr)["drafts"]) == 1
    assert (draft["origin_id"], draft["source"], draft["suggestion_id"], draft["line"], draft["body"]) == (
        f["id"], "suggestion", f["finding_id"], 22, f["comment_body"])
    assert draft["outside_link"] is False and body["dismissed"] is None and body["review_body_draft"] is None
    rec = state(pr)["stop_verdicts"][f["id"]]
    assert rec == body["verdict"]
    assert (rec["verdict"], rec["head_sha"], rec["opened_at"]) == ("real", "h", "2026-10-25T10:00:00+00:00")
    assert rec["at"] == rec["answered_at"] and rec["anchor"]["path"] == "src/core.py" and rec["anchor"]["line"] == 22
    assert label_rows(pr) == [("problem", f["id"], "finding")]                  # the second click wrote nothing
    [row] = labels.read(pr.repo)
    assert (row["source"], row["finding_id"], row["author"], row["line"]) == ("verdict", f["finding_id"], "me", 22)


def test_real_reuses_the_findings_own_pending_comment(pr):
    f = listed(pr)["finding"]
    own = pr.client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 22, "body": "Mine",
                                              "source": "suggestion", "suggestion_id": f["finding_id"]}, headers=ORIGIN)
    assert say(pr, f["id"], "real").json()["draft"]["id"] == own.json()["id"] and len(state(pr)["drafts"]) == 1


def test_not_dismisses_the_finding_and_undo_restores_it(pr):
    f = listed(pr)["finding"]
    r = say(pr, f["id"], "not")
    assert r.json()["dismissed"] == f["finding_id"] and r.json()["draft"] is None
    assert state(pr)["dismissed_suggestions"] == [f["finding_id"]]
    u = undo(pr, f["id"])
    assert u.json() == {"ok": True, "kept_draft": None, "restored": f["finding_id"], "review_body_draft": None}
    assert state(pr)["dismissed_suggestions"] == [] and state(pr)["stop_verdicts"] == {}
    assert label_rows(pr) == [("not-problem", f["id"], "finding"), ("retracted", f["id"], "finding")]


def test_changing_real_deletes_an_unedited_comment(pr):
    f = listed(pr)["finding"]
    say(pr, f["id"], "real")
    assert undo(pr, f["id"]).json()["kept_draft"] is None and state(pr)["drafts"] == []
    assert say(pr, f["id"], "not").json()["dismissed"] == f["finding_id"]
    assert label_rows(pr) == [("problem", f["id"], "finding"), ("retracted", f["id"], "finding"),
                              ("not-problem", f["id"], "finding")]


def test_changing_real_keeps_an_edited_comment_and_returns_it(pr):
    f = listed(pr)["finding"]
    draft = say(pr, f["id"], "real").json()["draft"]
    pr.client.patch(f"/api/drafts/{draft['id']}", json={"body": "My own words"}, headers=ORIGIN)
    kept = undo(pr, f["id"]).json()["kept_draft"]
    assert kept["id"] == draft["id"] and kept["body"] == "My own words"
    assert [d["id"] for d in state(pr)["drafts"]] == [draft["id"]]


def test_a_comment_being_posted_cant_be_taken_back(pr):
    f = listed(pr)["finding"]
    draft = say(pr, f["id"], "real").json()["draft"]

    def claim(s):
        d = next(d for d in s["drafts"] if d["id"] == draft["id"])
        d["posting"], d["posting_at"] = "p1", "2099-01-01T00:00:00+00:00"

    StateStore(pr.dir).update(claim)
    r = undo(pr, f["id"])
    assert r.status_code == 409 and r.json()["detail"] == BEING_POSTED
    assert state(pr)["stop_verdicts"][f["id"]]["verdict"] == "real"            # nothing changed


def test_risk_note_and_other_tools_real_answers_prefill_a_comment(pr):
    stops_ = listed(pr)
    note_draft = say(pr, stops_["risk_note"]["id"], "real").json()["draft"]
    assert (note_draft["line"], note_draft["body"], note_draft["source"], note_draft["origin_id"]) == (
        5, "Line 5 changed without a test.", "user", stops_["risk_note"]["id"])
    bot_draft = say(pr, stops_["bot"]["id"], "real").json()["draft"]
    assert (bot_draft["line"], bot_draft["body"]) == (36, "> lintbot[bot]: Magic value")
    assert label_rows(pr) == [("problem", stops_["risk_note"]["id"], "risk_note"),
                              ("problem", stops_["bot"]["id"], "bot")]


def test_real_on_a_failing_check_adds_its_fix_to_the_review_summary(pr):
    assert pr.client.put("/api/review-body-draft", json={"body": "Overall fine."}, headers=ORIGIN).json() == {"ok": True}
    ci = listed(pr)["ci"]
    r = say(pr, ci["id"], "real").json()
    expected = ("Overall fine.\n\nThe `unit` check fails. test_core fails.\n\n"
                "Fix: Update the expected value in test_core.")
    assert r["review_body_draft"] == expected and r["draft"] is None
    assert say(pr, ci["id"], "real").json()["review_body_draft"] == expected  # once
    assert pr.client.get("/api/state").json()["review_body_draft"] == expected
    u = undo(pr, ci["id"]).json()
    assert u["review_body_draft"] == "Overall fine." and state(pr)["review_body_draft"] == "Overall fine."
    assert label_rows(pr) == [("problem", ci["id"], "ci"), ("retracted", ci["id"], "ci")]


def test_look_for_real_waits_for_the_editor_and_a_cancelled_editor_is_not_sure(pr):
    look = listed(pr)["look_for"]
    assert (look["path"], look["line"]) == ("src/new_name.py", 10)
    r = say(pr, look["id"], "real").json()
    assert r["draft"] is None and state(pr)["drafts"] == []
    saved = pr.client.post("/api/drafts", json={"path": look["path"], "side": look["side"], "line": look["line"],
                                                "body": "Two callers still use the old name.", "source": "user",
                                                "origin_id": look["id"]}, headers=ORIGIN)
    assert saved.status_code == 200 and saved.json()["origin_id"] == look["id"]
    bad = pr.client.post("/api/drafts", json={"path": look["path"], "side": "RIGHT", "line": 10, "body": "x",
                                              "origin_id": "s-1"}, headers=ORIGIN)
    assert bad.status_code == 400
    # the other look_for flow: the editor was cancelled, so the app records Not sure
    assert say(pr, look["id"], "unsure").json()["verdict"]["verdict"] == "unsure"
    assert label_rows(pr) == [("problem", look["id"], "look_for"), ("retracted", look["id"], "look_for")]
    assert undo(pr, look["id"]).json()["kept_draft"] is None                   # "not sure" made no comment


def test_unsure_only_records(pr):
    f = listed(pr)["finding"]
    r = say(pr, f["id"], "unsure").json()
    assert (r["draft"], r["dismissed"], r["review_body_draft"]) == (None, None, None)
    s = state(pr)
    assert s["drafts"] == [] and s["dismissed_suggestions"] == [] and s["stop_verdicts"][f["id"]]["verdict"] == "unsure"
    assert labels.read(pr.repo) == []


def test_bad_ids_bad_verdicts_and_unknown_stops(pr):
    f = listed(pr)["finding"]
    assert say(pr, "stop-xyz", "real").status_code == 400
    assert say(pr, "stop-" + "A" * 40, "real").status_code == 400
    assert say(pr, f["id"], "maybe").status_code == 400
    assert say(pr, f["id"], "real", opened_at="x" * 100).status_code == 400
    assert say(pr, "stop-" + "0" * 40, "real").status_code == 404
    assert undo(pr, "stop-" + "0" * 40).status_code == 404
    assert undo(pr, "bad").status_code == 400
    assert undo(pr, f["id"]).json() == {"ok": True, "kept_draft": None, "restored": None, "review_body_draft": None}


def test_a_stop_that_changed_gets_409(pr):
    f = listed(pr)["finding"]
    write_pr(pr.dir, pr.diff, findings=[], bots=BOTS, ci=CI)                  # a re-run no longer finds it
    r = say(pr, f["id"], "real")
    assert r.status_code == 409 and r.json()["detail"] == STOP_CHANGED == "This stop changed; reload"


def test_an_answered_stop_that_disappears_stays_as_an_earlier_answer(pr):
    f = listed(pr)["finding"]
    say(pr, f["id"], "not")
    write_pr(pr.dir, pr.diff, findings=[], bots=BOTS, ci=CI)
    gone = next(s for s in pr.client.get("/api/analysis").json()["stops"] if s["id"] == f["id"])
    assert gone["dropped"] and gone["question"] == f["question"] and gone["line"] == 22 and not gone["more"]
    assert say(pr, f["id"], "real").status_code == 409
    u = undo(pr, f["id"]).json()
    assert u["restored"] == f["finding_id"] and state(pr)["stop_verdicts"] == {}
    assert label_rows(pr)[-1] == ("retracted", f["id"], "finding")


def test_generated_comment_with_an_outside_link_is_marked(pr):
    write_pr(pr.dir, pr.diff, bots=BOTS, ci=CI, findings=[
        {"title": "See the vendor docs", "category": "correctness", "severity": "major", "side": "RIGHT",
         "anchor": "inserted B", "line": 22, "impact": "Read https://example.com/limits first.", "fix": "",
         "suggestion": None, "why": "", "evidence": "", "confidence": 90}])
    f = listed(pr)["finding"]
    draft = say(pr, f["id"], "real").json()["draft"]
    assert draft["outside_link"] is True and state(pr)["drafts"][0]["outside_link"] is True


def test_a_failed_label_write_keeps_the_answer_and_warns(pr, monkeypatch):
    def busy(folder, rows, timeout=labels.LOCK_TIMEOUT_S):
        raise TimeoutError("labels.lock stayed busy for 5 s")

    monkeypatch.setattr(labels, "write", busy)
    f = listed(pr)["finding"]
    r = say(pr, f["id"], "not")
    assert r.status_code == 200 and r.json()["warning"] == "Couldn't save this answer for eval"
    assert state(pr)["stop_verdicts"][f["id"]]["verdict"] == "not"


def test_a_bug_in_building_stops_leaves_the_page_working(pr, monkeypatch):
    def broken(*a, **k):
        raise KeyError("boom")

    monkeypatch.setattr(stops, "build_stops", broken)
    r = pr.client.get("/api/analysis")
    assert r.status_code == 200
    a = r.json()
    assert a["stops"] == [] and a["findings"] and a["warnings"] == ["Guided review unavailable: 'boom'"]
    assert pr.client.get("/api/coverage").status_code == 200
    assert merge.build_analysis(pr.dir, with_stops=False)["findings"]                       # the chat's view
    with pytest.raises(KeyError):
        merge.build_analysis(pr.dir)                                                        # the CLI isn't guarded


def test_the_server_uses_its_cap_and_pins_answers_past_it(pr):
    cfg = Config()
    cfg.review.max_stops = 1
    client = TestClient(Guard(create_app(pr.dir, cfg), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set(session_cookie(PORT), TOKEN)
    a = client.get("/api/analysis").json()
    assert a["review_summary"]["places"] == 1 and a["review_summary"]["more_places"] == 4
    look = next(s for s in a["stops"] if s["kind"] == "look_for")
    assert look["more"]
    r = client.post(f"/api/stops/{look['id']}/verdict", json={"verdict": "unsure"}, headers=ORIGIN)
    assert r.status_code == 200
    a = client.get("/api/analysis").json()
    assert not next(s for s in a["stops"] if s["id"] == look["id"])["more"]
    assert a["review_summary"]["places"] == 2 and a["review_summary"]["more_places"] == 3
    on_disk = json.loads((pr.dir / "state.json").read_text())["stop_verdicts"]
    assert list(on_disk) == [look["id"]]
