import { create } from "zustand";
import { api, type NewDraft } from "./api";
import type {
  Analysis, ChatThread, ContextChip, Draft, ExistingComment, FileEntry, ReviewState, Side,
} from "./types";

export interface Selection {
  path: string;
  side: Side;
  start: number;
  end: number;
  text: string;
  hunk: number; // hunk index (-1 in full-file view)
}

export interface Toast { id: number; kind: "info" | "error" | "success"; text: string }

interface Store {
  analysis: Analysis | null;
  analysisError: string | null;
  state: ReviewState;
  githubComments: ExistingComment[];

  selection: Selection | null;
  chips: ContextChip[];
  dismissedAutoChip: Record<string, boolean>;
  chatOpen: boolean;
  chatFocusTick: number;
  chatInput: string;
  activeThreadId: string | null;
  draftsOpen: boolean;
  submitOpen: boolean;
  helpOpen: boolean;
  composer: { path: string; side: Side; start: number; end: number; body: string } | null;
  toasts: Toast[];

  loadAnalysis: () => Promise<void>;
  loadState: () => Promise<void>;
  loadGithubComments: () => Promise<void>;
  setReviewed: (path: string, reviewed: boolean) => Promise<void>;
  addDraft: (d: NewDraft) => Promise<Draft | null>;
  updateDraft: (id: string, body: string) => Promise<void>;
  deleteDraft: (id: string) => Promise<void>;
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
  openComposer: (c: { path: string; side: Side; start: number; end: number; body?: string }) => void;
  setComposerBody: (body: string) => void;
  closeComposer: () => void;
  toast: (kind: Toast["kind"], text: string) => void;
  dropToast: (id: number) => void;
}

const emptyState: ReviewState = { reviewed: {}, dismissed_suggestions: [], drafts: [], threads: [], submitted: [] };

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

  selection: null,
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

  loadAnalysis: async () => {
    try {
      const analysis = await api.analysis();
      set({ analysis, analysisError: null });
    } catch (e) {
      set({ analysisError: (e as Error).message });
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
  updateDraft: async (id, body) => {
    try {
      const draft = await api.updateDraft(id, body);
      set((s) => ({ state: { ...s.state, drafts: s.state.drafts.map((d) => (d.id === id ? draft : d)) } }));
    } catch (e) {
      get().toast("error", (e as Error).message);
    }
  },
  deleteDraft: async (id) => {
    try {
      await api.deleteDraft(id);
      set((s) => ({ state: { ...s.state, drafts: s.state.drafts.filter((d) => d.id !== id) } }));
    } catch (e) {
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
    writeLS("prr.chatOpen", "1");
    set((s) => ({
      chatOpen: true,
      chatFocusTick: s.chatFocusTick + 1,
      chatInput: prompt ?? s.chatInput,
    }));
  },
  setChatInput: (chatInput) => set({ chatInput }),
  setChatOpen: (open) => {
    writeLS("prr.chatOpen", open ? "1" : "0");
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
  toast: (kind, text) => {
    const id = ++toastSeq;
    set((s) => ({ toasts: [...s.toasts.slice(-3), { id, kind, text }] }));
    window.setTimeout(() => get().dropToast(id), kind === "error" ? 7000 : 3500);
  },
  dropToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

// ---------- derived helpers ----------

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

export function isStaleReview(reviewed: Record<string, string>, f: FileEntry): boolean {
  const blob = reviewed[f.path];
  return blob !== undefined && blob !== "" && f.head_blob !== null && blob !== f.head_blob;
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
