"""Who may do what in the chat. One place decides, for the PreToolUse hook, the approval tool and chat_argv.

Permission flow, for each chat turn:

 claude -p (chat turn)   env: PRR_THREAD_ID, PRR_PR_DIR, PRR_ROOTS   ← set in claude.stream_chat
   ├─ Bash / Read / Grep / Glob / LS / NotebookRead
   │     └─► PreToolUse hook: <venv python> -P -m prr.policy  (timeout 960 s)
   │            └─► decide(tool, input)
   │                  ├─ in-root read, or read-only in-root shell ─► allow
   │                  ├─ "Always allow" key for this thread       ─► allow (an outside read also marks the turn)
   │                  ├─ bypass mode (ask_before_actions=false)   ─► deny outside reads and env dumps, else no say
   │                  └─ else ─► _ask_reviewer ─► POST /api/internal/approvals ─► card ─► allow | deny
   │                                     server: any approval marks the turn
   ├─ mcp__* (every MCP tool), WebFetch, WebSearch ─► the same hook
   │        a connector lookup, or codegraph on another repo ─► marks the turn (POST /api/internal/taint)
   │        in a marked turn: GitHub posts, WebFetch, WebSearch, connector writes, shell commands ─► card,
   │        never "Always allow" (a remembered shell command asks again too)
   │        otherwise ─► no say (Claude Code's rules, then the approval tool)
   ├─ mcp__<connector>__get/search/read…, codegraph ─► --permission-prompt-tool ─► chat_tools.approve() ─► decide
   │        trusted author (context.json) ─► allow │ outside author ─► card
   ├─ mcp__prprism__post_comment / post_review ─► approve() ─► decide ─► marked turn? ─► card : allow
   └─ Write / Edit / other tools ─► approve() ─► decide ─► card (unless an "Always allow" key covers it)

The turn's mark lives in the review server (per thread, cleared when a new turn starts), because the hook runs in
its own process for every tool call.

Stdlib only (plus shellsafe and ledger): the hook starts before every tool call, so it has to start fast, and it
runs with `-P` so a `prr/` folder in the PR can't stand in for this package. Never import fastapi or pydantic here.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import ledger, shellsafe

PACKAGE_ROOT = str(Path(__file__).resolve().parent.parent)  # the folder holding this copy of the prr package
READ_TOOLS = ("Read", "Grep", "Glob", "LS", "NotebookRead")
EGRESS_TOOLS = ("WebFetch", "WebSearch")
POST_TOOLS = ("mcp__prprism__post_comment", "mcp__prprism__post_review")
HOOK_MATCHERS = ("Bash|Read|Grep|Glob|LS|NotebookRead", "mcp__.*", "WebFetch|WebSearch")
HOOK_TIMEOUT_S = 960  # the reviewer's 900 s to answer a card, plus a margin
PRIVATE_FILES = ("server.json", "server.pid")  # the review server's login token and process ids
TRUSTED = ("OWNER", "MEMBER", "COLLABORATOR")
CAUSE_KINDS = ("lookup", "outside_read", "approved")
# Folders "Always allow reads in …" is never offered for (nor anything above them, nor anything holding secrets).
NO_FOLDER_KEY = ("/", "~", *shellsafe.SECRET_PLACES)
APPROVAL_WAIT_S = int(os.environ.get("PRR_APPROVAL_WAIT_S") or 15 * 60)
# Connector tools that only look things up (getJiraIssue, searchJiraIssuesUsingJql, slack_read_channel, …): a read
# word in the name and no write word (search_replace and getAndDelete are writes).
_READ_ONLY_MCP = re.compile(r"(^|_)(get|search|list|read|fetch|lookup|query|find|view)([A-Z_]|$)")
_WRITE_WORDS = frozenset(
    "add append approve archive assign close comment create delete drop edit execute insert invite merge move "
    "patch post publish put react reject remove rename replace reset run schedule send set share submit "
    "transition trigger unpublish update upload upsert write".split())
DENIED = "The reviewer said no. Don't try this again; ask them what they want instead."
CHECK_FAILED = "PR Prism's check failed: {}; ask the reviewer"


# --------------------------------------------------------------------------------------
# What the PR folder says
# --------------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def context(pr_dir: Path) -> dict:
    return _read_json(pr_dir / "context.json") or {}


def roots(pr_dir: Path) -> list[Path]:
    """Where the chat may read without asking: the PR's worktree and the PR folder (minus PRIVATE_FILES)."""
    worktree = context(pr_dir).get("worktree")
    return [Path(worktree), pr_dir] if worktree else [pr_dir]


def scope(pr_dir: Path) -> shellsafe.Scope:
    env_roots = os.environ.get("PRR_ROOTS", "") if os.environ.get("PRR_PR_DIR") == str(pr_dir) else ""
    pr = context(pr_dir).get("pr") or {}
    return shellsafe.Scope.of(env_roots.split(os.pathsep) if env_roots else roots(pr_dir),
                              [pr_dir / name for name in PRIVATE_FILES],
                              f"{pr.get('owner', '')}/{pr.get('repo', '')}")


def trust(pr_dir: Path) -> dict:
    """The PR author's standing, read from context.json (never from the environment, so an old server and new code
    can't disagree). state: "trusted" (OWNER, MEMBER or COLLABORATOR), "outside", or "unknown" when the check
    failed or the PR was fetched before it existed; unknown is treated as outside."""
    pr = context(pr_dir).get("pr") or {}
    association = pr.get("author_association")
    ok = association in TRUSTED
    return {"author": pr.get("author", ""), "association": association, "trusted": ok,
            "state": "trusted" if ok else ("outside" if association else "unknown")}


def trusted(pr_dir: Path) -> bool:
    return trust(pr_dir)["trusted"]


def always_allowed(pr_dir: Path, thread_id: str) -> list[str]:
    """The thread's "Always allow" keys (state.json is written atomically, so a plain read is safe)."""
    state = _read_json(pr_dir / "state.json") or {}
    thread = next((t for t in state.get("threads", []) if t.get("id") == thread_id), None) if thread_id else None
    return list((thread or {}).get("always_allow", []))


# --------------------------------------------------------------------------------------
# Paths and "Always allow" keys
# --------------------------------------------------------------------------------------


def _expand_braces(pattern: str, limit: int = 32) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m or limit <= 0:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out += _expand_braces(pattern[:m.start()] + alt + pattern[m.end():], limit - 1)
    return out[:limit]


def _glob_base(pattern: str) -> str:
    """The folder a glob starts from: its parts up to the first wildcard (`/Users/**` → `/Users`)."""
    parts = []
    for part in pattern.split("/"):
        if any(ch in part for ch in "*?["):
            break
        parts.append(part)
    return "/".join(parts) if parts not in ([], [""]) else ("/" if pattern.startswith("/") else ".")


def read_paths(tool: str, inp: dict, cwd: str) -> list[str]:
    """What a read tool looks at, resolved: file_path, path, Glob's pattern, Grep's glob, notebook_path."""
    def full(p: str) -> str:
        return os.path.realpath(os.path.join(cwd, os.path.expanduser(str(p))))

    if tool == "Read":
        named = [inp.get("file_path")]
    elif tool == "NotebookRead":
        named = [inp.get("notebook_path")]
    elif tool == "LS":
        named = [inp.get("path")]
    elif tool in ("Grep", "Glob"):
        base = str(inp.get("path") or ".")
        pattern = inp.get("glob") if tool == "Grep" else inp.get("pattern")
        named = [base] + [_glob_base(os.path.join(base, p)) for p in _expand_braces(str(pattern))] if pattern else [base]
    else:
        named = []
    return [full(p) for p in named if p]


def _folder(tool: str, path: str) -> str:
    """The folder an "Always allow reads in …" key names: a file's folder, or the folder searched or listed."""
    if tool in ("Glob", "LS") or (tool == "Grep" and os.path.isdir(path)):
        return path
    return os.path.dirname(path)


def safe_folder_key(folder: str, sc: shellsafe.Scope | None = None) -> bool:
    """May "Always allow reads in <folder>" be offered? Never for /, ~ or anything above it, a folder that holds
    secrets (~/.ssh, ~/.config/gh, ~/.aws, ~/.kube, ~/.docker) or anything inside one, or one holding server.json."""
    f = os.path.realpath(os.path.expanduser(folder))
    for place in NO_FOLDER_KEY:
        p = os.path.realpath(os.path.expanduser(place))
        if place in ("/", "~") and shellsafe._under(p, f):
            return False
        if place not in ("/", "~") and (shellsafe._under(p, f) or shellsafe._under(f, p)):
            return False
    return not (sc and any(shellsafe._under(priv, f) for priv in sc.private))


def covered(keys: list[str], allowed: list[str]) -> bool:
    """Every key was remembered; a `Read:<folder>` key is also covered by a remembered folder above it."""
    folders = [a[5:] for a in allowed if a.startswith("Read:")]
    return bool(keys) and all(k in allowed or (k.startswith("Read:") and any(shellsafe._under(k[5:], f)
                                                                             for f in folders)) for k in keys)


def guard_card(tool: str, marked: bool) -> bool:
    """Cards that stand between outside data and a way out. They never offer (or remember) "Always allow"."""
    if not marked:
        return False
    if tool in POST_TOOLS or tool in EGRESS_TOOLS or tool == "Bash":
        return True
    return _connector(tool) and not _lookup(tool)


def approval_keys(tool: str, inp: dict, *, sc: shellsafe.Scope, cwd: str) -> list[str]:
    """What "Always allow in this chat" remembers: per program for shell commands (or the exact command when it
    reaches outside the PR), `Read:<folder>` per outside folder for read tools, else the tool. [] when "Always allow"
    must not be offered: posts, and folders or commands touching / , ~ or secrets."""
    if tool in POST_TOOLS:
        return []
    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        rep = shellsafe.analyze(cmd, sc, cwd)
        if any(shellsafe.secret_place(p) or not safe_folder_key(p, sc) for p in rep.paths):
            return []
        return ["Bash:" + k for k in shellsafe.approval_keys(cmd, sc, cwd)]
    if tool in READ_TOOLS:
        folders = []
        for p in read_paths(tool, inp, cwd):
            if not sc.contains(p):
                f = _folder(tool, p)
                if not safe_folder_key(f, sc):
                    return []
                if f not in folders:
                    folders.append(f)
        return ["Read:" + f for f in folders]
    return [tool]


def show_folder(path: str, width: int = 32) -> str:
    """~/code/billing, middle-truncated to about `width` characters: ~/code/very…/deep/folder."""
    home = os.path.realpath(os.path.expanduser("~"))
    p = os.path.realpath(path)
    if shellsafe._under(p, home):
        p = "~" + p[len(home):]
    if len(p) <= width:
        return p
    keep = width - 1
    return p[:keep // 2] + "…" + p[len(p) - (keep - keep // 2):]


def tool_label(tool: str) -> str:
    """mcp__claude_ai_Atlassian__getJiraIssue → "Atlassian: getJiraIssue"."""
    parts = tool.split("__")
    if tool.startswith("mcp__") and len(parts) >= 3:
        return f"{parts[1].removeprefix('claude_ai_').replace('_', ' ')}: {parts[2]}"
    return tool


def always_text(tool: str, keys: list[str]) -> tuple[str, str]:
    """The "Always allow" button's label and its tooltip ("" when it isn't offered)."""
    if not keys:
        return "", ""
    if tool in READ_TOOLS:
        folders = [k[5:] for k in keys]
        return ("Always allow reads in " + ", ".join(show_folder(f) for f in folders),
                "Don't ask again in this chat for reads in " + ", ".join(folders))
    if tool == "Bash":
        names = ", ".join(k.removeprefix("Bash:") for k in keys)
        return f"Always allow {names}", f"Don't ask again in this chat for commands that use only: {names}"
    return f"Always allow {tool_label(tool)} in this chat", f"Don't ask again in this chat for {tool_label(tool)}"


def _connector(tool: str) -> bool:
    return tool.startswith("mcp__") and not tool.startswith(("mcp__prprism__", "mcp__codegraph__"))


def _lookup(tool: str) -> bool:
    parts = tool.split("__")
    if len(parts) < 3 or not _READ_ONLY_MCP.search(parts[2]):
        return False
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", parts[2]).lower().split("_")
    return not _WRITE_WORDS.intersection(words)


def summary(tool: str, inp: dict) -> str:
    """A short line naming what the tool call touches, for causes and the approval log."""
    for key in ("command", "file_path", "notebook_path", "url", "query", "projectPath", "path", "pattern"):
        if inp.get(key):
            return str(inp[key])[:200]
    hint = next((str(v) for v in inp.values() if isinstance(v, (str, int)) and str(v).strip()), "")
    return f"{tool_label(tool)} {hint}".strip()[:200]


# --------------------------------------------------------------------------------------
# Talking to the review server
# --------------------------------------------------------------------------------------


def _server(pr_dir: Path, method: str, path: str, body: dict | None = None, timeout: float = 10) -> dict | None:
    info = _read_json(pr_dir / "server.json") or {}
    if not info.get("port"):
        return None
    base = f"http://127.0.0.1:{info['port']}"
    headers = {"Cookie": f"prr_session={info.get('token', '')}", "Origin": base}
    data = None
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, ValueError):
        return None


def turn_causes(pr_dir: Path, thread_id: str) -> list[dict]:
    """What marked this chat turn: [] when nothing did. Fails closed: if the server can't say, the turn is marked."""
    got = _server(pr_dir, "GET", "/api/internal/taint?" + urllib.parse.urlencode({"thread": thread_id}))
    if got is None:
        return [{"kind": "approved", "text": "PR Prism couldn't check what happened earlier this turn"}]
    return list(got.get("causes") or [])


def mark_turn(pr_dir: Path, thread_id: str, cause: dict) -> None:
    _server(pr_dir, "POST", "/api/internal/taint", {"thread_id": thread_id, **cause})


def _ask_reviewer(pr_dir: Path, tool: str, inp: dict, *, reason: dict | None = None, cwd: str | None = None,
                  thread_id: str | None = None) -> dict:
    """Ask through the review server (which shows the card in the chat) and wait for the answer."""
    info = _read_json(pr_dir / "server.json") or {}
    if not info.get("port"):
        return {"decision": "deny", "message": "The review app is not running, so nobody can approve this."}
    base = f"http://127.0.0.1:{info['port']}"
    tid = os.environ.get("PRR_THREAD_ID", "") if thread_id is None else thread_id
    data = json.dumps({"thread_id": tid, "tool_name": tool, "input": inp, "reason": reason, "cwd": cwd}).encode()
    req = urllib.request.Request(f"{base}/api/internal/approvals", data=data, method="POST", headers={
        "Content-Type": "application/json", "Cookie": f"prr_session={info.get('token', '')}", "Origin": base})
    try:
        with urllib.request.urlopen(req, timeout=APPROVAL_WAIT_S + 30) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        return {"decision": "deny", "message": f"Could not ask the reviewer: {e}"}


def _log(pr_dir: Path, tool: str, inp: dict, decision: str, why: str) -> None:
    """One line per decision that wasn't a plain in-PR read, in calls.jsonl (no `purpose`: never counted as cost)."""
    try:
        ledger.append(pr_dir, {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "kind": "approval",
                               "tool": tool, "input": summary(tool, inp), "decision": decision, "why": why})
    except OSError:
        pass


# --------------------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------------------


def _ask(kind: str, text: str, detail: str = "") -> dict:
    return {"decision": "ask", "reason": {"kind": kind, "text": text, **({"detail": detail} if detail else {})}}


def _deny(message: str) -> dict:
    return {"decision": "deny", "message": message}


def default_reason(tool: str) -> dict:
    if tool == "Bash":
        return {"kind": "action", "text": "This command can change files or run code"}
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return {"kind": "action", "text": "This changes a file in the PR's copy"}
    if tool in EGRESS_TOOLS:
        return {"kind": "action", "text": "This sends a request to the internet"}
    if _connector(tool):
        return {"kind": "action", "text": f"This changes something in {tool_label(tool).split(':')[0]}"}
    return {"kind": "action", "text": "This tool asks first"}


def guard_reason(tool: str, causes: list[dict]) -> dict:
    """Why a guard card asks, from what marked the turn."""
    read = any(c.get("kind") in ("lookup", "outside_read") for c in causes)
    what = "content from Jira/Slack/other files read this turn" if read else "output from a command you approved this turn"
    if tool in POST_TOOLS:
        thing = "review" if tool.endswith("post_review") else "comment"
        return {"kind": "marked_turn", "text": f"This {thing} may include {what}. Post it?"}
    return {"kind": "marked_turn", "text": f"This could send out {what}."}


def decide(tool: str, inp: dict, *, pr_dir: Path, thread_id: str = "", cwd: str | None = None,
           ask_first: bool = True, hook: bool = False) -> dict:
    """What to do with one tool call:
    - {"decision": "allow"}, maybe with "mark": a cause that marks the turn;
    - {"decision": "deny", "message": …} for Claude;
    - {"decision": "ask", "reason": {kind, text, detail?}}: show a card;
    - {"decision": "pass"} (hook only): no say; Claude Code's rules and the approval tool decide.
    `cwd` is the shell's folder (from the hook); None in the approval tool, which never auto-allows shell commands."""
    ctx = context(pr_dir)
    sc = scope(pr_dir)
    where = cwd or ctx.get("worktree") or str(pr_dir)
    no_say = {"decision": "pass" if hook else "allow"}
    allowed = always_allowed(pr_dir, thread_id)

    if tool in READ_TOOLS:
        outside = [p for p in read_paths(tool, inp, where) if not sc.contains(p)]
        if not outside:
            return {"decision": "allow"}
        shown = ", ".join(show_folder(p, 60) for p in outside[:3])
        if not ask_first:
            return _deny(f"Reading outside this PR is blocked ({shown}). Work with the PR's files instead.")
        keys = approval_keys(tool, inp, sc=sc, cwd=where)
        if covered(keys, allowed):
            return {"decision": "allow", "mark": {"kind": "outside_read", "text": shown}}
        return _ask("outside_read", "Outside this PR's code", shown)

    if tool == "Bash":
        cmd = str(inp.get("command", ""))
        rep = shellsafe.analyze(cmd, sc, where)
        if not ask_first:
            if rep.outside or rep.env:
                return _deny("Blocked: this command " + "; ".join((rep.outside + rep.env)[:3]) +
                             ". Commands may not read outside this PR or print the environment.")
            return no_say
        if cwd is not None and shellsafe.is_read_only(cmd, sc, where):
            return {"decision": "allow"}
        if covered(approval_keys(tool, inp, sc=sc, cwd=where), allowed):
            # A remembered program (curl, python, …) could send out what this turn read, so it asks again.
            causes = turn_causes(pr_dir, thread_id)
            if causes:
                return {"decision": "ask", "reason": guard_reason(tool, causes)}
            return {"decision": "allow", "mark": {"kind": "approved", "text": cmd[:200]}}
        if rep.env:
            return _ask("env", "Reads environment variables, which can hold secrets", "; ".join(rep.env))
        if rep.outside or rep.elsewhere:
            return _ask("outside_read", "Outside this PR's code",
                        "; ".join(rep.outside[:3]) or ", ".join(show_folder(p, 60) for p in rep.elsewhere[:3]))
        return {"decision": "ask", "reason": default_reason(tool)}

    if tool in EGRESS_TOOLS or tool in POST_TOOLS or (_connector(tool) and not _lookup(tool)):
        if not ask_first:
            return no_say
        causes = turn_causes(pr_dir, thread_id)
        if causes:
            return {"decision": "ask", "reason": guard_reason(tool, causes)}
        if hook or tool in POST_TOOLS:
            return no_say
        return {"decision": "allow"} if covered([tool], allowed) else {"decision": "ask", "reason": default_reason(tool)}

    if tool.startswith("mcp__codegraph__"):
        project = str(inp.get("projectPath") or "")
        repo_root = ctx.get("repo_root") or ""
        inside = not project or any(shellsafe._under(os.path.realpath(project), os.path.realpath(r))
                                    for r in (repo_root, ctx.get("worktree")) if r)
        if inside:
            return no_say
        mark = {"kind": "lookup", "text": f"codegraph on {show_folder(project, 60)}"}
        return _lookup_decision(tool, mark, pr_dir, allowed, ask_first, hook, "codegraph on another repo")

    if _connector(tool):  # a lookup
        mark = {"kind": "lookup", "text": summary(tool, inp)}
        return _lookup_decision(tool, mark, pr_dir, allowed, ask_first, hook, "a Jira, Slack or other lookup")

    if tool.startswith("mcp__prprism__"):
        return no_say  # add_pending_comment never leaves the app
    if hook:
        return no_say
    return {"decision": "allow"} if covered([tool], allowed) else {"decision": "ask", "reason": default_reason(tool)}


def _lookup_decision(tool: str, mark: dict, pr_dir: Path, allowed: list[str], ask_first: bool, hook: bool,
                     what: str) -> dict:
    """Lookups mark the turn. The hook never decides them; the approval tool asks first on an outsider's PR."""
    if hook:
        return {"decision": "pass", "mark": mark}
    if not ask_first or covered([tool], allowed):
        return {"decision": "allow", "mark": mark}
    who = trust(pr_dir)
    if who["trusted"]:
        return {"decision": "allow", "mark": mark}
    if who["state"] == "unknown":
        return _ask("outside_author", "Couldn't check the author; lookups ask first", what)
    return _ask("outside_author", f"Outside author (@{who['author']})", what)


def resolve(tool: str, inp: dict, *, pr_dir: Path, thread_id: str = "", cwd: str | None = None,
            ask_first: bool = True, hook: bool = False) -> dict:
    """decide(), then show the card when it says so. Returns {"decision": "allow" | "deny" | "pass", "message"?}."""
    verdict = decide(tool, inp, pr_dir=pr_dir, thread_id=thread_id, cwd=cwd, ask_first=ask_first, hook=hook)
    if verdict.get("mark"):
        mark_turn(pr_dir, thread_id, verdict["mark"])
    if verdict["decision"] == "ask":
        answer = _ask_reviewer(pr_dir, tool, inp, reason=verdict["reason"], cwd=cwd, thread_id=thread_id)
        ok = answer.get("decision") in ("allow", "always")
        _log(pr_dir, tool, inp, "allow" if ok else "deny", f"card: {verdict['reason']['kind']}")
        return {"decision": "allow"} if ok else _deny(answer.get("message") or DENIED)
    if verdict["decision"] == "deny" or verdict.get("mark"):
        _log(pr_dir, tool, inp, verdict["decision"], "deny" if verdict["decision"] == "deny" else "marks the turn")
    return verdict


# --------------------------------------------------------------------------------------
# The hook: `<venv python> -P -m prr.policy [--bypass]`
# --------------------------------------------------------------------------------------


def hook_command(ask_first: bool) -> str:
    """The PreToolUse hook's shell command. PYTHONPATH pins this copy of PR Prism (the one the server runs); -P keeps
    the chat's folder off sys.path. `|| exit 2` blocks the tool if Python can't even start the check."""
    import shlex

    return (f"PYTHONPATH={shlex.quote(PACKAGE_ROOT)} {shlex.quote(sys.executable)} -P -m prr.policy"
            + ("" if ask_first else " --bypass") + " || exit 2")


def hook_settings(ask_first: bool) -> dict:
    """`--settings` for the chat: one PreToolUse hook entry per matcher, all running the same check."""
    entry = [{"type": "command", "command": hook_command(ask_first), "timeout": HOOK_TIMEOUT_S}]
    return {"hooks": {"PreToolUse": [{"matcher": m, "hooks": entry} for m in HOOK_MATCHERS]}}


def run_hook(stdin_text: str, *, bypass: bool) -> dict | None:
    data = json.loads(stdin_text)
    if not isinstance(data, dict) or not isinstance(data.get("tool_name"), str):
        raise ValueError("the hook input has no tool_name")
    inp = data.get("tool_input") or {}
    if not isinstance(inp, dict):
        raise ValueError("the hook input's tool_input is not an object")
    pr_dir = Path(os.environ.get("PRR_PR_DIR", ""))
    if not pr_dir.is_absolute():
        raise ValueError("PRR_PR_DIR is not set, so the PR's folder is unknown")
    out = resolve(data["tool_name"], inp, pr_dir=pr_dir, thread_id=os.environ.get("PRR_THREAD_ID", ""),
                  cwd=str(data.get("cwd") or context(pr_dir).get("worktree") or pr_dir), ask_first=not bypass,
                  hook=True)
    if out["decision"] == "pass":
        return None
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": out["decision"],
                                   "permissionDecisionReason": out.get("message") or "Allowed by PR Prism"}}


def main(argv: list[str] | None = None) -> int:
    """Fails closed: any error (bad input, a bug, a missing variable) denies the tool with a reason, and the exit
    code is always 0 (a non-zero code other than 2 would let the tool run)."""
    args = sys.argv[1:] if argv is None else argv
    try:
        out = run_hook(sys.stdin.read(), bypass="--bypass" in args)
    except BaseException as e:  # noqa: BLE001 — whatever went wrong, the tool must not run unchecked
        out = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                      "permissionDecisionReason": CHECK_FAILED.format(f"{type(e).__name__}: {e}")}}
    if out is not None:
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
