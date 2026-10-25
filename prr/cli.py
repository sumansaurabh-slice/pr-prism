"""`prr` command line: doctor | fetch | serve | analyze | status | stop | clean | labels import | eval.

Logs go to stderr; the machine-readable result of each command goes to stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path

from . import cost, supervise
from .config import SKILL_DIR, WEB_DIST, Config, load_config, pr_dir
from .gitgh import CommandError, PRRef, git, parse_pr_arg, repo_root
from .util import read_json


def _err(msg: str) -> None:
    print(f"[prr] {msg}", file=sys.stderr, flush=True)


def _resolve(args: argparse.Namespace, cfg: Config) -> tuple[PRRef, Path, Path]:
    root = repo_root(args.repo or ".")
    ref = parse_pr_arg(args.pr, root)
    return ref, root, pr_dir(cfg, ref.owner, ref.repo, ref.number)


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def broad_allow_rules(settings: Path) -> list[str]:
    """Allow rules in your Claude Code settings that skip the question for every web fetch, every command, or all of
    an MCP server's tools. The chat loads your settings (`--setting-sources user`), so in a normal chat turn these
    run without a card; in a turn that already touched outside data, PR Prism asks anyway."""
    data = read_json(settings)
    perms = data.get("permissions") if isinstance(data, dict) else None
    rules = perms.get("allow") if isinstance(perms, dict) else None
    return [r for r in (rules if isinstance(rules, list) else []) if isinstance(r, str) and (
        r in ("*", "WebFetch", "WebFetch(*)", "WebFetch(domain:*)", "WebSearch", "Bash", "Bash(*)", "Bash(:*)")
        or (r.startswith("mcp__") and (r.count("__") == 1 or r.endswith("__*"))))]


def cmd_doctor(args: argparse.Namespace, cfg: Config) -> int:
    ok = True

    def check(label: str, passed: bool, hint: str = "", required: bool = True) -> None:
        nonlocal ok
        mark = "✓" if passed else ("✗" if required else "!")
        print(f"{mark} {label}" + ("" if passed or not hint else f" ({hint})"))
        ok = ok and (passed or not required)

    check(f"python {sys.version.split()[0]}", sys.version_info >= (3, 11), "need 3.11+")
    try:
        import fastapi  # noqa: F401
        import pydantic  # noqa: F401
        import uvicorn  # noqa: F401
        check("python deps (fastapi, uvicorn, pydantic)", True)
    except ImportError as e:
        check("python deps", False, f"{e}; run ./install.sh")
    for tool, hint in (("git", "install git"), ("gh", "brew install gh"), ("claude", "install Claude Code")):
        check(f"{tool} on PATH", shutil.which(tool) is not None, hint)
    if shutil.which("gh"):
        authed = subprocess.run(["gh", "auth", "status"], capture_output=True).returncode == 0
        check("gh authenticated", authed, "run: gh auth login")
    check("web UI built", (WEB_DIST / "index.html").exists(), "run ./install.sh (needs Node)")
    link = Path("~/.claude/skills/pr-prism").expanduser()
    installed = link.exists() and link.resolve() == SKILL_DIR.resolve()
    check("skill installed at ~/.claude/skills/pr-prism", installed, "run ./install.sh", required=False)
    check("codegraph (optional, for blast radius)", shutil.which("codegraph") is not None, "optional", required=False)
    broad = broad_allow_rules(Path("~/.claude/settings.json").expanduser())
    check("Claude Code settings ask before web fetches, any command and MCP tools", not broad,
          f"~/.claude/settings.json allows {', '.join(broad)} without asking, so the chat does too, except in a turn "
          "that read outside data", required=False)
    check("the chat asks before actions", cfg.chat.ask_before_actions,
          "ask_before_actions = false: commands, edits and Jira/Slack actions run without asking. Reads outside the PR "
          "and commands that print the environment are still blocked, but a program the chat runs can read or send "
          "anything", required=False)
    try:
        root = repo_root(args.repo or ".")
        check(f"inside git repo {root}", True)
    except CommandError:
        check("inside a git repo", False, "cd into the repo the PR belongs to", required=False)
    return 0 if ok else 1


def cmd_fetch(args: argparse.Namespace, cfg: Config) -> int:
    from .fetch import fetch

    ref, root, _ = _resolve(args, cfg)
    d = fetch(ref, root, cfg)
    ctx = read_json(d / "context.json")
    pr = ctx["pr"]
    cats: dict[str, int] = {}
    for f in ctx["files"]:
        cats[f["category"]] = cats.get(f["category"], 0) + 1
    print(json.dumps({
        "pr_dir": str(d), "title": pr["title"], "url": pr["url"], "author": pr["author"],
        "head_sha": pr["head_sha"], "files": pr["changed_files"], "additions": pr["additions"],
        "deletions": pr["deletions"], "categories": cats, "warnings": ctx["warnings"],
    }, indent=1))
    return 0


def ensure_server(d: Path, open_browser: bool) -> str:
    url = supervise.ensure_running(d)
    if open_browser:
        webbrowser.open(url)
    return url


def cmd_serve(args: argparse.Namespace, cfg: Config) -> int:
    ref, root, d = _resolve(args, cfg)
    if not (d / "context.json").exists():
        from .fetch import fetch
        fetch(ref, root, cfg)
    url = ensure_server(d, open_browser=not args.no_open)
    print(url)
    return 0


def cmd_analyze(args: argparse.Namespace, cfg: Config) -> int:
    from .analyze import AnalyzeError, analyze

    _, _, d = _resolve(args, cfg)
    if args.concurrency:
        cfg.analysis.concurrency = args.concurrency
    try:
        a = analyze(d, cfg, focus=args.focus or None, force=args.force, echo=lambda m: print(f"• {m}", flush=True))
    except AnalyzeError as e:
        _err(str(e))
        return 1
    _print_summary(a, d)
    return 0 if a["status"] == "complete" else 1


def _print_summary(a: dict, d: Path) -> None:
    s = a["summary"] or {}
    info = read_json(d / "server.json") or {}
    print("\n=== Review ready ===")
    print(f"PR #{a['pr']['number']}: {a['pr']['title']}")
    if s:
        print(f"Risk: {s['risk'].upper()}: " + "; ".join(s["risk_reasons"][:3]))
        print(f"Summary: {s['tldr']}")
    print("Reading order:")
    for i, ch in enumerate(a["chapters"], 1):
        mins = sum(next((f["minutes"] for f in a["files"] if f["path"] == cf["path"]), 0) for cf in ch["files"])
        print(f"  {i}. {ch['title']} ({len(ch['files'])} files, ~{mins:.0f} min)")
    shown = [f for f in a["findings"] if not f["low_confidence"]]
    count = {s: sum(1 for f in shown if f["severity"] == s) for s in ("blocker", "major", "minor", "nit")}
    print(f"Findings: {count['blocker']} blocking merge, {count['major']} must fix, {count['minor']} should fix, "
          f"{count['nit']} could improve ({len(a['findings']) - len(shown)} more hidden by the second look).")
    failing = [c["name"] for c in (a.get("ci") or {}).get("checks", []) if c["state"] == "fail"]
    if failing:
        print(f"Failing CI checks: {', '.join(failing)}")
    if a.get("bot_findings"):
        print(f"Other review tools left {len(a['bot_findings'])} comments; Claude checked each one.")
    fixed = sum(1 for r in a.get("rechecked", []) if r["status"] == "fixed")
    if a.get("rechecked"):
        print(f"Earlier problems re-checked: {fixed} fixed, {len(a['rechecked']) - fixed} still open.")
    print("Nothing is posted until you submit.")
    if a["repair_log"]:
        print(f"({len(a['repair_log'])} small fixes were made to Claude's output: see repair_log in analysis.json)")
    if info.get("url"):
        print(f"Open: {info['url']}")


def cmd_status(args: argparse.Namespace, cfg: Config) -> int:
    base = cfg.cache_dir
    dirs = sorted(base.glob("*/pr-*")) if base.exists() else []
    if args.pr:
        _, _, d = _resolve(args, cfg)
        dirs = [d] if d.exists() else []
    rows = []
    for d in dirs:
        ctx = read_json(d / "context.json") or {}
        prog = read_json(d / "progress.json") or {}
        info = read_json(d / "server.json") or {}
        pr = ctx.get("pr", {})
        spent = cost.summary(d, cfg.budget.warn_usd)  # analysis + chat + other checks, from calls.jsonl
        rows.append({
            "pr": f"{pr.get('owner')}/{pr.get('repo')}#{pr.get('number')}", "title": pr.get("title"),
            "head": (pr.get("head_sha") or "")[:10], "analysis": prog.get("status", "not analyzed"),
            "spend": cost.label(spent) + (" (over your limit)" if spent["over_budget"] else ""),
            "spend_usd": spent["total_usd"],
            "server": info.get("url") if supervise.healthy(d) else None, "dir": str(d),
        })
    print(json.dumps(rows, indent=1))
    return 0


def cmd_stop(args: argparse.Namespace, cfg: Config) -> int:
    _, _, d = _resolve(args, cfg)
    print("stopped (the link stays the same next time)" if supervise.stop(d) else "not running")
    return 0


def _has_terminal() -> bool:
    """A person typing in a terminal, as opposed to an agent or a script running `prr`."""
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, OSError):  # stdin closed
        return False


def _remove_refs(root: Path, number: int) -> None:
    out = git("for-each-ref", "--format=%(refname)", f"refs/pr-prism/{number}/", cwd=root, check=False)
    for name in out.split():
        git("update-ref", "-d", name, cwd=root, check=False)


def cmd_clean(args: argparse.Namespace, cfg: Config) -> int:
    """Free a PR's disk space: stop its server, remove its worktree and folder.

    The reviewer's work (state.json and the cost ledger) is archived first and comes back on the next fetch;
    `--purge` deletes it too, with all of the PR's refs. A PR whose review page is running is skipped unless
    `--force`. `--all` from an agent or a script (no terminal) refuses unless `--yes`.
    """
    from . import archive
    from .fetch import remove_worktree

    root = repo_root(args.repo or ".")
    if args.all:
        from .gitgh import default_repo
        owner, repo = default_repo(root)
        base = cfg.cache_dir / f"{owner}__{repo}"
        targets = sorted(base.glob("pr-*"))
        if args.purge:  # also the saved work of PRs whose folders are already gone
            targets = sorted(set(targets) | {base / f"pr-{n}" for n in archive.archived_numbers(cfg, owner, repo)})
    else:
        if not args.pr:
            _err("give a PR number or --all")
            return 2
        ref, _, d = _resolve(args, cfg)
        owner, repo = ref.owner, ref.repo
        targets = [d]

    if args.all and not args.yes and not _has_terminal():
        print(f"Not cleaned: `prr clean --all` removes every review of {owner}/{repo} at once, "
              "so it only runs when typed in a terminal.")
        if targets:
            print("It would remove:")
            for d in targets:
                title = ((read_json(d / "context.json") or {}).get("pr") or {}).get("title") or ""
                note = " (saved work only)" if not d.exists() else ""
                note = " (its review page is running)" if d.exists() and supervise.running(d) else note
                print(f"  #{d.name.split('-', 1)[1]} {title}{note}".rstrip())
        else:
            print("There is nothing to remove.")
        print("To clean one review, run `prr clean <PR>`.")
        return 1

    failed = False
    for d in targets:
        number = int(d.name.split("-", 1)[1])
        if not d.exists():
            if args.purge:
                _remove_refs(root, number)
                for p in archive.purge(cfg, owner, repo, number):
                    print(f"removed {p}")
            continue
        if not args.force and supervise.running(d):
            print(f"Skipped #{number}: its review page is running (use --force)")
            continue
        supervise.stop(d)  # first, so the server can't change state.json after it is archived
        kept = None
        if not args.purge:
            try:
                kept = archive.save(cfg, owner, repo, number, d)
            except OSError as e:
                print(f"Not cleaned #{number}: couldn't archive state ({e.strerror or e})")
                failed = True
                continue
        remove_worktree(root, d, number)  # keeps refs/pr-prism/<n>/reviewed
        shutil.rmtree(d, ignore_errors=True)
        print(f"removed {d}")
        if args.purge:
            _remove_refs(root, number)
            for p in archive.purge(cfg, owner, repo, number):
                print(f"removed {p}")
            continue
        if kept:
            what = archive.describe(kept["counts"]) or "your review state"
            print(f"kept {what} in {kept['path']}" + (" with its cost log" if kept["ledger"] else ""))
            print(f"  it comes back the next time you open #{number}")
        if git("rev-parse", "--verify", "--quiet", f"refs/pr-prism/{number}/reviewed", cwd=root, check=False).strip():
            print(f"kept refs/pr-prism/{number}/reviewed (the commit you last reviewed)")
    return 1 if failed else 0


def cmd_labels(args: argparse.Namespace, cfg: Config) -> int:
    """`prr labels import <pr…>`: the review comments already on past PRs become eval labels. Only reads GitHub."""
    from . import labels
    from .gitgh import viewer_login

    root = repo_root(args.repo or ".")
    viewer = viewer_login() if args.reviewers == "me" else None
    rows, failed = [], False
    for arg in args.prs:
        try:
            ref = parse_pr_arg(arg, root)
            row = labels.import_comments(ref, root, labels.repo_dir(cfg.cache_dir, ref.owner, ref.repo),
                                         reviewers=args.reviewers, viewer=viewer, split=args.split)
        except (CommandError, ValueError, OSError, TimeoutError) as e:
            _err(f"PR {arg}: not imported: {e}")
            failed = True
            continue
        whose = "your" if args.reviewers == "me" else "reviewers'"
        _err(f"#{row['pr']}: {row['imported']} of {whose} comments added as {row['split']} labels"
             + (f", {row['already_imported']} already there" if row["already_imported"] else "")
             + (f" ({row['outdated']} on code that changed later)" if row["outdated"] else ""))
        rows.append(row)
    print(json.dumps(rows, indent=1))
    return 1 if failed else 0


def cmd_eval(args: argparse.Namespace, cfg: Config) -> int:
    """`prr eval`: replay the analysis on the PRs you labelled and score it. It costs money, so it says how much first
    and asks (or needs --yes when no one is at a terminal)."""
    from . import evalrun
    from .gitgh import default_repo

    if args.runs < 1:
        _err("--runs must be 1 or more")
        return 2
    root = repo_root(args.repo or ".")
    owner, repo = default_repo(root)
    prs = None
    if args.prs:
        prs = []
        for arg in args.prs:
            num = arg.strip().lstrip("#")
            ref = PRRef(owner, repo, int(num)) if num.isdigit() else parse_pr_arg(arg, root)
            if (ref.owner.lower(), ref.repo.lower()) != (owner.lower(), repo.lower()):
                _err(f"{arg} is not a PR of {owner}/{repo}, the repo you are in")
                return 2
            prs.append(ref.number)

    def confirm(text: str) -> bool:
        print(text, flush=True)
        if args.yes:
            return True
        if not _has_terminal():
            print("`prr eval` spends money, so without a terminal it runs only with --yes.")
            return False
        try:
            return input("Run it? [y/N] ").strip().lower() in ("y", "yes")
        except EOFError:
            return False

    report = evalrun.run_eval(cfg, root, owner, repo, prs=prs, runs=args.runs, split=args.split,
                              max_usd=args.max_usd, keep=args.keep, confirm=confirm)
    return 0 if report and report["status"] == "complete" else 1


def cmd_supervise(args: argparse.Namespace, cfg: Config) -> int:
    supervise.supervise(Path(args.pr_dir))
    return 0


def cmd_mcp(args: argparse.Namespace, cfg: Config) -> int:
    from .chat_tools import serve_stdio

    serve_stdio(Path(args.pr_dir), cfg)
    return 0


def cmd_serve_child(args: argparse.Namespace, cfg: Config) -> int:
    from .server import serve_forever

    token = os.environ.get("PRR_TOKEN", "")  # passed in the environment, not argv, so `ps` doesn't show it
    if not token:
        _err("PRR_TOKEN is not set; start the server with `prr serve`")
        return 2
    serve_forever(Path(args.pr_dir), cfg, port=args.port, token=token)
    return 0


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="prr", description="PR Prism: interactive, Claude-guided GitHub PR review")
    p.add_argument("--repo", help="path inside the git repo (default: cwd)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check prerequisites")
    s = sub.add_parser("fetch", help="fetch PR metadata, refs, worktree and diff")
    s.add_argument("pr", help="PR number or URL")
    s = sub.add_parser("serve", help="start (or reuse) the review web app and print its URL")
    s.add_argument("pr")
    s.add_argument("--no-open", action="store_true", help="don't open a browser")
    s = sub.add_parser("analyze", help="run Claude's analysis (progressively visible in the web app)")
    s.add_argument("pr")
    s.add_argument("--focus", default="", help="free-text reviewer focus, injected into every prompt")
    s.add_argument("--force", action="store_true", help="ignore cached annotations")
    s.add_argument("--concurrency", type=int, default=0)
    s = sub.add_parser("status", help="list fetched PRs, analysis state and running servers")
    s.add_argument("pr", nargs="?")
    s = sub.add_parser("stop", help="stop a PR's web server (it only ever stops when asked)")
    s.add_argument("pr")
    s = sub.add_parser("clean", help="stop the server, remove the worktree and cached data (your review work is kept)")
    s.add_argument("pr", nargs="?")
    s.add_argument("--all", action="store_true", help="every PR of the current repo")
    s.add_argument("--purge", action="store_true",
                   help="also delete the kept review work (chats, pending comments, viewed marks) and the PR's refs")
    s.add_argument("--force", action="store_true", help="also clean PRs whose review page is running")
    s.add_argument("--yes", action="store_true", help="run --all without a terminal (scripts)")
    s = sub.add_parser("labels", help="eval labels: the problems you found on past PRs")
    labels_sub = s.add_subparsers(dest="labels_cmd", required=True)
    s = labels_sub.add_parser("import", help="add the review comments already on PRs as labels (only reads GitHub)")
    s.add_argument("prs", nargs="+", metavar="pr", help="PR numbers or URLs")
    s.add_argument("--reviewers", choices=["me", "all"], default="me",
                   help="whose root comments: yours (default) or every human reviewer's (never bots)")
    s.add_argument("--split", choices=["train", "holdout"], default="train",
                   help="holdout: PRs kept out of prompt work and reported on their own by `prr eval`")
    s = sub.add_parser("eval", help="replay the analysis on the PRs you labelled and score it (costs money; asks first)")
    s.add_argument("--prs", nargs="+", metavar="pr", help="only these PRs (default: every labelled PR of this repo)")
    s.add_argument("--runs", type=int, default=3, help="analyses per labelled PR commit (default 3)")
    s.add_argument("--split", choices=["train", "holdout", "all"], default="all",
                   help="which labelled PRs: train, holdout (the gate's PRs) or all (default)")
    s.add_argument("--max-usd", type=float, default=None, help="start no new run once this much has been spent")
    s.add_argument("--yes", action="store_true", help="don't ask before spending (needed without a terminal)")
    s.add_argument("--keep", action="store_true", help="keep the eval folders (worktrees and each run's files)")
    s = sub.add_parser("_serve", help=argparse.SUPPRESS)  # the supervisor
    s.add_argument("pr_dir")
    s = sub.add_parser("_mcp", help=argparse.SUPPRESS)  # the chat's comment tools (an MCP server on stdio)
    s.add_argument("pr_dir")
    s = sub.add_parser("_serve-child", help=argparse.SUPPRESS)  # the server the supervisor runs
    s.add_argument("pr_dir")
    s.add_argument("--port", type=int, required=True)

    args = p.parse_args(argv)
    cfg = load_config()
    handlers = {"doctor": cmd_doctor, "fetch": cmd_fetch, "serve": cmd_serve, "analyze": cmd_analyze,
                "status": cmd_status, "stop": cmd_stop, "clean": cmd_clean, "labels": cmd_labels, "eval": cmd_eval,
                "_serve": cmd_supervise, "_serve-child": cmd_serve_child, "_mcp": cmd_mcp}
    try:
        code = handlers[args.cmd](args, cfg)
    except (CommandError, ValueError, FileNotFoundError) as e:
        _err(f"error: {e}")
        code = 1
    sys.exit(code)

