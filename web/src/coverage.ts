// Coverage before submit (E5): what the reviewer hasn't checked yet. The one definition of "not checked" lives in
// prr/coverage.py (GET /api/coverage); this module only fetches it, records which hunks were on screen, and words it.
import { useEffect } from "react";
import { create } from "zustand";
import { api } from "./api";
import { navigate } from "./router";
import { splitPath, useStore } from "./store";
import type { Coverage } from "./types";

export const SEEN_AFTER_MS = 3000; // a hunk counts as seen after this long on screen
export const SEEN_SEND_MS = 1000;  // seen hunks are sent in one POST /api/seen per file, this long after the last one
const RELOAD_MS = 300;

interface CoverageStore {
  coverage: Coverage | null;
  error: string | null;
  /** GET /api/coverage. Resolves to the new coverage, or null when it failed (the old one is kept). */
  load: () => Promise<Coverage | null>;
}

let loadSeq = 0;

export const useCoverage = create<CoverageStore>((set) => ({
  coverage: null,
  error: null,
  load: async () => {
    const mine = ++loadSeq;
    try {
      const coverage = await api.coverage();
      if (mine === loadSeq) set({ coverage, error: null });
      return coverage;
    } catch (e) {
      if (mine === loadSeq) set({ error: (e as Error).message });
      return null;
    }
  },
}));

let reloadTimer: number | undefined;
function reloadSoon() {
  window.clearTimeout(reloadTimer);
  reloadTimer = window.setTimeout(() => void useCoverage.getState().load(), RELOAD_MS);
}

/** Keeps the coverage fresh while mounted: on each analysis poll and after each answer or dismiss. */
export function useCoverageSync() {
  const analysis = useStore((s) => s.analysis);
  const verdicts = useStore((s) => s.state.stop_verdicts);
  const dismissed = useStore((s) => s.state.dismissed_suggestions);
  useEffect(() => {
    if (analysis) reloadSoon();
  }, [analysis, verdicts, dismissed]);
}

// ---------- seen hunks (POST /api/seen)

const queued = new Map<string, Set<string>>(); // path → hunk ids waiting to be sent
const sent = new Map<string, Set<string>>();   // path → hunk ids sent (or being sent) since the page loaded
let sendTimer: number | undefined;

/** Already seen, sent, or waiting to be sent: no need to watch it again. */
export function isSeen(path: string, id: string): boolean {
  return (
    !!useStore.getState().state.seen_hunks?.[path]?.includes(id) || !!sent.get(path)?.has(id) || !!queued.get(path)?.has(id)
  );
}

/** A hunk was on screen for 3 s: send it with the others from the same moment (one POST per file). */
export function markHunkSeen(path: string, id: string) {
  if (isSeen(path, id)) return;
  const ids = queued.get(path) ?? new Set<string>();
  ids.add(id);
  queued.set(path, ids);
  window.clearTimeout(sendTimer);
  sendTimer = window.setTimeout(() => void sendSeen(), SEEN_SEND_MS);
}

async function sendSeen() {
  const batch = [...queued.entries()];
  queued.clear();
  await Promise.all(
    batch.map(async ([path, ids]) => {
      const hunks = [...ids];
      const done = sent.get(path) ?? new Set<string>();
      hunks.forEach((h) => done.add(h));
      sent.set(path, done);
      try {
        await api.markSeen(path, hunks);
        useStore.setState((s) => {
          const before = s.state.seen_hunks?.[path] ?? [];
          const seen_hunks = { ...s.state.seen_hunks, [path]: [...before, ...hunks.filter((h) => !before.includes(h))] };
          return { state: { ...s.state, seen_hunks } };
        });
      } catch {
        hunks.forEach((h) => done.delete(h)); // best effort: it is watched again the next time the file opens
      }
    }),
  );
  if (batch.length) reloadSoon();
}

/** Tests only: forget what this page sent. */
export function resetSeenForTests() {
  queued.clear();
  sent.clear();
  window.clearTimeout(sendTimer);
  window.clearTimeout(reloadTimer);
}

// ---------- words and places

export function plural(n: number, one: string, many: string = `${one}s`): string {
  return `${n} ${n === 1 ? one : many}`;
}

/** "2 of 5 risky hunks seen" on a high-risk file's row. */
export function riskySeenText(v: { seen: number; total: number }): string {
  return `${v.seen} of ${plural(v.total, "risky hunk")} seen`;
}

/** "High-risk code not seen: 12 hunks in 3 files" */
export function highRiskText(c: Coverage): string {
  const h = c.high_risk_unseen;
  return `High-risk code not seen: ${plural(h.hunks, "hunk")} in ${plural(h.files, "file")}`;
}

/** "app/x.py:12" → "x.py:12"; a CI check has no file. */
export function placeLabel(path: string | null, line: number | null): string {
  if (!path) return "CI";
  return `${splitPath(path).base}${line ? `:${line}` : ""}`;
}

export const stopHref = (stopId: string) => `#/review/${stopId}`;

/** The stop open in the Review view (#/review/<id>), if any. */
export function currentStopId(hash: string): string | null {
  const m = /^#\/?review\/(stop-[0-9a-f]{40})/.exec(hash);
  return m ? m[1] : null;
}

/**
 * The next unchecked place after the open stop, in the coverage's order (listed stops, then "more places"),
 * wrapping around; the first one when no stop is open.
 */
export function nextUnchecked(c: Coverage, current: string | null): string | null {
  const ids = c.not_checked.map((p) => p.stop_id);
  if (!ids.length) return null;
  if (current) {
    const i = ids.indexOf(current);
    if (i !== -1) return ids[(i + 1) % ids.length];
    // The open stop is already answered: go to the first unchecked one after it in reading order.
    const stops = useStore.getState().analysis?.stops ?? [];
    const pos = (id: string) => {
      const s = stops.find((x) => x.id === id);
      return s ? [s.more ? 1 : 0, s.order] : null;
    };
    const here = pos(current);
    if (here) {
      const after = ids.find((id) => {
        const p = pos(id);
        return p !== null && (p[0] > here[0] || (p[0] === here[0] && p[1] > here[1]));
      });
      if (after) return after;
    }
  }
  return ids[0];
}

/** The `]` key: go to the next place you haven't checked yet. */
export async function goNextUnchecked() {
  const c = await useCoverage.getState().load();
  const { toast } = useStore.getState();
  if (!c) {
    toast("error", "Couldn't check coverage");
    return;
  }
  const current = currentStopId(window.location.hash);
  const next = nextUnchecked(c, current);
  if (next && next === current) toast("info", "This is the last place you haven't checked");
  else if (next) navigate(stopHref(next));
  else if (c.count > 0) toast("info", "Nothing left to open: the rest are deep-check areas that didn't run");
  else toast("info", "You looked at every place that needed a human");
}
