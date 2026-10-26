// Understand this PR (prr/understand.py, docs/api.md "Understand"): the walk's order, progress and words. Pure
// helpers, shared by the Understand tab, the Overview's plan box, the Understand view and the Review view's link.
import { useCallback, useEffect, useMemo, useState } from "react";
import { explainKey, fileMap, readLS, splitPath, useStore, writeLS } from "./store";
import type { ContextChip, Side, StepExplanation, StepHunk, Stop, UnderstandLevel, UnderstandStep } from "./types";

export const LEVEL_TEXT: Record<UnderstandLevel, string> = {
  new: "I'm new to this area", familiar: "I know this area",
};
export const GROUP_TITLE = "Other changes in this chapter";
export const WHY_MISSING = "Not explained in the PR. Ask the author, or ask in the chat.";

/** "~N min to read": about a minute per step. */
export const readMinutes = (steps: UnderstandStep[]) => steps.length;

export interface StepChapter {
  id: string; title: string; goal: string | null;
  steps: UnderstandStep[];
  number: number;   // 1-based, in reading order ("Chapter 2 of 5")
}

export interface UnderstandModel {
  steps: UnderstandStep[];        // in reading order: what Got it, j and k walk through
  chapters: StepChapter[];
  understood: Record<string, string>;
  current: UnderstandStep | null; // the step in the route (null on the end screen, or an id that isn't a step)
  index: number;                  // its place in `steps` (-1 when none)
}

/** Steps grouped by chapter, in the order the chapters first appear. */
export function chaptersOf(steps: UnderstandStep[]): StepChapter[] {
  const out: StepChapter[] = [];
  const byId = new Map<string, StepChapter>();
  for (const s of steps) {
    let ch = byId.get(s.chapter_id);
    if (!ch) {
      ch = { id: s.chapter_id, title: s.chapter_title, goal: s.chapter_goal, steps: [], number: out.length + 1 };
      byId.set(s.chapter_id, ch);
      out.push(ch);
    }
    ch.steps.push(s);
  }
  return out;
}

const sortedCache = new WeakMap<UnderstandStep[], UnderstandStep[]>();
function inOrder(steps: UnderstandStep[]): UnderstandStep[] {
  let sorted = sortedCache.get(steps);
  if (!sorted) {
    sorted = [...steps].sort((a, b) => a.order - b.order);
    sortedCache.set(steps, sorted);
  }
  return sorted;
}

/** The steps in reading order (empty until GET /api/understand answers). */
export function useSteps(): UnderstandStep[] {
  const steps = useStore((s) => s.understand?.steps);
  return useMemo(() => (steps ? inOrder(steps) : []), [steps]);
}

export function useUnderstandModel(stepId: string | null): UnderstandModel {
  const understand = useStore((s) => s.understand);
  return useMemo(() => {
    const steps = understand ? inOrder(understand.steps) : [];
    const index = stepId ? steps.findIndex((s) => s.id === stepId) : -1;
    return {
      steps, chapters: chaptersOf(steps), understood: understand?.understood ?? {},
      current: index === -1 ? null : steps[index], index,
    };
  }, [understand, stepId]);
}

/** The first step not understood yet (null: every step is understood). */
export function firstToRead(steps: UnderstandStep[], understood: Record<string, string>): UnderstandStep | null {
  return steps.find((s) => !understood[s.id]) ?? null;
}

/** "Got it, next": the next step after `index` not understood yet; null when none is left after it (the end screen,
 * which lists the steps you skipped). */
export function nextToRead(steps: UnderstandStep[], index: number, understood: Record<string, string>): UnderstandStep | null {
  return steps.slice(index + 1).find((s) => !understood[s.id]) ?? null;
}

/** The heading: the file's path, or "Other changes in this chapter" for a group of a chapter's low-risk files. */
export function stepTitle(step: UnderstandStep): string {
  return step.kind === "group" ? GROUP_TITLE : step.path;
}

/** The step list's name for a step: the file name, or "Other changes". */
export function stepName(step: UnderstandStep): string {
  return step.kind === "group" ? "Other changes" : splitPath(step.path).base;
}

/** "part 2 of 3" for a large file split at hunk boundaries; null for a whole file or a group. */
export function partText(steps: UnderstandStep[], step: UnderstandStep): string | null {
  if (step.kind !== "part") return null;
  const parts = steps.filter((s) => s.kind === "part" && s.path === step.path);
  return parts.length > 1 ? `part ${parts.indexOf(step) + 1} of ${parts.length}` : null;
}

/** "Step 4 of 23 · Chapter 2 of 5" */
export function progressText(model: UnderstandModel, step: UnderstandStep): string {
  const ch = model.chapters.find((c) => c.id === step.chapter_id);
  const at = model.steps.indexOf(step);
  return `Step ${at + 1} of ${model.steps.length}${ch ? ` · Chapter ${ch.number} of ${model.chapters.length}` : ""}`;
}

function inHunk(h: StepHunk, side: Side, line: number): boolean {
  const [start, count] = side === "LEFT" ? [h.old_start, h.old_lines] : [h.new_start, h.new_lines];
  return count > 0 && line >= start && line < start + count;
}

/** The Review's "How this code works (step 4)": the step whose hunks hold the stop's line (a group step holds its
 * files' lines). `number` is the step's place in the walk. */
export function stepForStop(
  steps: UnderstandStep[], stop: Pick<Stop, "path" | "side" | "line">,
): { step: UnderstandStep; number: number } | null {
  if (!stop.path || stop.line == null) return null;
  const side = stop.side ?? "RIGHT";
  let at = steps.findIndex((s) => s.kind !== "group" && s.path === stop.path && s.hunks.some((h) => inHunk(h, side, stop.line!)));
  if (at === -1) at = steps.findIndex((s) => s.kind === "group" && s.files.includes(stop.path!));
  return at === -1 ? null : { step: steps[at], number: at + 1 };
}

const hunkHeader = (h: StepHunk) => `@@ -${h.old_start},${h.old_lines} +${h.new_start},${h.new_lines} @@`;

/** The step's explanation as plain text, for the chat (so it continues from what you read). */
export function explanationText(e: StepExplanation): string {
  const lines = [`Before: ${e.before}`, `What changes: ${e.change}`];
  if (e.why && !e.why_missing) lines.push(`Why: ${e.why}${e.why_source ? ` (Source: ${e.why_source})` : ""}`);
  else lines.push("Why: Not explained in the PR.");
  if (e.connects.length) lines.push("Where it connects:", ...e.connects.map((c) => `- ${c.path}:${c.line}: ${c.text}`));
  if (e.terms.length) lines.push("Terms:", ...e.terms.map((t) => `- ${t.term}: ${t.meaning}`));
  return lines.join("\n");
}

/** The chat's context chip for a step: its path, the lines its hunks cover, and in its text the hunks and the
 * explanation you read. Clicking it goes back to the step. */
export function stepChip(step: UnderstandStep, number: number | null, explanation: StepExplanation | null): ContextChip {
  const path = step.kind === "group" ? (step.files[0] ?? step.path) : step.path;
  const added = step.hunks.filter((h) => h.new_lines > 0);
  const side: Side = added.length || !step.hunks.length ? "RIGHT" : "LEFT";
  const spans = side === "RIGHT" ? added.map((h) => [h.new_start, h.new_lines]) : step.hunks.map((h) => [h.old_start, Math.max(h.old_lines, 1)]);
  const start = spans.length ? Math.min(...spans.map(([s]) => s)) : 0;
  const end = spans.length ? Math.max(...spans.map(([s, n]) => s + n - 1)) : 0;
  const parts = [`${stepTitle(step)}${step.kind === "group" ? `: ${step.files.join(", ")}` : ""}`];
  if (step.hunks.length) parts.push(`Hunks: ${step.hunks.map(hunkHeader).join(", ")}`);
  if (explanation) parts.push(explanationText(explanation));
  return {
    path, side, start, end, text: parts.join("\n"),
    label: `${number ? `Step ${number} · ` : ""}${stepName(step)}`,
    step_id: step.id,
  };
}

/** "Ask about this" (c): open the chat with the step attached, with the text shown at the current level. */
export function askAboutStep(step: UnderstandStep, number: number | null) {
  const st = useStore.getState();
  const level = st.understand?.level ?? "new";
  const e = st.explained[explainKey(step.id, level)];
  st.askClaude(stepChip(step, number, e?.status === "done" ? e.data : null));
}

// ------------------------------------------------------------------ "Viewed" files (collapsed diffs)

function readViewed(key: string | null): Record<string, string> {
  if (!key) return {};
  try {
    const v = JSON.parse(readLS(key) ?? "{}");
    return v && typeof v === "object" ? (v as Record<string, string>) : {};
  } catch {
    return {};
  }
}

/** "Viewed" on a file in the walk, like GitHub's: it collapses that file's diff. Kept in this browser, per PR, as
 * path → the file's head blob, so a file that changes in a later commit opens again. */
export function useFileViewed(path: string): [boolean, (on: boolean) => void] {
  const analysis = useStore((s) => s.analysis);
  const pr = analysis?.pr;
  const key = pr ? `prr.understand.viewed.${pr.owner}/${pr.repo}#${pr.number}` : null;
  const blob = fileMap(analysis).get(path)?.head_blob ?? "";
  const [viewed, setViewedMap] = useState<Record<string, string>>(() => readViewed(key));
  useEffect(() => setViewedMap(readViewed(key)), [key]);
  const set = useCallback(
    (on: boolean) => {
      const next = readViewed(key);   // fresh: other files' boxes may have changed it
      if (on) next[path] = blob;
      else delete next[path];
      if (key) writeLS(key, Object.keys(next).length ? JSON.stringify(next) : null);
      setViewedMap(next);
    },
    [key, path, blob],
  );
  return [path in viewed && viewed[path] === blob, set];
}
