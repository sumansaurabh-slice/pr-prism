"""Parse `git diff --full-index -M` output into files, hunks and commentable line ranges."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_INDEX = re.compile(r"^index ([0-9a-f]+)\.\.([0-9a-f]+)")
_NULL_BLOB = re.compile(r"^0+$")
MAX_LLM_LINE = 400


@dataclass
class Line:
    kind: str  # " " context, "+" added, "-" deleted
    old: int | None
    new: int | None
    text: str


@dataclass
class Hunk:
    id: str
    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    header: str
    lines: list[Line] = field(default_factory=list)

    def contains(self, side: str, line: int) -> bool:
        if side == "RIGHT":
            return self.new_lines > 0 and self.new_start <= line < self.new_start + self.new_lines
        return self.old_lines > 0 and self.old_start <= line < self.old_start + self.old_lines


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str
    binary: bool
    base_blob: str | None
    head_blob: str | None
    additions: int
    deletions: int
    hunks: list[Hunk]
    patch: str

    def hunk_for(self, side: str, line: int) -> Hunk | None:
        return next((h for h in self.hunks if h.contains(side, line)), None)

    def hunk_infos(self) -> list[dict]:
        return [
            {"id": h.id, "old_start": h.old_start, "old_lines": h.old_lines,
             "new_start": h.new_start, "new_lines": h.new_lines, "header": h.header}
            for h in self.hunks
        ]


def _unquote(s: str) -> str:
    """Undo git's C-style path quoting ("a/caf\\303\\251.txt")."""
    if not (len(s) >= 2 and s[0] == '"' and s[-1] == '"'):
        return s
    body, out, i = s[1:-1], bytearray(), 0
    simple = {"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\", "a": b"\a", "b": b"\b", "f": b"\f", "r": b"\r", "v": b"\v"}
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in simple:
                out += simple[nxt]
                i += 2
                continue
            if nxt.isdigit():
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
        out += c.encode()
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_prefix(p: str) -> str | None:
    p = _unquote(p.rstrip("\t"))
    if p == "/dev/null":
        return None
    return p[2:] if p[:2] in ("a/", "b/") else p


def _paths_from_diff_git(rest: str) -> tuple[str, str]:
    if rest.startswith('"'):
        end = rest.index('"', 1)
        while rest[end - 1] == "\\":
            end = rest.index('"', end + 1)
        a, b = rest[:end + 1], rest[end + 2:]
        return _strip_prefix(a) or "", _strip_prefix(b) or ""
    length = (len(rest) - 5) // 2
    if rest[2 + length:5 + length] == " b/" and rest[2:2 + length] == rest[5 + length:]:
        return rest[2:2 + length], rest[5 + length:]
    a, _, b = rest.partition(" b/")
    return _strip_prefix(a) or "", _unquote(b)


def parse(diff_text: str) -> list[FileDiff]:
    # Split on "\n" only: file content may legitimately contain \r, \x0b, \x1c, ... which splitlines() eats.
    lines = diff_text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    starts = [i for i, ln in enumerate(lines) if ln.startswith("diff --git ")]
    return [_parse_file(lines[s:e]) for s, e in zip(starts, starts[1:] + [len(lines)])]


def _parse_file(sec: list[str]) -> FileDiff:
    a_path, b_path = _paths_from_diff_git(sec[0][len("diff --git "):])
    status, binary = "modified", False
    old_path: str | None = None
    rename_from = rename_to = None
    base_blob = head_blob = None
    hunks: list[Hunk] = []
    adds = dels = 0
    i = 1
    while i < len(sec) and not sec[i].startswith("@@"):
        ln = sec[i]
        if ln.startswith("new file mode"):
            status = "added"
        elif ln.startswith("deleted file mode"):
            status = "deleted"
        elif ln.startswith("rename from "):
            rename_from, status = _unquote(ln[len("rename from "):]), "renamed"
        elif ln.startswith("rename to "):
            rename_to = _unquote(ln[len("rename to "):])
        elif ln.startswith("copy from "):
            rename_from, status = _unquote(ln[len("copy from "):]), "copied"
        elif ln.startswith("copy to "):
            rename_to = _unquote(ln[len("copy to "):])
        elif m := _INDEX.match(ln):
            base_blob = None if _NULL_BLOB.match(m.group(1)) else m.group(1)
            head_blob = None if _NULL_BLOB.match(m.group(2)) else m.group(2)
        elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
            binary = True
        elif ln.startswith("--- "):
            p = _strip_prefix(ln[4:])
            if p:
                a_path = p
        elif ln.startswith("+++ "):
            p = _strip_prefix(ln[4:])
            if p:
                b_path = p
        i += 1

    current: Hunk | None = None
    old_no = new_no = 0
    for ln in sec[i:]:
        if m := _HUNK.match(ln):
            current = Hunk(
                id=f"h{len(hunks) + 1}",
                old_start=int(m.group(1)), old_lines=int(m.group(2) if m.group(2) is not None else 1),
                new_start=int(m.group(3)), new_lines=int(m.group(4) if m.group(4) is not None else 1),
                header=m.group(5),
            )
            hunks.append(current)
            old_no, new_no = current.old_start, current.new_start
            continue
        if current is None or ln.startswith("\\"):
            continue
        kind = ln[:1] or " "
        text = ln[1:]
        if kind == "+":
            current.lines.append(Line("+", None, new_no, text))
            new_no += 1
            adds += 1
        elif kind == "-":
            current.lines.append(Line("-", old_no, None, text))
            old_no += 1
            dels += 1
        else:
            current.lines.append(Line(" ", old_no, new_no, text))
            old_no += 1
            new_no += 1

    if status in ("renamed", "copied"):
        old_path, path = rename_from or a_path, rename_to or b_path
    elif status == "deleted":
        path = a_path
    else:
        path = b_path or a_path
    return FileDiff(
        path=path, old_path=old_path, status=status, binary=binary,
        base_blob=base_blob, head_blob=head_blob, additions=adds, deletions=dels,
        hunks=hunks, patch="\n".join(sec) + "\n",
    )


def render_for_llm(fd: FileDiff, hunk_ids: set[str] | None = None) -> str:
    """Hunks with explicit line numbers so the model can anchor notes: `R12 +added`, `L9 -deleted`, `R10  context`."""
    out: list[str] = []
    for h in fd.hunks:
        if hunk_ids is not None and h.id not in hunk_ids:
            continue
        out.append(f"@@ {h.id} -{h.old_start},{h.old_lines} +{h.new_start},{h.new_lines} @@ {h.header}".rstrip())
        for ln in h.lines:
            tag = f"L{ln.old}" if ln.kind == "-" else f"R{ln.new}"
            text = ln.text if len(ln.text) <= MAX_LLM_LINE else ln.text[:MAX_LLM_LINE] + " …[truncated]"
            out.append(f"{tag:>6} {ln.kind}{text}")
    return "\n".join(out)
