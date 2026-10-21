import os
import subprocess
from pathlib import Path

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
