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
export type ProgressStatus = "fetched" | "annotating" | "synthesizing" | "complete" | "failed";
export type ReviewEvent = "COMMENT" | "APPROVE" | "REQUEST_CHANGES" | "PENDING";

export interface Commit { sha: string; headline: string; author: string }

export interface PRInfo {
  owner: string; repo: string; number: number; title: string; url: string; author: string; body: string;
  state: string; is_draft: boolean; base_ref: string; head_ref: string;
  base_sha: string; merge_base_sha: string; head_sha: string;
  additions: number; deletions: number; changed_files: number; labels: string[]; commits: Commit[];
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
/** A problem found in the PR, ready to post as a review comment. */
export interface Finding {
  id: string; path: string; side: Side; line: number; start_line: number | null;
  anchored: boolean;            // on a diff line; false = shown at file level, posted inside the review summary
  anchor: string;
  severity: Severity;           // blocker = blocks merge (rule-checked), major = must fix, minor = should fix, nit = could improve
  category: string; title: string; impact: string; fix: string;
  suggestion: string | null; one_click: boolean;
  why: string; evidence: string;
  confidence: number | null; judge_score: number | null; judge_reason: string | null;
  low_confidence: boolean;      // hidden by default
  source: string;               // "claude" | "rule:<id>"
  note: string | null;
  status: "new" | "still-open";
  also_flagged_by: string[];
  comment_body: string;
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

/** An earlier finding or comment, re-checked after new commits. */
export interface Rechecked {
  kind: "finding" | "comment"; title: string; path: string; line: number | null;
  status: "fixed" | "still-open"; how: string; author: string | null; html_url: string | null;
}

export interface Progress {
  status: ProgressStatus; clusters_total: number; clusters_done: number; clusters_failed: number;
  message: string; started_at: string | null; finished_at: string | null; log: string[];
}

export interface Analysis {
  schema_version: number; status: ProgressStatus; focus: string | null; pr: PRInfo;
  summary: Summary | null; files: FileEntry[]; chapters: Chapter[]; flows: Flow[];
  impact: ImpactItem[]; questions: Question[];
  findings: Finding[]; bot_findings: BotFinding[]; ci: CIInfo | null; rechecked: Rechecked[];
  progress: Progress; repair_log: string[];
}

// ---- state.json ----

export interface Draft {
  id: string; path: string; side: Side; line: number; start_line: number | null; body: string;
  start_side: Side | null;   // set only when the range starts on the other side (a deleted line → RIGHT)
  source: "user" | "suggestion" | "chat"; suggestion_id: string | null;
  anchored: boolean;   // false → cannot be an inline comment; goes into the review body as a permalink
  outdated: boolean;   // the PR moved and this line no longer maps
  commit_id: string; created_at: string;
}

export interface ContextChip { path: string; side: Side; start: number; end: number; text: string; label: string }
export interface ToolEvent { name: string; summary: string }

export interface ChatMessage {
  role: "user" | "assistant"; text: string; context: ContextChip[]; tools: ToolEvent[];
  cost_usd: number | null; error: string | null; ts: string;
}

export interface ChatThread { id: string; title: string; session_id: string; messages: ChatMessage[]; created_at: string }

export interface SubmittedReview {
  review_id: number | null; html_url: string; event: ReviewEvent; comments: number; at: string;
  source?: "chat"; // posted by the chat instead of the Submit review button
}

export interface ReviewState {
  reviewed: Record<string, string>;      // path → head_blob at the time it was marked reviewed
  dismissed_suggestions: string[];
  drafts: Draft[];
  threads: ChatThread[];
  submitted: SubmittedReview[];
}

export interface ExistingComment {
  id: number; path: string; line: number | null; side: Side | null; start_line: number | null;
  body: string; user: string; created_at: string; html_url: string; in_reply_to_id: number | null;
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
  | { type: "result"; session_id: string; cost_usd: number | null; duration_ms: number | null }
  | { type: "error"; message: string }
  | { type: "approval"; id: string; tool: string; title: string; summary: string; detail: string; always_label: string }
  | { type: "approval_done"; id: string; decision: ApprovalDecision }
  | { type: "done" };

export type ApprovalDecision = "allow" | "always" | "deny";
