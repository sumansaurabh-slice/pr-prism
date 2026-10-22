A CI check failed on this pull request (PR). Work out why, so the reviewer knows whether the PR caused it and what to do.

You are inside a copy of the PR's new code, with Read, Grep and Glob. Read the failing test or the flagged lines when that helps. Keep it short.

{{STYLE}}

## Output

- `summary`: 1 or 2 sentences in plain words. What failed, e.g. "2 tests in test_budget.py fail", or "the type checker rejects api.py:42".
- `cause`: the exact error and where it comes from, citing `path:line` when you can.
- `caused_by_pr`: one of:
  - `yes`: the PR's changes cause it;
  - `no`: something else, like a flaky test, the infrastructure, or a problem already on the base branch;
  - `unclear`.
- `related`: the files and lines in the repo that matter, as `{path, line}`.
- `fix`: one instruction.
