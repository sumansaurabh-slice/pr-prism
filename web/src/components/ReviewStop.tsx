// One stop of the guided review (design D6): the question, the code, "How it could happen", Claude's view (muted,
// last, so you judge the code first), and a sticky action bar with three equal answers (D18).
import { useEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import {
  Decoration, Diff, Hunk, getChangeKey, isDelete, isInsert, parseDiff, tokenize,
  type ChangeData, type FileData, type HunkData, type HunkTokens,
} from "react-diff-view";
import { api } from "../api";
import { Avatar } from "../icons";
import type { ReviewNav } from "../keyboard";
import { InlineText, Markdown } from "../markdown";
import {
  KIND_TEXT, VERDICT_TEXT, analysisRunning, moreComing, nextOpen, reviewCounts, stopChip, stopDraft, stopDone,
  stopPrompt, sureText, whereText, type ReviewModel,
} from "../review";
import { REVIEW_END, fileHref, navigate, reviewHref, understandHref } from "../router";
import { fmtMinutes, splitPath, useStore, type ToastAction } from "../store";
import type { Draft, Severity, Side, Stop, Verdict } from "../types";
import { stepForStop, useSteps } from "../understand";
import { DraftCard, FindingLabel, INTENT, SEVERITY_TEXT, diffCache, diffKey, languageOf, refractorAdapter } from "./FileView";

// ------------------------------------------------------------------ shared actions (keys, toasts, the end screen)

/** When each stop was first opened this session: sent with the answer (`opened_at`) to calibrate the estimate. */
const openedAt = new Map<string, string>();
function openedOf(id: string): string {
  if (!openedAt.has(id)) openedAt.set(id, new Date().toISOString());
  return openedAt.get(id)!;
}

function findStop(id: string): Stop | null {
  return useStore.getState().analysis?.stops.find((s) => s.id === id) ?? null;
}

/** Open the chat with the stop attached as a context chip ("Ask in chat"); `goBack` first returns to the stop. */
export function askAboutStop(stop: Stop, number: number | null, goBack = false) {
  if (goBack) navigate(reviewHref(stop.id));
  const chip = stopChip(stop, number, codeFor(stop));
  useStore.getState().askClaude(chip ?? undefined, stopPrompt(stop));
}

/** Undo (u, or the toast's button): take the answer back and return to its stop. */
export async function undoStop(stopId: string) {
  const st = useStore.getState();
  if (st.lastAnswer?.stopId === stopId) st.dropToast(st.lastAnswer.toastId);
  const res = await st.undoAnswer(stopId);
  if (!res) return;
  navigate(reviewHref(stopId));
  if (res.kept_draft) useStore.getState().setKeepAsk({ stopId, draft: res.kept_draft, then: null });
}

export function undoLast() {
  const la = useStore.getState().lastAnswer;
  if (la) void undoStop(la.stopId);
  else useStore.getState().toast("info", "Nothing to undo. Undo works while the message about your answer shows.");
}

/** "Marked not a problem · Undo (u)" or "Marked not sure · Ask in chat (c) · Undo (u)", for 5 seconds. */
function answeredToast(stop: Stop, verdict: Verdict, number: number | null) {
  const st = useStore.getState();
  const actions: ToastAction[] = [];
  if (verdict === "unsure") actions.push({ label: "Ask in chat (c)", run: () => askAboutStop(stop, number, true) });
  actions.push({ label: "Undo (u)", run: () => void undoStop(stop.id) });
  const toastId = st.toast("info", verdict === "not" ? "Marked not a problem" : "Marked not sure", { actions, ms: 5000 });
  st.setLastAnswer({ stopId: stop.id, verdict, toastId });
}

// ------------------------------------------------------------------ the code

/** The change's line number on `side` (null: the change isn't on that side). */
export function lineOn(c: ChangeData, side: Side): number | null {
  if (isInsert(c)) return side === "RIGHT" ? c.lineNumber : null;
  if (isDelete(c)) return side === "LEFT" ? c.lineNumber : null;
  return side === "RIGHT" ? c.newLineNumber : c.oldLineNumber;
}

const LONG_HUNK = 40; // longer hunks show the stop's lines with 10 lines around them, until "Show all"
const AROUND = 10;

interface Picked {
  file: FileData;
  hunk: HunkData;       // the whole hunk
  short: HunkData;      // trimmed around the stop's lines (the same object when short)
  keys: string[];       // the stop's lines, marked
  endKey: string | null; // where the comment editor goes (after the stop's last line)
  code: string;         // the stop's lines, for the chat chip
}

function pickHunk(patch: string, stop: Stop): Picked | null {
  let file: FileData | undefined;
  try {
    file = parseDiff(patch)[0];
  } catch {
    return null;
  }
  if (!file || stop.line == null) return null;
  const side = stop.side ?? "RIGHT";
  const end = stop.line;
  const start = Math.min(stop.start_line ?? end, end);
  const inRange = (c: ChangeData) => {
    const n = lineOn(c, side);
    return n !== null && n >= start && n <= end;
  };
  const hunk = file.hunks.find((h) => h.changes.some(inRange));
  if (!hunk) return null;
  const marked = hunk.changes.filter(inRange);
  const keys = marked.map(getChangeKey);
  const last = marked[marked.length - 1];
  let short = hunk;
  if (hunk.changes.length > LONG_HUNK) {
    const first = hunk.changes.indexOf(marked[0]);
    const lastAt = hunk.changes.indexOf(last);
    const changes = hunk.changes.slice(Math.max(0, first - AROUND), lastAt + AROUND + 1);
    if (changes.length < hunk.changes.length) short = { ...hunk, changes };
  }
  return { file, hunk, short, keys, endKey: getChangeKey(last), code: marked.map((c) => c.content).join("\n") };
}

/** The stop's lines from the diff already loaded (for the chat chip); "" when it isn't loaded. */
function codeFor(stop: Stop): string {
  const patch = stop.path ? diffCache.get(diffKey(stop.path)) : undefined;
  return (patch && pickHunk(patch, stop)?.code) || "";
}

/** The file's diff (GET /api/diff), shared with Files changed through diffCache. */
export function usePatch(path: string): { patch: string | null; error: string | null } {
  const key = diffKey(path);
  const [patch, setPatch] = useState<string | null>(diffCache.get(key) ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (diffCache.has(key)) {
      setPatch(diffCache.get(key)!);
      return;
    }
    let cancelled = false;
    api.diff(path).then(
      (r) => {
        diffCache.set(key, r.patch);
        if (!cancelled) setPatch(r.patch);
      },
      (e) => !cancelled && setError((e as Error).message),
    );
    return () => {
      cancelled = true;
    };
  }, [path, key]);
  return { patch, error };
}

/** The hunk with the stop's lines marked, unified only. `widget` (the comment editor, or your comment) goes
 * under the stop's last line. */
function StopHunk({ stop, widget }: { stop: Stop; widget: ReactNode }) {
  const path = stop.path!;
  const { patch, error } = usePatch(path);
  const [whole, setWhole] = useState(false);
  const picked = useMemo(() => (patch ? pickHunk(patch, stop) : null), [patch, stop]);
  const shown = picked ? (whole ? picked.hunk : picked.short) : null;
  const tokens: HunkTokens | null = useMemo(() => {
    const lang = languageOf(path);
    if (!shown || !lang) return null;
    try {
      return tokenize([shown], { highlight: true, refractor: refractorAdapter, language: lang });
    } catch {
      return null;
    }
  }, [shown, path]);
  const { dir, base } = splitPath(path);
  const side = stop.side ?? "RIGHT";
  const where =
    stop.line == null
      ? "the whole file"
      : `${side === "LEFT" ? "old " : ""}line ${stop.start_line && stop.start_line !== stop.line ? `${stop.start_line}–${stop.line}` : stop.line}`;

  let body: ReactNode;
  if (error) body = <div className="blankslate">Could not load the change: {error}</div>;
  else if (patch === null) body = <div className="blankslate">Loading the change…</div>;
  else if (!picked || !shown) {
    body = (
      <div className="blankslate">
        {stop.line == null ? "This file has no changed lines to show." : `Line ${stop.line} is outside this PR's changes.`}{" "}
        <a href={fileHref(path, stop.line, side, true)}>Open the whole file</a>
      </div>
    );
  } else {
    body = (
      <div className="diff-wrap">
        <Diff
          viewType="unified"
          diffType={picked.file.type}
          hunks={[shown]}
          tokens={tokens}
          selectedChanges={picked.keys}
          widgets={widget && picked.endKey ? { [picked.endKey]: <div className="widget-stack">{widget}</div> } : {}}
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
    );
  }
  return (
    <div className="file-box">
      <div className="file-header">
        <div className="file-info">
          <span className="file-path" title={path}>
            <span className="file-dir">{dir}</span>
            {base}
          </span>
          <span className="color-muted small">{where} marked</span>
        </div>
        <div className="file-actions">
          {picked && picked.short !== picked.hunk && (
            <button className="btn btn-sm" onClick={() => setWhole((v) => !v)}>
              {whole ? "Show the lines around it" : `Show all ${picked.hunk.changes.length} lines of this change`}
            </button>
          )}
          <a className="btn btn-sm" href={fileHref(path, stop.line, side)}>
            Open in Files changed
          </a>
        </div>
      </div>
      {body}
      {widget && (!picked || !shown || !picked.endKey) && <div className="widget-stack">{widget}</div>}
    </div>
  );
}

/** A CI stop's "code": the failing check as CI reported it (Claude's explanation comes after, as its view). */
function StopCheck({ stop }: { stop: Stop }) {
  const check = useStore((s) => s.analysis?.ci?.checks.find((c) => c.name === stop.check_name));
  return (
    <section className="Box">
      <div className="Box-header">
        <h3 className="Box-title">{stop.check_name ?? "CI check"}</h3>
        <span className="Label Label--danger">failing</span>
        {check?.app && <span className="color-muted small">{check.app}</span>}
        {check?.url && (
          <a className="small" href={check.url} target="_blank" rel="noreferrer noopener">
            Details
          </a>
        )}
      </div>
      <div className="Box-body">
        {check?.summary && <Markdown className="color-muted" text={check.summary.slice(0, 600)} />}
        {check?.log_tail ? (
          <details className="ci-log" open>
            <summary>End of the log</summary>
            <pre>{check.log_tail}</pre>
          </details>
        ) : (
          !check?.summary && <p className="color-muted">CI gave no log for this check.</p>
        )}
      </div>
    </section>
  );
}

// ------------------------------------------------------------------ the comment editor (y)

function StopEditor({
  stop, draft, onSave, onKeep,
}: {
  stop: Stop; draft: Draft | null; onSave: (body: string) => void; onKeep: (body: string) => void;
}) {
  const [body, setBody] = useState(draft?.body ?? "");
  const fresh = !draft; // a look_for stop: nothing is saved until you save
  return (
    <div className="review-comment composer">
      <div className="rc-head">
        <Avatar name="You" kind="you" />
        <strong>{fresh ? "Add a comment" : "Your comment"}</strong>
        <span className="rc-meta">on {whereText(stop)}</span>
        {draft?.outside_link && (
          <span className="Label Label--attention" title="It links outside this repository. Check the link before you post.">
            contains an outside link
          </span>
        )}
      </div>
      <div className="rc-body">
        <textarea
          className="input comment-input"
          autoFocus
          rows={5}
          aria-label="Comment"
          placeholder={fresh ? "What should the author check or change?" : "Leave a comment"}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              if (body.trim()) onSave(body);
            } else if (e.key === "Escape") {
              e.preventDefault();
              e.stopPropagation();
              onKeep(body);
            }
          }}
        />
      </div>
      <div className="rc-actions">
        <span className="rc-hint">
          {fresh
            ? "⌘/Ctrl + Enter adds it and goes to the next stop · Esc cancels and marks this Not sure"
            : "⌘/Ctrl + Enter saves and goes to the next stop · Esc keeps the comment and stays"}
        </span>
        <button className="btn btn-sm" onClick={() => onKeep(body)}>
          {fresh ? "Cancel" : "Keep and stay"}
        </button>
        <button className="btn btn-primary btn-sm" disabled={!body.trim()} onClick={() => onSave(body)}>
          {fresh ? "Add comment and next" : "Save and next"}
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ the stop

const VERDICT_BUTTONS: [Verdict, string, string][] = [
  ["real", "Real problem", "y"], ["not", "Not a problem", "n"], ["unsure", "Not sure", "?"],
];

export function ReviewStop({
  stop, model, navRef,
}: {
  stop: Stop; model: ReviewModel & { index: number }; navRef: MutableRefObject<Partial<ReviewNav>>;
}) {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const fresh = useStore((s) => s.freshStops);
  const keepAsk = useStore((s) => (s.keepAsk?.stopId === stop.id ? s.keepAsk : null));
  const verdict = state.stop_verdicts[stop.id] ?? null;
  const draft = stopDraft(stop, state.drafts);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const steps = useSteps();
  const how = stepForStop(steps, stop); // "How this code works (step 4)": the Understand step that holds its line
  const [editing, setEditing] = useState<{ draft: Draft | null } | null>(null);
  const [changing, setChanging] = useState(false);
  const [ciAdded, setCiAdded] = useState(false);
  const [busy, setBusy] = useState(false);

  const moreAt = model.more.findIndex((s) => s.id === stop.id);
  const earlierAt = model.earlier.findIndex((s) => s.id === stop.id);
  const inMain = model.index !== -1;
  // What j / k and "next" walk: the numbered list, then the group you opened this stop from.
  const seq = inMain ? model.main : moreAt !== -1 ? [...model.main, ...model.more] : earlierAt !== -1 ? [...model.main, ...model.earlier] : model.main;
  const number = inMain ? model.index + 1 : moreAt !== -1 ? model.main.length + moreAt + 1 : null;
  const inData = !!analysis?.stops.some((s) => s.id === stop.id);
  const droppedHere = stop.dropped || (model.gone.has(stop.id) && (stop.low_confidence || !inData));
  const frozen = stop.dropped; // an earlier answer whose stop is gone: Undo works, answering again doesn't
  const dismissedElsewhere = !verdict && stopDone(stop, state);
  const freshBefore = model.main.slice(0, Math.max(0, model.index)).filter((s) => fresh.includes(s.id));

  useEffect(() => {
    openedOf(stop.id);
    useStore.getState().seenStop(stop.id);
    headingRef.current?.focus();
  }, [stop.id]);

  const focusHeading = () => headingRef.current?.focus();
  const go = (s: Stop | null) => navigate(s ? reviewHref(s.id) : REVIEW_END);
  const goNext = () => go(nextOpen(seq, stop.id, useStore.getState().state, stop.id));
  const step = (delta: 1 | -1) => {
    const at = seq.findIndex((s) => s.id === stop.id);
    const to = at === -1 ? (delta === 1 ? 0 : seq.length - 1) : at + delta;
    if (to >= seq.length) navigate(REVIEW_END);
    else if (to >= 0) go(seq[to]);
  };

  const answer = async (v: Verdict) => {
    const st = useStore.getState();
    if (v === "real") {
      if (stop.kind === "look_for") {
        setEditing({ draft: null }); // the comment is made when you save it (with origin_id)
        return;
      }
      setBusy(true);
      const res = await st.answerStop(stop, "real", openedOf(stop.id));
      setBusy(false);
      if (!res) return;
      if (stop.kind === "ci") setCiAdded(true);
      else if (res.draft) setEditing({ draft: res.draft });
      return;
    }
    // Not a problem / Not sure: move on at once; the answer is saved on the way.
    goNext();
    const res = await st.answerStop(stop, v, openedOf(stop.id));
    if (res) answeredToast(stop, v, number);
  };

  // Change answer: DELETE, then (after "Keep your comment?" when the comment was edited) the new answer.
  const change = async (v: Verdict) => {
    setChanging(false);
    setBusy(true);
    const res = await useStore.getState().undoAnswer(stop.id);
    setBusy(false);
    if (!res) return;
    if (res.kept_draft) {
      useStore.getState().setKeepAsk({ stopId: stop.id, draft: res.kept_draft, then: v });
      return;
    }
    await answer(v);
  };

  const choose = (v: Verdict) => {
    const st = useStore.getState();
    if (busy || st.keepAsk?.stopId === stop.id) return;
    if (frozen) {
      st.toast("info", "Second look dropped this stop, so it can't be answered again. You can undo your earlier answer.");
      return;
    }
    const prev = st.state.stop_verdicts[stop.id];
    if (!prev) return void answer(v);
    if (prev.verdict !== v) return void change(v);
    // The same answer again: y opens your comment, n and ? move on.
    if (v !== "real") goNext();
    else if (stop.kind === "ci") setCiAdded(true);
    else setEditing({ draft: stopDraft(stop, st.state.drafts) });
  };

  const saveEditor = async (body: string) => {
    const st = useStore.getState();
    const d = editing?.draft ?? null;
    if (d) {
      if (body !== d.body && !(await st.updateDraft(d.id, body))) return;
    } else {
      const made = await st.addDraft({
        path: stop.path!, side: stop.side ?? "RIGHT", line: stop.line!, start_line: stop.start_line, body,
        source: "user", origin_id: stop.id,
      });
      if (!made) return;
      if (useStore.getState().state.stop_verdicts[stop.id]?.verdict !== "real") {
        if (!(await useStore.getState().answerStop(stop, "real", openedOf(stop.id)))) return;
      }
    }
    setEditing(null);
    goNext();
  };

  const keepEditor = async (body: string) => {
    const st = useStore.getState();
    const d = editing?.draft ?? null;
    setEditing(null);
    if (!d && !st.state.stop_verdicts[stop.id]) {
      // A look_for stop's editor, cancelled: that answers Not sure.
      const res = await st.answerStop(stop, "unsure", openedOf(stop.id));
      if (res) answeredToast(stop, "unsure", number);
    } else if (d && body.trim() && body !== d.body) {
      await st.updateDraft(d.id, body);
    }
    focusHeading();
  };

  const resolveKeep = async (keep: boolean) => {
    const st = useStore.getState();
    const k = st.keepAsk;
    if (!k) return;
    if (!keep) await st.deleteDraft(k.draft.id); // before the new answer, so its eval label stays
    st.setKeepAsk(null);
    if (k.then) await answer(k.then);
    else focusHeading();
  };

  const undoCi = async () => {
    setCiAdded(false);
    await useStore.getState().undoAnswer(stop.id);
    focusHeading();
  };

  const chat = () => {
    const la = useStore.getState().lastAnswer;
    const other = la && la.verdict === "unsure" && la.stopId !== stop.id ? findStop(la.stopId) : null;
    if (other) {
      const at = model.main.findIndex((s) => s.id === other.id);
      askAboutStop(other, at === -1 ? null : at + 1, true);
    } else askAboutStop(stop, number);
  };

  // The keys (registered by the Review view); refreshed every render so they see this render's state.
  navRef.current = {
    real: () => choose("real"),
    not: () => choose("not"),
    unsure: () => choose("unsure"),
    chat,
    next: () => step(1),
    prev: () => step(-1),
    undo: () => (ciAdded ? void undoCi() : undoLast()),
  };

  // ---- progress: "Stop 2 of 7 · ~25 min left", or "3 answered · 4 left · more coming (…)" while Claude runs
  const counts = reviewCounts(model.main, state);
  let progress: string;
  if (analysis && analysisRunning(analysis)) {
    progress = `${counts.answered} answered · ${counts.left} left · ${moreComing(analysis)}`;
  } else if (inMain) {
    progress = `Stop ${model.index + 1} of ${counts.total} · ${counts.left ? `${fmtMinutes(counts.minutes)} left` : "all answered"}`;
  } else if (moreAt !== -1) {
    progress = `More place ${moreAt + 1} of ${model.more.length}`;
  } else {
    progress = earlierAt !== -1 ? "Earlier answer" : "Not in the list";
  }

  const severity = stop.severity && stop.severity in SEVERITY_TEXT ? (stop.severity as Severity) : null;
  const sure = sureText(stop);
  const widget = editing ? (
    <StopEditor key={editing.draft?.id ?? "new"} stop={stop} draft={editing.draft} onSave={(b) => void saveEditor(b)} onKeep={(b) => void keepEditor(b)} />
  ) : draft && stop.kind !== "ci" ? (
    <DraftCard d={draft} />
  ) : null;

  let actions: ReactNode;
  if (keepAsk) {
    actions = <KeepAsk draft={keepAsk.draft} onChoose={(keep) => void resolveKeep(keep)} />;
  } else if (ciAdded) {
    actions = (
      <>
        <span role="status">
          Added to your review summary ·{" "}
          <button className="btn-link" onClick={() => void undoCi()}>
            Undo
          </button>
        </span>
        <button className="btn btn-sm" onClick={() => step(1)}>
          Next stop (<kbd>j</kbd>)
        </button>
      </>
    );
  } else if (frozen) {
    actions = (
      <>
        <span>
          {verdict ? (
            <>
              You said: <strong>{VERDICT_TEXT[verdict.verdict]}</strong>
            </>
          ) : (
            "Your answer was taken back."
          )}
        </span>
        {verdict && (
          <button className="btn btn-sm" disabled={busy} onClick={() => void undoStop(stop.id)}>
            Undo answer
          </button>
        )}
      </>
    );
  } else if (verdict && !changing) {
    const what =
      verdict.verdict !== "real" ? "" : stop.kind === "ci" ? " · added to your review summary" : draft ? " · comment added" : "";
    actions = (
      <>
        <span>
          You said: <strong>{VERDICT_TEXT[verdict.verdict]}</strong>
          {what}
        </span>
        <button className="btn btn-sm" disabled={busy} onClick={() => setChanging(true)}>
          Change answer
        </button>
      </>
    );
  } else {
    actions = (
      <>
        {dismissedElsewhere && <span className="color-muted small">You dismissed this finding in Files changed.</span>}
        <div className="stop-verdicts" role="group" aria-label="Your answer">
          {VERDICT_BUTTONS.map(([v, label, key]) => (
            <button
              key={v}
              className="btn"
              disabled={busy}
              aria-pressed={changing ? verdict?.verdict === v : undefined}
              onClick={() => choose(v)}
            >
              {label} ({key})
            </button>
          ))}
        </div>
        {changing && (
          <button className="btn btn-invisible btn-sm" onClick={() => setChanging(false)}>
            Cancel
          </button>
        )}
      </>
    );
  }

  return (
    <article className="review-stop" aria-labelledby={`q-${stop.id}`}>
      {freshBefore.length > 0 && (
        <div className="flash flash-info">
          <button className="btn-link" onClick={() => go(freshBefore[0])}>
            {freshBefore.length === 1 ? "1 new stop" : `${freshBefore.length} new stops`} earlier in the list
          </button>
        </div>
      )}
      <header className="stop-head">
        <h2 id={`q-${stop.id}`} className="stop-question" tabIndex={-1} ref={headingRef}>
          <InlineText text={stop.question} />
        </h2>
        <div className="label-list">
          {severity && <FindingLabel severity={severity} category={stop.category} />}
          {stop.category && stop.category !== INTENT && <span className="Label Label--secondary">{stop.category}</span>}
          <span className="Label Label--secondary">{KIND_TEXT[stop.kind]}</span>
          {droppedHere && (
            <span className="Label Label--attention" title="Claude's second look no longer lists this. You can still answer it or skip it.">
              Second look dropped this
            </span>
          )}
          {stop.low_confidence && !droppedHere && <span className="Label Label--secondary">low confidence</span>}
          {stop.more && <span className="Label Label--secondary">past the list</span>}
          <button className="btn-link small" onClick={chat}>
            Ask in chat (c)
          </button>
          {how && (
            <a href={understandHref(how.step.id)}>
              How this code works (step {how.number})
            </a>
          )}
        </div>
      </header>

      {stop.kind === "ci" || !stop.path ? <StopCheck stop={stop} /> : <StopHunk stop={stop} widget={widget} />}

      {stop.scenario.length > 0 && (
        <section className="finding-scenario-box">
          <h3 className="finding-scenario-title small">How it could happen</h3>
          <ol className="finding-scenario">
            {stop.scenario.map((s, i) => (
              <li key={i}>
                <InlineText text={s} />
              </li>
            ))}
          </ol>
        </section>
      )}

      {(stop.claude_view || sure) && (
        <section className="color-muted stop-claude" aria-label="Claude's view">
          <h3 className="finding-scenario-title small">Claude's view</h3>
          {stop.claude_view && <Markdown text={stop.claude_view} />}
          {sure && <div className="finding-meta">{sure}</div>}
        </section>
      )}

      <div className="stop-bar">
        <div className="stop-bar-progress">
          <div>{progress}</div>
          <div className="stop-legend">
            <kbd>y</kbd> real · <kbd>n</kbd> not · <kbd>?</kbd> not sure · <kbd>c</kbd> ask in chat · <kbd>u</kbd> undo ·{" "}
            <kbd>j</kbd>/<kbd>k</kbd> next/previous
          </div>
        </div>
        <div className="stop-bar-actions">{actions}</div>
      </div>
    </article>
  );
}

/** Change answer (or Undo) on an edited "Real problem" comment: keep it, or delete it. */
function KeepAsk({ draft, onChoose }: { draft: Draft; onChoose: (keep: boolean) => void }) {
  const keepRef = useRef<HTMLButtonElement>(null);
  useEffect(() => keepRef.current?.focus(), []);
  const preview = draft.body.length > 80 ? `${draft.body.slice(0, 80)}…` : draft.body;
  return (
    <div role="group" aria-label="Keep your comment?" className="stop-keep">
      <span>
        You edited this comment. <strong>Keep your comment?</strong> <span className="color-muted">"{preview}"</span>
      </span>
      <button ref={keepRef} className="btn btn-sm" onClick={() => onChoose(true)}>
        Keep it
      </button>
      <button className="btn btn-sm btn-danger" onClick={() => onChoose(false)}>
        Delete it
      </button>
    </div>
  );
}
