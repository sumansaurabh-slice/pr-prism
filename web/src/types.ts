// Mirrors prr/models.py (analysis.json) and prr/state.py (state.json). Change both together.

export type Level = "low" | "medium" | "high";
export type Side = "LEFT" | "RIGHT";
export type Category =
  | "schema" | "types" | "core" | "api" | "ui" | "config" | "infra" | "test" | "docs" | "generated";
export type NoteKind = "explain" | "risk" | "flow" | "test-gap";
export type Severity = "blocker" | "major" | "minor" | "nit";
export type FileStatus = "added" | "modified" | "deleted" | "renamed" | "copied";
export type ImpactKind =
  | "callers" | "breaking" | "migration" | "config" | "perf" | "security" | "tests" | "sensitive" | "other";
export type FlowNodeKind = "entry" | "logic" | "store" | "external" | "test";
// deepchecking: the deep check ("what could break") runs after the summary; its findings arrive one area at a time
export type ProgressStatus = "fetched" | "annotating" | "synthesizing" | "deepchecking" | "complete" | "failed";
export type ReviewEvent = "COMMENT" | "APPROVE" | "REQUEST_CHANGES" | "PENDING";
// judge = scored by the second look; rule = a blocker the code check confirmed ("confirmed by rule");
// carried = from the previous round, keeping its verdict; pending = waiting for its second look;
// none = the second look failed ("not second-checked"; never hidden)
export type SecondLook = "judge" | "rule" | "carried" | "pending" | "none";
// guided review: where a stop came from, and the reviewer's answer (Real problem / Not a problem / Not sure)
export type StopKind = "finding" | "risk_note" | "look_for" | "ci" | "bot";
export type Verdict = "real" | "not" | "unsure";

export interface Commit { sha: string; headline: string; author: string }

export interface PRInfo {
  owner: string; repo: string; number: number; title: string; url: string; author: string; body: string;
  state: string; is_draft: boolean; base_ref: string; head_ref: string;
  base_sha: string; merge_base_sha: string; head_sha: string;
  additions: number; deletions: number; changed_files: number; labels: string[]; commits: Commit[];
  author_association?: string | null; // OWNER, MEMBER, COLLABORATOR, CONTRIBUTOR, …; null: the check failed
}

export interface HunkInfo {
  id: string; old_start: number; old_lines: number; new_start: number; new_lines: number; header: string;
}

export interface Note { side: Side; start: number; end: number; kind: NoteKind; text: string }

export interface FileEntry {
  path: string; old_path: string | null; status: FileStatus;
  additions: number; deletions: number; binary: boolean;
  category: Category; generated: boolean; sensitive: boolean; minutes: number;
  base_blob: string | null; head_blob: string | null;
  tests_for: string | null; tested_by: string[];
  is_test: boolean; is_docs: boolean; kind_reason: string | null; // for the "Hide test/docs files" checkboxes
  hunks: HunkInfo[];
  annotated: boolean;
  risk: Level | null; summary: string | null; look_for: string[]; notes: Note[]; changed_symbols: string[];
}

export interface Summary { tldr: string; why: string; how: string; risk: Level; risk_reasons: string[] }
export interface ChapterFile { path: string; why_now: string }
export interface Chapter { id: string; title: string; goal: string; files: ChapterFile[] }
export interface FlowNode { id: string; label: string; kind: FlowNodeKind; path: string | null; line: number | null }
export interface FlowEdge { source: string; target: string; label: string | null }
export interface Flow { id: string; title: string; narrative: string; nodes: FlowNode[]; edges: FlowEdge[] }
export interface Ref { path: string; line: number | null; at: "head" | "base" }
export interface ImpactItem { kind: ImpactKind; severity: Level; title: string; detail: string; refs: Ref[] }
export interface Question { text: string; refs: Ref[] }
/** A spot a deep-check finding read to show the failure (a line in the PR's new code; may be outside the diff). */
export interface EvidenceRef { path: string; line: number }
/** A problem found in the PR, ready to post as a review comment. */
export interface Finding {
  id: string; path: string; side: Side; line: number; start_line: number | null;
  anchored: boolean;            // on a diff line; false = shown at file level, posted inside the review summary
  anchor: string;
  severity: Severity;           // blocker = blocks merge (rule-checked), major = must fix, minor = should fix, nit = could improve
  // "intent" = a question for the author ("Why was X removed?"): always minor, shown as "Question for the author";
  // its title is the question and comment_body the polite question to post
  category: string; title: string; impact: string; fix: string;
  suggestion: string | null; one_click: boolean;
  why: string; evidence: string;
  confidence: number | null; judge_score: number | null; judge_reason: string | null;
  low_confidence: boolean;      // hidden by default: only the second look hides (drop, or a score under 5)
  second_look: SecondLook;
  source: string;               // "claude" | "rule:<id>"
  note: string | null;
  status: "new" | "still-open";
  also_flagged_by: string[];
  comment_body: string;
  scenario: string[];           // deep check: "How it could happen", 2-6 steps with example values ([] otherwise)
  question: string | null;      // deep check: one yes/no question for the reviewer
  evidence_refs: EvidenceRef[]; // deep check: up to 4 spots it read
  sources: string[];            // which passes found it: "annotate", "blockers", "deep-check" (twins list both)
}

/** A comment another review tool (a bot) already posted, with Claude's second look. */
export interface BotFinding {
  comment_id: number; bot: string; path: string; line: number | null; side: Side | null;
  severity: Severity | null; title: string; body: string; html_url: string;
  verdict: "agree" | "disagree" | "unsure" | null; verdict_reason: string | null;
}

export interface CIExplanation {
  summary: string; cause: string; caused_by_pr: "yes" | "no" | "unclear";
  related: { path: string; line: number | null }[]; fix: string;
}
export interface CICheck {
  name: string; kind: "check" | "status"; state: "pass" | "fail" | "pending" | "skipped" | "neutral";
  url: string | null; app: string | null; summary: string | null; log_tail: string | null;
  explanation: CIExplanation | null;
}
export interface CIAnnotation { check: string; path: string; line: number; end_line: number | null; level: string; message: string }
export interface CIInfo { fetched_at: string | null; head_sha: string | null; checks: CICheck[]; annotations: CIAnnotation[] }

/** An earlier finding or comment, re-checked on the latest commit. */
export interface Rechecked {
  kind: "finding" | "comment"; title: string; path: string; line: number | null;
  status: "fixed" | "still-open"; how: string; author: string | null; html_url: string | null;
}

export interface Progress {
  status: ProgressStatus; clusters_total: number; clusters_done: number; clusters_failed: number;
  message: string; started_at: string | null; finished_at: string | null; log: string[];
  cost_usd: number;             // this analysis run's spend so far
  deepcheck_total: number;      // areas the deep check is checking ("Deep check: 2 of 4 areas done")
  deepcheck_done: number;
  deepcheck_cost_usd: number;   // the deep check's share of cost_usd
}

/**
 * Guided review: one place that needs a human, with one question (prr/stops.py). `id` is stable across re-runs.
 * Listed stops are those with `more`, `low_confidence` and `dropped` all false; `more` = past the cap ("N more
 * places"); `low_confidence` = a finding the second look hides (listed only with "Include low-confidence findings");
 * `dropped` = an answer whose stop is gone or now hidden ("earlier answer", "Second look dropped this").
 */
export interface Stop {
  id: string;                   // "stop-<40 hex>"; the route is #/review/<id>
  kind: StopKind;
  path: string | null; side: Side | null; line: number | null; start_line: number | null;  // path null for CI
  question: string;             // the heading (may hold Markdown inline code)
  scenario: string[];           // "How it could happen" (deep-check findings)
  claude_view: string | null;   // Claude's view, shown muted after the code (Markdown)
  confidence: number | null;    // "% sure"
  judge_score: number | null;   // "second look 7/10"
  second_look: SecondLook | null;
  severity: string | null; category: string | null;
  finding_id: string | null; bot_comment_id: number | null; check_name: string | null;
  comment_body: string | null;  // what "Real problem" pre-fills (CI: what it adds to the review summary)
  chapter: string | null;       // the reading-order chapter's id (Analysis.chapters[].id)
  order: number;                // position in reading order (chapter, file, line; CI first)
  more: boolean;
  low_confidence: boolean;
  sources: string[];            // annotate, blockers, deep-check, ci, bots
  changed_lines: number;        // changed lines in the stop's hunk (the whole file for a look_for item)
  dropped: boolean;
}

/** The Overview's plan box: "N places need a human · M files have nothing flagged · ~K min". */
export interface ReviewSummary {
  places: number;               // listed stops
  more_places: number;          // stops past the cap
  quiet_files: number;          // files with no stop at all
  minutes_left: number;         // Σ over unanswered listed stops of 2 + min(5, floor(changed_lines / 20))
}

/** A deep-check area this run didn't check (failed, stopped or skipped); it counts as not checked. */
export interface SkippedArea { title: string; reason: string }

export interface Analysis {
  schema_version: number; status: ProgressStatus; focus: string | null; pr: PRInfo;
  summary: Summary | null; files: FileEntry[]; chapters: Chapter[]; flows: Flow[];
  impact: ImpactItem[]; questions: Question[];
  findings: Finding[]; bot_findings: BotFinding[]; ci: CIInfo | null; rechecked: Rechecked[];
  progress: Progress; repair_log: string[];
  warnings: string[];           // what didn't run fully, e.g. a deep-check area that failed or was skipped
  skipped_areas: SkippedArea[]; // "Nothing risky found" only when the analysis is complete and this is empty
  stops: Stop[];                // guided review, in reading order, pinned by your answers
  review_summary: ReviewSummary;
}

// ---- state.json ----

export interface Draft {
  id: string; path: string; side: Side; line: number; start_line: number | null; body: string;
  start_side: Side | null;   // set only when the range starts on the other side (a deleted line → RIGHT)
  source: "user" | "suggestion" | "chat"; suggestion_id: string | null;
  origin_id?: string | null;   // the guided-review stop it was made from; its eval label follows the comment
  outside_link?: boolean;      // made by "Real problem" from a text with a link outside github.com/<owner>/<repo>
  anchored: boolean;   // false → cannot be an inline comment; goes into the review body as a permalink
  outdated: boolean;   // from an older commit (restored after `prr clean`): read-only and never sent until re-placed
  commit_id: string; created_at: string;
  posting?: string;    // set while a post is sending it: edits and deletes get 409 "being posted"
  posting_at?: string;
}

/** POST /api/drafts/{id}/replace: the new line for an outdated comment (same shape as a new comment's lines). */
export interface DraftPlace { side: Side; line: number; start_line?: number | null; start_side?: Side | null }

/** state.restored: the one-time "Restored your work from …" summary after `prr clean` + `prr fetch`. */
export interface RestoredSummary {
  archived_at: string; restored_at: string; archive: string;
  chats: number; pending_comments: number; need_replacing: number; viewed_marks: number;
  message: string;     // the banner text, from prr/archive.py message(); added by GET /api/state
}

export interface ContextChip {
  path: string; side: Side; start: number; end: number; text: string; label: string;
  stop_id?: string;  // a guided-review stop ("Ask in chat"): clicking the chip goes back to #/review/<id>
}
export interface ToolEvent { name: string; summary: string }

export interface ChatMessage {
  role: "user" | "assistant"; text: string; context: ContextChip[]; tools: ToolEvent[];
  cost_usd: number | null;       // this answer's own cost (null: unknown, or a stopped answer)
  error: string | null; ts: string;
  cost_unknown?: boolean;        // "cost unknown"
  // "(includes the earlier answer's cost)": an earlier answer whose cost wasn't known (it stopped, or its result
  // had no cost) is counted in this one
  includes_stopped?: boolean;
  session_total_usd?: number | null;
  approvals?: ApprovalRecord[];  // the turn's approval cards, shown as one line each once the turn ends
}

export interface ChatThread {
  id: string; title: string; session_id: string; messages: ChatMessage[]; created_at: string;
  long_hint_dismissed?: boolean; // "Keep going" on the long-thread hint
}

export interface SubmittedReview {
  review_id: number | null; html_url: string; event: ReviewEvent; comments: number; at: string;
  source?: "chat"; // posted by the chat instead of the Submit review button
}

/** Where a stop was when it was answered, so an answer whose stop disappears still lists as an earlier answer. */
export interface StopAnchor {
  kind: StopKind; path: string | null; side: Side | null; line: number | null; start_line: number | null;
  question: string; finding_id: string | null; bot_comment_id: number | null; check_name: string | null;
}

/** state.stop_verdicts[stopId] */
export interface StopVerdict {
  verdict: Verdict;
  at: string;                  // when it was saved (server time)
  head_sha: string;            // the analyzed head it was answered on
  anchor: StopAnchor;
  opened_at: string | null;    // when the stop was opened (sent by the app)
  answered_at: string;
}

export interface ReviewState {
  reviewed: Record<string, string>;      // path → head_blob at the time it was marked reviewed
  dismissed_suggestions: string[];
  drafts: Draft[];
  threads: ChatThread[];
  submitted: SubmittedReview[];
  restored?: RestoredSummary;
  stop_verdicts: Record<string, StopVerdict>;
  seen_hunks: Record<string, string[]>;  // path → hunk ids on screen for 3 s (hunkId in api.ts)
  review_body_draft: string;             // the Submit dialog's review summary, autosaved
  // Set when you submit a review (Submit, or the chat's post_review; not a lone chat comment): see GET /api/since-review
  reviewed_head?: string | null;         // the analyzed head you reviewed (kept as refs/pr-prism/<n>/reviewed)
  reviewed_base?: string | null;         // its merge base
  reviewed_stop_ids?: string[];          // the guided-review stops present then
}

/** POST /api/stops/{id}/verdict */
export interface VerdictResult {
  verdict: StopVerdict;
  draft: Draft | null;               // real on a finding, risk note or other tool's stop: the comment to edit
  dismissed: string | null;          // not on a finding: the finding id it dismissed
  review_body_draft: string | null;  // real on a CI stop: the review summary with the fix added
  warning?: string;                  // "Couldn't save this answer for eval" (the answer itself is saved)
}

/** DELETE /api/stops/{id}/verdict (Undo, or the first half of "Change answer") */
export interface UndoResult {
  ok: boolean;
  kept_draft: Draft | null;          // an edited "Real problem" comment that was kept: ask "Keep your comment?"
  restored: string | null;           // the finding a "Not a problem" answer had dismissed, now back
  review_body_draft: string | null;  // a CI "Real problem" taken out of the review summary: the new text
  warning?: string;
}

/** GET /api/coverage: what hasn't been checked yet (prr/coverage.py). */
export interface Coverage {
  not_checked: { stop_id: string; path: string | null; line: number | null; question: string; kind: StopKind }[];
  unsure: { stop_id: string; path: string | null; line: number | null; question: string }[];
  skipped_areas: SkippedArea[];
  high_risk_unseen: {
    hunks: number; files: number;
    by_file: Record<string, { seen: number; total: number }>;  // "2 of 5 risky hunks seen"
  };
  low_confidence: number;            // "Low confidence, not checked: N"
  running: boolean;                  // "Analysis still running; this list may grow"
  count: number;                     // the warning: not_checked.length + skipped_areas.length
}

export interface ExistingComment {
  id: number; path: string; line: number | null; side: Side | null; start_line: number | null;
  body: string; user: string; created_at: string; html_url: string; in_reply_to_id: number | null;
  review_id?: number | null;             // the GitHub review it belongs to
}

export interface ReviewPreview {
  payload: Record<string, unknown>;      // exact JSON that will be POSTed to GitHub
  inline_count: number;
  unanchored: Draft[];                   // folded into the body as permalinks
  analyzed_head: string; current_head: string; head_changed: boolean;
  viewer: string; is_author: boolean;
}

// SSE events from POST /api/chat/threads/{id}/messages
export type ChatEvent =
  | { type: "text"; text: string }
  | { type: "thinking" }
  | { type: "tool"; name: string; summary: string }
  | {
      type: "result"; session_id: string; duration_ms: number | null;
      cost_usd: number | null; cost_unknown: boolean; includes_stopped: boolean; session_total_usd: number | null;
      thread: ThreadCost;
    }
  | { type: "error"; message: string }
  | ({ type: "approval" } & ApprovalRequest)
  | { type: "approval_done"; id: string; decision: ApprovalDecision; why: ApprovalWhy }
  | { type: "done" };

export type ApprovalDecision = "allow" | "always" | "deny";
// answered: by the reviewer; covered: by an "Always allow" on another card ("Allowed by your earlier choice");
// timeout: nobody answered in time ("Timed out · Claude was told no"); closed: the chat ended while it waited
export type ApprovalWhy = "answered" | "covered" | "timeout" | "closed";

/** Why a card asks (prr/policy.py). */
export interface ApprovalReason {
  kind: "outside_read" | "outside_author" | "marked_turn" | "env" | "action";
  text: string;     // "Outside this PR's code", "Outside author (@x)", "This comment may include …. Post it?"
  detail?: string;  // what it touches: "~/code/billing", "a Jira, Slack or other lookup"
}

/** What marked the turn, on guard cards: "Earlier this turn Claude read: …" / "Earlier this turn you approved: …". */
export interface ApprovalCause { kind: "lookup" | "outside_read" | "approved"; text: string }

/** One approval card, from the server's `approval_card` (prr/server.py). */
export interface ApprovalRequest {
  id: string; tool: string;
  title: string;            // "Read a file", "Run a command", "Post a comment on GitHub"
  summary: string;          // what it touches: "~/code/billing/config.py", "npm test", "src/a.py:42"
  detail: string;           // the raw input: the command for Bash, else JSON
  reason: ApprovalReason;
  causes: ApprovalCause[];  // filled only on guard cards
  guard: boolean;           // stands between outside data and a way out: only Deny and Post / Allow once
  body: string | null;      // a post's Markdown (post cards only)
  always: boolean;          // may offer "Always allow" (never on guard cards)
  always_text: string;      // the button's label, e.g. "Always allow reads in ~/code"; "" when not offered
  always_title: string;     // its tooltip
  always_label: string;
}

/** A card once its turn ends, saved with the answer: "Allowed: Read ~/code/billing/config.py". */
export interface ApprovalRecord {
  id: string; tool: string; title: string; summary: string;
  decision: ApprovalDecision | null; why: ApprovalWhy | null;
}

/** GET /api/trust: is the PR's author a member or collaborator? unknown = the check failed (treated as outside). */
export interface Trust {
  author: string; association: string | null; trusted: boolean; state: "trusted" | "outside" | "unknown";
}

// ---- GET /api/cost (every total comes from the PR's calls.jsonl) ----

export interface ThreadCost {
  total_usd: number;   // the thread so far
  turns: number;       // answers in the thread
  unknown: boolean;    // part of its cost isn't counted (yet): show "≥"
  long: boolean;       // 15+ answers or $3+: the "start a fresh thread" hint may show
}

export interface CostRun {
  run_id: string; started_at: string | null; cost_usd: number; calls: number;
  unknown: boolean; running: boolean;
}

export interface CostSummary {
  total_usd: number;                                         // analysis + chat + other
  unknown: boolean;                                          // some cost is unknown: show "≥ $X"
  running: boolean;                                          // the analysis is still running: show "so far"
  breakdown: { analysis: number; chat: number; other: number };
  runs: CostRun[];                                           // analysis runs, oldest first (killed runs included)
  threads: Record<string, ThreadCost>;                       // by thread id
  warn_usd: number;                                          // [budget] warn_usd; 0 = off
  over_budget: boolean;                                      // total_usd > warn_usd: "over your $N limit"
  long_thread: { turns: number; usd: number };               // the hint's thresholds (15, 3.0)
}

// ---- since your review (GET /api/since-review, GET /api/head, POST /api/reanalyze; prr/since.py) ----

/** One hunk of the interdiff: the commit you reviewed → the analyzed head. */
export interface InterdiffHunk { old_start: number; old_lines: number; new_start: number; new_lines: number }
export interface InterdiffFile { path: string; hunks: InterdiffHunk[] }

/** fixed / still-open: the re-check on the new head (note says why); line-unchanged: the new commits didn't touch
 * it; checking: a re-read is running ("Checking…"); not-checked: over recheck_max ("N not checked (limit; raise
 * `recheck_max`)"), or not re-checked on this commit yet. */
export type EarlierCommentStatus = "fixed" | "still-open" | "line-unchanged" | "checking" | "not-checked";

/** One of your earlier comments on the PR. `id` is its GitHub id (ExistingComment.id); `line` is null when outdated. */
export interface EarlierComment {
  id: number; path: string; line: number | null; status: EarlierCommentStatus; note: string | null;
}

export interface SinceReview {
  reviewed_head: string | null;  // the head of your last submitted review; null: none yet
  head: string;                  // the analyzed head
  files: InterdiffFile[];        // [] with no reviewed head or the same head: hide the toggle
  patch: Record<string, string>; // path → that file's interdiff as a unified diff (like GET /api/diff's patch)
  note: string | null;           // "main moved; some changes may come from main", the reviewed commit is gone, or
                                 // "Couldn't compute changes since your review: …"
  ok: boolean;                   // false: git failed; show the note and disable the toggle
  full_pr: boolean;              // the reviewed commit is gone: files and patch are the whole PR
  comments: EarlierComment[];
  comments_error: string | null; // GitHub couldn't list the PR's comments
  new_stop_ids: string[];        // guided-review stops whose lines the new commits touched ("Only new stops")
}

/** The last complete analysis run's time and cost (calls.jsonl): "about 6 min and $2, based on the last run". */
export interface RunEstimate { minutes: number; usd: number }

/** GET /api/head: the PR's head on GitHub (read at most once a minute) against the analyzed head. */
export interface HeadStatus {
  analyzed_head: string;
  current_head: string | null;   // null when GitHub couldn't be reached (see error)
  head_changed: boolean;         // "New commits since you opened this"
  running: boolean;              // an analysis (or a re-read) is running
  estimate: RunEstimate | null;  // null: no run has completed yet
  error: string | null;
}

/** POST /api/reanalyze ("Re-read the PR"); 409 while an analysis runs. */
export interface ReanalyzeResult { started: boolean; estimate: RunEstimate | null }
