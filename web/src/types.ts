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
export interface Suggestion {
  id: string; path: string; side: Side; line: number; start_line: number | null; severity: Severity; body: string;
}

export interface Progress {
  status: ProgressStatus; clusters_total: number; clusters_done: number; clusters_failed: number;
  message: string; started_at: string | null; finished_at: string | null; log: string[];
}

export interface Analysis {
  schema_version: number; status: ProgressStatus; focus: string | null; pr: PRInfo;
  summary: Summary | null; files: FileEntry[]; chapters: Chapter[]; flows: Flow[];
  impact: ImpactItem[]; questions: Question[]; suggestions: Suggestion[];
  progress: Progress; repair_log: string[];
}

// ---- state.json ----

export interface Draft {
  id: string; path: string; side: Side; line: number; start_line: number | null; body: string;
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

export interface SubmittedReview { review_id: number; html_url: string; event: ReviewEvent; comments: number; at: string }

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
  | { type: "done" };
