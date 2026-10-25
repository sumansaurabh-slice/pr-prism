// One step of "Understand this PR": the file and its role, what the code did before, what changes, the diff with the
// lines that matter marked, why (or "Not explained in the PR"), where it connects, the terms a newcomer needs, and the
// tests. The diff shows at once; the text is made on demand. A sticky bar: progress, Ask about this, Got it, next,
// and the background level.
import { useCallback, useEffect, useMemo, useRef, type MutableRefObject, type ReactNode } from "react";
import {
  Decoration, Diff, Hunk, getChangeKey, parseDiff, tokenize,
  type FileData, type GutterOptions, type HunkData, type HunkTokens,
} from "react-diff-view";
import { IconCheck } from "../icons";
import type { UnderstandNav } from "../keyboard";
import { InlineText, Markdown } from "../markdown";
import { UNDERSTAND_END, fileHref, navigate, understandHref } from "../router";
import { explainKey, fileMap, splitPath, useStore } from "../store";
import type { StepExplanation, StepHunk, UnderstandLevel, UnderstandStep } from "../types";
import {
  LEVEL_TEXT, WHY_MISSING, askAboutStep, nextToRead, partText, progressText, stepTitle, type UnderstandModel,
} from "../understand";
import { languageOf, refractorAdapter } from "./FileView";
import { lineOn, usePatch } from "./ReviewStop";

// ------------------------------------------------------------------ the diff

/** Do the two ranges (start, count) share a line? */
function meet(a: number, n: number, b: number, m: number): boolean {
  return n > 0 && m > 0 && a <= b + m - 1 && b <= a + n - 1;
}

/** The diff's hunk is one of the step's (steps split files at hunk boundaries, so overlap means the same hunk). */
function isStepHunk(h: HunkData, s: StepHunk): boolean {
  return meet(h.newStart, h.newLines, s.new_start, s.new_lines) || meet(h.oldStart, h.oldLines, s.old_start, s.old_lines);
}

type LookAt = StepExplanation["look_at"];
const NO_LINES: LookAt = [];

/** The step's hunks of one file, unified, with the `look_at` lines marked (background and a ▶ in the gutter, never
 * colour alone). `hunks` null: every hunk of the file (a group step). */
function StepDiff({ path, hunks, lookAt }: { path: string; hunks: StepHunk[] | null; lookAt: LookAt }) {
  const { patch, error } = usePatch(path);
  const picked = useMemo(() => {
    if (!patch) return null;
    let file: FileData | undefined;
    try {
      file = parseDiff(patch)[0];
    } catch {
      return null;
    }
    if (!file) return null;
    const shown = hunks?.length ? file.hunks.filter((h) => hunks.some((s) => isStepHunk(h, s))) : file.hunks;
    return { file, shown };
  }, [patch, hunks]);
  const marked = useMemo(() => {
    const keys = new Set<string>();
    for (const h of picked?.shown ?? []) {
      for (const c of h.changes) if (lookAt.some((l) => lineOn(c, l.side) === l.line)) keys.add(getChangeKey(c));
    }
    return keys;
  }, [picked, lookAt]);
  const tokens: HunkTokens | null = useMemo(() => {
    const lang = languageOf(path);
    if (!picked?.shown.length || !lang) return null;
    try {
      return tokenize(picked.shown, { highlight: true, refractor: refractorAdapter, language: lang });
    } catch {
      return null;
    }
  }, [picked, path]);
  const renderGutter = useCallback(
    ({ change, side, renderDefault }: GutterOptions) =>
      side === "old" && marked.has(getChangeKey(change)) ? (
        <>
          <span className="look-mark" aria-hidden="true">
            ▶
          </span>
          <span className="sr-only">Look here. </span>
          {renderDefault()}
        </>
      ) : (
        renderDefault()
      ),
    [marked],
  );
  const { dir, base } = splitPath(path);

  let body: ReactNode;
  if (error) body = <div className="blankslate">Could not load the change: {error}</div>;
  else if (patch === null) body = <div className="blankslate">Loading the change…</div>;
  else if (!picked?.shown.length) {
    body = (
      <div className="blankslate">
        No changed lines to show here. <a href={fileHref(path, null, undefined, true)}>Open the whole file</a>
      </div>
    );
  } else {
    body = (
      <div className="diff-wrap">
        <Diff
          viewType="unified"
          diffType={picked.file.type}
          hunks={picked.shown}
          tokens={tokens}
          selectedChanges={[...marked]}
          renderGutter={renderGutter}
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
          {marked.size > 0 && (
            <span className="color-muted small">
              {marked.size} line{marked.size === 1 ? "" : "s"} to look at, marked ▶
            </span>
          )}
        </div>
        <div className="file-actions">
          <a className="btn btn-sm" href={fileHref(path)}>
            Open in Files changed
          </a>
        </div>
      </div>
      {body}
    </div>
  );
}

// ------------------------------------------------------------------ the text

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="und-section" aria-label={title}>
      <h3 className="und-section-title">{title}</h3>
      {children}
    </section>
  );
}

/** "app/api/webhooks.py:13", linking to the file view at that line (the whole file when it isn't in the PR). */
function ConnectLink({ path, line }: { path: string; line: number }) {
  const changed = fileMap(useStore((s) => s.analysis)).has(path);
  return (
    <a href={fileHref(path, line || null, "RIGHT", !changed)}>
      <code>
        {path}
        {line ? `:${line}` : ""}
      </code>
    </a>
  );
}

function PathLinks({ paths }: { paths: string[] }) {
  return (
    <>
      {paths.map((p, i) => (
        <span key={p}>
          {i > 0 && ", "}
          <a href={fileHref(p)}>
            <code>{p}</code>
          </a>
        </span>
      ))}
    </>
  );
}

const LEVELS: UnderstandLevel[] = ["new", "familiar"];

// ------------------------------------------------------------------ the step

export function UnderstandStepView({
  step, model, navRef,
}: {
  step: UnderstandStep; model: UnderstandModel; navRef: MutableRefObject<Partial<UnderstandNav>>;
}) {
  const level = useStore((s) => s.understand?.level ?? "new");
  const entry = useStore((s) => s.explained[explainKey(step.id, level)]);
  const setLevel = useStore((s) => s.setUnderstandLevel);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const number = model.index + 1;
  const after = model.steps[model.index + 1] ?? null;
  const done = entry?.status === "done" ? entry.data : null;
  const understood = !!model.understood[step.id];
  const chapter = model.chapters.find((c) => c.id === step.chapter_id);
  const firstInChapter = chapter?.steps[0] === step;

  useEffect(() => {
    headingRef.current?.focus();
  }, [step.id]);
  // The text is made when you open the step (a failed fetch-ahead is tried again), at the level you chose; the
  // next step's text is fetched ahead once this one is in.
  useEffect(() => {
    void useStore.getState().explainStep(step.id, level, true);
  }, [step.id, level]);
  useEffect(() => {
    if (done && after) void useStore.getState().explainStep(after.id, level);
  }, [done, after, level]);

  const go = (s: UnderstandStep | null) => navigate(s ? understandHref(s.id) : UNDERSTAND_END);
  const gotIt = () => {
    const st = useStore.getState();
    if (!st.understand?.understood[step.id]) void st.setUnderstood(step.id, true);
    go(nextToRead(model.steps, model.index, { ...model.understood, [step.id]: "now" }));
  };
  const move = (delta: 1 | -1) => {
    const to = model.index + delta;
    if (to >= model.steps.length) navigate(UNDERSTAND_END);
    else if (to >= 0) go(model.steps[to]);
  };
  const ask = () => askAboutStep(step, number);
  const retry = () => void useStore.getState().explainStep(step.id, level, true);

  // The keys (registered by the Understand view); refreshed every render so they see this render's state.
  navRef.current = { gotIt, chat: ask, next: () => move(1), prev: () => move(-1) };

  let text: ReactNode;
  if (!entry || entry.status === "loading") {
    text = (
      <p className="color-muted und-loading" role="status">
        Reading the code around this change…
      </p>
    );
  } else if (entry.status === "error") {
    text = (
      <div className="flash flash-error" role="alert">
        <span>
          Couldn't explain this step: {entry.error} ·{" "}
          <button className="btn-link" onClick={retry}>
            Try again
          </button>
        </span>
      </div>
    );
  } else {
    text = (
      <>
        <Section title="Before">
          <Markdown text={entry.data.before} />
        </Section>
        <Section title="What changes">
          <Markdown text={entry.data.change} />
        </Section>
      </>
    );
  }

  const part = partText(model.steps, step);
  const lookAt = done?.look_at ?? NO_LINES;
  return (
    <article className="review-stop und-step" aria-labelledby={`h-${step.id}`}>
      <header className="stop-head">
        <h2 id={`h-${step.id}`} className="stop-question und-title" tabIndex={-1} ref={headingRef}>
          {stepTitle(step)}
        </h2>
        {step.kind === "group" ? (
          <p className="und-role-line color-muted">
            <PathLinks paths={step.files} />
          </p>
        ) : (
          step.role && <Markdown className="und-role-line inline-md color-muted" text={step.role} />
        )}
        <div className="label-list">
          {chapter && (
            <span className="Label Label--secondary" title={step.chapter_goal ?? undefined}>
              Chapter {chapter.number} · {step.chapter_title.replace(/`/g, "")}
            </span>
          )}
          {part && <span className="Label Label--secondary">{part}</span>}
          {understood && (
            <span className="Label Label--success stop-answer">
              <IconCheck size={12} />
              Understood
            </span>
          )}
        </div>
        {firstInChapter && step.chapter_goal && (
          <p className="color-muted small und-goal">
            <InlineText text={step.chapter_goal} />
          </p>
        )}
      </header>

      {text}

      {step.kind === "group" ? (
        step.files.map((p) => <StepDiff key={p} path={p} hunks={null} lookAt={NO_LINES} />)
      ) : (
        <StepDiff key={step.path} path={step.path} hunks={step.hunks} lookAt={lookAt} />
      )}

      {done && (
        <>
          <Section title="Why">
            {done.why && !done.why_missing ? (
              <>
                <Markdown text={done.why} />
                {done.why_source && <p className="color-muted small und-source">Source: {done.why_source}</p>}
              </>
            ) : (
              <p className="color-muted">{WHY_MISSING}</p>
            )}
          </Section>
          {done.connects.length > 0 && (
            <Section title="Where it connects">
              <ul className="und-list">
                {done.connects.map((c, i) => (
                  <li key={i}>
                    <ConnectLink path={c.path} line={c.line} /> <InlineText text={c.text} />
                  </li>
                ))}
              </ul>
            </Section>
          )}
          {done.terms.length > 0 && (
            <Section title="Terms">
              <dl className="und-terms">
                {done.terms.map((t) => (
                  <div key={t.term}>
                    <dt>
                      <InlineText text={t.term} />
                    </dt>
                    <dd>
                      <InlineText text={t.meaning} />
                    </dd>
                  </div>
                ))}
              </dl>
            </Section>
          )}
        </>
      )}

      {step.tests.length > 0 && (
        <p className="und-meta">
          Tested by: <PathLinks paths={step.tests} />
        </p>
      )}
      {step.skim.length > 0 && (
        <p className="und-meta">
          Skim (generated, lock or binary files): <PathLinks paths={step.skim} />
        </p>
      )}

      <div className="stop-bar">
        <div className="stop-bar-progress">
          <div>{progressText(model, step)}</div>
          <div className="stop-legend">
            <kbd>y</kbd> or <kbd>→</kbd> got it · <kbd>c</kbd> ask about this · <kbd>j</kbd>/<kbd>k</kbd> next/previous
          </div>
        </div>
        <div className="stop-bar-actions">
          <div className="und-level" role="group" aria-label="Background">
            {LEVELS.map((l) => (
              <button key={l} className="btn btn-sm" aria-pressed={level === l} onClick={() => void setLevel(l)}>
                {level === l && <IconCheck size={12} />}
                {LEVEL_TEXT[l]}
              </button>
            ))}
          </div>
          <button className="btn" onClick={ask}>
            Ask about this (c)
          </button>
          <button className="btn btn-primary" onClick={gotIt}>
            Got it, next (y)
          </button>
        </div>
      </div>
    </article>
  );
}
