"""Second look: a separate, cheaper model scores every finding against its code. It can only veto.

Findings scoring below MIN_SCORE (or marked "drop") are kept but hidden as "low confidence". The reviewer's own
"% sure" never hides a finding by itself: the second look decides. If the judge fails, the finding is kept and
marked "not second-checked" (a failure marker in judge.json), and it is tried again on the next run: the second
look must never silently remove a real problem.
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
from .util import now_iso

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
    """Score findings not yet in `cache` ({id: {score, verdict, reason}}). Returns the updated cache.

    A finding the judge could not score gets a failure marker, {id: {failed: true, reason, at}}, so the app can
    tell "not second-checked" from "waiting for its second look". Marked findings are tried again next run.
    """
    from .analyze import style_rules

    todo = [f for f in findings if f["severity"] != "blocker" and (f["id"] not in cache or failed(cache[f["id"]]))]
    if not todo:
        return cache
    head = (PROMPTS_DIR / "judge.md").read_text().replace("{{STYLE}}", style_rules())
    batches = [todo[i:i + BATCH] for i in range(0, len(todo), BATCH)]

    def one(batch: list[dict]) -> list[Score]:
        prompt = head + "\n---\n\n" + "\n\n".join(_render(f, diffs) for f in batch)
        raw, _ = run_structured(prompt, llm_schema(JudgeOut), cwd=cwd, model=cfg.models.judge, tools=[],
                                max_turns=3, timeout=300)
        return JudgeOut.model_validate(raw).scores

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            wanted = {f["id"] for f in batch}
            reason = "the second look gave no score for it"
            try:
                for s in fut.result():
                    if s.id in wanted:
                        cache[s.id] = {"score": max(1, min(10, s.score)), "verdict": s.verdict, "reason": s.reason}
                        wanted.discard(s.id)
            except (ClaudeError, ValueError) as e:
                reason = str(e)[:300]
                echo(f"The second look failed for some findings, so they stay shown, marked not second-checked: "
                     f"{str(e)[:150]}")
            for fid in wanted:
                cache[fid] = {"failed": True, "reason": reason, "at": now_iso()}
    return cache


def failed(verdict: dict | None) -> bool:
    """A failure marker: the judge was asked but gave no verdict."""
    return bool(verdict and verdict.get("failed"))


def is_low(f: dict, verdict: dict | None, floor: int | None = None) -> bool:
    """Hidden as low confidence: the second look said drop or scored it under MIN_SCORE.

    The reviewer's own confidence counts only when `floor` is given (eval compares with the old rule); normal runs
    never pass it. A failed second look never hides anything.
    """
    if f["severity"] == "blocker":
        return False
    if verdict and not failed(verdict) and (verdict["score"] < MIN_SCORE or verdict["verdict"] == "drop"):
        return True
    return floor is not None and f.get("confidence") is not None and f["confidence"] < floor


def second_look(f: dict, verdict: dict | None, *, carried: bool = False, done: bool = False) -> str:
    """Where a finding's second look stands: judge | rule | carried | pending | none.

    `done` means the analysis run has finished, so a finding with no verdict and no marker (an analysis from
    before failure markers) was never scored: "none", not "pending".
    """
    if f["severity"] == "blocker":
        return "rule"
    if carried:
        return "carried"
    if failed(verdict):
        return "none"
    if verdict:
        return "judge"
    return "none" if done else "pending"
