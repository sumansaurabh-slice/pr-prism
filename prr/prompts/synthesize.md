You are writing a short guide that helps a colleague review a large pull request (PR). Notes for each file already exist; a digest of them is below. Your job is the big picture:
- **why** the change was made;
- **how** it works;
- the **order** to read the files in;
- **how data moves** through the code;
- **what else could break**.

The guide is shown in a review app. Keep it short and easy to scan.

You are inside a copy of the PR's new code. You may use Read, Grep and Glob to confirm a flow or a caller when the digest is not enough. Keep it to a few reads.

{{STYLE}}

## What to produce

### 1. summary
- **tldr**: 2 or 3 short sentences in plain words. What does this PR do, and who is it for? What is the main piece of code that changed?
- **why**: why the change was made. Use the PR description, commit messages and the code. What problem was there before? If the description does not say, make a careful guess and start that sentence with "Likely:".
- **how**: a short numbered list (3 to 6 steps) of how the change works. One idea per step. Name the main functions in backticks and add `path:line` where it helps.
- **risk**: `low`, `medium` or `high` for the whole PR.
- **risk_reasons**: 1 to 5 short reasons. Each says what could go wrong and where.

### 2. chapters: the reading order
This is the most important part. Put EVERY changed file (except generated, vendored or lock files) into 3 to 8 chapters. Order them so each chapter only needs the chapters before it:
- Usual order: data formats, schema and types first; then the main logic; then the code that calls it (handlers, commands, entry points); then settings, build and deploy; then tests; then docs.
- Change this order if the PR is easier to understand another way. For example, start with the one file that explains everything.
- Put a test file right after the code it tests when reading it helps explain the behavior. Otherwise, put tests in a later chapter.
- Inside a chapter, put files before the files that use them.

Each chapter has:
- an `id` (kebab-case);
- a `title`: a short, plain name for the topic, e.g. "How retries work". Not "Chapter 2".
- a `goal`: one short sentence on what the reader will understand after this chapter.
- `files`: each with a `why_now` line, i.e. one short sentence on why to read it at this point. Example: "Defines `RetryPolicy`, which every later file uses."

Use file paths exactly as given. You may leave out generated, vendored and lock files; the app puts them in a "Skim" group.

### 3. flows: how data moves
Give 1 to 3 flows. Each flow is a small diagram (4 to 12 boxes) of a main path the PR adds or changes, e.g. "HTTP request → check input → service → save to DB → send event". Each flow has:
- a `title`;
- `nodes`, each with:
  - `id`: short, unique within the flow;
  - `label`: the function or part name;
  - `kind`, one of:
    - `entry`: where it starts, e.g. an HTTP handler, CLI command, queue consumer or scheduled job;
    - `logic`;
    - `store`: a database, cache, file or queue;
    - `external`: another service or a third-party API;
    - `test`;
  - `path` and `line` when the node maps to code.
- `edges`: `source` → `target` node ids, with a short `label` naming what is passed.
- `narrative`: 3 to 6 short sentences. Walk through the flow step by step and say where it can fail.

### 4. impact: what this PR can affect outside its own lines
Give a list of items. Each item has a `kind`:
- `breaking`: a public API, data format, CLI flag, setting name or database schema changed. Say who is affected and whether old users still work.
- `callers`: important code outside the PR that uses what changed, taken from "Code outside this PR that uses the changed names" below. Say what may need an update or a re-test.
- `migration`: database or data changes. Cover order, locking, undo, and which goes first in deploy.
- `config`: new or changed settings, flags or environment variables. Cover defaults and rollout.
- `perf`: slow paths, repeated database calls, loops or memory use with no limit.
- `security`: login and permissions, input checks, secrets, injection.
- `tests`: important behavior with no tests, or tests that may be flaky.
- `other`.

Each item also has:
- a `severity`;
- a `title`: one short line;
- a `detail`: 1 to 3 short sentences. Say what could happen and what to check.
- `refs`: `path` + `line`, with `at` = `head`, or `base` for deleted code.

Only include items that really matter; usually 3 to 10.

### 5. questions
Give 2 to 6 short questions the reviewer should ask the PR author: things the code cannot answer, rollout or compatibility worries, or missing context. Add `refs` where they help.

### 6. test_files and docs_files
The app has "Hide test files" and "Hide docs files" checkboxes. List the changed files for each. Use exact paths from the file list.
- **test_files:** files whose main job is testing:
  - tests and test helpers;
  - fixtures, mocks and sample data;
  - eval or benchmark harnesses;
  - test runner config.

  Use the repo's own conventions, not only file names. Example: a `support/` folder that only tests import counts.
- **docs_files:** files that are documentation:
  - READMEs, guides, design docs and plans;
  - changelogs;
  - doc-site pages.

  Code samples that live inside a docs folder count as docs.
- A file goes in at most one list. Code that runs in production goes in neither, even if it is test-related, like a feature flag for tests.
- The file list above already marks files that the language rules recognise as `test file` or `docs file`. Include those too, unless they are clearly wrong.

## Writing rules for this guide
- Point to code as inline code: `path/to/file.ext:123` or `path:120-140`. The app turns these into links.
- Be specific. Name the functions, fields, flags and files. No filler like "This PR improves the codebase".
- Never invent behavior you cannot see. Mark guesses with "Likely:".

## Reviewer's focus

{{FOCUS}}

## Reviewer's own review checklist

{{GUIDELINES}}
