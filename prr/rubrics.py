"""Which review checklists apply to a batch of files, and the repo's shared review checklist.

The repo's .pr-prism/guidelines.md is read from the merge-base, not from the PR: a PR must not be able to change the
rules it is reviewed against. The repo's own rules for its code (CLAUDE.md, AGENTS.md, …) are in prr/reporules.py.
"""

from __future__ import annotations

import functools
import posixpath
import re
from pathlib import Path

from .config import PROMPTS_DIR
from .gitgh import git

RUBRICS_DIR = PROMPTS_DIR / "rubrics"
MAX_CONVENTIONS = 20_000
TEAM_CHECKLIST = ".pr-prism/guidelines.md"  # optional, in the reviewed repo: the team's shared review checklist

_BY_EXT = {
    ".py": "python", ".go": "go", ".java": "java", ".kt": "java", ".kts": "java", ".scala": "java", ".groovy": "java",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript", ".ts": "javascript",
    ".tsx": "javascript", ".vue": "javascript", ".svelte": "javascript", ".swift": "swift", ".dart": "dart",
    ".tf": "terraform", ".tfvars": "terraform", ".hcl": "terraform", ".sql": "sql",
}
_ANDROID = re.compile(r"(^|/)(android|app/src/main)/|Activity\.kt$|Fragment\.kt$|ViewModel\.kt$|Screen\.kt$")


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


@functools.lru_cache(maxsize=16)
def team_checklist(repo_root: str, merge_base: str) -> str:
    """The reviewed repo's shared checklist (.pr-prism/guidelines.md) at the merge-base, without HTML comments.
    Empty when the repo has none. Cached: every batch of a review asks for the same commit."""
    text = git("show", f"{merge_base}:{TEAM_CHECKLIST}", cwd=Path(repo_root), check=False)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()
    if len(text) > MAX_CONVENTIONS:
        text = text[:MAX_CONVENTIONS] + "\n…[cut]"
    return text
