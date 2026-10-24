"""Running `claude -p`: structured-output calls for analysis and streamed, resumable calls for chat.

Analysis calls are locked down: read-only tools (`--tools`), no MCP servers except ours (`--strict-mcp-config`),
and `--permission-mode dontAsk` refuses everything else.

Chat calls are full Claude Code: every built-in tool, the reviewer's own MCP servers and claude.ai connectors
(Jira, Slack, …). Anything that is not on a short read-only list goes to the app's approval tool
(`--permission-prompt-tool`), which asks the reviewer in the browser first.

Both use `--setting-sources user`: the PR's own .claude/settings.json (hooks, allow rules) is never loaded, and a
PR's .mcp.json is not loaded either (it would need approval in project settings).
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from .gitgh import claude_env
from .ledger import Ledger

READ_ONLY_TOOLS = ["Read", "Grep", "Glob"]

# The chat runs these without asking: reading and searching, and git/shell commands that only look at things.
CHAT_AUTO_ALLOW = [
    "Read", "Grep", "Glob", "LS", "NotebookRead", "TodoWrite", "ToolSearch", "Task",
    "mcp__prprism", "mcp__codegraph",
    *(f"Bash({c} *)" for c in (
        "git blame", "git log", "git show", "git diff", "git status", "git grep", "git rev-parse", "git ls-files",
        "git branch", "git shortlog", "git cat-file", "rg", "grep", "ls", "wc", "tree", "file",
    )),
]
APPROVAL_TOOL = "mcp__prprism__approve"
STREAM_LIMIT = 64 * 1024 * 1024  # tool results can put whole files on one stream-json line


class ClaudeError(RuntimeError):
    pass


def base_argv(model: str, tools: list[str], *, mcp_config: str | None = None, mcp_tools: list[str] | None = None,
              add_dirs: list[Path] | None = None) -> list[str]:
    argv = [
        "claude", "-p", "--model", model,
        "--tools", ",".join(tools),
        "--permission-mode", "dontAsk",
        "--setting-sources", "user",
        "--strict-mcp-config",
    ]
    allowed = tools + (mcp_tools or [])
    if allowed:
        argv += ["--allowedTools", *allowed]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    for d in add_dirs or []:
        argv += ["--add-dir", str(d)]
    return argv


@dataclass
class CallStats:
    cost_usd: float | None = None
    duration_ms: int | None = None
    session_id: str | None = None
    num_turns: int | None = None


def run_structured(prompt: str, schema: dict[str, Any], *, cwd: Path, model: str, tools: list[str] | None = None,
                   max_turns: int = 40, timeout: float = 900, add_dirs: list[Path] | None = None,
                   ledger: Ledger | None = None, purpose: str = "other", target: str = "") -> tuple[dict, CallStats]:
    """One structured-output call. With a ledger, its cost goes to calls.jsonl before this returns or raises
    (a timeout's cost is unknown: the process was killed before it reported one)."""
    started = time.monotonic()
    call = {"stats": CallStats(), "outcome": "error", "error": None}
    try:
        return _run_structured(prompt, schema, cwd, model, tools, max_turns, timeout, add_dirs, call)
    except Exception as e:
        call["error"] = str(e)
        raise
    finally:
        if ledger is not None:
            ledger.record(purpose, model=model, target=target, cost_usd=call["stats"].cost_usd,
                          duration_ms=int((time.monotonic() - started) * 1000), outcome=call["outcome"],
                          error=call["error"])


def _run_structured(prompt: str, schema: dict[str, Any], cwd: Path, model: str, tools: list[str] | None,
                    max_turns: int, timeout: float, add_dirs: list[Path] | None, call: dict) -> tuple[dict, CallStats]:
    argv = base_argv(model, READ_ONLY_TOOLS if tools is None else tools, add_dirs=add_dirs)
    argv += ["--output-format", "json", "--json-schema", json.dumps(schema), "--max-turns", str(max_turns)]
    try:
        proc = subprocess.run(argv, input=prompt, cwd=cwd, env=claude_env(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        call["outcome"] = "timeout"
        raise ClaudeError(f"timed out after {timeout:.0f}s") from e
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        tail = (proc.stderr or proc.stdout).strip()[-500:]
        raise ClaudeError(f"claude exited {proc.returncode} without JSON output: {tail}") from e
    stats = call["stats"] = CallStats(data.get("total_cost_usd"), data.get("duration_ms"), data.get("session_id"),
                                      data.get("num_turns"))
    if data.get("is_error") or data.get("subtype") != "success":
        raise ClaudeError(f"{data.get('subtype')}: {str(data.get('result'))[:500]}")
    out = data.get("structured_output")
    if out is None:
        try:
            out = json.loads(data.get("result") or "")
        except json.JSONDecodeError as e:
            call["outcome"] = "refused"
            raise ClaudeError("no structured_output in result") from e
    call["outcome"] = "ok"
    return out, stats


# --------------------------------------------------------------------------------------
# Streaming chat
# --------------------------------------------------------------------------------------


# How the chat's own tools are shown to the reviewer while Claude works.
FRIENDLY_TOOL_NAMES = {
    "mcp__prprism__add_pending_comment": "Added pending comment",
    "mcp__prprism__post_comment": "Posted comment to GitHub",
    "mcp__prprism__post_review": "Posted review to GitHub",
}


def friendly_tool_name(name: str) -> str:
    """mcp__claude_ai_Atlassian__createJiraIssue → "Atlassian: createJiraIssue"."""
    if name in FRIENDLY_TOOL_NAMES:
        return FRIENDLY_TOOL_NAMES[name]
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return f"{parts[1].removeprefix('claude_ai_').replace('_', ' ')}: {parts[2]}"
    return name


def tool_summary(name: str, inp: dict[str, Any], cwd: Path | None = None) -> str:
    def rel(p: str) -> str:
        if cwd and p.startswith(str(cwd) + os.sep):
            return p[len(str(cwd)) + 1:]
        return p

    if name == "Bash":
        return str(inp.get("command", ""))[:200]
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return rel(str(inp.get("file_path") or inp.get("notebook_path") or ""))
    if name in ("WebFetch", "WebSearch"):
        return str(inp.get("url") or inp.get("query") or "")[:200]
    if name == "Read":
        s = rel(inp.get("file_path", ""))
        if inp.get("offset"):
            s += f" (from line {inp['offset']})"
        return s
    if name == "Grep":
        where = inp.get("path") or inp.get("glob") or inp.get("type") or ""
        return f"{inp.get('pattern', '')!r}" + (f" in {rel(where)}" if where else "")
    if name == "Glob":
        return inp.get("pattern", "")
    if name.startswith("mcp__prprism__"):
        if "path" in inp:
            start = f"{inp['start_line']}-" if inp.get("start_line") else ""
            return f"{rel(str(inp['path']))}:{start}{inp.get('line', '')}"
        return f"{str(inp.get('event') or 'COMMENT').lower()}" + (" with pending comments" if inp.get("include_pending") else "")
    if "codegraph" in name:
        return str(inp.get("query") or inp.get("symbol") or inp)[:120]
    return json.dumps(inp)[:120]


@dataclass
class StreamTranslator:
    """stream-json lines → UI events (text / thinking / tool / result / error)."""

    cwd: Path | None = None
    _text_msgs: set[str] = field(default_factory=set)
    _current_msg: str | None = None

    def feed(self, obj: dict[str, Any]) -> list[dict[str, Any]]:
        t = obj.get("type")
        if t == "stream_event":
            ev = obj.get("event", {})
            et = ev.get("type")
            if et == "message_start":
                self._current_msg = ev.get("message", {}).get("id")
            elif et == "content_block_start" and ev.get("content_block", {}).get("type") == "thinking":
                return [{"type": "thinking"}]
            elif et == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                if self._current_msg:
                    self._text_msgs.add(self._current_msg)
                return [{"type": "text", "text": ev["delta"].get("text", "")}]
            return []
        if t == "assistant":
            msg = obj.get("message", {})
            out: list[dict[str, Any]] = []
            for block in msg.get("content", []):
                if block.get("type") == "tool_use":
                    name = block.get("name", "")
                    out.append({"type": "tool", "name": friendly_tool_name(name),
                                "summary": tool_summary(name, block.get("input") or {}, self.cwd)})
                elif block.get("type") == "text" and msg.get("id") not in self._text_msgs and block.get("text"):
                    out.append({"type": "text", "text": block["text"]})
            return out
        if t == "result":
            if obj.get("is_error") or obj.get("subtype") != "success":
                return [{"type": "error", "message": str(obj.get("result") or obj.get("subtype"))[:1000]}]
            return [{"type": "result", "session_id": obj.get("session_id"), "cost_usd": obj.get("total_cost_usd"),
                     "duration_ms": obj.get("duration_ms")}]
        return []


@dataclass
class ChatProcess:
    proc: asyncio.subprocess.Process

    def stop(self) -> None:
        if self.proc.returncode is None:
            self.proc.terminate()


def chat_argv(model: str, *, mcp_config: str, system_file: Path, add_dirs: list[Path], ask_first: bool,
              session_id: str, resume: bool) -> list[str]:
    """Full Claude Code for the chat. With ask_first, anything off CHAT_AUTO_ALLOW is asked about in the app."""
    argv = ["claude", "-p", "--model", model, "--setting-sources", "user", "--mcp-config", mcp_config,
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            "--append-system-prompt-file", str(system_file), "--max-turns", "100"]
    if ask_first:
        argv += ["--permission-mode", "default", "--permission-prompts", "host", "--permission-prompt-tool", APPROVAL_TOOL]
    else:
        argv += ["--permission-mode", "bypassPermissions"]
    argv += ["--allowedTools", *CHAT_AUTO_ALLOW]
    for d in add_dirs:
        argv += ["--add-dir", str(d)]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    return argv


async def stream_chat(prompt: str, *, cwd: Path, session_id: str, resume: bool, model: str, system_file: Path,
                      add_dirs: list[Path], mcp_config: str, ask_first: bool = True,
                      on_start: Any = None) -> AsyncIterator[dict[str, Any]]:
    argv = chat_argv(model, mcp_config=mcp_config, system_file=system_file, add_dirs=add_dirs, ask_first=ask_first,
                     session_id=session_id, resume=resume)
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=claude_env(), limit=STREAM_LIMIT,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    if on_start:
        on_start(ChatProcess(proc))
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(prompt.encode())
    await proc.stdin.drain()
    proc.stdin.close()
    translator = StreamTranslator(cwd=cwd)
    saw_result = False
    try:
        async for raw in proc.stdout:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for ev in translator.feed(obj):
                saw_result = saw_result or ev["type"] in ("result", "error")
                yield ev
        code = await proc.wait()
        if not saw_result:
            err = (await proc.stderr.read()).decode(errors="replace").strip()
            yield {"type": "error", "message": "stopped" if code in (-15, 143) else (err[-800:] or f"claude exited {code}")}
    finally:
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
