"""state.json: the reviewer's own data (reviewed files, drafts, chat threads, guided-review answers, seen hunks,
the review summary draft). Survives re-analysis."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from . import cost, ledger
from .util import read_json, write_json

T = TypeVar("T")

# 2: each chat answer stores its own cost (version 1 stored the session's running total; see cost.upgrade_threads)
STATE_VERSION = 2


def empty_state() -> dict[str, Any]:
    return {"version": STATE_VERSION, "reviewed": {}, "dismissed_suggestions": [], "drafts": [], "threads": [],
            "submitted": [], "stop_verdicts": {}, "seen_hunks": {}, "review_body_draft": ""}


class StateStore:
    """Read-modify-write under an flock, so the server and `prr fetch` (draft remapping) can't clobber each other."""

    def __init__(self, pr_dir: Path):
        self.path = pr_dir / "state.json"
        self.lock_path = pr_dir / ".state.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def read(self) -> tuple[dict[str, Any], list[dict]]:
        """(state, calls.jsonl lines an upgrade from an older state.json owes). Upgrades in memory only."""
        raw = read_json(self.path, {}) or {}
        state = empty_state()
        state.update(raw)
        owed: list[dict] = []
        if raw and raw.get("version", 1) < STATE_VERSION:
            owed = cost.upgrade_threads(state["threads"])
            state["version"] = STATE_VERSION
        return state, owed

    def load(self) -> dict[str, Any]:
        """The current state, upgraded in memory if it is from an older version. Never writes."""
        return self.read()[0]

    def update(self, fn: Callable[[dict[str, Any]], T]) -> T:
        """The first update after an upgrade writes the upgraded state back (once: it then has the new version)."""
        with self._locked():
            state, owed = self.read()
            if owed:  # ledger first: a retried upgrade skips threads already recorded, so nothing counts twice
                for line in cost.not_yet_recorded(ledger.read(self.path.parent), owed):
                    ledger.append(self.path.parent, line)
            result = fn(state)
            write_json(self.path, state)
            return result
