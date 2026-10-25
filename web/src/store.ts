import { create } from "zustand";
import { api, type NewDraft } from "./api";
import type {
  Analysis, ChatThread, ContextChip, CostSummary, Draft, DraftPlace, ExistingComment, FileEntry, ReviewState, Side, Stop,
  Trust, UndoResult, Verdict, VerdictResult,
} from "./types";

/** Picked lines. `start` is on `startSide`, `end` is on `side`; they differ only for a range that starts on a
 * deleted line and ends on an added or unchanged one (GitHub allows that). */
export interface Selection {
  path: string;
  startSide: Side;
  start: number;
  side: Side;
  end: number;
  text: string;
  hunk: number; // hunk index (-1 in full-file view)
  keys: string[]; // diff change keys in the range, in diff order (empty in full-file view)
}

/** A toast's buttons, e.g. "Undo (u)" on "Marked not a problem". */
export interface ToastAction { label: string; run: () => void }
export interface Toast { id: number; kind: "info" | "error" | "success"; text: string; actions?: ToastAction[] }
export interface ToastOptions { actions?: ToastAction[]; ms?: number }

/** The "Hide …" checkboxes in the file list. They also decide which files j/k and "Mark viewed & next" visit. */
export interface ViewPrefs { hideViewed: boolean; hideTests: boolean; hideDocs: boolean }
const PREF_KEYS: Record<keyof ViewPrefs, string> = {
  hideViewed: "prr.hideReviewed", hideTests: "prr.hideTests", hideDocs: "prr.hideDocs",
};

interface Store {
  analysis: Analysis | null;
  analysisError: string | null;
  state: ReviewState;
  githubComments: ExistingComment[];
  cost: CostSummary | null;      // GET /api/cost: the header total and each chat thread's total
  trust: Trust | null;           // GET /api/trust: the outside-author notice in the chat and the header

  selection: Selection | null;
  // Re-place pick mode for an outdated comment: the next line picked in this file's diff becomes its new line.
  replacing: { draftId: string; path: string } | null;
  replaceErrors: Record<string, string>; // draft id → why its last re-place failed (shown on the card)
  chips: ContextChip[];
  dismissedAutoChip: Record<string, boolean>;
  chatOpen: boolean;
  chatFocusTick: number;
  chatInput: string;
  activeThreadId: string | null;
  draftsOpen: boolean;
  submitOpen: boolean;
  helpOpen: boolean;
  composer: { path: string; startSide: Side; side: Side; start: number; end: number; body: string } | null;
  toasts: Toast[];
  prefs: ViewPrefs;

  // ---- guided review (the Review view)
  includeLow: boolean;           // "Include low-confidence findings" (the Review view's own switch)
  freshStops: string[];          // stops that arrived on a later poll ("1 new stop earlier in the list"), until opened
  lastAnswer: { stopId: string; verdict: Verdict; toastId: number } | null; // what u undoes while its toast shows
  // Change answer or Undo kept an edited "Real problem" comment: "Keep your comment?" on that stop. `then` is the
  // new answer to save once you choose (Change answer), null for Undo.
  keepAsk: { stopId: string; draft: Draft; then: Verdict | null } | null;
  chatRail: boolean;             // the Review view below 1280 px: the chat is a rail that opens as an overlay

  loadAnalysis: () => Promise<void>;
  loadState: () => Promise<void>;
  loadGithubComments: () => Promise<void>;
  loadCost: () => Promise<void>;
  loadTrust: () => Promise<void>;
  setReviewed: (path: string, reviewed: boolean) => Promise<void>;
  addDraft: (d: NewDraft) => Promise<Draft | null>;
  updateDraft: (id: string, body: string) => Promise<boolean>;
  deleteDraft: (id: string) => Promise<void>;
  startReplace: (d: Draft) => void;
  cancelReplace: () => void;
  placeDraft: (id: string, place: DraftPlace) => Promise<boolean>;
  dismissRestored: () => Promise<void>;
  dismissSuggestion: (id: string) => Promise<void>;
  restoreSuggestion: (id: string) => Promise<void>;
  setThreads: (threads: ChatThread[]) => void;
  upsertThread: (t: ChatThread) => void;

  setSelection: (s: Selection | null) => void;
  addChip: (c: ContextChip) => void;
  removeChip: (i: number) => void;
  clearChips: () => void;
  dismissAutoChip: (path: string) => void;
  askClaude: (c?: ContextChip, prompt?: string) => void;
  setChatInput: (text: string) => void;
  setChatOpen: (open: boolean) => void;
  setActiveThread: (id: string | null) => void;
  setDraftsOpen: (open: boolean) => void;
  setSubmitOpen: (open: boolean) => void;
  setHelpOpen: (open: boolean) => void;
  openComposer: (c: { path: string; startSide: Side; side: Side; start: number; end: number; body?: string }) => void;
  setComposerBody: (body: string) => void;
  closeComposer: () => void;
  toast: (kind: Toast["kind"], text: string, opts?: ToastOptions) => number;
  dropToast: (id: number) => void;
  setPref: (key: keyof ViewPrefs, value: boolean) => void;

  setIncludeLow: (on: boolean) => void;
  seenStop: (id: string) => void;
  answerStop: (stop: Stop, verdict: Verdict, openedAt?: string) => Promise<VerdictResult | null>;
  undoAnswer: (stopId: string) => Promise<UndoResult | null>;
  setLastAnswer: (a: Store["lastAnswer"]) => void;
  setKeepAsk: (k: Store["keepAsk"]) => void;
  setChatRail: (on: boolean) => void;
}

const emptyState: ReviewState = {
  reviewed: {}, dismissed_suggestions: [], drafts: [], threads: [], submitted: [], stop_verdicts: {}, seen_hunks: {},
  review_body_draft: "",
};

function readLS(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}
export function writeLS(key: string, value: string | null) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* storage unavailable */
  }
}
export { readLS };

let toastSeq = 0;

export const useStore = create<Store>((set, get) => ({
  analysis: null,
  analysisError: null,
  state: emptyState,
  githubComments: [],
  cost: null,
  trust: null,

  selection: null,
  replacing: null,
  replaceErrors: {},
  chips: [],
  dismissedAutoChip: {},
  chatOpen: readLS("prr.chatOpen") !== "0",
  chatFocusTick: 0,
  chatInput: "",
  activeThreadId: readLS("prr.activeThread"),
  draftsOpen: false,
  submitOpen: false,
  helpOpen: false,
  composer: null,
  toasts: [],
  prefs: {
    hideViewed: readLS(PREF_KEYS.hideViewed) === "1",
    hideTests: readLS(PREF_KEYS.hideTests) === "1",
    hideDocs: readLS(PREF_KEYS.hideDocs) === "1",
  },
  includeLow: readLS("prr.includeLow") === "1",
  freshStops: [],
  lastAnswer: null,
  keepAsk: null,
  chatRail: false,

  loadAnalysis: async () => {
    void get().loadCost(); // the header total moves with the analysis ("so far" while it runs)
    try {
      const analysis = await api.analysis();
      set((s) => {
        // Stops that land on a later poll (the analysis is still running) are "new" until you open them.
        const known = s.analysis ? new Set((s.analysis.stops ?? []).map((x) => x.id)) : null;
        const added = known ? (analysis.stops ?? []).filter((x) => !known.has(x.id)).map((x) => x.id) : [];
        return { analysis, analysisError: null, freshStops: added.length ? [...s.freshStops, ...added] : s.freshStops };
      });
    } catch (e) {
      set({ analysisError: (e as Error).message });
    }
  },
  loadCost: async () => {
    try {
      set({ cost: await api.cost() });
    } catch {
      /* optional data: the header just shows no total */
    }
  },
  loadTrust: async () => {
    try {
      set({ trust: await api.getTrust() });
    } catch {
      /* optional data: no notice (the server still asks before outsider lookups) */
    }
  },
  loadState: async () => {
    try {
      set({ state: await api.state() });
    } catch (e) {
      get().toast("error", `Could not load your review progress: ${(e as Error).message}`);
    }
  },
  loadGithubComments: async () => {
    try {
      const { comments } = await api.githubThreads();
      set({ githubComments: comments });
    } catch {
      /* optional data — the diff still works without it */
    }
  },
  setReviewed: async (path, reviewed) => {
    const prev = get().state.reviewed;
    const optimistic = { ...prev };
    if (reviewed) optimistic[path] = fileByPath(get().analysis, path)?.head_blob ?? "";
    else delete optimistic[path];
    set((s) => ({ state: { ...s.state, reviewed: optimistic } }));
    try {
      const res = await api.setReviewed(path, reviewed);
      set((s) => ({ state: { ...s.state, reviewed: res.reviewed } }));
    } catch (e) {
      set((s) => ({ state: { ...s.state, reviewed: prev } }));
      get().toast("error", (e as Error).message);
    }
  },
  addDraft: async (d) => {
    try {
      const draft = await api.createDraft(d);
      set((s) => ({ state: { ...s.state, drafts: [...s.state.drafts, draft] } }));
      get().toast(
        "success",
        draft.anchored ? "Comment added to your review" : "Comment added. Its line is not in the diff, so it will go in your review summary.",
      );
      return draft;
    } catch (e) {
      get().toast("error", `Could not save the comment: ${(e as Error).message}`);
      return null;
    }
  },
  // A 409 (the comment is being posted, or is outdated) shows the server's own words in an error toast.
  updateDraft: async (id, body) => {
    try {
      const draft = await api.updateDraft(id, body);
      set((s) => ({ state: { ...s.state, drafts: s.state.drafts.map((d) => (d.id === id ? draft : d)) } }));
      return true;
    } catch (e) {
      get().toast("error", (e as Error).message);
      return false;
    }
  },
  deleteDraft: async (id) => {
    try {
      const res = await api.deleteDraft(id);
      set((s) => ({
        state: { ...s.state, drafts: s.state.drafts.filter((d) => d.id !== id) },
        replacing: s.replacing?.draftId === id ? null : s.replacing,
      }));
      if (res.warning) get().toast("error", res.warning);
    } catch (e) {
      get().toast("error", (e as Error).message);
    }
  },
  startReplace: (d) =>
    set((s) => ({
      replacing: { draftId: d.id, path: d.path },
      replaceErrors: withoutKey(s.replaceErrors, d.id),
      selection: null,
      composer: null,
    })),
  cancelReplace: () => set({ replacing: null }),
  placeDraft: async (id, place) => {
    try {
      const draft = await api.replaceDraft(id, place);
      set((s) => ({
        state: { ...s.state, drafts: s.state.drafts.map((d) => (d.id === id ? draft : d)) },
        replacing: null,
        replaceErrors: withoutKey(s.replaceErrors, id),
        selection: null,
      }));
      get().toast(
        "success",
        draft.anchored ? "Comment moved. It will be sent with your review." : "Comment moved. Its line is not in the diff, so it will go in your review summary.",
      );
      return true;
    } catch (e) {
      set((s) => ({ replacing: null, replaceErrors: { ...s.replaceErrors, [id]: (e as Error).message }, selection: null }));
      return false;
    }
  },
  dismissRestored: async () => {
    const prev = get().state.restored;
    set((s) => ({ state: { ...s.state, restored: undefined } }));
    try {
      await api.dismissRestored();
    } catch (e) {
      set((s) => ({ state: { ...s.state, restored: prev } }));
      get().toast("error", (e as Error).message);
    }
  },
  dismissSuggestion: async (id) => {
    try {
      const res = await api.dismissSuggestion(id);
      set((s) => ({ state: { ...s.state, dismissed_suggestions: res.dismissed_suggestions } }));
    } catch (e) {
      get().toast("error", (e as Error).message);
    }
  },
  restoreSuggestion: async (id) => {
    try {
      const res = await api.restoreSuggestion(id);
      set((s) => ({ state: { ...s.state, dismissed_suggestions: res.dismissed_suggestions } }));
    } catch (e) {
      get().toast("error", (e as Error).message);
    }
  },
  setThreads: (threads) => set((s) => ({ state: { ...s.state, threads } })),
  upsertThread: (t) =>
    set((s) => {
      const exists = s.state.threads.some((x) => x.id === t.id);
      const threads = exists ? s.state.threads.map((x) => (x.id === t.id ? t : x)) : [...s.state.threads, t];
      return { state: { ...s.state, threads } };
    }),

  setSelection: (selection) => set({ selection }),
  addChip: (c) =>
    set((s) => {
      const dup = s.chips.some((x) => x.path === c.path && x.side === c.side && x.start === c.start && x.end === c.end);
      return dup ? s : { chips: [...s.chips, c] };
    }),
  removeChip: (i) => set((s) => ({ chips: s.chips.filter((_, j) => j !== i) })),
  clearChips: () => set({ chips: [] }),
  dismissAutoChip: (path) => set((s) => ({ dismissedAutoChip: { ...s.dismissedAutoChip, [path]: true } })),
  askClaude: (c, prompt) => {
    if (c) get().addChip(c);
    if (!get().chatRail) writeLS("prr.chatOpen", "1");
    set((s) => ({
      chatOpen: true,
      chatFocusTick: s.chatFocusTick + 1,
      chatInput: prompt ?? s.chatInput,
    }));
  },
  setChatInput: (chatInput) => set({ chatInput }),
  setChatOpen: (open) => {
    if (!get().chatRail) writeLS("prr.chatOpen", open ? "1" : "0"); // the rail's overlay doesn't change your choice
    set({ chatOpen: open });
  },
  setActiveThread: (id) => {
    writeLS("prr.activeThread", id);
    set({ activeThreadId: id });
  },
  setDraftsOpen: (draftsOpen) => set({ draftsOpen }),
  setSubmitOpen: (submitOpen) => set({ submitOpen }),
  setHelpOpen: (helpOpen) => set({ helpOpen }),
  openComposer: (c) => set({ composer: { ...c, body: c.body ?? "" } }),
  setComposerBody: (body) => set((s) => (s.composer ? { composer: { ...s.composer, body } } : s)),
  closeComposer: () => set({ composer: null }),
  setPref: (key, value) => {
    writeLS(PREF_KEYS[key], value ? "1" : "0");
    set((s) => ({ prefs: { ...s.prefs, [key]: value } }));
  },
  toast: (kind, text, opts) => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts.slice(-3), { id, kind, text, actions: opts?.actions }] }));
    window.setTimeout(() => get().dropToast(id), opts?.ms ?? (kind === "error" ? 7000 : 3500));
    return id;
  },
  dropToast: (id) =>
    set((s) => ({
      toasts: s.toasts.filter((t) => t.id !== id),
      lastAnswer: s.lastAnswer?.toastId === id ? null : s.lastAnswer,
    })),

  setIncludeLow: (on) => {
    writeLS("prr.includeLow", on ? "1" : "0");
    set({ includeLow: on });
  },
  seenStop: (id) => set((s) => (s.freshStops.includes(id) ? { freshStops: s.freshStops.filter((x) => x !== id) } : s)),
  // POST /api/stops/{id}/verdict, then what it did: the "Real problem" comment, the dismissed finding, or the CI
  // text added to the review summary.
  answerStop: async (stop, verdict, openedAt) => {
    try {
      const res = await api.answerStop(stop.id, verdict, openedAt);
      set((s) => {
        const st = s.state;
        const d = res.draft;
        const drafts = !d
          ? st.drafts
          : st.drafts.some((x) => x.id === d.id)
            ? st.drafts.map((x) => (x.id === d.id ? d : x))
            : [...st.drafts, d];
        const dismissed =
          res.dismissed && !st.dismissed_suggestions.includes(res.dismissed)
            ? [...st.dismissed_suggestions, res.dismissed]
            : st.dismissed_suggestions;
        return {
          state: {
            ...st, drafts, dismissed_suggestions: dismissed,
            stop_verdicts: { ...st.stop_verdicts, [stop.id]: res.verdict },
            review_body_draft: res.review_body_draft ?? st.review_body_draft,
          },
        };
      });
      if (res.warning) get().toast("error", res.warning);
      return res;
    } catch (e) {
      get().toast("error", `Could not save your answer: ${(e as Error).message}`);
      return null;
    }
  },
  // DELETE /api/stops/{id}/verdict: Undo, or the first half of "Change answer". An unedited "Real problem" comment
  // goes with the answer; an edited one is kept (kept_draft), so the view can ask "Keep your comment?".
  undoAnswer: async (stopId) => {
    try {
      const res = await api.undoAnswer(stopId);
      set((s) => {
        const st = s.state;
        return {
          state: {
            ...st,
            stop_verdicts: withoutKey(st.stop_verdicts, stopId),
            drafts: st.drafts.filter((d) => d.origin_id !== stopId || d.id === res.kept_draft?.id),
            dismissed_suggestions: res.restored
              ? st.dismissed_suggestions.filter((x) => x !== res.restored)
              : st.dismissed_suggestions,
            review_body_draft: res.review_body_draft ?? st.review_body_draft,
          },
          lastAnswer: s.lastAnswer?.stopId === stopId ? null : s.lastAnswer,
        };
      });
      if (res.warning) get().toast("error", res.warning);
      return res;
    } catch (e) {
      get().toast("error", `Could not take back your answer: ${(e as Error).message}`);
      return null;
    }
  },
  setLastAnswer: (lastAnswer) => set({ lastAnswer }),
  setKeepAsk: (keepAsk) => set({ keepAsk }),
  // Entering the rail hides the chat without touching your saved choice; leaving it brings that choice back.
  setChatRail: (on) => set({ chatRail: on, chatOpen: on ? false : readLS("prr.chatOpen") !== "0" }),
}));

// ---------- derived helpers ----------

function withoutKey<T>(rec: Record<string, T>, key: string): Record<string, T> {
  if (!(key in rec)) return rec;
  const rest = { ...rec };
  delete rest[key];
  return rest;
}

const fileMapCache = new WeakMap<Analysis, Map<string, FileEntry>>();

export function fileMap(analysis: Analysis | null): Map<string, FileEntry> {
  if (!analysis) return new Map();
  let m = fileMapCache.get(analysis);
  if (!m) {
    m = new Map(analysis.files.map((f) => [f.path, f]));
    fileMapCache.set(analysis, m);
  }
  return m;
}

export function fileByPath(analysis: Analysis | null, path: string): FileEntry | undefined {
  return fileMap(analysis).get(path);
}

const orderCache = new WeakMap<Analysis, string[]>();

/** Review order: chapter order, then any file the chapters missed. */
export function reviewOrder(analysis: Analysis | null): string[] {
  if (!analysis) return [];
  let order = orderCache.get(analysis);
  if (!order) {
    const seen = new Set<string>();
    order = [];
    const known = fileMap(analysis);
    for (const ch of analysis.chapters) {
      for (const f of ch.files) {
        if (known.has(f.path) && !seen.has(f.path)) {
          seen.add(f.path);
          order.push(f.path);
        }
      }
    }
    for (const f of analysis.files) if (!seen.has(f.path)) order.push(f.path);
    orderCache.set(analysis, order);
  }
  return order;
}

/** Hidden by the "Hide …" checkboxes? A file that changed after you viewed it is never hidden as "viewed". */
export function hiddenByPrefs(f: FileEntry, prefs: ViewPrefs, reviewed: Record<string, string>): boolean {
  if (prefs.hideTests && f.is_test) return true;
  if (prefs.hideDocs && f.is_docs) return true;
  return prefs.hideViewed && f.path in reviewed && !isStaleReview(reviewed, f);
}

export function isStaleReview(reviewed: Record<string, string>, f: FileEntry): boolean {
  const blob = reviewed[f.path];
  return blob !== undefined && blob !== "" && f.head_blob !== null && blob !== f.head_blob;
}

/** The summary, flows and impact may still arrive: the analysis hasn't reached the deep check (or the end) yet. */
export function summaryPending(analysis: Analysis): boolean {
  return ["fetched", "annotating", "synthesizing"].includes(analysis.status);
}

export function totalMinutes(analysis: Analysis | null): number {
  if (!analysis) return 0;
  return Math.round(analysis.files.reduce((a, f) => a + (f.minutes || 0), 0));
}

export function fmtMinutes(m: number): string {
  if (m < 60) return `~${Math.max(1, Math.round(m))} min`;
  const h = Math.floor(m / 60);
  const r = Math.round(m % 60);
  return `~${h}h${r ? ` ${r}m` : ""}`;
}

export function splitPath(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf("/");
  return i === -1 ? { dir: "", base: path } : { dir: path.slice(0, i + 1), base: path.slice(i + 1) };
}

export function chipLabel(c: ContextChip): string {
  const { base } = splitPath(c.path);
  if (!c.start) return base;
  return c.start === c.end ? `${base}:${c.start}` : `${base}:${c.start}-${c.end}`;
}
