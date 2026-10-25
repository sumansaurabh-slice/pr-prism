// Builders for "Understand this PR" tests: three steps in two chapters (the first on the fixture file src/core.py,
// whose one hunk is old 1-4 / new 1-5), and a step's explanation.
import { PATH } from "./fixtures";
import type { StepExplanation, Understand, UnderstandStep } from "../types";

/** A step id ("und-" + 40 hex) from a short tag, e.g. uid("a") → "und-aaaa…". */
export const uid = (tag: string) => `und-${tag.repeat(40).slice(0, 40)}`;

export function makeStep(over: Partial<UnderstandStep> = {}): UnderstandStep {
  return {
    id: uid("a"), chapter_id: "c1", chapter_title: "The data", chapter_goal: "Start with the data model.", path: PATH,
    hunks: [{ old_start: 1, old_lines: 4, new_start: 1, new_lines: 5 }], role: "Holds `x` and `y`.", tests: [],
    skim: [], order: 0, kind: "file", files: [PATH],
    ...over,
  };
}

/** 1 src/core.py (tested by tests/test_core.py; poetry.lock to skim), 2 src/util.py (chapter 1), 3 src/api.py
 * (chapter 2). */
export const step1 = makeStep({ tests: ["tests/test_core.py"], skim: ["poetry.lock"] });
export const step2 = makeStep({
  id: uid("b"), path: "src/util.py", files: ["src/util.py"], role: "Small helpers.", order: 1,
  hunks: [{ old_start: 0, old_lines: 0, new_start: 1, new_lines: 3 }],
});
export const step3 = makeStep({
  id: uid("c"), chapter_id: "c2", chapter_title: "The API", chapter_goal: "How callers see it.", path: "src/api.py",
  files: ["src/api.py"], role: null, order: 2, hunks: [{ old_start: 8, old_lines: 6, new_start: 8, new_lines: 6 }],
});
export const STEPS = [step1, step2, step3];

export function makeUnderstand(over: Partial<Understand> = {}): Understand {
  return { steps: STEPS, level: "new", understood: {}, ...over };
}

export function explanation(over: Partial<StepExplanation> = {}): StepExplanation {
  return {
    id: step1.id, level: "new",
    before: "`x` was 1, and nothing else was set.",
    change: "`x` becomes 2 and a new `y` is set to 3.",
    why: "The description asks for a bigger `x`.", why_source: "PR description", why_missing: false,
    connects: [
      { path: PATH, line: 4, text: "prints `x`" },
      { path: "src/other.py", line: 10, text: "reads `x` (outside this PR)" },
    ],
    terms: [{ term: "x", meaning: "How big the batch is." }],
    look_at: [{ side: "RIGHT", line: 3 }],
    cached: false, cost_usd: 0.08,
    ...over,
  };
}
