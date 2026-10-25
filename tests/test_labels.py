"""E3 labels: labels.jsonl (append, lock, compaction, what counts toward recall), match_label, and the label writes
from posting and from deleting a pending comment. Nothing here reaches GitHub (PRR_GITHUB_DRY_RUN=1 and fakes)."""

import fcntl
import json
import os
import stat
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prr import chat_tools, github, labels, server
from prr.config import Config
from prr.gitgh import CommandError, PRRef
from prr.server import Guard, create_app
from prr.state import StateStore
from tests.conftest import _git
from tests.test_merge import make_pr_dir


def mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def problem(**kw) -> dict:
    base = {"pr": 7, "kind": "problem", "source": "posted", "commit_id": "c1", "path": "src/a.py", "side": "RIGHT",
            "line": 22, "text": "This drops the error"}
    return labels.new_label(**{**base, **kw})


# ---- the file -----------------------------------------------------------------------------------


def test_append_writes_one_private_line_per_label(tmp_path):
    folder = tmp_path / "o__r"
    first = labels.append(folder, pr=7, kind="problem", source="imported", commit_id="c1", merge_base_sha="b1",
                          path="src/a.py", side="RIGHT", line=22, text="This drops the error", author="me",
                          gh_comment_id=101, split="holdout")
    labels.append(folder, pr=7, kind="not-problem", source="verdict", origin_id="s-2", stop_kind="finding",
                  path="src/a.py", line=40)
    assert mode(folder / "labels.jsonl") == 0o600 and mode(folder / "labels.lock") == 0o600
    rows = labels.read(folder)
    assert [r["kind"] for r in rows] == ["problem", "not-problem"]
    assert rows[0] == first and set(first) == {
        "v", "id", "pr", "commit_id", "merge_base_sha", "path", "side", "line", "kind", "text", "author", "source",
        "origin_id", "gh_comment_id", "finding_id", "stop_kind", "split", "at"}
    assert (first["v"], first["split"], rows[1]["split"]) == (1, "holdout", "train")


def test_bad_values_are_refused():
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="maybe", source="posted")
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="problem", source="dismissed")      # dismissing is not a label source
    with pytest.raises(ValueError):
        labels.new_label(pr=1, kind="problem", source="posted", split="test")


def test_a_damaged_line_is_skipped(tmp_path):
    folder = tmp_path / "o__r"
    labels.write(folder, [problem()])
    with open(folder / "labels.jsonl", "a") as fh:
        fh.write('{"pr": 7, "kind": "probl')
    assert len(labels.read(folder)) == 1


def test_a_busy_lock_times_out(tmp_path):
    folder = tmp_path / "o__r"
    folder.mkdir()
    holder = os.open(folder / "labels.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)                         # another process is appending
    try:
        with pytest.raises(TimeoutError):
            labels.write(folder, [problem()], timeout=0.2)
        assert labels.LOCK_TIMEOUT_S == 5.0
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    labels.write(folder, [problem()])                          # free again
    assert len(labels.read(folder)) == 1


def test_save_never_raises(tmp_path, monkeypatch, capsys):
    pr_dir = tmp_path / "o__r" / "pr-7"
    pr_dir.mkdir(parents=True)

    def full_disk(folder, rows, timeout=labels.LOCK_TIMEOUT_S):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(labels, "write", full_disk)
    assert labels.save(pr_dir, [problem()]) == "Couldn't save this answer for eval"
    assert "could not save 1 label" in capsys.readouterr().err
    monkeypatch.undo()
    assert labels.save(pr_dir, [problem(text="half a character: \ud800")]) == "Couldn't save this answer for eval"
    assert "UnicodeEncodeError" in capsys.readouterr().err


def test_only_pr_folders_in_the_cache_get_labels(tmp_path):
    assert labels.repo_dir_for(tmp_path / "o__r" / "pr-7") == tmp_path / "o__r"
    assert labels.repo_dir_for(tmp_path / "_eval" / "run" / "pr-7-abc") is None    # an eval copy
    assert labels.repo_dir_for(tmp_path / "test_x0") is None
    assert labels.save(tmp_path / "test_x0", [problem()]) is None
    assert not (tmp_path / "labels.jsonl").exists()


# ---- compaction: one label per issue --------------------------------------------------------------


def test_a_verdict_its_posted_comment_and_an_import_are_one_label():
    verdict = problem(source="verdict", origin_id="s-1", stop_kind="finding", finding_id="f-1", commit_id="c1")
    posted = problem(source="posted", origin_id="s-1", gh_comment_id=555)
    imported = problem(source="imported", gh_comment_id=555, author="me", split="holdout")
    [one] = labels.compact([verdict, posted, imported])
    assert one["source"] == "imported" and one["origin_id"] == "s-1" and one["finding_id"] == "f-1"


def test_an_import_of_a_comment_posted_without_its_id_is_the_same_label():
    posted = problem(source="posted", origin_id="s-1", gh_comment_id=None, line=22)
    imported = problem(source="imported", gh_comment_id=900, line=21)   # one line apart, same place otherwise
    elsewhere = problem(source="imported", gh_comment_id=901, line=30)
    other_side = problem(source="imported", gh_comment_id=902, line=22, side="LEFT")
    out = labels.compact([posted, imported, elsewhere, other_side])
    assert [x["gh_comment_id"] for x in out] == [900, 901, 902]
    assert out[0]["origin_id"] == "s-1"


def test_two_different_comments_on_adjacent_lines_stay_two():
    a = problem(source="posted", gh_comment_id=10, line=10, text="Leaks the file handle")
    b = problem(source="posted", gh_comment_id=11, line=11, text="Wrong timezone")
    assert len(labels.compact([a, b])) == 2
    stops = [problem(source="verdict", origin_id="s-1", line=10), problem(source="verdict", origin_id="s-2", line=11)]
    assert len(labels.compact(stops)) == 2


def test_the_latest_answer_wins_and_retracted_removes_it():
    real = problem(source="verdict", origin_id="s-1")
    assert labels.compact([real, problem(kind="not-problem", source="verdict", origin_id="s-1")])[0]["kind"] == \
        "not-problem"
    retract = labels.new_label(pr=7, kind="retracted", source="verdict", origin_id="s-1")
    assert labels.compact([real, retract]) == []
    again = problem(source="verdict", origin_id="s-1", text="real after all")
    assert [x["text"] for x in labels.compact([real, retract, again])] == ["real after all"]


def test_retracting_a_deleted_draft(tmp_path):
    pr = {"number": 7}
    assert labels.retracted(pr, {"id": "d1", "origin_id": None}) == []
    [row] = labels.retracted(pr, {"id": "d1", "origin_id": "s-1", "path": "src/a.py", "side": "RIGHT", "line": 22,
                                  "commit_id": "c1"})
    assert (row["kind"], row["origin_id"], row["source"]) == ("retracted", "s-1", "verdict")


def test_load_compacts_what_is_on_disk(tmp_path):
    folder = tmp_path / "o__r"
    labels.write(folder, [problem(origin_id="s-1"), problem(origin_id="s-1", gh_comment_id=3)])
    labels.write(folder, [problem(source="imported", gh_comment_id=3)])
    assert len(labels.read(folder)) == 3 and len(labels.load(folder)) == 1


# ---- what counts toward recall, and matching -----------------------------------------------------


def test_the_recall_denominator_leaves_out_ci_and_look_for():
    assert labels.countable(problem())
    assert labels.countable(problem(source="verdict", origin_id="s", stop_kind="risk-note"))
    assert not labels.countable(problem(source="verdict", origin_id="s", stop_kind="ci", path=None, line=None))
    assert not labels.countable(problem(source="verdict", origin_id="s", stop_kind="look_for"))   # even with a line
    assert not labels.countable(problem(line=None))
    assert not labels.countable(problem(kind="not-problem"))


def test_match_label_uses_the_finding_title_and_impact():
    label = problem(line=22, text="The retry loop swallows the timeout error, so callers never see it")
    finding = {"path": "src/a.py", "line": 24, "title": "Timeout error swallowed", "impact": "Callers never see it"}
    assert labels.match_label(label, finding)
    assert not labels.match_label(label, {**finding, "line": 26})                     # more than 3 lines away
    assert not labels.match_label(label, {**finding, "path": "src/b.py"})
    assert not labels.match_label(label, {**finding, "title": "Unused import", "impact": "Lint noise"})
    assert not labels.match_label(problem(line=None), finding)                       # no line: never matched


def test_posted_drafts_get_their_github_ids():
    drafts = [
        {"id": "d1", "path": "src/a.py", "line": 22, "side": "RIGHT", "body": "first", "anchored": True,
         "origin_id": "s-1"},
        {"id": "d2", "path": "src/a.py", "line": 15, "side": "RIGHT", "body": "outside the diff", "anchored": False},
        {"id": "d3", "path": "src/b.py", "line": 3, "side": "LEFT", "body": "removed line", "anchored": True},
    ]
    gh = [{"id": 11, "path": "src/b.py", "line": 3, "body": "removed line"},
          {"id": 10, "path": "src/a.py", "line": 22, "body": "first\n"}]
    out = labels.posted_drafts(drafts, gh)
    assert [(c["path"], c["line"], c["side"], c["gh_comment_id"], c["origin_id"]) for c in out] == [
        ("src/a.py", 22, "RIGHT", 10, "s-1"), ("src/a.py", 15, "RIGHT", None, None), ("src/b.py", 3, "LEFT", 11, None)]
    rows = labels.for_posted({"number": 7, "head_sha": "h", "merge_base_sha": "b"}, out, commit_id="h", author="me")
    assert {(r["kind"], r["source"], r["commit_id"], r["merge_base_sha"], r["author"]) for r in rows} == {
        ("problem", "posted", "h", "b", "me")}
    moved = labels.for_posted({"number": 7, "head_sha": "h", "merge_base_sha": "b"}, out, commit_id="h2", author="me")
    assert {r["merge_base_sha"] for r in moved} == {None}              # posted on a newer head: eval works it out


# ---- label writes: the app's Submit, the chat's posts, and deleting a pending comment ---------------

PORT = 40125
TOKEN = "labels-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}


@pytest.fixture()
def pr(tmp_path, tricky_repo, monkeypatch):
    """A PR folder where the cache keeps it (<cache>/o__r/pr-1), with the app and the chat tools on it."""
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: "h")
    monkeypatch.setattr(server, "viewer_login", lambda: "me")
    pr_dir = tmp_path / "o__r" / "pr-1"
    pr_dir.mkdir(parents=True)
    make_pr_dir(pr_dir, tricky_repo["diff"])
    (pr_dir / "diff.patch").write_text(tricky_repo["diff"])
    client = TestClient(Guard(create_app(pr_dir, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    return SimpleNamespace(client=client, dir=pr_dir, repo=tmp_path / "o__r", cfg=Config())


def add(pr, line: int, body: str, **extra) -> dict:
    r = pr.client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": line, "body": body, **extra},
                       headers=ORIGIN)
    assert r.status_code == 200, r.text
    return r.json()


def with_origin(pr, draft: dict, origin: str) -> None:
    """Guided review (Wave 3) sets origin_id from the stop; until then, set it the way it will be stored."""
    StateStore(pr.dir).update(lambda s: next(d for d in s["drafts"] if d["id"] == draft["id"]).update(origin_id=origin))


def test_new_drafts_have_no_origin(pr):
    assert add(pr, 22, "x")["origin_id"] is None


def test_submit_saves_a_problem_label_per_posted_comment(pr, monkeypatch):
    first = add(pr, 22, "Check the inserted lines", start_line=21)
    with_origin(pr, first, "s-1")
    add(pr, 15, "This line is outside the diff")
    monkeypatch.setattr(github, "submit", lambda o, r, n, payload: {"id": 9, "html_url": "u", "user": {"login": "me"}})
    asked = []

    def ids(owner, repo, number, review_id):
        asked.append((owner, repo, number, review_id))
        return [{"id": 501, "path": "src/core.py", "line": 22, "body": "Check the inserted lines"}]

    monkeypatch.setattr(github, "review_comments", ids)
    r = pr.client.post("/api/review/submit", json={"body": "Notes", "event": "COMMENT"}, headers=ORIGIN)
    assert r.status_code == 200 and "warning" not in r.json()
    assert asked == [("o", "r", 1, 9)]
    assert StateStore(pr.dir).load()["drafts"] == []
    rows = labels.read(pr.repo)
    assert [(x["kind"], x["source"], x["pr"], x["commit_id"], x["path"], x["line"], x["side"], x["gh_comment_id"],
             x["origin_id"], x["author"], x["text"]) for x in rows] == [
        ("problem", "posted", 1, "h", "src/core.py", 22, "RIGHT", 501, "s-1", "me", "Check the inserted lines"),
        ("problem", "posted", 1, "h", "src/core.py", 15, "RIGHT", None, None, "me", "This line is outside the diff")]
    assert {x["merge_base_sha"] for x in rows} == {"b"}


def test_a_review_saved_as_pending_on_github_is_not_a_label_yet(pr, monkeypatch):
    add(pr, 22, "first")
    r = pr.client.post("/api/review/submit", json={"body": "", "event": "PENDING"}, headers=ORIGIN)
    assert r.status_code == 200 and labels.read(pr.repo) == []
    assert StateStore(pr.dir).load()["drafts"] == []


def test_a_failed_label_write_keeps_the_post_and_warns(pr, monkeypatch):
    def busy(folder, rows, timeout=labels.LOCK_TIMEOUT_S):
        raise TimeoutError("labels.lock stayed busy for 5 s")

    monkeypatch.setattr(labels, "write", busy)
    add(pr, 22, "first")
    r = pr.client.post("/api/review/submit", json={"body": "Notes", "event": "COMMENT"}, headers=ORIGIN)
    assert r.status_code == 200
    assert r.json()["ok"] and r.json()["warning"] == "Couldn't save this answer for eval"
    s = StateStore(pr.dir).load()
    assert s["drafts"] == [] and len(s["submitted"]) == 1                   # the review itself went through

    text, err = chat_tools.call_tool(pr.dir, pr.cfg, "post_comment", {"path": "src/core.py", "line": 22, "body": "x"})
    assert not err and "Posted the comment" in text and "Couldn't save this answer for eval" in text


def test_a_bug_in_making_labels_never_leaves_posted_drafts_behind(pr, monkeypatch):
    def broken(*a, **k):
        raise KeyError("head_sha")

    monkeypatch.setattr(labels, "for_posted", broken)
    add(pr, 22, "first")
    r = pr.client.post("/api/review/submit", json={"body": "Notes", "event": "COMMENT"}, headers=ORIGIN)
    assert r.status_code == 200 and r.json()["warning"] == "Couldn't save this answer for eval"
    add(pr, 5, "second")
    text, err = chat_tools.call_tool(pr.dir, pr.cfg, "post_review", {"body": "More", "include_pending": True})
    assert not err and "Couldn't save this answer for eval" in text
    s = StateStore(pr.dir).load()
    assert s["drafts"] == [] and len(s["submitted"]) == 2                   # nothing can be posted twice


def test_chat_posts_pass_each_comment_to_record(pr, monkeypatch):
    seen = []
    real = chat_tools._record

    def spy(pr_dir, html_url, event, comments, **kw):
        out = real(pr_dir, html_url, event, comments, **kw)
        posted = kw.get("posted")
        seen.append(posted() if callable(posted) else posted)
        return out

    monkeypatch.setattr(chat_tools, "_record", spy)
    monkeypatch.setattr(github, "post_comment", lambda o, r, n, c: {"id": 77, "html_url": "u", "user": {"login": "me"}})
    text, err = chat_tools.call_tool(pr.dir, pr.cfg, "post_comment",
                                     {"path": "src/core.py", "line": 22, "body": "Posted from the chat"})
    assert not err, text

    pending = add(pr, 5, "Sent with the review")
    with_origin(pr, pending, "s-9")
    monkeypatch.setattr(github, "submit", lambda o, r, n, payload: {"id": 8, "html_url": "u", "user": {"login": "me"}})
    states = []

    def ids(o, r, n, rid):
        states.append(StateStore(pr.dir).load())            # GitHub is asked for the ids only once state.json is safe
        return [{"id": 88, "path": "src/core.py", "line": 5, "body": "Sent with the review"}]

    monkeypatch.setattr(github, "review_comments", ids)
    text, err = chat_tools.call_tool(pr.dir, pr.cfg, "post_review", {"body": "Done", "include_pending": True})
    assert not err, text
    assert states[0]["drafts"] == [] and states[0]["submitted"][-1]["review_id"] == 8

    assert seen == [
        [{"path": "src/core.py", "line": 22, "side": "RIGHT", "body": "Posted from the chat", "gh_comment_id": 77}],
        [{"path": "src/core.py", "line": 5, "side": "RIGHT", "body": "Sent with the review", "gh_comment_id": 88,
          "origin_id": "s-9"}]]
    rows = labels.read(pr.repo)
    assert [(x["gh_comment_id"], x["origin_id"], x["source"], x["author"]) for x in rows] == [
        (77, None, "posted", "me"), (88, "s-9", "posted", "me")]
    # a review with only a summary posts no comments and saves no labels
    text, err = chat_tools.call_tool(pr.dir, pr.cfg, "post_review", {"body": "One more thing"})
    assert not err and len(labels.read(pr.repo)) == 2


def test_deleting_a_comment_made_from_an_answer_retracts_it(pr):
    plain, answered = add(pr, 22, "my own"), add(pr, 5, "from a stop")
    with_origin(pr, answered, "s-3")
    labels.append(pr.repo, pr=1, kind="problem", source="verdict", origin_id="s-3", stop_kind="finding",
                  path="src/core.py", side="RIGHT", line=5, commit_id="h", text="Real problem")
    assert len(labels.load(pr.repo)) == 1

    assert pr.client.delete(f"/api/drafts/{plain['id']}", headers=ORIGIN).json() == {"ok": True}
    assert len(labels.read(pr.repo)) == 1                                    # nothing to take back
    assert pr.client.delete(f"/api/drafts/{answered['id']}", headers=ORIGIN).json() == {"ok": True}
    assert [x["kind"] for x in labels.read(pr.repo)] == ["problem", "retracted"]
    assert labels.load(pr.repo) == []


def test_a_failed_retraction_still_deletes_and_warns(pr, monkeypatch):
    answered = add(pr, 5, "from a stop")
    with_origin(pr, answered, "s-3")

    def full_disk(folder, rows, timeout=labels.LOCK_TIMEOUT_S):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(labels, "write", full_disk)
    r = pr.client.delete(f"/api/drafts/{answered['id']}", headers=ORIGIN)
    assert r.json() == {"ok": True, "warning": "Couldn't save this answer for eval"}
    assert StateStore(pr.dir).load()["drafts"] == []


# ---- prr labels import --------------------------------------------------------------------------


def gh_comment(cid: int, login: str, kind: str = "User", **kw) -> dict:
    """One review comment as GitHub's REST API returns it."""
    base = {"id": cid, "user": {"login": login, "type": kind}, "path": "src/core.py", "line": 22, "side": "RIGHT",
            "original_line": 22, "commit_id": "c-now", "original_commit_id": "c-then", "body": f"comment {cid}",
            "in_reply_to_id": None, "created_at": "2026-10-01T10:00:00Z", "html_url": "u"}
    return {**base, **kw}


class FakeGitHub:
    """Stands in for `gh`: answers the read-only calls the import makes and records every call."""

    def __init__(self, monkeypatch, comments: list[dict], meta: dict):
        self.calls: list[tuple] = []
        self.comments, self.meta = comments, meta

        def fake_gh(*args, **kwargs):
            self.calls.append(args)
            return "".join(json.dumps(c) + "\n" for c in self.comments)

        def fake_gh_json(*args, **kwargs):
            self.calls.append(args)
            if args[:2] == ("pr", "view"):
                return self.meta
            raise CommandError(["gh", *args], 1, "", "offline in tests")

        monkeypatch.setattr(github, "gh", fake_gh)
        monkeypatch.setattr(labels, "gh_json", fake_gh_json)

    def writes(self) -> list[tuple]:
        return [c for c in self.calls if {"-X", "--method", "-f", "-F", "--field", "--raw-field", "--input"} & set(c)]


def import_into(tmp_path, tricky_repo, monkeypatch, comments, meta, **kw):
    monkeypatch.setattr(labels, "find_remote", lambda root, ref: str(root))
    fake = FakeGitHub(monkeypatch, comments, meta)
    folder = tmp_path / "o__r"
    out = labels.import_comments(PRRef("o", "r", 1), tricky_repo["root"], folder, **kw)
    assert fake.writes() == []                                              # only reads GitHub
    return out, labels.read(folder), fake


def test_import_keeps_your_root_comments_where_you_wrote_them(tmp_path, tricky_repo, monkeypatch):
    head, base = tricky_repo["head"], tricky_repo["base"]
    comments = [
        gh_comment(1, "me", original_commit_id=head, commit_id=head),
        gh_comment(2, "me", line=None, original_line=30, original_commit_id=head, commit_id="c-later"),  # outdated
        gh_comment(3, "me", in_reply_to_id=1),                                    # a reply: not a new issue
        gh_comment(4, "alice", original_commit_id=head),                          # someone else's
        gh_comment(5, "Me", original_line=None, line=7, commit_id=head, original_commit_id="c-old"),  # no original
    ]
    merged = {"number": 1, "state": "MERGED", "baseRefName": "main", "baseRefOid": base}
    out, rows, fake = import_into(tmp_path, tricky_repo, monkeypatch, comments, merged, reviewers="me", viewer="me")
    assert [(r["gh_comment_id"], r["commit_id"], r["line"], r["author"]) for r in rows] == [
        (1, head, 22, "me"), (2, head, 30, "me"), (5, head, 7, "Me")]
    assert {(r["kind"], r["source"], r["split"], r["merge_base_sha"]) for r in rows} == {
        ("problem", "imported", "train", base)}                                # merged PR: GitHub's recorded base
    assert (out["imported"], out["outdated"]) == (3, 1)
    assert ("pr", "view", "1", "--repo", "o/r", "--json", "number,state,baseRefName,baseRefOid") in fake.calls
    assert ("api", "--paginate", "repos/o/r/pulls/1/comments?per_page=100", "--jq", ".[]") in fake.calls


def test_a_merged_pr_whose_base_moved_on_starts_at_the_merge_base(tmp_path, tricky_repo, monkeypatch):
    root, head, base = tricky_repo["root"], tricky_repo["head"], tricky_repo["base"]
    _git(root, "checkout", "-q", "-b", "trunk", base)                         # the base branch moved on meanwhile
    (root / "elsewhere.txt").write_text("later\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "later on the base branch")
    later = _git(root, "rev-parse", "HEAD").strip()
    _git(root, "checkout", "-q", "main")
    merged = {"number": 1, "state": "MERGED", "baseRefName": "trunk", "baseRefOid": later}
    _, rows, _ = import_into(tmp_path, tricky_repo, monkeypatch, [gh_comment(1, "me", original_commit_id=head)],
                             merged, viewer="me")
    assert rows[0]["merge_base_sha"] == base                                # not `later`: its changes aren't the PR's


def test_import_of_an_open_pr_uses_git_merge_base(tmp_path, tricky_repo, monkeypatch):
    head, base = tricky_repo["head"], tricky_repo["base"]
    _git(tricky_repo["root"], "branch", "trunk", base)                         # the PR's base branch
    open_pr = {"number": 1, "state": "OPEN", "baseRefName": "trunk", "baseRefOid": "not-used-for-open-prs"}
    _, rows, _ = import_into(tmp_path, tricky_repo, monkeypatch, [gh_comment(1, "me", original_commit_id=head)],
                             open_pr, viewer="me")
    assert rows[0]["merge_base_sha"] == base


def test_import_all_reviewers_leaves_out_bots(tmp_path, tricky_repo, monkeypatch):
    head = tricky_repo["head"]
    comments = [gh_comment(1, "me", original_commit_id=head), gh_comment(2, "alice", original_commit_id=head),
                gh_comment(3, "review-tool[bot]", original_commit_id=head),
                gh_comment(4, "helper", kind="Bot", original_commit_id=head)]
    merged = {"number": 1, "state": "MERGED", "baseRefName": "main", "baseRefOid": tricky_repo["base"]}
    _, rows, _ = import_into(tmp_path, tricky_repo, monkeypatch, comments, merged, reviewers="all")
    assert [r["author"] for r in rows] == ["me", "alice"]


def test_import_again_adds_nothing_new_and_a_holdout_split_wins(tmp_path, tricky_repo, monkeypatch):
    head = tricky_repo["head"]
    comments = [gh_comment(1, "me", original_commit_id=head), gh_comment(2, "me", original_commit_id=head, line=40,
                                                                          original_line=40)]
    merged = {"number": 1, "state": "MERGED", "baseRefName": "main", "baseRefOid": tricky_repo["base"]}
    import_into(tmp_path, tricky_repo, monkeypatch, comments, merged, viewer="me")
    out, rows, _ = import_into(tmp_path, tricky_repo, monkeypatch, comments, merged, viewer="me")
    assert (out["imported"], out["already_imported"], len(rows)) == (0, 2, 2)
    out, rows, _ = import_into(tmp_path, tricky_repo, monkeypatch, comments, merged, viewer="me", split="holdout")
    assert out["imported"] == 2
    assert [x["split"] for x in labels.load(tmp_path / "o__r")] == ["holdout", "holdout"]


def test_import_needs_your_login_for_your_comments(tmp_path, tricky_repo):
    with pytest.raises(ValueError, match="login"):
        labels.import_comments(PRRef("o", "r", 1), tricky_repo["root"], tmp_path, reviewers="me", viewer=None)
    with pytest.raises(ValueError, match="me or all"):
        labels.import_comments(PRRef("o", "r", 1), tricky_repo["root"], tmp_path, reviewers="bots")


def test_prr_labels_import_command(tmp_path, tricky_repo, monkeypatch, capsys):
    from prr import cli, config, gitgh

    head = tricky_repo["head"]
    monkeypatch.setattr(labels, "find_remote", lambda root, ref: str(root))
    FakeGitHub(monkeypatch, [gh_comment(1, "me", original_commit_id=head)],
               {"number": 1, "state": "MERGED", "baseRefName": "main", "baseRefOid": tricky_repo["base"]})
    monkeypatch.setattr(gitgh, "viewer_login", lambda: "me")
    monkeypatch.setattr(cli, "parse_pr_arg", lambda arg, root: PRRef("o", "r", int(arg)))
    monkeypatch.setattr(cli, "load_config", lambda: Config(cache_dir=tmp_path / "cache"))
    monkeypatch.setattr(config, "REPO_DIR", tmp_path)                    # no config.toml: nothing real is read
    with pytest.raises(SystemExit) as done:
        cli.main(["--repo", str(tricky_repo["root"]), "labels", "import", "1", "--split", "holdout"])
    assert done.value.code == 0
    out = capsys.readouterr()
    assert json.loads(out.out)[0]["imported"] == 1 and "#1: 1 of your comments added as holdout labels" in out.err
    [row] = labels.load(tmp_path / "cache" / "o__r")
    assert row["split"] == "holdout" and row["gh_comment_id"] == 1
