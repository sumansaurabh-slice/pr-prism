"""From the reviewer's raw findings to findings that are placed, de-duplicated and safe to post.

- Placement: models copy the code line correctly far more often than they get its number right, so each
  finding carries an `anchor` (the exact line text) and is placed by matching that text in the diff.
- De-duplication: batches can overlap, so the same issue on the same line is kept once.
- One-click check: suggested code is offered as a GitHub "suggestion" only when it plausibly replaces exactly
  the commented lines.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from .diffparse import FileDiff

SEVERITY_RANK = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}
SEVERITY_TEXT = {"blocker": "Blocks merge", "major": "Must fix", "minor": "Should fix", "nit": "Could improve"}
MIN_ANCHOR = 6          # shorter anchors ("}", "end") match too much to be useful
NEAR_LINES = 3          # two findings this close, same file and category, about the same thing, are one finding
MIN_IDENT_OVERLAP = 0.2  # suggested code must share this much vocabulary with the code it replaces
SOURCE_ORDER = ["annotate", "blockers", "deep-check"]  # the passes that find problems, in the order they run
DEEP_FIELDS = ("scenario", "question", "evidence_refs")  # what the deep check adds to a finding
MAX_STEPS = 6           # scenario steps kept
MAX_REFS = 4            # evidence spots kept

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD = re.compile(r"[a-z0-9]+")


def norm(text: str) -> str:
    return " ".join(text.split())


@dataclass
class DiffLine:
    side: str
    line: int
    text: str
    hunk: str


class FileLines:
    """Every commentable line of one file's diff. RIGHT = added + unchanged lines; LEFT = deleted lines."""

    def __init__(self, fd: FileDiff):
        self.fd = fd
        self.right: list[DiffLine] = []
        self.left: list[DiffLine] = []
        for h in fd.hunks:
            for ln in h.lines:
                if ln.kind == "-":
                    self.left.append(DiffLine("LEFT", ln.old, ln.text, h.id))
                else:
                    self.right.append(DiffLine("RIGHT", ln.new, ln.text, h.id))

    def on(self, side: str) -> list[DiffLine]:
        return self.right if side == "RIGHT" else self.left

    def at(self, side: str, line: int) -> DiffLine | None:
        return next((d for d in self.on(side) if d.line == line), None)

    def resolve(self, side: str, anchor: str, hint: int | None) -> DiffLine | None:
        """The diff line the anchor names: exact text first, then "contains the anchor"; nearest to the hint."""
        want = norm(anchor)
        order = [side, "LEFT" if side == "RIGHT" else "RIGHT"] if side in ("LEFT", "RIGHT") else ["RIGHT", "LEFT"]
        for s in order:
            lines = self.on(s)
            hits = [d for d in lines if norm(d.text) == want] if want else []
            if not hits and len(want) >= MIN_ANCHOR:
                hits = [d for d in lines if want in norm(d.text)]
            if hits:
                return min(hits, key=lambda d: abs(d.line - hint) if hint else 0)
        if hint and side in ("LEFT", "RIGHT"):
            return self.at(side, hint)  # no text match, but the number points at a diff line
        return None


def finding_id(path: str, category: str, anchor: str, title: str) -> str:
    raw = f"{path}|{category}|{norm(anchor)}|{norm(title).lower()}"
    return "f-" + hashlib.sha1(raw.encode()).hexdigest()[:12]


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _overlap(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _better(a: dict, b: dict) -> dict:
    """Of two findings about the same thing, keep the more severe, then the more confident."""
    ka = (SEVERITY_RANK.get(a["severity"], 9), -(a.get("confidence") or 0))
    kb = (SEVERITY_RANK.get(b["severity"], 9), -(b.get("confidence") or 0))
    return a if ka <= kb else b


def _merge_twins(a: dict, b: dict) -> dict:
    """The better of two twins, also listing every pass that found it. When the kept one has no deep-check text
    (scenario, question, evidence spots), it takes the other's."""
    best = _better(a, b)
    other = b if best is a else a
    keep = dict(best)
    found = {*keep.get("sources", []), *other.get("sources", [])}
    keep["sources"] = sorted(found, key=lambda s: (SOURCE_ORDER.index(s) if s in SOURCE_ORDER else 9, s))
    for key in DEEP_FIELDS:
        if not keep.get(key) and other.get(key):
            keep[key] = other[key]
    return keep


def dedupe(findings: list[dict]) -> tuple[list[dict], int]:
    """Same file + category + anchor, or same file + category within a few lines and about the same thing."""
    kept: list[dict] = []
    removed = 0
    for f in sorted(findings, key=lambda x: (x["path"], x["line"])):
        twin = None
        for k in kept:
            if k["path"] != f["path"] or k["category"] != f["category"]:
                continue
            same_anchor = len(norm(f["anchor"])) >= MIN_ANCHOR and norm(k["anchor"]) == norm(f["anchor"])
            near = abs(k["line"] - f["line"]) <= NEAR_LINES and _overlap(_words(k["title"]), _words(f["title"])) >= 0.5
            if same_anchor or near:
                twin = k
                break
        if twin is None:
            kept.append(f)
        else:
            kept[kept.index(twin)] = _merge_twins(twin, f)
            removed += 1
    return kept, removed


def _brackets_balance_alike(old: str, new: str) -> bool:
    return all(old.count(o) - old.count(c) == new.count(o) - new.count(c) for o, c in ("()", "[]", "{}"))


def one_click_ok(fl: FileLines, side: str, start: int, end: int, code: str | None) -> bool:
    """A GitHub suggestion replaces exactly the commented lines; only offer it when that is plausible."""
    if not code or side != "RIGHT":
        return False
    lines = [fl.at("RIGHT", n) for n in range(start, end + 1)]
    if any(d is None for d in lines) or len({d.hunk for d in lines}) != 1:  # type: ignore[union-attr]
        return False
    old = "\n".join(d.text for d in lines)  # type: ignore[union-attr]
    if not _brackets_balance_alike(old, code):
        return False
    a, b = set(_IDENT.findall(old)), set(_IDENT.findall(code))
    return (not a and not b) or _overlap(a, b) >= MIN_IDENT_OVERLAP


def comment_body(f: dict) -> str:
    """The Markdown posted to GitHub for one finding."""
    parts = [f"**{f['title']}**", f"*{SEVERITY_TEXT.get(f['severity'], f['severity'])} · {f['category']}*"]
    if f.get("impact"):
        parts.append(f["impact"])
    if f.get("fix"):
        parts.append(f"**Fix:** {f['fix']}")
    if f.get("suggestion"):
        fence = "suggestion" if f.get("one_click") else ""
        parts.append(f"```{fence}\n{f['suggestion'].rstrip()}\n```")
    if f.get("why"):
        parts.append(f"<details>\n<summary>Why</summary>\n\n{f['why']}\n\n</details>")
    return "\n\n".join(parts)


def _refs(raw: object) -> list[dict]:
    """Evidence spots as {path, line}; malformed ones are left out."""
    out = []
    for r in raw if isinstance(raw, list) else []:
        if isinstance(r, dict) and isinstance(r.get("path"), str) and r["path"].strip() and isinstance(r.get("line"), int):
            out.append({"path": r["path"].strip(), "line": r["line"]})
    return out[:MAX_REFS]


def place(raw: dict, path: str, fl: FileLines | None) -> dict:
    """One raw finding (from the reviewer) → a placed finding dict (without scores or status yet)."""
    side = raw.get("side") if raw.get("side") in ("LEFT", "RIGHT") else "RIGHT"
    hint = raw.get("line") if isinstance(raw.get("line"), int) else None
    anchor = raw.get("anchor") or ""
    hit = fl.resolve(side, anchor, hint) if fl else None
    if hit:
        side, line, anchored = hit.side, hit.line, True
        if not anchor:
            anchor = hit.text
    else:
        line, anchored = hint or 1, False
    start = raw.get("start_line")
    if isinstance(start, int) and hint and fl and anchored:
        start = start + (line - hint)  # shift the range by however far the anchor moved it
        first = fl.at(side, start)
        if start >= line or first is None or first.hunk != hit.hunk:  # type: ignore[union-attr]
            start = None
    else:
        start = None
    category = str(raw.get("category") or "correctness")
    title = str(raw.get("title") or "").strip()[:120] or "Possible problem"
    f = {
        "id": finding_id(path, category, anchor, title), "path": path, "side": side, "line": line,
        "start_line": start, "anchored": anchored, "anchor": anchor,
        "severity": raw.get("severity") if raw.get("severity") in SEVERITY_RANK else "minor",
        "category": category, "title": title, "impact": raw.get("impact") or "", "fix": raw.get("fix") or "",
        "suggestion": raw.get("suggestion") or None, "why": raw.get("why") or "", "evidence": raw.get("evidence") or "",
        "confidence": raw.get("confidence") if isinstance(raw.get("confidence"), int) else None,
        "source": raw.get("source") or "claude", "note": raw.get("note"),
        "scenario": [str(x).strip() for x in raw.get("scenario") or [] if str(x).strip()][:MAX_STEPS],
        "question": str(raw.get("question") or "").strip() or None,
        "evidence_refs": _refs(raw.get("evidence_refs")),
        "sources": list(raw.get("sources") or (["blockers"] if str(raw.get("source") or "").startswith("rule:")
                                                else ["annotate"])),
    }
    f["one_click"] = bool(fl) and anchored and one_click_ok(fl, side, start or line, line, f["suggestion"])  # type: ignore[arg-type]
    return f


def from_legacy(old: dict, fl: FileLines | None) -> dict:
    """An older-format suggestion ({side, line, start_line, severity, body}) as a raw finding."""
    body = str(old.get("body") or "")
    first = re.split(r"(?<=[.!?])\s", body.strip(), maxsplit=1)[0].strip("*_ ")
    hit = fl.at(old.get("side", "RIGHT"), old.get("line", 0)) if fl else None
    sev = {"blocker": "major"}.get(old.get("severity"), old.get("severity"))
    return {"title": first[:70], "category": "correctness", "severity": sev, "side": old.get("side"),
            "anchor": hit.text if hit else "", "line": old.get("line"), "start_line": old.get("start_line"),
            "impact": "", "fix": "", "suggestion": None, "why": body, "evidence": "", "confidence": None}
