"""Keeping the reviewer's work when `prr clean` removes a PR folder, and bringing it back on the next fetch.

Before `prr clean` deletes anything, it copies the PR's state.json (chats, pending comments, viewed marks…)
and its calls.jsonl cost ledger here:

    <cache>/archive/<owner>__<repo>/pr-<n>-<UTC>.json          (state.json)
    <cache>/archive/<owner>__<repo>/pr-<n>-<UTC>.calls.jsonl   (calls.jsonl, when the PR has one)

`prr fetch` into a fresh PR folder restores the newest archive:
- a pending comment made on the commit being fetched comes back as it was;
- any other pending comment comes back outdated: on its old line, read-only and never sent, until the
  reviewer re-places it (POST /api/drafts/{id}/replace) or deletes it. It is never moved silently.
The state gets a one-time `restored` summary for the app's banner.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config
from .state import StateStore
from .util import now_iso, read_json

# state.json keys holding the reviewer's work: chat threads, pending comments, stop verdicts, reviewed stop
# ids, submitted reviews, viewed marks, dismissed findings, seen hunks, the review body draft and cost totals.
# A PR whose keys are all empty (and that has no cost ledger) is not archived. New kinds of work go here.
WORK_FIELDS = (
    "threads", "drafts", "stop_verdicts", "reviewed_stop_ids", "submitted", "reviewed", "dismissed_suggestions",
    "seen_hunks", "review_body_draft", "analysis_cost_total", "server_cost_usd",
)
LEDGER = "calls.jsonl"
_NAME = re.compile(r"^pr-(\d+)-(\d{8}T\d{6}Z)(?:-(\d+))?\.(json|calls\.jsonl)$")
_STAMP = "%Y%m%dT%H%M%SZ"


def archive_dir(cfg: Config, owner: str, repo: str) -> Path:
    return cfg.cache_dir / "archive" / f"{owner}__{repo}"


def _write_private(path: Path, data: bytes) -> None:
    """Atomic write of a file only the user can read (0600, like state.json)."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _has_work(raw: bytes) -> bool:
    if not raw.strip():
        return False
    try:
        state = json.loads(raw)
    except ValueError:
        return True  # unreadable, so it can't be shown to be empty: keep it rather than delete it
    return not isinstance(state, dict) or any(state.get(k) for k in WORK_FIELDS)


def counts(state: dict[str, Any]) -> dict[str, int]:
    drafts = state.get("drafts") or []
    return {
        "chats": len(state.get("threads") or []),
        "pending_comments": len(drafts),
        "need_replacing": sum(1 for d in drafts if d.get("outdated")),
        "viewed_marks": len(state.get("reviewed") or {}),
    }


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def describe(c: dict[str, int]) -> str:
    """'3 chats, 5 pending comments (2 need re-placing), 12 viewed marks'; empty parts are left out."""
    parts = []
    if c.get("chats"):
        parts.append(_plural(c["chats"], "chat"))
    if c.get("pending_comments"):
        replace = c.get("need_replacing") or 0
        extra = f" ({replace} need{'s' if replace == 1 else ''} re-placing)" if replace else ""
        parts.append(_plural(c["pending_comments"], "pending comment") + extra)
    if c.get("viewed_marks"):
        parts.append(_plural(c["viewed_marks"], "viewed mark"))
    return ", ".join(parts)


def message(summary: dict[str, Any]) -> str:
    """The restore banner: 'Restored your work from Oct 23: 3 chats, 5 pending comments (2 need re-placing), …'."""
    when = datetime.fromisoformat(summary["archived_at"]).astimezone()
    what = describe(summary)
    return f"Restored your work from {when:%b} {when.day}" + (f": {what}." if what else ".")


def _entries(cfg: Config, owner: str, repo: str) -> list[tuple[int, str, int, Path]]:
    d = archive_dir(cfg, owner, repo)
    found = []
    for p in d.iterdir() if d.is_dir() else []:
        m = _NAME.match(p.name)
        if m:
            found.append((int(m.group(1)), m.group(2), int(m.group(3) or 1), p))
    return found


def archives(cfg: Config, owner: str, repo: str, number: int) -> list[Path]:
    """This PR's saved states, newest first."""
    mine = [(stamp, seq, p) for n, stamp, seq, p in _entries(cfg, owner, repo) if n == number and p.suffix == ".json"]
    return [p for *_, p in sorted(mine, reverse=True)]


def archived_numbers(cfg: Config, owner: str, repo: str) -> set[int]:
    return {n for n, *_ in _entries(cfg, owner, repo)}


def ledger_for(state_archive: Path) -> Path:
    return state_archive.with_name(state_archive.name.removesuffix(".json") + ".calls.jsonl")


def save(cfg: Config, owner: str, repo: str, number: int, pr_dir: Path) -> dict[str, Any] | None:
    """Copy the PR's work out of its folder before it is deleted.

    Returns {path, ledger, counts}, or None when there is no work to keep. Raises OSError when the copy fails;
    the caller must then leave the PR folder alone.
    """
    state_path, ledger_path = pr_dir / "state.json", pr_dir / LEDGER
    raw = state_path.read_bytes() if state_path.exists() else b""
    ledger = ledger_path.read_bytes() if ledger_path.exists() else b""
    if not _has_work(raw) and not ledger.strip():
        return None
    d = archive_dir(cfg, owner, repo)
    stamp = datetime.now(timezone.utc).strftime(_STAMP)
    base, seq = f"pr-{number}-{stamp}", 1
    while (d / f"{base}.json").exists() or (d / f"{base}.calls.jsonl").exists():  # cleaned twice in one second
        seq += 1
        base = f"pr-{number}-{stamp}-{seq}"
    path = d / f"{base}.json"
    written: list[Path] = []
    try:
        if ledger.strip():
            _write_private(ledger_for(path), ledger)
            written.append(ledger_for(path))
        _write_private(path, raw if raw.strip() else b"{}\n")
        written.append(path)
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    state = read_json(path)
    return {"path": path, "ledger": ledger_for(path) if ledger.strip() else None,
            "counts": counts(state if isinstance(state, dict) else {})}


def purge(cfg: Config, owner: str, repo: str, number: int) -> list[Path]:
    """Delete every saved state and ledger of this PR. Returns what was removed."""
    removed = []
    for n, _, _, p in sorted(_entries(cfg, owner, repo), key=lambda e: e[3].name):
        if n == number:
            p.unlink(missing_ok=True)
            removed.append(p)
    return removed


def archived_at(state_archive: Path) -> str:
    m = _NAME.match(state_archive.name)
    when = datetime.strptime(m.group(2), _STAMP).replace(tzinfo=timezone.utc) if m else datetime.now(timezone.utc)
    return when.isoformat(timespec="seconds")


def restore(cfg: Config, owner: str, repo: str, number: int, pr_dir: Path, head_sha: str) -> dict[str, Any] | None:
    """Bring the newest archive back into a fresh PR folder. Returns the `restored` summary, or None."""
    for path in archives(cfg, owner, repo, number):
        saved = read_json(path)
        if isinstance(saved, dict):
            break
    else:
        return None
    for d in saved.get("drafts") or []:
        d.pop("posting", None)  # a post that never finished; the draft is pending again
        if d.get("commit_id") != head_sha:
            d["outdated"] = True  # kept on its old line until the reviewer re-places it
    summary = {"archived_at": archived_at(path), "restored_at": now_iso(), "archive": path.name, **counts(saved)}
    saved["restored"] = summary

    def apply(s: dict[str, Any]) -> None:
        s.clear()
        s.update(saved)

    StateStore(pr_dir).update(apply)
    ledger = ledger_for(path)
    if ledger.exists():
        old = ledger.read_bytes()
        dest = pr_dir / LEDGER
        newer = dest.read_bytes() if dest.exists() else b""
        _write_private(dest, old + (b"" if old.endswith(b"\n") or not old else b"\n") + newer)
    return summary
