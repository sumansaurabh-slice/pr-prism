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
| `GET /api/state` | | `ReviewState` |
| `GET /api/diff?path=` | | `{path, patch}`: the unified diff for one file, with `diff --git` header lines, ready for `parseDiff` |
| `GET /api/file?path=&at=head\|base` | | `{path, at, content}`: full file text at the PR head or merge-base |
| `PUT /api/reviewed` | `{path, reviewed: bool}` | `{reviewed: Record<path, blob>}` |
| `POST /api/drafts` | `{path, side, line, start_line?, body, source, suggestion_id?}` | `Draft`. Comments outside a hunk are accepted with `anchored: false`. |
| `PATCH /api/drafts/{id}` | `{body}` | `Draft`. 409 `{detail}` while a post is sending this draft ("…being posted…"), or while the draft is `outdated` (read-only until re-placed) |
| `DELETE /api/drafts/{id}` | | `{ok: true}`, or 409 `{detail}` ("…being posted…") while a post is sending this draft |
| `POST /api/drafts/{id}/replace` | `{side, line, start_line?, start_side?}` | `Draft` on the new line, with `outdated: false` and `commit_id` = the analyzed head. Checked like `POST /api/drafts` (400 with `{detail}`); 404 for an unknown id; 409 while it is being posted. |
| `POST /api/restored/dismiss` | | `{ok: true}`: removes `restored` (the one-time "Restored your work" summary) from the state |
| `POST /api/suggestions/{id}/dismiss` | | `{dismissed_suggestions}` |
| `POST /api/suggestions/{id}/restore` | | `{dismissed_suggestions}` |
| `GET /api/github/threads` | | `{comments: ExistingComment[]}`: review comments already on the PR |
| `POST /api/ci/refresh` | | `CIInfo`: reads the CI checks again and explains new failures; the next `/api/analysis` includes them |
| `POST /api/review/preview` | `{body, event}` | `ReviewPreview`, built from the drafts Submit would send now (not outdated, not being posted) |
| `POST /api/review/draft-summary` | | `{body}`: Claude drafts the overall review body from the drafts and the analysis |
| `POST /api/review/submit` | `{body, event, commit: "analyzed"\|"current"}` | `{ok, review_id, html_url}`, or 4xx `{detail}`. Only the drafts this post sent are removed; outdated drafts aren't sent and stay. 409 "Nothing new to post: …" when every draft is already being posted by another post (the chat's). |
| `GET /api/chat/threads` | | `ChatThread[]` |
| `POST /api/chat/threads` | `{title?}` | `ChatThread` |
| `DELETE /api/chat/threads/{id}` | | `{ok: true}` |
| `POST /api/chat/threads/{id}/messages` | `{text, context: ContextChip[]}` | `text/event-stream` (see below) |
| `POST /api/chat/threads/{id}/stop` | | `{ok: true}` |

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
| `result` | `{session_id, cost_usd, duration_ms}` |
| `error` | `{message}` |
| `done` | none; the stream closes after this |

Each thread allows one turn in flight; a second request gets 409. The server persists the user message and the final assistant message to `state.json`.
