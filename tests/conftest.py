import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from prr.fetch import DIFF_ARGS


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, env=env, check=True,
                          capture_output=True).stdout.decode()


@pytest.fixture()
def tricky_repo(tmp_path: Path) -> dict:
    """A repo whose base→head diff has every awkward case: rename+edit, delete, binary, mode change,
    no trailing newline, CRLF, spaces and unicode in names."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "src").mkdir()
    (r / "src/core.py").write_text("".join(f"line {i}\n" for i in range(1, 41)))
    (r / "src/old_name.py").write_text("".join(f"keep {i}\n" for i in range(1, 21)))
    (r / "gone.txt").write_text("bye\n")
    (r / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)))
    (r / "run.sh").write_text("echo hi\n")
    (r / "noeol.txt").write_text("a\nb")
    (r / "crlf.txt").write_bytes(b"one\r\ntwo\r\nthree\r\n")
    (r / "with space.md").write_text("hello\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "base")
    base = _git(r, "rev-parse", "HEAD").strip()

    core = [f"line {i}\n" for i in range(1, 41)]
    core[4] = "line 5 changed\n"
    core.insert(20, "inserted A\n")
    core.insert(21, "inserted B\n")
    del core[35]
    (r / "src/core.py").write_text("".join(core))
    _git(r, "mv", "src/old_name.py", "src/new_name.py")
    renamed = [f"keep {i}\n" for i in range(1, 21)]
    renamed[9] = "keep 10 edited\n"
    (r / "src/new_name.py").write_text("".join(renamed))
    _git(r, "rm", "-q", "gone.txt")
    (r / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(255, -1, -1)))
    os.chmod(r / "run.sh", 0o755)
    (r / "noeol.txt").write_text("a\nB")
    (r / "crlf.txt").write_bytes(b"one\r\nTWO\r\nthree\r\n")
    (r / "with space.md").write_text("hello\nworld\n")
    (r / "café.txt").write_text("new\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "head")
    head = _git(r, "rev-parse", "HEAD").strip()
    diff = _git(r, *DIFF_ARGS, base, head)
    return {"root": r, "base": base, "head": head, "diff": diff}


class ReviewStub:
    """A stand-in for the review server's internal endpoints, on a real port (hooks run in other processes).
    It records every approval request and answers with `decision` at once; it keeps each thread's turn mark."""

    def __init__(self, pr_dir: Path):
        stub = self
        self.approvals: list[dict] = []
        self.taints: list[dict] = []
        self.causes: dict[str, list[dict]] = {}
        self.decision = "deny"

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _reply(self, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                causes = stub.causes.get(parse_qs(urlparse(self.path).query).get("thread", [""])[0], [])
                self._reply({"marked": bool(causes), "causes": causes})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/internal/taint":
                    stub.taints.append(body)
                    stub.causes.setdefault(body["thread_id"], []).append({"kind": body["kind"], "text": body["text"]})
                    self._reply({"marked": True})
                    return
                stub.approvals.append(body)
                if stub.decision in ("allow", "always"):  # like the server: an approval marks the turn
                    stub.causes.setdefault(body["thread_id"], []).append({"kind": "approved", "text": "x"})
                    self._reply({"decision": stub.decision})
                else:
                    self._reply({"decision": "deny", "message": "The reviewer said no."})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        (pr_dir / "server.json").write_text(json.dumps({"port": self.port, "token": "t", "url": "u"}))

    def new_turn(self, thread_id: str) -> None:
        self.causes.pop(thread_id, None)


@pytest.fixture()
def review_stub():
    """review_stub(pr_dir) → a ReviewStub whose port is in pr_dir/server.json."""
    stubs: list[ReviewStub] = []

    def start(pr_dir: Path) -> ReviewStub:
        stubs.append(ReviewStub(pr_dir))
        return stubs[-1]

    yield start
    for s in stubs:
        s.server.shutdown()
