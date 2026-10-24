"""Posting a review: the app's Submit, the chat's post_review, and the claim helper they share (prr/drafts.py).
Nothing here reaches GitHub (PRR_GITHUB_DRY_RUN=1, or a fake `github.submit` / `github.gh`)."""

import subprocess
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prr import chat_tools, drafts, github, server
from prr.config import Config
from prr.server import Guard, create_app
from prr.state import StateStore
from tests.test_merge import make_pr_dir

PORT = 40124
TOKEN = "posting-token"
ORIGIN = {"origin": f"http://127.0.0.1:{PORT}"}
DRY_RUN_URL = "https://github.com/dry-run/repos/o/r/pulls/1/reviews"


@pytest.fixture()
def app(tmp_path, tricky_repo, monkeypatch):
    monkeypatch.setenv("PRR_GITHUB_DRY_RUN", "1")
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: "h")   # the PR hasn't moved
    monkeypatch.setattr(server, "viewer_login", lambda: "me")                       # not the author ("a")
    make_pr_dir(tmp_path, tricky_repo["diff"])
    (tmp_path / "diff.patch").write_text(tricky_repo["diff"])
    client = TestClient(Guard(create_app(tmp_path, Config()), TOKEN, PORT), base_url=f"http://127.0.0.1:{PORT}")
    client.cookies.set("prr_session", TOKEN)
    sent: list[dict] = []
    real_submit = github.submit

    def spy(owner, repo, number, payload):
        sent.append(payload)
        return real_submit(owner, repo, number, payload)

    monkeypatch.setattr(github, "submit", spy)
    return SimpleNamespace(client=client, pr_dir=tmp_path, sent=sent)


def add_draft(app, line: int, body: str, **extra) -> dict:
    r = app.client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": line, "body": body, **extra},
                        headers=ORIGIN)
    assert r.status_code == 200, r.text
    return r.json()


def submit(app, body: str = "", event: str = "COMMENT", commit: str = "analyzed"):
    return app.client.post("/api/review/submit", json={"body": body, "event": event, "commit": commit}, headers=ORIGIN)


def state(app) -> dict:
    return StateStore(app.pr_dir).load()


def test_submit_sends_the_drafts_and_records_the_review(app, monkeypatch):
    add_draft(app, 22, "Check the inserted lines", start_line=21)
    add_draft(app, 15, "This line is outside the diff")

    r = submit(app, "Two notes.")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "review_id": 0, "html_url": DRY_RUN_URL}
    assert app.sent == [{
        "commit_id": "h",
        "event": "COMMENT",
        "comments": [{"path": "src/core.py", "body": "Check the inserted lines", "line": 22, "side": "RIGHT",
                      "start_line": 21, "start_side": "RIGHT"}],
        "body": "Two notes.\n\n---\n**Comments on lines outside the diff:**\n\n"
                "- [`src/core.py#L15`](https://github.com/o/r/blob/h/src/core.py#L15):\n  This line is outside the diff",
    }]
    s = state(app)
    assert s["drafts"] == []
    [record] = s["submitted"]
    assert set(record) == {"review_id", "html_url", "event", "comments", "at"}
    assert (record["review_id"], record["html_url"], record["event"], record["comments"]) == (0, DRY_RUN_URL, "COMMENT", 1)

    # With no pending comments, the summary alone is sent; "current" posts on the PR's latest commit.
    monkeypatch.setattr(github, "current_head", lambda owner, repo, number: "h2")
    r = submit(app, "Just a summary.", commit="current")
    assert r.status_code == 200, r.text
    assert app.sent[-1] == {"commit_id": "h2", "event": "COMMENT", "comments": [], "body": "Just a summary."}
    assert [x["comments"] for x in state(app)["submitted"]] == [1, 0]


@pytest.mark.parametrize("event", ["APPROVE", "REQUEST_CHANGES"])
def test_the_author_cannot_approve_or_request_changes(app, monkeypatch, event):
    monkeypatch.setattr(server, "viewer_login", lambda: "A")   # the PR author, in any letter case
    draft = add_draft(app, 22, "x")

    r = submit(app, "Looks good", event=event)
    assert r.status_code == 400
    assert r.json()["detail"] == "GitHub doesn't let a PR author approve or request changes on their own PR"
    assert app.sent == []
    assert state(app)["drafts"] == [draft] and state(app)["submitted"] == []


def test_a_github_error_keeps_the_drafts(app, monkeypatch):
    def reject(owner, repo, number, payload):
        app.sent.append(payload)
        raise github.ReviewError("GitHub rejected the review: Validation Failed")

    monkeypatch.setattr(github, "submit", reject)
    drafts = [add_draft(app, 22, "first"), add_draft(app, 15, "second")]

    r = submit(app, "Notes")
    assert r.status_code == 422
    assert r.json()["detail"] == "GitHub rejected the review: Validation Failed"
    assert len(app.sent) == 1
    assert state(app)["drafts"] == drafts and state(app)["submitted"] == []


def test_a_draft_added_while_posting_is_kept(app, monkeypatch):
    add_draft(app, 22, "sent with the review")

    def slow(owner, repo, number, payload):
        app.sent.append(payload)
        text, err = chat_tools.call_tool(app.pr_dir, Config(), "add_pending_comment",
                                         {"path": "src/core.py", "line": 5, "body": "added while posting"})
        assert not err, text
        return {"id": 7, "html_url": "https://github.com/o/r/pull/1#review-7"}

    monkeypatch.setattr(github, "submit", slow)

    assert submit(app, "Notes").status_code == 200
    assert [c["body"] for c in app.sent[0]["comments"]] == ["sent with the review"]
    assert [d["body"] for d in state(app)["drafts"]] == ["added while posting"]
    assert state(app)["submitted"][-1]["review_id"] == 7


# ---- the claim helper (prr/drafts.py) ---------------------------------------------------

T0 = 1_800_000_000.0  # a fixed clock


def store_with(tmp_path, *items: dict) -> StateStore:
    store = StateStore(tmp_path)
    store.update(lambda s: s["drafts"].extend({"body": i["id"], "outdated": False, **i} for i in items))
    return store


def test_claim_marks_what_this_post_sends_and_skips_the_rest(tmp_path):
    store = store_with(tmp_path, {"id": "a"}, {"id": "b"}, {"id": "old", "outdated": True})

    claim = drafts.claim_for_post(store, "p1", now=T0)
    assert [d["id"] for d in claim.drafts] == ["a", "b"] and (claim.busy, claim.outdated) == (0, 1)
    held = {d["id"]: (d.get("posting"), d.get("posting_at")) for d in store.load()["drafts"]}
    at = datetime.fromtimestamp(T0, timezone.utc).isoformat(timespec="seconds")
    assert held == {"a": ("p1", at), "b": ("p1", at), "old": (None, None)}

    second = drafts.claim_for_post(store, "p2", now=T0 + 5)   # another post at the same time gets nothing
    assert second.drafts == [] and (second.busy, second.outdated) == (2, 1)
    assert [d["posting"] for d in store.load()["drafts"] if d["id"] != "old"] == ["p1", "p1"]


def test_finish_removes_only_the_claimed_drafts_and_records_the_review(tmp_path):
    store = store_with(tmp_path, {"id": "a"}, {"id": "old", "outdated": True})
    drafts.claim_for_post(store, "p1", now=T0)
    store.update(lambda s: s["drafts"].append({"id": "new", "body": "added meanwhile", "outdated": False}))

    drafts.finish_post(store, "p2", {"review_id": 0})          # someone else's post id removes nothing
    assert [d["id"] for d in store.load()["drafts"]] == ["a", "old", "new"]
    drafts.finish_post(store, "p1", {"review_id": 9})
    s = store.load()
    assert [d["id"] for d in s["drafts"]] == ["old", "new"]
    assert [r["review_id"] for r in s["submitted"]] == [0, 9]


def test_release_makes_the_drafts_pending_again_unchanged(tmp_path):
    store = store_with(tmp_path, {"id": "a"}, {"id": "b"})
    before = store.load()["drafts"]
    drafts.claim_for_post(store, "p1", now=T0)

    drafts.release(store, "p2")                                # only the owner's release counts
    assert all(d["posting"] == "p1" for d in store.load()["drafts"])
    drafts.release(store, "p1")
    assert store.load()["drafts"] == before
    assert [d["id"] for d in drafts.claim_for_post(store, "p3", now=T0).drafts] == ["a", "b"]


def test_a_stale_claim_is_released_by_the_next_claim(tmp_path):
    store = store_with(tmp_path, {"id": "a"})
    drafts.claim_for_post(store, "died", now=T0)
    [d] = store.load()["drafts"]
    expiry = T0 + drafts.CLAIM_EXPIRY_S
    assert drafts.is_posting(d, now=expiry - 1) and not drafts.is_posting(d, now=expiry + 1)
    assert drafts.postable([d], now=T0 + 60) == [] and drafts.postable([d], now=expiry + 1) == [d]

    still = drafts.claim_for_post(store, "p2", now=expiry - 1)
    assert still.drafts == [] and still.busy == 1
    taken = drafts.claim_for_post(store, "p3", now=expiry + 1)
    assert [d["id"] for d in taken.drafts] == ["a"] and taken.busy == 0
    assert store.load()["drafts"][0]["posting"] == "p3"


def test_a_stale_claim_on_an_outdated_draft_is_cleared(tmp_path):
    store = store_with(tmp_path, {"id": "old", "outdated": True, "posting": "died",
                                  "posting_at": datetime.fromtimestamp(T0, timezone.utc).isoformat()})
    claim = drafts.claim_for_post(store, "p2", now=T0 + drafts.CLAIM_EXPIRY_S + 1)
    assert claim.drafts == [] and claim.outdated == 1
    assert "posting" not in store.load()["drafts"][0]


# ---- both post paths -------------------------------------------------------------------


def chat_post(app, body: str = "From the chat", **args):
    return chat_tools.call_tool(app.pr_dir, Config(), "post_review", {"body": body, "include_pending": True, **args})


def pause_first_post(app, monkeypatch) -> SimpleNamespace:
    """A fake github.submit: the first post waits at GitHub until `go` is set; later ones go straight through."""
    gate = SimpleNamespace(entered=threading.Event(), go=threading.Event())

    def fake(owner, repo, number, payload):
        app.sent.append(payload)
        n = len(app.sent)
        if n == 1:
            gate.entered.set()
            assert gate.go.wait(10), "the test never let the first post finish"
        return {"id": n, "html_url": f"https://github.com/o/r/pull/1#review-{n}"}

    monkeypatch.setattr(github, "submit", fake)
    return gate


def sent_bodies(app) -> list[str]:
    return sorted(c["body"] for p in app.sent for c in p["comments"])


def test_app_and_chat_posting_at_once_send_each_comment_once(app, monkeypatch):
    first, second = add_draft(app, 22, "first"), add_draft(app, 5, "second")
    assert first["anchored"] and second["anchored"]
    gate = pause_first_post(app, monkeypatch)
    out = {}
    app_post = threading.Thread(target=lambda: out.setdefault("app", submit(app, "From the app")))
    app_post.start()
    assert gate.entered.wait(10)                               # the app's Submit is at GitHub with both comments

    text, err = chat_post(app)                                 # the chat posts meanwhile: only its summary goes
    assert not err and "0 line comments" in text and "2 pending comments were left out" in text
    gate.go.set()
    app_post.join(10)

    assert out["app"].status_code == 200, out["app"].text
    assert sent_bodies(app) == ["first", "second"]
    assert [p["body"] for p in app.sent] == ["From the app", "From the chat"]
    s = state(app)
    assert s["drafts"] == []
    assert sorted((r.get("source", "app"), r["comments"]) for r in s["submitted"]) == [("app", 2), ("chat", 0)]


def test_submit_while_the_chat_is_posting_says_nothing_new(app, monkeypatch):
    add_draft(app, 22, "first")
    gate = pause_first_post(app, monkeypatch)
    out = {}
    chat = threading.Thread(target=lambda: out.setdefault("chat", chat_post(app)))
    chat.start()
    assert gate.entered.wait(10)

    r = submit(app, "From the app")
    assert r.status_code == 409 and r.json()["detail"].startswith("Nothing new to post")
    gate.go.set()
    chat.join(10)

    text, err = out["chat"]
    assert not err and "1 line comments" in text
    assert len(app.sent) == 1 and sent_bodies(app) == ["first"]
    assert state(app)["drafts"] == [] and [r["source"] for r in state(app)["submitted"]] == ["chat"]


def test_a_draft_added_while_the_chat_posts_is_kept(app, monkeypatch):
    add_draft(app, 22, "sent with the review")

    def slow(owner, repo, number, payload):
        app.sent.append(payload)
        add_draft(app, 5, "added while posting")               # the reviewer adds one in the app meanwhile
        return {"id": 3, "html_url": "https://github.com/o/r/pull/1#review-3"}

    monkeypatch.setattr(github, "submit", slow)
    text, err = chat_post(app)
    assert not err and "1 line comments" in text
    assert sent_bodies(app) == ["sent with the review"]
    assert [d["body"] for d in state(app)["drafts"]] == ["added while posting"]
    assert "posting" not in state(app)["drafts"][0]


def test_a_failed_chat_post_keeps_the_drafts(app, monkeypatch):
    def reject(owner, repo, number, payload):
        raise github.ReviewError("GitHub rejected the review: Validation Failed")

    monkeypatch.setattr(github, "submit", reject)
    before = [add_draft(app, 22, "first"), add_draft(app, 15, "second")]
    text, err = chat_post(app)
    assert err and text == "GitHub rejected the review: Validation Failed"
    assert state(app)["drafts"] == before and state(app)["submitted"] == []


def test_any_failure_releases_the_claims(app, monkeypatch):
    def crash(owner, repo, number, payload):
        raise RuntimeError("network down")

    monkeypatch.setattr(github, "submit", crash)
    before = [add_draft(app, 22, "first")]
    with pytest.raises(RuntimeError):
        submit(app, "Notes")
    assert state(app)["drafts"] == before
    with pytest.raises(RuntimeError):
        chat_post(app)
    assert state(app)["drafts"] == before and state(app)["submitted"] == []


def test_drafts_being_posted_cant_be_edited_or_deleted(app):
    d = add_draft(app, 22, "first")
    store = StateStore(app.pr_dir)
    drafts.claim_for_post(store, "p1")
    edit = app.client.patch(f"/api/drafts/{d['id']}", json={"body": "changed"}, headers=ORIGIN)
    assert edit.status_code == 409 and "being posted" in edit.json()["detail"]
    gone = app.client.delete(f"/api/drafts/{d['id']}", headers=ORIGIN)
    assert gone.status_code == 409 and "being posted" in gone.json()["detail"]
    assert state(app)["drafts"][0]["body"] == "first"

    drafts.release(store, "p1")
    assert app.client.patch(f"/api/drafts/{d['id']}", json={"body": "changed"}, headers=ORIGIN).json()["body"] == "changed"
    assert app.client.delete(f"/api/drafts/{d['id']}", headers=ORIGIN).json() == {"ok": True}
    assert state(app)["drafts"] == []


def test_submit_takes_back_a_stale_claim(app):
    d = add_draft(app, 22, "left behind by a post that died")
    old = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - drafts.CLAIM_EXPIRY_S - 60, timezone.utc)
    StateStore(app.pr_dir).update(lambda s: s["drafts"][0].update(posting="died", posting_at=old.isoformat()))
    edit = app.client.patch(f"/api/drafts/{d['id']}", json={"body": "edited after the crash"}, headers=ORIGIN)
    assert edit.status_code == 200

    assert submit(app, "Notes").status_code == 200
    assert sent_bodies(app) == ["edited after the crash"]
    assert state(app)["drafts"] == []


def test_outdated_drafts_are_not_sent_and_stay_pending(app):
    add_draft(app, 22, "current")
    old = add_draft(app, 5, "from an older commit")
    StateStore(app.pr_dir).update(lambda s: s["drafts"][1].update(outdated=True))

    preview = app.client.post("/api/review/preview", json={"body": "Notes", "event": "COMMENT"}, headers=ORIGIN).json()
    assert [c["body"] for c in preview["payload"]["comments"]] == ["current"]
    assert "older commit" not in preview["payload"]["body"] and preview["unanchored"] == []

    assert submit(app, "Notes").status_code == 200
    assert sent_bodies(app) == ["current"] and "older commit" not in app.sent[0]["body"]
    assert state(app)["drafts"] == [{**old, "outdated": True}]

    text, err = chat_post(app)                                 # the chat says why it didn't send it
    assert not err and "0 line comments" in text and "1 pending comments are from an older commit" in text
    assert state(app)["drafts"] == [{**old, "outdated": True}]


def test_github_posts_time_out_after_two_minutes(monkeypatch):
    monkeypatch.delenv("PRR_GITHUB_DRY_RUN", raising=False)
    calls = []

    def hung_gh(*args, **kwargs):                              # stands in for `gh`; nothing is run
        calls.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(["gh", *args], kwargs.get("timeout"))

    monkeypatch.setattr(github, "gh", hung_gh)
    with pytest.raises(github.ReviewError, match="didn't answer within 120 seconds"):
        github.submit("o", "r", 1, {"body": "x"})
    with pytest.raises(github.ReviewError, match="the comment may or may not be on the PR"):
        github.post_comment("o", "r", 1, {"body": "x"})
    assert calls == [120, 120] and github.POST_TIMEOUT_S < drafts.CLAIM_EXPIRY_S
