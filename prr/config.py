"""Settings from <repo>/config.toml, plus paths shared by every command."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
SKILL_DIR = REPO_DIR / "skill"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
WEB_DIST = REPO_DIR / "web" / "dist"
GUIDELINES_FILE = SKILL_DIR / "review-guidelines.md"


@dataclass
class Models:
    annotate: str = "sonnet"
    docs: str = "haiku"
    synthesize: str = "opus"
    chat: str = "opus"
    blockers: str = "sonnet"   # the blocker-rule pass
    judge: str = "haiku"       # the second look at every finding
    ci: str = "sonnet"         # explaining CI failures, other tools' comments, re-checks


@dataclass
class AnalysisSettings:
    concurrency: int = 6
    cluster_tokens: int = 12_000   # small batches, so the reviewer can read every file in full
    cluster_max_files: int = 8
    retries: int = 2
    annotate_timeout_s: int = 900
    synthesize_timeout_s: int = 1200


@dataclass
class ChatSettings:
    can_post: bool = True      # the chat may post comments and comment-only reviews to GitHub when asked
    can_approve: bool = False  # the chat may also approve or request changes (off: those stay on the Submit button)
    ask_before_actions: bool = True  # commands, edits and connector actions ask the reviewer first (off: no questions)


@dataclass
class Config:
    cache_dir: Path = Path("~/.cache/pr-prism").expanduser()
    models: Models = field(default_factory=Models)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    chat: ChatSettings = field(default_factory=ChatSettings)


def load_config() -> Config:
    cfg = Config()
    path = REPO_DIR / "config.toml"
    if not path.exists():
        return cfg
    raw = tomllib.loads(path.read_text())
    if "cache_dir" in raw:
        cfg.cache_dir = Path(raw["cache_dir"]).expanduser()
    for key, value in raw.get("models", {}).items():
        if hasattr(cfg.models, key):
            setattr(cfg.models, key, str(value))
    for key, value in raw.get("analysis", {}).items():
        if hasattr(cfg.analysis, key):
            setattr(cfg.analysis, key, int(value))
    for key, value in raw.get("chat", {}).items():
        if hasattr(cfg.chat, key):
            setattr(cfg.chat, key, bool(value))
    return cfg


def pr_dir(cfg: Config, owner: str, repo: str, number: int) -> Path:
    return cfg.cache_dir / f"{owner}__{repo}" / f"pr-{number}"
