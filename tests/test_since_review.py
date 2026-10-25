"""E4, since your last review: the reviewed commit, the interdiff, new stops, your earlier comments' status, re-reading
the PR and the head poll. Temp git repos only; nothing reaches GitHub or Claude (fakes, or PRR_GITHUB_DRY_RUN=1)."""

import json
import os
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prr import analyze, bots, chat_tools, ci, cli, config, github, merge, recheck, server, since
from prr.config import Config
from prr.fetch import DIFF_ARGS
from prr.gitgh import PRRef
from prr.state import StateStore
from prr.util import read_json, write_json
from tests.conftest import _git
from tests.test_cost import FakeClaude
from tests.test_merge import make_pr_dir

PORT = 40125
TOKEN = "since-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


def commit(root, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def pr_at(pr_dir, root, base: str, head: str) -> dict:
    """A fetched PR folder for base...head of the repo at `root`."""
    diff = _git(root, *DIFF_ARGS, base, head)
    ctx = make_pr_dir(pr_dir, diff)
    ctx["pr"].update(head_sha=head, merge_base_sha=base)
    ctx = {**ctx, "repo_root": str(root), "worktree": str(root)}
    write_json(pr_dir / "context.json", ctx)
    (pr_dir / "diff.patch").write_text(diff)
    return ctx


@pytest.fixture()
def pushed(tmp_path, tricky_repo):
    """You reviewed the tricky repo's head; then the author pushed a fix on line 30 of src/core.py and put
    `with space.md` back as it was on the base, so that file is no longer part of the PR."""
    root = tricky_repo["root"]
    core = (root / "src/core.py").read_text().splitlines(keepends=True)
    core[29] = "line 28 fixed\n"
    (root / "src/core.py").write_text("".join(core))
    (root / "with space.md").write_text("hello\n")
    head = commit(root, "fix")
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    ctx = pr_at(pr_dir, root, tricky_repo["base"], head)
    return SimpleNamespace(root=root, base=tricky_repo["base"], reviewed=tricky_repo["head"], head=head, ctx=ctx,
                           pr_dir=pr_dir)


def reviewed(p, **extra) -> dict:
    return {"reviewed_head": p.reviewed, "reviewed_base": p.base, **extra}


CORE_FIX = {"path": "src/core.py", "hunks": [{"old_start": 27, "old_lines": 7, "new_start": 27, "new_lines": 7}]}


# ---- the interdiff -------------------------------------------------------------------------------------------------


def test_interdiff_covers_only_the_prs_current_files(pushed):
    out = since.changes(pushed.pr_dir, pushed.ctx, reviewed(pushed))
    assert out["ok"] and out["note"] is None and not out["full_pr"]
    assert (out["reviewed_head"], out["head"]) == (pushed.reviewed, pushed.head)
    assert out["files"] == [CORE_FIX]                       # `with space.md` changed too, but left the PR
    assert list(out["patch"]) == ["src/core.py"]
    assert "-line 28\n+line 28 fixed\n" in out["patch"]["src/core.py"]


def test_no_reviewed_head_or_the_same_head_has_no_changes(pushed):
    empty = {"files": [], "patch": {}, "note": None, "ok": True, "full_pr": False}
    assert since.changes(pushed.pr_dir, pushed.ctx, {}) == {"reviewed_head": None, "head": pushed.head, **empty}
    same = since.changes(pushed.pr_dir, pushed.ctx, {"reviewed_head": pushed.head})
    assert same == {"reviewed_head": pushed.head, "head": pushed.head, **empty}


def test_base_moved_note(tmp_path, tricky_repo):
    """The author merged main into the PR after your review: main's change to a PR file shows up, with a note."""
    root, base, reviewed_head = tricky_repo["root"], tricky_repo["base"], tricky_repo["head"]
    _git(root, "branch", "feature", reviewed_head)
    _git(root, "checkout", "-q", "-B", "upstream", base)
    (root / "src/core.py").write_text("line 1 from main\n" + "".join(f"line {i}\n" for i in range(2, 41)))
    new_base = commit(root, "main moves")
    _git(root, "checkout", "-q", "feature")
    _git(root, "merge", "-q", "--no-edit", "upstream")
    head = _git(root, "rev-parse", "HEAD").strip()
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    ctx = pr_at(pr_dir, root, new_base, head)

    out = since.changes(pr_dir, ctx, {"reviewed_head": reviewed_head, "reviewed_base": base})
    assert out["ok"] and out["note"] == "main moved; some changes may come from main"
    assert [f["path"] for f in out["files"]] == ["src/core.py"] and "+line 1 from main" in out["patch"]["src/core.py"]
    # An older state.json without reviewed_base: git tells that the new merge base isn't part of the reviewed commit.
    assert since.changes(pr_dir, ctx, {"reviewed_head": reviewed_head})["note"] == out["note"]
    # Same base: no note.
    ctx["pr"]["merge_base_sha"] = base
    assert since.changes(pr_dir, ctx, {"reviewed_head": reviewed_head})["note"] is None


def test_an_unreachable_reviewed_commit_falls_back_to_the_whole_pr(pushed, monkeypatch):
    tries = []
    monkeypatch.setattr(since, "fetch_commit", lambda root, pr, sha: tries.append(sha) or False)
    gone = "f" * 40
    tried: set[str] = set()

    out = since.changes(pushed.pr_dir, pushed.ctx, {"reviewed_head": gone, "reviewed_base": pushed.base}, tried)
    assert out["ok"] and out["full_pr"] and tries == [gone]
    assert out["note"] == ("The commit you reviewed (fffffff) can't be found, likely after a force-push, so this "
                           "shows the whole PR.")
    assert [f["path"] for f in out["files"]] == [f["path"] for f in pushed.ctx["files"]]
    core = next(f for f in out["files"] if f["path"] == "src/core.py")
    assert core["hunks"] == [{k: h[k] for k in ("old_start", "old_lines", "new_start", "new_lines")}
                             for h in next(f for f in pushed.ctx["files"] if f["path"] == "src/core.py")["hunks"]]
    assert out["patch"]["src/core.py"].startswith("diff --git a/src/core.py b/src/core.py")
    since.changes(pushed.pr_dir, pushed.ctx, {"reviewed_head": gone}, tried)
    assert tries == [gone]                                       # fetched once, not on every poll


def test_a_missing_reviewed_commit_is_fetched_by_sha(pushed, tmp_path, monkeypatch):
    """The commit isn't in your repo (say it was force-pushed away and git dropped it) but GitHub still has it."""
    other = tmp_path / "upstream"
    _git(tmp_path, "clone", "-q", str(pushed.root), str(other))
    _git(other, "checkout", "-q", "-b", "older", pushed.base)
    (other / "src/core.py").write_text("an older attempt\n")
    older = commit(other, "older attempt")
    monkeypatch.setattr(since, "find_remote", lambda root, ref: str(other))
    assert not since.have_commit(pushed.root, older)

    out = since.changes(pushed.pr_dir, pushed.ctx, {"reviewed_head": older, "reviewed_base": pushed.base})
    assert out["ok"] and not out["full_pr"] and since.have_commit(pushed.root, older)
    assert "-an older attempt" in out["patch"]["src/core.py"]


def test_any_other_git_failure_disables_the_view(pushed, monkeypatch):
    real = since.git

    def broken(*args, **kw):
        if "diff" in args:
            raise since.CommandError(["git", "diff"], 128, "", "fatal: unable to read tree 1234567\n")
        return real(*args, **kw)

    monkeypatch.setattr(since, "git", broken)
    out = since.changes(pushed.pr_dir, pushed.ctx, reviewed(pushed))
    assert not out["ok"] and out["files"] == [] and out["patch"] == {}
    assert out["note"] == "Couldn't compute changes since your review: fatal: unable to read tree 1234567"
    bad = since.changes(pushed.pr_dir, pushed.ctx, {"reviewed_head": "--output=/tmp/x"})   # never reaches git
    assert not bad["ok"] and bad["note"].endswith("the saved commit id is not valid")


# ---- new stops -----------------------------------------------------------------------------------------------------


FILES = [{"path": "src/a.py", "hunks": [{"old_start": 10, "old_lines": 7, "new_start": 10, "new_lines": 8}]}]


def test_a_stop_is_new_when_its_lines_meet_an_interdiff_hunk():
    assert since.is_new_stop({"id": "stop-1", "path": "src/a.py", "side": "RIGHT", "line": 12}, FILES)
    assert since.is_new_stop({"id": "stop-2", "path": "src/a.py", "start_line": 3, "line": 10}, FILES)   # touches 10
    assert since.is_new_stop({"id": "stop-3", "path": "src/a.py", "line": 17}, FILES)                    # last line
    assert not since.is_new_stop({"id": "stop-4", "path": "src/a.py", "line": 18}, FILES)
    assert not since.is_new_stop({"id": "stop-5", "path": "src/b.py", "line": 12}, FILES)                # other file
    assert not since.is_new_stop({"id": "stop-6", "path": "src/a.py", "side": "LEFT", "line": 12}, FILES)
    assert not since.is_new_stop({"id": "stop-7", "path": "src/a.py"}, FILES)                            # no line
    assert not since.is_new_stop({"id": "stop-8", "kind": "ci"}, FILES)                                  # no path
    assert not since.is_new_stop({"id": "stop-1", "path": "src/a.py", "line": 12}, [])                   # no changes


def test_the_new_stop_rule_ignores_ids():
    """Intersection only: a stop you saw before is new again when the new commits touch it, and a stop whose id is new
    (a re-run re-anchored it) isn't new when they don't."""
    seen_before = {"id": "stop-" + "a" * 40, "path": "src/a.py", "line": 12}
    re_anchored = {"id": "stop-" + "b" * 40, "path": "src/a.py", "line": 40}
    reviewed_stop_ids = [seen_before["id"]]
    assert seen_before["id"] in reviewed_stop_ids and since.is_new_stop(seen_before, FILES)
    assert re_anchored["id"] not in reviewed_stop_ids and not since.is_new_stop(re_anchored, FILES)


# ---- your earlier comments -----------------------------------------------------------------------------------------


def gh_comment(cid: int, line, *, user: str = "me", reply_to=None, original_line=None, start_line=None) -> dict:
    return {"id": cid, "path": "src/core.py", "line": line, "side": "RIGHT", "start_line": start_line, "user": user,
            "body": f"Comment {cid}", "created_at": "2026-10-20T09:00:00Z", "html_url": f"https://x/{cid}",
            "in_reply_to_id": reply_to, "is_bot": False, "commit_id": "c", "original_commit_id": "o",
            "original_line": original_line or line, "review_id": 900,
            "diff_hunk": "@@ -1,5 +1,5 @@\n line 1\n line 2\n line 3\n line 4\n-line 5\n+line 5 changed"}


OUTDATED, NEAR_FIX, UNTOUCHED = gh_comment(1, None, original_line=5), gh_comment(2, 31), gh_comment(3, 22)


def result(c: dict, status: str, how: str, **extra) -> dict:
    """A re-check result as prr/recheck.py saves it in recheck.json."""
    return {**recheck.comment_entry(c), "status": status, "how": how, **extra}


def test_comment_status_outdated_in_the_interdiff_and_untouched():
    results = {"c-1": {"status": "fixed", "how": "The value is checked first now (src/core.py:5)."},
               "c-2": {"status": "still-open", "how": "The fix is two lines up but doesn't cover this case."}}
    rows = since.comment_statuses([OUTDATED, NEAR_FIX, UNTOUCHED], [CORE_FIX], results, running=False)
    assert rows == [
        {"id": 1, "path": "src/core.py", "line": None, "status": "fixed",
         "note": "The value is checked first now (src/core.py:5)."},
        {"id": 2, "path": "src/core.py", "line": 31, "status": "still-open",
         "note": "The fix is two lines up but doesn't cover this case."},
        {"id": 3, "path": "src/core.py", "line": 22, "status": "line-unchanged", "note": None},
    ]
    running = since.comment_statuses([OUTDATED, NEAR_FIX, UNTOUCHED], [CORE_FIX], results, running=True)
    assert [r["status"] for r in running] == ["checking", "checking", "line-unchanged"]
    fresh = since.comment_statuses([OUTDATED, UNTOUCHED], [CORE_FIX], {}, running=False)   # nothing checked here yet
    assert [(r["status"], r["note"]) for r in fresh] == [("not-checked", since.NOT_YET), ("line-unchanged", None)]


def test_comments_over_recheck_max_are_not_checked():
    over = {"status": "still-open", "how": recheck.NOT_CHECKED, "not_checked": True}
    rows = since.comment_statuses([OUTDATED, NEAR_FIX, UNTOUCHED], [CORE_FIX], {"c-1": over, "c-2": over}, False)
    assert [(r["status"], r["note"]) for r in rows] == [
        ("not-checked", "2 not checked (limit; raise `recheck_max`)"),
        ("not-checked", "2 not checked (limit; raise `recheck_max`)"),
        ("line-unchanged", None),
    ]


def test_your_comments_are_the_thread_starters_you_wrote():
    alice = gh_comment(4, 30, user="alice")
    reply = gh_comment(5, 31, reply_to=2)
    bot = {**gh_comment(7, 12), "is_bot": True}
    in_a_review = {**gh_comment(8, 12), "review_id": 77}
    lone = {**gh_comment(9, 12), "review_id": 78}
    everything = [OUTDATED, alice, reply, bot, in_a_review, lone]
    assert [c["id"] for c in since.mine(everything, "ME", [])] == [1, 8, 9]
    # Your login couldn't be read: the comments PR Prism posted for you (a review's id, or a lone comment's own id).
    submitted = [{"review_id": 77}, {"review_id": 9}, {"review_id": 0}]
    assert [c["id"] for c in since.mine(everything, None, submitted)] == [8, 9]


def test_recheck_max_is_a_config_key(tmp_path, monkeypatch):
    assert Config().analysis.recheck_max == 20
    assert config.load_config().analysis.recheck_max == 20
    (tmp_path / "config.toml").write_text("[analysis]\nrecheck_max = 5\n")
    monkeypatch.setattr(config, "REPO_DIR", tmp_path)
    assert config.load_config().analysis.recheck_max == 5


def test_recheck_asks_about_comments_next_to_a_change_and_stops_at_recheck_max(tmp_path, monkeypatch):
    wt = tmp_path / "wt"
    (wt / "src").mkdir(parents=True)
    (wt / "src/a.py").write_text("".join(f"v{i} = {i}\n" for i in range(1, 41)))
    ctx = {"pr": {"head_sha": "h"}, "files": [{"path": "src/a.py", "status": "modified"}]}

    def item(cid: str, line: int, anchor: str, **extra) -> dict:
        return {"id": cid, "kind": "comment", "title": cid, "path": "src/a.py", "side": "RIGHT", "line": line,
                "anchor": anchor, "severity": None, "category": None, "old_window": "", "author": "me",
                "html_url": None, **extra}

    asked: list[str] = []

    def fake(prompt, schema, **kw):
        asked.append(prompt)
        return {"checks": [{"id": "c-1", "fixed": True, "confidence": 90, "reason": "A guard was added on line 9."},
                           {"id": "c-2", "fixed": False, "confidence": 70, "reason": "Unclear."}]}, {}

    monkeypatch.setattr(recheck, "run_structured", fake)
    cfg = Config()
    cfg.analysis.recheck_max = 2
    items = [item("c-1", 10, "v10 = 10", changed_nearby=True),   # its line is still there: asked anyway
             item("c-2", 20, "a line that is gone"), item("c-3", 30, "another line that is gone")]
    cache: dict = {}
    out = recheck.run(items, ctx, wt, cfg, cache, lambda m: None)
    assert len(asked) == 1 and "### c-1" in asked[0] and "### c-2" in asked[0] and "### c-3" not in asked[0]
    assert (out["c-1"]["status"], out["c-1"]["how"]) == ("fixed", "A guard was added on line 9.")
    assert out["c-2"]["status"] == "still-open" and not out["c-2"].get("not_checked")
    assert out["c-3"]["status"] == "still-open" and out["c-3"]["not_checked"] and out["c-3"]["how"] == recheck.NOT_CHECKED
    assert {v["id"] for k, v in cache.items() if not k.startswith("_")} == {"c-1", "c-2"}   # c-3 is asked next time
    # Without the flag, a line that is still there is "still open" with no question asked.
    again = recheck.run([item("c-9", 10, "v10 = 10")], ctx, wt, cfg, {}, lambda m: None)
    assert again["c-9"]["status"] == "still-open" and len(asked) == 1


def test_the_analysis_rechecks_your_comments_first_with_those_next_to_a_change(pushed, monkeypatch):
    FakeClaude(monkeypatch)
    StateStore(pushed.pr_dir).update(lambda s: s.update(reviewed(pushed)))
    write_json(pushed.pr_dir / "history.json", {"head_sha": pushed.reviewed, "items": [
        {"id": "f-0", "kind": "finding", "title": "t", "path": "src/core.py", "side": "RIGHT", "line": 5,
         "anchor": "x", "old_window": "", "severity": "minor", "category": "c", "author": None, "html_url": None}]})
    alice = gh_comment(4, None, user="alice", original_line=12)
    monkeypatch.setattr(github, "existing_comments", lambda *a: [alice, OUTDATED, NEAR_FIX, UNTOUCHED])
    monkeypatch.setattr(since, "viewer_login", lambda: "me")
    monkeypatch.setattr(ci, "fetch", lambda *a, **k: None)          # no CI here (the analysis goes on without it)
    monkeypatch.setattr(bots, "check", lambda *a, **k: {})
    seen: list[list[dict]] = []
    fixed = result(OUTDATED, "fixed", "Fixed.")
    monkeypatch.setattr(recheck, "run", lambda items, *a, **k: seen.append(items) or {"c-1": fixed})

    analyze._analyze(pushed.pr_dir, pushed.ctx, Config(), None, True, lambda m: None)
    [items] = seen
    assert [it["id"] for it in items] == ["c-1", "c-2", "f-0", "c-4"]   # yours first, then as before
    near = items[1]
    assert near["changed_nearby"] and near["line"] == 31 and near["kind"] == "comment"
    saved = read_json(pushed.pr_dir / "recheck.json")
    assert saved["head_sha"] == pushed.head and saved["results"] == {"c-1": fixed}


# ---- the app: GET /api/since-review, GET /api/head, POST /api/reanalyze --------------------------------------------


@pytest.fixture()
def app(pushed, monkeypatch):
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(server, "viewer_login", lambda: "me")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: pushed.head)
    client = TestClient(server.Guard(server.create_app(pushed.pr_dir, Config()), TOKEN, PORT),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    return SimpleNamespace(client=client, p=pushed)


def test_since_review_endpoint(app, monkeypatch):
    p = app.p
    alice = gh_comment(4, 30, user="alice")
    monkeypatch.setattr(github, "existing_comments", lambda *a: [OUTDATED, NEAR_FIX, UNTOUCHED, alice])
    real_build = merge.build_analysis
    monkeypatch.setattr(merge, "build_analysis", lambda d: {**real_build(d), "stops": [
        {"id": "stop-near", "path": "src/core.py", "side": "RIGHT", "line": 29},
        {"id": "stop-far", "path": "src/core.py", "side": "RIGHT", "line": 5}, "not a stop"]})
    assert app.client.get("/api/since-review").json() == {
        "reviewed_head": None, "head": p.head, "files": [], "patch": {}, "note": None, "ok": True, "full_pr": False,
        "comments": [], "comments_error": None, "new_stop_ids": []}

    StateStore(p.pr_dir).update(lambda s: s.update(reviewed(p)))
    write_json(p.pr_dir / "recheck.json", {"head_sha": p.head, "results": {
        "c-1": result(OUTDATED, "fixed", "Fixed on line 5."), "c-2": result(NEAR_FIX, "still-open", "Not yet.")}})
    out = app.client.get("/api/since-review").json()
    assert out["ok"] and out["files"] == [CORE_FIX] and list(out["patch"]) == ["src/core.py"]
    assert out["new_stop_ids"] == ["stop-near"]
    assert [(c["id"], c["status"]) for c in out["comments"]] == [(1, "fixed"), (2, "still-open"), (3, "line-unchanged")]

    write_json(p.pr_dir / "analyze.pid", {"pid": os.getpid()})   # a re-read is running
    out = app.client.get("/api/since-review").json()
    assert [c["status"] for c in out["comments"]] == ["checking", "checking", "line-unchanged"]


def test_since_review_without_github_still_shows_the_changes(app, monkeypatch):
    StateStore(app.p.pr_dir).update(lambda s: s.update(reviewed(app.p)))

    def offline(*a):
        raise RuntimeError("network down")

    monkeypatch.setattr(github, "existing_comments", offline)
    out = app.client.get("/api/since-review").json()
    assert out["ok"] and out["files"] == [CORE_FIX]
    assert out["comments"] == [] and out["comments_error"] == "could not load existing comments: network down"


class FakeProc:
    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


def ledger_line(run_id: str, purpose: str, at: str, usd: float, ms: int = 60_000, outcome: str = "ok") -> dict:
    return {"at": at, "run_id": run_id, "purpose": purpose, "model": "sonnet", "target": "", "duration_ms": ms,
            "cost_usd": usd, "outcome": outcome, "error": None}


def write_ledger(pr_dir, *lines: dict) -> None:
    (pr_dir / "calls.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_estimate_comes_from_the_last_complete_run(tmp_path):
    assert since.estimate(tmp_path) is None
    write_ledger(tmp_path,
                 ledger_line("r1", "annotate", "2026-10-24T10:01:00+00:00", 1.0),
                 ledger_line("r1", "synthesis", "2026-10-24T10:06:00+00:00", 1.5),
                 ledger_line("r2", "annotate", "2026-10-24T11:01:00+00:00", 0.4),      # stopped before its summary
                 ledger_line(None, "chat", "2026-10-24T11:02:00+00:00", 3.0),          # not an analysis
                 {"at": "x", "tool": "Bash", "decision": "allow"})                     # not a call
    assert since.estimate(tmp_path) == {"minutes": 6, "usd": 2.5}
    write_json(tmp_path / "progress.json", {"run_id": "r1", "status": "failed"})      # the latest run says so
    assert since.estimate(tmp_path) is None
    write_json(tmp_path / "progress.json", {"run_id": "r2", "status": "complete",
                                            "started_at": "2026-10-24T11:00:00+00:00",
                                            "finished_at": "2026-10-24T11:12:20+00:00"})
    assert since.estimate(tmp_path) == {"minutes": 12, "usd": 0.4}


def test_reanalyze_starts_one_run_and_says_what_it_will_cost(app, monkeypatch):
    p = app.p
    started: list[tuple] = []

    def start(pr_dir, ctx):                                      # stands in for the background `prr _reanalyze`
        started.append((pr_dir, ctx["pr"]["number"], FakeProc()))
        return started[-1][2]

    monkeypatch.setattr(since, "start_reread", start)
    write_ledger(p.pr_dir, ledger_line("r1", "annotate", "2026-10-24T10:01:00+00:00", 1.25),
                 ledger_line("r1", "synthesis", "2026-10-24T10:06:00+00:00", 0.75))

    r = app.client.post("/api/reanalyze", headers=ORIGIN)
    assert r.status_code == 200 and r.json() == {"started": True, "estimate": {"minutes": 6, "usd": 2.0}}
    assert [s[:2] for s in started] == [(p.pr_dir, 1)]
    again = app.client.post("/api/reanalyze", headers=ORIGIN)
    assert again.status_code == 409 and len(started) == 1
    assert again.json()["detail"] == "The PR is already being read. Wait for it to finish."
    assert app.client.get("/api/head").json()["running"]

    started[0][2].code = 0                                       # that run finished
    (p.pr_dir / "calls.jsonl").unlink()
    assert not app.client.get("/api/head").json()["running"]
    r = app.client.post("/api/reanalyze", headers=ORIGIN)
    assert r.status_code == 200 and r.json() == {"started": True, "estimate": None} and len(started) == 2


def test_reanalyze_refuses_while_another_analysis_runs(app, monkeypatch):
    monkeypatch.setattr(since, "start_reread", lambda d, c: pytest.fail("must not start"))
    write_json(app.p.pr_dir / "analyze.pid", {"pid": os.getpid()})       # e.g. `prr analyze` from the terminal
    r = app.client.post("/api/reanalyze", headers=ORIGIN)
    assert r.status_code == 409 and r.json()["detail"] == "The PR is already being read. Wait for it to finish."


def test_head_poll_compares_githubs_head_with_the_analyzed_one(app, monkeypatch):
    p = app.p
    calls: list[str] = []
    github_head = {"sha": p.head}

    def current_head(owner, repo, number):
        calls.append(number)
        return github_head["sha"]

    monkeypatch.setattr(github, "current_head", current_head)
    clock = {"now": 1_800_000_000.0}
    monkeypatch.setattr(server, "time", SimpleNamespace(time=lambda: clock["now"], monotonic=lambda: 0.0))

    first = app.client.get("/api/head").json()
    assert first == {"analyzed_head": p.head, "current_head": p.head, "head_changed": False, "running": False,
                     "estimate": None, "error": None}
    github_head["sha"] = "a" * 40                                # the author pushes
    clock["now"] += 30
    assert app.client.get("/api/head").json()["head_changed"] is False and len(calls) == 1   # cached for a minute
    clock["now"] += 31
    later = app.client.get("/api/head").json()
    assert later["current_head"] == "a" * 40 and later["head_changed"] and len(calls) == 2

    def offline(owner, repo, number):
        raise RuntimeError("network down")

    monkeypatch.setattr(github, "current_head", offline)
    clock["now"] += 61
    out = app.client.get("/api/head").json()
    assert out["current_head"] is None and not out["head_changed"]
    assert out["error"] == "could not reach GitHub: network down"


# ---- recording the reviewed commit ---------------------------------------------------------------------------------


@pytest.fixture()
def posting(tmp_path, tricky_repo, monkeypatch):
    """A PR at the tricky repo's head, ready to post (dry run); its analysis has two guided-review stops."""
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(server, "viewer_login", lambda: "me")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: tricky_repo["head"])
    pr_dir = tmp_path / "pr"
    pr_dir.mkdir()
    ctx = pr_at(pr_dir, tricky_repo["root"], tricky_repo["base"], tricky_repo["head"])
    real_build = merge.build_analysis
    monkeypatch.setattr(merge, "build_analysis",
                        lambda d: {**real_build(d), "stops": [{"id": "stop-a"}, {"id": "stop-b"}]})
    client = TestClient(server.Guard(server.create_app(pr_dir, Config()), TOKEN, PORT),
                        base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    return SimpleNamespace(client=client, pr_dir=pr_dir, ctx=ctx, root=tricky_repo["root"], head=tricky_repo["head"],
                           base=tricky_repo["base"])


def ref(p) -> str:
    return _git(p.root, "for-each-ref", "--format=%(objectname)", since.ref_name(1)).strip()


def test_app_submit_records_the_reviewed_commit(posting):
    r = posting.client.post("/api/drafts", headers=ORIGIN,
                            json={"path": "src/core.py", "side": "RIGHT", "line": 22, "body": "Check this"})
    assert r.status_code == 200
    r = posting.client.post("/api/review/submit", json={"body": "Notes", "event": "COMMENT"}, headers=ORIGIN)
    assert r.status_code == 200, r.text
    s = StateStore(posting.pr_dir).load()
    assert (s["reviewed_head"], s["reviewed_base"], s["reviewed_stop_ids"]) == (posting.head, posting.base,
                                                                               ["stop-a", "stop-b"])
    assert ref(posting) == posting.head                          # kept in your repo, so git never drops it
    assert set(s["submitted"][0]) == {"review_id", "html_url", "event", "comments", "at"}


def test_chat_post_review_records_the_reviewed_commit_but_post_comment_does_not(posting):
    text, err = chat_tools.call_tool(posting.pr_dir, Config(), "post_comment",
                                     {"path": "src/core.py", "line": 22, "body": "One comment"})
    assert not err, text
    s = StateStore(posting.pr_dir).load()
    assert len(s["submitted"]) == 1 and "reviewed_head" not in s and "reviewed_stop_ids" not in s
    assert ref(posting) == ""

    text, err = chat_tools.call_tool(posting.pr_dir, Config(), "post_review", {"body": "Done.", "include_pending": True})
    assert not err, text
    s = StateStore(posting.pr_dir).load()
    assert (s["reviewed_head"], s["reviewed_stop_ids"]) == (posting.head, ["stop-a", "stop-b"])
    assert ref(posting) == posting.head


def test_a_review_still_counts_when_the_ref_cant_be_kept(posting, monkeypatch, capsys):
    def fail(root, pr, sha):
        raise since.CommandError(["git", "update-ref"], 1, "", "locked")

    monkeypatch.setattr(since, "keep_commit", fail)
    text, err = chat_tools.call_tool(posting.pr_dir, Config(), "post_review", {"body": "Done."})
    assert not err, text
    assert StateStore(posting.pr_dir).load()["reviewed_head"] == posting.head
    assert "could not keep the reviewed commit" in capsys.readouterr().err


# ---- prr _reanalyze ------------------------------------------------------------------------------------------------


def test_reread_fetches_then_analyzes_with_the_last_focus(tmp_path, monkeypatch):
    import prr.analyze
    import prr.fetch

    d = tmp_path / "pr"
    d.mkdir()
    write_json(d / "progress.json", {"status": "complete", "focus": "the retry logic"})
    ref_ = PRRef("o", "r", 7)
    monkeypatch.setattr(cli, "_resolve", lambda args, cfg: (ref_, tmp_path, d))
    monkeypatch.setattr(cli, "_print_summary", lambda a, d: None)
    steps: list = []

    def fake_fetch(ref, root, cfg):
        steps.append(("fetch", ref.number, read_json(d / "analyze.pid")["pid"]))   # held from the start
        return d

    def fake_analyze(pr_dir, cfg, focus=None, force=False, echo=print):
        steps.append(("analyze", focus))
        return {"status": "complete"}

    monkeypatch.setattr(prr.fetch, "fetch", fake_fetch)
    monkeypatch.setattr(prr.analyze, "analyze", fake_analyze)
    with pytest.raises(SystemExit) as done:
        cli.main(["_reanalyze", "https://github.com/o/r/pull/7"])
    assert done.value.code == 0
    assert steps == [("fetch", 7, os.getpid()), ("analyze", "the retry logic")]
    assert not (d / "analyze.pid").exists()

    def broken_fetch(ref, root, cfg):
        raise since.CommandError(["git", "fetch"], 128, "", "fatal: could not read from remote")

    monkeypatch.setattr(prr.fetch, "fetch", broken_fetch)
    with pytest.raises(SystemExit) as done:
        cli.main(["_reanalyze", "https://github.com/o/r/pull/7"])
    assert done.value.code == 1 and not (d / "analyze.pid").exists()     # a failed fetch frees the PR again


def test_reread_refuses_while_another_analysis_runs(tmp_path, monkeypatch, capsys):
    import prr.fetch

    d = tmp_path / "pr"
    d.mkdir()
    ppid = os.getppid()                                          # alive, and not this process
    write_json(d / "analyze.pid", {"pid": ppid})
    monkeypatch.setattr(cli, "_resolve", lambda args, cfg: (PRRef("o", "r", 7), tmp_path, d))
    monkeypatch.setattr(prr.fetch, "fetch", lambda *a: pytest.fail("must not fetch"))
    with pytest.raises(SystemExit) as done:
        cli.main(["_reanalyze", "7"])
    assert done.value.code == 1 and "already running" in capsys.readouterr().err
    assert read_json(d / "analyze.pid") == {"pid": ppid}
