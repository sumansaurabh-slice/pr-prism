"""fetch: the PR author's association (E1b) and a worktree without symbolic links (E1)."""

import os

from prr import fetch, gitgh, policy
from prr.config import Config
from prr.gitgh import PRRef
from prr.models import PRInfo
from prr.util import read_json
from tests.conftest import _git


def fetch_with(tmp_path, tricky_repo, monkeypatch, association):
    root = tricky_repo["root"]
    _git(root, "update-ref", "refs/pull/1/head", tricky_repo["head"])
    meta = {"number": 1, "title": "t", "url": "u", "author": {"login": "alice"}, "body": "", "state": "OPEN",
            "isDraft": False, "baseRefName": "main", "headRefName": "f", "baseRefOid": tricky_repo["base"],
            "headRefOid": tricky_repo["head"], "additions": 0, "deletions": 0, "changedFiles": 0, "labels": [],
            "commits": []}
    calls = []

    def fake_gh_json(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("pr", "view"):
            return meta
        if args[0] == "api" and association is not None:
            return {"association": association}
        raise gitgh.CommandError(["gh", *args], 1, "", "offline in tests")

    monkeypatch.setattr(fetch, "gh_json", fake_gh_json)
    monkeypatch.setattr(fetch, "find_remote", lambda root, ref: str(root))
    d = fetch.fetch(PRRef("o", "r", 1), root, Config(cache_dir=tmp_path / "cache"))
    return d, calls


def test_the_author_association_is_read_with_gh_api(tmp_path, tricky_repo, monkeypatch):
    d, calls = fetch_with(tmp_path, tricky_repo, monkeypatch, "CONTRIBUTOR")
    # a read-only REST call: no -X, no -f fields
    assert ("api", "repos/o/r/pulls/1", "--jq", "{association: .author_association}") in calls
    pr = read_json(d / "context.json")["pr"]
    assert pr["author_association"] == "CONTRIBUTOR" and PRInfo.model_validate(pr).author_association == "CONTRIBUTOR"
    assert policy.trust(d)["state"] == "outside"


def test_a_failed_author_check_counts_as_an_outsider(tmp_path, tricky_repo, monkeypatch, capsys):
    d, _ = fetch_with(tmp_path, tricky_repo, monkeypatch, None)
    assert read_json(d / "context.json")["pr"]["author_association"] is None
    assert policy.trust(d) == {"author": "alice", "association": None, "trusted": False, "state": "unknown"}
    assert "lookups ask first" in capsys.readouterr().err


def _repo_with_a_link(tmp_path):
    repo = tmp_path / "linked"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.txt").write_text("a\n")
    os.symlink("../../.ssh/id_rsa", repo / "key")
    os.symlink("/etc", repo / "etc")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "links")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def test_symbolic_links_become_plain_files(tmp_path):
    repo, sha = _repo_with_a_link(tmp_path)
    wt = tmp_path / "wt"
    fetch._ensure_worktree(repo, wt, sha)
    assert not (wt / "key").is_symlink() and (wt / "key").read_text() == "../../.ssh/id_rsa"
    assert not (wt / "etc").is_symlink() and not fetch._has_links(wt)
    fetch._ensure_worktree(repo, wt, sha)      # checking out again keeps it that way
    assert not fetch._has_links(wt)


def test_an_older_worktree_with_links_is_made_again(tmp_path):
    repo, sha = _repo_with_a_link(tmp_path)
    wt = tmp_path / "old"
    _git(repo, "worktree", "add", "-q", "--detach", str(wt), sha)       # made before this fix: real links
    assert (wt / "key").is_symlink()
    fetch._ensure_worktree(repo, wt, sha)
    assert not fetch._has_links(wt) and (wt / "a.txt").read_text() == "a\n"


# ---- prepare: the review inputs for one commit, shared by fetch and eval -------------------------


def test_fetch_builds_its_inputs_with_prepare(tmp_path, tricky_repo, monkeypatch):
    seen = []
    real = fetch.prepare

    def spy(ref, head_sha, merge_base_sha, meta, **kw):
        seen.append((head_sha, merge_base_sha, kw["d"]))
        return real(ref, head_sha, merge_base_sha, meta, **kw)

    monkeypatch.setattr(fetch, "prepare", spy)
    d, _ = fetch_with(tmp_path, tricky_repo, monkeypatch, "MEMBER")
    pr = read_json(d / "context.json")["pr"]
    assert seen == [(tricky_repo["head"], pr["merge_base_sha"], d)] and pr["head_sha"] == tricky_repo["head"]


def test_prepare_builds_a_pinned_commit(tmp_path, tricky_repo):
    meta = {"number": 1, "title": "t", "url": "u", "author": {"login": "alice"}, "body": "", "state": "MERGED",
            "isDraft": False, "baseRefName": "main", "headRefName": "f", "baseRefOid": tricky_repo["base"],
            "headRefOid": "a-later-head", "additions": None, "deletions": None, "changedFiles": None,
            "labels": [], "commits": []}                    # GitHub's counts are for a later head: not compared
    d = tmp_path / "pinned"
    ctx, files = fetch.prepare(PRRef("o", "r", 1), tricky_repo["head"], tricky_repo["base"], meta,
                               root=tricky_repo["root"], d=d)
    assert (ctx["pr"]["head_sha"], ctx["pr"]["merge_base_sha"]) == (tricky_repo["head"], tricky_repo["base"])
    assert (d / "diff.patch").read_bytes().decode() == tricky_repo["diff"]
    assert read_json(d / "context.json") == ctx and ctx["warnings"] == [] and ctx["worktree"] == str(d / "worktree")
    assert (d / "worktree" / "src" / "new_name.py").exists() and len(files) == len(ctx["files"])
