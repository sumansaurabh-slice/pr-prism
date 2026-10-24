"""Posting a review: the app's Submit. Nothing here reaches GitHub (PRR_GITHUB_DRY_RUN=1, or a fake `github.submit`)."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from prr import chat_tools, github, server
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


@pytest.mark.xfail(strict=True, reason="today's Submit clears every draft, even one added while it posts (D15 fixes this)")
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
