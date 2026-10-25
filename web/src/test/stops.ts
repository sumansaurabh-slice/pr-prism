// Builders for guided-review tests: stops of each kind on the fixture file (src/core.py), answers, and an
// analysis that lists them.
import { makeAnalysis, makeFile, PATH } from "./fixtures";
import type { Analysis, Stop, StopVerdict, Verdict } from "../types";

export const stopId = (n: number) => `stop-${String(n).repeat(40)}`;

export function makeStop(over: Partial<Stop> = {}): Stop {
  return {
    id: stopId(9), kind: "finding", path: PATH, side: "RIGHT", line: 3, start_line: null,
    question: "Is `y` ever read?", scenario: [], claude_view: "Nothing reads `y`.", confidence: 70, judge_score: 7,
    second_look: "judge", severity: "major", category: "correctness", finding_id: "f1", bot_comment_id: null,
    check_name: null, comment_body: "y is never used", chapter: null, order: 9, more: false, low_confidence: false,
    sources: ["annotate"], changed_lines: 3, dropped: false,
    ...over,
  };
}

/** 1 CI, 2 a deep-check finding, 3 a risk note, 4 a look_for item; 5 past the cap; 6 low confidence. */
export const ci = makeStop({
  id: stopId(1), kind: "ci", path: null, side: null, line: null, question: "Is this failure caused by this PR? `unit-tests` fails.",
  claude_view: "The test still expects `delivered`.", confidence: null, judge_score: null, second_look: null, severity: null,
  category: null, finding_id: null, check_name: "unit-tests", comment_body: "**unit-tests** fails: update the test.", order: 0,
  changed_lines: 0, sources: ["ci"],
});
export const finding = makeStop({
  id: stopId(2), question: "Can `y` be left unused?", order: 1, scenario: ["A caller sets `y`.", "Nothing reads it."],
  sources: ["annotate", "deep-check"],
});
export const note = makeStop({
  id: stopId(3), kind: "risk_note", line: 2, question: "Is this safe? `x` changes from 1 to 2.", finding_id: null,
  severity: null, category: null, confidence: null, judge_score: null, second_look: null, comment_body: "Is `x = 2` safe?",
  claude_view: "Callers may expect 1.", order: 2,
});
export const lookFor = makeStop({
  id: stopId(4), kind: "look_for", line: 2, question: "Do callers handle `x = 2`?", finding_id: null, severity: null,
  category: null, confidence: null, judge_score: null, second_look: null, comment_body: null, claude_view: null, order: 3,
});
export const more = makeStop({ id: stopId(5), question: "Is `os` still needed?", line: 4, finding_id: "f5", order: 4, more: true });
export const low = makeStop({
  id: stopId(6), question: "Is the import order right?", line: 1, finding_id: "f6", order: 5, low_confidence: true,
  judge_score: 3,
});

export const STOPS = [ci, finding, note, lookFor, more, low];

export function reviewAnalysis(over: Partial<Analysis> = {}): Analysis {
  return makeAnalysis({
    files: [makeFile(), makeFile({ path: "docs/notes.md", is_docs: true })],
    stops: STOPS,
    review_summary: { places: 4, more_places: 1, quiet_files: 61, minutes_left: 8 },
    ...over,
  });
}

export function verdictFor(stop: Stop, verdict: Verdict): StopVerdict {
  return {
    verdict, at: "t", head_sha: "h", opened_at: null, answered_at: "t",
    anchor: {
      kind: stop.kind, path: stop.path, side: stop.side, line: stop.line, start_line: stop.start_line,
      question: stop.question, finding_id: stop.finding_id, bot_comment_id: stop.bot_comment_id, check_name: stop.check_name,
    },
  };
}
