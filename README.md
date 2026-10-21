# /pr-review: interactive, Claude-guided PR reviews

A Claude Code skill that turns a large GitHub pull request (5–10k lines) into a local review app. The app looks and feels like GitHub's own PR pages, and everything Claude writes uses plain, simple English (about 10th-class level: short sentences, everyday words, no jargon or buzzwords).

- **Reading order**: files grouped into chapters and ordered so each one only depends on what you've already read. Each file says why to read it now.
- **Overview**: a short summary, why the change was made, how it works, what could go wrong, and questions to ask the author.
- **Files changed**: the diff with Claude's notes (explains / possible problem / data flow / missing test) right under the lines they refer to, shown like GitHub review comments. Claude's suggested comments sit next to them; you add, edit or dismiss each one. Mark files as **Viewed**, like on GitHub.
- **Data Flow**: interactive graphs of the runtime paths the PR adds or changes. Click a node to jump to the code.
- **Impact**: breaking changes, migrations, config, security, and the changed symbols that are referenced *outside* the PR (found with codegraph when the repo is indexed, `git grep` otherwise).
- **Chat**: deep-dive questions answered by a real Claude Code instance running read-only inside the PR's checkout. It streams its answer, shows which files it reads, and cites `file:line` references you can click.
- **Publish**: draft comments inline, then preview the exact GitHub payload and submit as Comment, Approve, Request changes, or a pending review.

## Install

```bash
./install.sh            # creates .venv, builds web/, symlinks skill/ → ~/.claude/skills/pr-review
./install.sh --uninstall
```

**Requirements:**
- Python 3.11+
- Node 20+ (to build the UI)
- `git`
- `gh`, logged in
- Claude Code, logged in

## Use

Inside Claude Code, in the repo the PR belongs to:

```
/pr-review 1234
/pr-review https://github.com/org/repo/pull/1234 focus on the retry logic and the migration
/pr-review status            # fetched PRs, analysis state, running servers
/pr-review stop 1234         # stop that PR's web server
/pr-review clean 1234        # remove the worktree and cached data (or: clean --all)
```

**What the skill does:**
1. `prr fetch` checks the PR head out into an isolated worktree under `~/.cache/pr-review/`. Your working copy is never touched.
2. `prr serve` opens the app; the review path is usable right away.
3. `prr analyze` runs Claude's analysis in the background. The app fills in as each batch finishes.

Rerunning after the PR changes only re-annotates the files that changed, and draft comments are remapped to the new lines.

**Keyboard:**

| Key | Action |
|---|---|
| `j` / `k` | Next / previous file |
| `n` / `p` | Next / previous note |
| `c` | Comment |
| `r` | Mark reviewed |
| `/` | Chat |
| `?` | Help |

## Make it yours

- `skill/review-guidelines.md` is your personal review checklist. It is injected into every analysis prompt.
- `prr/prompts/style.md` holds the plain-language writing rules (short sentences, everyday words, banned buzzwords). Every prompt includes it, so all of Claude's text follows it.
- `prr/prompts/*.md` holds the detailed instructions for annotation, synthesis and chat.
- `config.toml` sets the models (annotate / synthesize / chat), concurrency, batch size and cache location.

## How it works

```
/pr-review (SKILL.md) ─► prr fetch ─► context.json, diff.patch, worktree/      (deterministic: no LLM)
                      ─► prr serve ─► FastAPI on 127.0.0.1 + React app
                      ─► prr analyze ─► N × `claude -p --json-schema` (per batch of related files, parallel)
                                     ─► impact (codegraph / git grep, no LLM)
                                     ─► 1 × `claude -p --json-schema` synthesis
                                     ─► analysis.json   (repaired, never rejected)
browser chat ─► POST /api/chat/... ─► `claude -p --resume <session>` (read-only tools, cwd = worktree) ─► SSE
Submit       ─► POST /api/review/submit ─► `gh api POST /repos/{o}/{r}/pulls/{n}/reviews`
```

**Guarantees:**
- Python computes the facts. Categories, generated/vendored detection, sensitive paths, the import graph, the dependency order, reading-time estimates and the lines that can take a comment involve no LLM.
- Claude does the judgment: summaries, risk, notes, chapters, flows, impact narrative and suggestions.
- Bad model output is repaired, not rejected. An anchor outside the diff is clamped or folded into the file summary, an unknown path is dropped, and a file the guide forgot still lands in a chapter. All repairs are listed in `repair_log`.

**Safety:**
- **Network:** the server binds to 127.0.0.1 only and uses a random token that becomes an HttpOnly SameSite=Strict cookie. It checks the Host header (blocks DNS rebinding) and the Origin of every state-changing request. It sends a strict CSP and renders markdown without raw HTML.
- **Claude calls:** every call uses `--setting-sources user --strict-mcp-config`, read-only tools and `--permission-mode dontAsk`. A PR's own `.claude/settings.json` hooks or `.mcp.json` never run, and chat can't execute code.
- **Git:** worktree checkouts run with `core.hooksPath=/dev/null`.
- **GitHub:** nothing is posted until you confirm the previewed payload.

## Develop

```bash
.venv/bin/python -m pytest          # backend tests
cd web && npm run typecheck && npm run build
cd web && npm run mock              # dependency-free mock API (web/mock/*) for UI work without a PR
cd web && PRR_PORT=<port of a running prr server> npm run dev   # UI dev server with API proxy
```

API contract: `docs/api.md`. Types: `prr/models.py` ↔ `web/src/types.ts`.
