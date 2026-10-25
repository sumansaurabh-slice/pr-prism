"""`prr clean`: what it removes, what it keeps, when it refuses, and how the next fetch brings the work back.
Uses a temp cache and temp git repos only."""

import errno
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from prr import archive, cli, fetch, gitgh
from prr.config import SKILL_DIR, Config
from prr.state import STATE_VERSION, StateStore, empty_state
from prr.util import read_json, write_json
from tests.conftest import _git


class _Terminal:
    """Stands in for stdin when a test runs `prr` as a person at a terminal."""

    def isatty(self) -> bool:
        return True


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # nothing here may reach the real ~/.cache
    cache = tmp_path / "cache"
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    monkeypatch.setattr(cli, "load_config", lambda: Config(cache_dir=cache))
    monkeypatch.setattr(gitgh, "default_repo", lambda cwd: ("o", "r"))
    monkeypatch.setattr(sys, "stdin", _Terminal())
    return {"cache": cache, "repo": repo, "tmp": tmp_path}


def make_pr(env: dict, number: int, owner: str = "o", repo: str = "r") -> Path:
    """A fetched PR folder: a git worktree at the repo's head, the PR's refs, and a context.json."""
    d = env["cache"] / f"{owner}__{repo}" / f"pr-{number}"
    d.mkdir(parents=True)
    head = _git(env["repo"], "rev-parse", "HEAD").strip()
    _git(env["repo"], "worktree", "add", "-q", "--detach", str(d / "worktree"), head)
    for name in ("head", "base"):
        _git(env["repo"], "update-ref", f"refs/pr-prism/{number}/{name}", head)
    write_json(d / "context.json", {"pr": {"owner": owner, "repo": repo, "number": number, "title": f"PR {number}",
                                           "head_sha": head}, "files": []})
    return d


def run_clean(env: dict, capsys, *args: str) -> tuple[int, str]:
    with pytest.raises(SystemExit) as exc:
        cli.main(["--repo", str(env["repo"]), "clean", *args])
    return exc.value.code, capsys.readouterr().out


def worktrees(env: dict) -> str:
    return _git(env["repo"], "worktree", "list", "--porcelain")


def refs(env: dict, number: int) -> list[str]:
    return _git(env["repo"], "for-each-ref", "--format=%(refname)", f"refs/pr-prism/{number}/").split()


# ---- today's behavior, pinned before the safe-clean change -------------------------------


def test_clean_removes_the_worktree_and_the_prs_refs(env, capsys):
    d = make_pr(env, 5)
    assert str(d / "worktree") in worktrees(env)
    code, _ = run_clean(env, capsys, "5")
    assert code == 0
    assert str(d / "worktree") not in worktrees(env)
    assert refs(env, 5) == []


def test_clean_removes_the_pr_folder_and_says_so(env, capsys):
    d = make_pr(env, 5)
    code, out = run_clean(env, capsys, "5")
    assert code == 0
    assert not d.exists()
    assert f"removed {d}" in out


def test_clean_all_touches_only_this_repos_prs(env, capsys):
    mine = [make_pr(env, 1), make_pr(env, 2)]
    other = env["cache"] / "other__x" / "pr-1"
    other.mkdir(parents=True)
    (other / "state.json").write_text("{}")
    code, _ = run_clean(env, capsys, "--all")
    assert code == 0
    assert not any(d.exists() for d in mine)
    assert (other / "state.json").exists()


def test_clean_skips_a_missing_folder(env, capsys):
    kept = make_pr(env, 5)
    code, out = run_clean(env, capsys, "9")
    assert code == 0
    assert "removed" not in out
    assert kept.exists() and str(kept / "worktree") in worktrees(env)


# ---- safe clean: the reviewer's work is archived first (E8) --------------------------------


class _NoTerminal:
    """stdin of an agent or a script."""

    def isatty(self) -> bool:
        return False


def work_state(head: str) -> dict:
    """A state.json with some of everything the reviewer makes (current version, so loading it changes nothing)."""
    return {
        "version": STATE_VERSION,
        "reviewed": {"src/a.py": "blob1"},
        "dismissed_suggestions": ["f-1"],
        "drafts": [
            {"id": "same", "path": "a.py", "side": "RIGHT", "line": 1, "start_line": None, "start_side": None,
             "body": "made on this head", "source": "user", "suggestion_id": None, "anchored": True,
             "outdated": False, "commit_id": head, "created_at": "2026-10-23T10:00:00+00:00"},
            {"id": "older", "path": "a.py", "side": "RIGHT", "line": 7, "start_line": 5, "start_side": None,
             "body": "made on an older head", "source": "chat", "suggestion_id": None, "anchored": True,
             "outdated": False, "commit_id": "0" * 40, "created_at": "2026-10-22T10:00:00+00:00"},
        ],
        "threads": [{"id": "t1", "title": "Why?", "session_id": "s1", "started": True,
                     "messages": [{"role": "user", "text": "why?"}], "created_at": "2026-10-22T10:00:00+00:00"}],
        "submitted": [{"review_id": 1, "html_url": "u", "event": "COMMENT", "comments": 2, "at": "x"}],
    }


def saved(env: dict, number: int, owner: str = "o", repo: str = "r") -> list[Path]:
    d = env["cache"] / "archive" / f"{owner}__{repo}"
    return sorted(p for p in d.glob(f"pr-{number}-*") if p.is_file()) if d.exists() else []


def test_clean_archives_the_state_before_removing(env, capsys):
    d = make_pr(env, 5)
    write_json(d / "state.json", work_state("h"))
    raw = (d / "state.json").read_bytes()
    code, out = run_clean(env, capsys, "5")
    assert code == 0 and not d.exists()
    [archived] = saved(env, 5)
    assert re.fullmatch(r"pr-5-\d{8}T\d{6}Z\.json", archived.name)
    assert archived.read_bytes() == raw
    assert archived.stat().st_mode & 0o777 == 0o600
    assert "kept 1 chat, 2 pending comments, 1 viewed mark in" in out and str(archived) in out


def test_viewed_marks_alone_are_archived(env, capsys):
    d = make_pr(env, 5)
    write_json(d / "state.json", {**empty_state(), "reviewed": {"a.py": "blob"}})
    _, out = run_clean(env, capsys, "5")
    assert len(saved(env, 5)) == 1 and "kept 1 viewed mark" in out


def test_an_empty_state_archives_nothing(env, capsys):
    write_json(make_pr(env, 5) / "state.json", empty_state())
    make_pr(env, 6)  # no state.json at all
    code, out = run_clean(env, capsys, "--all")
    assert code == 0 and saved(env, 5) == [] and saved(env, 6) == []
    assert "kept" not in out


def test_the_cost_ledger_is_archived_next_to_the_state(env, capsys):
    d = make_pr(env, 5)
    write_json(d / "state.json", empty_state())
    ledger = b'{"purpose": "annotate", "cost_usd": 0.4}\n{"purpose": "chat", "cost_usd": 0.1}\n'
    (d / "calls.jsonl").write_bytes(ledger)
    run_clean(env, capsys, "5")
    calls, state = saved(env, 5)
    assert state.name.endswith(".json") and calls.name == state.name.removesuffix(".json") + ".calls.jsonl"
    assert calls.read_bytes() == ledger
    assert calls.stat().st_mode & 0o777 == 0o600


def test_a_failed_archive_leaves_the_pr_alone(env, capsys, monkeypatch):
    d = make_pr(env, 5)
    write_json(d / "state.json", work_state("h"))

    def full_disk(path, data):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(archive, "_write_private", full_disk)
    code, out = run_clean(env, capsys, "5")
    assert code == 1
    assert "Not cleaned #5: couldn't archive state (No space left on device)" in out
    assert (d / "state.json").exists() and str(d / "worktree") in worktrees(env) and refs(env, 5)
    assert "removed" not in out


def test_clean_keeps_the_reviewed_ref(env, capsys):
    make_pr(env, 5)
    head = _git(env["repo"], "rev-parse", "HEAD").strip()
    _git(env["repo"], "update-ref", "refs/pr-prism/5/reviewed", head)
    _, out = run_clean(env, capsys, "5")
    assert refs(env, 5) == ["refs/pr-prism/5/reviewed"]
    assert "kept refs/pr-prism/5/reviewed" in out


def test_purge_deletes_the_folder_the_refs_and_the_archives(env, capsys):
    for n in (5, 6):
        write_json(make_pr(env, n) / "state.json", work_state("h"))
    run_clean(env, capsys, "5")  # an older archive of #5
    d = make_pr(env, 5)
    write_json(d / "state.json", work_state("h"))
    (d / "calls.jsonl").write_text('{"cost_usd": 1}\n')
    head = _git(env["repo"], "rev-parse", "HEAD").strip()
    _git(env["repo"], "update-ref", "refs/pr-prism/5/reviewed", head)
    assert len(saved(env, 5)) == 1
    code, out = run_clean(env, capsys, "5", "--purge")
    assert code == 0 and not d.exists()
    assert refs(env, 5) == [] and saved(env, 5) == []
    assert str(d / "worktree") not in worktrees(env)
    assert "kept" not in out
    assert (env["cache"] / "o__r" / "pr-6").exists() and refs(env, 6)  # other PRs untouched


def test_purge_of_an_already_cleaned_pr_removes_its_saved_work(env, capsys):
    write_json(make_pr(env, 5) / "state.json", work_state("h"))
    run_clean(env, capsys, "5")
    [archived] = saved(env, 5)
    code, out = run_clean(env, capsys, "5", "--purge")
    assert code == 0 and saved(env, 5) == [] and f"removed {archived}" in out


# ---- live reviews and agents (D18) ---------------------------------------------------------


@pytest.fixture()
def live_server():
    """Fakes a running review page: a process that looks like `prr _serve`, recorded in server.pid."""
    procs: list[subprocess.Popen] = []

    def start(d: Path) -> subprocess.Popen:
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "prr", "_serve", str(d)])
        threading.Thread(target=p.wait, daemon=True).start()  # reap it at once, so it doesn't linger as a zombie
        write_json(d / "server.pid", {"pid": p.pid, "child_pid": None, "port": 1, "started_at": "x"})
        procs.append(p)
        return p

    yield start
    for p in procs:
        if p.poll() is None:
            p.kill()


def test_a_live_pr_is_skipped_and_force_cleans_it(env, capsys, live_server):
    d = make_pr(env, 5)
    write_json(d / "state.json", work_state("h"))
    server = live_server(d)
    code, out = run_clean(env, capsys, "5")
    assert code == 0
    assert "Skipped #5: its review page is running (use --force)" in out
    assert d.exists() and server.poll() is None and saved(env, 5) == []
    code, out = run_clean(env, capsys, "5", "--force")
    assert code == 0 and not d.exists() and f"removed {d}" in out
    assert server.wait(timeout=10) is not None
    assert len(saved(env, 5)) == 1


def test_all_without_a_terminal_refuses_unless_yes(env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NoTerminal())
    dirs = [make_pr(env, 1), make_pr(env, 2)]
    code, out = run_clean(env, capsys, "--all")
    assert code == 1 and all(d.exists() for d in dirs)
    assert "Not cleaned" in out and "#1 PR 1" in out and "#2 PR 2" in out and "prr clean <PR>" in out
    assert "--yes" not in out and "--force" not in out
    code, out = run_clean(env, capsys, "--all", "--yes")
    assert code == 0 and not any(d.exists() for d in dirs)


def test_one_pr_without_a_terminal_needs_no_yes(env, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", _NoTerminal())
    d = make_pr(env, 5)
    code, _ = run_clean(env, capsys, "5")
    assert code == 0 and not d.exists()


def test_skill_md_suggests_only_cleaning_one_pr():
    text = (SKILL_DIR / "SKILL.md").read_text()
    assert "--all" not in text and "--purge" not in text and "--yes" not in text and "--force" not in text
    assert "clean <pr>" in text.split("argument-hint:", 1)[1].splitlines()[0]
    assert "`/pr-prism clean <PR>`" in text


# ---- restore on the next fetch -------------------------------------------------------------


def test_restore_brings_back_every_field(env, tmp_path):
    cfg = Config(cache_dir=env["cache"])
    old = tmp_path / "old"
    state = {**work_state("head1"), "stop_verdicts": {"stop-1": "real"}, "reviewed_stop_ids": ["stop-1"],
             "seen_hunks": {"a.py": ["h1"]}, "review_body_draft": "Looks good overall.", "analysis_cost_total": 1.5,
             "server_cost_usd": 0.2, "some_later_key": {"kept": True}}
    write_json(old / "state.json", state)
    (old / "calls.jsonl").write_text('{"cost_usd": 1.5}\n')
    kept = archive.save(cfg, "o", "r", 5, old)
    assert kept["counts"] == {"chats": 1, "pending_comments": 2, "need_replacing": 0, "viewed_marks": 1}

    fresh = tmp_path / "fresh"
    summary = archive.restore(cfg, "o", "r", 5, fresh, "head1")
    back = StateStore(fresh).load()
    for key, value in state.items():
        if key != "drafts":
            assert back[key] == value, key
    assert (fresh / "calls.jsonl").read_text() == '{"cost_usd": 1.5}\n'
    assert summary == back["restored"] and summary["archive"] == kept["path"].name
    assert summary["archived_at"] == archive.archived_at(kept["path"])
    assert {k: summary[k] for k in ("chats", "pending_comments", "need_replacing", "viewed_marks")} == {
        "chats": 1, "pending_comments": 2, "need_replacing": 1, "viewed_marks": 1}
    assert archive.message(summary).endswith(": 1 chat, 2 pending comments (1 needs re-placing), 1 viewed mark.")


def test_same_head_drafts_come_back_as_they_were_and_others_outdated_on_the_same_line(env, tmp_path):
    cfg = Config(cache_dir=env["cache"])
    old = tmp_path / "old"
    state = work_state("head1")
    write_json(old / "state.json", state)
    archive.save(cfg, "o", "r", 5, old)
    fresh = tmp_path / "fresh"
    archive.restore(cfg, "o", "r", 5, fresh, "head1")
    same, older = StateStore(fresh).load()["drafts"]
    assert same == state["drafts"][0]
    assert older == {**state["drafts"][1], "outdated": True}  # same line, range and commit: never remapped


def test_restore_uses_the_newest_archive(env, tmp_path):
    cfg = Config(cache_dir=env["cache"])
    for i, when in enumerate(("20261020T100000Z", "20261023T090000Z", "20261021T100000Z")):
        write_json(archive.archive_dir(cfg, "o", "r") / f"pr-5-{when}.json", {"review_body_draft": f"v{i}"})
    fresh = tmp_path / "fresh"
    summary = archive.restore(cfg, "o", "r", 5, fresh, "h")
    assert StateStore(fresh).load()["review_body_draft"] == "v1"
    assert summary["archived_at"] == "2026-10-23T09:00:00+00:00"
    assert archive.restore(cfg, "o", "r", 6, tmp_path / "other", "h") is None


def pr_head(env: dict, number: int) -> str:
    """Give the temp repo a PR: a commit on its own branch, published as refs/pull/<n>/head."""
    repo = env["repo"]
    _git(repo, "checkout", "-q", "-b", f"pr{number}")
    (repo / "a.py").write_text("".join(f"x{i} = {i}\n" for i in range(number + 10)))
    _git(repo, "commit", "-q", "-am", f"PR {number}")
    head = _git(repo, "rev-parse", "HEAD").strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "update-ref", f"refs/pull/{number}/head", head)
    return head


def fetch_pr(env: dict, monkeypatch, number: int) -> Path:
    """`prr fetch`, with GitHub replaced by the temp repo."""
    root = env["repo"]
    head = _git(root, "rev-parse", f"refs/pull/{number}/head").strip()
    base = _git(root, "rev-parse", "main").strip()
    meta = {"number": number, "title": f"PR {number}", "url": "u", "author": {"login": "a"}, "body": "",
            "state": "OPEN", "isDraft": False, "baseRefName": "main", "headRefName": f"pr{number}",
            "baseRefOid": base, "headRefOid": head, "additions": 0, "deletions": 0, "changedFiles": 0,
            "labels": [], "commits": []}

    def fake_gh_json(*args, **kwargs):
        if args[:2] == ("pr", "view"):
            return meta
        raise gitgh.CommandError(["gh", *args], 1, "", "offline in tests")

    monkeypatch.setattr(fetch, "gh_json", fake_gh_json)
    monkeypatch.setattr(fetch, "find_remote", lambda root, ref: str(root))
    monkeypatch.setenv("GIT_LFS_SKIP_SMUDGE", "1")
    return fetch.fetch(gitgh.PRRef("o", "r", number), root, Config(cache_dir=env["cache"]))


def test_fetch_restores_the_work_into_a_fresh_folder(env, capsys, monkeypatch):
    head = pr_head(env, 5)
    d = make_pr(env, 5)
    state = work_state(head)
    write_json(d / "state.json", state)
    (d / "calls.jsonl").write_text('{"cost_usd": 0.5}\n')
    run_clean(env, capsys, "5")
    assert not d.exists()

    assert fetch_pr(env, monkeypatch, 5) == d
    back = StateStore(d).load()
    assert back["threads"] == state["threads"] and back["reviewed"] == state["reviewed"]
    assert back["drafts"][0] == state["drafts"][0]
    assert back["drafts"][1]["outdated"] is True and back["drafts"][1]["line"] == 7
    assert (d / "calls.jsonl").read_text() == '{"cost_usd": 0.5}\n'
    assert "Restored your work from" in capsys.readouterr().err

    # A later fetch into the same folder leaves the state alone.
    StateStore(d).update(lambda s: s.__setitem__("drafts", []))
    fetch_pr(env, monkeypatch, 5)
    assert StateStore(d).load()["drafts"] == []


def test_after_clean_all_yes_force_every_prs_work_can_be_restored(env, capsys, monkeypatch, live_server):
    monkeypatch.setattr(sys, "stdin", _NoTerminal())
    states = {}
    for n in (5, 6):
        head = pr_head(env, n)
        d = make_pr(env, n)
        states[n] = work_state(head)
        write_json(d / "state.json", states[n])
        live_server(d)
    code, out = run_clean(env, capsys, "--all", "--yes", "--force")
    assert code == 0 and "Skipped" not in out
    for n in (5, 6):
        d = fetch_pr(env, monkeypatch, n)
        back = StateStore(d).load()
        for key in ("threads", "reviewed", "dismissed_suggestions", "submitted"):
            assert back[key] == states[n][key]
        assert [x["id"] for x in back["drafts"]] == ["same", "older"]
        assert back["restored"]["pending_comments"] == 2 and back["restored"]["need_replacing"] == 1


# ---- restored outdated comments in the app: read-only, re-place, never sent ------------------

PORT, TOKEN = 40124, "secret-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


def restored_drafts() -> list[dict]:
    """Two pending comments as a restore leaves them: one from this head ("h"), one from an older commit."""
    base = {"path": "src/core.py", "side": "RIGHT", "start_line": None, "start_side": None, "source": "user",
            "suggestion_id": None, "anchored": True, "created_at": "2026-10-22T10:00:00+00:00"}
    return [{**base, "id": "fresh", "line": 22, "body": "current", "outdated": False, "commit_id": "h"},
            {**base, "id": "old", "line": 7, "body": "from before", "outdated": True, "commit_id": "0" * 40}]


@pytest.fixture()
def app_client(tmp_path, tricky_repo):
    from fastapi.testclient import TestClient

    from prr.server import Guard, create_app
    from tests.test_merge import make_pr_dir

    make_pr_dir(tmp_path, tricky_repo["diff"])
    (tmp_path / "diff.patch").write_text(tricky_repo["diff"])
    restored = {"archived_at": "2026-10-23T17:38:00+00:00", "restored_at": "x", "archive": "a",
                "chats": 0, "pending_comments": 2, "need_replacing": 1, "viewed_marks": 0}
    write_json(tmp_path / "state.json", {**empty_state(), "drafts": restored_drafts(), "restored": restored})
    client = TestClient(Guard(create_app(tmp_path, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    return client


def test_an_outdated_draft_is_read_only_until_re_placed(app_client):
    c = app_client
    assert c.patch("/api/drafts/old", json={"body": "edited"}, headers=ORIGIN).status_code == 409
    assert c.post("/api/drafts/old/replace", json={"side": "MIDDLE", "line": 22}, headers=ORIGIN).status_code == 400
    assert c.post("/api/drafts/nope/replace", json={"side": "RIGHT", "line": 22}, headers=ORIGIN).status_code == 404
    assert c.get("/api/state").json()["drafts"] == restored_drafts()  # failed attempts change nothing

    r = c.post("/api/drafts/old/replace", json={"side": "RIGHT", "line": 22, "start_line": 21}, headers=ORIGIN)
    assert r.status_code == 200
    moved = r.json()
    assert (moved["id"], moved["body"], moved["line"], moved["start_line"]) == ("old", "from before", 22, 21)
    assert moved["outdated"] is False and moved["anchored"] is True and moved["commit_id"] == "h"
    assert c.get("/api/state").json()["drafts"][1] == moved
    assert c.patch("/api/drafts/old", json={"body": "edited"}, headers=ORIGIN).json()["body"] == "edited"

    # Off the diff it is accepted like a new comment: unanchored, so it would go in the review body as a link.
    off = c.post("/api/drafts/fresh/replace", json={"side": "RIGHT", "line": 15}, headers=ORIGIN).json()
    assert off["anchored"] is False and off["outdated"] is False


def test_the_restore_summary_shows_once_and_can_be_dismissed(app_client):
    c = app_client
    assert c.get("/api/state").json()["restored"]["need_replacing"] == 1
    assert c.post("/api/restored/dismiss", headers=ORIGIN).json() == {"ok": True}
    assert "restored" not in c.get("/api/state").json()


def test_the_app_gets_the_restore_banner_text(app_client, tmp_path):
    restored = app_client.get("/api/state").json()["restored"]
    assert restored["message"] == archive.message(restored)
    assert restored["message"].startswith("Restored your work from Oct 2")  # Oct 23 (or 24 east of UTC+6)
    assert restored["message"].endswith(": 2 pending comments (1 needs re-placing).")
    assert "message" not in read_json(tmp_path / "state.json")["restored"]  # added when sent, never stored


def test_an_unreadable_archive_date_still_gets_a_banner(tmp_path, tricky_repo):
    from fastapi.testclient import TestClient

    from prr.server import Guard, create_app
    from tests.test_merge import make_pr_dir

    make_pr_dir(tmp_path, tricky_repo["diff"])
    restored = {"archived_at": "yesterday", "chats": 3, "pending_comments": 0, "need_replacing": 0, "viewed_marks": 12}
    write_json(tmp_path / "state.json", {**empty_state(), "restored": restored})
    client = TestClient(Guard(create_app(tmp_path, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    assert client.get("/api/state").json()["restored"]["message"] == "Restored your work: 3 chats, 12 viewed marks."


def test_submit_never_sends_outdated_drafts(app_client, monkeypatch):
    from prr import github, server

    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(server, "viewer_login", lambda: "me")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: "h")
    c = app_client
    preview = c.post("/api/review/preview", json={"body": "Thanks", "event": "COMMENT"}, headers=ORIGIN).json()
    assert preview["inline_count"] == 1 and preview["unanchored"] == []
    assert "from before" not in json.dumps(preview["payload"])
    r = c.post("/api/review/submit", json={"body": "Thanks", "event": "COMMENT"}, headers=ORIGIN)
    assert r.status_code == 200
    state = c.get("/api/state").json()
    assert [d["id"] for d in state["drafts"]] == ["old"] and state["submitted"][-1]["comments"] == 1


def test_the_chat_never_sends_outdated_drafts(tmp_path, tricky_repo, monkeypatch):
    from prr import chat_tools
    from prr.config import ChatSettings
    from tests.test_merge import make_pr_dir

    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    make_pr_dir(tmp_path, tricky_repo["diff"])
    write_json(tmp_path / "state.json", {**empty_state(), "drafts": restored_drafts()})
    text, err = chat_tools.call_tool(tmp_path, Config(chat=ChatSettings()), "post_review",
                                     {"body": "Thanks", "include_pending": True})
    assert not err and "1 line comments" in text
    assert [d["id"] for d in StateStore(tmp_path).load()["drafts"]] == ["old"]
