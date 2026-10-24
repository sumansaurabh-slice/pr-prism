"""calls.jsonl: one line per Claude call in a PR folder. Every cost total is computed from this file.

A call line is {at, run_id, purpose, model, target, duration_ms, cost_usd, outcome, error}:
- run_id: the analysis run that made the call, or null for the server (chat, review summary, CI refresh);
- purpose: annotate | blockers | judge | deepcheck | ci | bots | recheck | synthesis | chat | summary;
- cost_usd: null when the cost is unknown (a timeout, a crash, a chat result without a cost);
- outcome: ok | error | refused | malformed | timeout.

Lines without a `purpose` (approval decisions, for example) are not calls and never count toward a total.

The analysis threads, the server and the chat hook all append to the same file, so each line goes out in a single
O_APPEND write: concurrent writers never interleave inside a line. Stdlib only, so the hook can import it cheaply.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CALLS_FILE = "calls.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_run_id() -> str:
    """Sorts by start time; the suffix keeps two runs started in the same second apart."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]


def append(pr_dir: Path, entry: dict[str, Any]) -> None:
    """Add one line to the PR's calls.jsonl in a single O_APPEND write."""
    data = (json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    fd = os.open(pr_dir / CALLS_FILE, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def read(pr_dir: Path) -> list[dict[str, Any]]:
    """Every line of the PR's calls.jsonl (a damaged line is skipped)."""
    try:
        text = (pr_dir / CALLS_FILE).read_text(errors="replace")
    except FileNotFoundError:
        return []
    out = []
    for line in text.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


@dataclass(frozen=True)
class Ledger:
    """Where calls are recorded: the PR folder, plus the analysis run's id (None for the server's own calls)."""

    pr_dir: Path
    run_id: str | None = None

    def record(self, purpose: str, *, model: str | None, target: str = "", duration_ms: int | None = None,
               cost_usd: float | None = None, outcome: str = "ok", error: str | None = None,
               **extra: Any) -> dict[str, Any]:
        entry = {"at": _now(), "run_id": self.run_id, "purpose": purpose, "model": model, "target": target,
                 "duration_ms": duration_ms, "cost_usd": cost_usd, "outcome": outcome,
                 "error": error[:300] if error else None, **extra}
        try:
            append(self.pr_dir, entry)
        except OSError as e:  # a full disk must not break the review; the cost is lost, so say so
            print(f"[prr] could not record a Claude call's cost in {CALLS_FILE}: {e}", file=sys.stderr, flush=True)
        return entry
