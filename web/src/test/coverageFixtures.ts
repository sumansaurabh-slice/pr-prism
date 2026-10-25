// Builders for guided-review stops, answers and coverage (GET /api/coverage) in component tests.
import type { Coverage, Stop, StopVerdict, Verdict } from "../types";
import { PATH } from "./fixtures";

/** A stop id ("stop-" + 40 hex) from a short tag, e.g. sid("a") → "stop-aaaa…". */
export const sid = (tag: string) => `stop-${tag.repeat(40).slice(0, 40)}`;

export function makeStop(over: Partial<Stop> = {}): Stop {
  return {
    id: sid("a"), kind: "finding", path: PATH, side: "RIGHT", line: 3, start_line: null,
    question: "Is this a real problem? y is never used", scenario: [], claude_view: null, confidence: 70,
    judge_score: null, second_look: "judge", severity: "major", category: "correctness", finding_id: "f1",
    bot_comment_id: null, check_name: null, comment_body: "y is never used", chapter: null, order: 1, more: false,
    low_confidence: false, sources: ["annotate"], changed_lines: 3, dropped: false,
    ...over,
  };
}

export function makeVerdict(stop: Stop, verdict: Verdict): StopVerdict {
  return {
    verdict, at: "2026-10-25T10:00:00+00:00", head_sha: "h", opened_at: null, answered_at: "2026-10-25T10:00:00+00:00",
    anchor: {
      kind: stop.kind, path: stop.path, side: stop.side, line: stop.line, start_line: stop.start_line,
      question: stop.question, finding_id: stop.finding_id, bot_comment_id: stop.bot_comment_id,
      check_name: stop.check_name,
    },
  };
}

/** A place not checked yet, as GET /api/coverage lists it. */
export function place(tag: string, path: string | null, line: number | null, question: string): Coverage["not_checked"][number] {
  return { stop_id: sid(tag), path, line, question, kind: path ? "finding" : "ci" };
}

export function makeCoverage(over: Partial<Coverage> = {}): Coverage {
  const c: Coverage = {
    not_checked: [], unsure: [], skipped_areas: [],
    high_risk_unseen: { hunks: 0, files: 0, by_file: {} },
    low_confidence: 0, running: false, count: 0,
    ...over,
  };
  if (over.count === undefined) c.count = c.not_checked.length + c.skipped_areas.length;
  return c;
}
