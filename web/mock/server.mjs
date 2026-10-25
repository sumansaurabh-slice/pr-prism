// Frontend-only mock of the prr API (docs/api.md) for UI work without a real PR.
//   npm run build && node mock/server.mjs        → http://127.0.0.1:8765
//   (or `PRR_PORT=8765 npm run dev` for hot reload against it)
// No auth, no GitHub, no Claude: canned data from mock/*.json and mock/diff.patch.
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
// PRR_MOCK_NEW_COMMITS=1 makes GitHub's head differ from the analyzed one ("New commits since you opened this").
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
    current_head: analysis.pr.head_sha, head_changed: false, viewer: "you", is_author: false,
  };
}

async function api(req, res, url) {
  const p = url.pathname;
  const m = req.method;
  if (p === "/api/health") return send(res, 200, { ok: true, pr: analysis.pr.number });
  if (p === "/api/analysis") return send(res, 200, analysis);
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
      source: b.source, suggestion_id: b.suggestion_id ?? null, origin_id: null, anchored: inHunk(b.path, b.side, b.line),
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
