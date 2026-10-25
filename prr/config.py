"""Settings from config.toml (the repo's defaults, then yours in ~/.config/pr-prism), and paths every command uses."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent  # the repo, or the plugin's folder (bin/prr installs it editable)
SKILL_DIR = REPO_DIR / "skills" / "pr-prism"
PROMPTS_DIR = PACKAGE_DIR / "prompts"
GUIDELINES_FILE = SKILL_DIR / "review-guidelines.md"
USER_CONFIG_FILE = Path("~/.config/pr-prism/config.toml").expanduser()  # yours: overrides <repo>/config.toml per key
TOOL_CACHE = Path("~/.cache/pr-prism")  # bin/prr keeps venv-<version> and web-<version> here (expanded when used)


def version() -> str:
    """This copy's version. pyproject.toml is the one source: bin/prr and scripts/release.sh read it too."""
    try:
        return str(tomllib.loads((REPO_DIR / "pyproject.toml").read_text())["project"]["version"])
    except (OSError, KeyError, ValueError):
        from importlib.metadata import PackageNotFoundError
        from importlib.metadata import version as installed
        try:
            return installed("prr")
        except PackageNotFoundError:
            return "unknown"


def find_web_dist() -> Path:
    """The built web app: PRR_WEB_DIST, else <repo>/web/dist if it is built (a dev checkout), else the copy bin/prr
    downloaded for this version."""
    if os.environ.get("PRR_WEB_DIST"):
        return Path(os.environ["PRR_WEB_DIST"]).expanduser()
    dev = REPO_DIR / "web" / "dist"
    if (dev / "index.html").is_file():
        return dev
    return TOOL_CACHE.expanduser() / f"web-{version()}"


WEB_DIST = find_web_dist()


@dataclass
class Models:
    annotate: str = "sonnet"
    docs: str = "haiku"
    synthesize: str = "opus"
    chat: str = "opus"
    blockers: str = "sonnet"   # the blocker-rule pass
    judge: str = "haiku"       # the second look at every finding
    ci: str = "sonnet"         # explaining CI failures, other tools' comments, re-checks
    deepcheck: str = "opus"    # the deep check: "what could break" in the riskiest areas


@dataclass
class AnalysisSettings:
    concurrency: int = 6
    cluster_tokens: int = 12_000   # small batches, so the reviewer can read every file in full
    cluster_max_files: int = 8
    retries: int = 2
    annotate_timeout_s: int = 900
    synthesize_timeout_s: int = 1200
    deepcheck: bool = True             # run the deep check after the summary
    deepcheck_max_targets: int = 5     # areas checked per PR; riskier areas past this are named as skipped
    deepcheck_target_timeout_s: int = 900  # one area's time limit
    deepcheck_budget_s: int = 1500     # no area starts (or retries) after this; running ones stop 60 s later
    recheck_max: int = 20              # earlier problems and comments re-checked by Claude per run; the rest: "not checked"


@dataclass
class ChatSettings:
    can_post: bool = True      # the chat may post comments and comment-only reviews to GitHub when asked
    can_approve: bool = False  # the chat may also approve or request changes (off: those stay on the Submit button)
    ask_before_actions: bool = True  # commands, edits and connector actions ask the reviewer first (off: no questions)


@dataclass
class BudgetSettings:
    warn_usd: float = 0.0  # the app shows "over your $N limit" once a PR's spend passes this (0: off); warning only


@dataclass
class ReviewSettings:
    max_stops: int = 12  # guided review lists at most this many places (answered ones always stay); the rest: "N more"


@dataclass
class Config:
    cache_dir: Path = Path("~/.cache/pr-prism").expanduser()
    models: Models = field(default_factory=Models)
    analysis: AnalysisSettings = field(default_factory=AnalysisSettings)
    chat: ChatSettings = field(default_factory=ChatSettings)
    budget: BudgetSettings = field(default_factory=BudgetSettings)
    review: ReviewSettings = field(default_factory=ReviewSettings)


def _flag(value: object) -> bool:
    """A TOML boolean, or a string such as "false" written by hand."""
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def config_files() -> list[Path]:
    """The repo's defaults first, then yours: a key set in your file wins."""
    return [REPO_DIR / "config.toml", USER_CONFIG_FILE]


def read_raw_config() -> dict:
    """The config files merged key by key; inside a section like [models], only the keys you set change."""
    raw: dict = {}
    for path in config_files():
        if not path.exists():
            continue
        for key, value in tomllib.loads(path.read_text()).items():
            raw[key] = {**raw[key], **value} if isinstance(value, dict) and isinstance(raw.get(key), dict) else value
    return raw


def load_config() -> Config:
    cfg = Config()
    raw = read_raw_config()
    if "cache_dir" in raw:
        cfg.cache_dir = Path(raw["cache_dir"]).expanduser()
    for key, value in raw.get("models", {}).items():
        if hasattr(cfg.models, key):
            setattr(cfg.models, key, str(value))
    for key, value in raw.get("analysis", {}).items():
        if hasattr(cfg.analysis, key):
            setattr(cfg.analysis, key, _flag(value) if isinstance(getattr(cfg.analysis, key), bool) else int(value))
    for key, value in raw.get("chat", {}).items():
        if hasattr(cfg.chat, key):
            setattr(cfg.chat, key, bool(value))
    for key, value in raw.get("budget", {}).items():
        if hasattr(cfg.budget, key):
            setattr(cfg.budget, key, float(value))
    for key, value in raw.get("review", {}).items():
        if hasattr(cfg.review, key):
            setattr(cfg.review, key, int(value))
    return cfg


def pr_dir(cfg: Config, owner: str, repo: str, number: int) -> Path:
    return cfg.cache_dir / f"{owner}__{repo}" / f"pr-{number}"
