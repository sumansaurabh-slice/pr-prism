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
| `GET /api/analysis` | | `Analysis`, merged fresh on every call. While `status` is not `complete`/`failed`, poll every 3 s. |
| `GET /api/state` | | `ReviewState`. After a restore (see `prr clean`) it has `restored: {archived_at, restored_at, archive, chats, pending_comments, need_replacing, viewed_marks, message}` until dismissed; `message` is the banner text from `prr/archive.py` `message()`, e.g. "Restored your work from Oct 23: 3 chats, 5 pending comments (2 need re-placing), 12 viewed marks." |
| `GET /api/cost` | | `CostSummary`: `{total_usd, unknown, running, breakdown: {analysis, chat, other}, runs, threads: {<thread id>: {total_usd, turns, unknown, long}}, warn_usd, over_budget, long_thread: {turns, usd}}`, all computed from the PR's `calls.jsonl`. The app shows "$12.40 spent" ("≥" when `unknown`, "so far" while `running`) and refreshes it on each analysis poll and after each chat answer. |
| `GET /api/diff?path=` | | `{path, patch}`: the unified diff for one file, with `diff --git` header lines, ready for `parseDiff` |
| `GET /api/file?path=&at=head\|base` | | `{path, at, content}`: full file text at the PR head or merge-base |
| `PUT /api/reviewed` | `{path, reviewed: bool}` | `{reviewed: Record<path, blob>}` |
| `POST /api/drafts` | `{path, side, line, start_line?, body, source, suggestion_id?}` | `Draft`. Comments outside a hunk are accepted with `anchored: false`. `origin_id` is the guided-review stop the draft came from (null until guided review sets it). |
| `PATCH /api/drafts/{id}` | `{body}` | `Draft`. 409 `{detail}` while a post is sending this draft ("…being posted…"), or while the draft is `outdated` (read-only until re-placed) |
| `DELETE /api/drafts/{id}` | | `{ok: true}`, or 409 `{detail}` ("…being posted…") while a post is sending this draft. Deleting a draft with an `origin_id` takes back that "Real problem" answer in `labels.jsonl`; if that write fails the draft is still deleted and the reply adds `warning: "Couldn't save this answer for eval"` (shown as a toast). |
| `POST /api/drafts/{id}/replace` | `{side, line, start_line?, start_side?}` | `Draft` on the new line, with `outdated: false` and `commit_id` = the analyzed head. Checked like `POST /api/drafts` (400 with `{detail}`); 404 for an unknown id; 409 while it is being posted. |
| `POST /api/restored/dismiss` | | `{ok: true}`: removes `restored` (the one-time "Restored your work" summary) from the state |
| `POST /api/suggestions/{id}/dismiss` | | `{dismissed_suggestions}` |
| `POST /api/suggestions/{id}/restore` | | `{dismissed_suggestions}` |
| `GET /api/github/threads` | | `{comments: ExistingComment[]}`: review comments already on the PR |
| `POST /api/ci/refresh` | | `CIInfo`: reads the CI checks again and explains new failures; the next `/api/analysis` includes them |
| `POST /api/review/preview` | `{body, event}` | `ReviewPreview`, built from the drafts Submit would send now (not outdated, not being posted) |
| `POST /api/review/draft-summary` | | `{body}`: Claude drafts the overall review body from the drafts and the analysis |
| `POST /api/review/submit` | `{body, event, commit: "analyzed"\|"current"}` | `{ok, review_id, html_url}`, or 4xx `{detail}`. Only the drafts this post sent are removed; outdated drafts aren't sent and stay. 409 "Nothing new to post: …" when every draft is already being posted by another post (the chat's). Each posted comment is saved as a problem label for `prr eval`; if that write fails the review still counts and the reply adds `warning: "Couldn't save this answer for eval"` (shown as a toast). |
| `GET /api/chat/threads` | | `ChatThread[]` |
| `POST /api/chat/threads` | `{title?}` | `ChatThread` |
| `DELETE /api/chat/threads/{id}` | | `{ok: true}` |
| `POST /api/chat/threads/{id}/messages` | `{text, context: ContextChip[]}` | `text/event-stream` (see below) |
| `POST /api/chat/threads/{id}/stop` | | `{ok: true}` |
| `POST /api/chat/threads/{id}/long-hint/dismiss` | | `{ok: true}`: "Keep going" on the long-thread hint. Sets `long_hint_dismissed: true` on the thread, so the hint never shows for it again. 404 for an unknown thread. |
| `POST /api/chat/approvals/{id}` | `{decision: "allow"\|"always"\|"deny"}` | `{ok: true}`: the answer to an approval card. `always` on a card that can't be remembered (`always: false`, e.g. a guard card) counts as `allow`. 404 when the card no longer waits (it timed out, or another card's "Always allow" covered it). |
| `GET /api/trust` | | `{author, association, trusted, state: "trusted"\|"outside"\|"unknown"}`: is the PR's author an owner, member or collaborator? `unknown` means the check failed (treated as outside). On `outside` the chat shows "Outside author: @user isn't a member or collaborator of this repo. Jira, Slack and other-repo lookups ask first." and the header an "outside author" label; on `unknown`, "Couldn't check the author; lookups ask first." |

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
