"""E5 coverage before submit: the one "not checked" definition (prr/coverage.py), seen hunks and the review summary
draft. Nothing reaches GitHub."""

import pytest

from prr import coverage, deepcheck, stops
from prr.state import StateStore
from prr.util import write_json
from tests.test_stops import ORIGIN, analysis, answered, file, finding, hunk, note, pr  # noqa: F401 (fixture)


def built(a: dict, state: dict | None = None, max_stops: int = 12) -> dict:
    stops.build_stops(a, state, max_stops=max_stops)
    return a


def ids(a: dict) -> dict[str, str]:
    return {s["finding_id"] or s["question"]: s["id"] for s in a["stops"]}


def three_findings(status: str = "complete") -> dict:
    return analysis([file("a.py"), file("b.py")], status=status, findings=[
        finding("f1", "a.py", 10), finding("f2", "a.py", 40), finding("f3", "b.py", 5, "nit"),
        finding("f4", "b.py", 30, low_confidence=True), finding("f5", "b.py", 50, low_confidence=True)])


def test_hunk_ids_follow_the_header():
    assert coverage.hunk_id(hunk(10, 3, 12, 5)) == "RIGHT:12-16"
    assert coverage.hunk_id(hunk(10, 3, 9, 0)) == "LEFT:10-12"            # only deletes lines
    assert all(coverage.HUNK_ID.fullmatch(x) for x in ("RIGHT:12-16", "LEFT:1-1"))
    assert not any(coverage.HUNK_ID.fullmatch(x) for x in ("h1", "RIGHT:1", "right:1-2", "RIGHT:1-2 "))


def test_not_checked_is_every_unanswered_stop_including_more_places():
    a = built(three_findings(), max_stops=1)
    c = coverage.coverage(a, {"stop_verdicts": {}, "dismissed_suggestions": []})
    assert [(x["path"], x["line"], x["kind"]) for x in c["not_checked"]] == [
        ("a.py", 10, "finding"), ("a.py", 40, "finding"), ("b.py", 5, "finding")]   # listed first, then "more"
    assert c["not_checked"][0]["question"] == "Is this a real problem? Problem f1"
    assert (c["count"], c["low_confidence"], c["unsure"], c["running"]) == (3, 2, [], False)


def test_any_answer_or_a_dismissal_counts_as_checked_and_unsure_is_listed():
    a = built(three_findings())
    sid = ids(a)
    state = answered((sid["f1"], "real"), (sid["f2"], "unsure"), (sid["f4"], "not"))
    state["dismissed_suggestions"] = ["f3"]
    c = coverage.coverage(built(three_findings(), state), state)
    assert c["not_checked"] == [] and c["count"] == 0
    assert c["unsure"] == [{"stop_id": sid["f2"], "path": "a.py", "line": 40, "question": "Is this a real problem? Problem f2"}]
    assert c["low_confidence"] == 1                                       # f5; f4 was answered


def test_skipped_deep_check_areas_count_toward_the_warning():
    a = built(three_findings(status="deepchecking"))
    a["skipped_areas"] = [{"title": "Retries", "reason": "timed out after 10m 0s"}]
    c = coverage.coverage(a, {})
    assert c["skipped_areas"] == [{"title": "Retries", "reason": "timed out after 10m 0s"}]
    assert c["count"] == 4 and c["running"] is True


def test_high_risk_code_not_seen_counts_hunks_on_screen_and_answered_stops():
    hot = file("hot.py", "high", hunks=[hunk(1, 5, 1, 6, 1), hunk(20, 5, 21, 7, 2), hunk(40, 3, 43, 0, 3)])
    cold = file("cold.py", "high", hunks=[hunk(1, 5, 1, 5)])
    a = built(analysis([hot, cold, file("calm.py")], findings=[finding("f1", "hot.py", 25)]))
    sid = ids(a)["f1"]
    state = {**answered((sid, "not")), "seen_hunks": {"hot.py": ["RIGHT:1-6", "RIGHT:900-901"], "cold.py": []}}
    c = coverage.coverage(a, state)
    assert c["high_risk_unseen"] == {"hunks": 2, "files": 2, "by_file": {"hot.py": {"seen": 2, "total": 3},
                                                                         "cold.py": {"seen": 0, "total": 1}}}
    state["seen_hunks"] = {"hot.py": ["RIGHT:1-6", "LEFT:40-42"], "cold.py": ["RIGHT:1-5"]}
    assert coverage.coverage(a, state)["high_risk_unseen"]["hunks"] == 0


def test_an_empty_analysis_has_nothing_to_check():
    c = coverage.coverage(built(analysis([file("a.py", "high", hunks=[])])), {})
    assert c == {"not_checked": [], "unsure": [], "skipped_areas": [], "low_confidence": 0, "running": False,
                 "count": 0, "high_risk_unseen": {"hunks": 0, "files": 0, "by_file": {}}}


def test_dropped_answers_are_not_counted_as_not_checked():
    a = built(analysis([file("a.py")], findings=[finding("f1", "a.py", 10)]))
    s = a["stops"][0]
    state = answered((s["id"], "unsure"))
    state["stop_verdicts"][s["id"]]["anchor"] = stops.anchor_of(s)
    gone = built(analysis([file("a.py")]), state)
    c = coverage.coverage(gone, state)
    assert c["not_checked"] == [] and [x["stop_id"] for x in c["unsure"]] == [s["id"]]


def test_skipped_areas_come_from_this_runs_deep_check(tmp_path):
    write_json(tmp_path / "progress.json", {"status": "complete", "run_id": "r1"})
    targets = [{"title": "Checked", "status": "checked"}, {"title": "Empty", "status": "nothing-found"},
               {"title": "Failed", "status": "failed", "reason": "malformed output"},
               {"title": "Stopped", "status": "stopped", "reason": "ran past the time budget"},
               {"title": "Late", "status": "skipped", "reason": "the time budget ran out before it could start"},
               {"title": "Over the limit", "status": "skipped", "capped": True, "high": True,
                "reason": "over the area limit"},
               {"title": "Calm, over the limit", "status": "skipped", "capped": True, "high": False}]
    write_json(tmp_path / deepcheck.FILE, {"cache": {}, "run": {"run_id": "r1", "targets": targets, "warnings": []}})
    assert [x["title"] for x in deepcheck.skipped(tmp_path)] == ["Failed", "Stopped", "Late", "Over the limit"]
    write_json(tmp_path / "progress.json", {"status": "annotating", "run_id": "r2"})
    assert deepcheck.skipped(tmp_path) == []                              # a new run: not its areas


def test_add_seen_keeps_the_newest_ids():
    assert coverage.add_seen(["a", "b"], ["b", "c", "c"]) == ["a", "b", "c"]
    many = [f"RIGHT:{n}-{n}" for n in range(coverage.MAX_SEEN_PER_FILE)]
    kept = coverage.add_seen(many, ["RIGHT:0-0", "LEFT:1-1"])
    assert len(kept) == coverage.MAX_SEEN_PER_FILE and kept[-2:] == ["RIGHT:0-0", "LEFT:1-1"]
    assert "RIGHT:1-1" not in kept                                          # the oldest went


# ---- the API ------------------------------------------------------------------------------------------


def seen(pr, path: str, hunks: list[str]):  # noqa: F811
    return pr.client.post("/api/seen", json={"path": path, "hunks": hunks}, headers=ORIGIN)


def test_seen_hunks_are_saved_once_per_file(pr):  # noqa: F811
    assert seen(pr, "src/core.py", ["RIGHT:2-8", "RIGHT:18-25"]).json() == {"ok": True}
    assert seen(pr, "src/core.py", ["RIGHT:2-8", "RIGHT:33-38"]).status_code == 200
    assert StateStore(pr.dir).load()["seen_hunks"] == {"src/core.py": ["RIGHT:18-25", "RIGHT:2-8", "RIGHT:33-38"]}
    assert pr.client.get("/api/state").json()["seen_hunks"]["src/core.py"][-1] == "RIGHT:33-38"


def test_seen_input_checks(pr):  # noqa: F811
    assert seen(pr, "nope.py", ["RIGHT:1-2"]).status_code == 404
    assert seen(pr, "src/core.py", ["h1"]).status_code == 400
    too_many = [f"RIGHT:{n}-{n}" for n in range(coverage.MAX_SEEN_PER_FILE + 1)]
    assert seen(pr, "src/core.py", too_many).status_code == 400
    assert seen(pr, "src/core.py", too_many[:-1]).status_code == 200
    assert seen(pr, "src/core.py", ["LEFT:1-1"]).status_code == 200
    kept = StateStore(pr.dir).load()["seen_hunks"]["src/core.py"]
    assert len(kept) == coverage.MAX_SEEN_PER_FILE and kept[-1] == "LEFT:1-1"


def test_review_summary_draft_is_saved_and_capped(pr):  # noqa: F811
    assert pr.client.get("/api/state").json()["review_body_draft"] == ""
    body = "x" * stops.MAX_REVIEW_BODY
    assert pr.client.put("/api/review-body-draft", json={"body": body}, headers=ORIGIN).json() == {"ok": True}
    assert pr.client.get("/api/state").json()["review_body_draft"] == body
    r = pr.client.put("/api/review-body-draft", json={"body": body + "y"}, headers=ORIGIN)
    assert r.status_code == 400 and StateStore(pr.dir).load()["review_body_draft"] == body


def test_coverage_endpoint_uses_the_same_stops_as_the_review(pr):  # noqa: F811
    c = pr.client.get("/api/coverage").json()
    a = pr.client.get("/api/analysis").json()
    assert c["count"] == len(c["not_checked"]) == a["review_summary"]["places"] + a["review_summary"]["more_places"]
    assert set(c["high_risk_unseen"]["by_file"]) == {"src/core.py", "src/new_name.py"}
    first = c["not_checked"][0]["stop_id"]
    pr.client.post(f"/api/stops/{first}/verdict", json={"verdict": "unsure"}, headers=ORIGIN)
    c = pr.client.get("/api/coverage").json()
    assert first not in [x["stop_id"] for x in c["not_checked"]] and [x["stop_id"] for x in c["unsure"]] == [first]


@pytest.mark.parametrize("bad", [{"path": "src/core.py"}, {"hunks": []}])
def test_seen_needs_a_path_and_hunks(pr, bad):  # noqa: F811
    assert pr.client.post("/api/seen", json=bad, headers=ORIGIN).status_code == 422
