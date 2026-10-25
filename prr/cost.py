"""Honest cost: each chat answer's own cost, and the PR's totals, all computed from calls.jsonl (prr/ledger.py).

Chat: every `result` event carries the session's running total (`total_cost_usd`), not the turn's cost. Each thread
keeps a bookmark of the last result it saw (`last_session_id`, `last_total_usd`), and an answer costs:
- the same session as the bookmark → total − last total;
- the first result in the thread, a new session id, or a total lower than the last one → the whole total;
- an answer that stopped with an error (no result) → nothing yet: its cost folds into the next answer, which is
  marked `includes_stopped`;
- a result without `total_cost_usd` → "cost unknown", and the bookmark (so the thread total) stays as it was.

Thread fields: last_session_id, last_total_usd, uncounted (answers since the bookmark whose cost isn't counted yet),
cost_lost (some answer's cost can never be counted: the session changed before a later answer covered it).
Answer fields: cost_usd (this answer only, or null), cost_unknown, includes_stopped, session_total_usd.
"""

from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from . import ledger
from .util import read_json

LONG_THREAD_TURNS = 15   # after the answer that reaches this many answers, suggest a fresh thread
LONG_THREAD_USD = 3.0    # … or once the thread has cost this much
RUNNING = ("annotating", "synthesizing", "deepchecking")


# --------------------------------------------------------------------------------------
# Per-turn chat cost
# --------------------------------------------------------------------------------------


def turn_cost(thread: dict, session_id: str | None, total: float | None) -> tuple[dict, dict]:
    """(this answer's cost fields, the thread's new bookmark) for a turn that ended with a result event."""
    uncounted = thread.get("uncounted") or 0
    lost = bool(thread.get("cost_lost"))
    if not isinstance(total, (int, float)):
        return ({"cost_usd": None, "cost_unknown": True, "includes_stopped": False, "session_total_usd": None},
                {"uncounted": uncounted + 1})
    last_sid, last_total = thread.get("last_session_id"), thread.get("last_total_usd")
    if last_sid is None or last_total is None:          # the first result: the session's total covers every answer
        turn, includes = total, uncounted > 0
    elif session_id == last_sid and total >= last_total:
        turn, includes = total - last_total, uncounted > 0
    else:                                               # a new session, or its total went down: start counting again
        turn, includes = total, False
        lost = lost or uncounted > 0                    # those answers' costs are in the old session's total, unseen
    fields = {"cost_usd": round(turn, 6), "cost_unknown": False, "includes_stopped": includes,
              "session_total_usd": total}
    return fields, {"last_session_id": session_id, "last_total_usd": total, "uncounted": 0, "cost_lost": lost}


def stopped_turn(thread: dict) -> tuple[dict, dict]:
    """(answer fields, new bookmark) for a turn that ended without a result: its cost folds into the next answer."""
    return ({"cost_usd": None, "cost_unknown": True, "includes_stopped": False, "session_total_usd": None},
            {"uncounted": (thread.get("uncounted") or 0) + 1})


def thread_unknown(thread: dict) -> bool:
    """Part of this thread's cost isn't counted (yet): the total is a lower bound."""
    return bool(thread.get("uncounted")) or bool(thread.get("cost_lost"))


def answers(thread: dict) -> int:
    return sum(1 for m in thread.get("messages", []) if m.get("role") == "assistant")


def thread_status(thread: dict, spent: float) -> dict:
    """{total_usd, turns, unknown, long}: what the thread picker and the long-thread hint need."""
    n = answers(thread)
    return {"total_usd": round(spent, 4), "turns": n, "unknown": thread_unknown(thread),
            "long": n >= LONG_THREAD_TURNS or spent >= LONG_THREAD_USD}


def chat_spent(pr_dir: Path, tid: str) -> float:
    """One thread's total: the sum of its chat lines in calls.jsonl."""
    return sum(x.get("cost_usd") or 0.0 for x in ledger.read(pr_dir) if x.get("purpose") == "chat" and x.get("target") == tid)


def chat_line(tid: str, model: str | None, fields: dict, *, duration_ms: int | None, error: str | None) -> dict:
    """Keyword arguments for Ledger.record for one chat turn."""
    return {"model": model, "target": tid, "duration_ms": duration_ms, "cost_usd": fields["cost_usd"],
            "outcome": "error" if error else "ok", "error": error, "includes_stopped": fields["includes_stopped"]}


# --------------------------------------------------------------------------------------
# Migration: state.json version 1 stored the session's running total under each answer
# --------------------------------------------------------------------------------------


def upgrade_threads(threads: list[dict]) -> list[dict]:
    """Turn version-1 threads' running totals into per-answer costs, in place. Returns the calls.jsonl lines those
    answers never got (marked `migrated`), so the PR total still includes chats from before the ledger existed."""
    lines = []
    for t in threads:
        t.update({"last_session_id": None, "last_total_usd": None, "uncounted": 0, "cost_lost": False})
        for m in t.get("messages", []):
            if m.get("role") != "assistant":
                continue
            running = m.get("cost_usd")
            if m.get("error") and running is None:
                fields, mark = stopped_turn(t)
            else:  # every turn of a version-1 thread resumed the thread's one session
                fields, mark = turn_cost(t, t.get("session_id"), running)
            m.update(fields)
            t.update(mark)
            error = str(m["error"])[:300] if m.get("error") else None
            lines.append({"at": m.get("ts"), "run_id": None, "purpose": "chat", "model": None, "target": t.get("id"),
                          "duration_ms": None, "cost_usd": fields["cost_usd"], "outcome": "error" if error else "ok",
                          "error": error, "includes_stopped": fields["includes_stopped"], "migrated": True})
    return lines


def not_yet_recorded(existing: list[dict], migrated: list[dict]) -> list[dict]:
    """The migrated lines for threads that have none in calls.jsonl yet (so a retried migration never counts twice)."""
    done = {x.get("target") for x in existing if x.get("migrated")}
    return [x for x in migrated if x.get("target") not in done]


# --------------------------------------------------------------------------------------
# Totals
# --------------------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def analysis_running(pr_dir: Path) -> bool:
    """An analysis process is working on this PR right now (a killed run leaves its status behind, not its process)."""
    if (read_json(pr_dir / "progress.json") or {}).get("status") not in RUNNING:
        return False
    pid = (read_json(pr_dir / "analyze.pid") or {}).get("pid")
    return isinstance(pid, int) and _pid_alive(pid)


BEFORE_LEDGER = "before-ledger"


def before_ledger(pr_dir: Path) -> dict | None:
    """The last analysis run from before calls.jsonl existed, as a ledger line, or None. Its progress.json counted
    only some calls (annotate and synthesis), so the line is `partial`: the total then reads "≥ $X"."""
    prog = read_json(pr_dir / "progress.json") or {}
    spent = prog.get("cost_usd")
    if prog.get("run_id") or not isinstance(spent, (int, float)) or spent <= 0:
        return None
    return {"at": prog.get("started_at"), "run_id": BEFORE_LEDGER, "purpose": "annotate", "model": None,
            "target": None, "duration_ms": None, "cost_usd": round(float(spent), 6), "outcome": "ok", "error": None,
            "migrated": True, "partial": True}


def keep_before_ledger(pr_dir: Path) -> None:
    """Before a new run replaces progress.json: copy an older run's spend into calls.jsonl (once)."""
    old = before_ledger(pr_dir)
    if old and not any(x.get("run_id") == BEFORE_LEDGER for x in ledger.read(pr_dir)):
        ledger.append(pr_dir, old)


def run_total(lines: list[dict], run_id: str) -> float:
    return sum(x.get("cost_usd") or 0.0 for x in lines if x.get("purpose") and x.get("run_id") == run_id)


def totals(lines: list[dict], threads: list[dict]) -> dict[str, Any]:
    """Sums of the ledger's call lines: per analysis run, analysis / chat / other, per thread, and the PR total."""
    runs: dict[str, dict] = {}
    by_thread: dict[str, float] = defaultdict(float)
    analysis = chat = other = 0.0
    unknown = False
    for x in lines:
        purpose = x.get("purpose")
        if not purpose:
            continue
        c = x.get("cost_usd")
        c = float(c) if isinstance(c, (int, float)) else None
        if purpose == "chat":  # a chat answer's unknown cost is judged from its thread (it may fold into the next)
            chat += c or 0.0
            by_thread[x.get("target") or ""] += c or 0.0
            continue
        unknown = unknown or c is None or bool(x.get("partial"))
        rid = x.get("run_id")
        if rid:
            r = runs.setdefault(rid, {"run_id": rid, "started_at": x.get("at"), "cost_usd": 0.0, "calls": 0,
                                      "unknown": False})
            r["cost_usd"] += c or 0.0
            r["calls"] += 1
            r["unknown"] = r["unknown"] or c is None or bool(x.get("partial"))
            analysis += c or 0.0
        else:
            other += c or 0.0
    per_thread = {t["id"]: thread_status(t, by_thread.get(t["id"], 0.0)) for t in threads}
    unknown = unknown or any(thread_unknown(t) for t in threads)
    for r in runs.values():
        r["cost_usd"] = round(r["cost_usd"], 4)
    return {
        "total_usd": round(analysis + chat + other, 4),
        "unknown": unknown,
        "breakdown": {"analysis": round(analysis, 4), "chat": round(chat, 4), "other": round(other, 4)},
        "runs": sorted(runs.values(), key=lambda r: r["started_at"] or ""),
        "threads": per_thread,
    }


def summary(pr_dir: Path, warn_usd: float = 0.0) -> dict[str, Any]:
    """What the app's header, chat and `prr status` show about spend for one PR."""
    from .state import StateStore

    state, migrated = StateStore(pr_dir).read()
    lines = ledger.read(pr_dir)
    lines += not_yet_recorded(lines, migrated)  # an old state.json not yet written back still counts its chats
    old = before_ledger(pr_dir)
    if old and not any(x.get("run_id") == BEFORE_LEDGER for x in lines):
        lines.append(old)  # an analysis from before calls.jsonl, not yet copied into it
    out = totals(lines, state["threads"])
    running = analysis_running(pr_dir)
    current = (read_json(pr_dir / "progress.json") or {}).get("run_id")
    for r in out["runs"]:
        r["running"] = running and r["run_id"] == current
    out["running"] = running
    out["warn_usd"] = warn_usd
    out["over_budget"] = warn_usd > 0 and out["total_usd"] > warn_usd
    out["long_thread"] = {"turns": LONG_THREAD_TURNS, "usd": LONG_THREAD_USD}
    return out


def label(s: dict) -> str:
    """"$12.40", "≥ $12.40" when part of the cost is unknown, plus " so far" while the analysis runs."""
    return f"{'≥ ' if s['unknown'] else ''}${s['total_usd']:.2f}{' so far' if s['running'] else ''}"
