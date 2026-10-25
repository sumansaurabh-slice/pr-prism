"""`prr fetch`: PR metadata, refs, merge-base, an isolated worktree at the PR head, the diff and context.json.

fetch() reads the PR from GitHub and fetches its head and base; prepare() then builds the review inputs (worktree,
diff.patch, context.json) for one commit from local git objects. `prr eval` uses prepare() too, on a pinned commit.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import anchors, archive, classify, diffparse, filekinds, graph
from .config import Config, pr_dir
from .gitgh import CommandError, PRRef, find_remote, gh_json, git, git_ok
from .util import now_iso, read_json, write_json, write_text

_PR_FIELDS = (
    "number,title,url,author,body,state,isDraft,baseRefName,headRefName,baseRefOid,headRefOid,"
    "additions,deletions,changedFiles,labels,commits"
)

# Never run hooks from the PR's own tree (core.hooksPath=.husky would resolve inside the worktree),
# never shell out to external diff/textconv drivers, and pin prefixes/algorithm regardless of user config.
_SAFE = ["-c", "core.hooksPath=/dev/null"]
_NO_LINKS = ["-c", "core.symlinks=false"]
DIFF_ARGS = [
    "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--full-index", "-M", "-U3",
    "--diff-algorithm=myers", "--src-prefix=a/", "--dst-prefix=b/", "--no-relative",
]


def _log(msg: str) -> None:
    print(f"[prr] {msg}", file=sys.stderr, flush=True)


def _merge_base(root: Path, remote: str, ref: PRRef, base_ref: str, head_sha: str) -> str:
    try:
        return git("merge-base", base_ref, head_sha, cwd=root).strip()
    except CommandError:
        # Shallow clone or unrelated history: ask GitHub, then fetch that one commit.
        data = gh_json("api", f"repos/{ref.slug}/compare/{base_ref.rsplit('/', 1)[-1]}...{head_sha}", "--jq", "{sha: .merge_base_commit.sha}")
        sha = data["sha"]
        git("fetch", "--no-tags", "--quiet", "--depth=1", remote, sha, cwd=root)
        return sha


def _has_links(wt: Path) -> bool:
    for folder, dirs, files in os.walk(wt):
        if any(os.path.islink(os.path.join(folder, name)) for name in dirs + files):
            return True
        dirs[:] = [d for d in dirs if d != ".git"]
    return False


def _ensure_worktree(root: Path, wt: Path, sha: str) -> None:
    """Check out the PR head. Symbolic links come out as small files holding their target (core.symlinks=false), so
    a link in the PR (to ~/.ssh, say) can't make a read inside the worktree land outside it. A worktree made before
    this, which may still hold links, is made again."""
    os.environ["GIT_LFS_SKIP_SMUDGE"] = "1"  # LFS blobs aren't needed to review code; skip the download
    if (wt / ".git").exists():
        try:
            git(*_SAFE, *_NO_LINKS, "checkout", "--quiet", "--detach", "--force", sha, cwd=wt)
            if not _has_links(wt):
                return
            _log("The worktree still has symbolic links from an older checkout; making it again")
        except CommandError:
            pass
        git("worktree", "remove", "--force", str(wt), cwd=root, check=False)
        shutil.rmtree(wt, ignore_errors=True)  # rmtree removes links themselves, never what they point to
    git("worktree", "prune", cwd=root, check=False)
    git(*_SAFE, *_NO_LINKS, "worktree", "add", "--quiet", "--detach", "--force", str(wt), sha, cwd=root)


def _author_association(ref: PRRef) -> str | None:
    """OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, NONE… from GitHub's REST API, read-only (`gh pr view --json` has no
    such field). None when the call fails: the author then counts as an outsider until the next fetch retries."""
    try:
        data = gh_json("api", f"repos/{ref.slug}/pulls/{ref.number}", "--jq", "{association: .author_association}")
    except Exception as e:  # noqa: BLE001 — never fail the fetch over this; lookups just ask first
        _log(f"warning: couldn't check the PR author ({str(e)[:200]}); Jira, Slack and other-repo lookups ask first")
        return None
    value = data.get("association") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value else None


def read_meta(ref: PRRef) -> dict:
    """The PR as `gh pr view --json` gives it (read-only)."""
    return gh_json("pr", "view", str(ref.number), "--repo", ref.slug, "--json", _PR_FIELDS)


def fetch(ref: PRRef, root: Path, cfg: Config) -> Path:
    d = pr_dir(cfg, ref.owner, ref.repo, ref.number)
    fresh = not (d / "state.json").exists()  # e.g. after `prr clean`: the saved work is restored below
    d.mkdir(parents=True, exist_ok=True)
    previous = read_json(d / "context.json")

    _log(f"Reading PR {ref.slug}#{ref.number} from GitHub")
    meta = read_meta(ref)
    association = _author_association(ref)  # an outsider's PR: connector lookups ask first (prr/policy.py)
    remote = find_remote(root, ref)
    head_ref = f"refs/pr-prism/{ref.number}/head"
    base_ref = f"refs/pr-prism/{ref.number}/base"
    _log(f"Fetching PR head and base ({meta['baseRefName']}) from {remote}")
    git("fetch", "--no-tags", "--quiet", "--force", remote,
        f"+refs/pull/{ref.number}/head:{head_ref}", f"+refs/heads/{meta['baseRefName']}:{base_ref}", cwd=root)
    head_sha = git("rev-parse", head_ref, cwd=root).strip()
    warnings: list[str] = []
    if head_sha != meta["headRefOid"]:
        warnings.append(f"PR head moved while fetching ({meta['headRefOid'][:10]} → {head_sha[:10]}); using {head_sha[:10]}")
    base_point = base_ref
    if meta["state"] != "OPEN":
        # A merged PR's head is already in the base branch, so diff against the base commit GitHub recorded.
        base_point = meta["baseRefOid"]
        if not git_ok("cat-file", "-e", f"{base_point}^{{commit}}", cwd=root):
            git("fetch", "--no-tags", "--quiet", remote, base_point, cwd=root)
    merge_base = _merge_base(root, remote, ref, base_point, head_sha)

    _, files = prepare(ref, head_sha, merge_base, meta, root=root, d=d, association=association, warnings=warnings)
    if previous and previous["pr"]["head_sha"] != head_sha:
        moved = anchors.remap_state(d, root, previous["pr"]["head_sha"], head_sha, files)
        if moved:
            _log(f"PR moved since last fetch: remapped {moved['remapped']} drafts, {moved['outdated']} now outdated")
    if fresh:
        restored = archive.restore(cfg, ref.owner, ref.repo, ref.number, d, head_sha)
        if restored:
            _log(archive.message(restored))
    for w in warnings:
        _log(f"warning: {w}")
    return d


def prepare(ref: PRRef, head_sha: str, merge_base_sha: str, meta: dict, *, root: Path, d: Path,
            association: str | None = None, warnings: list[str] | None = None) -> tuple[dict, list[diffparse.FileDiff]]:
    """The review inputs for one commit of a PR, all in folder `d`: an isolated worktree at `head_sha`, diff.patch
    (from `merge_base_sha`) and context.json. Both commits must already be in the repo at `root`. `meta` is the PR as
    `gh pr view --json` gives it; GitHub's +/- counts are compared only when it has them (eval drops them for an older
    commit). Appends to `warnings`. Shared by fetch() (the PR's head) and `prr eval` (a pinned commit).
    Returns (the context, the parsed diff)."""
    warnings = warnings if warnings is not None else []
    wt = d / "worktree"
    _log(f"Checking out PR head {head_sha[:10]} into an isolated worktree")
    _ensure_worktree(root, wt, head_sha)

    diff_text = git(*DIFF_ARGS, merge_base_sha, head_sha, cwd=root)
    write_text(d / "diff.patch", diff_text)
    files = diffparse.parse(diff_text)

    adds, dels = sum(f.additions for f in files), sum(f.deletions for f in files)
    github_counts = (meta.get("additions"), meta.get("deletions"), meta.get("changedFiles"))
    if all(isinstance(n, int) for n in github_counts) and (adds, dels, len(files)) != github_counts:
        warnings.append(
            f"Local diff (+{adds}/-{dels}, {len(files)} files) differs from GitHub "
            f"(+{meta['additions']}/-{meta['deletions']}, {meta['changedFiles']} files); "
            "usually a base branch that moved since GitHub last computed the PR"
        )

    cats = {f.path: classify.category(f) for f in files}
    pairs = classify.pair_tests(cats)
    tested_by: dict[str, list[str]] = {}
    for t, s in pairs.items():
        tested_by.setdefault(s, []).append(t)
    live = [f.path for f in files if f.status != "deleted" and not f.binary and cats[f.path] != "generated"]
    edges = graph.import_edges(live, wt)
    dep_rank = {p: i for i, p in enumerate(graph.dependency_order(live, edges))}
    rank = {c: i for i, c in enumerate(classify.CATEGORY_ORDER)}
    order = sorted((f.path for f in files), key=lambda p: (rank[cats[p]], dep_rank.get(p, 1 << 30), p))

    ctx = {
        "fetched_at": now_iso(),
        "repo_root": str(root),
        "worktree": str(wt),
        "pr": {
            "owner": ref.owner, "repo": ref.repo, "number": ref.number,
            "title": meta["title"], "url": meta["url"], "author": (meta.get("author") or {}).get("login", ""),
            "author_association": association,
            "body": meta.get("body") or "", "state": meta["state"], "is_draft": meta["isDraft"],
            "base_ref": meta["baseRefName"], "head_ref": meta["headRefName"],
            "base_sha": meta["baseRefOid"], "merge_base_sha": merge_base_sha, "head_sha": head_sha,
            "additions": adds, "deletions": dels, "changed_files": len(files),
            "labels": [lb["name"] for lb in meta.get("labels") or []],
            "commits": [
                {"sha": c["oid"], "headline": c.get("messageHeadline", ""),
                 "author": ((c.get("authors") or [{}])[0].get("login") or (c.get("authors") or [{}])[0].get("name") or "")}
                for c in meta.get("commits") or []
            ],
        },
        "files": [
            {
                "path": f.path, "old_path": f.old_path, "status": f.status,
                "additions": f.additions, "deletions": f.deletions, "binary": f.binary,
                "category": cats[f.path], "generated": cats[f.path] == "generated",
                "sensitive": classify.is_sensitive(f.path), "minutes": classify.minutes(f, cats[f.path]),
                "base_blob": f.base_blob, "head_blob": f.head_blob,
                "tests_for": pairs.get(f.path), "tested_by": sorted(tested_by.get(f.path, [])),
                "imports": edges.get(f.path, []),
                "hunks": f.hunk_infos(),
            }
            for f in files
        ],
        "order": order,
        "test_roots": filekinds.test_roots(wt),  # test folders the PR's own pytest config names
        "warnings": warnings,
    }
    write_json(d / "context.json", ctx)
    return ctx, files


def remove_worktree(root: Path, d: Path, number: int) -> None:
    wt = d / "worktree"
    if wt.exists():
        git("worktree", "remove", "--force", str(wt), cwd=root, check=False)
    git("worktree", "prune", cwd=root, check=False)
    for name in ("head", "base"):
        git("update-ref", "-d", f"refs/pr-prism/{number}/{name}", cwd=root, check=False)
