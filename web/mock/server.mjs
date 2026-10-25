// Frontend-only mock of the prr API (docs/api.md) for UI work without a real PR.
//   npm run build && node mock/server.mjs        → http://127.0.0.1:8765
//   (or `PRR_PORT=8765 npm run dev` for hot reload against it)
// No auth, no GitHub, no Claude: canned data from mock/*.json and mock/diff.patch.
import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import { join, extname, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "..", "dist");
const port = Number(process.env.PRR_PORT || 8765);

const analysis = JSON.parse(readFileSync(join(here, "analysis.json"), "utf8"));
const state = JSON.parse(readFileSync(join(here, "state.json"), "utf8"));
const patch = readFileSync(join(here, "diff.patch"), "utf8");
const patches = new Map();
for (const chunk of patch.split(/^(?=diff --git )/m)) {
  const m = /^diff --git a\/(.+?) b\/(.+)$/m.exec(chunk);
  if (m) patches.set(m[2], chunk);
}
const ghComments = [
  {
    id: 9001, path: "app/api/webhooks.py", line: 13, side: "RIGHT", start_line: null, user: "alice",
    body: "Do we version this endpoint anywhere?", created_at: "2026-10-20T09:00:00Z",
    html_url: "https://github.com/acme/hooks/pull/482#discussion_r9001", in_reply_to_id: null,
  },
  // Your comments from the review you submitted on an older commit (see /api/since-review below).
  {
    id: 9002, path: "app/delivery/sender.py", line: 20, side: "RIGHT", start_line: null, user: "you",
    body: "If this fails, should we keep the last exception? Right now the failure reason is lost.",
    created_at: "2026-10-19T15:00:00Z", html_url: "https://github.com/acme/hooks/pull/482#discussion_r9002",
    in_reply_to_id: null, review_id: 700,
  },
  {
    id: 9003, path: "app/delivery/retry.py", line: 14, side: "RIGHT", start_line: null, user: "you",
    body: "Nit: name the backoff constant.", created_at: "2026-10-19T15:00:00Z",
    html_url: "https://github.com/acme/hooks/pull/482#discussion_r9003", in_reply_to_id: null, review_id: 700,
  },
  {
    id: 9004, path: "app/api/webhooks.py", line: null, side: "RIGHT", start_line: null, user: "you",
    body: "This drops `delivered` from the response, which breaks old clients.", created_at: "2026-10-19T15:00:00Z",
    html_url: "https://github.com/acme/hooks/pull/482#discussion_r9004", in_reply_to_id: null, review_id: 700,
  },
];

// Since your review (prr/since.py). The mock pretends you reviewed an older commit; PRR_MOCK_SINCE=0 starts with no
// reviewed commit, and submitting a review here moves it to the current head (both hide the toggle).
// PRR_MOCK_NEW_COMMITS=1 makes GitHub's head differ from the analyzed one ("New commits since you opened this", in
// the banner and in Submit's warning).
state.reviewed_head ??= process.env.PRR_MOCK_SINCE === "0" ? null : "b".repeat(40);
const githubHead = process.env.PRR_MOCK_NEW_COMMITS === "1" ? "e".repeat(40) : analysis.pr.head_sha;
const estimate = { minutes: 6, usd: 2.1 };
let rereadUntil = 0;
const interdiff = [
  { path: "app/delivery/sender.py", hunks: [{ old_start: 16, old_lines: 6, new_start: 16, new_lines: 7 }] },
];
const interdiffPatch = {
  "app/delivery/sender.py": [
    "diff --git a/app/delivery/sender.py b/app/delivery/sender.py",
    "index 5555550..5555555 100644",
    "--- a/app/delivery/sender.py",
    "+++ b/app/delivery/sender.py",
    "@@ -16,6 +16,7 @@ def send(delivery: WebhookDelivery, client: httpx.Client) -> WebhookDelivery:",
    "     try:",
    "         with_retry(attempt, retry_on=(httpx.TransportError, httpx.HTTPStatusError))",
    "         delivery.status = DeliveryStatus.DELIVERED",
    "-    except httpx.HTTPError:",
    "+    except httpx.HTTPError as exc:",
    "         delivery.status = DeliveryStatus.FAILED",
    "+        delivery.last_error = str(exc)",
    "     return delivery",
    "",
  ].join("\n"),
};
const earlierComments = [
  { id: 9002, path: "app/delivery/sender.py", line: 20, status: "fixed",
    note: "The failure reason is now kept in last_error (app/delivery/sender.py:21)." },
  { id: 9003, path: "app/delivery/retry.py", line: 14, status: "line-unchanged", note: null },
  { id: 9004, path: "app/api/webhooks.py", line: null, status: "still-open",
    note: "The response still leaves out `delivered`, so older clients break." },
];

// A stop is new when its lines meet an interdiff hunk (same path, new-side lines): prr/since.py is_new_stop.
function isNewStop(stop) {
  if (!stop.path || (stop.side ?? "RIGHT") !== "RIGHT") return false;
  const lo = stop.start_line ?? stop.start ?? stop.line;
  const hi = stop.end_line ?? stop.end ?? stop.line;
  if (lo == null && hi == null) return false;
  return interdiff.some((f) => f.path === stop.path && f.hunks.some((h) =>
    h.new_start <= (hi ?? lo) && (lo ?? hi) <= h.new_start + Math.max(h.new_lines, 1) - 1));
}

function sinceReview() {
  const head = analysis.pr.head_sha;
  const base = { reviewed_head: state.reviewed_head, head, files: [], patch: {}, note: null, ok: true, full_pr: false,
                 comments: [], comments_error: null, new_stop_ids: [] };
  if (!state.reviewed_head || state.reviewed_head === head) return base;
  const running = Date.now() < rereadUntil;
  return {
    ...base, files: interdiff, patch: interdiffPatch,
    comments: earlierComments.map((c) => (running && c.status !== "line-unchanged" ? { ...c, status: "checking", note: null } : c)),
    new_stop_ids: (analysis.stops ?? []).filter(isNewStop).map((s) => s.id),
  };
}

const MIME = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml", ".json": "application/json" };
let seq = 100;
const now = () => new Date().toISOString();

function send(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}
async function readBody(req) {
  let raw = "";
  for await (const c of req) raw += c;
  return raw ? JSON.parse(raw) : {};
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function inHunk(path, side, line) {
  const f = analysis.files.find((x) => x.path === path);
  return !!f?.hunks.some((h) =>
    side === "RIGHT" ? line >= h.new_start && line < h.new_start + h.new_lines : line >= h.old_start && line < h.old_start + h.old_lines,
  );
}

// ---- guided review and coverage (prr/stops.py, prr/coverage.py), simplified: the stops in analysis.json were
// built once by prr/stops.py with a cap of 8; answers here don't re-run the cap.
const STOP_ID = /^stop-[0-9a-f]{40}$/;
const listedStops = () => analysis.stops.filter((s) => !s.more && !s.low_confidence && !s.dropped);
const isDone = (s) => !!state.stop_verdicts[s.id] || (!!s.finding_id && state.dismissed_suggestions.includes(s.finding_id));

function reviewSummary() {
  const left = listedStops().filter((s) => !isDone(s));
  return {
    ...analysis.review_summary,
    minutes_left: left.reduce((n, s) => n + 2 + Math.min(5, Math.floor(s.changed_lines / 20)), 0),
  };
}

function hunkId(h) {
  if (h.new_lines > 0) return `RIGHT:${h.new_start}-${h.new_start + h.new_lines - 1}`;
  return `LEFT:${h.old_start}-${h.old_start + Math.max(h.old_lines, 1) - 1}`;
}

function coverage() {
  const ordered = [...analysis.stops].sort((a, b) => a.order - b.order);
  const left = ordered
    .filter((s) => !s.low_confidence && !s.dropped && !isDone(s))
    .sort((a, b) => Number(a.more) - Number(b.more) || a.order - b.order);
  const answered = ordered.filter((s) => state.stop_verdicts[s.id]);
  const byFile = {};
  for (const f of analysis.files) {
    if (f.risk !== "high" || !f.hunks.length) continue;
    const seen = new Set(state.seen_hunks[f.path] || []);
    const looked = f.hunks.filter((h) => {
      if (seen.has(hunkId(h))) return true;
      return answered.some((s) => {
        if (s.path !== f.path || s.line == null) return false;
        const [start, count] = s.side === "LEFT" ? [h.old_start, h.old_lines] : [h.new_start, h.new_lines];
        return count > 0 && s.line >= start && s.line < start + count;
      });
    }).length;
    byFile[f.path] = { seen: looked, total: f.hunks.length };
  }
  const files = Object.values(byFile);
  const notChecked = left.map((s) => ({ stop_id: s.id, path: s.path, line: s.line, question: s.question, kind: s.kind }));
  return {
    not_checked: notChecked,
    unsure: ordered
      .filter((s) => state.stop_verdicts[s.id]?.verdict === "unsure")
      .map((s) => ({ stop_id: s.id, path: s.path, line: s.line, question: s.question })),
    skipped_areas: analysis.skipped_areas,
    high_risk_unseen: {
      hunks: files.reduce((n, v) => n + v.total - v.seen, 0),
      files: files.filter((v) => v.seen < v.total).length,
      by_file: byFile,
    },
    low_confidence: ordered.filter((s) => s.low_confidence && !isDone(s)).length,
    running: !["complete", "failed"].includes(analysis.status),
    count: notChecked.length + analysis.skipped_areas.length,
  };
}

function answerStop(stop, verdict, openedAt) {
  const prev = state.stop_verdicts[stop.id];
  if (!prev || prev.verdict !== verdict) {
    const anchor = Object.fromEntries(
      ["kind", "path", "side", "line", "start_line", "question", "finding_id", "bot_comment_id", "check_name"].map((k) => [k, stop[k]]),
    );
    state.stop_verdicts[stop.id] = { verdict, at: now(), head_sha: analysis.pr.head_sha, anchor, opened_at: openedAt ?? null, answered_at: now() };
  }
  const out = { verdict: state.stop_verdicts[stop.id], draft: null, dismissed: null, review_body_draft: null };
  if (verdict === "real" && stop.kind === "ci") {
    const block = (stop.comment_body || "").trim();
    if (block && !state.review_body_draft.includes(block)) {
      state.review_body_draft = state.review_body_draft.trim() ? `${state.review_body_draft.trimEnd()}\n\n${block}` : block;
    }
    out.review_body_draft = state.review_body_draft;
  } else if (verdict === "real" && stop.kind !== "look_for") {
    let d = state.drafts.find((x) => x.origin_id === stop.id);
    if (!d) {
      d = {
        id: `d${++seq}`, path: stop.path, side: stop.side, line: stop.line, start_line: stop.start_line ?? null, start_side: null,
        body: stop.comment_body || "", source: stop.kind === "finding" ? "suggestion" : "user",
        suggestion_id: stop.kind === "finding" ? stop.finding_id : null, origin_id: stop.id, outside_link: false,
        anchored: inHunk(stop.path, stop.side, stop.line), outdated: false, commit_id: analysis.pr.head_sha, created_at: now(),
      };
      state.drafts.push(d);
    }
    out.draft = d;
  } else if (verdict === "not" && stop.finding_id) {
    if (!state.dismissed_suggestions.includes(stop.finding_id)) state.dismissed_suggestions.push(stop.finding_id);
    out.dismissed = stop.finding_id;
  }
  return out;
}

function takeBack(id, stop) {
  const rec = state.stop_verdicts[id];
  const out = { ok: true, kept_draft: null, restored: null, review_body_draft: null };
  if (!rec) return out;
  delete state.stop_verdicts[id];
  const live = stop && !stop.dropped ? stop : null;
  if (rec.verdict === "not") {
    const fid = stop?.finding_id ?? rec.anchor?.finding_id;
    if (fid && state.dismissed_suggestions.includes(fid)) {
      state.dismissed_suggestions = state.dismissed_suggestions.filter((x) => x !== fid);
      out.restored = fid;
    }
  } else if (rec.verdict === "real" && (live?.kind ?? rec.anchor?.kind) === "ci") {
    const block = (live?.comment_body || "").trim();
    const at = block ? state.review_body_draft.indexOf(block) : -1;
    if (at >= 0) {
      const text = state.review_body_draft;
      state.review_body_draft = `${text.slice(0, at).trimEnd()}\n\n${text.slice(at + block.length).trimStart()}`.trim();
      out.review_body_draft = state.review_body_draft;
    }
  } else if (rec.verdict === "real") {
    for (const d of state.drafts.filter((x) => x.origin_id === id)) {
      if (live && live.comment_body != null && d.body.trim() === live.comment_body.trim()) {
        state.drafts = state.drafts.filter((x) => x.id !== d.id);
      } else {
        out.kept_draft = d;
      }
    }
  }
  return out;
}

// ---- Understand this PR (prr/understand.py), simplified: one step per file in reading order; a test attaches to
// the file it tests ("Tested by"), and generated, lock and binary files are listed on the chapter's first step to
// skim. Each step's text is canned and takes about a second the first time, as if Claude were reading the code;
// app/config/settings.py has no reason in the PR (why_missing). PRR_MOCK_UNDERSTAND_FAIL=1 makes every step fail (502).
const LEVELS = ["new", "familiar"];
state.understood_steps ??= {};
state.understand_level ??= "new";
const firstSentence = (text) => (text ? (/^.*?[.!?](?=\s|$)/.exec(text)?.[0] ?? text) : null);

function buildSteps() {
  const files = new Map(analysis.files.map((f) => [f.path, f]));
  const steps = [];
  for (const ch of analysis.chapters) {
    const skim = [];
    const mine = [];
    for (const cf of ch.files) {
      const f = files.get(cf.path);
      if (!f) continue;
      if (f.generated || f.binary) {
        skim.push(f.path);
        continue;
      }
      if (f.tests_for && files.has(f.tests_for)) continue; // listed under the code it tests
      const hunks = f.hunks.map(({ old_start, old_lines, new_start, new_lines }) => ({ old_start, old_lines, new_start, new_lines }));
      const range = hunks.map((h) => `${h.old_start},${h.old_lines},${h.new_start},${h.new_lines}`).join(";");
      mine.push({
        id: `und-${createHash("sha1").update(`${f.path}|${range}|${f.head_blob}`).digest("hex")}`,
        chapter_id: ch.id, chapter_title: ch.title, chapter_goal: ch.goal || null, path: f.path, hunks,
        role: firstSentence(f.summary), tests: f.tested_by.filter((t) => files.has(t)), skim: [], order: 0, kind: "file",
        files: [f.path],
      });
    }
    if (mine.length) mine[0].skim = skim;
    steps.push(...mine);
  }
  steps.forEach((s, i) => (s.order = i));
  return steps;
}
const steps = buildSteps();

const EXPLAIN = {
  "app/models/webhook.py": {
    before: "`WebhookDelivery` is the record of one webhook send. It had a single `delivered` flag: true once the POST worked, false before that and after a failure, so a failed send looked like one that hadn't run yet.",
    change: "The flag becomes a `status` with four values (pending, retrying, delivered, failed), and an `attempts` counter is added. Code can now tell a failure apart from a send that hasn't happened.",
    why: "Failed deliveries were invisible to support, because `delivered = False` meant both \"not sent yet\" and \"gave up\".",
    why_source: "PR description",
    connects: [
      { path: "app/delivery/sender.py", line: 18, text: "`send()` sets `status` after its tries" },
      { path: "app/api/webhooks.py", line: 13, text: "the redeliver endpoint returns `status` and `attempts`" },
      { path: "app/workers/reconcile.py", line: 42, text: "still reads `delivered` (outside this PR)" },
    ],
    terms: [
      { term: "`DeliveryStatus`", meaning: "Where a delivery is: pending, retrying, delivered or failed." },
      { term: "dataclass", meaning: "A Python class whose fields are listed once; `@dataclass` writes its constructor." },
    ],
    look_at: [{ side: "LEFT", line: 10 }, { side: "RIGHT", line: 18 }, { side: "RIGHT", line: 19 }],
  },
  "app/delivery/retry.py": {
    before: "There was no retry code. A webhook send was one HTTP POST; if it failed, the error went straight back to the caller.",
    change: "Adds `with_retry()`, which calls a function again when it raises one of the given errors, waiting a random, growing time between tries (5 tries by default). After the last try it raises the error.",
    why: "Receivers often fail for a moment (deploys, rate limits), and one failed POST used to lose the delivery.",
    why_source: "commit \"Retry webhook deliveries with exponential backoff\"",
    connects: [
      { path: "app/delivery/sender.py", line: 17, text: "`send()` wraps each POST in `with_retry()`" },
      { path: "tests/delivery/test_retry.py", line: 16, text: "the test calls it with a function that fails twice" },
    ],
    terms: [
      { term: "exponential backoff", meaning: "Waiting longer after each failure: 0.5 s, 1 s, 2 s, and so on." },
      { term: "full jitter", meaning: "Waiting a random time up to that limit, so many senders don't retry at the same moment." },
      { term: "`retry_on`", meaning: "The errors worth trying again; any other error is raised at once." },
    ],
    look_at: [{ side: "RIGHT", line: 11 }, { side: "RIGHT", line: 19 }, { side: "RIGHT", line: 22 }],
  },
  "app/config/settings.py": {
    before: "`Settings` reads the app's configuration from environment variables. Webhook sends waited at most 10 seconds for a reply.",
    change: "Sends now wait up to 30 seconds, and two new settings shape the retry loop: how many tries (5) and the longest wait between them (30 s).",
    why: null,
    why_source: null,
    connects: [{ path: "app/delivery/sender.py", line: 6, text: "`TIMEOUT` is still 10 here; it doesn't read these settings" }],
    terms: [{ term: "`BaseSettings`", meaning: "A pydantic class that fills its fields from environment variables, such as `WEBHOOK_TIMEOUT_S`." }],
    look_at: [{ side: "RIGHT", line: 6 }, { side: "RIGHT", line: 7 }],
  },
  "app/delivery/sender.py": {
    before: "`send()` made one POST to the receiver. On success it set `delivered = True`; on an HTTP error it raised, and the caller had to handle it.",
    change: "Each POST now runs inside `with_retry()`, counting tries in `attempts`. When every try fails, `send()` no longer raises: it sets `status` to failed and returns the delivery.",
    why: "Callers shouldn't have to catch delivery errors; the status says what happened.",
    why_source: "PR description",
    connects: [
      { path: "app/api/webhooks.py", line: 11, text: "`redeliver()` calls `send()` and saves the result" },
      { path: "app/delivery/retry.py", line: 14, text: "`with_retry()` does the waiting between tries" },
    ],
    terms: [
      { term: "`raise_for_status()`", meaning: "An httpx call that raises an error for a 4xx or 5xx reply." },
      { term: "`httpx.TransportError`", meaning: "The connection itself failed: no reply came back." },
    ],
    look_at: [{ side: "RIGHT", line: 17 }, { side: "RIGHT", line: 19 }, { side: "RIGHT", line: 20 }],
  },
  "app/api/webhooks.py": {
    before: "The redeliver endpoint sends one stored delivery again and answers with its id and `delivered` (true or false).",
    change: "The answer now has `status` and `attempts` instead of `delivered`. Clients that read `delivered` no longer get it.",
    why: "It follows the new `status` field: \"the API now reports the real outcome\".",
    why_source: "PR description",
    connects: [{ path: "app/models/webhook.py", line: 18, text: "where `status` comes from" }],
    terms: [{ term: "redeliver", meaning: "Sending a webhook again on request, for example after the receiver was down." }],
    look_at: [{ side: "LEFT", line: 13 }, { side: "RIGHT", line: 13 }],
  },
};
const explained = new Set(); // "<id>:<level>" made once already: answered at once, as from the cache

async function explainStep(step, level) {
  const key = `${step.id}:${level}`;
  const cached = explained.has(key);
  if (!cached) await sleep(1000);
  if (process.env.PRR_MOCK_UNDERSTAND_FAIL === "1") return [502, { detail: "Claude stopped before it finished (mock)" }];
  explained.add(key);
  const e = EXPLAIN[step.path] ?? {
    before: "This file wasn't part of the code before in this form.", change: "See the diff.", why: null, why_source: null,
    connects: [], terms: [], look_at: [],
  };
  const familiar = level === "familiar";
  return [200, {
    id: step.id, level, ...e,
    before: familiar ? firstSentence(e.before) : e.before,
    why_missing: !e.why,
    terms: familiar ? [] : e.terms,
    cached, cost_usd: cached ? 0 : 0.08,
  }];
}

function preview(body, event) {
  const anchored = state.drafts.filter((d) => d.anchored);
  const unanchored = state.drafts.filter((d) => !d.anchored);
  const payload = {
    commit_id: analysis.pr.head_sha,
    body,
    ...(event === "PENDING" ? {} : { event }),
    comments: anchored.map((d) => ({
      path: d.path, side: d.side, line: d.line, body: d.body,
      ...(d.start_line ? { start_line: d.start_line, start_side: d.side } : {}),
    })),
  };
  return {
    payload, inline_count: anchored.length, unanchored, analyzed_head: analysis.pr.head_sha,
    current_head: githubHead, head_changed: githubHead !== analysis.pr.head_sha, viewer: "you", is_author: false,
  };
}

async function api(req, res, url) {
  const p = url.pathname;
  const m = req.method;
  if (p === "/api/health") return send(res, 200, { ok: true, pr: analysis.pr.number });
  if (p === "/api/analysis") return send(res, 200, { ...analysis, review_summary: reviewSummary() });
  if (p === "/api/coverage") return send(res, 200, coverage());
  if (p === "/api/review-body-draft" && m === "PUT") {
    const b = await readBody(req);
    if (typeof b.body !== "string" || b.body.length > 65536) return send(res, 400, { detail: "The review summary is limited to 65,536 characters" });
    state.review_body_draft = b.body;
    return send(res, 200, { ok: true });
  }
  if (p === "/api/seen" && m === "POST") {
    const b = await readBody(req);
    if (!analysis.files.some((f) => f.path === b.path)) return send(res, 404, { detail: "file is not part of this PR" });
    if (!Array.isArray(b.hunks) || b.hunks.length > 5000 || b.hunks.some((h) => !/^(LEFT|RIGHT):\d{1,9}-\d{1,9}$/.test(h))) {
      return send(res, 400, { detail: "not a hunk id" });
    }
    const kept = (state.seen_hunks[b.path] || []).filter((h) => !b.hunks.includes(h));
    state.seen_hunks[b.path] = [...kept, ...new Set(b.hunks)].slice(-5000);
    return send(res, 200, { ok: true });
  }
  if (p === "/api/state") return send(res, 200, state);
  if (p === "/api/trust") return send(res, 200, { author: analysis.pr.author, association: "CONTRIBUTOR", trusted: false, state: "outside" });
  if (p === "/api/cost") {
    return send(res, 200, {
      total_usd: 3.42, unknown: false, running: false, breakdown: { analysis: 2.1, chat: 1.2, other: 0.12 }, runs: [],
      threads: Object.fromEntries(state.threads.map((t) => [t.id, { total_usd: 1.2, turns: 2, unknown: false, long: false }])),
      warn_usd: 0, over_budget: false, long_thread: { turns: 15, usd: 3 },
    });
  }
  if (p === "/api/diff") {
    const path = url.searchParams.get("path");
    return patches.has(path) ? send(res, 200, { path, patch: patches.get(path) }) : send(res, 404, { detail: "not in diff" });
  }
  if (p === "/api/file") {
    const path = url.searchParams.get("path");
    const body = patches.get(path);
    const content = body
      ? body.split("\n").filter((l) => /^[ +]/.test(l) && !l.startsWith("+++")).map((l) => l.slice(1)).join("\n")
      : `# ${path}\n# (mock) file outside the PR\n` + Array.from({ length: 120 }, (_, i) => `line_${i + 3} = ${i}`).join("\n");
    return send(res, 200, { path, at: url.searchParams.get("at"), content });
  }
  if (p === "/api/reviewed" && m === "PUT") {
    const { path, reviewed } = await readBody(req);
    const f = analysis.files.find((x) => x.path === path);
    if (reviewed) state.reviewed[path] = f?.head_blob ?? "";
    else delete state.reviewed[path];
    return send(res, 200, { reviewed: state.reviewed });
  }
  if (p === "/api/drafts" && m === "POST") {
    const b = await readBody(req);
    const d = {
      id: `d${++seq}`, path: b.path, side: b.side, line: b.line, start_line: b.start_line ?? null, body: b.body,
      source: b.source, suggestion_id: b.suggestion_id ?? null, origin_id: b.origin_id ?? null, anchored: inHunk(b.path, b.side, b.line),
      outdated: false, commit_id: analysis.pr.head_sha, created_at: now(),
    };
    state.drafts.push(d);
    return send(res, 200, d);
  }
  let mm;
  if ((mm = /^\/api\/drafts\/([^/]+)$/.exec(p))) {
    const i = state.drafts.findIndex((d) => d.id === decodeURIComponent(mm[1]));
    if (i === -1) return send(res, 404, { detail: "no such draft" });
    if (m === "DELETE") {
      state.drafts.splice(i, 1);
      return send(res, 200, { ok: true });
    }
    const b = await readBody(req);
    state.drafts[i] = { ...state.drafts[i], body: b.body };
    return send(res, 200, state.drafts[i]);
  }
  if ((mm = /^\/api\/suggestions\/([^/]+)\/(dismiss|restore)$/.exec(p))) {
    const id = decodeURIComponent(mm[1]);
    state.dismissed_suggestions = state.dismissed_suggestions.filter((x) => x !== id);
    if (mm[2] === "dismiss") state.dismissed_suggestions.push(id);
    return send(res, 200, { dismissed_suggestions: state.dismissed_suggestions });
  }
  if ((mm = /^\/api\/stops\/([^/]+)\/verdict$/.exec(p))) {
    const id = decodeURIComponent(mm[1]);
    if (!STOP_ID.test(id)) return send(res, 400, { detail: "not a stop id" });
    const stop = analysis.stops.find((s) => s.id === id);
    if (!stop && !state.stop_verdicts[id]) return send(res, 404, { detail: "no such stop" });
    if (m === "DELETE") return send(res, 200, takeBack(id, stop));
    const b = await readBody(req);
    if (!["real", "not", "unsure"].includes(b.verdict)) return send(res, 400, { detail: "verdict must be real, not or unsure" });
    if (!stop || stop.dropped) return send(res, 409, { detail: "This stop changed; reload" });
    return send(res, 200, answerStop(stop, b.verdict, b.opened_at));
  }
  if (p === "/api/understand" && m === "GET") {
    return send(res, 200, { steps, level: state.understand_level, understood: state.understood_steps });
  }
  if (p === "/api/understand/level" && m === "PUT") {
    const b = await readBody(req);
    if (!LEVELS.includes(b.level)) return send(res, 400, { detail: "level must be new or familiar" });
    state.understand_level = b.level;
    return send(res, 200, { level: b.level });
  }
  if ((mm = /^\/api\/understand\/steps\/([^/]+)(\/understood)?$/.exec(p)) && m === "POST") {
    const step = steps.find((s) => s.id === decodeURIComponent(mm[1]));
    if (!step) return send(res, 404, { detail: "no such step" });
    const b = await readBody(req);
    if (mm[2]) {
      if (b.understood) state.understood_steps[step.id] ??= now();
      else delete state.understood_steps[step.id];
      return send(res, 200, { understood: !!b.understood });
    }
    if (!LEVELS.includes(b.level)) return send(res, 400, { detail: "level must be new or familiar" });
    const [status, out] = await explainStep(step, b.level);
    return send(res, status, out);
  }
  if (p === "/api/github/threads") return send(res, 200, { comments: ghComments });
  if (p === "/api/review/preview") {
    const b = await readBody(req);
    return send(res, 200, preview(b.body ?? "", b.event));
  }
  if (p === "/api/review/draft-summary") {
    await sleep(600);
    return send(res, 200, { body: "Thanks! The retry helper looks solid. The main concerns are the breaking API response and retrying 4xx. See inline comments." });
  }
  if (p === "/api/since-review") return send(res, 200, sinceReview());
  if (p === "/api/head") {
    return send(res, 200, {
      analyzed_head: analysis.pr.head_sha, current_head: githubHead, head_changed: githubHead !== analysis.pr.head_sha,
      running: Date.now() < rereadUntil, estimate, error: null,
    });
  }
  if (p === "/api/reanalyze" && m === "POST") {
    if (Date.now() < rereadUntil) return send(res, 409, { detail: "The PR is already being read. Wait for it to finish." });
    rereadUntil = Date.now() + 8000;
    return send(res, 200, { started: true, estimate });
  }
  if (p === "/api/review/submit") {
    const b = await readBody(req);
    state.submitted.push({ review_id: ++seq, html_url: `${analysis.pr.url}#pullrequestreview-${seq}`, event: b.event, comments: state.drafts.length, at: now() });
    state.drafts = [];
    state.reviewed_head = analysis.pr.head_sha;
    state.reviewed_base = analysis.pr.merge_base_sha;
    state.reviewed_stop_ids = (analysis.stops ?? []).map((s) => s.id);
    return send(res, 200, { ok: true, review_id: seq, html_url: `${analysis.pr.url}#pullrequestreview-${seq}` });
  }
  if (p === "/api/chat/threads" && m === "GET") return send(res, 200, state.threads);
  if (p === "/api/chat/threads" && m === "POST") {
    const b = await readBody(req);
    const t = { id: `t${++seq}`, title: b.title || "New conversation", session_id: crypto.randomUUID(), messages: [], created_at: now() };
    state.threads.push(t);
    return send(res, 200, t);
  }
  if ((mm = /^\/api\/chat\/threads\/([^/]+)$/.exec(p)) && m === "DELETE") {
    state.threads = state.threads.filter((t) => t.id !== mm[1]);
    return send(res, 200, { ok: true });
  }
  if ((mm = /^\/api\/chat\/threads\/([^/]+)\/stop$/.exec(p))) return send(res, 200, { ok: true });
  if ((mm = /^\/api\/chat\/threads\/([^/]+)\/messages$/.exec(p))) {
    const t = state.threads.find((x) => x.id === mm[1]);
    if (!t) return send(res, 404, { detail: "no such thread" });
    const b = await readBody(req);
    res.writeHead(200, { "Content-Type": "text/event-stream", "Cache-Control": "no-cache", Connection: "keep-alive" });
    const ev = (e) => res.write(`data: ${JSON.stringify(e)}\n\n`);
    let closed = false;
    req.on("close", () => (closed = true));
    ev({ type: "thinking" });
    await sleep(400);
    ev({ type: "tool", name: "Grep", summary: "with_retry" });
    await sleep(300);
    ev({ type: "tool", name: "Read", summary: "app/delivery/sender.py" });
    const answer =
      "Here's what happens in `app/delivery/sender.py:16-20`:\n\n1. `attempt()` is retried by `with_retry` (`app/delivery/retry.py:14`).\n2. On final failure the exception propagates and **status becomes FAILED**.\n\n```python\nexcept httpx.HTTPError:\n    delivery.status = DeliveryStatus.FAILED\n```\n\nA caller outside the PR, `app/workers/reconcile.py:42`, still reads `delivered`.";
    let text = "";
    for (const piece of answer.match(/.{1,12}/gs)) {
      if (closed) return;
      text += piece;
      ev({ type: "text", text: piece });
      await sleep(25);
    }
    ev({ type: "result", session_id: t.session_id, cost_usd: 0.0187, duration_ms: 3100 });
    ev({ type: "done" });
    res.end();
    t.messages.push(
      { role: "user", text: b.text, context: b.context ?? [], tools: [], cost_usd: null, error: null, ts: now() },
      { role: "assistant", text, context: [], tools: [{ name: "Grep", summary: "with_retry" }, { name: "Read", summary: "app/delivery/sender.py" }], cost_usd: 0.0187, error: null, ts: now() },
    );
    return;
  }
  return send(res, 404, { detail: `mock: no route ${m} ${p}` });
}

createServer(async (req, res) => {
  const url = new URL(req.url, `http://${req.headers.host}`);
  try {
    if (url.pathname.startsWith("/api/")) return await api(req, res, url);
    let file = join(dist, url.pathname === "/" ? "index.html" : url.pathname);
    if (!file.startsWith(dist) || !existsSync(file) || statSync(file).isDirectory()) file = join(dist, "index.html");
    res.writeHead(200, { "Content-Type": MIME[extname(file)] || "application/octet-stream" });
    res.end(readFileSync(file));
  } catch (e) {
    send(res, 500, { detail: String(e) });
  }
}).listen(port, "127.0.0.1", () => console.log(`mock prr server on http://127.0.0.1:${port}`));
