"""E7: honest cost. calls.jsonl is the single source of every total; chat answers show their own cost."""

import argparse
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prr import bots, ci, cli, cost, judge, ledger, recheck, server
from prr.analyze import Progress
from prr.claude import ClaudeError, run_structured
from prr.config import Config
from prr.ledger import Ledger
from prr.state import STATE_VERSION, StateStore
from prr.util import read_json, write_json, session_cookie
from tests.test_merge import make_pr_dir

ROOT = Path(__file__).resolve().parents[1]


# ---- per-turn chat cost -------------------------------------------------------------------------


def answer(thread: dict, session_id: str | None, total: float | None) -> dict:
    fields, mark = cost.turn_cost(thread, session_id, total)
    thread.update(mark)
    thread["messages"].append({"role": "assistant", **fields})
    return fields


def stop(thread: dict) -> dict:
    fields, mark = cost.stopped_turn(thread)
    thread.update(mark)
    thread["messages"].append({"role": "assistant", "error": "stopped", **fields})
    return fields


def new_thread() -> dict:
    return {"id": "t1", "session_id": "s1", "messages": []}


def test_first_turn_costs_the_whole_session_total():
    t = new_thread()
    f = answer(t, "s1", 0.79)
    assert (f["cost_usd"], f["cost_unknown"], f["includes_stopped"]) == (0.79, False, False)
    assert (t["last_session_id"], t["last_total_usd"]) == ("s1", 0.79)


def test_resumed_turn_costs_the_difference():
    t = new_thread()
    answer(t, "s1", 0.79)
    assert answer(t, "s1", 1.25)["cost_usd"] == 0.46
    assert answer(t, "s1", 1.25)["cost_usd"] == 0.0          # a free answer (all cached) costs nothing


def test_session_change_starts_counting_again():
    t = new_thread()
    answer(t, "s1", 2.0)
    f = answer(t, "s2", 0.3)
    assert f["cost_usd"] == 0.3 and t["last_session_id"] == "s2" and not cost.thread_unknown(t)


def test_negative_difference_counts_the_whole_total():
    t = new_thread()
    answer(t, "s1", 2.0)
    assert answer(t, "s1", 0.5)["cost_usd"] == 0.5


def test_stopped_answer_folds_into_the_next_one():
    t = new_thread()
    answer(t, "s1", 1.0)
    s = stop(t)
    assert (s["cost_usd"], s["cost_unknown"]) == (None, True) and cost.thread_unknown(t)
    f = answer(t, "s1", 1.6)
    assert f["cost_usd"] == 0.6 and f["includes_stopped"] and not cost.thread_unknown(t)

    first = new_thread()                                     # a stopped first answer: the next result's total covers it
    stop(first)
    assert answer(first, "s1", 0.5) == {"cost_usd": 0.5, "cost_unknown": False, "includes_stopped": True,
                                        "session_total_usd": 0.5}


def test_stopped_answer_before_a_session_change_stays_unknown():
    t = new_thread()
    answer(t, "s1", 1.0)
    stop(t)
    f = answer(t, "s2", 0.2)
    assert f["cost_usd"] == 0.2 and not f["includes_stopped"]
    assert t["cost_lost"] and cost.thread_unknown(t)        # the header says "≥ $X" from now on


def test_missing_cost_is_unknown_and_leaves_the_total_alone():
    t = new_thread()
    answer(t, "s1", 1.0)
    f = answer(t, "s1", None)
    assert (f["cost_usd"], f["cost_unknown"]) == (None, True)
    assert t["last_total_usd"] == 1.0 and cost.thread_unknown(t)
    assert answer(t, "s1", 1.5)["cost_usd"] == 0.5           # the next answer covers it


def test_long_thread_hint_after_15_answers_or_3_dollars():
    t = new_thread()
    for i in range(14):
        answer(t, "s1", 0.1 * (i + 1))
    assert not cost.thread_status(t, 1.4)["long"]
    answer(t, "s1", 1.5)
    assert cost.thread_status(t, 1.5) == {"total_usd": 1.5, "turns": 15, "unknown": False, "long": True}
    assert cost.thread_status(new_thread(), 3.0)["long"]


# ---- the chat in the app ------------------------------------------------------------------------


PORT = 40124
TOKEN = "cost-token"


@pytest.fixture()
def app_dir(tmp_path, tricky_repo):
    ctx = make_pr_dir(tmp_path, tricky_repo["diff"])
    write_json(tmp_path / "context.json", {**ctx, "repo_root": str(tmp_path), "worktree": str(tmp_path)})
    (tmp_path / "diff.patch").write_text(tricky_repo["diff"])
    return tmp_path


def client_for(pr_dir: Path, cfg: Config | None = None) -> TestClient:
    c = TestClient(server.Guard(server.create_app(pr_dir, cfg or Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    c.cookies.set(session_cookie(PORT), TOKEN)
    return c


def test_each_chat_answer_shows_its_own_cost(app_dir, monkeypatch):
    script = [
        [{"type": "text", "text": "Hi"}, {"type": "result", "session_id": "s1", "cost_usd": 0.79, "duration_ms": 5}],
        [{"type": "result", "session_id": "s1", "cost_usd": 1.25, "duration_ms": 5}],
        [{"type": "text", "text": "Half an answ"}, {"type": "error", "message": "stopped"}],
        [{"type": "result", "session_id": "s1", "cost_usd": 2.0, "duration_ms": 5}],
    ]

    async def fake_stream(prompt, **kw):  # the session's running total, as `claude -p` reports it
        for ev in script.pop(0):
            yield ev

    monkeypatch.setattr(server, "stream_chat", fake_stream)
    client = client_for(app_dir)
    h = {"origin": f"http://127.0.0.1:{PORT}"}
    tid = client.post("/api/chat/threads", json={}, headers=h).json()["id"]
    results = []
    for i in range(4):
        body = client.post(f"/api/chat/threads/{tid}/messages", json={"text": f"question {i}"}, headers=h).text
        events = [json.loads(chunk[6:]) for chunk in body.split("\n\n") if chunk.startswith("data: ")]
        results.append(next((e for e in events if e["type"] == "result"), None))

    assert [r and r["cost_usd"] for r in results] == [0.79, 0.46, None, 0.75]
    assert results[3]["includes_stopped"] and results[3]["session_total_usd"] == 2.0
    assert results[3]["thread"] == {"total_usd": 2.0, "turns": 4, "unknown": False, "long": False}
    saved = [m for m in client.get("/api/state").json()["threads"][0]["messages"] if m["role"] == "assistant"]
    assert [m["cost_usd"] for m in saved] == [0.79, 0.46, None, 0.75]
    assert saved[2]["cost_unknown"] and saved[3]["includes_stopped"] and not saved[1]["includes_stopped"]
    chat = [x for x in ledger.read(app_dir) if x["purpose"] == "chat"]
    assert [(x["cost_usd"], x["outcome"], x["target"]) for x in chat] == [
        (0.79, "ok", tid), (0.46, "ok", tid), (None, "error", tid), (0.75, "ok", tid)]
    s = client.get("/api/cost").json()
    assert s["breakdown"] == {"analysis": 0.0, "chat": 2.0, "other": 0.0} and s["total_usd"] == 2.0
    assert s["threads"][tid] == {"total_usd": 2.0, "turns": 4, "unknown": False, "long": False}
    assert (s["unknown"], s["running"], s["warn_usd"], s["over_budget"]) == (False, False, 0.0, False)


def test_keep_going_hides_the_long_thread_hint_for_that_thread_only(app_dir):
    client = client_for(app_dir)
    h = {"origin": f"http://127.0.0.1:{PORT}"}
    long_one = client.post("/api/chat/threads", json={}, headers=h).json()["id"]
    other = client.post("/api/chat/threads", json={}, headers=h).json()["id"]
    assert client.post(f"/api/chat/threads/{long_one}/long-hint/dismiss", headers=h).json() == {"ok": True}
    threads = {t["id"]: t for t in client.get("/api/state").json()["threads"]}
    assert threads[long_one]["long_hint_dismissed"] is True
    assert "long_hint_dismissed" not in threads[other]
    assert client.post("/api/chat/threads/nope/long-hint/dismiss", headers=h).status_code == 404
    assert client.post(f"/api/chat/threads/{other}/long-hint/dismiss").status_code == 403  # same-origin only


# ---- migration --------------------------------------------------------------------------------------


def v1_state() -> dict:
    """A thread saved before E7: each answer holds the session's running total."""
    def a(total, error=None, ts="2026-10-20T10:00:00+00:00"):
        return {"role": "assistant", "text": "", "context": [], "tools": [], "cost_usd": total, "error": error, "ts": ts}

    def q():
        return {"role": "user", "text": "q", "context": [], "tools": [], "cost_usd": None, "error": None, "ts": "t"}

    return {"reviewed": {}, "dismissed_suggestions": [], "drafts": [], "submitted": [], "threads": [
        {"id": "t1", "title": "x", "session_id": "s1", "started": True, "created_at": "t",
         "messages": [q(), a(0.79), q(), a(1.25), q(), a(None, "stopped"), q(), a(2.0)]}]}


def test_old_threads_are_converted_once_and_written_back(tmp_path):
    write_json(tmp_path / "state.json", v1_state())
    before = (tmp_path / "state.json").read_text()
    store = StateStore(tmp_path)

    loaded = store.load()                                   # load() converts in memory and never writes
    assert [m["cost_usd"] for m in loaded["threads"][0]["messages"] if m["role"] == "assistant"] == [0.79, 0.46, None, 0.75]
    assert (tmp_path / "state.json").read_text() == before and not (tmp_path / ledger.CALLS_FILE).exists()
    assert cost.summary(tmp_path)["breakdown"]["chat"] == pytest.approx(2.0)   # counted even before it's written

    store.update(lambda s: None)
    saved = read_json(tmp_path / "state.json")
    msgs = [m for m in saved["threads"][0]["messages"] if m["role"] == "assistant"]
    assert saved["version"] == STATE_VERSION
    assert [m["cost_usd"] for m in msgs] == [0.79, 0.46, None, 0.75] and msgs[3]["includes_stopped"]
    lines = ledger.read(tmp_path)
    assert len(lines) == 4 and all(x["migrated"] and x["purpose"] == "chat" for x in lines)

    store.update(lambda s: None)                            # once: the next update changes nothing
    assert read_json(tmp_path / "state.json") == saved and len(ledger.read(tmp_path)) == 4

    write_json(tmp_path / "state.json", v1_state())         # a crash after the ledger lines, before state.json
    store.update(lambda s: None)
    assert len(ledger.read(tmp_path)) == 4                  # nothing counted twice

    kept = (tmp_path / ledger.CALLS_FILE).read_text().splitlines(keepends=True)
    (tmp_path / ledger.CALLS_FILE).write_text("".join(kept[:2]))   # a crash halfway through the ledger lines
    write_json(tmp_path / "state.json", v1_state())
    store.update(lambda s: None)
    assert [x["seq"] for x in ledger.read(tmp_path)] == [x["seq"] for x in
                                                          (json.loads(k) for k in kept)]   # the rest, each once
    s = cost.summary(tmp_path)
    assert s["threads"]["t1"] == {"total_usd": 2.0, "turns": 4, "unknown": False, "long": False}


def test_the_server_upgrades_old_state_when_it_starts(app_dir):
    write_json(app_dir / "state.json", v1_state())
    client_for(app_dir)
    assert read_json(app_dir / "state.json")["version"] == STATE_VERSION and len(ledger.read(app_dir)) == 4


# ---- analysis runs ------------------------------------------------------------------------------------


def test_a_killed_runs_spend_is_kept(tmp_path):
    first = Progress(tmp_path, None, lambda m: None)
    first.ledger.record("annotate", model="sonnet", target="group 1", cost_usd=1.5, outcome="ok")
    first.update("Group 1 of 3 done")
    assert first.data["cost_usd"] == 1.5 and read_json(tmp_path / "progress.json")["cost_usd"] == 1.5
    dead = subprocess.Popen([sys.executable, "-c", "pass"])  # the run is killed: no "complete", its process is gone
    dead.wait()
    write_json(tmp_path / "analyze.pid", {"pid": dead.pid})
    assert not cost.summary(tmp_path)["running"]

    second = Progress(tmp_path, None, lambda m: None)       # the next run starts its own count
    second.update("Reading 3 files")
    assert second.data["cost_usd"] == 0.0
    second.ledger.record("annotate", model="sonnet", target="group 1", cost_usd=0.5, outcome="ok")
    second.update("Group 1 of 3 done")
    write_json(tmp_path / "analyze.pid", {"pid": os.getpid()})
    s = cost.summary(tmp_path)
    assert s["total_usd"] == 2.0 and s["breakdown"]["analysis"] == 2.0
    assert [(r["run_id"], r["cost_usd"], r["running"]) for r in s["runs"]] == [
        (first.ledger.run_id, 1.5, False), (second.ledger.run_id, 0.5, True)]
    assert s["running"] and cost.label(s) == "$2.00 so far"


class FakeClaude:
    """Stands in for `claude -p --output-format json`: each call reports a cost; nothing leaves the machine."""

    OUT = {"scores": {"scores": []}, "verdicts": {"verdicts": []}, "checks": {"checks": []}, "body": {"body": "Fine."},
           "cause": {"summary": "s", "cause": "c", "caused_by_pr": "no", "related": [], "fix": "f"},
           "flow_edges": {"files": [], "flow_edges": []}, "blockers": {"blockers": []},
           "chapters": {"summary": {"tldr": "t", "why": "w", "how": "h", "risk": "low", "risk_reasons": []},
                        "chapters": [], "flows": [], "impact": [], "questions": [], "test_files": [], "docs_files": []}}

    def __init__(self, monkeypatch):
        self.reported: list[float] = []
        self.next_cost = 0.05
        self.mode = "ok"
        import prr.claude
        monkeypatch.setattr(prr.claude, "subprocess", types.SimpleNamespace(
            run=self.run, TimeoutExpired=subprocess.TimeoutExpired))

    def run(self, argv, *, input, cwd, env, capture_output, text, timeout):
        if self.mode == "timeout":
            self.mode = "ok"
            raise subprocess.TimeoutExpired(argv, timeout)
        props = json.loads(argv[argv.index("--json-schema") + 1]).get("properties", {})
        out = next((v for k, v in self.OUT.items() if k in props), {})
        c = round(self.next_cost, 2)
        self.next_cost += 0.01
        self.reported.append(c)
        failed = self.mode == "error"
        self.mode = "ok"
        data = {"type": "result", "subtype": "error_max_turns" if failed else "success", "is_error": failed,
                "total_cost_usd": c, "duration_ms": 10, "structured_output": out}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(data), stderr="")


def test_an_analysis_from_before_the_ledger_still_counts(tmp_path):
    """progress.json from before calls.jsonl kept that run's (partial) spend: it counts, and the total reads "≥"."""
    write_json(tmp_path / "progress.json", {"status": "complete", "started_at": "2026-10-24T12:08:59+00:00",
                                            "cost_usd": 1.6712})
    s = cost.summary(tmp_path)
    assert s["total_usd"] == 1.6712 and s["unknown"] and cost.label(s) == "≥ $1.67"

    cost.keep_before_ledger(tmp_path)          # a new run starts: the old spend moves into calls.jsonl, once
    cost.keep_before_ledger(tmp_path)
    write_json(tmp_path / "progress.json", {"status": "annotating", "run_id": "r2", "cost_usd": 0.0})
    s = cost.summary(tmp_path)
    assert s["total_usd"] == 1.6712 and s["unknown"]
    assert [x["run_id"] for x in ledger.read(tmp_path)] == [cost.BEFORE_LEDGER]


def test_totals_match_the_ledger_across_two_runs_and_every_call_type(tmp_path, monkeypatch):
    fake = FakeClaude(monkeypatch)
    cfg = Config()
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src/a.py").write_text("x = 2\n")
    ctx = {"pr": {"head_sha": "h"}, "files": [{"path": "src/a.py", "status": "modified"}]}
    finding = {"id": "f1", "path": "src/a.py", "side": "RIGHT", "line": 1, "severity": "minor", "category": "c",
               "title": "t", "impact": "", "fix": "", "why": "", "evidence": ""}
    failing_ci = {"checks": [{"name": "lint", "kind": "check", "state": "fail", "app": None, "summary": "s",
                              "log_tail": "boom"}], "annotations": []}
    bot = [{"comment_id": 1, "bot": "tool[bot]", "path": "src/a.py", "line": 1, "side": "RIGHT", "body": "b"}]
    earlier = [{"id": "f-0", "kind": "finding", "title": "t", "path": "src/a.py", "side": "RIGHT", "line": 1,
                "anchor": "x = 1  # gone", "old_window": "", "author": None, "html_url": None}]
    schema = {"type": "object", "properties": {"files": {"type": "array"}}}

    def quiet(_m):
        return None

    r1 = Ledger(tmp_path, "r1")
    run_structured("p", schema, cwd=wt, model="sonnet", ledger=r1, purpose="annotate", target="group 1")
    run_structured("p", schema, cwd=wt, model="sonnet", ledger=r1, purpose="blockers", target="group 1")
    judge.run([finding], {}, cfg, wt, {}, quiet, ledger=r1)
    ci.explain_failures(failing_ci, {"files": [{"path": "src/a.py"}]}, cfg, wt, quiet, ledger=r1)
    bots.check(bot, cfg, wt, {}, quiet, ledger=r1)
    recheck.run(earlier, ctx, wt, cfg, {}, quiet, ledger=r1)
    run_structured("p", schema, cwd=wt, model="opus", ledger=r1, purpose="synthesis")

    r2 = Ledger(tmp_path, "r2")
    run_structured("p", schema, cwd=wt, model="sonnet", ledger=r2, purpose="annotate", target="group 1")
    fake.mode = "error"                                     # failed, but it still cost money
    with pytest.raises(ClaudeError):
        run_structured("p", schema, cwd=wt, model="sonnet", ledger=r2, purpose="annotate", target="group 2")
    fake.mode = "timeout"                                   # killed: its cost is unknown
    with pytest.raises(ClaudeError, match="timed out"):
        run_structured("p", schema, cwd=wt, model="sonnet", timeout=5, ledger=r2, purpose="annotate", target="group 3")

    other = Ledger(tmp_path)                                # the server's own calls
    run_structured("p", {"type": "object", "properties": {"body": {}}}, cwd=wt, model="opus", ledger=other,
                   purpose="summary", target="review summary")
    ci.explain_failures(json.loads(json.dumps(failing_ci)), {"files": []}, cfg, wt, quiet, ledger=other)
    write_json(tmp_path / "state.json", {"version": STATE_VERSION, "threads": [{"id": "t1", "messages": []}]})
    other.record("chat", **cost.chat_line("t1", "opus", {"cost_usd": 0.8, "includes_stopped": False},
                                          duration_ms=9, error=None))
    ledger.append(tmp_path, {"at": "t", "tool": "Bash", "input": "ls", "decision": "allow"})  # an approval: not a call

    lines = ledger.read(tmp_path)
    calls = [x for x in lines if x.get("purpose")]
    assert len(calls) == len(lines) - 1
    assert {x["purpose"] for x in calls} == {"annotate", "blockers", "judge", "ci", "bots", "recheck", "synthesis",
                                             "summary", "chat"}
    timeout = next(x for x in calls if x["outcome"] == "timeout")
    assert timeout["cost_usd"] is None and timeout["run_id"] == "r2" and "timed out" in timeout["error"]
    errored = next(x for x in calls if x["outcome"] == "error")
    assert errored["cost_usd"] is not None and errored["target"] == "group 2"

    s = cost.summary(tmp_path, warn_usd=1.0)
    reported = sum(fake.reported)
    assert s["total_usd"] == pytest.approx(reported + 0.8)
    assert s["total_usd"] == pytest.approx(sum(x["cost_usd"] or 0 for x in lines if x.get("purpose")))
    by_run = {r["run_id"]: r for r in s["runs"]}
    assert by_run["r1"]["calls"] == 7 and by_run["r2"]["calls"] == 3
    assert by_run["r1"]["cost_usd"] == pytest.approx(sum(x["cost_usd"] for x in calls if x["run_id"] == "r1"))
    assert s["breakdown"]["analysis"] == pytest.approx(by_run["r1"]["cost_usd"] + by_run["r2"]["cost_usd"])
    assert s["breakdown"]["other"] == pytest.approx(sum(x["cost_usd"] for x in calls
                                                        if x["run_id"] is None and x["purpose"] != "chat"))
    assert s["breakdown"]["chat"] == 0.8 and s["threads"]["t1"]["total_usd"] == 0.8
    assert s["unknown"] and by_run["r2"]["unknown"] and not by_run["r1"]["unknown"]
    assert s["over_budget"] and s["warn_usd"] == 1.0 and cost.label(s).startswith("≥ $")


def test_two_analysis_runs_record_every_call(tmp_path, tricky_repo, monkeypatch):
    from prr import analyze, github

    fake = FakeClaude(monkeypatch)
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    ctx = make_pr_dir(pr_dir, tricky_repo["diff"])
    ctx["pr"].update(merge_base_sha=tricky_repo["base"], head_sha=tricky_repo["head"])
    write_json(pr_dir / "context.json", {**ctx, "repo_root": str(tricky_repo["root"]), "worktree": str(tricky_repo["root"])})
    (pr_dir / "diff.patch").write_text(tricky_repo["diff"])
    monkeypatch.setattr(ci, "fetch", lambda *a: {"fetched_at": None, "head_sha": "h", "annotations": [], "checks": [
        {"name": "lint", "kind": "check", "state": "fail", "url": None, "app": None, "summary": "s", "log_tail": "x",
         "explanation": None}]})
    monkeypatch.setattr(github, "existing_comments", lambda *a: [
        {"id": 1, "user": "tool[bot]", "is_bot": True, "path": "src/core.py", "line": 5, "side": "RIGHT",
         "body": "**Possible bug in this line**", "html_url": "u"}])

    first = analyze.analyze(pr_dir, Config(), echo=lambda m: None)
    second = analyze.analyze(pr_dir, Config(), force=True, echo=lambda m: None)

    lines = ledger.read(pr_dir)
    assert {x["purpose"] for x in lines} >= {"annotate", "blockers", "ci", "bots", "synthesis"}
    assert len({x["run_id"] for x in lines}) == 2 and all(x["outcome"] == "ok" for x in lines)
    s = cost.summary(pr_dir)
    assert s["total_usd"] == pytest.approx(sum(fake.reported)) and s["breakdown"]["analysis"] == s["total_usd"]
    assert [r["cost_usd"] for r in s["runs"]] == [first["progress"]["cost_usd"], second["progress"]["cost_usd"]]
    assert read_json(pr_dir / "progress.json")["cost_usd"] == second["progress"]["cost_usd"] > 0
    assert not s["running"] and not s["unknown"]


def test_prr_status_shows_each_prs_spend(tmp_path, capsys):
    cfg = Config()
    cfg.cache_dir = tmp_path
    cfg.budget.warn_usd = 1.0
    d = tmp_path / "o__r" / "pr-7"
    d.mkdir(parents=True)
    write_json(d / "context.json", {"pr": {"owner": "o", "repo": "r", "number": 7, "title": "t", "head_sha": "abc"}})
    Ledger(d, "r1").record("annotate", model="sonnet", cost_usd=1.25)
    Ledger(d).record("summary", model="opus", cost_usd=None, outcome="timeout")
    cli.cmd_status(argparse.Namespace(pr=None, repo=None), cfg)
    row = json.loads(capsys.readouterr().out)[0]
    assert row["spend"] == "≥ $1.25 (over your limit)" and row["spend_usd"] == 1.25


def test_budget_setting_is_read_from_config(tmp_path, monkeypatch):
    from prr import config
    (tmp_path / "config.toml").write_text("[budget]\nwarn_usd = 12.5\n")
    monkeypatch.setattr(config, "REPO_DIR", tmp_path)
    assert config.load_config().budget.warn_usd == 12.5


# ---- concurrent writers --------------------------------------------------------------------------------


WRITER = """
import sys
from pathlib import Path
from prr import ledger
lg = ledger.Ledger(Path(sys.argv[1]), sys.argv[2])
for i in range(int(sys.argv[3])):
    lg.record("annotate", model="m", target=str(i), cost_usd=0.01, note="p" * 4000)
"""


def test_concurrent_appends_stay_whole_lines(tmp_path):
    per = 150
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(tmp_path), f"proc{n}", str(per)], cwd=ROOT)
             for n in range(3)]

    def write(name: str) -> None:
        lg = Ledger(tmp_path, name)
        for i in range(per):
            lg.record("judge", model="m", target=str(i), cost_usd=0.01, note="t" * 4000)

    threads = [threading.Thread(target=write, args=(f"thread{n}",)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(p.wait(timeout=60) == 0 for p in procs)

    raw = (tmp_path / ledger.CALLS_FILE).read_text().splitlines()
    parsed = [json.loads(line) for line in raw]             # every line is whole JSON
    assert len(parsed) == 9 * per
    assert {(x["run_id"], x["target"]) for x in parsed} == {(f"{k}{n}", str(i)) for k, count in (("proc", 3), ("thread", 6))
                                                            for n in range(count) for i in range(per)}
    assert cost.summary(tmp_path)["total_usd"] == pytest.approx(9 * per * 0.01)
