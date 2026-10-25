#!/usr/bin/env bash
# Publish a PR Prism release for plugin installs, at the version in pyproject.toml (the one version source).
#   scripts/release.sh --dry-run   print every command; run nothing, change nothing
#   scripts/release.sh             check, ask, then build, commit, tag, push and publish v<version>
#
# What a release holds: the web app built from this commit (web-dist.tar.gz and its .sha256) on the GitHub release,
# and, in the tagged commit, skills/pr-prism/web-dist.sha256 plus the version stamped into .claude-plugin/plugin.json.
# bin/prr only uses a downloaded web app that matches the committed checksum, so it is trusted exactly as much as the
# tag. To release: bump `version` in pyproject.toml, then run this from a clean main.
set -euo pipefail

GH_REPO="sumansaurabh-slice/pr-prism"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO/.release"
DRY=0
case "${1:-}" in
  --dry-run) DRY=1 ;;
  "") ;;
  *) echo "usage: scripts/release.sh [--dry-run]" >&2; exit 2 ;;
esac

VERSION="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb"))["project"]["version"])' \
  "$REPO/pyproject.toml")"
TAG="v$VERSION"
NOTES="PR Prism $TAG. Install: claude plugin marketplace add $GH_REPO, then claude plugin install pr-prism@pr-prism. \
web-dist.tar.gz is the prebuilt web app; bin/prr checks it against skills/pr-prism/web-dist.sha256 in this tag."

step() {  # print a command, then run it (only print it in a dry run)
  printf '+'
  printf ' %q' "$@"
  printf '\n'
  if [ "$DRY" = 0 ]; then "$@"; fi
}

fail() { echo "✗ $*" >&2; exit 1; }

echo "PR Prism $TAG → github.com/$GH_REPO$([ "$DRY" = 1 ] && echo ' (dry run: nothing runs)')"

# Checks. A dry run prints them instead.
if [ "$DRY" = 1 ]; then
  echo "+ check: on main, a clean tree (pyproject.toml may hold the version bump), $TAG is new, gh is logged in"
else
  for tool in git gh npm python3; do command -v "$tool" >/dev/null || fail "$tool is not installed"; done
  [ "$(git -C "$REPO" rev-parse --abbrev-ref HEAD)" = "main" ] || fail "release from main"
  [ -z "$(git -C "$REPO" status --porcelain -- . ':!pyproject.toml')" ] || fail "commit or stash your changes first"
  ! git -C "$REPO" rev-parse -q --verify "refs/tags/$TAG" >/dev/null || fail "$TAG already exists; bump the version"
  [ -z "$(git -C "$REPO" ls-remote --tags origin "refs/tags/$TAG")" ] || fail "$TAG already exists on origin"
  gh auth status >/dev/null 2>&1 || fail "gh is not logged in: gh auth login"
  read -r -p "Publish PR Prism $TAG to github.com/$GH_REPO? [y/N] " answer
  [[ "$answer" == [yY] ]] || { echo "Stopped; nothing was changed."; exit 1; }
fi

step npm --prefix "$REPO/web" ci --no-audit --no-fund --loglevel=error
step npm --prefix "$REPO/web" run build --silent
step rm -rf "$OUT"
step python3 "$REPO/scripts/release_files.py" pack "$REPO/web/dist" "$OUT/web-dist.tar.gz"
step python3 "$REPO/scripts/release_files.py" stamp "$VERSION" "$OUT/web-dist.tar.gz.sha256"
if command -v claude >/dev/null; then
  step claude plugin validate "$REPO"
fi
step git -C "$REPO" add pyproject.toml .claude-plugin/plugin.json skills/pr-prism/web-dist.sha256
step git -C "$REPO" commit -m "Release $TAG"
step git -C "$REPO" tag -a "$TAG" -m "PR Prism $TAG"
step git -C "$REPO" push origin HEAD
step git -C "$REPO" push origin "$TAG"
step gh release create "$TAG" -R "$GH_REPO" --verify-tag --title "PR Prism $TAG" --notes "$NOTES" \
  "$OUT/web-dist.tar.gz" "$OUT/web-dist.tar.gz.sha256"

if [ "$DRY" = 1 ]; then
  echo "Dry run: nothing was run or changed."
else
  echo "Released $TAG. Teammates get it with: claude plugin update pr-prism@pr-prism"
fi
