#!/usr/bin/env bash
# Install the /pr-prism Claude Code skill from this repo (the dev install; teammates can use the plugin, see README).
#   ./install.sh              venv + deps, build the web UI, symlink skills/pr-prism into ~/.claude/skills/pr-prism
#   ./install.sh --uninstall  remove the symlink (leaves this repo and cached reviews alone)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL="$REPO/skills/pr-prism"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SKILLS_DIR="$CLAUDE_DIR/skills"
LINK="$SKILLS_DIR/pr-prism"
OLD_LINK="$SKILLS_DIR/pr-review"   # the skill's name before it was renamed to pr-prism

remove_old_link() {
  if [ -L "$OLD_LINK" ] && [ "$(readlink "$OLD_LINK")" = "$REPO/skill" ]; then
    rm "$OLD_LINK"
    echo "Removed the old /pr-review shortcut ($OLD_LINK)"
  fi
}

# The PR Prism plugin's install ids (pr-prism@<marketplace>), from Claude Code's install records.
plugin_installs() {
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$CLAUDE_DIR/plugins/installed_plugins.json" <<'PY'
import json, sys
try:
    with open(sys.argv[1]) as f:
        plugins = json.load(f).get("plugins") or {}
except (OSError, ValueError, AttributeError):
    plugins = {}
for key in plugins:
    if key.split("@")[0] == "pr-prism":
        print(key)
PY
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

PLUGINS="$(plugin_installs)"
if [ -n "$PLUGINS" ]; then
  FIRST="$(echo "$PLUGINS" | head -n 1)"
  echo "✗ PR Prism is already installed as a Claude Code plugin ($(echo "$PLUGINS" | paste -sd ' ' -))." >&2
  echo "  Installing it from this folder too would give you two /pr-prism skills. Use one of them:" >&2
  echo "  - keep the plugin: nothing to do here (to update it: claude plugin update $FIRST)" >&2
  echo "  - or use this folder: claude plugin uninstall $FIRST, then run ./install.sh again" >&2
  exit 1
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

echo "→ Skill ($LINK → $SKILL)"
chmod +x "$SKILL/bin/prr"
mkdir -p "$SKILLS_DIR"
if [ -e "$LINK" ] && [ ! -L "$LINK" ]; then
  echo "✗ $LINK exists and is not a symlink; move it away and rerun." >&2
  exit 1
fi
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" != "$SKILL" ]; then
  OLD_TARGET="$(readlink "$LINK")"
  if [ "$OLD_TARGET" = "$REPO/skill" ]; then
    echo "  Replaced the old link to $OLD_TARGET: the skill moved to skills/pr-prism"
  else
    echo "  Replaced the old link to $OLD_TARGET"
  fi
fi
ln -sfn "$SKILL" "$LINK"
remove_old_link

echo
"$SKILL/bin/prr" doctor || true
# After a git pull: review servers started before it still run the old code until they restart.
"$REPO/.venv/bin/python" -P -c 'import sys; from prr.install import print_old_servers; print_old_servers(sys.argv[1])' \
  "$LINK/bin/prr" || true
echo
echo "Installed. In Claude Code, inside any GitHub repo:"
echo "  /pr-prism 1234"
echo "  /pr-prism https://github.com/org/repo/pull/1234 focus on the retry logic"
