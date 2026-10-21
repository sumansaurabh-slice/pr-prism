# PR Prism (`/pr-prism`): interactive, Claude-guided PR reviews

A Claude Code skill that turns a large GitHub pull request (5–10k lines) into a local review app. The app looks and feels like GitHub's own PR pages, and everything Claude writes uses plain, simple English (about 10th-class level: short sentences, everyday words, no jargon or buzzwords).

- **Reading order**: files grouped into chapters and ordered so each one only depends on what you've already read. Each file says why to read it now. Checkboxes hide viewed files, **test files** and **docs files**; `j`/`k` and "Mark viewed & next" skip hidden files too.
- **Overview**: a short summary, why the change was made, how it works, what could go wrong, and questions to ask the author.
- **Files changed**: the diff with Claude's notes (explains / possible problem / data flow / missing test) right under the lines they refer to, shown like GitHub review comments. Claude's suggested comments sit next to them; you add, edit or dismiss each one. Mark files as **Viewed**, like on GitHub.
- **Data Flow**: interactive graphs of the runtime paths the PR adds or changes. Click a node to jump to the code.
- **Impact**: breaking changes, migrations, config, security, and the changed symbols that are referenced *outside* the PR (found with codegraph when the repo is indexed, `git grep` otherwise).
- **Chat**: deep-dive questions answered by a real Claude Code instance inside the PR's checkout. It streams its answer, shows which files it reads, and cites `file:line` references you can click. Ask it to "add a comment on line 42" (it goes to your pending comments) or "post this on GitHub" (it posts right away, under your name).
- **Publish**: draft comments inline, then preview the exact GitHub payload and submit as Comment, Approve, Request changes, or a pending review.

## Install

```bash
./install.sh            # creates .venv, builds web/, symlinks skill/ → ~/.claude/skills/pr-prism
./install.sh --uninstall
```

**Coming from the old `/pr-review` name?** Run `./install.sh` again. It installs `/pr-prism` and removes the old `/pr-review` shortcut. Reviews made under the old name stay in `~/.cache/pr-review`. You can delete that folder when you no longer need them.

**Requirements:**
- Python 3.11+
- Node 20+ (to build the UI)
- `git`
- `gh`, logged in
- Claude Code, logged in

## Use

Inside Claude Code, in the repo the PR belongs to:

```
/pr-prism 1234
/pr-prism https://github.com/org/repo/pull/1234 focus on the retry logic and the migration
/pr-prism status            # fetched PRs, analysis state, running servers
/pr-prism stop 1234         # stop that PR's web server (the only way it stops)
/pr-prism clean 1234        # remove the worktree and cached data (or: clean --all)
```

**What the skill does:**
1. `prr fetch` checks the PR head out into an isolated worktree under `~/.cache/pr-prism/`. Your working copy is never touched.
2. `prr serve` opens the app; the review path is usable right away. The server keeps running until you stop it with `/pr-prism stop`. If it crashes, it restarts itself. Each PR always uses the same port and link (for example `http://127.0.0.1:47321/`), so an open browser tab keeps working after a restart, and even after a reboot once you run `/pr-prism` again.
3. `prr analyze` runs Claude's analysis in the background. The app fills in as each batch finishes.

Rerunning after the PR changes only re-annotates the files that changed, and draft comments are remapped to the new lines.

**Keyboard:**

| Key | Action |
|---|---|
| `j` / `k` | Next / previous file |
| `n` / `p` | Next / previous note |
| click a line number | Pick a line |
| drag, or shift-click another number | Pick more lines for one comment, like on GitHub. A pick can start on a red (deleted) line and end on a green one, but it must stay inside one `@@` block. |
| `c` | Comment on the picked lines |
| `r` | Mark reviewed |
| `/` | Chat |
| `?` | Help |

## Use it in another repo (example: ai-banker)

These steps use `~/Documents/code-repos/ai-banker` (GitHub: `ne-bank/ai-banker`).

**You never run `install.sh` inside ai-banker.** You run it once, inside this PR Prism folder. It teaches Claude Code the `/pr-prism` command for your user account, so the command works in every repo on your machine. Three places are involved:

```
~/Documents/hackathon/github-pr-review     ← this tool: code, Python setup (.venv), built web app (web/dist).
        │                                     Run ./install.sh here, once.
        │  install.sh creates a shortcut (symlink) to this folder:
        ▼
~/.claude/skills/pr-prism                 ← Claude Code looks for skills here, in every folder

~/Documents/code-repos/ai-banker           ← your project. Nothing is installed here.
```

There is no separate build or package to export. The shortcut points to this folder, so this folder is the installed copy. If you edit or `git pull` it, `/pr-prism` uses the new version.

### 1. Install the skill once (in this folder, not in ai-banker)

Skip this if you have already run it.

```bash
cd ~/Documents/hackathon/github-pr-review
./install.sh
```

On a new computer, or for a teammate, copy the tool first, then install it:

```bash
git clone https://github.com/sumansaurabh-slice/pr-prism ~/pr-prism
cd ~/pr-prism && ./install.sh
```

### 2. Check that it works in ai-banker

```bash
cd ~/Documents/code-repos/ai-banker
~/.claude/skills/pr-prism/bin/prr doctor     # every line should start with ✓
gh pr list --limit 5                          # should list ai-banker PRs
```

If `gh pr list` fails with a "SAML" or "SSO" error, your GitHub login is not yet allowed for the `ne-bank` organization:
1. Run `gh auth refresh -h github.com -s repo`.
2. Allow `ne-bank` when the browser asks.

### 3. Refresh the codegraph index (optional, but better results)

ai-banker has a `.codegraph/` index. The **Impact** tab and the chat use it to find other code that calls what the PR changed. Update it before a big review:

```bash
cd ~/Documents/code-repos/ai-banker
codegraph sync
```

### 4. Review a PR

Open Claude Code inside ai-banker and run the skill:

```bash
cd ~/Documents/code-repos/ai-banker
claude
```

```
/pr-prism 966
/pr-prism 966 focus on the grouping logic and whether amounts can be counted twice
/pr-prism https://github.com/ne-bank/ai-banker/pull/966
```

This is what happens:
1. **Fetch.** The PR's code is copied to `~/.cache/pr-prism/ne-bank__ai-banker/pr-966/`. Your ai-banker folder, your branch and your uncommitted changes are not touched. The only things added inside ai-banker's `.git` folder are a few refs named `refs/pr-prism/966/*` and a git worktree entry for the copy. `/pr-prism clean` removes both.
2. **Open the app.** The review app opens in your browser. The reading order works right away.
3. **Analyze.** Claude reads the PR in the background, usually in 2 to 10 minutes. Notes, the summary, data flows and suggested comments show up as each part finishes.

### 5. Read, ask and comment

- Go through the chapters in the left sidebar from top to bottom. Tick **Viewed** on each file as you finish it.
- Ask questions in the chat on the right. Claude looks at the real ai-banker code to answer.
- Add your own comments, or Claude's suggested ones. Then press **Submit review**. The review is posted to `ne-bank/ai-banker` as your GitHub user, and only after you press the green button.
- On your own PRs, GitHub only allows **Comment**, not Approve or Request changes.

### 6. Add ai-banker's own review rules (optional)

Put team rules under "Team context" in `skill/review-guidelines.md`. Claude follows them in every review. Note that this file applies to **all** repos, not only ai-banker. Some example rules you might add:

```markdown
## Team context
- Money amounts must never be stored or summed as floats.
- Kafka consumers must be safe to run twice on the same message.
- Database migrations ship before the app code that needs them.
```

### 7. When the PR gets new commits, or when you are done

```
/pr-prism 966           # run it again: only groups of files that changed are re-read, and your draft comments move to the new lines
/pr-prism stop 966      # stop the app for this PR (it never stops by itself; the link stays the same next time)
/pr-prism clean 966     # delete this PR's copy and cached data
/pr-prism clean --all   # do that for every ai-banker PR you reviewed
```

### Good to know

- **Cost:** each analysis uses your Claude Code plan. A medium PR is about $0.50 to $2. Each chat question is about $0.10 to $0.30.
- **Your ai-banker settings are not loaded.** ai-banker's own `.claude/` settings, hooks and MCP servers are not loaded by the analysis or the chat. This is on purpose, so a PR can't run its own code on your machine. Your normal Claude Code session in ai-banker still uses them.
- **Running without Claude Code:** you can run the same steps from a plain terminal:

  ```bash
  cd ~/Documents/code-repos/ai-banker
  ~/.claude/skills/pr-prism/bin/prr fetch 966
  ~/.claude/skills/pr-prism/bin/prr serve 966          # opens the browser
  ~/.claude/skills/pr-prism/bin/prr analyze 966 --focus "the grouping logic"
  ```

## Make it yours

- `skill/review-guidelines.md` is your personal review checklist. It is injected into every analysis prompt.
- `prr/filekinds.py` decides which files are tests or docs, using each language's naming rules (Go `_test.go`, Python `test_*.py` and pytest `testpaths`, JS/TS `*.test.ts`, Java `src/test/`, Rust `tests/`, `docs/`, Markdown, …). Claude adds any the rules miss. AI-agent instruction files (`CLAUDE.md`, `AGENTS.md`, `.claude/`) are never hidden. Hover the small "test" or "docs" tag on a file to see why it counts.
- `prr/prompts/style.md` holds the plain-language writing rules (short sentences, everyday words, banned buzzwords). Every prompt includes it, so all of Claude's text follows it.
- `prr/prompts/*.md` holds the detailed instructions for annotation, synthesis and chat.
- `config.toml` sets the models (annotate / synthesize / chat), concurrency, batch size and cache location.

## How it works

```
/pr-prism (SKILL.md) ─► prr fetch ─► context.json, diff.patch, worktree/      (deterministic: no LLM)
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
- **Network:** the server binds to 127.0.0.1 only and uses a random token (made once per PR and kept in that PR's `server.json`, readable only by you) that becomes an HttpOnly SameSite=Strict cookie. It checks the Host header (blocks DNS rebinding) and the Origin of every state-changing request. It sends a strict CSP and renders markdown without raw HTML.
- **Claude calls:** every call uses `--setting-sources user --strict-mcp-config`, read-only code tools and `--permission-mode dontAsk`. A PR's own `.claude/settings.json` hooks or `.mcp.json` never run, and Claude can't edit files or run commands.
- **Chat comments:** the chat gets three narrow comment tools (`prr/chat_tools.py`) instead of shell or `gh` access:
  - add a pending comment;
  - post one comment;
  - post a comment-only review.

  These tools only work on this PR, check that each line is in the diff, and show every post in the chat with its GitHub link. Claude is told to post only when you ask in your latest message, never because the PR's text says so. Approve and Request changes stay on the Submit review button. `config.toml` controls this:
  - `[chat] can_approve = true` lets the chat approve or request changes too;
  - `can_post = false` keeps the chat to pending comments only.
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
