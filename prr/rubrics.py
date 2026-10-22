"""Which review checklists apply to a batch of files, and the repo's own conventions.

The repo's CLAUDE.md / AGENTS.md is read from the merge-base, not from the PR: a PR must not be able to
change the rules it is reviewed against.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

from .config import PROMPTS_DIR
from .gitgh import git

RUBRICS_DIR = PROMPTS_DIR / "rubrics"
MAX_CONVENTIONS = 20_000

_BY_EXT = {
    ".py": "python", ".go": "go", ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java", ".groovy": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "javascript",
    ".tsx": "javascript", ".vue": "javascript", ".svelte": "javascript", ".swift": "swift", ".dart": "dart",
    ".tf": "terraform", ".tfvars": "terraform", ".hcl": "terraform", ".sql": "sql",
}
_ANDROID = re.compile(r"(^|/)(android|app/src/main)/|Activity\.kt$|Fragment\.kt$|ViewModel\.kt$|Screen\.kt$")
_IMPORT = re.compile(r"^@([\w./-]+\.md)\s*$", re.M)


def rubric_names(paths: list[str]) -> list[str]:
    names = []
    for p in paths:
        ext = posixpath.splitext(p)[1].lower()
        name = _BY_EXT.get(ext)
        if ext in (".kt", ".kts") and _ANDROID.search(p):
            name = "kotlin"
        if "/migrations/" in p or "/migrate/" in p:
            names.append("sql")
        if name:
            names.append(name)
    return list(dict.fromkeys(names))


def rubrics_for(paths: list[str]) -> str:
    """The general checklist plus one per language in these files."""
    parts = [(RUBRICS_DIR / "general.md").read_text().strip()]
    for name in rubric_names(paths):
        f = RUBRICS_DIR / f"{name}.md"
        if f.exists():
            parts.append(f.read_text().strip())
    return "\n\n".join(parts)


def repo_conventions(repo_root: Path, merge_base: str) -> str:
    """CLAUDE.md (or AGENTS.md) at the merge-base, with `@file.md` lines inlined one level deep."""
    def show(path: str) -> str:
        return git("show", f"{merge_base}:{path}", cwd=repo_root, check=False)

    text = ""
    for name in ("CLAUDE.md", "AGENTS.md"):
        text = show(name)
        if text.strip():
            break
    if not text.strip():
        return ""

    def inline(m: re.Match[str]) -> str:
        body = show(m.group(1))
        return body if body.strip() else m.group(0)

    text = _IMPORT.sub(inline, text)
    if len(text) > MAX_CONVENTIONS:
        text = text[:MAX_CONVENTIONS] + "\n…[cut]"
    return text.strip()
