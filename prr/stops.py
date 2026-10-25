"""Guided review: the short list of places that need a human ("stops"), each with one question the reviewer answers
Real problem / Not a problem / Not sure.

`build_stops(analysis, state=None)` is pure and deterministic, and fills analysis["stops"] and
analysis["review_summary"]. merge.build_analysis calls it without state, so the analysis.json that the CLI and eval
write holds unpinned stops. The review server calls it with the reviewer's state.json on every request, which pins
answered stops: they stay listed whatever the cap, they are never folded into a neighbour, and an answer whose stop
is gone (or is now hidden by the second look) stays listed as `dropped`, the end screen's "earlier answer".

**Where stops come from, by priority.** At most [review] max_stops are listed; the rest are "N more places".
  1. blocker and major findings
  2. deep-check findings
  3. failing CI checks this PR caused or may have caused (and failures Claude couldn't explain)
  4. other review tools' findings Claude agrees with
  5. minor findings, and questions for the author (category `intent`, always minor)
  6. risk notes in high-risk files
  7. risk notes in medium-risk files
  8. "things to check" (look_for) of high-risk files that have no other stop
  9. nit findings
A finding's question is its own `question` (the deep check's yes/no question), else "Is this a real problem? <title>";
a question for the author is asked as it is: its title.
Findings the second look hides are stops too, marked `low_confidence`. They sit outside the cap and the counts: the
Review view lists them only when its "Include low-confidence findings" switch is on.

**Ids** stay the same across re-runs: "stop-" + sha1(kind | path | side | anchor text | source key). The source key
is the category and anchor for a finding (never its title), the kind and text for a note, the text for a look_for
item, the check name for CI and the comment id for another tool's comment.

**Order** is reading order: chapter, then the file's place in it, then line. CI checks have no file and come first.
Line stops within 3 lines of each other, in one file and on one side, become one: a finding takes in a nearby note
or another tool's comment, another tool's comment takes in a note, and of two notes the first takes in the second.
What was taken in is added to the kept stop's "Claude's view". Two findings never merge (each is its own comment), and
an answered stop is never taken in.

**Answers** (POST/DELETE /api/stops/{id}/verdict): `answer` and `take_back` change a state.json dict inside
StateStore.update, so a double-click on "Real problem" makes one comment.
- real: a finding, risk note or other tool's stop gets a pending comment pre-filled with `comment_body` and
  `origin_id` = the stop id (an existing one is reused); a CI stop adds `comment_body` to the review summary draft
  (`review_body_draft`); a look_for stop records only (the app's editor makes the comment, with `origin_id`, on save).
- not: also dismisses the finding.
- unsure: records only.
Taking an answer back restores a finding that "not" dismissed, removes what a CI "real" added to the review summary,
and deletes a "real" comment that is unchanged from `comment_body`; an edited one is kept and returned, so the app can
ask "Keep your comment?". Answers become eval labels in the server (prr/labels.py).
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from . import anchors
from .diffparse import FileDiff
from .drafts import is_posting, new_draft
from .findings import INTENT, norm
from .models import ReviewSummary, Stop

MAX_STOPS = 12
NEAR_LINES = 3                 # line stops this close, in one file and on one side, become one stop
MAX_REVIEW_BODY = 65_536       # characters in the saved review summary draft
ID = re.compile(r"stop-[0-9a-f]{40}")
VERDICTS = ("real", "not", "unsure")
CI_CAUSED = ("yes", "unclear")  # failing checks that are stops (a failure Claude couldn't explain counts as unclear)
_RANK = {"finding": 0, "bot": 1, "risk_note": 2}                       # who keeps a merged stop
_TAKES_IN = {"finding": ("bot", "risk_note"), "bot": ("risk_note",), "risk_note": ("risk_note",)}
_KIND_ORDER = {"ci": 0, "finding": 1, "bot": 2, "risk_note": 3, "look_for": 4}
_ANCHOR_KEYS = ("kind", "path", "side", "line", "start_line", "question", "finding_id", "bot_comment_id", "check_name")
_GONE = 10**9                  # reading position of a file no longer in the PR (an earlier answer's)
_URL = re.compile(r"https?://[^\s<>()\[\]`'\"]+", re.IGNORECASE)


class BeingPosted(Exception):
    """The comment an answer made is being posted to GitHub right now, so the answer can't be taken back yet."""


def stop_id(kind: str, path: str | None, side: str | None, anchor: str, key: str) -> str:
    raw = "|".join((kind, path or "", side or "", norm(anchor), key))
    return "stop-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _join(*parts: str | None) -> str | None:
    seen = list(dict.fromkeys(p.strip() for p in parts if p and p.strip()))
    return "\n\n".join(seen) or None


def _new(kind: str, path: str | None, side: str | None, line: int | None, *, key: str, question: str, prio: int,
         anchor: str = "", fold: str = "", **fields: Any) -> dict:
    """A candidate stop. Keys starting with "_" are working data, dropped before the stop is returned."""
    stop: dict[str, Any] = {
        "kind": kind, "path": path, "side": side, "line": line, "start_line": None, "question": question,
        "scenario": [], "claude_view": None, "confidence": None, "judge_score": None, "second_look": None,
        "severity": None, "category": None, "finding_id": None, "bot_comment_id": None, "check_name": None,
        "comment_body": None, "chapter": None, "order": 0, "more": False, "low_confidence": False, "sources": [],
        "changed_lines": 0, "dropped": False, **fields,
    }
    stop.update(id=stop_id(kind, path, side, anchor, key), _prio=prio, _anchor=anchor, _key=key, _fold=fold)
    return stop


# --------------------------------------------------------------------------------------
# Hunks
# --------------------------------------------------------------------------------------


def _hunk(file: dict, side: str | None, line: int | None) -> dict | None:
    if line is None:
        return None
    for h in file["hunks"]:
        start, count = (h["old_start"], h["old_lines"]) if side == "LEFT" else (h["new_start"], h["new_lines"])
        if count > 0 and start <= line < start + count:
            return h
    return None


def _changed(file: dict | None, side: str | None, line: int | None, diffs: dict[str, FileDiff] | None) -> int:
    """Added and deleted lines in the hunk the line is in (its size, when the diff itself isn't at hand)."""
    h = _hunk(file, side, line) if file else None
    if h is None:
        return 0
    fd = (diffs or {}).get(file["path"])  # type: ignore[index]
    exact = next((x for x in fd.hunks if (x.old_start, x.new_start) == (h["old_start"], h["new_start"])), None) \
        if fd else None
    if exact is not None:
        return sum(1 for ln in exact.lines if ln.kind != " ")
    return max(h["old_lines"], h["new_lines"])


def first_change(file: dict, diffs: dict[str, FileDiff] | None) -> tuple[str, int | None]:
    """(side, line) of the file's first changed line: an added line when its first changed hunk adds any, else the
    first deleted one. Without the diff, the first hunk's first line."""
    fd = (diffs or {}).get(file["path"])
    for h in fd.hunks if fd else []:
        added = next((ln.new for ln in h.lines if ln.kind == "+"), None)
        if added is not None:
            return "RIGHT", added
        deleted = next((ln.old for ln in h.lines if ln.kind == "-"), None)
        if deleted is not None:
            return "LEFT", deleted
    for h in file["hunks"]:
        if h["new_lines"] > 0:
            return "RIGHT", h["new_start"]
        if h["old_lines"] > 0:
            return "LEFT", h["old_start"]
    return "RIGHT", None


# --------------------------------------------------------------------------------------
# Candidates, one function per source
# --------------------------------------------------------------------------------------


def _finding_prio(f: dict) -> int:
    if f["category"] == INTENT:
        return 5  # a question for the author ranks with minor findings, whoever found it
    if f["severity"] in ("blocker", "major"):
        return 1
    if "deep-check" in (f.get("sources") or []):
        return 2
    return 5 if f["severity"] == "minor" else 9


def _finding_question(f: dict) -> str:
    if f["category"] == INTENT:
        return f["title"]  # already the question, e.g. "Why was the retry removed?"
    return f.get("question") or f"Is this a real problem? {f['title']}"


def _from_findings(analysis: dict, files: dict[str, dict], diffs: dict[str, FileDiff] | None) -> list[dict]:
    out = []
    for f in analysis["findings"]:
        out.append(_new(
            "finding", f["path"], f["side"], f["line"], anchor=f["anchor"], key=f"{f['category']}|{norm(f['anchor'])}",
            question=_finding_question(f), prio=_finding_prio(f),
            start_line=f["start_line"], scenario=list(f.get("scenario") or []),
            claude_view=_join(f.get("impact"), f.get("why")), confidence=f.get("confidence"),
            judge_score=f.get("judge_score"), second_look=f.get("second_look"), severity=f["severity"],
            category=f["category"], finding_id=f["id"], comment_body=f["comment_body"],
            low_confidence=bool(f["low_confidence"]), sources=list(f.get("sources") or []),
            changed_lines=_changed(files.get(f["path"]), f["side"], f["line"], diffs)))
    return out


def _from_notes(files: list[dict], diffs: dict[str, FileDiff] | None) -> list[dict]:
    out = []
    for file in files:
        if file.get("risk") not in ("high", "medium"):
            continue
        for n in file["notes"]:
            text = n["text"].strip()
            if n["kind"] != "risk" or not text:
                continue
            out.append(_new(
                "risk_note", file["path"], n["side"], n["end"], key=f"risk|{norm(text).lower()}",
                question=f"Is this safe? {text}", prio=6 if file["risk"] == "high" else 7,
                fold=f"**Risk note:** {text}", start_line=n["start"] if n["start"] < n["end"] else None,
                comment_body=text, sources=["annotate"], changed_lines=_changed(file, n["side"], n["end"], diffs)))
    return out


def _from_bots(analysis: dict, files: dict[str, dict], diffs: dict[str, FileDiff] | None) -> list[dict]:
    out = []
    for b in analysis["bot_findings"]:
        file = files.get(b["path"])
        if b.get("verdict") != "agree" or file is None:
            continue
        side, line = b.get("side") or "RIGHT", b.get("line")
        if line is None:  # a comment on the whole file: at its first change
            side, line = first_change(file, diffs)
        title = " ".join(str(b["title"]).split()).rstrip(" .:;!?") or "this point"
        out.append(_new(
            "bot", b["path"], side, line, key=str(b["comment_id"]), question=f"Do you agree with {b['bot']}: {title}?",
            prio=4, fold=f"**{b['bot']}:** {title}", claude_view=b.get("verdict_reason") or None,
            severity=b.get("severity"), bot_comment_id=b["comment_id"], comment_body=f"> {b['bot']}: {title}",
            sources=["bots"], changed_lines=_changed(file, side, line, diffs)))
    return out


def ci_summary_text(name: str, explanation: dict) -> str:
    """What "Real problem" on a failing check adds to the review summary: the failure and Claude's fix."""
    summary, fix = (explanation.get("summary") or "").strip(), (explanation.get("fix") or "").strip()
    text = f"The `{name}` check fails." + (f" {summary}" if summary else "")
    return text + (f"\n\nFix: {fix}" if fix else "")


def _from_ci(analysis: dict) -> list[dict]:
    out = []
    for c in (analysis.get("ci") or {}).get("checks") or []:
        ex = c.get("explanation") or {}
        caused = ex.get("caused_by_pr") or "unclear"
        if c["state"] != "fail" or caused not in CI_CAUSED:
            continue
        guess = "Claude thinks this PR caused it." if caused == "yes" else "Claude isn't sure this PR caused it."
        out.append(_new(
            "ci", None, None, None, key=c["name"], question=f"Is this failure caused by this PR? `{c['name']}` fails.",
            prio=3, claude_view=_join(ex.get("summary"), ex.get("cause"), guess), check_name=c["name"],
            comment_body=ci_summary_text(c["name"], ex), sources=["ci"]))
    return out


def _from_look_for(files: list[dict], taken: set[str], diffs: dict[str, FileDiff] | None) -> list[dict]:
    out = []
    for file in files:
        if file.get("risk") != "high" or file["path"] in taken:
            continue
        side, line = first_change(file, diffs)
        for item in file["look_for"]:
            text = " ".join(item.split())
            if text:
                out.append(_new("look_for", file["path"], side, line, key=text.lower(), question=text, prio=8,
                                sources=["annotate"], changed_lines=file["additions"] + file["deletions"]))
    return out


def _candidates(analysis: dict, diffs: dict[str, FileDiff] | None) -> list[dict]:
    """Every stop the analysis has, before merging and the cap. Look_for items only count for high-risk files that
    have no other (shown) stop."""
    files = {f["path"]: f for f in analysis["files"]}
    found = (_from_findings(analysis, files, diffs) + _from_bots(analysis, files, diffs)
             + _from_notes(analysis["files"], diffs) + _from_ci(analysis))
    taken = {s["path"] for s in found if s["path"] and not s["low_confidence"]}
    out = found + _from_look_for(analysis["files"], taken, diffs)
    for i, s in enumerate(out):
        s["_seq"] = i
    return out


# --------------------------------------------------------------------------------------
# Merging, the cap and order
# --------------------------------------------------------------------------------------


def _span(s: dict) -> tuple[int, int]:
    return (s["start_line"] or s["line"]), s["line"]


def _gap(a: dict, b: dict) -> int:
    (a0, a1), (b0, b1) = _span(a), _span(b)
    return max(a0, b0) - min(a1, b1)


def _near(a: dict, b: dict) -> bool:
    if a["path"] != b["path"] or a["side"] != b["side"] or a["line"] is None or b["line"] is None:
        return False
    return _gap(a, b) <= NEAR_LINES


def _take_in(host: dict, other: dict) -> None:
    host["claude_view"] = _join(host["claude_view"], other["_fold"])
    host["sources"] = list(dict.fromkeys(host["sources"] + other["sources"]))
    host["_prio"] = min(host["_prio"], other["_prio"])
    host["changed_lines"] = max(host["changed_lines"], other["changed_lines"])


def _merge_nearby(candidates: list[dict], pinned: set[str]) -> list[dict]:
    """Line stops close together become one (see the module docstring). Low-confidence findings stay apart."""
    mergeable = [s for s in candidates if s["kind"] in _RANK and not s["low_confidence"]]
    ids = {id(s) for s in mergeable}
    kept: list[dict] = []
    by_place: dict[tuple, list[dict]] = {}  # (path, side) → kept stops there
    for s in sorted(mergeable, key=lambda s: (_RANK[s["kind"]], s["path"], s["side"], _span(s)[0] if s["line"] else 0,
                                              s["id"])):
        here = by_place.setdefault((s["path"], s["side"]), [])
        hosts = [] if s["id"] in pinned else [k for k in here if s["kind"] in _TAKES_IN[k["kind"]] and _near(k, s)]
        if hosts:
            _take_in(min(hosts, key=lambda k: (_RANK[k["kind"]], _gap(k, s), k["line"])), s)
        else:
            kept.append(s)
            here.append(s)
    return [s for s in candidates if id(s) not in ids] + kept


def _unique(stops: list[dict]) -> None:
    """Two stops with the same id (two equal notes far apart, two findings on an empty anchor): the later one's id
    also hashes its finding id or line."""
    seen: set[str] = set()
    for s in stops:
        n = 0
        while s["id"] in seen:
            n += 1
            extra = s["finding_id"] or f"{s['line']}"
            s["id"] = stop_id(s["kind"], s["path"], s["side"], s["_anchor"], f"{s['_key']}|{extra}|{n}")
        seen.add(s["id"])


def _reading_positions(analysis: dict) -> dict[str, tuple[int, int, str | None]]:
    """path → (chapter index, index in the chapter, chapter id); files outside every chapter come after them."""
    pos: dict[str, tuple[int, int, str | None]] = {}
    for ci, ch in enumerate(analysis["chapters"]):
        for fi, cf in enumerate(ch["files"]):
            pos.setdefault(cf["path"], (ci, fi, ch["id"]))
    after = len(analysis["chapters"])
    for i, f in enumerate(analysis["files"]):
        pos.setdefault(f["path"], (after, i, None))
    return pos


def _reading_key(s: dict, pos: dict[str, tuple[int, int, str | None]]) -> tuple:
    tie = (_KIND_ORDER[s["kind"]], s.get("_seq", _GONE), s["id"])  # then the order the analysis lists them in
    if s["path"] is None:
        return (0, 0, 0, "", 0, *tie)
    ci, fi, _ = pos.get(s["path"], (_GONE, 0, None))
    return (1, ci, fi, s["path"], s["line"] or 0, *tie)


def _apply_cap(stops: list[dict], pinned: set[str], max_stops: int) -> None:
    """The max_stops highest-priority stops are listed, plus every answered one; the rest are "more places"."""
    capped = [s for s in stops if not (s["low_confidence"] or s["dropped"])]
    ranked = sorted(capped, key=lambda s: (s["_prio"], s["_read"]))
    listed = {s["id"] for s in ranked[:max(0, max_stops)]} | pinned
    for s in capped:
        s["more"] = s["id"] not in listed


# --------------------------------------------------------------------------------------
# Answers in state.json
# --------------------------------------------------------------------------------------


def verdicts(state: dict | None) -> dict[str, dict]:
    """state.stop_verdicts, without anything malformed."""
    raw = (state or {}).get("stop_verdicts")
    return {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("verdict") in VERDICTS} \
        if isinstance(raw, dict) else {}


def done_ids(stops: list[dict], state: dict | None) -> set[str]:
    """Stops that count as looked at: answered (any answer), or whose finding was dismissed elsewhere."""
    answered = verdicts(state)
    dismissed = set((state or {}).get("dismissed_suggestions") or [])
    return {s["id"] for s in stops if s["id"] in answered or (s["finding_id"] and s["finding_id"] in dismissed)}


def _as(kind: type, value: Any) -> Any:
    return value if isinstance(value, kind) and not isinstance(value, bool) else None


def _earlier(sid: str, rec: dict) -> dict | None:
    """An answer whose stop is gone, as a dropped stop, from where the stop was when it was answered."""
    a = rec.get("anchor") if isinstance(rec.get("anchor"), dict) else {}
    if a.get("kind") not in _KIND_ORDER:
        return None
    stop = _new(a["kind"], _as(str, a.get("path")), a.get("side") if a.get("side") in ("LEFT", "RIGHT") else None,
                _as(int, a.get("line")), key="", question=_as(str, a.get("question")) or "An earlier answer", prio=0,
                start_line=_as(int, a.get("start_line")), finding_id=_as(str, a.get("finding_id")),
                bot_comment_id=_as(int, a.get("bot_comment_id")), check_name=_as(str, a.get("check_name")),
                dropped=True)
    stop["id"] = sid
    return stop


def anchor_of(stop: dict) -> dict:
    """Where a stop was when it was answered, with its question and source ids: an answer whose stop later
    disappears still lists as an earlier answer."""
    return {k: stop[k] for k in _ANCHOR_KEYS}


# --------------------------------------------------------------------------------------
# build_stops
# --------------------------------------------------------------------------------------


def _summary(stops: list[dict], files: list[dict], done: set[str]) -> dict:
    listed = [s for s in stops if not (s["more"] or s["low_confidence"] or s["dropped"])]
    flagged = {s["path"] for s in stops if s["path"] and not (s["low_confidence"] or s["dropped"])}
    return {"places": len(listed), "more_places": sum(1 for s in stops if s["more"]),
            "quiet_files": sum(1 for f in files if f["path"] not in flagged),
            "minutes_left": sum(2 + min(5, s["changed_lines"] // 20) for s in listed if s["id"] not in done)}


def build_stops(analysis: dict, state: dict | None = None, *, max_stops: int = MAX_STOPS,
                diffs: dict[str, FileDiff] | None = None) -> list[dict]:
    """Fill analysis["stops"] (in reading order) and analysis["review_summary"]; returns the stops.

    `state` (state.json) pins answered stops; without it nothing is pinned. `diffs` (path → parsed diff) makes the
    changed-line counts and a file's first changed line exact; without it hunk sizes stand in.
    """
    answered = verdicts(state)
    pinned = set(answered)
    stops = _merge_nearby(_candidates(analysis, diffs), pinned)
    _unique(stops)
    have = {s["id"] for s in stops}
    for s in stops:
        s["dropped"] = s["id"] in pinned and s["low_confidence"]  # answered, then hidden by the second look
    stops += [e for sid, rec in answered.items() if sid not in have and ID.fullmatch(sid)
              for e in [_earlier(sid, rec)] if e]
    pos = _reading_positions(analysis)
    for s in stops:
        s["_read"] = _reading_key(s, pos)
        s["chapter"] = pos[s["path"]][2] if s["path"] in pos else None
    _apply_cap(stops, pinned, max_stops)
    stops.sort(key=lambda s: s["_read"])
    out = [Stop.model_validate({**{k: v for k, v in s.items() if not k.startswith("_")}, "order": i})
           .model_dump(mode="json") for i, s in enumerate(stops)]
    analysis["stops"] = out
    analysis["review_summary"] = ReviewSummary.model_validate(
        _summary(out, analysis["files"], done_ids(out, state))).model_dump(mode="json")
    return out


# --------------------------------------------------------------------------------------
# Answering (inside StateStore.update)
# --------------------------------------------------------------------------------------


def outside_links(body: str, owner: str, repo: str) -> list[str]:
    """Links in a comment that lead anywhere but this repo on GitHub (github.com/<owner>/<repo>)."""
    home = f"github.com/{owner}/{repo}".lower()
    out = []
    for url in _URL.findall(body or ""):
        url = url.rstrip(".,;:!?")
        rest = url.split("://", 1)[1].lower().removeprefix("www.")
        if rest == home or (rest.startswith(home) and rest[len(home)] in "/#?"):
            continue
        out.append(url)
    return out


def add_to_body(text: str, block: str) -> str:
    """The review summary with `block` added as its last paragraph (once). Raises ValueError past MAX_REVIEW_BODY."""
    block = block.strip()
    if not block or block in text:
        return text
    out = f"{text.rstrip()}\n\n{block}" if text.strip() else block
    if len(out) > MAX_REVIEW_BODY:
        raise ValueError(f"The review summary would be longer than {MAX_REVIEW_BODY:,} characters")
    return out


def remove_from_body(text: str, block: str) -> str:
    """The review summary without `block`, when it is still there as it was added."""
    block = block.strip()
    at = text.find(block) if block else -1
    if at < 0:
        return text
    return f"{text[:at].rstrip()}\n\n{text[at + len(block):].lstrip()}".strip()


def _comment_for(s: dict, stop: dict, ctx: dict) -> dict:
    """The stop's pending comment: the one made from it before, or the finding's own ("Add to my review"), or a new
    one pre-filled with `comment_body`. Raises ValueError when it can't be a comment on this PR."""
    mine = next((d for d in s["drafts"] if d.get("origin_id") == stop["id"]), None)
    if mine is None and stop["finding_id"]:
        mine = next((d for d in s["drafts"] if d.get("suggestion_id") == stop["finding_id"] and not d.get("origin_id")),
                    None)
    if mine is not None:
        return mine
    if stop["path"] is None or stop["side"] is None or stop["line"] is None:
        raise ValueError("this place has no line to comment on")
    start = stop["start_line"]
    fc = next((f for f in ctx["files"] if f["path"] == stop["path"]), None)
    if start is not None and fc is not None and anchors.check(fc, stop["side"], stop["line"], start) is not None \
            and anchors.check(fc, stop["side"], stop["line"], None) is None:
        start = None  # the range would leave its hunk; the last line alone can still be an inline comment
    finding = stop["kind"] == "finding"
    draft = new_draft(ctx, stop["path"], stop["side"], stop["line"], stop["comment_body"] or "", start_line=start,
                      source="suggestion" if finding else "user", suggestion_id=stop["finding_id"] if finding else None,
                      origin_id=stop["id"])
    pr = ctx["pr"]
    draft["outside_link"] = bool(outside_links(draft["body"], pr["owner"], pr["repo"]))
    s["drafts"].append(draft)
    return draft


def answer(s: dict, stop: dict, verdict: str, ctx: dict, *, opened_at: str | None, now: str) -> dict:
    """Record `verdict` for `stop` in state `s` and do what it does (module docstring). Answering again with the same
    verdict changes nothing. Returns {verdict: <record>, draft, dismissed, review_body_draft, previous}, where
    `previous` is the earlier verdict (None if there was none). Raises ValueError for what can't be done."""
    if verdict not in VERDICTS:
        raise ValueError("verdict must be real, not or unsure")
    answered = s.setdefault("stop_verdicts", {})
    prev = answered.get(stop["id"]) if isinstance(answered.get(stop["id"]), dict) else None
    if prev is None or prev.get("verdict") != verdict:
        answered[stop["id"]] = {"verdict": verdict, "at": now, "head_sha": ctx["pr"]["head_sha"],
                                "anchor": anchor_of(stop), "opened_at": opened_at, "answered_at": now}
    out: dict[str, Any] = {"verdict": answered[stop["id"]], "draft": None, "dismissed": None,
                           "review_body_draft": None, "previous": (prev or {}).get("verdict")}
    if verdict == "real" and stop["kind"] == "ci":
        s["review_body_draft"] = add_to_body(s.get("review_body_draft") or "", stop["comment_body"] or "")
        out["review_body_draft"] = s["review_body_draft"]
    elif verdict == "real" and stop["kind"] != "look_for":
        out["draft"] = _comment_for(s, stop, ctx)
    elif verdict == "not" and stop["finding_id"]:
        if stop["finding_id"] not in s["dismissed_suggestions"]:
            s["dismissed_suggestions"].append(stop["finding_id"])
        out["dismissed"] = stop["finding_id"]
    return out


def take_back(s: dict, sid: str, stop: dict | None) -> dict:
    """Undo, or the first half of "Change answer": remove the answer to `sid` and what it did (module docstring).
    `stop` is the stop as listed now (None or dropped when it is gone). Returns {kept_draft, restored,
    review_body_draft, previous}. Raises BeingPosted when its comment is being posted right now."""
    rec = verdicts(s).get(sid)
    out: dict[str, Any] = {"kept_draft": None, "restored": None, "review_body_draft": None,
                           "previous": (rec or {}).get("verdict")}
    if rec is None:
        return out
    a = rec.get("anchor") if isinstance(rec.get("anchor"), dict) else {}
    live = stop if stop is not None and not stop["dropped"] else None
    kind = (live or {}).get("kind") or a.get("kind")
    mine = [d for d in s["drafts"] if d.get("origin_id") == sid] if rec["verdict"] == "real" and kind != "ci" else []
    if any(is_posting(d) for d in mine):
        raise BeingPosted(sid)
    del s["stop_verdicts"][sid]
    if rec["verdict"] == "not":
        fid = (stop or {}).get("finding_id") or a.get("finding_id")
        if fid and fid in s["dismissed_suggestions"]:
            s["dismissed_suggestions"].remove(fid)
            out["restored"] = fid
    elif rec["verdict"] == "real" and kind == "ci" and live and live["comment_body"]:
        before = s.get("review_body_draft") or ""
        after = remove_from_body(before, live["comment_body"])
        if after != before:
            s["review_body_draft"] = out["review_body_draft"] = after
    for d in mine:
        if live and live["comment_body"] is not None and d["body"].strip() == live["comment_body"].strip():
            s["drafts"].remove(d)
        else:
            out["kept_draft"] = d
    return out
