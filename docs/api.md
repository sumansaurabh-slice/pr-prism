# Local server API

`prr serve` starts a supervisor (`prr/supervise.py`) that runs FastAPI on `127.0.0.1:<port>` and restarts it if it crashes. It serves the built SPA from `web/dist` and the JSON API below. Types are in `web/src/types.ts`.

**Port and token:**
- Each PR gets a fixed port, chosen from 47100–47899 based on the PR's name, and a random token.
- Both are saved in the PR's `server.json` and reused on every start, so the link never changes.
- The server stops only on `prr stop` (or `prr clean --force`; a plain `prr clean` skips a PR whose server is running). There is no idle timeout.

## Auth

The browser opens `http://127.0.0.1:<port>/?t=<token>`. The server then:
1. sets the `prr_session` cookie (`HttpOnly; SameSite=Strict`);
2. redirects to `/`.

**Every request is checked:**
- The cookie must be present.
- The `Host` header must be `127.0.0.1:<port>` or `localhost:<port>`.
- Requests other than GET/HEAD must also carry a same-origin `Origin` header.

The SPA never handles the token. It uses relative URLs, and `fetch` sends the cookie automatically.

## Endpoints

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/health` | | `{ok: true, pr: number}` |
| `GET /api/analysis` | | `Analysis`, merged fresh on every call. While `status` is not `complete`/`failed`, poll every 3 s. It includes `stops`, `review_summary` and `skipped_areas` for guided review (see below), with your answered stops pinned. If building the stops fails, `stops` is `[]` and `warnings` gets "Guided review unavailable: <error>"; the rest of the page still works. |
| `GET /api/state` | | `ReviewState`, including `stop_verdicts`, `seen_hunks` and `review_body_draft` (see below). After a restore (see `prr clean`) it has `restored: {archived_at, restored_at, archive, chats, pending_comments, need_replacing, viewed_marks, message}` until dismissed; `message` is the banner text from `prr/archive.py` `message()`, e.g. "Restored your work from Oct 23: 3 chats, 5 pending comments (2 need re-placing), 12 viewed marks." |
| `GET /api/cost` | | `CostSummary`: `{total_usd, unknown, running, breakdown: {analysis, chat, other}, runs, threads: {<thread id>: {total_usd, turns, unknown, long}}, warn_usd, over_budget, long_thread: {turns, usd}}`, all computed from the PR's `calls.jsonl`. The app shows "$12.40 spent" ("≥" when `unknown`, "so far" while `running`) and refreshes it on each analysis poll and after each chat answer. |
| `GET /api/diff?path=` | | `{path, patch}`: the unified diff for one file, with `diff --git` header lines, ready for `parseDiff` |
| `GET /api/file?path=&at=head\|base` | | `{path, at, content}`: full file text at the PR head or merge-base |
| `PUT /api/reviewed` | `{path, reviewed: bool}` | `{reviewed: Record<path, blob>}` |
| `POST /api/drafts` | `{path, side, line, start_line?, body, source, suggestion_id?, origin_id?}` | `Draft`. Comments outside a hunk are accepted with `anchored: false`. `origin_id` is the guided-review stop the draft came from: "Real problem" sets it, and a look_for stop's comment editor passes it when it saves. It must be a stop id (400 otherwise). A `suggestion` or `chat` draft also keeps `generated_body`, the text PR Prism wrote (for a suggestion, the `comment_body` of the finding `suggestion_id` names, even when `body` was edited before adding; otherwise `body`), so its eval label can say who wrote the posted comment. |
| `PATCH /api/drafts/{id}` | `{body}` | `Draft`. 409 `{detail}` while a post is sending this draft ("…being posted…"), or while the draft is `outdated` (read-only until re-placed) |
| `DELETE /api/drafts/{id}` | | `{ok: true}`, or 409 `{detail}` ("…being posted…") while a post is sending this draft. Deleting a draft with an `origin_id` takes back that "Real problem" answer in `labels.jsonl`; if that write fails the draft is still deleted and the reply adds `warning: "Couldn't save this answer for eval"` (shown as a toast). |
| `POST /api/drafts/{id}/replace` | `{side, line, start_line?, start_side?}` | `Draft` on the new line, with `outdated: false` and `commit_id` = the analyzed head. Checked like `POST /api/drafts` (400 with `{detail}`); 404 for an unknown id; 409 while it is being posted. |
| `POST /api/restored/dismiss` | | `{ok: true}`: removes `restored` (the one-time "Restored your work" summary) from the state |
| `POST /api/stops/{id}/verdict` | `{verdict: "real"\|"not"\|"unsure", opened_at?}` | `{verdict: StopVerdict, draft: Draft\|null, dismissed: string\|null, review_body_draft: string\|null, warning?}`: your answer to a guided-review stop and what it did (see [Guided review](#guided-review)). 400 for a bad id or verdict, 404 for an id that was never a stop here, 409 "This stop changed; reload" for a stop that is gone. |
| `DELETE /api/stops/{id}/verdict` | | `{ok, kept_draft: Draft\|null, restored: string\|null, review_body_draft: string\|null, warning?}`: Undo, or the first half of "Change answer". 409 "…being posted…" while its comment is being posted. |
| `PUT /api/review-body-draft` | `{body}` | `{ok: true}`: saves the Submit dialog's review summary (autosaved as you type), which `GET /api/state` returns as `review_body_draft`. At most 65,536 characters (400 otherwise). Submitting a review doesn't clear it: the app saves `""` after a successful post. |
| `POST /api/seen` | `{path, hunks: string[]}` | `{ok: true}`: hunks of `path` that were on screen for 3 s (hunk ids below). Adds to `state.seen_hunks[path]`, keeping the newest 5,000. 404 for a file outside the PR; 400 for a bad hunk id or more than 5,000 ids. |
| `GET /api/coverage` | | `Coverage`: what you haven't checked yet (see [Coverage before submit](#coverage-before-submit)). |
| `POST /api/suggestions/{id}/dismiss` | | `{dismissed_suggestions}` |
| `POST /api/suggestions/{id}/restore` | | `{dismissed_suggestions}` |
| `GET /api/github/threads` | | `{comments: ExistingComment[]}`: review comments already on the PR (cached for 60 s); `review_id` is the GitHub review each belongs to |
| `GET /api/since-review` | | `SinceReview`: what changed since your last submitted review (see [Since your review](#since-your-review)) |
| `GET /api/head` | | `HeadStatus`: `{analyzed_head, current_head, head_changed, running, estimate: {minutes, usd} \| null, error}`. GitHub's head is read at most once a minute (Submit's own check refreshes it). `head_changed` shows "New commits since you opened this"; `estimate` is the last complete run's time and cost for the "Re-read the PR" button; `error` (with `current_head: null`) when GitHub couldn't be reached. |
| `POST /api/reanalyze` | | `{started: true, estimate: {minutes, usd} \| null}`: "Re-read the PR". Starts `prr _reanalyze` in the background: `prr fetch`, then `prr analyze` with the last run's focus (output in the PR folder's `analyze.log`). The page keeps its view; the progress banner follows the run through `GET /api/analysis`. 409 `{detail: "The PR is already being read. Wait for it to finish."}` while any analysis of this PR runs. |
| `POST /api/ci/refresh` | | `CIInfo`: reads the CI checks again and explains new failures; the next `/api/analysis` includes them |
| `POST /api/review/preview` | `{body, event}` | `ReviewPreview`, built from the drafts Submit would send now (not outdated, not being posted) |
| `POST /api/review/draft-summary` | | `{body}`: Claude drafts the overall review body from the drafts and the analysis |
| `POST /api/review/submit` | `{body, event, commit: "analyzed"\|"current"}` | `{ok, review_id, html_url}`, or 4xx `{detail}`. Only the drafts this post sent are removed; outdated drafts aren't sent and stay. 409 "Nothing new to post: …" when every draft is already being posted by another post (the chat's). Each posted comment is saved as a problem label for `prr eval`, with `written_by`: `human` for a `user` draft, and for a `suggestion` or `chat` draft `tool` unless more than 30% of the posted words are the reviewer's (compared with `generated_body`), then `human` (see `prr/labels.py`); if that write fails the review still counts and the reply adds `warning: "Couldn't save this answer for eval"` (shown as a toast). The state then has `reviewed_head` (the analyzed head), `reviewed_base` and `reviewed_stop_ids`, for `GET /api/since-review`. |
| `GET /api/chat/threads` | | `ChatThread[]` |
| `POST /api/chat/threads` | `{title?}` | `ChatThread` |
| `DELETE /api/chat/threads/{id}` | | `{ok: true}` |
| `POST /api/chat/threads/{id}/messages` | `{text, context: ContextChip[]}` | `text/event-stream` (see below) |
| `POST /api/chat/threads/{id}/stop` | | `{ok: true}` |
| `POST /api/chat/threads/{id}/long-hint/dismiss` | | `{ok: true}`: "Keep going" on the long-thread hint. Sets `long_hint_dismissed: true` on the thread, so the hint never shows for it again. 404 for an unknown thread. |
| `POST /api/chat/approvals/{id}` | `{decision: "allow"\|"always"\|"deny"}` | `{ok: true}`: the answer to an approval card. `always` on a card that can't be remembered (`always: false`, e.g. a guard card) counts as `allow`. 404 when the card no longer waits (it timed out, or another card's "Always allow" covered it). |
| `GET /api/trust` | | `{author, association, trusted, state: "trusted"\|"outside"\|"unknown"}`: is the PR's author an owner, member or collaborator? `unknown` means the check failed (treated as outside). On `outside` the chat shows "Outside author: @user isn't a member or collaborator of this repo. Jira, Slack and other-repo lookups ask first." and the header an "outside author" label; on `unknown`, "Couldn't check the author; lookups ask first." |

### Since your review

When you submit a review (Submit, or the chat's `post_review`; a single chat `post_comment` doesn't count), the server saves in `state.json`:
- `reviewed_head`: the analyzed head you reviewed, also kept as `refs/pr-prism/<n>/reviewed` in your repo (fetched by sha if needed; `prr clean` keeps it);
- `reviewed_base`: its merge base;
- `reviewed_stop_ids`: the guided-review stops present then.

`GET /api/since-review` returns `SinceReview`:

| Field | Meaning |
|---|---|
| `reviewed_head`, `head` | the commit you reviewed (null: no review yet) and the analyzed head |
| `files` | `[{path, hunks: [{old_start, old_lines, new_start, new_lines}]}]`: `git diff <reviewed_head> <head> -- <the PR's current files>`. Empty with no reviewed head or the same head: the app hides its toggle. |
| `patch` | `{path: unified diff}`, one entry per file in `files`, in the same form as `GET /api/diff`'s `patch` |
| `note` | "main moved; some changes may come from main" (named after the PR's base branch) when the merge base moved; "The commit you reviewed (abc1234) can't be found, likely after a force-push, so this shows the whole PR." when it is gone from your repo and can't be fetched by sha; "Couldn't compute changes since your review: <reason>" when git fails |
| `ok` | false when git failed: no files; the app shows the note and disables the toggle |
| `full_pr` | true when the reviewed commit is gone: `files` and `patch` are the whole PR |
| `comments` | your earlier comments: `[{id, path, line, status, note}]`, `id` being the GitHub comment id and `line` null when outdated |
| `comments_error` | why the comments couldn't be read from GitHub, else null |
| `new_stop_ids` | guided-review stops whose lines meet an interdiff hunk (same path, new-side lines, with git's 3 lines of context). Ids don't count: a re-run can re-anchor a stop without a code change. |

A comment's `status`:
- `fixed` / `still-open`: the re-check on the new head (outdated comments, and comments whose lines the new commits touched); `note` says why;
- `line-unchanged`: the new commits didn't touch its lines;
- `checking`: an analysis or re-read is running ("Checking…");
- `not-checked`: over `[analysis] recheck_max` (note "N not checked (limit; raise `recheck_max`)"), or not re-checked on this commit yet (note "Not checked on this commit yet. Re-read the PR to check it.").

The re-check runs in the analysis of the new head, your own comments first, at most `recheck_max` (default 20) per run.

### Review events

`event` takes one of these values:

| Value | Effect |
|---|---|
| `COMMENT` | Submits the review as comments only |
| `APPROVE` | Submits an approving review |
| `REQUEST_CHANGES` | Submits a review that requests changes |
| `PENDING` | Saves the review on GitHub as a pending (unsubmitted) review, which you finish on github.com |

If you are the PR author, `APPROVE` and `REQUEST_CHANGES` are rejected.

### Chat stream

Each SSE frame looks like `data: <json>\n\n`, where the JSON is one `ChatEvent`:

| Event | Payload |
|---|---|
| `text` | `{text}`: append this chunk to the assistant message |
| `thinking` | none; the model is thinking |
| `tool` | `{name, summary}`, e.g. `{name: "Read", summary: "pkg/cmd/pr/view.go"}` |
| `result` | `{session_id, duration_ms, cost_usd, cost_unknown, includes_stopped, session_total_usd, thread: {total_usd, turns, unknown, long}}`. `cost_usd` is this answer's own cost (null when unknown). `includes_stopped` means an earlier answer's cost is counted in this one (that answer stopped, or its result had no cost). `thread.long` (15+ answers or $3+) is when the app offers a fresh thread, after the answer ends. |
| `error` | `{message}` |
| `approval` | `{id, tool, title, summary, detail, reason: {kind, text, detail?}, causes: [{kind, text}], guard, body, always, always_text, always_title, always_label}`: Claude waits for your answer (see below) |
| `approval_done` | `{id, decision: "allow"\|"always"\|"deny", why: "answered"\|"covered"\|"timeout"\|"closed"}`: the card is settled. `covered`: another card's "Always allow" covered it ("Allowed by your earlier choice"); `timeout`: nobody answered in time ("Timed out · Claude was told no") |
| `done` | none; the stream closes after this |

Each thread allows one turn in flight; a second request gets 409. The server persists the user message and the final assistant message to `state.json`. An assistant message whose turn showed cards also has `approvals: [{id, tool, title, summary, decision, why}]`, one per card in arrival order; the app shows each as one line under the answer ("Allowed: Read ~/code/billing/config.py", "Denied: …", "Timed out: …"). A card still waiting when the turn ended is saved as `decision: "deny", why: "closed"` ("Not answered: …").

#### Approval cards

The card reads top to bottom:
1. **the action** in plain words, from `tool`, `title` and `summary` ("Read ~/code/billing/config.py", "Post a comment on src/a.py:42");
2. **why it asks**: `reason.text` as an attention label, then `reason.detail`. `reason.kind` is `outside_read`, `outside_author`, `marked_turn` (a guard card), `env` or `action`. Guard cards also list `causes`: "Earlier this turn Claude read: …" (`lookup`, `outside_read`) and "Earlier this turn you approved: …" (`approved`);
3. **the body**: `body` (a post's Markdown, only on post cards) rendered as Markdown, a fetch's URL or a shell command in mono, or `detail` (the raw input) behind "Show input";
4. **the buttons**: Deny, then `always_text` (only when `always`; its tooltip is `always_title`), then Allow ("Post" on post cards). Guard cards (`guard: true`, posting or sending out after outside data) offer only Deny and Post / "Allow once".

Each button's accessible name includes the action ("Allow: Read ~/code/billing/config.py"). No key approves a card, and global shortcuts ignore keys pressed on one. A new card is read out by an `aria-live="polite"` region and never takes focus from the message box.

## Guided review

`Analysis.stops` is a short list of places that need a human, each with one question, in reading order (chapter, then file, then line; failing CI checks first). Built by `prr/stops.py` `build_stops`.

**`Stop`:** `{id, kind, path, side, line, start_line, question, scenario, claude_view, confidence, judge_score, second_look, severity, category, finding_id, bot_comment_id, check_name, comment_body, chapter, order, more, low_confidence, sources, changed_lines, dropped}`.
- `id` is `stop-<40 hex>`. It stays the same across re-runs: it hashes the kind, path, side, anchor text and a source key (a finding's category and anchor, never its title; a note's text; a look_for item's text; the check name; the other tool's comment id).
- `kind` and its question:

  | `kind` | From | `question` | "Real problem" (`real`) does |
  |---|---|---|---|
  | `finding` | a finding (shown, or waiting for its second look) | its deep-check `question`, else "Is this a real problem? <title>" | makes a pending comment from `comment_body` (the finding's), or reuses the one made before (`origin_id` = stop id) or the finding's own "Add to my review" comment |
  | `risk_note` | a risk note in a high- or medium-risk file | "Is this safe? <note>" | makes a pending comment at the note's lines, pre-filled with the note |
  | `look_for` | a "things to check" item of a high-risk file with no other stop | the item | nothing yet: the app opens a comment editor at `line` (the file's first changed line) and saves with `origin_id`; a cancelled editor answers `unsure` |
  | `ci` | a failing check this PR caused or may have caused, or that Claude couldn't explain | "Is this failure caused by this PR? `<name>` fails." | adds `comment_body` (the failure and Claude's fix) to `review_body_draft`, once |
  | `bot` | another review tool's finding Claude agrees with | "Do you agree with <bot>: <title>?" | makes a pending comment at its line, pre-filled with a one-line quote |
- `claude_view` is what Claude thinks (Markdown), shown muted after the code. Notes or other tools' comments within 3 lines of a stop (same file and side) are folded into it: a finding takes in a note or another tool's comment, another tool's comment takes in a note, and two notes become one. Two findings never merge, and an answered stop is never folded in.
- At most `[review] max_stops` (12) stops are listed, by priority: blocker and major findings; deep-check findings; CI; other tools' findings; minor findings; risk notes in high-risk files; risk notes in medium-risk files; look_for items; nit findings. The rest have `more: true` ("N more places"). A stop you answered stays listed whatever the cap.
- `low_confidence: true`: a finding the second look hides. It is outside the cap and the counts; show it only with the Review view's "Include low-confidence findings" switch.
- `dropped: true`: a stop you answered that is gone, or that the second look now hides ("earlier answer", "Second look dropped this"). It is rebuilt from the answer's `anchor`; answering it again gets 409, Undo works.
- `changed_lines` is the number of added and deleted lines in the stop's hunk (a look_for item: the whole file's).

**`review_summary`:** `{places, more_places, quiet_files, minutes_left}` for the plan box ("4 places need a human · 61 files have nothing flagged · ~25 min").
- `places`: listed stops (`more`, `low_confidence` and `dropped` all false).
- `more_places`: stops with `more`.
- `quiet_files`: files with no stop at all (a file whose stops are all past the cap is not quiet).
- `minutes_left`: the sum, over listed stops not answered yet, of 2 + min(5, floor(`changed_lines` / 20)).

A stop whose finding was dismissed elsewhere counts as done. `skipped_areas: [{title, reason}]` lists the deep-check areas this run didn't check (failed, stopped, skipped for time, or a high-risk area over the area limit); "Nothing risky found" should show only when the analysis is complete and it is empty.

**Answers** (`state.stop_verdicts[stopId]`): `{verdict, at, head_sha, anchor, opened_at, answered_at}`. `anchor` is where the stop was: `{kind, path, side, line, start_line, question, finding_id, bot_comment_id, check_name}`.
- `POST /api/stops/{id}/verdict`:
  - `real` does what the table says. It is idempotent: a second `real` returns the same draft and adds nothing. A comment made from a text with a link outside `github.com/<owner>/<repo>` has `outside_link: true`.
  - `not` also dismisses the finding (`dismissed` = its id).
  - `unsure` only records.
  - Answering again with the same verdict changes nothing. A different verdict replaces the answer but doesn't undo the old one's effects: for "Change answer", call `DELETE` first.
  - Each answer is an eval label (`labels.jsonl`, keyed by the stop id): `real` → problem, `not` → not-problem; `unsure` makes none and retracts an earlier `real`/`not`. On a finding or risk-note stop the label is `written_by: "tool"` (PR Prism raised it), on other stops `"unknown"`. If that write fails the answer is still saved and the reply adds `warning: "Couldn't save this answer for eval"`.
- `DELETE /api/stops/{id}/verdict` removes the answer and what it did:
  - after `not`: the finding is restored (`restored` = its id);
  - after `real` on a CI stop: its text is taken out of `review_body_draft` (if still there unchanged), and the new text is returned;
  - after `real` on other stops: the comment is deleted if its body is unchanged from `comment_body`; an edited one is kept and returned as `kept_draft`, so the app can ask "Keep your comment?" (delete it with `DELETE /api/drafts/{id}`);
  - a `retracted` label is appended for an earlier `real`/`not`.
  - An id with no answer returns `{ok: true, …null}`.

## Coverage before submit

`GET /api/coverage` returns `prr/coverage.py` `coverage(analysis, state)`:

```
{not_checked: [{stop_id, path, line, question, kind}], unsure: [{stop_id, path, line, question}],
 skipped_areas: [{title, reason}], high_risk_unseen: {hunks, files, by_file: {[path]: {seen, total}}},
 low_confidence, running, count}
```

- **Not checked** (one definition for the Overview, the Review progress and the Submit warning): stops with no answer, including "more places", and not dismissed elsewhere; listed stops first, then "more", each in reading order. Any answer counts as checked; `unsure` also lists the "Not sure" ones.
- `count` = `not_checked.length + skipped_areas.length`: the warning number ("2 places not checked yet").
- `high_risk_unseen`: hunks of high-risk files that were never on screen for 3 s and hold no answered stop ("High-risk code not seen: 12 hunks in 3 files"). `by_file` covers every high-risk file with hunks and feeds "2 of 5 risky hunks seen" on file rows. Not part of `count`.
- `low_confidence`: unanswered findings the second look hides ("Low confidence, not checked: N"). Not part of `count`.
- `running`: the analysis is still running ("Analysis still running; this list may grow").

**Hunk ids** (`POST /api/seen`, `state.seen_hunks`): computed by the app from each hunk's header `@@ -a,b +c,d @@`:
- `"RIGHT:<c>-<c+d-1>"` for a hunk with new lines (the new line range);
- `"LEFT:<a>-<a+b-1>"` for a hunk that only deletes lines (`d` = 0).

`hunkId()` in `web/src/api.ts` and `hunk_id()` in `prr/coverage.py` implement the same rule. Ids from an older head stay in `seen_hunks` until they age out (newest 5,000 per file).
