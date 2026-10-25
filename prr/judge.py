"""Second look: a separate, cheaper model scores every finding against its code. It can only veto.

Findings scoring below MIN_SCORE (or marked "drop") are kept but hidden as "low confidence". The reviewer's own
"% sure" never hides a finding by itself: the second look decides. If the judge fails, the finding is kept and
marked "not second-checked" (a failure marker in judge.json), and it is tried again on the next run: the second
look must never silently remove a real problem.

The judge has no tools. For a deep-check finding it also gets the scenario and the code around each spot the finding
cites, which Python reads from the worktree; a spot that doesn't exist is shown as "evidence not found".

A question for the author (category `intent`) is scored on another question: is the change real, and do the PR's
title, description, commit messages and code leave it unexplained? Those findings go in their own batches, which
also get the PR's text. Their verdict keeps a hash of that text (`pr`), so an edited description scores them again.
"""

from __future__ import annotations

import hashlib
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .claude import ClaudeError, run_structured
from .config import PROMPTS_DIR, Config
from .diffparse import FileDiff, render_for_llm
from .findings import INTENT
from .ledger import Ledger
from .models import llm_schema
from .util import now_iso

MIN_SCORE = 5  # hide only findings the second look rejects; it only sees the hunk, so "can't confirm" (5-7) stays shown
BATCH = 12
MAX_CODE_CHARS = 4000
WINDOW = 8              # lines either side of a spot a deep-check finding cites
MAX_WINDOW_LINE = 300   # characters kept per line in those windows
MAX_PR_BODY = 6000      # characters of the PR description shown with questions for the author
MAX_COMMITS = 40        # commit messages shown with them


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


def evidence_window(cwd: Path | None, path: str, line: int) -> str | None:
    """The lines around one spot a finding cites (>> marks it), read from the PR's code by Python, not by the model.
    None when the file is missing, the line is past its end, or the path leads outside the worktree."""
    if cwd is None:
        return None
    root = os.path.realpath(cwd)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep):
        return None
    try:
        lines = Path(full).read_text(errors="replace").splitlines()
    except (OSError, ValueError):
        return None
    if not 1 <= line <= len(lines):
        return None
    out = []
    for n in range(max(1, line - WINDOW), min(len(lines), line + WINDOW) + 1):
        text = lines[n - 1] if len(lines[n - 1]) <= MAX_WINDOW_LINE else lines[n - 1][:MAX_WINDOW_LINE] + " …"
        out.append(f"{'>>' if n == line else '  '} {n:>5}  {text}")
    return "\n".join(out)


def _evidence(f: dict, cwd: Path | None) -> str:
    refs = f.get("evidence_refs") or []
    if not refs:
        return "(no spots cited)"
    out = []
    for r in refs:
        window = evidence_window(cwd, r["path"], r["line"])
        out.append(f"`{r['path']}:{r['line']}`\n```\n{window}\n```" if window
                   else f"`{r['path']}:{r['line']}`: evidence not found (no such file or line)")
    return "\n".join(out)


def pr_text(pr: dict | None) -> str:
    """The PR's title, description and commit messages: what a question for the author is checked against."""
    if not pr:
        return "(The PR's title and description are not available.)"
    body = (pr.get("body") or "").strip() or "(no description)"
    if len(body) > MAX_PR_BODY:
        body = body[:MAX_PR_BODY] + "\n…[description cut]"
    commits = "\n".join(f"- {c.get('headline', '')}" for c in (pr.get("commits") or [])[:MAX_COMMITS]) or "(none)"
    return f"Title: {pr.get('title') or ''}\n\nDescription:\n{body}\n\nCommit messages:\n{commits}"


def pr_key(pr: dict | None) -> str | None:
    """A short hash of pr_text: a question's verdict is stale once the PR's text changes."""
    return hashlib.sha1(pr_text(pr).encode()).hexdigest()[:12] if pr else None


def _render(f: dict, diffs: dict[str, FileDiff], cwd: Path | None = None) -> str:
    parts = [f"### {f['id']}", f"File: {f['path']}, line {f['line']} ({f['side']})",
             f"Severity: {f['severity']} · {f['category']}", f"Title: {f['title']}", f"Impact: {f['impact']}",
             f"Fix: {f['fix']}"]
    if f.get("category") == INTENT:
        parts.insert(1, 'Kind: a question for the author, not a problem report (see "Questions for the author")')
    if f.get("suggestion"):
        parts.append(f"Suggested code:\n```\n{f['suggestion']}\n```")
    parts += [f"Why: {f['why']}", f"Evidence: {f['evidence'] or 'none given'}",
              f"Code (>> marks the line the finding is on):\n```\n{code_around(f, diffs)}\n```"]
    if f.get("scenario") or f.get("evidence_refs"):  # from the deep check
        steps = "\n".join(f"{i}. {step}" for i, step in enumerate(f.get("scenario") or [], 1))
        parts.append("How it could happen:\n" + (steps or "(no steps given)"))
        if f.get("question"):
            parts.append(f"Question for the reviewer: {f['question']}")
        parts.append("Code at the spots it cites:\n" + _evidence(f, cwd))
    return "\n".join(parts)


def _stale(f: dict, verdict: dict, key: str | None) -> bool:
    """A question for the author that was scored against an older title, description or commit list."""
    return f.get("category") == INTENT and key is not None and verdict.get("pr") != key


def run(findings: list[dict], diffs: dict[str, FileDiff], cfg: Config, cwd: Path, cache: dict,
        echo: Callable[[str], None], ledger: Ledger | None = None, pr: dict | None = None) -> dict:
    """Score findings not yet in `cache` ({id: {score, verdict, reason}}). Returns the updated cache.

    A finding the judge could not score gets a failure marker, {id: {failed: true, reason, at}}, so the app can
    tell "not second-checked" from "waiting for its second look". Marked findings are tried again next run.
    `pr` (context.json's pr) is what questions for the author are checked against; their verdicts keep its hash.
    """
    from .analyze import style_rules

    key = pr_key(pr)
    todo = [f for f in findings if f["severity"] != "blocker"
            and (f["id"] not in cache or failed(cache[f["id"]]) or _stale(f, cache[f["id"]], key))]
    if not todo:
        return cache
    head = (PROMPTS_DIR / "judge.md").read_text().replace("{{STYLE}}", style_rules())
    asks = [f for f in todo if f.get("category") == INTENT]
    rest = [f for f in todo if f.get("category") != INTENT]
    batches = [group[i:i + BATCH] for group in (rest, asks) for i in range(0, len(group), BATCH)]

    def one(batch: list[dict]) -> list[Score]:
        about_pr = ("## The pull request (for the questions to the author)\n\n" + pr_text(pr) + "\n\n---\n\n"
                    if any(f.get("category") == INTENT for f in batch) else "")
        prompt = head + "\n---\n\n" + about_pr + "\n\n".join(_render(f, diffs, cwd) for f in batch)
        raw, _ = run_structured(prompt, llm_schema(JudgeOut), cwd=cwd, model=cfg.models.judge, tools=[],
                                max_turns=3, timeout=300, ledger=ledger, purpose="judge",
                                target=",".join(f["id"] for f in batch))
        return JudgeOut.model_validate(raw).scores

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(one, b): b for b in batches}
        for fut in as_completed(futures):
            batch = futures[fut]
            wanted = {f["id"] for f in batch}
            asked = {f["id"] for f in batch if f.get("category") == INTENT}
            reason = "the second look gave no score for it"
            try:
                for s in fut.result():
                    if s.id in wanted:
                        cache[s.id] = {"score": max(1, min(10, s.score)), "verdict": s.verdict, "reason": s.reason}
                        if s.id in asked and key:
                            cache[s.id]["pr"] = key
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
