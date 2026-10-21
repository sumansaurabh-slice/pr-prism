import { useCallback, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from "react";
import {
  Decoration, Diff, Hunk, getChangeKey, isDelete, isInsert, parseDiff, tokenize,
  type ChangeData, type FileData, type HunkData, type HunkTokens, type TokenizeOptions,
} from "react-diff-view";
import "react-diff-view/style/index.css";
import { refractor } from "refractor";
import jsx from "refractor/jsx";
import tsx from "refractor/tsx";
import toml from "refractor/toml";
import protobuf from "refractor/protobuf";
import graphql from "refractor/graphql";
import hcl from "refractor/hcl";
import docker from "refractor/docker";
import { api } from "../api";
import { registerFileNav, stepFile } from "../keyboard";
import { Markdown } from "../markdown";
import { goFile, navigate, fileHref, useNavTick } from "../router";
import { fileByPath, fmtMinutes, isStaleReview, readLS, splitPath, useStore, writeLS, type Selection } from "../store";
import type { Draft, ExistingComment, FileEntry, Note, Side, Suggestion } from "../types";
import { RiskBadge } from "./Header";

for (const lang of [jsx, tsx, toml, protobuf, graphql, hcl, docker]) refractor.register(lang);

// ------------------------------------------------------------------ helpers

const diffCache = new Map<string, string>();
const fileCache = new Map<string, string>();

const EXT_LANG: Record<string, string> = {
  js: "javascript", mjs: "javascript", cjs: "javascript", jsx: "jsx", ts: "typescript", mts: "typescript",
  cts: "typescript", tsx: "tsx", py: "python", go: "go", java: "java", kt: "kotlin", kts: "kotlin", rb: "ruby",
  rs: "rust", c: "c", h: "c", cc: "cpp", cpp: "cpp", hpp: "cpp", cs: "csharp", swift: "swift", php: "php",
  sql: "sql", sh: "bash", bash: "bash", zsh: "bash", yml: "yaml", yaml: "yaml", json: "json", md: "markdown",
  css: "css", scss: "scss", less: "less", html: "markup", xml: "markup", svg: "markup", toml: "toml",
  proto: "protobuf", graphql: "graphql", gql: "graphql", tf: "hcl", hcl: "hcl", lua: "lua", r: "r",
  pl: "perl", m: "objectivec", ini: "ini", mk: "makefile", diff: "diff",
};

function languageOf(path: string): string | null {
  const base = splitPath(path).base;
  if (/^dockerfile/i.test(base)) return "docker";
  if (/^makefile$/i.test(base)) return "makefile";
  const ext = base.includes(".") ? base.split(".").pop()!.toLowerCase() : "";
  const lang = EXT_LANG[ext];
  return lang && refractor.registered(lang) ? lang : null;
}

// react-diff-view 3 was written against refractor 2 (highlight → node[]); refractor 5 returns a root.
type HighlightOptions = Extract<TokenizeOptions, { highlight: true }>;
const refractorAdapter = {
  highlight: (value: string, language: string) => refractor.highlight(value, language).children,
} as unknown as HighlightOptions["refractor"];

interface LineIndex {
  rightKey: Map<number, string>;
  leftKey: Map<number, string>;
  rightHunk: Map<number, number>;
  leftHunk: Map<number, number>;
  content: Map<string, string>;
  keyOrder: Map<string, number>;
}

function buildIndex(hunks: HunkData[]): LineIndex {
  const idx: LineIndex = {
    rightKey: new Map(), leftKey: new Map(), rightHunk: new Map(), leftHunk: new Map(),
    content: new Map(), keyOrder: new Map(),
  };
  let n = 0;
  hunks.forEach((h, hi) => {
    for (const c of h.changes) {
      const key = getChangeKey(c);
      idx.content.set(key, c.content);
      idx.keyOrder.set(key, n++);
      if (isInsert(c)) {
        idx.rightKey.set(c.lineNumber, key);
        idx.rightHunk.set(c.lineNumber, hi);
      } else if (isDelete(c)) {
        idx.leftKey.set(c.lineNumber, key);
        idx.leftHunk.set(c.lineNumber, hi);
      } else {
        idx.rightKey.set(c.newLineNumber, key);
        idx.rightHunk.set(c.newLineNumber, hi);
        idx.leftKey.set(c.oldLineNumber, key);
        idx.leftHunk.set(c.oldLineNumber, hi);
      }
    }
  });
  return idx;
}

/** Context lines are commented on the RIGHT (new) side, deletions on the LEFT. */
function lineOf(c: ChangeData): { side: Side; line: number } {
  if (isInsert(c)) return { side: "RIGHT", line: c.lineNumber };
  if (isDelete(c)) return { side: "LEFT", line: c.lineNumber };
  return { side: "RIGHT", line: c.newLineNumber };
}

function keyFor(idx: LineIndex, side: Side, line: number): string | undefined {
  return (side === "RIGHT" ? idx.rightKey : idx.leftKey).get(line);
}

function selectionText(idx: LineIndex, sel: { side: Side; start: number; end: number }): string {
  const lines: string[] = [];
  for (let l = sel.start; l <= sel.end; l++) {
    const k = keyFor(idx, sel.side, l);
    if (k !== undefined) lines.push(idx.content.get(k) ?? "");
  }
  return lines.join("\n");
}

function rangeLabel(start: number, end: number) {
  return start === end ? `L${start}` : `L${start}–${end}`;
}

function flash(el: Element | null) {
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("flash");
  void (el as HTMLElement).offsetWidth;
  el.classList.add("flash");
}

// ------------------------------------------------------------------ widget cards

const NOTE_LABEL: Record<Note["kind"], string> = {
  explain: "Explains", risk: "Risk", flow: "Data flow", "test-gap": "Test gap",
};

function NoteCard({ note, index }: { note: Note; index: number }) {
  return (
    <div className={`card note note-${note.kind}`} id={`note-${index}`} data-note-index={index}>
      <div className="card-head">
        <span className="note-kind">{NOTE_LABEL[note.kind]}</span>
        <span className="card-sub">
          Claude · {note.side === "LEFT" ? "old " : ""}
          {rangeLabel(note.start, note.end)}
        </span>
      </div>
      <Markdown text={note.text} />
    </div>
  );
}

function SuggestionCard({ s }: { s: Suggestion }) {
  const addDraft = useStore((st) => st.addDraft);
  const dismiss = useStore((st) => st.dismissSuggestion);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(s.body);
  const accept = (text: string) =>
    void addDraft({
      path: s.path, side: s.side, line: s.line, start_line: s.start_line, body: text,
      source: "suggestion", suggestion_id: s.id,
    });
  return (
    <div className={`card suggestion sev-${s.severity}`}>
      <div className="card-head">
        <span className={`sev-badge sev-${s.severity}`}>{s.severity}</span>
        <span className="card-sub">
          Suggested comment · {s.start_line ? rangeLabel(s.start_line, s.line) : rangeLabel(s.line, s.line)}
        </span>
      </div>
      {editing ? (
        <textarea className="input comment-input" value={body} autoFocus onChange={(e) => setBody(e.target.value)} rows={5} />
      ) : (
        <Markdown text={s.body} />
      )}
      <div className="card-actions">
        {editing ? (
          <>
            <button className="btn btn-primary btn-sm" disabled={!body.trim()} onClick={() => accept(body)}>
              Save as draft
            </button>
            <button className="btn btn-sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-primary btn-sm" onClick={() => accept(s.body)}>
              Accept
            </button>
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              Edit & accept
            </button>
            <button className="btn btn-ghost btn-sm" onClick={() => void dismiss(s.id)}>
              Dismiss
            </button>
          </>
        )}
      </div>
    </div>
  );
}

export function DraftCard({ d, compact }: { d: Draft; compact?: boolean }) {
  const updateDraft = useStore((s) => s.updateDraft);
  const deleteDraft = useStore((s) => s.deleteDraft);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(d.body);
  useEffect(() => setBody(d.body), [d.body]);
  return (
    <div className={`card draft${d.outdated ? " outdated" : ""}`}>
      <div className="card-head">
        <span className="draft-label">Your draft</span>
        <span className="card-sub">
          {d.side === "LEFT" ? "old " : ""}
          {d.start_line ? rangeLabel(d.start_line, d.line) : rangeLabel(d.line, d.line)}
          {d.source !== "user" && ` · from ${d.source}`}
        </span>
        {!d.anchored && <span className="tag tag-warn" title="This line isn't part of the diff">goes in review body</span>}
        {d.outdated && <span className="tag tag-warn">outdated</span>}
        {compact && (
          <a className="card-jump" href={fileHref(d.path, d.start_line ?? d.line, d.side)}>
            jump →
          </a>
        )}
      </div>
      {editing ? (
        <textarea className="input comment-input" value={body} autoFocus rows={4} onChange={(e) => setBody(e.target.value)} />
      ) : (
        <Markdown text={d.body} />
      )}
      <div className="card-actions">
        {editing ? (
          <>
            <button
              className="btn btn-primary btn-sm"
              disabled={!body.trim()}
              onClick={() => void updateDraft(d.id, body).then(() => setEditing(false))}
            >
              Save
            </button>
            <button className="btn btn-sm" onClick={() => { setBody(d.body); setEditing(false); }}>
              Cancel
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              Edit
            </button>
            <button className="btn btn-ghost btn-sm btn-danger" onClick={() => void deleteDraft(d.id)}>
              Delete
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function GithubThread({ comments }: { comments: ExistingComment[] }) {
  return (
    <div className="card gh-thread">
      {comments.map((c) => (
        <div key={c.id} className="gh-comment">
          <div className="card-head">
            <span className="gh-user">@{c.user}</span>
            <span className="card-sub">on GitHub · {new Date(c.created_at).toLocaleDateString()}</span>
            <a className="card-jump" href={c.html_url} target="_blank" rel="noreferrer noopener">
              open ↗
            </a>
          </div>
          <Markdown text={c.body} />
        </div>
      ))}
    </div>
  );
}

function Composer() {
  const composer = useStore((s) => s.composer);
  const setBody = useStore((s) => s.setComposerBody);
  const close = useStore((s) => s.closeComposer);
  const addDraft = useStore((s) => s.addDraft);
  const setSelection = useStore((s) => s.setSelection);
  const askClaude = useStore((s) => s.askClaude);
  if (!composer) return null;
  const save = async () => {
    const d = await addDraft({
      path: composer.path, side: composer.side, line: composer.end,
      start_line: composer.start < composer.end ? composer.start : null, body: composer.body, source: "user",
    });
    if (d) {
      close();
      setSelection(null);
    }
  };
  return (
    <div className="card composer">
      <div className="card-head">
        <span className="draft-label">New comment</span>
        <span className="card-sub">
          {composer.side === "LEFT" ? "old " : ""}
          {rangeLabel(composer.start, composer.end)}
        </span>
      </div>
      <textarea
        className="input comment-input"
        autoFocus
        rows={4}
        placeholder="Leave a review comment (Markdown, ```suggestion blocks work). ⌘/Ctrl+Enter to save."
        value={composer.body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && composer.body.trim()) void save();
        }}
      />
      <div className="card-actions">
        <button className="btn btn-primary btn-sm" disabled={!composer.body.trim()} onClick={() => void save()}>
          Save draft
        </button>
        <button className="btn btn-sm" onClick={close}>
          Cancel
        </button>
        <button
          className="btn btn-ghost btn-sm"
          title="Ask Claude to word this comment"
          onClick={() =>
            askClaude(
              undefined,
              composer.body.trim()
                ? `Help me phrase this review comment for the selected code, concise and constructive:\n\n${composer.body}`
                : "Draft a review comment for the selected code. Reply with only the comment body.",
            )
          }
        >
          Ask Claude to word it
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ full file view

function FullFile({
  path, at, focusLine, inPR, selection, onSelect,
}: {
  path: string; at: "head" | "base"; focusLine: number | null; inPR: boolean;
  selection: Selection | null; onSelect: (line: number, shift: boolean, text: (s: number, e: number) => string) => void;
}) {
  const [content, setContent] = useState<string | null>(fileCache.get(`${at}:${path}`) ?? null);
  const [error, setError] = useState<string | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const navTick = useNavTick();
  useEffect(() => {
    const k = `${at}:${path}`;
    if (fileCache.has(k)) {
      setContent(fileCache.get(k)!);
      return;
    }
    setContent(null);
    setError(null);
    api.file(path, at).then(
      (r) => {
        fileCache.set(k, r.content);
        setContent(r.content);
      },
      (e) => setError((e as Error).message),
    );
  }, [path, at]);
  const lines = useMemo(() => (content === null ? [] : content.replace(/\n$/, "").split("\n")), [content]);
  useEffect(() => {
    if (!focusLine || !lines.length) return;
    const t = window.setTimeout(() => flash(ref.current?.querySelector(`[data-line="${focusLine}"]`) ?? null), 30);
    return () => window.clearTimeout(t);
  }, [focusLine, lines.length, navTick]);
  if (error) return <div className="empty">Could not load {path}: {error}</div>;
  if (content === null) return <div className="empty">Loading file…</div>;
  const side: Side = at === "base" ? "LEFT" : "RIGHT";
  const selected = (l: number) => selection && selection.path === path && selection.side === side && selection.hunk === -1 && l >= selection.start && l <= selection.end;
  const text = (s: number, e: number) => lines.slice(s - 1, e).join("\n");
  return (
    <div className="full-file" ref={ref}>
      {!inPR && <div className="banner banner-muted">This file isn’t changed in this PR — showing it at the PR head for context.</div>}
      <table className="ff-table">
        <tbody>
          {lines.map((l, i) => (
            <tr key={i} data-line={i + 1} className={selected(i + 1) ? "ff-selected" : undefined}>
              <td className="ff-num" onClick={(e) => onSelect(i + 1, e.shiftKey, text)}>
                {i + 1}
              </td>
              <td className="ff-code">{l || " "}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ main view

export function FileView({ path, line, side, full }: { path: string; line: number | null; side: Side; full: boolean }) {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const githubComments = useStore((s) => s.githubComments);
  const selection = useStore((s) => s.selection);
  const composer = useStore((s) => s.composer);
  const setSelection = useStore((s) => s.setSelection);
  const openComposer = useStore((s) => s.openComposer);
  const askClaude = useStore((s) => s.askClaude);
  const setReviewed = useStore((s) => s.setReviewed);
  const restoreSuggestion = useStore((s) => s.restoreSuggestion);
  const toast = useStore((s) => s.toast);
  const navTick = useNavTick();
  const file = fileByPath(analysis, path);
  const inPR = !!file;

  const [patch, setPatch] = useState<string | null>(diffCache.get(path) ?? null);
  const [diffError, setDiffError] = useState<string | null>(null);
  const [viewType, setViewType] = useState<"unified" | "split">(() =>
    readLS("prr.viewType") === "split" ? "split" : "unified",
  );
  const [showDismissed, setShowDismissed] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const noteCursor = useRef(-1);

  useEffect(() => {
    noteCursor.current = -1;
    if (!inPR) return;
    if (diffCache.has(path)) {
      setPatch(diffCache.get(path)!);
      return;
    }
    setPatch(null);
    setDiffError(null);
    let cancelled = false;
    api.diff(path).then(
      (r) => {
        diffCache.set(path, r.patch);
        if (!cancelled) setPatch(r.patch);
      },
      (e) => !cancelled && setDiffError((e as Error).message),
    );
    return () => {
      cancelled = true;
    };
  }, [path, inPR]);

  const fileData: FileData | null = useMemo(() => {
    if (!patch) return null;
    try {
      // "zip" pairs deletions with insertions side by side — right for split view, confusing in unified.
      return parseDiff(patch, viewType === "split" ? { nearbySequences: "zip" } : undefined)[0] ?? null;
    } catch {
      return null;
    }
  }, [patch, viewType]);
  const hunks = useMemo(() => fileData?.hunks ?? [], [fileData]);
  const index = useMemo(() => buildIndex(hunks), [hunks]);

  const tokens: HunkTokens | null = useMemo(() => {
    if (!hunks.length) return null;
    const lang = languageOf(path);
    const size = hunks.reduce((a, h) => a + h.changes.length, 0);
    if (!lang || size > 6000) return null;
    try {
      return tokenize(hunks, { highlight: true, refractor: refractorAdapter, language: lang });
    } catch {
      return null;
    }
  }, [hunks, path]);

  const showFull = !inPR || full || (file?.binary ?? false);
  const fullAt: "head" | "base" = file?.status === "deleted" ? "base" : "head";

  // ---- navigation to ?line=
  useEffect(() => {
    if (!line || showFull || !fileData) return;
    const k = keyFor(index, side, line);
    if (k === undefined) {
      // Line is outside the diff — show it in the full file instead.
      navigate(fileHref(path, line, side, true));
      return;
    }
    const hunk = (side === "RIGHT" ? index.rightHunk : index.leftHunk).get(line) ?? 0;
    setSelection({ path, side, start: line, end: line, hunk, text: index.content.get(k) ?? "" });
    const t = window.setTimeout(
      () => flash(containerRef.current?.querySelector(`[data-change-key="${k}"]`)?.closest("tr") ?? null),
      30,
    );
    return () => window.clearTimeout(t);
  }, [line, side, path, fileData, navTick, showFull]);

  // ---- selection
  const onGutterClick = useCallback(
    ({ change }: { change: ChangeData | null }, e: MouseEvent<HTMLElement>) => {
      if (!change) return;
      const { side: s, line: l } = lineOf(change);
      const hunk = (s === "RIGHT" ? index.rightHunk : index.leftHunk).get(l) ?? 0;
      const cur = useStore.getState().selection;
      let next = { path, side: s, start: l, end: l, hunk };
      if (e.shiftKey && cur && cur.path === path && cur.side === s && cur.hunk === hunk) {
        next = { ...next, start: Math.min(cur.start, l), end: Math.max(cur.end, l) };
      } else if (cur && cur.path === path && cur.side === s && cur.start === l && cur.end === l) {
        setSelection(null);
        return;
      }
      setSelection({ ...next, text: selectionText(index, next) });
    },
    [index, path, setSelection],
  );

  const onFullSelect = useCallback(
    (l: number, shift: boolean, text: (s: number, e: number) => string) => {
      const s: Side = fullAt === "base" ? "LEFT" : "RIGHT";
      const cur = useStore.getState().selection;
      let start = l;
      let end = l;
      if (shift && cur && cur.path === path && cur.hunk === -1 && cur.side === s) {
        start = Math.min(cur.start, l);
        end = Math.max(cur.end, l);
      }
      setSelection({ path, side: s, start, end, hunk: -1, text: text(start, end) });
    },
    [path, fullAt, setSelection],
  );

  const sel = selection && selection.path === path ? selection : null;
  const selectedChanges = useMemo(() => {
    if (!sel || sel.hunk === -1) return [];
    const keys: string[] = [];
    for (let l = sel.start; l <= sel.end; l++) {
      const k = keyFor(index, sel.side, l);
      if (k) keys.push(k);
    }
    return keys;
  }, [sel, index]);

  const commentOnSelection = useCallback(() => {
    const s = useStore.getState().selection;
    if (!s || s.path !== path) {
      toast("info", "Click a line number first (shift-click to extend), then press c.");
      return;
    }
    if (!inPR) {
      toast("info", "This file isn't part of the PR, so it can't take review comments.");
      return;
    }
    openComposer({ path, side: s.side, start: s.start, end: s.end });
  }, [path, inPR, openComposer, toast]);

  const askAboutSelection = () => {
    if (!sel) return;
    askClaude({
      path, side: sel.side, start: sel.start, end: sel.end, text: sel.text,
      label: `${splitPath(path).base}:${sel.start === sel.end ? sel.start : `${sel.start}-${sel.end}`}`,
    });
  };

  // ---- widgets
  const notes = file?.notes ?? [];
  const sortedNotes = useMemo(() => {
    return notes
      .map((n, i) => ({ n, i, key: keyFor(index, n.side, n.end) }))
      .sort((a, b) => (index.keyOrder.get(a.key ?? "") ?? 1e9) - (index.keyOrder.get(b.key ?? "") ?? 1e9));
  }, [notes, index]);

  const acceptedIds = useMemo(
    () => new Set(state.drafts.map((d) => d.suggestion_id).filter(Boolean) as string[]),
    [state.drafts],
  );
  const dismissed = useMemo(() => new Set(state.dismissed_suggestions), [state.dismissed_suggestions]);
  const fileSuggestions = useMemo(
    () => (analysis?.suggestions ?? []).filter((s) => s.path === path && !acceptedIds.has(s.id)),
    [analysis, path, acceptedIds],
  );
  const liveSuggestions = useMemo(() => fileSuggestions.filter((s) => !dismissed.has(s.id)), [fileSuggestions, dismissed]);
  const dismissedSuggestions = useMemo(() => fileSuggestions.filter((s) => dismissed.has(s.id)), [fileSuggestions, dismissed]);
  const fileDrafts = useMemo(() => state.drafts.filter((d) => d.path === path), [state.drafts, path]);

  const { widgets, floating } = useMemo(() => {
    const byKey = new Map<string, ReactNode[]>();
    const floating: ReactNode[] = [];
    const put = (side: Side, l: number, node: ReactNode) => {
      const k = keyFor(index, side, l);
      if (k === undefined) floating.push(node);
      else {
        const arr = byKey.get(k) ?? [];
        arr.push(node);
        byKey.set(k, arr);
      }
    };
    sortedNotes.forEach(({ n }, order) => put(n.side, n.end, <NoteCard key={`n${order}`} note={n} index={order} />));
    for (const s of liveSuggestions) put(s.side, s.line, <SuggestionCard key={`s${s.id}`} s={s} />);
    const roots = new Map<number, ExistingComment[]>();
    const ghFile = githubComments.filter((c) => c.path === path);
    for (const c of ghFile) {
      const rootId = c.in_reply_to_id ?? c.id;
      roots.set(rootId, [...(roots.get(rootId) ?? []), c]);
    }
    for (const [rootId, thread] of roots) {
      const root = thread.find((c) => c.id === rootId) ?? thread[0];
      const node = <GithubThread key={`g${rootId}`} comments={thread} />;
      if (root.line) put(root.side ?? "RIGHT", root.line, node);
      else floating.push(node);
    }
    for (const d of fileDrafts) put(d.side, d.line, <DraftCard key={`d${d.id}`} d={d} />);
    if (composer && composer.path === path && composer.start > 0 && hunks.length) {
      put(composer.side, composer.end, <Composer key="composer" />);
    }
    const widgets: Record<string, ReactNode> = {};
    for (const [k, nodes] of byKey) widgets[k] = <div className="widget-stack">{nodes}</div>;
    return { widgets, floating };
  }, [index, sortedNotes, liveSuggestions, githubComments, fileDrafts, composer, path, hunks.length]);

  // ---- keyboard registration
  useEffect(() => {
    const go = (delta: 1 | -1) => {
      const total = sortedNotes.length;
      if (!total) {
        toast("info", "No Claude notes in this file.");
        return;
      }
      noteCursor.current = (noteCursor.current + delta + total) % total;
      flash(containerRef.current?.querySelector(`[data-note-index="${noteCursor.current}"]`) ?? null);
    };
    registerFileNav({ nextNote: () => go(1), prevNote: () => go(-1), commentOnSelection });
    return () => registerFileNav(null);
  }, [sortedNotes.length, commentOnSelection, toast]);

  const markAndNext = async () => {
    if (!file) return;
    if (!(file.path in state.reviewed) || isStaleReview(state.reviewed, file)) await setReviewed(file.path, true);
    stepFile(1);
  };

  const reviewed = file ? file.path in state.reviewed : false;
  const stale = file ? isStaleReview(state.reviewed, file) : false;

  return (
    <div className="file-view" ref={containerRef}>
      <FileHeader
        path={path}
        file={file}
        reviewed={reviewed}
        stale={stale}
        showFull={showFull}
        canToggleFull={inPR && !file?.binary}
        viewType={viewType}
        onViewType={(v) => {
          setViewType(v);
          writeLS("prr.viewType", v);
        }}
        onMarkNext={() => void markAndNext()}
      />

      {dismissedSuggestions.length > 0 && (
        <div className="dismissed-bar">
          <button className="link-btn" onClick={() => setShowDismissed((v) => !v)}>
            {showDismissed ? "Hide" : "Show"} {dismissedSuggestions.length} dismissed suggestion
            {dismissedSuggestions.length > 1 ? "s" : ""}
          </button>
          {showDismissed &&
            dismissedSuggestions.map((s) => (
              <div key={s.id} className="dismissed-item">
                <span className={`sev-badge sev-${s.severity}`}>{s.severity}</span>
                <span className="dismissed-text">{s.body.slice(0, 140)}</span>
                <button className="btn btn-sm" onClick={() => void restoreSuggestion(s.id)}>
                  Restore
                </button>
              </div>
            ))}
        </div>
      )}

      {!showFull && floating.length > 0 && (
        <div className="floating-notes">
          <div className="section-label">Notes outside the visible hunks</div>
          {floating}
        </div>
      )}

      {showFull ? (
        <FullFile path={path} at={fullAt} focusLine={line} inPR={inPR} selection={selection} onSelect={onFullSelect} />
      ) : diffError ? (
        <div className="empty">Could not load the diff: {diffError}</div>
      ) : patch === null ? (
        <div className="empty">Loading diff…</div>
      ) : !hunks.length ? (
        <div className="empty">
          {file?.binary ? "Binary file — no text diff." : file?.status === "renamed" ? "Renamed without content changes." : "No text changes (mode change or empty file)."}
        </div>
      ) : (
        <div className="diff-wrap">
          <Diff
            viewType={viewType}
            diffType={fileData!.type}
            hunks={hunks}
            tokens={tokens}
            widgets={widgets}
            selectedChanges={selectedChanges}
            gutterEvents={{ onClick: onGutterClick }}
            optimizeSelection
          >
            {(hs) =>
              hs.flatMap((h) => [
                <Decoration key={`deco-${h.content}`}>
                  <span className="hunk-header">{h.content}</span>
                </Decoration>,
                <Hunk key={`hunk-${h.content}`} hunk={h} />,
              ])
            }
          </Diff>
        </div>
      )}

      {showFull && composer && composer.path === path && (
        <div className="full-composer">
          <Composer />
        </div>
      )}

      {sel && (
        <div className="selection-bar" role="toolbar">
          <span className="sel-label">
            {sel.side === "LEFT" ? "old " : ""}
            {rangeLabel(sel.start, sel.end)} selected
          </span>
          {inPR && (
            <button className="btn btn-primary btn-sm" onClick={commentOnSelection}>
              Comment <kbd>c</kbd>
            </button>
          )}
          <button className="btn btn-sm" onClick={askAboutSelection}>
            Ask Claude
          </button>
          <button className="btn btn-ghost btn-sm" onClick={() => setSelection(null)}>
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

function FileHeader({
  path, file, reviewed, stale, showFull, canToggleFull, viewType, onViewType, onMarkNext,
}: {
  path: string; file: FileEntry | undefined; reviewed: boolean; stale: boolean; showFull: boolean;
  canToggleFull: boolean; viewType: "unified" | "split"; onViewType: (v: "unified" | "split") => void;
  onMarkNext: () => void;
}) {
  const askClaude = useStore((s) => s.askClaude);
  const dismissedAuto = useStore((s) => s.dismissedAutoChip);
  const setReviewed = useStore((s) => s.setReviewed);
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  useEffect(() => setChecked({}), [path]);
  const { dir, base } = splitPath(path);
  const askFile = () => {
    if (dismissedAuto[path]) useStore.setState((s) => ({ dismissedAutoChip: { ...s.dismissedAutoChip, [path]: false } }));
    askClaude(undefined, "Walk me through this file: what changed, why, how it connects to the rest of the PR, and what I should verify.");
  };
  return (
    <div className="fv-header">
      <div className="fv-title-row">
        <h2 className="fv-path" title={path}>
          <span className="rp-dir">{dir}</span>
          {base}
        </h2>
        <div className="fv-actions">
          {canToggleFull && (
            <button className="btn btn-sm" onClick={() => navigate(fileHref(path, null, "RIGHT", !showFull))}>
              {showFull ? "Show diff" : "Full file"}
            </button>
          )}
          {!showFull && (
            <div className="seg">
              <button className={viewType === "unified" ? "on" : ""} onClick={() => onViewType("unified")}>
                Unified
              </button>
              <button className={viewType === "split" ? "on" : ""} onClick={() => onViewType("split")}>
                Split
              </button>
            </div>
          )}
          <button className="btn btn-sm" onClick={askFile}>
            Ask Claude about this file
          </button>
          {file && (
            <>
              <label className="fv-reviewed">
                <input type="checkbox" checked={reviewed} onChange={(e) => void setReviewed(path, e.target.checked)} />
                Reviewed
              </label>
              <button className="btn btn-primary btn-sm" onClick={onMarkNext} title="Mark reviewed and open the next file (j)">
                Mark reviewed & next →
              </button>
            </>
          )}
        </div>
      </div>
      {file && (
        <div className="fv-meta">
          <span className={`status-letter st-${file.status}`}>{file.status}</span>
          {file.old_path && <span className="mono dim">from {file.old_path}</span>}
          <span className="pill">{file.category}</span>
          <RiskBadge risk={file.risk} />
          <span className="add">+{file.additions}</span>
          <span className="del">−{file.deletions}</span>
          <span className="dim">{fmtMinutes(file.minutes)}</span>
          {file.sensitive && <span className="tag tag-warn">sensitive path</span>}
          {file.generated && <span className="tag tag-dim">generated — skim</span>}
          {stale && <span className="tag tag-warn">changed since you reviewed</span>}
        </div>
      )}
      {file?.summary && <Markdown className="fv-summary" text={file.summary} />}
      {file && !file.annotated && !file.generated && (
        <p className="dim fv-pending">Claude hasn’t annotated this file yet.</p>
      )}
      {file && file.look_for.length > 0 && (
        <div className="look-for">
          <div className="section-label">Look for</div>
          {file.look_for.map((item, i) => (
            <label key={i} className={`look-item${checked[i] ? " checked" : ""}`}>
              <input type="checkbox" checked={!!checked[i]} onChange={(e) => setChecked((c) => ({ ...c, [i]: e.target.checked }))} />
              <Markdown text={item} className="inline-md" />
            </label>
          ))}
        </div>
      )}
      {file && (file.tests_for || file.tested_by.length > 0) && (
        <div className="fv-links">
          {file.tests_for && (
            <span>
              Tests for <a href={fileHref(file.tests_for)} className="mono">{file.tests_for}</a>
            </span>
          )}
          {file.tested_by.length > 0 && (
            <span>
              Tested by{" "}
              {file.tested_by.map((t, i) => (
                <span key={t}>
                  {i > 0 && ", "}
                  <a href={fileHref(t)} className="mono" onClick={(e) => { e.preventDefault(); goFile(t); }}>
                    {splitPath(t).base}
                  </a>
                </span>
              ))}
            </span>
          )}
        </div>
      )}
    </div>
  );
}
