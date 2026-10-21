#!/usr/bin/env bash
# Install the /pr-prism Claude Code skill from this repo.
#   ./install.sh              venv + deps, build the web UI, symlink skill/ into ~/.claude/skills/pr-prism
#   ./install.sh --uninstall  remove the symlink (leaves this repo and cached reviews alone)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="$HOME/.claude/skills"
LINK="$SKILLS_DIR/pr-prism"
OLD_LINK="$SKILLS_DIR/pr-review"   # the skill's name before it was renamed to pr-prism

remove_old_link() {
  if [ -L "$OLD_LINK" ] && [ "$(readlink "$OLD_LINK")" = "$REPO/skill" ]; then
    rm "$OLD_LINK"
    echo "Removed the old /pr-review shortcut ($OLD_LINK)"
  fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
  remove_old_link
  if [ -L "$LINK" ]; then
    rm "$LINK"
    echo "Removed $LINK"
  else
    echo "Nothing to remove at $LINK"
  fi
  echo "Cached reviews (worktrees, analyses) live in ~/.cache/pr-prism; run 'prr clean --all' inside a repo to remove them."
  exit 0
fi

need() {
  command -v "$1" >/dev/null 2>&1 || { echo "✗ '$1' not found: $2" >&2; exit 1; }
}
need python3 "install Python 3.11+"
need node "install Node 20+ (builds the web UI)"
need npm "install npm"
need git "install git"
need gh "brew install gh && gh auth login"
need claude "install Claude Code"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || { echo "✗ Python 3.11+ required (found $(python3 --version))" >&2; exit 1; }

echo "→ Python environment (.venv)"
[ -d "$REPO/.venv" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install --quiet --upgrade pip
"$REPO/.venv/bin/pip" install --quiet -e "$REPO[dev]"

echo "→ Web UI (web/dist)"
(
  cd "$REPO/web"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund --loglevel=error; else npm install --no-audit --no-fund --loglevel=error; fi
  npm run build --silent
)

echo "→ Skill (~/.claude/skills/pr-prism → $REPO/skill)"
chmod +x "$REPO/skill/bin/prr"
mkdir -p "$SKILLS_DIR"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "✗ $LINK exists and is not a symlink; move it away and rerun." >&2
  exit 1
fi
ln -sfn "$REPO/skill" "$LINK"
remove_old_link

echo
"$REPO/skill/bin/prr" doctor || true
echo
echo "Installed. In Claude Code, inside any GitHub repo:"
echo "  /pr-prism 1234"
echo "  /pr-prism https://github.com/org/repo/pull/1234 focus on the retry logic"
