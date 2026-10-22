"""Second look: a separate, cheaper model scores every finding against its code. It can only veto.

Findings scoring below MIN_SCORE (or marked "drop") are kept but hidden as "low confidence". If the judge
fails, the finding is kept: the second look must never silently remove a real problem.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .claude import ClaudeError, run_structured
from .config import PROMPTS_DIR, Config
from .diffparse import FileDiff, render_for_llm
from .models import llm_schema

MIN_SCORE = 5  # hide only findings the second look rejects; it only sees the hunk, so "can't confirm" (5-7) stays shown
BATCH = 12
MAX_CODE_CHARS = 4000


class Score(BaseModel):
    id: str
    score: int
    verdict: Literal["keep", "drop"]
    reason: str


class JudgeOut(BaseModel):
    scores: list[Score]


def code_around(f: dict, diffs: dict[str, FileDiff]) -> str:
    """The hunk the finding sits in, with its line marked by >>."""
    fd = diffs.get(f["path"])
    if fd is None or not fd.hunks:
        return "(no text diff for this file)"
    h = fd.hunk_for(f["side"], f["line"]) or fd.hunks[0]
    tag = f"{'R' if f['side'] == 'RIGHT' else 'L'}{f['line']}"
    lines = [(">> " if ln.strip().startswith(tag + " ") else "   ") + ln for ln in render_for_llm(fd, {h.id}).split("\n")]
    return "\n".join(lines)[:MAX_CODE_CHARS]


def _render(f: dict, diffs: dict[str, FileDiff]) -> str:
    parts = [f"### {f['id']}", f"File: {f['path']}, line {f['line']} ({f['side']})",
             f"Severity: {f['severity']} · {f['category']}", f"Title: {f['title']}", f"Impact: {f['impact']}",
             f"Fix: {f['fix']}"]
    if f.get("suggestion"):
        parts.append(f"Suggested code:\n```\n{f['suggestion']}\n```")
    parts += [f"Why: {f['why']}", f"Evidence: {f['evidence'] or 'none given'}",
              f"Code (>> marks the line the finding is on):\n```\n{code_around(f, diffs)}\n```"]
    return "\n".join(parts)


def run(findings: list[dict], diffs: dict[str, FileDiff], cfg: Config, cwd: Path, cache: dict,
        echo: Callable[[str], None]) -> dict:
    """Score findings not yet in `cache` ({id: {score, verdict, reason}}). Returns the updated cache."""
    from .analyze import style_rules

    todo = [f for f in findings if f["severity"] != "blocker" and f["id"] not in cache]
    if not todo:
        return cache
    head = (PROMPTS_DIR / "judge.md").read_text().replace("{{STYLE}}", style_rules())
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def one(batch: list[dict]) -> list[Score]:
        prompt = head + "\n---\n\n" + "\n\n".join(_render(f, diffs) for f in batch)
        raw, _ = run_structured(prompt, llm_schema(JudgeOut), cwd=cwd, model=cfg.models.judge, tools=[],
                                max_turns=3, timeout=300)
        return JudgeOut.model_validate(raw).scores

    wanted = {f["id"] for f in todo}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(one, b) for b in batches]
        for fut in as_completed(futures):
            try:
                for s in fut.result():
                    if s.id in wanted:
                        cache[s.id] = {"score": max(1, min(10, s.score)), "verdict": s.verdict, "reason": s.reason}
            except (ClaudeError, ValueError) as e:
                echo(f"The second look failed for some findings, so they stay as they are: {str(e)[:150]}")
    return cache


def is_low(f: dict, verdict: dict | None) -> bool:
    if f["severity"] == "blocker":
        return False
    if verdict and (verdict["score"] < MIN_SCORE or verdict["verdict"] == "drop"):
        return True
    return f.get("confidence") is not None and f["confidence"] < 50
