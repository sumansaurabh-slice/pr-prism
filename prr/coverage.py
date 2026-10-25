"""Coverage before submit: what the reviewer hasn't checked yet. A warning only; it never blocks a review.

One definition of "not checked", used by the Overview, the Review progress and the Submit dialog: unanswered stops,
including the "N more places" past the cap, plus deep-check areas this run didn't check. The warning number is
`count` = len(not_checked) + len(skipped_areas). A stop is checked once it has any answer (Real problem, Not a
problem or Not sure; "not sure" is also listed under `unsure`), or when its finding was dismissed elsewhere.

Two more lines are shown collapsed and are not part of the warning:
- high-risk code not seen: hunks of high-risk files that were never on screen for 3 s (POST /api/seen) and hold no
  answered stop. `by_file` feeds the file tree's "2 of 5 risky hunks seen";
- low confidence, not checked: unanswered findings the second look hides.

Hunk ids are strings the web app computes from each hunk's header ("@@ -a,b +c,d @@"): "RIGHT:<c>-<c+d-1>", or
"LEFT:<a>-<a+b-1>" for a hunk that only deletes lines (d = 0).
"""

from __future__ import annotations

import re

from .stops import done_ids, verdicts

HUNK_ID = re.compile(r"(LEFT|RIGHT):\d{1,9}-\d{1,9}")
MAX_SEEN_PER_FILE = 5_000


def hunk_id(h: dict) -> str:
    """The id the web app gives a hunk when it has been on screen (see the module docstring)."""
    if h["new_lines"] > 0:
        return f"RIGHT:{h['new_start']}-{h['new_start'] + h['new_lines'] - 1}"
    return f"LEFT:{h['old_start']}-{h['old_start'] + max(h['old_lines'], 1) - 1}"


def _holds(h: dict, side: str | None, line: int | None) -> bool:
    if line is None:
        return False
    start, count = (h["old_start"], h["old_lines"]) if side == "LEFT" else (h["new_start"], h["new_lines"])
    return count > 0 and start <= line < start + count


def _high_risk(analysis: dict, state: dict, answered: dict[str, dict]) -> dict:
    seen_by_file = state.get("seen_hunks") if isinstance(state.get("seen_hunks"), dict) else {}
    spots = [(s["path"], s["side"], s["line"]) for s in analysis.get("stops") or [] if s["id"] in answered]
    by_file: dict[str, dict[str, int]] = {}
    for f in analysis["files"]:
        if f.get("risk") != "high" or not f["hunks"]:
            continue
        seen = set(seen_by_file.get(f["path"]) or [])
        looked = sum(1 for h in f["hunks"] if hunk_id(h) in seen
                     or any(p == f["path"] and _holds(h, side, line) for p, side, line in spots))
        by_file[f["path"]] = {"seen": looked, "total": len(f["hunks"])}
    return {"hunks": sum(v["total"] - v["seen"] for v in by_file.values()),
            "files": sum(1 for v in by_file.values() if v["seen"] < v["total"]), "by_file": by_file}


def coverage(analysis: dict, state: dict) -> dict:
    """{not_checked, unsure, skipped_areas, high_risk_unseen, low_confidence, running, count} for an analysis with
    stops (merge.build_analysis) and the reviewer's state.json."""
    stops = sorted(analysis.get("stops") or [], key=lambda s: s["order"])
    answered = verdicts(state)
    done = done_ids(stops, state)
    shown = [s for s in stops if not (s["low_confidence"] or s["dropped"])]
    left = sorted((s for s in shown if s["id"] not in done), key=lambda s: (s["more"], s["order"]))
    not_checked = [{"stop_id": s["id"], "path": s["path"], "line": s["line"], "question": s["question"],
                    "kind": s["kind"]} for s in left]
    unsure = [{"stop_id": s["id"], "path": s["path"], "line": s["line"], "question": s["question"]}
              for s in stops if answered.get(s["id"], {}).get("verdict") == "unsure"]
    skipped = [{"title": a["title"], "reason": a["reason"]} for a in analysis.get("skipped_areas") or []]
    return {
        "not_checked": not_checked,
        "unsure": unsure,
        "skipped_areas": skipped,
        "high_risk_unseen": _high_risk(analysis, state, answered),
        "low_confidence": sum(1 for s in stops if s["low_confidence"] and s["id"] not in done),
        "running": analysis["status"] not in ("complete", "failed"),
        "count": len(not_checked) + len(skipped),
    }


def add_seen(seen: list[str], hunks: list[str]) -> list[str]:
    """A file's seen hunk ids with `hunks` added, keeping the newest MAX_SEEN_PER_FILE."""
    new = list(dict.fromkeys(hunks))
    fresh = set(new)
    return ([x for x in seen if x not in fresh] + new)[-MAX_SEEN_PER_FILE:]
