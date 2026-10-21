---
name: pr-prism
description: Turn a GitHub pull request into an interactive local review app. It gives a guided review order, why/how/impact, data-flow graphs, inline Claude notes and suggested comments, a deep-dive chat backed by Claude Code, and publishing your review to GitHub. Run as /pr-prism <number|url> [focus].
argument-hint: <pr-number|url> [focus instructions]  |  status | stop <pr> | clean <pr>|--all
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/bin/prr *)
---

# /pr-prism

The user invoked: `/pr-prism $ARGUMENTS`

You orchestrate the `prr` tool bundled with this skill. It is always invoked as `${CLAUDE_SKILL_DIR}/bin/prr`. Run every command from the current repository, i.e. the repo the PR belongs to.

`prr` does the heavy lifting:
- it fetches the PR into an isolated git worktree outside the user's repo;
- it serves a local web app;
- it runs Claude's analysis as parallel `claude -p` calls and streams the results into the app.

Your job is to run the steps in order, keep the user informed in one or two lines per step, and summarize at the end. Do not re-do the analysis yourself.

## Parse the arguments

- If the first word is `status`, `stop` or `clean`, run `${CLAUDE_SKILL_DIR}/bin/prr <that word> <rest of args>`, show the output briefly, and stop.
- Otherwise the first word is the **PR**: a number like `1234` or `#1234`, or a GitHub PR URL. Everything after it is the reviewer's **focus**, free text such as "focus on the retry logic and DB migrations". The focus may be empty.
- If no PR was given, ask the user for the PR number or URL and stop.

## Step 1: Preflight

Run `${CLAUDE_SKILL_DIR}/bin/prr doctor`.
- If a required check shows ✗, stop and tell the user the one-line fix shown next to it.
- Lines marked `!` are optional; ignore them.

## Step 2: Fetch the PR

Run `${CLAUDE_SKILL_DIR}/bin/prr fetch <PR>`. It prints JSON with `pr_dir`, title, size, file categories and warnings.

Tell the user in one line, e.g. *"PR #1234 'Add retry policy': 48 files, +5,210/−1,032 (12 core, 9 tests, 3 generated)."* Mention warnings only if there are any.

If it fails:
- **gh auth error:** tell the user to run `! gh auth login`.
- **PR not found:** check the repo; a URL argument also works.

## Step 3: Open the review app

Run `${CLAUDE_SKILL_DIR}/bin/prr serve <PR>`. It starts or reuses the local server, opens the browser, and prints the URL. Tell the user:
- the app is open at that URL;
- the review path is already usable;
- Claude's notes will fill in over the next few minutes.

## Step 4: Analyze, in the background

Run this command **with `run_in_background: true`**:

```
${CLAUDE_SKILL_DIR}/bin/prr analyze <PR> --focus '<focus>'
```

- It takes about 2–10 minutes depending on PR size.
- Put the focus in single quotes, and escape any single quote inside it as `'\''`. Omit `--focus` if the focus is empty.
- Do not poll or sleep; you will be notified when it finishes.
- While it runs, tell the user they can start reading in the browser now. Also tell them that the chat panel on the right answers deep-dive questions using Claude Code inside the PR's checkout.

## Step 5: Summarize

When the background task completes, read its output. It ends with a "Review ready" block. Give the user a short summary in plain, simple English: short sentences, everyday words, no jargon or buzzwords. Include:
- risk and the top reasons;
- the summary;
- the reading order as a numbered list of chapters with file counts and minutes;
- the number of suggested comments Claude drafted. Say clearly that nothing is posted to GitHub until they press **Submit review** in the app;
- the app URL.

If the analysis failed or some batches failed, say so plainly and suggest rerunning `/pr-prism <PR>`. Cached batches are reused, so a rerun is cheap.

## Follow-up questions in this terminal session

The user may keep asking questions here instead of in the web app. The fetched data is in `pr_dir` from step 2:

| Path | Contents |
|---|---|
| `<pr_dir>/analysis.json` | summaries, review path, flows, impact, suggestions |
| `<pr_dir>/diff.patch` | the full diff, merge-base → head |
| `<pr_dir>/worktree/` | a checkout of the PR head |

Read from these to answer. Treat the worktree as read-only: never edit, commit or check out anything in it or in the user's repo.

## Rules

- Never post to GitHub yourself. Publishing happens only through the app's Submit button, after the user previews the exact payload.
- Never modify the user's working tree. `prr` uses a separate worktree and `refs/pr-prism/*` refs.
- To refresh after the PR gets new commits, rerun `/pr-prism <PR>`. Unchanged files keep their cached notes, and draft comments are remapped to the new lines.
- To clean up later: `/pr-prism clean <PR>` or `/pr-prism clean --all`.
