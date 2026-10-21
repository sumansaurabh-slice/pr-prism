import pytest
from fastapi.testclient import TestClient

from prr.config import Config
from prr.server import Guard, create_app
from tests.test_merge import make_pr_dir

PORT = 40123
TOKEN = "secret-token"


@pytest.fixture()
def client(tmp_path, tricky_repo):
    make_pr_dir(tmp_path, tricky_repo["diff"])
    (tmp_path / "diff.patch").write_text(tricky_repo["diff"])
    app = Guard(create_app(tmp_path, Config()), TOKEN, PORT, on_activity=lambda: None)
    return TestClient(app, base_url=f"http://127.0.0.1:{PORT}")


def test_requires_cookie(client):
    assert client.get("/api/health").status_code == 401


def test_token_login_sets_cookie_and_redirects(client):
    r = client.get(f"/?t={TOKEN}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=Strict" in r.headers["set-cookie"]
    assert client.get("/api/health").json()["ok"] is True


def test_wrong_token_rejected(client):
    assert client.get("/?t=nope", follow_redirects=False).status_code == 401


def test_bad_host_rejected(client):
    client.cookies.set("prr_session", TOKEN)
    assert client.get("/api/health", headers={"host": "evil.example:80"}).status_code == 403


def test_post_needs_same_origin(client):
    client.cookies.set("prr_session", TOKEN)
    body = {"path": "src/core.py", "reviewed": True}
    assert client.put("/api/reviewed", json=body).status_code == 403
    assert client.put("/api/reviewed", json=body, headers={"origin": "http://evil.example"}).status_code == 403
    r = client.put("/api/reviewed", json=body, headers={"origin": f"http://127.0.0.1:{PORT}"})
    assert r.status_code == 200 and "src/core.py" in r.json()["reviewed"]


def test_security_headers_and_diff(client):
    client.cookies.set("prr_session", TOKEN)
    r = client.get("/api/diff", params={"path": "src/core.py"})
    assert r.status_code == 200 and r.json()["patch"].startswith("diff --git a/src/core.py")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert client.get("/api/diff", params={"path": "../../etc/passwd"}).status_code == 404


def test_drafts_anchor_validation(client):
    client.cookies.set("prr_session", TOKEN)
    h = {"origin": f"http://127.0.0.1:{PORT}"}
    ok = client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 22, "start_line": 21, "body": "x"}, headers=h).json()
    out = client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 15, "body": "y"}, headers=h).json()
    assert ok["anchored"] is True and out["anchored"] is False
    assert client.post("/api/drafts", json={"path": "nope", "side": "RIGHT", "line": 1, "body": "z"}, headers=h).status_code == 400
    assert len(client.get("/api/state").json()["drafts"]) == 2


def test_multi_line_drafts(client):
    client.cookies.set("prr_session", TOKEN)
    h = {"origin": f"http://127.0.0.1:{PORT}"}
    same = client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 8, "start_line": 6, "body": "range"}, headers=h).json()
    assert (same["start_line"], same["line"], same["start_side"], same["anchored"]) == (6, 8, None, True)
    mixed = client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 6, "start_line": 5,
                                             "start_side": "LEFT", "body": "red to green"}, headers=h).json()
    assert (mixed["start_line"], mixed["line"], mixed["start_side"], mixed["anchored"]) == (5, 6, "LEFT", True)
    single = client.post("/api/drafts", json={"path": "src/core.py", "side": "RIGHT", "line": 8, "start_line": 8, "body": "one"}, headers=h).json()
    assert single["start_line"] is None and single["start_side"] is None
