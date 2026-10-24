"""`prr` command line: doctor | fetch | serve | analyze | status | stop | clean.

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

from . import supervise
from .config import SKILL_DIR, WEB_DIST, Config, load_config, pr_dir
from .gitgh import CommandError, PRRef, parse_pr_arg, repo_root
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
        rows.append({
            "pr": f"{pr.get('owner')}/{pr.get('repo')}#{pr.get('number')}", "title": pr.get("title"),
            "head": (pr.get("head_sha") or "")[:10], "analysis": prog.get("status", "not analyzed"),
            "server": info.get("url") if supervise.healthy(d) else None, "dir": str(d),
        })
    print(json.dumps(rows, indent=1))
    return 0


def cmd_stop(args: argparse.Namespace, cfg: Config) -> int:
    _, _, d = _resolve(args, cfg)
    print("stopped (the link stays the same next time)" if supervise.stop(d) else "not running")
    return 0


def cmd_clean(args: argparse.Namespace, cfg: Config) -> int:
    from .fetch import remove_worktree

    root = repo_root(args.repo or ".")
    if args.all:
        from .gitgh import default_repo
        owner, repo = default_repo(root)
        targets = sorted((cfg.cache_dir / f"{owner}__{repo}").glob("pr-*"))
    else:
        if not args.pr:
            _err("give a PR number or --all")
            return 2
        targets = [_resolve(args, cfg)[2]]
    for d in targets:
        if not d.exists():
            continue
        supervise.stop(d)
        remove_worktree(root, d, int(d.name.split("-", 1)[1]))
        shutil.rmtree(d, ignore_errors=True)
        print(f"removed {d}")
    return 0


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
    s = sub.add_parser("clean", help="stop the server, remove the worktree and cached data")
    s.add_argument("pr", nargs="?")
    s.add_argument("--all", action="store_true", help="every PR of the current repo")
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
                "status": cmd_status, "stop": cmd_stop, "clean": cmd_clean, "_serve": cmd_supervise,
                "_serve-child": cmd_serve_child, "_mcp": cmd_mcp}
    try:
        code = handlers[args.cmd](args, cfg)
    except (CommandError, ValueError, FileNotFoundError) as e:
        _err(f"error: {e}")
        code = 1
    sys.exit(code)

