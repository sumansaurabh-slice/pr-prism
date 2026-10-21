"""state.json: the reviewer's own data (reviewed files, drafts, chat threads). Survives re-analysis."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from .util import read_json, write_json

T = TypeVar("T")


def empty_state() -> dict[str, Any]:
    return {"reviewed": {}, "dismissed_suggestions": [], "drafts": [], "threads": [], "submitted": []}


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

    def load(self) -> dict[str, Any]:
        state = empty_state()
        state.update(read_json(self.path, {}) or {})
        return state

    def update(self, fn: Callable[[dict[str, Any]], T]) -> T:
        with self._locked():
            state = self.load()
            result = fn(state)
            write_json(self.path, state)
            return result
