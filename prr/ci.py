"""CI results for the PR's head commit: GitHub check runs and commit statuses (Jenkins and similar tools report
through statuses), annotations on files, and the tail of failing GitHub Actions job logs. Failures are then
explained by Claude against the code, so the reviewer knows whether the PR caused them.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel

from .claude import ClaudeError, run_structured
from .config import PROMPTS_DIR, Config
from .gitgh import gh
from .models import llm_schema
from .util import now_iso

MAX_EXPLAINED = 4       # failing checks explained per run
LOG_TAIL_LINES = 250
MAX_LOG_CHARS = 25_000
MAX_ANNOTATIONS = 60

_JOB_URL = re.compile(r"/actions/runs/\d+/job(?:s)?/(\d+)")
_TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?", re.M)
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Related(BaseModel):
    path: str
    line: int | None = None


class Explanation(BaseModel):
    summary: str
    cause: str
    caused_by_pr: Literal["yes", "no", "unclear"]
    related: list[Related]
    fix: str


def _lines(out: str) -> list[dict]:
    return [json.loads(x) for x in out.splitlines() if x.strip()]


def _check_state(status: str, conclusion: str | None) -> str:
    if status != "completed":
        return "pending"
    return {"success": "pass", "skipped": "skipped", "neutral": "neutral", "stale": "neutral"}.get(conclusion or "", "fail")


def _status_state(state: str) -> str:
    return {"success": "pass", "pending": "pending"}.get(state, "fail")


def _job_log(owner: str, repo: str, url: str | None) -> str | None:
    m = _JOB_URL.search(url or "")
    if not m:
        return None
    try:
        raw = gh("api", f"repos/{owner}/{repo}/actions/jobs/{m.group(1)}/logs")
    except Exception:  # noqa: BLE001 — logs are a bonus; the check status is still shown
        return None
    text = _ANSI.sub("", _TIMESTAMP.sub("", raw))
    tail = "\n".join(text.splitlines()[-LOG_TAIL_LINES:])
    return tail[-MAX_LOG_CHARS:]


def fetch(owner: str, repo: str, head_sha: str) -> dict:
    """{fetched_at, head_sha, checks: [...], annotations: [...]} for the head commit."""
    checks: list[dict] = []
    annotations: list[dict] = []
    runs = _lines(gh("api", "--paginate", f"repos/{owner}/{repo}/commits/{head_sha}/check-runs?per_page=100",
                     "--jq", ".check_runs[] | {id, name, status, conclusion, html_url, details_url, app: .app.slug, "
                     "title: .output.title, summary: .output.summary, annotations: .output.annotations_count}"))
    latest: dict[str, dict] = {}
    for r in runs:  # a re-run creates a new check run with the same name; keep the newest (highest id)
        if r["name"] not in latest or r["id"] > latest[r["name"]]["id"]:
            latest[r["name"]] = r
    for r in latest.values():
        state = _check_state(r["status"], r["conclusion"])
        summary = "\n\n".join(x for x in (r.get("title"), (r.get("summary") or "")[:2000]) if x) or None
        check = {"name": r["name"], "kind": "check", "state": state, "url": r.get("details_url") or r.get("html_url"),
                 "app": r.get("app"), "summary": summary, "log_tail": None, "explanation": None, "_id": r["id"]}
        if state == "fail":
            if r.get("app") == "github-actions":
                check["log_tail"] = _job_log(owner, repo, check["url"])
            if r.get("annotations"):
                try:
                    for a in _lines(gh("api", f"repos/{owner}/{repo}/check-runs/{r['id']}/annotations?per_page=50",
                                       "--jq", ".[] | {path, start_line, end_line, annotation_level, message, title}")):
                        if len(annotations) < MAX_ANNOTATIONS and a.get("path") and not a["path"].startswith(".github"):
                            annotations.append({"check": r["name"], "path": a["path"], "line": a.get("start_line") or 1,
                                                "end_line": a.get("end_line"), "level": a.get("annotation_level") or "notice",
                                                "message": ((a.get("title") + ": ") if a.get("title") else "") + (a.get("message") or "")})
                except Exception:  # noqa: BLE001
                    pass
        checks.append(check)
    combined = json.loads(gh("api", f"repos/{owner}/{repo}/commits/{head_sha}/status") or "{}")
    for s in combined.get("statuses", []):
        checks.append({"name": s.get("context") or "status", "kind": "status", "state": _status_state(s.get("state", "")),
                       "url": s.get("target_url"), "app": None, "summary": s.get("description"), "log_tail": None,
                       "explanation": None})
    order = {"fail": 0, "pending": 1, "pass": 2, "neutral": 3, "skipped": 4}
    checks.sort(key=lambda c: (order.get(c["state"], 9), c["name"].lower()))
    return {"fetched_at": now_iso(), "head_sha": head_sha, "checks": checks, "annotations": annotations}


def explain_failures(ci: dict, ctx: dict, cfg: Config, cwd: Path, echo: Callable[[str], None]) -> dict:
    """Ask Claude why each failing check failed (up to MAX_EXPLAINED), in parallel."""
    from .analyze import style_rules

    failing = [c for c in ci["checks"] if c["state"] == "fail"][:MAX_EXPLAINED]
    if not failing:
        return ci
    head = (PROMPTS_DIR / "ci.md").read_text().replace("{{STYLE}}", style_rules())
    changed = "\n".join(f"- {f['path']}" for f in ctx["files"][:300])

    def one(check: dict) -> None:
        notes = [a for a in ci["annotations"] if a["check"] == check["name"]][:30]
        prompt = (head + f"\n---\n\n## The failing check\n\nName: {check['name']}\nTool: {check.get('app') or check['kind']}\n"
                  f"Summary: {check.get('summary') or '(none)'}\n")
        if notes:
            prompt += "\n## Its notes on files\n" + "\n".join(f"- {a['path']}:{a['line']} ({a['level']}) {a['message'][:300]}" for a in notes)
        if check.get("log_tail"):
            prompt += f"\n\n## The end of its log\n```\n{check['log_tail']}\n```"
        if not notes and not check.get("log_tail"):
            prompt += "\n\nThere is no log or file note for this check. Explain what you can from its name and summary."
        prompt += f"\n\n## Files this PR changes\n{changed}\n"
        try:
            raw, _ = run_structured(prompt, llm_schema(Explanation), cwd=cwd, model=cfg.models.ci, max_turns=12, timeout=600)
            check["explanation"] = Explanation.model_validate(raw).model_dump()
        except (ClaudeError, ValueError) as e:
            echo(f"Could not explain the failing check {check['name']}: {str(e)[:150]}")

    with ThreadPoolExecutor(max_workers=MAX_EXPLAINED) as pool:
        list(pool.map(one, failing))
    return ci


def public(ci: dict | None) -> dict | None:
    """ci.json without internal fields, as the web app gets it."""
    if not ci:
        return None
    return {**ci, "checks": [{k: v for k, v in c.items() if not k.startswith("_")} for c in ci["checks"]]}
