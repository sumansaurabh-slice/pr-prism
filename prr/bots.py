"""Findings that other review tools (bot accounts) already posted on the PR, and Claude's second look at them.

They are shown next to Claude's own findings, Claude says whether it agrees with each, and a Claude finding
on the same spot is marked "also flagged by" so the reviewer doesn't read the same issue twice.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from . import findings
from .claude import ClaudeError, run_structured
from .ledger import Ledger
from .config import PROMPTS_DIR, Config
from .models import llm_schema

MAX_CHECKED = 25

_SEVERITY = [
    ("blocker", re.compile(r"⛔|\bcatastrophic\b|\bblocker\b", re.I)),
    ("major", re.compile(r"🔴|\bcritical\b|\bhigh\b|\bmajor\b|\bsevere\b", re.I)),
    ("minor", re.compile(r"🟡|🟠|\bwarning\b|\bmedium\b|\bmoderate\b|\bminor\b", re.I)),
    ("nit", re.compile(r"🟢|🔵|\bsuggestion\b|\blow\b|\bnit\b|\binfo\b", re.I)),
]
_BOLD = re.compile(r"\*\*([^*\n]{4,140})\*\*")
_WORD = re.compile(r"[a-z0-9]+")
_FILLER = {"a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "is", "are", "be", "it", "its",
           "this", "that", "not", "no", "but", "also", "s", "by", "from", "as", "can", "when", "does", "do"}


class Verdict(BaseModel):
    comment_id: int
    verdict: Literal["agree", "disagree", "unsure"]
    reason: str


class VerdictsOut(BaseModel):
    verdicts: list[Verdict]


def comment_title(body: str) -> str:
    """The comment's first bold phrase that isn't a severity label, else its first line, without markdown."""
    for m in _BOLD.finditer(body):
        text = m.group(1).strip()
        if not any(rx.fullmatch(text.strip(" ·:")) for _, rx in _SEVERITY) and len(text) > 8:
            return text[:120]
    first = next((ln.replace("**", "").strip(" #*>-") for ln in body.splitlines() if ln.strip(" #*>-")), "")
    return first[:120] or "Comment"


def _severity(body: str) -> str | None:
    """From the comment's first line only: titles like "Blocking call in async code" are not severities."""
    head = next((ln for ln in body.splitlines() if ln.strip()), "")[:200]
    return next((name for name, rx in _SEVERITY if rx.search(head)), None)


def from_comments(comments: list[dict]) -> list[dict]:
    """Root review comments written by bot accounts, as bot findings (the same comment repeated counts once)."""
    out = []
    seen: set[tuple] = set()
    for c in comments:
        if not c.get("is_bot") or c.get("in_reply_to_id"):
            continue
        body = c.get("body") or ""
        key = (c["user"], c["path"], c.get("line"), body.strip()[:300])
        if key in seen:
            continue
        seen.add(key)
        out.append({"comment_id": c["id"], "bot": c["user"], "path": c["path"], "line": c.get("line"),
                    "side": c.get("side"), "severity": _severity(body), "title": comment_title(body), "body": body[:4000],
                    "html_url": c.get("html_url", ""), "verdict": None, "verdict_reason": None})
    return out


def check(found: list[dict], cfg: Config, cwd: Path, cache: dict, echo: Callable[[str], None],
          ledger: Ledger | None = None) -> dict:
    """Claude's verdict on bot findings not yet in `cache` ({comment_id: {verdict, reason}})."""
    from .analyze import style_rules

    todo = [b for b in found if str(b["comment_id"]) not in cache and b["line"]][:MAX_CHECKED]
    if not todo:
        return cache
    prompt = (PROMPTS_DIR / "other_reviewers.md").read_text().replace("{{STYLE}}", style_rules()) + "\n---\n"
    for b in todo:
        prompt += (f"\n### comment_id {b['comment_id']} by {b['bot']}\nOn `{b['path']}:{b['line']}` ({b['side'] or 'RIGHT'})\n"
                   f"```\n{b['body'][:1500]}\n```\n")
    try:
        raw, _ = run_structured(prompt, llm_schema(VerdictsOut), cwd=cwd, model=cfg.models.ci, max_turns=25, timeout=900,
                                ledger=ledger, purpose="bots", target=f"{len(todo)} comments")
        ids = {b["comment_id"] for b in todo}
        for v in VerdictsOut.model_validate(raw).verdicts:
            if v.comment_id in ids:
                cache[str(v.comment_id)] = {"verdict": v.verdict, "reason": v.reason}
    except (ClaudeError, ValueError) as e:
        echo(f"Could not check the other review tools' comments: {str(e)[:150]}")
    return cache


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def same_spot(finding: dict, bot: dict) -> bool:
    """A Claude finding and a bot finding about the same issue: same file, within a few lines, similar words."""
    if bot["path"] != finding["path"]:
        return False
    title_a, title_b = _words(finding["title"]) - _FILLER, _words(bot["title"]) - _FILLER
    shared = title_a & title_b
    file_level = not bot["line"] or bot["line"] <= 1  # the tool couldn't place it on a line
    if file_level and len(shared) >= 3 and len(shared) / min(len(title_a), len(title_b)) >= 0.5:
        return True  # same issue in other words, posted at file level
    return findings.near_same(finding["path"], finding["line"], finding["title"] + " " + finding["impact"],
                              bot["path"], bot["line"], bot["title"] + " " + bot["body"][:600])
