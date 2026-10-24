"""`prr clean`: what it removes and what it leaves alone. Uses a temp cache and a temp git repo only."""

import sys
from pathlib import Path

import pytest

from prr import cli, gitgh
from prr.config import Config
from prr.util import write_json
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
