// Small builders for component tests: an analysis with one changed file, drafts, findings, cost and chat data.
import { useStore } from "../store";
import type {
  Analysis, ChatMessage, ChatThread, CostSummary, Draft, FileEntry, Finding, ReviewPreview, ReviewState,
} from "../types";

export const PATH = "src/core.py";

/** One hunk: old lines 1-4, new lines 1-5. Change keys: N1, D2, I2, I3, N3, N4. */
export const PATCH = [
  `diff --git a/${PATH} b/${PATH}`,
  "index 1111111..2222222 100644",
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -1,4 +1,5 @@",
  " import os",
  "-x = 1",
  "+x = 2",
  "+y = 3",
  " print(x)",
  " print(os)",
  "",
].join("\n");

export function makeFile(over: Partial<FileEntry> = {}): FileEntry {
  return {
    path: PATH, old_path: null, status: "modified", additions: 2, deletions: 1, binary: false,
    category: "core", generated: false, sensitive: false, minutes: 3, base_blob: "b1", head_blob: "b2",
    tests_for: null, tested_by: [], is_test: false, is_docs: false, kind_reason: null,
    hunks: [{ id: "h1", old_start: 1, old_lines: 4, new_start: 1, new_lines: 5, header: "@@ -1,4 +1,5 @@" }],
    annotated: true, risk: "low", summary: null, look_for: [], notes: [], changed_symbols: [],
    ...over,
  };
}

export function makeFinding(over: Partial<Finding> = {}): Finding {
  return {
    id: "f1", path: PATH, side: "RIGHT", line: 3, start_line: null, anchored: false, anchor: "",
    severity: "major", category: "correctness", title: "y is never used", impact: "", fix: "",
    suggestion: null, one_click: false, why: "", evidence: "", confidence: 70, judge_score: null, judge_reason: null,
    low_confidence: false, second_look: "judge", source: "claude", note: null, status: "new", also_flagged_by: [],
    comment_body: "y is never used",
    ...over,
  };
}

export function makeAnalysis(over: Partial<Analysis> = {}): Analysis {
  return {
    schema_version: 1, status: "complete", focus: null,
    pr: {
      owner: "o", repo: "r", number: 7, title: "Make x bigger", url: "https://github.com/o/r/pull/7", author: "a",
      body: "", state: "OPEN", is_draft: false, base_ref: "main", head_ref: "feature", base_sha: "base",
      merge_base_sha: "base", head_sha: "h", additions: 2, deletions: 1, changed_files: 1, labels: [], commits: [],
    },
    summary: null, files: [makeFile()], chapters: [], flows: [], impact: [], questions: [], findings: [],
    bot_findings: [], ci: null, rechecked: [],
    progress: {
      status: "complete", clusters_total: 1, clusters_done: 1, clusters_failed: 0, message: "", started_at: null,
      finished_at: null, log: [], cost_usd: 0,
    },
    repair_log: [],
    ...over,
  };
}

export function makeDraft(over: Partial<Draft> = {}): Draft {
  return {
    id: "d1", path: PATH, side: "RIGHT", line: 2, start_line: null, start_side: null, body: "Why 2?",
    source: "user", suggestion_id: null, origin_id: null, anchored: true, outdated: false, commit_id: "h",
    created_at: "2026-10-22T10:00:00+00:00",
    ...over,
  };
}

export function makeState(over: Partial<ReviewState> = {}): ReviewState {
  return { reviewed: {}, dismissed_suggestions: [], drafts: [], threads: [], submitted: [], ...over };
}

export function makeCost(over: Partial<CostSummary> = {}): CostSummary {
  return {
    total_usd: 12.4, unknown: false, running: false, breakdown: { analysis: 10.2, chat: 2, other: 0.2 },
    runs: [], threads: {}, warn_usd: 0, over_budget: false, long_thread: { turns: 15, usd: 3 },
    ...over,
  };
}

export function ask(text: string, over: Partial<ChatMessage> = {}): ChatMessage {
  return { role: "user", text, context: [], tools: [], cost_usd: null, error: null, ts: "t", ...over };
}

export function answer(text: string, over: Partial<ChatMessage> = {}): ChatMessage {
  return {
    role: "assistant", text, context: [], tools: [], cost_usd: null, error: null, ts: "t", cost_unknown: false,
    includes_stopped: false, session_total_usd: null, ...over,
  };
}

export function makeThread(over: Partial<ChatThread> = {}): ChatThread {
  return { id: "t1", title: "Review chat", session_id: "s1", messages: [], created_at: "t", ...over };
}

export function makePreview(over: Partial<ReviewPreview> = {}): ReviewPreview {
  return {
    payload: {}, inline_count: 1, unanchored: [], analyzed_head: "h", current_head: "h", head_changed: false,
    viewer: "me", is_author: false, ...over,
  };
}

const initial = useStore.getState();

/** A fresh store for each test: the initial state and actions, plus what the test needs. */
export function resetStore(over: Partial<ReturnType<typeof useStore.getState>> = {}) {
  useStore.setState({ ...initial, ...over }, true);
}
