// Guided review (prr/stops.py, docs/api.md "Guided review"): which stops the Review view lists, how far you are,
// and the words it uses. Pure helpers, shared by the Review tab, the Overview's plan box and the Review view.
import { useMemo, useRef } from "react";
import { useSinceView, useStore } from "./store";
import type { Analysis, ContextChip, Draft, ReviewState, Stop, StopKind, Verdict } from "./types";

export const KIND_TEXT: Record<StopKind, string> = {
  finding: "finding", risk_note: "risk note", look_for: "check", ci: "CI", bot: "other tool",
};
export const VERDICT_TEXT: Record<Verdict, string> = {
  real: "Real problem", not: "Not a problem", unsure: "Not sure",
};

/** In the numbered list: not past the cap, not an earlier answer; a low-confidence finding only with the switch. */
export function inMainList(s: Stop, includeLow: boolean): boolean {
  return !s.more && !s.dropped && (!s.low_confidence || includeLow);
}

/** Answered, or its finding was dismissed elsewhere (that counts as done). */
export function stopDone(s: Stop, state: ReviewState): boolean {
  return !!state.stop_verdicts[s.id] || (!!s.finding_id && state.dismissed_suggestions.includes(s.finding_id));
}

/** The time estimate per stop, as the server's `minutes_left`: 2 + min(5, floor(changed lines / 20)). */
export const stopMinutes = (s: Stop) => 2 + Math.min(5, Math.floor(s.changed_lines / 20));

export interface ReviewModel {
  main: Stop[];     // numbered 1..N: what y / n / ? and j / k walk through
  more: Stop[];     // past the cap: "N more places", collapsed
  earlier: Stop[];  // earlier answers whose stop is gone or now hidden ("Second look dropped this")
  low: number;      // low-confidence findings the switch adds (or added)
  gone: Set<string>; // ids in `main` kept only because you are on them (dropped or hidden since you opened them)
}

/**
 * Sort the stops into the list. `pinned` is the stop you are on when it was in the numbered list: if a later poll
 * moves it past the cap, hides it or drops it, it stays where it was (the server can't know which stop is open).
 */
export function buildModel(
  stops: Stop[], includeLow: boolean, pinned: Stop | null = null, onlyNew: Set<string> | null = null,
): ReviewModel {
  const byOrder = (a: Stop, b: Stop) => a.order - b.order;
  const keep = (s: Stop) => !onlyNew || onlyNew.has(s.id);
  const main = stops.filter((s) => inMainList(s, includeLow) && keep(s));
  const gone = new Set<string>();
  if (pinned && !main.some((s) => s.id === pinned.id)) {
    const latest = stops.find((s) => s.id === pinned.id);
    gone.add(pinned.id);
    main.push(latest ?? { ...pinned, dropped: true });
    main.sort(byOrder);
  }
  const rest = (s: Stop) => !main.some((m) => m.id === s.id);
  return {
    main,
    more: stops.filter((s) => s.more && !s.dropped && !s.low_confidence && keep(s) && rest(s)).sort(byOrder),
    earlier: stops.filter((s) => s.dropped && rest(s)).sort(byOrder),
    low: stops.filter((s) => s.low_confidence && !s.dropped).length,
    gone,
  };
}

export interface ReviewCounts { total: number; answered: number; left: number; minutes: number }

export function reviewCounts(main: Stop[], state: ReviewState): ReviewCounts {
  const open = main.filter((s) => !stopDone(s, state));
  return {
    total: main.length,
    answered: main.length - open.length,
    left: open.length,
    minutes: open.reduce((n, s) => n + stopMinutes(s), 0),
  };
}

/** "Only new stops" (E4): while "Changes since your review" is on, only the stops whose lines changed since then.
 * null when the filter doesn't apply (the view is off, or you turned the filter off). */
export function useNewStops(): Set<string> | null {
  const since = useSinceView();
  const only = useStore((s) => s.onlyNewStops);
  const ids = useStore((s) => s.sinceReview?.new_stop_ids);
  return useMemo(() => (since.on && only && ids ? new Set(ids) : null), [since.on, only, ids]);
}

/** The numbered list without pinning: for the Review tab's "N left" and the Overview's plan box. */
export function useReviewCounts(): ReviewCounts & { places: number; quiet: number; started: boolean } {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const includeLow = useStore((s) => s.includeLow);
  const onlyNew = useNewStops();
  return useMemo(() => {
    const stops = analysis?.stops ?? [];
    const main = stops.filter((s) => inMainList(s, includeLow) && (!onlyNew || onlyNew.has(s.id)));
    return {
      ...reviewCounts(main, state),
      places: analysis?.review_summary?.places ?? main.length,
      quiet: analysis?.review_summary?.quiet_files ?? 0,
      started: stops.some((s) => !!state.stop_verdicts[s.id]),
    };
  }, [analysis, state, includeLow, onlyNew]);
}

/** Every stop seen this session, so one that vanishes while you are on it can still be shown. */
const lastSeen = new Map<string, Stop>();

/**
 * The Review view's list for the stop you are on (`currentId`), keeping that stop in place if a poll moves it
 * past the cap, hides it or drops it. Returns the stop itself too (null for an id this page never saw).
 */
export function useReviewModel(currentId: string | null): ReviewModel & { current: Stop | null; index: number } {
  const analysis = useStore((s) => s.analysis);
  const includeLow = useStore((s) => s.includeLow);
  const onlyNew = useNewStops();
  const pin = useRef<Stop | null>(null);
  return useMemo(() => {
    const stops = analysis?.stops ?? [];
    for (const s of stops) lastSeen.set(s.id, s);
    if (pin.current && pin.current.id !== currentId) pin.current = null;
    const model = buildModel(stops, includeLow, pin.current, onlyNew);
    const index = currentId ? model.main.findIndex((s) => s.id === currentId) : -1;
    if (index !== -1) pin.current = model.main[index];
    const current =
      index !== -1
        ? model.main[index]
        : (stops.find((s) => s.id === currentId) ?? (currentId ? lastSeen.get(currentId) : undefined) ?? null);
    return { ...model, current, index };
  }, [analysis, includeLow, onlyNew, currentId]);
}

/** The next stop to answer after `fromId` in list order (wrapping), skipping `skip` (the one just answered). */
export function nextOpen(main: Stop[], fromId: string | null, state: ReviewState, skip?: string): Stop | null {
  const at = fromId ? main.findIndex((s) => s.id === fromId) : -1;
  const ordered = [...main.slice(at + 1), ...main.slice(0, at + 1)];
  return ordered.find((s) => s.id !== skip && s.id !== fromId && !stopDone(s, state)) ?? null;
}

/** The comment a "Real problem" answer made (or a look_for stop's editor saved). */
export function stopDraft(stop: Stop, drafts: Draft[]): Draft | null {
  return drafts.find((d) => d.origin_id === stop.id) ?? null;
}

export function whereText(stop: Pick<Stop, "path" | "line" | "kind" | "check_name">): string {
  if (!stop.path) return stop.check_name ?? "CI check";
  const base = stop.path.slice(stop.path.lastIndexOf("/") + 1);
  return stop.line ? `${base}:${stop.line}` : base;
}

/** "70% sure · second look 7/10", "second look pending", "not second-checked", or "" for none. */
export function sureText(stop: Stop): string {
  const parts: string[] = [];
  if (stop.confidence != null) parts.push(`${stop.confidence}% sure`);
  if (stop.second_look === "pending") parts.push("second look pending");
  else if (stop.second_look === "none") parts.push("not second-checked");
  else if (stop.second_look === "rule") parts.push("confirmed by rule");
  else if (stop.judge_score != null) parts.push(`second look ${stop.judge_score}/10`);
  return parts.join(" · ");
}

/** The chat's context chip for a stop: its lines (with the code, when known) and a label that names the stop. */
export function stopChip(stop: Stop, number: number | null, code: string): ContextChip | null {
  if (!stop.path) return null;
  const end = stop.line ?? 0;
  return {
    path: stop.path, side: stop.side ?? "RIGHT", start: end ? (stop.start_line ?? end) : 0, end, text: code,
    label: `${number ? `Stop ${number} · ` : ""}${whereText(stop)}`,
    stop_id: stop.id,
  };
}

/** What to type in the chat for a stop: its question, and Claude's view as a starting point. */
export function stopPrompt(stop: Stop): string {
  const view = stop.claude_view ? `\n\nYour earlier view:\n${stop.claude_view}` : "";
  return `Help me answer this review question: ${stop.question}${view}\n\nIs it a real problem? Check the code and say why.`;
}

/** Deep check progress for "more coming (deep check 2 of 4 areas)". */
export function moreComing(analysis: Analysis): string {
  const p = analysis.progress;
  if (p.status === "deepchecking" && p.deepcheck_total) {
    return `more coming (deep check ${p.deepcheck_done} of ${p.deepcheck_total} areas)`;
  }
  return "more coming";
}

export function analysisRunning(analysis: Analysis): boolean {
  return analysis.status !== "complete" && analysis.status !== "failed";
}
