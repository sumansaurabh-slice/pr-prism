"""Blocker rules: a narrow pass for problems that should stop a merge, confirmed by code before it counts.

The model proposes; plain pattern checks on the diff must confirm each rule's key parts. A proposal the
checks can't confirm is still shown, as a "must fix" finding with a note, never as a blocker.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from .diffparse import FileDiff

RuleId = Literal["sql-from-request", "shell-from-request", "hardcoded-secret", "auth-check-removed", "destructive-migration"]
RULE_CATEGORY = {"sql-from-request": "security", "shell-from-request": "security", "hardcoded-secret": "security",
                 "auth-check-removed": "security", "destructive-migration": "data"}


class BlockerOut(BaseModel):
    rule_id: RuleId
    path: str
    side: Literal["LEFT", "RIGHT"]
    anchor: str
    line: int
    title: str
    impact: str
    fix: str
    evidence_quote: str


class BlockersOut(BaseModel):
    blockers: list[BlockerOut]


_REQUEST = re.compile(
    r"@(RequestParam|PathVariable|RequestBody|RequestHeader|QueryParam|PathParam|Body|Query|Param)\b"
    r"|\brequest\.(args|form|json|GET|POST|body|query|query_params|params|data|values|headers|path_params)\b"
    r"|\breq\.(body|query|params|headers)\b|\bc\.(Query|Param|PostForm|DefaultQuery|Bind\w*|ShouldBind\w*)\("
    r"|\bctx\.(Query|Param|FormValue)\(|\br\.(URL\.Query|FormValue|PostFormValue|PathValue)\(|mux\.Vars\("
    r"|\bevent\[[\"'](body|queryStringParameters|pathParameters)|\bmessage\.(value|body|payload)\b|\bpayload\[", re.I)
_SQL_TEXT = re.compile(r"\b(select|insert\s+into|update|delete\s+from)\b[^;]*\b(from|into|set|where|values)\b", re.I)
_STRING_BUILD = re.compile(r"""f["'][^"']*\{|%\s*\(|["']\s*%\s*\w|["']\s*\+\s*\w|\w\s*\+\s*["']|\.format\(|Sprintf\(|\$\{|String\.format\(|concat\(""")
_SHELL = re.compile(r"shell\s*=\s*True|\bos\.(system|popen)\(|exec\.Command\(\s*\"(sh|bash)\"|Runtime\.getRuntime\(\)\.exec"
                    r"|child_process\.(exec|execSync)\(|\bexecSync\(|\bProcessBuilder\(\s*\"(sh|bash)\"")
_SECRET = re.compile(
    r"AKIA[0-9A-Z]{16}|-----BEGIN (RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----|\bghp_[A-Za-z0-9]{36}\b|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bsk-[A-Za-z0-9]{24,}|\bAIza[0-9A-Za-z_-]{35}\b"
    r"|(password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret|private[_-]?key)\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']", re.I)
_PLACEHOLDER = re.compile(r"changeme|example|dummy|placeholder|xxx+|test|fake|sample|\$\{|<[a-z_]+>|os\.environ|getenv|process\.env", re.I)
_AUTH = re.compile(r"@PreAuthorize|@Secured|@RolesAllowed|login_required|permission_required|IsAuthenticated|has_?permission"
                   r"|check_?permission|require_?auth|requireAuth|authorize\w*|is_?admin|verify_?token|authenticate\w*|ensure_?owner|owner_?check",
                   re.I)
_DESTRUCTIVE = re.compile(r"\bdrop\s+(table|column|schema)\b|\btruncate\s+(table\s+)?\w|op\.drop_(table|column)|\.dropTable\(|\.dropColumn\(", re.I)
_DELETE_ALL = re.compile(r"\bdelete\s+from\s+\w+\s*;?\s*$", re.I)
_MIGRATION = re.compile(r"(^|/)(migrations?|migrate|alembic|db|sql|schema)/|\.sql$", re.I)


def _added(fd: FileDiff) -> list[str]:
    return [ln.text for h in fd.hunks for ln in h.lines if ln.kind == "+"]


def _deleted(fd: FileDiff) -> list[str]:
    return [ln.text for h in fd.hunks for ln in h.lines if ln.kind == "-"]


def corroborate(b: dict, fd: FileDiff | None, head_text: str) -> str | None:
    """None if the code confirms the rule's key parts; otherwise the plain reason it could not."""
    if fd is None:
        return "the file is not in this PR's diff"
    added, deleted = _added(fd), _deleted(fd)
    hunk_text = "\n".join(ln.text for h in fd.hunks for ln in h.lines)
    rule = b["rule_id"]
    if rule == "sql-from-request":
        if not any(_SQL_TEXT.search(t) and _STRING_BUILD.search(t) for t in added):
            return "no added line builds SQL text from joined or formatted strings"
        if not (_REQUEST.search(hunk_text) or _REQUEST.search(head_text)):
            return "no request input is visible in this file"
        return None
    if rule == "shell-from-request":
        if not any(_SHELL.search(t) for t in added):
            return "no added line runs a shell command"
        if not (_REQUEST.search(hunk_text) or _REQUEST.search(head_text)):
            return "no request input is visible in this file"
        return None
    if rule == "hardcoded-secret":
        if not any((m := _SECRET.search(t)) and not _PLACEHOLDER.search(m.group(0)) for t in added):
            return "no added line looks like a real credential"
        return None
    if rule == "auth-check-removed":
        removed = {m.group(0).lower() for t in deleted for m in _AUTH.finditer(t)}
        kept = {m.group(0).lower() for t in added for m in _AUTH.finditer(t)}
        if not removed:
            return "no deleted line has a login or permission check"
        if removed <= kept:
            return "the same checks are added back in this file"
        return None
    if rule == "destructive-migration":
        hits = [t for t in added if _DESTRUCTIVE.search(t) or _DELETE_ALL.search(t)]
        if not hits:
            return "no added line drops, truncates or deletes everything"
        if not (_MIGRATION.search(fd.path) or "migration" in head_text.lower()):
            return "the file doesn't look like a migration or a database script"
        return None
    return "unknown rule"


def to_raw_finding(b: dict, problem: str | None) -> dict:
    """A blocker proposal as a raw finding for findings.place(): a blocker if confirmed, else must-fix."""
    note = None if problem is None else f"Shown as must fix, not as a blocker: the code check found {problem}."
    return {
        "title": b["title"], "category": RULE_CATEGORY.get(b["rule_id"], "security"),
        "severity": "blocker" if problem is None else "major", "side": b["side"], "anchor": b["anchor"],
        "line": b["line"], "impact": b["impact"], "fix": b["fix"], "suggestion": None,
        "why": f"Evidence from the diff:\n\n```\n{b['evidence_quote'].strip()}\n```", "evidence": "diff only",
        "confidence": 95 if problem is None else 75, "source": f"rule:{b['rule_id']}", "note": note,
    }


def head_text(worktree: Path, path: str) -> str:
    try:
        return (worktree / path).read_text(errors="replace")[:400_000]
    except OSError:
        return ""
