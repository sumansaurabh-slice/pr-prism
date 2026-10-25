"""Thin argv-only wrappers around git, gh and claude. Nothing here goes through a shell."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class CommandError(RuntimeError):
    def __init__(self, argv: list[str], code: int, stdout: str, stderr: str):
        self.argv, self.code, self.stdout, self.stderr = argv, code, stdout, stderr
        msg = (stderr or stdout).strip().splitlines()
        super().__init__(f"`{' '.join(argv[:4])}…` exited {code}: {msg[-1] if msg else ''}")


def run(argv: list[str], *, cwd: Path | str | None = None, input: str | None = None,
        check: bool = True, env: dict[str, str] | None = None, timeout: float | None = None) -> str:
    # Bytes in, decode ourselves: text=True would apply universal newlines and turn CRLF diff lines into LF.
    proc = subprocess.run(argv, cwd=cwd, input=input.encode() if input is not None else None,
                          capture_output=True, env=env, timeout=timeout)
    stdout = proc.stdout.decode("utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise CommandError(argv, proc.returncode, stdout, proc.stderr.decode("utf-8", errors="replace"))
    return stdout


def git(*args: str, cwd: Path | str, check: bool = True, input: str | None = None) -> str:
    return run(["git", "-c", "core.quotePath=false", *args], cwd=cwd, check=check, input=input)


def git_ok(*args: str, cwd: Path | str) -> bool:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True).returncode == 0


def gh(*args: str, cwd: Path | str | None = None, input: str | None = None, check: bool = True,
       timeout: float | None = None) -> str:
    return run(["gh", *args], cwd=cwd, input=input, check=check, env=gh_env(), timeout=timeout)


def gh_json(*args: str, cwd: Path | str | None = None, input: str | None = None) -> Any:
    out = gh(*args, cwd=cwd, input=input)
    return json.loads(out) if out.strip() else None


def gh_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GH_PROMPT_DISABLED"] = "1"
    env["NO_COLOR"] = "1"
    return env


# Variables Claude Code sets for its own child processes. A nested `claude -p` that inherits them
# thinks it is part of the parent session, so they are dropped. Auth/provider variables are kept.
_CLAUDE_SESSION_VARS = re.compile(
    r"^(CLAUDECODE|CLAUDE_PID|CLAUDE_EFFORT|CLAUDE_CODE_(ENTRYPOINT|SESSION_.*|CHILD_SESSION|MESSAGING_.*|EXECPATH|SSE_PORT))$"
)


def claude_env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _CLAUDE_SESSION_VARS.match(k)}


# --------------------------------------------------------------------------------------
# Repository / PR resolution
# --------------------------------------------------------------------------------------

_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/(\d+)")
_REMOTE_URL = re.compile(r"github\.com[:/]+([^/]+)/(.+?)(?:\.git)?/?$")


@dataclass
class PRRef:
    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def repo_root(cwd: Path | str = ".") -> Path:
    return Path(git("rev-parse", "--show-toplevel", cwd=cwd).strip())


def default_repo(cwd: Path | str) -> tuple[str, str]:
    """The GitHub repo PRs belong to. Respects `gh repo set-default`, so forks resolve to upstream."""
    data = gh_json("repo", "view", "--json", "owner,name", cwd=cwd)
    return data["owner"]["login"], data["name"]


def parse_pr_arg(arg: str, cwd: Path | str) -> PRRef:
    arg = arg.strip()
    m = _PR_URL.search(arg)
    if m:
        return PRRef(m.group(1), m.group(2), int(m.group(3)))
    num = arg.lstrip("#")
    if not num.isdigit():
        raise ValueError(f"Expected a PR number or URL, got {arg!r}")
    owner, repo = default_repo(cwd)
    return PRRef(owner, repo, int(num))


def find_remote(root: Path, ref: PRRef) -> str:
    """Name of a git remote pointing at owner/repo, or the repo's https URL if none does."""
    for line in git("remote", "-v", cwd=root).splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        m = _REMOTE_URL.search(parts[1])
        if m and m.group(1).lower() == ref.owner.lower() and m.group(2).lower() == ref.repo.lower():
            return parts[0]
    return f"https://github.com/{ref.slug}.git"


def viewer_login() -> str:
    return gh("api", "user", "--jq", ".login").strip()
