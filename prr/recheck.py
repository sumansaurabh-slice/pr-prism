"""After new commits: are earlier problems fixed?

Checked for (a) findings from the previous review round and (b) review comments on the PR that GitHub now
marks as outdated. In order, for each one:
  1. its file left the PR or was deleted → fixed;
  2. its exact line is still in the file → still open (no model call: the code didn't change there);
  3. otherwise Claude compares old and new code and says "fixed" only with positive evidence (≥ 80% sure).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .bots import comment_title
from .claude import ClaudeError, run_structured
from .ledger import Ledger
from .config import PROMPTS_DIR, Config
from .findings import norm
from .models import llm_schema
from .util import now_iso

MAX_CHECKED = 20
MIN_FIXED_CONFIDENCE = 80
WINDOW_BEFORE, WINDOW_AFTER = 25, 40
MIN_SUBSTRING_ANCHOR = 20


class Check(BaseModel):
    id: str
    fixed: bool
    confidence: int
    reason: str


class ChecksOut(BaseModel):
    checks: list[Check]


def window(lines: list[str], line: int) -> str:
    lo, hi = max(0, line - 1 - WINDOW_BEFORE), min(len(lines), line + WINDOW_AFTER)
    return "\n".join(f"{n + 1:>5}  {lines[n]}" for n in range(lo, hi))


def history_entry(f: dict, head_lines: list[str]) -> dict:
    """What to remember about a finding so the next round can re-check it."""
    return {"id": f["id"], "kind": "finding", "title": f["title"], "path": f["path"], "side": f["side"], "line": f["line"],
            "anchor": f["anchor"], "severity": f["severity"], "category": f["category"],
            "old_window": window(head_lines, f["line"]) if f["side"] == "RIGHT" else "", "author": None, "html_url": None}


def comment_entry(c: dict) -> dict | None:
    """An outdated review comment as a re-check item; its anchor is the commented line of its diff hunk."""
    hunk = c.get("diff_hunk") or ""
    last = next((ln for ln in reversed(hunk.splitlines()) if ln and not ln.startswith("@@")), "")
    if not last:
        return None
    body = (c.get("body") or "").strip()
    if c.get("is_bot"):
        title = comment_title(body)
    else:
        title = next((ln.replace("**", "").strip(" #*>-") for ln in body.splitlines() if ln.strip(" #*>-")), "Comment")[:120]
    return {"id": f"c-{c['id']}", "kind": "comment", "title": title, "path": c["path"], "side": c.get("side") or "RIGHT",
            "line": c.get("original_line") or 0, "anchor": last[1:] if last[:1] in "+- " else last,
            "severity": None, "category": None, "old_window": hunk[-3000:], "author": c.get("user"),
            "html_url": c.get("html_url")}


def _present(anchor: str, head_lines: list[str]) -> bool:
    want = norm(anchor)
    if not want:
        return False
    if any(norm(x) == want for x in head_lines):
        return True
    return len(want) >= MIN_SUBSTRING_ANCHOR and any(want in norm(x) for x in head_lines)


def run(items: list[dict], ctx: dict, worktree: Path, cfg: Config, cache: dict, echo: Callable[[str], None],
        ledger: Ledger | None = None) -> dict:
    """{item id: {status, how, ...item}} for every item, reusing `cache` for items already decided at this head."""
    from .analyze import style_rules

    head_sha = ctx["pr"]["head_sha"]
    files = {f["path"]: f for f in ctx["files"]}
    out: dict[str, dict] = {}
    ask: list[dict] = []
    for it in items:
        key = hashlib.sha1(f"{it['id']}|{head_sha}".encode()).hexdigest()[:16]
        if key in cache:
            out[it["id"]] = {**cache[key], "title": it["title"]}
            continue
        fc = files.get(it["path"])
        path = worktree / it["path"]
        if fc is None or fc["status"] == "deleted" or not path.exists():
            how = "the file was deleted" if path.exists() is False else "the file is no longer changed by this PR"
            out[it["id"]] = cache[key] = {**it, "status": "fixed", "how": how}
            continue
        head_lines = path.read_text(errors="replace").splitlines()
        if it["side"] == "RIGHT" and _present(it["anchor"], head_lines):
            out[it["id"]] = cache[key] = {**it, "status": "still-open", "how": "the same line is still in the code"}
            continue
        ask.append({**it, "_key": key, "_new_window": window(head_lines, it["line"] or 1)})

    if ask:
        prompt = (PROMPTS_DIR / "recheck.md").read_text().replace("{{STYLE}}", style_rules()) + "\n---\n"
        for it in ask[:MAX_CHECKED]:
            prompt += (f"\n### {it['id']}\nFile: {it['path']}\nThe earlier problem: {it['title']}\nThe line it was on: "
                       f"`{it['anchor'].strip()}`\n\nOld code:\n```\n{it['old_window'] or '(not saved)'}\n```\n\n"
                       f"New code near the same place:\n```\n{it['_new_window']}\n```\n")
        decided: dict[str, Check] = {}
        try:
            raw, _ = run_structured(prompt, llm_schema(ChecksOut), cwd=worktree, model=cfg.models.ci, tools=[],
                                    max_turns=3, timeout=600, ledger=ledger, purpose="recheck",
                                    target=f"{len(ask[:MAX_CHECKED])} earlier problems")
            decided = {c.id: c for c in ChecksOut.model_validate(raw).checks}
        except (ClaudeError, ValueError) as e:
            echo(f"Could not re-check earlier findings: {str(e)[:150]}")
        for it in ask:
            c = decided.get(it["id"])
            clean = {k: v for k, v in it.items() if not k.startswith("_")}
            if c and c.fixed and c.confidence >= MIN_FIXED_CONFIDENCE:
                res = {**clean, "status": "fixed", "how": c.reason}
            else:
                res = {**clean, "status": "still-open",
                       "how": c.reason if c else "the code changed here, but it's not clear the problem is fixed"}
            out[it["id"]] = res
            if c:  # only cache real answers, so a failed check is retried next time
                cache[it["_key"]] = res
    cache["_checked_at"] = now_iso()
    return out
