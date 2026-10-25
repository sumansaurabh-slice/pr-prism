"""How PR Prism is installed, for `prr doctor` and install.sh.

There are two ways to install it, and only one may be used at a time, so there is a single /pr-prism:
- install.sh (a dev checkout): ~/.claude/skills/pr-prism is a symlink to the checkout's skills/pr-prism;
- the Claude Code plugin: `pr-prism@<marketplace>` in ~/.claude/plugins/installed_plugins.json. Claude Code keeps each
  version in ~/.claude/plugins/cache/<marketplace>/pr-prism/<version>/, and bin/prr sets it up on first run.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import supervise
from .config import PACKAGE_DIR, REPO_DIR, SKILL_DIR
from .util import read_json

PLUGIN = "pr-prism"
E2E_TEST = "tests/test_e2e_chat_security.py"
E2E_KEY = "pr-prism/e2e-last"  # pytest's cache key; tests/conftest.py writes it after the E2E runs
E2E_RECORD = REPO_DIR / ".pytest_cache" / "v" / E2E_KEY

Check = tuple[str, bool, str]  # (label, passed, hint) for `prr doctor`


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude").expanduser()


def skill_link() -> Path:
    return claude_dir() / "skills" / "pr-prism"


def plugin_installs() -> list[dict]:
    """Each install of the PR Prism plugin that Claude Code recorded: {id, version, path, scope}."""
    data = read_json(claude_dir() / "plugins" / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    found = []
    for key, entries in plugins.items() if isinstance(plugins, dict) else []:
        if key.split("@", 1)[0] != PLUGIN:
            continue
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                found.append({"id": key, "version": e.get("version"), "path": e.get("installPath"),
                              "scope": e.get("scope")})
    return found


def install_check() -> Check:
    """Which installs exist. Both at once means two /pr-prism skills (install.sh refuses that, but a plugin can be
    installed after it)."""
    link = skill_link()
    plugins = plugin_installs()
    names = ", ".join(sorted({p["id"] for p in plugins}))
    linked = link.is_symlink() or link.exists()
    if linked and plugins:
        return (f"installed twice: with install.sh ({_home(link)}) and as the plugin {names}", False,
                f"/pr-prism runs the install.sh copy; keep one: ./install.sh --uninstall, or claude plugin uninstall "
                f"{plugins[0]['id']}")
    if linked:
        if not link.exists():
            return (f"installed with install.sh, but {_home(link)} points to a missing folder ({os.readlink(link)})",
                    False, "run ./install.sh again in the PR Prism folder")
        target = link.resolve()
        other = "" if target == SKILL_DIR.resolve() else f"; this copy is {SKILL_DIR}"
        return f"installed with install.sh: {_home(link)} → {target}{other}", True, ""
    if plugins:
        versions = ", ".join(f"{p['id']} {p['version']}" for p in plugins)
        return f"installed as the Claude Code plugin {versions}", True, ""
    return "installed for Claude Code", False, "run ./install.sh, or install the plugin (see the README)"


def claude_version() -> str | None:
    claude = shutil.which("claude")
    if not claude:
        return None
    try:
        out = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"\d+\.\d+\.\d+\S*", out)
    return m.group(0) if m else None


def e2e_check(current_claude: str | None) -> Check | None:
    """The last run of the end-to-end chat security test in this checkout. None outside a dev checkout."""
    rec = read_json(E2E_RECORD)
    rerun = f"rerun: .venv/bin/python -m pytest {E2E_TEST}"
    if not isinstance(rec, dict):
        if (REPO_DIR / ".venv").is_dir():
            return "end-to-end chat security test", False, f"no run recorded in this checkout; {rerun}"
        return None
    when = str(rec.get("at", ""))[:16].replace("T", " ")
    ran_on = rec.get("claude") or "unknown"
    if not rec.get("passed"):
        failed = ", ".join(k for k, v in (rec.get("results") or {}).items() if v != "passed")
        return f"end-to-end chat security test failed on {when} (Claude Code {ran_on})", False, f"{failed}; {rerun}"
    if current_claude and ran_on != current_claude:
        return (f"end-to-end chat security test last passed on Claude Code {ran_on}; you now run {current_claude}",
                False, rerun)
    return f"end-to-end chat security test passed on {when} (Claude Code {ran_on})", True, ""


# ---- review servers still running old code (install.sh, after a git pull) -------------------------------------


def _code_time() -> float:
    """When this copy's code last changed: the newest .py or prompt file in the package."""
    return max((p.stat().st_mtime for p in PACKAGE_DIR.rglob("*") if p.suffix in (".py", ".md") and p.is_file()),
               default=0.0)


def old_servers(cache_dir: Path) -> list[dict]:
    """Review servers that started before this copy's code last changed, so they still run the old code."""
    changed = _code_time()
    found = []
    for d in sorted(cache_dir.glob("*/pr-*")) if cache_dir.is_dir() else []:
        if not supervise.running(d):
            continue
        rt = supervise.runtime(d) or {}
        try:
            started = datetime.fromisoformat(rt["started_at"]).timestamp()
        except (KeyError, TypeError, ValueError):
            started = 0.0  # a server from before supervision: always old
        if started >= changed:
            continue
        ctx = read_json(d / "context.json") or {}
        pr = ctx.get("pr") or {}
        found.append({"pr": f"{pr.get('owner')}/{pr.get('repo')}#{pr.get('number')}", "number": pr.get("number"),
                      "repo_root": ctx.get("repo_root"), "dir": str(d)})
    return found


def print_old_servers(prr: str) -> None:
    """Print each review server that runs old code, with the command that restarts it."""
    from .config import load_config

    rows = [r for r in old_servers(load_config().cache_dir) if r["number"] and r["repo_root"]]
    if not rows:
        return
    cmd = _home(Path(prr))
    print("\nThese review servers still run the old code. Restart each one to use the new code:")
    for r in rows:
        n = r["number"]
        print(f"  {r['pr']}:  cd {shlex.quote(r['repo_root'])} && {cmd} stop {n} && {cmd} serve {n}")


def _home(path: Path) -> str:
    """The path with ~ for your home folder, ready to paste into a shell."""
    home = str(Path.home())
    text = str(path)
    if text == home or text.startswith(home + os.sep):
        rest = text[len(home):].lstrip(os.sep)
        return "~/" + shlex.quote(rest) if rest else "~"
    return shlex.quote(text)
