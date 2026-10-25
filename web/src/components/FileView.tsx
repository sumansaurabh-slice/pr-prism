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
import { InlineText, Markdown } from "../markdown";
import { goFile, navigate, fileHref, useNavTick } from "../router";
import { fileByPath, fmtMinutes, isStaleReview, readLS, splitPath, useStore, writeLS, type Selection } from "../store";
import type { CIAnnotation, Draft, ExistingComment, FileEntry, Finding, Note, SecondLook, Severity, Side } from "../types";
import { Avatar, DiffStat, StatusIcon } from "../icons";
import { RiskBadge } from "./Header";

for (const lang of [jsx, tsx, toml, protobuf, graphql, hcl, docker]) refractor.register(lang);

// ------------------------------------------------------------------ helpers

export const diffCache = new Map<string, string>(); // path → patch, shared with the Review view's hunks
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

export function languageOf(path: string): string | null {
  const base = splitPath(path).base;
  if (/^dockerfile/i.test(base)) return "docker";
  if (/^makefile$/i.test(base)) return "makefile";
  const ext = base.includes(".") ? base.split(".").pop()!.toLowerCase() : "";
  const lang = EXT_LANG[ext];
  return lang && refractor.registered(lang) ? lang : null;
}

// react-diff-view 3 was written against refractor 2 (highlight → node[]); refractor 5 returns a root.
type HighlightOptions = Extract<TokenizeOptions, { highlight: true }>;
export const refractorAdapter = {
  highlight: (value: string, language: string) => refractor.highlight(value, language).children,
} as unknown as HighlightOptions["refractor"];

interface LineIndex {
  rightKey: Map<number, string>;
  leftKey: Map<number, string>;
  rightHunk: Map<number, number>;
  leftHunk: Map<number, number>;
  content: Map<string, string>;
  keyOrder: Map<string, number>;
  hunkKeys: string[][]; // change keys of each hunk, in diff order
  hunkOfKey: Map<string, number>;
  changeByKey: Map<string, ChangeData>;
}

function buildIndex(hunks: HunkData[]): LineIndex {
  const idx: LineIndex = {
    rightKey: new Map(), leftKey: new Map(), rightHunk: new Map(), leftHunk: new Map(),
    content: new Map(), keyOrder: new Map(), hunkKeys: [], hunkOfKey: new Map(), changeByKey: new Map(),
  };
  let n = 0;
  hunks.forEach((h, hi) => {
    idx.hunkKeys.push([]);
    for (const c of h.changes) {
      const key = getChangeKey(c);
      idx.content.set(key, c.content);
      idx.keyOrder.set(key, n++);
      idx.hunkKeys[hi].push(key);
      idx.hunkOfKey.set(key, hi);
      idx.changeByKey.set(key, c);
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

function keyFor(idx: LineIndex, side: Side, line: number): string | undefined {
  return (side === "RIGHT" ? idx.rightKey : idx.leftKey).get(line);
}

function oldNo(c: ChangeData): number | null {
  return isInsert(c) ? null : isDelete(c) ? c.lineNumber : c.oldLineNumber;
}
function newNo(c: ChangeData): number | null {
  return isDelete(c) ? null : isInsert(c) ? c.lineNumber : c.newLineNumber;
}

type Anchor = { startSide: Side; start: number; side: Side; end: number };

/** Where GitHub will put a comment on the lines first..last (in diff order), or why it can't be one comment. */
function anchorFor(first: ChangeData, last: ChangeData): Anchor | string {
  const [fo, fn, lo, ln] = [oldNo(first), newNo(first), oldNo(last), newNo(last)];
  if (fn !== null && ln !== null) return { startSide: "RIGHT", start: fn, side: "RIGHT", end: ln };
  if (fo !== null && lo !== null) return { startSide: "LEFT", start: fo, side: "LEFT", end: lo };
  if (fo !== null && ln !== null) return { startSide: "LEFT", start: fo, side: "RIGHT", end: ln };
  return "GitHub can't make one comment that starts on a new (green) line and ends on a deleted (red) line. Start your pick on the red line instead.";
}

const CROSS_HUNK = "A comment can only cover lines inside one block of changes (one @@ section). Pick lines in the same block.";

function rangeLabel(start: number, end: number) {
  return start === end ? `L${start}` : `L${start}–${end}`;
}

function flash(el: Element | null) {
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.remove("line-flash");
  void (el as HTMLElement).offsetWidth;
  el.classList.add("line-flash");
}

// ------------------------------------------------------------------ widget cards

const NOTE_LABEL: Record<Note["kind"], string> = {
  explain: "Explains", risk: "Possible problem", flow: "Data flow", "test-gap": "Missing test",
};
const NOTE_VARIANT: Record<Note["kind"], string> = {
  explain: "accent", risk: "danger", flow: "done", "test-gap": "attention",
};
export const SEVERITY_TEXT: Record<Severity, string> = {
  blocker: "Blocks merge", major: "Must fix", minor: "Should fix", nit: "Could improve",
};
const SEVERITY_VARIANT: Record<Severity, string> = {
  blocker: "danger", major: "severe", minor: "attention", nit: "secondary",
};

export function SeverityLabel({ severity }: { severity: Severity }) {
  return <span className={`Label Label--${SEVERITY_VARIANT[severity]}`}>{SEVERITY_TEXT[severity]}</span>;
}

// How Claude's second look ended for a finding (R3). "judge" and "carried" need no label: their score shows.
const SECOND_LOOK: Partial<Record<SecondLook, { text: string; variant: string; title: string }>> = {
  none: { text: "not second-checked", variant: "attention", title: "Claude's second look failed for this one, so it is shown without it." },
  rule: { text: "confirmed by rule", variant: "secondary", title: "A fixed code check found this, so it needs no second look." },
  pending: { text: "second look pending", variant: "secondary", title: "Claude hasn't finished its second look at this one yet." },
};

export function SecondLookLabel({ look, small }: { look: SecondLook | undefined; small?: boolean }) {
  const l = look ? SECOND_LOOK[look] : undefined;
  if (!l) return null;
  return (
    <span className={`Label Label--${l.variant}${small ? " Label--small" : ""}`} title={l.title}>
      {l.text}
    </span>
  );
}

/** "deep check" on findings the deep check found (alone, or as a twin of another pass's finding). */
export function DeepCheckLabel({ f, small }: { f: Finding; small?: boolean }) {
  if (!(f.sources ?? []).includes("deep-check")) return null;
  return (
    <span
      className={`Label Label--done${small ? " Label--small" : ""}`}
      title="Found by the deep check, which asks what could break and follows the code across files."
    >
      deep check
    </span>
  );
}

/** A deep-check finding's yes/no question (bold) and its numbered "How it could happen" steps. */
export function DeepCheckDetail({ f, collapsed }: { f: Finding; collapsed?: boolean }) {
  const steps = f.scenario ?? [];
  if (!f.question && !steps.length) return null;
  const list = (
    <ol className="finding-scenario">
      {steps.map((s, i) => (
        <li key={i}>
          <InlineText text={s} />
        </li>
      ))}
    </ol>
  );
  return (
    <div className="finding-deep">
      {f.question && (
        <p className="finding-question">
          <strong>
            <InlineText text={f.question} />
          </strong>
        </p>
      )}
      {steps.length > 0 &&
        (collapsed ? (
          <details className="finding-scenario-box">
            <summary>How it could happen</summary>
            {list}
          </details>
        ) : (
          <div className="finding-scenario-box">
            <div className="finding-scenario-title">How it could happen</div>
            {list}
          </div>
        ))}
    </div>
  );
}

const VERDICT_TEXT = { agree: "Claude agrees", disagree: "Claude disagrees", unsure: "Claude isn't sure" } as const;
const VERDICT_VARIANT = { agree: "success", disagree: "danger", unsure: "secondary" } as const;

function lineText(side: Side, start: number, end: number, startSide: Side = side) {
  if (startSide !== side) return `old L${start} to new L${end}`;
  return `${side === "LEFT" ? "old " : ""}${rangeLabel(start, end)}`;
}

function NoteCard({ note, index }: { note: Note; index: number }) {
  return (
    <div className={`review-comment note note-${note.kind}`} id={`note-${index}`} data-note-index={index}>
      <div className="rc-head">
        <Avatar name="Claude" kind="claude" />
        <strong>Claude</strong>
        <span className={`Label Label--${NOTE_VARIANT[note.kind]}`}>{NOTE_LABEL[note.kind]}</span>
        <span className="rc-meta">{lineText(note.side, note.start, note.end)}</span>
      </div>
      <div className="rc-body">
        <Markdown text={note.text} />
      </div>
    </div>
  );
}

function FindingCard({ f }: { f: Finding }) {
  const addDraft = useStore((st) => st.addDraft);
  const dismiss = useStore((st) => st.dismissSuggestion);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(f.comment_body);
  const accept = (text: string) =>
    void addDraft({
      path: f.path, side: f.side, line: f.line, start_line: f.start_line, body: text,
      source: "suggestion", suggestion_id: f.id,
    });
  const by = f.source.startsWith("rule:") ? "Blocker check" : "Claude";
  return (
    <div className={`review-comment finding sev-${f.severity}${f.low_confidence ? " low" : ""}`} data-finding={f.id}>
      <div className="rc-head">
        <Avatar name="Claude" kind="claude" />
        <SeverityLabel severity={f.severity} />
        <span className="Label Label--secondary">{f.category}</span>
        <DeepCheckLabel f={f} />
        <SecondLookLabel look={f.second_look} />
        {f.status === "still-open" && <span className="Label Label--attention" title="Also found in the previous review round">Still open</span>}
        {f.also_flagged_by.map((b) => (
          <span key={b} className="Label Label--accent" title="Another review tool flagged the same spot">
            Also flagged by {b.replace(/\[bot\]$/, "")}
          </span>
        ))}
        <span className="rc-meta">{lineText(f.side, f.start_line ?? f.line, f.line)}</span>
      </div>
      <div className="rc-body">
        {editing ? (
          <textarea className="input comment-input" value={body} autoFocus onChange={(e) => setBody(e.target.value)} rows={8} />
        ) : (
          <>
            <div className="finding-title">{f.title}</div>
            {f.impact && <Markdown className="finding-impact" text={f.impact} />}
            <DeepCheckDetail f={f} />
            {f.fix && (
              <div className="finding-fix">
                <strong>Fix:</strong> <Markdown className="inline-md" text={f.fix} />
              </div>
            )}
            {f.suggestion && (
              <div className="finding-code">
                <div className="finding-code-label">
                  {f.one_click ? "Suggested change (can be applied on GitHub with one click)" : "Suggested code"}
                </div>
                <pre>{f.suggestion}</pre>
              </div>
            )}
            {f.note && <div className="finding-note">{f.note}</div>}
            {(f.why || f.evidence) && (
              <details className="finding-why">
                <summary>Why</summary>
                {f.why && <Markdown text={f.why} />}
                {f.evidence && f.evidence !== "diff only" && (
                  <div className="finding-evidence">
                    Checked: <Markdown className="inline-md" text={f.evidence} />
                  </div>
                )}
              </details>
            )}
            <div className="finding-meta">
              {by}
              {f.confidence != null && ` · ${f.confidence}% sure`}
              {f.judge_score != null && (
                <span title={f.judge_reason ?? undefined}> · second look {f.judge_score}/10</span>
              )}
              {f.low_confidence && " · low confidence"}
            </div>
          </>
        )}
      </div>
      <div className="rc-actions">
        {editing ? (
          <>
            <button className="btn btn-sm" onClick={() => setEditing(false)}>
              Cancel
            </button>
            <button className="btn btn-primary btn-sm" disabled={!body.trim()} onClick={() => accept(body)}>
              Add to my review
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-invisible btn-sm" onClick={() => void dismiss(f.id)} title="Hide this finding">
              Dismiss
            </button>
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              Edit first
            </button>
            <button className="btn btn-primary btn-sm" onClick={() => accept(f.comment_body)}>
              Add to my review
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function CINote({ a }: { a: CIAnnotation }) {
  return (
    <div className={`review-comment ci-note ci-${a.level}`}>
      <div className="rc-head">
        <span className={`Label Label--${a.level === "failure" ? "danger" : "attention"}`}>CI</span>
        <strong>{a.check}</strong>
        <span className="rc-meta">L{a.line}{a.end_line && a.end_line !== a.line ? `–${a.end_line}` : ""}</span>
      </div>
      <div className="rc-body">
        <pre className="ci-message">{a.message}</pre>
      </div>
    </div>
  );
}

const SOURCE_TEXT: Record<Draft["source"], string> = {
  user: "", suggestion: "from Claude's suggestion", chat: "from the chat",
};

export function DraftCard({ d, compact }: { d: Draft; compact?: boolean }) {
  const updateDraft = useStore((s) => s.updateDraft);
  const deleteDraft = useStore((s) => s.deleteDraft);
  const startReplace = useStore((s) => s.startReplace);
  const setDraftsOpen = useStore((s) => s.setDraftsOpen);
  const replaceError = useStore((s) => s.replaceErrors[d.id]);
  const [editing, setEditing] = useState(false);
  const [body, setBody] = useState(d.body);
  useEffect(() => setBody(d.body), [d.body]);
  const where = lineText(d.side, d.start_line ?? d.line, d.line, d.start_side ?? d.side);
  // An outdated comment's line is on an older commit: pick its new line in this file's diff.
  const rePlace = () => {
    startReplace(d);
    if (compact) {
      setDraftsOpen(false);
      goFile(d.path);
    }
  };
  return (
    <div className={`review-comment draft${d.outdated ? " outdated" : ""}`} data-draft={d.id}>
      <div className="rc-head">
        <Avatar name="You" kind="you" />
        <strong>You</strong>
        <span className="Label Label--attention" title="Not sent to GitHub yet">
          Pending
        </span>
        <span className="rc-meta">
          {where}
          {d.outdated && " on an older commit"}
          {SOURCE_TEXT[d.source] && ` · ${SOURCE_TEXT[d.source]}`}
        </span>
        {!d.anchored && !d.outdated && (
          <span className="Label Label--secondary" title="This line is not part of the diff, so the comment goes into your review summary as a link">
            Goes in summary
          </span>
        )}
        {d.outdated && (
          <span className="Label Label--danger" title="Written on an older commit. It won't be sent until you re-place it.">
            Outdated
          </span>
        )}
        {compact &&
          (d.outdated ? (
            <a className="rc-jump" href={fileHref(d.path)}>
              Go to file
            </a>
          ) : (
            <a className="rc-jump" href={fileHref(d.path, d.start_side ? d.line : (d.start_line ?? d.line), d.side)}>
              Go to line
            </a>
          ))}
      </div>
      <div className="rc-body">
        {editing ? (
          <textarea className="input comment-input" value={body} autoFocus rows={4} onChange={(e) => setBody(e.target.value)} />
        ) : (
          <Markdown text={d.body} />
        )}
        {replaceError && (
          <div className="flash flash-error rc-error" role="alert">
            Could not move this comment: {replaceError}
          </div>
        )}
      </div>
      <div className="rc-actions">
        {d.outdated ? (
          <>
            <button className="btn btn-invisible btn-sm btn-danger" onClick={() => void deleteDraft(d.id)}>
              Delete
            </button>
            <button className="btn btn-sm" onClick={rePlace} title={`Pick a new line for the comment that was on ${where}`}>
              Re-place
            </button>
          </>
        ) : editing ? (
          <>
            <button className="btn btn-sm" onClick={() => { setBody(d.body); setEditing(false); }}>
              Cancel
            </button>
            <button
              className="btn btn-primary btn-sm"
              disabled={!body.trim()}
              onClick={() => void updateDraft(d.id, body).then((ok) => ok && setEditing(false))}
            >
              Save
            </button>
          </>
        ) : (
          <>
            <button className="btn btn-invisible btn-sm btn-danger" onClick={() => void deleteDraft(d.id)}>
              Delete
            </button>
            <button className="btn btn-sm" onClick={() => setEditing(true)}>
              Edit
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function GithubThread({ comments }: { comments: ExistingComment[] }) {
  const analysis = useStore((s) => s.analysis);
  const bot = analysis?.bot_findings.find((b) => b.comment_id === comments[0]?.id);
  return (
    <div className="review-comment gh-thread">
      {bot?.verdict && (
        <div className={`bot-verdict verdict-${bot.verdict}`}>
          <span className={`Label Label--${VERDICT_VARIANT[bot.verdict]}`}>{VERDICT_TEXT[bot.verdict]}</span>
          {bot.verdict_reason && <Markdown className="inline-md" text={bot.verdict_reason} />}
        </div>
      )}
      {comments.map((c) => (
        <div key={c.id} className="gh-comment">
          <div className="rc-head">
            <Avatar name={c.user} />
            <strong>{c.user}</strong>
            <span className="rc-meta">commented on GitHub · {new Date(c.created_at).toLocaleDateString()}</span>
            <a className="rc-jump" href={c.html_url} target="_blank" rel="noreferrer noopener">
              View on GitHub
            </a>
          </div>
          <div className="rc-body">
            <Markdown text={c.body} />
          </div>
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
      start_line: composer.startSide !== composer.side || composer.start < composer.end ? composer.start : null,
      start_side: composer.startSide !== composer.side ? composer.startSide : null,
      body: composer.body, source: "user",
    });
    if (d) {
      close();
      setSelection(null);
    }
  };
  return (
    <div className="review-comment composer">
      <div className="rc-head">
        <Avatar name="You" kind="you" />
        <strong>Add a comment</strong>
        <span className="rc-meta">on {lineText(composer.side, composer.start, composer.end, composer.startSide)}</span>
      </div>
      <div className="rc-body">
        <textarea
          className="input comment-input"
          autoFocus
          rows={4}
          placeholder="Leave a comment"
          value={composer.body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && composer.body.trim()) void save();
          }}
        />
      </div>
      <div className="rc-actions">
        <span className="rc-hint">Markdown works. Press ⌘/Ctrl + Enter to add.</span>
        <button
          className="btn btn-invisible btn-sm"
          title="Ask Claude to help you word this comment"
          onClick={() =>
            askClaude(
              undefined,
              composer.body.trim()
                ? `Help me word this review comment for the selected code. Keep it short, clear and kind:\n\n${composer.body}`
                : "Write a short review comment for the selected code. Reply with only the comment text.",
            )
          }
        >
          Ask Claude to help
        </button>
        <button className="btn btn-sm" onClick={close}>
          Cancel
        </button>
        <button className="btn btn-primary btn-sm" disabled={!composer.body.trim()} onClick={() => void save()}>
          Add review comment
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
  if (error) return <div className="blankslate">Could not load {path}: {error}</div>;
  if (content === null) return <div className="blankslate">Loading the file…</div>;
  const side: Side = at === "base" ? "LEFT" : "RIGHT";
  const selected = (l: number) => selection && selection.path === path && selection.side === side && selection.hunk === -1 && l >= selection.start && l <= selection.end;
  const text = (s: number, e: number) => lines.slice(s - 1, e).join("\n");
  return (
    <div className="full-file" ref={ref}>
      {!inPR && <div className="flash flash-muted">This file is not changed in this PR. It is shown here so you can see the code around it.</div>}
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

/** In Re-place pick mode for a comment in this file: move it to the picked lines. */
function placeOn(path: string, s: Anchor) {
  const st = useStore.getState();
  const target = st.replacing;
  if (!target || target.path !== path) return;
  const mixed = s.startSide !== s.side;
  void st
    .placeDraft(target.draftId, {
      side: s.side, line: s.end,
      start_line: mixed || s.start < s.end ? s.start : null,
      start_side: mixed ? s.startSide : null,
    })
    .then((ok) => {
      // The error is shown on the comment's card, which may be scrolled out of view.
      if (!ok) document.querySelector(`[data-draft="${target.draftId}"]`)?.scrollIntoView?.({ block: "center", behavior: "smooth" });
    });
}

/** "Click a line in the diff to move this comment there · Esc to cancel", while Re-place waits for a pick. */
function ReplaceBanner() {
  const cancel = useStore((s) => s.cancelReplace);
  return (
    <div className="replace-banner" role="status">
      <span>Click a line in the diff to move this comment there · Esc to cancel</span>
      <button className="btn btn-sm" onClick={cancel}>
        Cancel
      </button>
    </div>
  );
}

export function FileView({ path, line, side, full }: { path: string; line: number | null; side: Side; full: boolean }) {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const githubComments = useStore((s) => s.githubComments);
  const selection = useStore((s) => s.selection);
  const composer = useStore((s) => s.composer);
  const replacingHere = useStore((s) => s.replacing?.path === path);
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
    anchorKey.current = k;
    const picked = rangeSelection(k, k);
    if (typeof picked !== "string") setSelection(picked);
    const t = window.setTimeout(
      () => flash(containerRef.current?.querySelector(`[data-change-key="${k}"]`)?.closest("tr") ?? null),
      30,
    );
    return () => window.clearTimeout(t);
  }, [line, side, path, fileData, navTick, showFull]);

  // ---- selection: click a line number, then drag or shift-click to pick more lines (like GitHub)
  const anchorKey = useRef<string | null>(null); // where the current pick started
  // Set while the mouse button is held on a line number. `blocked` is why the line under the pointer can't end
  // the pick; it is shown only if the button is released there, not for every line the pointer passes.
  const drag = useRef<{ blocked: string | null } | null>(null);

  function rangeSelection(fromKey: string, toKey: string): Selection | string {
    const hunk = index.hunkOfKey.get(fromKey);
    if (hunk === undefined) return "That line is not in the diff.";
    if (index.hunkOfKey.get(toKey) !== hunk) return CROSS_HUNK;
    const keys = index.hunkKeys[hunk];
    let i = keys.indexOf(fromKey);
    let j = keys.indexOf(toKey);
    if (i > j) [i, j] = [j, i];
    const range = keys.slice(i, j + 1);
    const anchor = anchorFor(index.changeByKey.get(range[0])!, index.changeByKey.get(range[range.length - 1])!);
    if (typeof anchor === "string") return anchor;
    return { path, ...anchor, hunk, keys: range, text: range.map((k) => index.content.get(k) ?? "").join("\n") };
  }

  /** Select from the anchor to `toKey`. Returns null on success, or the reason that range isn't allowed. */
  function extendTo(toKey: string): string | null {
    if (!anchorKey.current) return "Click a line number first.";
    const picked = rangeSelection(anchorKey.current, toKey);
    if (typeof picked === "string") return picked;
    setSelection(picked);
    return null;
  }

  const onLineMouseDown = ({ change }: { change: ChangeData | null }, e: MouseEvent<HTMLElement>) => {
    if (!change || e.button !== 0) return;
    e.preventDefault(); // don't start a text selection while picking lines
    const key = getChangeKey(change);
    const cur = useStore.getState().selection;
    if (e.shiftKey && cur && cur.path === path && cur.hunk !== -1 && anchorKey.current) {
      const blocked = extendTo(key);
      if (blocked) toast("info", blocked);
      return;
    }
    anchorKey.current = key;
    drag.current = { blocked: null };
    extendTo(key);
  };

  const onLineMouseEnter = ({ change }: { change: ChangeData | null }) => {
    if (!drag.current || !change) return;
    drag.current.blocked = extendTo(getChangeKey(change));
  };

  useEffect(() => {
    const stop = () => {
      const picking = drag.current;
      const blocked = picking?.blocked;
      drag.current = null;
      if (blocked) useStore.getState().toast("info", `${blocked} The pick stays at the last line that works.`);
      // Re-place pick mode: the lines just picked (a click, or a drag) become the outdated comment's new place.
      const s = useStore.getState().selection;
      if (picking && s && s.path === path) placeOn(path, s);
    };
    window.addEventListener("mouseup", stop);
    return () => window.removeEventListener("mouseup", stop);
  }, [path]);

  const onFullSelect = useCallback(
    (l: number, shift: boolean, text: (s: number, e: number) => string) => {
      const s: Side = fullAt === "base" ? "LEFT" : "RIGHT";
      if (useStore.getState().replacing?.path === path) {
        placeOn(path, { startSide: s, start: l, side: s, end: l });
        return;
      }
      const cur = useStore.getState().selection;
      let start = l;
      let end = l;
      if (shift && cur && cur.path === path && cur.hunk === -1 && cur.side === s) {
        start = Math.min(cur.start, l);
        end = Math.max(cur.end, l);
      }
      setSelection({ path, startSide: s, side: s, start, end, hunk: -1, keys: [], text: text(start, end) });
    },
    [path, fullAt, setSelection],
  );

  const sel = selection && selection.path === path ? selection : null;
  const selectedChanges = useMemo(() => (sel && sel.hunk !== -1 ? sel.keys : []), [sel]);

  const commentOnSelection = useCallback(() => {
    const s = useStore.getState().selection;
    if (!s || s.path !== path) {
      toast("info", "First click a line number. Drag, or shift-click another number, to pick more lines. Then press c.");
      return;
    }
    if (!inPR) {
      toast("info", "This file is not part of the PR, so you can't comment on it.");
      return;
    }
    openComposer({ path, startSide: s.startSide, side: s.side, start: s.start, end: s.end });
  }, [path, inPR, openComposer, toast]);

  const askAboutSelection = () => {
    if (!sel) return;
    askClaude({
      path, side: sel.side, start: sel.start, end: sel.end, text: sel.text,
      label: `${splitPath(path).base}:${sel.startSide !== sel.side ? `old ${sel.start} to ${sel.end}` : sel.start === sel.end ? sel.start : `${sel.start}-${sel.end}`}`,
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
  const [showLow, setShowLow] = useState(false);
  const fileSuggestions = useMemo(
    () => (analysis?.findings ?? []).filter((s) => s.path === path && !acceptedIds.has(s.id)),
    [analysis, path, acceptedIds],
  );
  const lowFindings = useMemo(
    () => fileSuggestions.filter((s) => s.low_confidence && !dismissed.has(s.id)),
    [fileSuggestions, dismissed],
  );
  const liveSuggestions = useMemo(
    () => fileSuggestions.filter((s) => !dismissed.has(s.id) && (showLow || !s.low_confidence)),
    [fileSuggestions, dismissed, showLow],
  );
  const dismissedSuggestions = useMemo(() => fileSuggestions.filter((s) => dismissed.has(s.id)), [fileSuggestions, dismissed]);
  const ciNotes = useMemo(() => (analysis?.ci?.annotations ?? []).filter((a) => a.path === path), [analysis, path]);
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
    for (const s of liveSuggestions) {
      const card = <FindingCard key={`s${s.id}`} f={s} />;
      if (s.anchored) put(s.side, s.line, card);
      else floating.push(card);
    }
    for (const [i, a] of ciNotes.entries()) put("RIGHT", a.line, <CINote key={`ci${i}`} a={a} />);
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
    for (const d of fileDrafts) {
      const card = <DraftCard key={`d${d.id}`} d={d} />;
      if (d.outdated) floating.push(card); // its line is on an older commit, so it is never pinned to today's diff
      else put(d.side, d.line, card);
    }
    if (composer && composer.path === path && composer.start > 0 && hunks.length) {
      put(composer.side, composer.end, <Composer key="composer" />);
    }
    const widgets: Record<string, ReactNode> = {};
    for (const [k, nodes] of byKey) widgets[k] = <div className="widget-stack">{nodes}</div>;
    return { widgets, floating };
  }, [index, sortedNotes, liveSuggestions, ciNotes, githubComments, fileDrafts, composer, path, hunks.length]);

  // ---- keyboard registration
  useEffect(() => {
    const go = (delta: 1 | -1) => {
      const total = sortedNotes.length;
      if (!total) {
        toast("info", "Claude has no notes on this file.");
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
      {file && <FileSummary file={file} stale={stale} />}

      <div className="file-box">
        <FileHeader
          path={path}
          file={file}
          reviewed={reviewed}
          showFull={showFull}
          canToggleFull={inPR && !file?.binary}
          viewType={viewType}
          onViewType={(v) => {
            setViewType(v);
            writeLS("prr.viewType", v);
          }}
          onMarkNext={() => void markAndNext()}
        />

        {lowFindings.length > 0 && (
          <div className="dismissed-bar">
            <button className="btn-link" onClick={() => setShowLow((v) => !v)}>
              {showLow ? "Hide" : "Show"} {lowFindings.length} low-confidence finding{lowFindings.length > 1 ? "s" : ""}
            </button>{" "}
            <span className="color-muted">
              (the second look scored {lowFindings.length > 1 ? "them" : "it"} low, or Claude wasn't sure)
            </span>
          </div>
        )}
        {dismissedSuggestions.length > 0 && (
          <div className="dismissed-bar">
            <button className="btn-link" onClick={() => setShowDismissed((v) => !v)}>
              {showDismissed ? "Hide" : "Show"} {dismissedSuggestions.length} dismissed suggestion
              {dismissedSuggestions.length > 1 ? "s" : ""}
            </button>
            {showDismissed &&
              dismissedSuggestions.map((s) => (
                <div key={s.id} className="dismissed-item">
                  <SeverityLabel severity={s.severity} />
                  <span className="dismissed-text">{s.title}</span>
                  <button className="btn btn-sm" onClick={() => void restoreSuggestion(s.id)}>
                    Show again
                  </button>
                </div>
              ))}
          </div>
        )}

        {!showFull && floating.length > 0 && (
          <div className="floating-notes">
            <div className="floating-title">About this file, or about lines not shown in this diff</div>
            {floating}
          </div>
        )}

        {showFull ? (
          <FullFile path={path} at={fullAt} focusLine={line} inPR={inPR} selection={selection} onSelect={onFullSelect} />
        ) : diffError ? (
          <div className="blankslate">Could not load the changes: {diffError}</div>
        ) : patch === null ? (
          <div className="blankslate">Loading the changes…</div>
        ) : !hunks.length ? (
          <div className="blankslate">
            {file?.binary
              ? "This is a binary file, so there is no text to show."
              : file?.status === "renamed"
                ? "This file was renamed. Its content did not change."
                : "No text changed (only file permissions, or the file is empty)."}
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
              gutterEvents={{ onMouseDown: onLineMouseDown, onMouseEnter: onLineMouseEnter }}
              codeEvents={{ onMouseEnter: onLineMouseEnter }}
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
      </div>

      {file && (
        <div className="file-foot">
          <button className="btn btn-primary" onClick={() => void markAndNext()} title="Mark this file as viewed and open the next file">
            Mark as viewed and go to next file
          </button>
        </div>
      )}

      {replacingHere && <ReplaceBanner />}
      {sel && !replacingHere && (
        <div className="selection-bar" role="toolbar">
          <span className="sel-label">{lineText(sel.side, sel.start, sel.end, sel.startSide)} selected</span>
          {sel.start === sel.end && sel.startSide === sel.side && sel.hunk !== -1 && (
            <span className="sel-hint">Drag or shift-click line numbers to pick more lines</span>
          )}
          {inPR && (
            <button className="btn btn-primary btn-sm" onClick={commentOnSelection}>
              Comment <kbd>c</kbd>
            </button>
          )}
          <button className="btn btn-sm" onClick={askAboutSelection}>
            Ask Claude
          </button>
          <button className="btn btn-invisible btn-sm" onClick={() => setSelection(null)}>
            Clear
          </button>
        </div>
      )}
    </div>
  );
}

const CATEGORY_TEXT: Record<FileEntry["category"], string> = {
  schema: "Data format", types: "Types", core: "Main logic", api: "Entry point", ui: "User interface",
  config: "Settings", infra: "Build and CI", test: "Test", docs: "Docs", generated: "Generated",
};

function FileHeader({
  path, file, reviewed, showFull, canToggleFull, viewType, onViewType, onMarkNext,
}: {
  path: string; file: FileEntry | undefined; reviewed: boolean; showFull: boolean;
  canToggleFull: boolean; viewType: "unified" | "split"; onViewType: (v: "unified" | "split") => void;
  onMarkNext: () => void;
}) {
  const askClaude = useStore((s) => s.askClaude);
  const dismissedAuto = useStore((s) => s.dismissedAutoChip);
  const setReviewed = useStore((s) => s.setReviewed);
  const { dir, base } = splitPath(path);
  const askFile = () => {
    if (dismissedAuto[path]) useStore.setState((s) => ({ dismissedAutoChip: { ...s.dismissedAutoChip, [path]: false } }));
    askClaude(undefined, "Explain this file to me: what changed, why, how it connects to the rest of the PR, and what I should check.");
  };
  return (
    <div className="file-header">
      <div className="file-info">
        {file && <StatusIcon status={file.status} />}
        <h2 className="file-path" title={path}>
          {file?.old_path && (
            <>
              <span className="file-dir">{file.old_path}</span> →{" "}
            </>
          )}
          <span className="file-dir">{dir}</span>
          {base}
        </h2>
        {file && <DiffStat additions={file.additions} deletions={file.deletions} />}
      </div>
      <div className="file-actions">
        {!showFull && (
          <div className="segmented" role="group" aria-label="Diff layout">
            <button className={viewType === "unified" ? "on" : ""} onClick={() => onViewType("unified")}>
              Unified
            </button>
            <button className={viewType === "split" ? "on" : ""} onClick={() => onViewType("split")}>
              Split
            </button>
          </div>
        )}
        {canToggleFull && (
          <button className="btn btn-sm" onClick={() => navigate(fileHref(path, null, "RIGHT", !showFull))}>
            {showFull ? "Show changes" : "Show whole file"}
          </button>
        )}
        <button className="btn btn-sm" onClick={askFile}>
          Ask Claude
        </button>
        {file && (
          <>
            <label className={`viewed-toggle${reviewed ? " checked" : ""}`} title="Mark this file as viewed (r)">
              <input type="checkbox" checked={reviewed} onChange={(e) => void setReviewed(path, e.target.checked)} />
              Viewed
            </label>
            <button className="btn btn-sm" onClick={onMarkNext} title="Mark this file as viewed and open the next file">
              Mark viewed & next →
            </button>
          </>
        )}
      </div>
    </div>
  );
}

function FileSummary({ file, stale }: { file: FileEntry; stale: boolean }) {
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  useEffect(() => setChecked({}), [file.path]);
  const hasNotes = file.summary || file.look_for.length > 0;
  return (
    <section className="Box claude-box">
      <div className="Box-header claude-box-head">
        <Avatar name="Claude" kind="claude" />
        <strong>Claude's notes on this file</strong>
        <RiskBadge risk={file.risk} />
        <span className="Label Label--secondary">{CATEGORY_TEXT[file.category]}</span>
        {(file.is_test || file.is_docs) && (
          <span className="Label Label--secondary" title={file.kind_reason ?? undefined}>
            {file.is_test ? "Test file" : "Docs file"}
          </span>
        )}
        {file.sensitive && (
          <span className="Label Label--attention" title="Security, CI, secrets or packaging file">
            Needs extra care
          </span>
        )}
        {stale && <span className="Label Label--attention">Changed since you viewed it</span>}
        <span className="claude-box-time">{fmtMinutes(file.minutes)} to review</span>
      </div>
      <div className="Box-body">
        {file.summary && <Markdown className="claude-summary" text={file.summary} />}
        {!hasNotes && !file.generated && (
          <p className="color-muted">
            {!file.annotated
              ? "Claude has not read this file yet."
              : file.notes.length > 0
                ? "Claude's notes for this file are next to the lines below."
                : "Claude has nothing to add about this file."}
          </p>
        )}
        {file.generated && <p className="color-muted">A tool made this file. You only need to skim it.</p>}
        {file.look_for.length > 0 && (
          <div className="check-list">
            <div className="check-list-title">Things to check</div>
            {file.look_for.map((item, i) => (
              <label key={i} className={`check-item${checked[i] ? " checked" : ""}`}>
                <input type="checkbox" checked={!!checked[i]} onChange={(e) => setChecked((c) => ({ ...c, [i]: e.target.checked }))} />
                <Markdown text={item} className="inline-md" />
              </label>
            ))}
          </div>
        )}
        {(file.tests_for || file.tested_by.length > 0) && (
          <div className="file-links">
            {file.tests_for && (
              <span>
                Tests the code in <a href={fileHref(file.tests_for)} className="mono">{file.tests_for}</a>
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
    </section>
  );
}
