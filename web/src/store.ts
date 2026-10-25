import { useMemo } from "react";
import { create } from "zustand";
import { api, ApiError, type NewDraft } from "./api";
import type {
  Analysis, ChatThread, ContextChip, CostSummary, Draft, DraftPlace, ExistingComment, FileEntry, HeadStatus,
  ReviewState, Side, SinceReview, StepExplanation, Stop, Trust, UndoResult, Understand, UnderstandLevel, Verdict,
  VerdictResult,
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

/** A step's text at one level (POST /api/understand/steps/{id}): on its way, here, or why it failed. */
export type Explained =
  | { status: "loading" }
  | { status: "done"; data: StepExplanation }
  | { status: "error"; error: string };
export const explainKey = (id: string, level: UnderstandLevel) => `${id}:${level}`;

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
  onlyNewStops: boolean;         // "Only new stops": on by default while "Changes since your review" is on (E4)
  freshStops: string[];          // stops that arrived on a later poll ("1 new stop earlier in the list"), until opened
  lastAnswer: { stopId: string; verdict: Verdict; toastId: number } | null; // what u undoes while its toast shows
  // Change answer or Undo kept an edited "Real problem" comment: "Keep your comment?" on that stop. `then` is the
  // new answer to save once you choose (Change answer), null for Undo.
  keepAsk: { stopId: string; draft: Draft; then: Verdict | null } | null;
  chatRail: boolean;             // the Review view below 1280 px: the chat is a rail that opens as an overlay
  // ---- since your review (E4) and new commits (design D12)
  sinceReview: SinceReview | null;      // GET /api/since-review; guided review's "Only new stops" reads its new_stop_ids
  sinceChoice: Record<string, boolean>; // head → "Changes since your review" on/off as you left it (localStorage)
  head: HeadStatus | null;              // GET /api/head: "New commits since you opened this", and whether a run is going
  loadSinceReview: () => Promise<void>;
  loadHead: () => Promise<void>;
  setSinceOn: (on: boolean) => void;
  reanalyze: () => Promise<void>;

  // ---- Understand this PR
  understand: Understand | null;        // GET /api/understand: the steps, the level, and the steps you understood
  understandError: string | null;
  explained: Record<string, Explained>; // explainKey(step, level) → its text
  loadUnderstand: () => Promise<void>;
  explainStep: (id: string, level: UnderstandLevel, again?: boolean) => Promise<void>;
  setUnderstandLevel: (level: UnderstandLevel) => Promise<void>;
  setUnderstood: (id: string, understood: boolean) => Promise<void>;

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
  setOnlyNewStops: (on: boolean) => void;
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
  onlyNewStops: true,
  freshStops: [],
  lastAnswer: null,
  keepAsk: null,
  chatRail: false,

  understand: null,
  understandError: null,
  explained: {},
  loadUnderstand: async () => {
    try {
      set({ understand: await api.understand(), understandError: null });
    } catch (e) {
      set({ understandError: (e as Error).message });
    }
  },
  // Made on demand and cached by the server; here each step's text is fetched once per level. `again` retries one
  // that failed (Try again, or opening a step whose fetch-ahead failed).
  explainStep: async (id, level, again = false) => {
    const key = explainKey(id, level);
    const had = get().explained[key];
    if (had && (had.status !== "error" || !again)) return;
    set((s) => ({ explained: { ...s.explained, [key]: { status: "loading" } } }));
    let next: Explained;
    try {
      next = { status: "done", data: await api.explainStep(id, level) };
    } catch (e) {
      next = { status: "error", error: (e as Error).message };
    }
    set((s) => ({ explained: { ...s.explained, [key]: next } }));
  },
  // "I'm new to this area" / "I know this area": remembered per PR; the step on screen re-fetches at the new level.
  setUnderstandLevel: async (level) => {
    const prev = get().understand;
    if (!prev || prev.level === level) return;
    set({ understand: { ...prev, level } });
    try {
      await api.setUnderstandLevel(level);
    } catch (e) {
      set((s) => ({ understand: s.understand && { ...s.understand, level: prev.level } }));
      get().toast("error", `Could not save the level: ${(e as Error).message}`);
    }
  },
  setUnderstood: async (id, understood) => {
    const prev = get().understand;
    if (!prev) return;
    const mark = (u: Understand, at: string | undefined): Understand => ({
      ...u, understood: at ? { ...u.understood, [id]: at } : withoutKey(u.understood, id),
    });
    set({ understand: mark(prev, understood ? (prev.understood[id] ?? new Date().toISOString()) : undefined) });
    try {
      await api.setUnderstood(id, understood);
    } catch (e) {
      set((s) => ({ understand: s.understand && mark(s.understand, prev.understood[id]) }));
      get().toast("error", `Could not save "Got it": ${(e as Error).message}`);
    }
  },

  sinceReview: null,
  sinceChoice: {},
  head: null,
  loadSinceReview: async () => {
    try {
      const sr = await api.sinceReview();
      const saved = readLS(sinceKey(sr.head));
      set((s) => ({
        sinceReview: sr,
        sinceChoice:
          saved === null || sr.head in s.sinceChoice ? s.sinceChoice : { ...s.sinceChoice, [sr.head]: saved === "1" },
      }));
    } catch {
      /* optional data: no toggle */
    }
  },
  loadHead: async () => {
    try {
      const wasRunning = get().head?.running;
      const head = await api.head();
      set({ head });
      if (wasRunning && !head.running) void get().loadAnalysis(); // the run ended: show what it found
    } catch {
      /* optional data: no "New commits" banner */
    }
  },
  setSinceOn: (on) => {
    const head = get().sinceReview?.head;
    if (!head) return;
    writeLS(sinceKey(head), on ? "1" : "0");
    set((s) => ({ sinceChoice: { ...s.sinceChoice, [head]: on } }));
  },
  // "Re-read the PR": the server fetches and analyzes in the background. The polls follow the run (the progress
  // banner) and the page keeps its view. A 409 means a run is already going: show its words and follow that one.
  reanalyze: async () => {
    try {
      await api.reanalyze();
    } catch (e) {
      const busy = e instanceof ApiError && e.status === 409;
      get().toast(busy ? "info" : "error", (e as Error).message);
      if (!busy) return;
    }
    set((s) => ({ head: s.head && { ...s.head, running: true } }));
    void get().loadAnalysis();
  },

  loadAnalysis: async () => {
    void get().loadCost(); // the header total moves with the analysis ("so far" while it runs)
    try {
      const prev = get().analysis;
      const next = await api.analysis();
      // While a re-read runs, the page keeps the analysis you are working with until the run ends (design D12);
      // only the progress banner follows the run.
      const keep = prev?.status === "complete" && !!get().head?.running;
      const analysis = keep ? { ...prev, progress: next.progress } : next;
      set((s) => {
        // Stops that land on a later poll (the analysis is still running) are "new" until you open them.
        const known = s.analysis ? new Set((s.analysis.stops ?? []).map((x) => x.id)) : null;
        const added = known ? (analysis.stops ?? []).filter((x) => !known.has(x.id)).map((x) => x.id) : [];
        return { analysis, analysisError: null, freshStops: added.length ? [...s.freshStops, ...added] : s.freshStops };
      });
      if (prev && prev.pr.head_sha !== analysis.pr.head_sha) {
        // A re-read brought new commits: drafts may be outdated now, and GitHub's threads moved with the code.
        void get().loadState();
        void get().loadGithubComments();
      }
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

  setOnlyNewStops: (onlyNewStops) => set({ onlyNewStops }),
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

const sinceKey = (head: string) => `prr.sinceReview.${head}`;

/** The "Changes since your review" toggle (E4, design issue 24).
 * - `shown`: you submitted a review on an older head;
 * - `ok`: false when git failed (the toggle is disabled, with the note as its tooltip);
 * - `on`: on by default, else as you left it for this head;
 * - `files`: the paths changed since your review. */
export interface SinceView { shown: boolean; ok: boolean; on: boolean; files: Set<string>; review: SinceReview | null }

const sinceFiles = new WeakMap<SinceReview, Set<string>>();

export function sinceView(sr: SinceReview | null, choice: Record<string, boolean>): SinceView {
  if (!sr || !sr.reviewed_head || sr.reviewed_head === sr.head) {
    return { shown: false, ok: false, on: false, files: new Set(), review: sr };
  }
  let files = sinceFiles.get(sr);
  if (!files) {
    files = new Set(sr.files.map((f) => f.path));
    sinceFiles.set(sr, files);
  }
  return { shown: true, ok: sr.ok, on: sr.ok && (choice[sr.head] ?? true), files, review: sr };
}

export function useSinceView(): SinceView {
  const sr = useStore((s) => s.sinceReview);
  const choice = useStore((s) => s.sinceChoice);
  return useMemo(() => sinceView(sr, choice), [sr, choice]);
}

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

/** Hidden by the "Hide …" checkboxes? A file changed since you viewed it is never hidden as "viewed". */
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
