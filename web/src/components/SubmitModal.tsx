import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import { highRiskText, placeLabel, plural, riskySeenText, stopHref, useCoverage } from "../coverage";
import { IconCheck, IconX } from "../icons";
import { InlineText } from "../markdown";
import { fileHref, navigate } from "../router";
import { splitPath, useStore } from "../store";
import type { Coverage, ReviewEvent, ReviewPreview } from "../types";

const EVENTS: { value: ReviewEvent; label: string; hint: string }[] = [
  { value: "COMMENT", label: "Comment", hint: "Share feedback without approving." },
  { value: "APPROVE", label: "Approve", hint: "Say the PR is good to merge." },
  { value: "REQUEST_CHANGES", label: "Request changes", hint: "Ask for changes before it can be merged." },
  { value: "PENDING", label: "Save on GitHub, don't send yet", hint: "Only you can see it. Finish it later on github.com." },
];

export const SAVE_BODY_MS = 600; // the review summary autosaves this long after you stop typing
const MAX_ROWS = 5;              // places listed under the coverage line, then "and N more"

type CoverageView = { status: "loading" } | { status: "error" } | { status: "ready"; coverage: Coverage };

/** A link that closes the dialog first. The dialog unmounts on close, so the link navigates itself. */
function JumpLink({ href, title, onJump, children }: {
  href: string; title?: string; onJump: (href: string) => void; children: ReactNode;
}) {
  return (
    <a
      href={href}
      title={title}
      onClick={(e) => {
        e.preventDefault();
        onJump(href);
      }}
    >
      {children}
    </a>
  );
}

/** A place you can jump to: "retry.py:14 · Should a 4xx stop the retries?" */
function PlaceLink({ href, path, line, question, onJump }: {
  href: string; path: string | null; line: number | null; question: string; onJump: (href: string) => void;
}) {
  return (
    <JumpLink href={href} title={path ?? undefined} onJump={onJump}>
      <span className="mono">{placeLabel(path, line)}</span> · <InlineText text={question} />
    </JumpLink>
  );
}

/** The places not checked yet: stops to jump to, then deep-check areas that didn't run; at most 5, then "and N more". */
function CoverageRows({ coverage, onJump }: { coverage: Coverage; onJump: (href: string) => void }) {
  const rows = [
    ...coverage.not_checked.map((p) => (
      <li key={p.stop_id}>
        <PlaceLink href={stopHref(p.stop_id)} path={p.path} line={p.line} question={p.question} onJump={onJump} />
      </li>
    )),
    ...coverage.skipped_areas.map((a, i) => (
      <li key={`area-${i}`}>
        Deep check didn't run: {a.title} <span className="color-muted">({a.reason})</span>
      </li>
    )),
  ];
  const more = rows.length - MAX_ROWS;
  return (
    <ul className="coverage-list" aria-label="Places not checked yet">
      {rows.slice(0, MAX_ROWS)}
      {more > 0 && <li className="color-muted">and {more} more</li>}
    </ul>
  );
}

/** Coverage before submit (design D8, D15): a warning only; Submit stays enabled whatever it says. */
function SubmitCoverage({ view, event, onJump }: { view: CoverageView; event: ReviewEvent; onJump: (href: string) => void }) {
  const stops = useStore((s) => s.analysis?.stops);
  const verdicts = useStore((s) => s.state.stop_verdicts);
  const dismissed = useStore((s) => s.state.dismissed_suggestions);
  const [show, setShow] = useState(false);
  if (view.status !== "ready") {
    return (
      <div className="coverage color-muted" role="status">
        {view.status === "loading" ? "Checking what you've looked at…" : "Couldn't check coverage"}
      </div>
    );
  }
  const c = view.coverage;
  const n = c.count;
  const low = (stops ?? []).filter(
    (s) => s.low_confidence && !s.dropped && !verdicts?.[s.id] && !(s.finding_id && dismissed.includes(s.finding_id)),
  );
  const unseenFiles = Object.entries(c.high_risk_unseen.by_file).filter(([, v]) => v.seen < v.total);
  return (
    <div className="coverage">
      {n === 0 ? (
        <p className="coverage-line color-muted">
          <IconCheck size={14} /> You looked at every place that needed a human
        </p>
      ) : event === "APPROVE" ? (
        <div className="flash flash-warn coverage-warn" role="alert">
          <p>
            <strong>You're approving with {plural(n, "risky place")} not checked</strong>
          </p>
          <CoverageRows coverage={c} onJump={onJump} />
        </div>
      ) : (
        <>
          <p className="coverage-line color-muted">
            {plural(n, "place")} not checked yet{" "}
            <button className="btn-link" aria-expanded={show} onClick={() => setShow((v) => !v)}>
              {show ? "hide" : "show"}
            </button>
          </p>
          {show && <CoverageRows coverage={c} onJump={onJump} />}
        </>
      )}
      {c.running && <p className="coverage-line color-muted">Analysis still running; this list may grow</p>}
      {c.unsure.length > 0 && (
        <details className="coverage-more">
          <summary>Not sure: {c.unsure.length}</summary>
          <ul className="coverage-list">
            {c.unsure.map((p) => (
              <li key={p.stop_id}>
                <PlaceLink href={stopHref(p.stop_id)} path={p.path} line={p.line} question={p.question} onJump={onJump} />
              </li>
            ))}
          </ul>
        </details>
      )}
      {c.high_risk_unseen.hunks > 0 && (
        <details className="coverage-more">
          <summary>{highRiskText(c)}</summary>
          <ul className="coverage-list">
            {unseenFiles.map(([path, v]) => (
              <li key={path}>
                <JumpLink href={fileHref(path)} title={path} onJump={onJump}>
                  <span className="mono">{splitPath(path).base}</span>
                </JumpLink>{" "}
                <span className="color-muted">· {riskySeenText(v)}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      {c.low_confidence > 0 && (
        <details className="coverage-more">
          <summary>Low confidence, not checked: {c.low_confidence}</summary>
          <ul className="coverage-list">
            {low.map((s) => (
              <li key={s.id}>
                {s.path ? (
                  <PlaceLink
                    href={fileHref(s.path, s.line, s.side ?? "RIGHT")} path={s.path} line={s.line} question={s.question}
                    onJump={onJump}
                  />
                ) : (
                  <InlineText text={s.question} />
                )}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}

export function SubmitModal() {
  const open = useStore((s) => s.submitOpen);
  const setOpen = useStore((s) => s.setSubmitOpen);
  const drafts = useStore((s) => s.state.drafts);
  const loadState = useStore((s) => s.loadState);
  const toast = useStore((s) => s.toast);
  // The review summary is saved as you type (PUT /api/review-body-draft), so closing the dialog or following a
  // jump link never loses it. A CI stop's "Real problem" adds to the same saved text.
  const savedBody = useStore((s) => s.state.review_body_draft);
  const [body, setBody] = useState(savedBody ?? "");
  const synced = useRef(savedBody ?? ""); // the text last loaded from or saved to the server
  const pendingSave = useRef<{ text: string; timer: number } | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [coverage, setCoverage] = useState<CoverageView>({ status: "loading" });
  const [event, setEvent] = useState<ReviewEvent>("COMMENT");
  const [commit, setCommit] = useState<"analyzed" | "current">("analyzed");
  const [preview, setPreview] = useState<ReviewPreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [drafting, setDrafting] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [done, setDone] = useState<{ html_url: string } | null>(null);
  const [showPayload, setShowPayload] = useState(false);
  const seq = useRef(0);

  // Every opening starts on Comment, never Approve (R0), e.g. from the end of the guided review.
  useEffect(() => {
    if (open) setEvent("COMMENT");
  }, [open]);

  const saveBody = useCallback(async (text: string) => {
    try {
      await api.saveReviewBody(text);
      synced.current = text;
      setSaveError(null);
      useStore.setState((s) => ({ state: { ...s.state, review_body_draft: text } }));
    } catch (e) {
      setSaveError((e as Error).message);
    }
  }, []);

  /** Save a waiting edit now (on close, on a jump link). */
  const flushBody = useCallback(() => {
    const p = pendingSave.current;
    if (!p) return;
    window.clearTimeout(p.timer);
    pendingSave.current = null;
    void saveBody(p.text);
  }, [saveBody]);

  const dropPendingSave = () => {
    if (pendingSave.current) window.clearTimeout(pendingSave.current.timer);
    pendingSave.current = null;
  };

  const typed = useRef(false); // edited since the dialog opened
  const changeBody = (text: string) => {
    typed.current = true;
    setBody(text);
    if (pendingSave.current) window.clearTimeout(pendingSave.current.timer);
    pendingSave.current = { text, timer: window.setTimeout(flushBody, SAVE_BODY_MS) };
  };

  // Open: start from the saved summary, re-read it from the server (it may have changed in another tab or by a
  // CI stop's answer) and check coverage. Close: save what's still waiting.
  useEffect(() => {
    if (!open) {
      flushBody();
      return;
    }
    const saved = useStore.getState().state.review_body_draft ?? "";
    synced.current = saved;
    typed.current = false;
    setBody(saved);
    setCoverage({ status: "loading" });
    let live = true;
    void useCoverage
      .getState()
      .load()
      .then((c) => live && setCoverage(c ? { status: "ready", coverage: c } : { status: "error" }));
    api.state().then(
      (st) => {
        const latest = st.review_body_draft ?? "";
        if (!live || typed.current || latest === useStore.getState().state.review_body_draft) return;
        useStore.setState((s) => ({ state: { ...s.state, review_body_draft: latest } }));
      },
      () => {}, // the saved copy stays
    );
    return () => {
      live = false;
    };
  }, [open, flushBody]);
  useEffect(() => flushBody, [flushBody]); // unmount

  // The saved summary changed elsewhere (a CI stop's "Real problem", or the state loading late): show it.
  useEffect(() => {
    const saved = savedBody ?? "";
    if (saved === synced.current) return;
    synced.current = saved;
    dropPendingSave();
    setBody(saved);
  }, [savedBody]);

  useEffect(() => {
    if (!open) {
      setDone(null);
      setSubmitError(null);
      return;
    }
    const mine = ++seq.current;
    const t = window.setTimeout(() => {
      api.previewReview(body, event).then(
        (p) => {
          if (mine !== seq.current) return;
          setPreview(p);
          setPreviewError(null);
          if (p.is_author && (event === "APPROVE" || event === "REQUEST_CHANGES")) setEvent("COMMENT");
        },
        (e) => mine === seq.current && setPreviewError((e as Error).message),
      );
    }, 250);
    return () => window.clearTimeout(t);
  }, [open, body, event, drafts]);

  if (!open) return null;

  const close = () => setOpen(false);
  // A jump link saves the summary, closes the dialog and goes to the place.
  const jump = (href: string) => {
    flushBody();
    setOpen(false);
    navigate(href);
  };
  // Comments from an older commit stay pending and are never sent (the preview leaves them out).
  const outdated = drafts.filter((d) => d.outdated).length;
  // GitHub requires a body or at least one comment for everything except a bare approval.
  const empty = event !== "APPROVE" && !body.trim() && drafts.length - outdated === 0;

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await api.submitReview(body, event, commit);
      setDone({ html_url: res.html_url });
      dropPendingSave();
      setBody("");
      await saveBody(""); // posted: the next review starts from an empty summary
      await loadState();
      toast("success", event === "PENDING" ? "Review saved on GitHub (not sent yet)" : "Review sent");
      if (res.warning) toast("error", res.warning);
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="overlay overlay-center" onClick={close}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Finish your review" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>{done ? "Review sent" : "Finish your review"}</h2>
          <button className="btn-octicon" onClick={close} aria-label="Close">
            <IconX />
          </button>
        </header>

        {done ? (
          <div className="modal-body">
            <p>
              {event === "PENDING" ? "Your review is saved on GitHub but not sent. Open it there to finish it." : "Your review is now on GitHub."}
            </p>
            <a className="btn btn-primary" href={done.html_url} target="_blank" rel="noreferrer noopener">
              Open on GitHub ↗
            </a>
          </div>
        ) : (
          <>
            <div className="modal-body">
              <div className="field">
                <div className="field-row">
                  <label htmlFor="review-body" className="field-label">
                    Summary
                  </label>
                  <button
                    className="btn btn-sm"
                    disabled={drafting}
                    onClick={async () => {
                      setDrafting(true);
                      try {
                        const r = await api.draftSummary();
                        changeBody(r.body);
                      } catch (e) {
                        toast("error", `Claude could not write it: ${(e as Error).message}`);
                      } finally {
                        setDrafting(false);
                        void useStore.getState().loadCost(); // that Claude call costs money too
                      }
                    }}
                  >
                    {drafting ? "Claude is writing…" : "Ask Claude to write it"}
                  </button>
                </div>
                <textarea
                  id="review-body"
                  className="input"
                  rows={6}
                  value={body}
                  placeholder="Leave a comment (optional if you have line comments)"
                  onChange={(e) => changeBody(e.target.value)}
                />
                {saveError && <p className="color-muted small">Couldn't save the summary yet: {saveError}</p>}
              </div>

              <fieldset className="events">
                {EVENTS.map((ev) => {
                  const blocked = preview?.is_author && (ev.value === "APPROVE" || ev.value === "REQUEST_CHANGES");
                  return (
                    <label key={ev.value} className={`event${event === ev.value ? " on" : ""}${blocked ? " disabled" : ""}`}>
                      <input
                        type="radio"
                        name="event"
                        value={ev.value}
                        checked={event === ev.value}
                        disabled={!!blocked}
                        onChange={() => setEvent(ev.value)}
                      />
                      <span>
                        <span className="event-label">{ev.label}</span>
                        <span className="event-hint">
                          {blocked ? "You can't do this on your own PR." : ev.hint}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </fieldset>

              <SubmitCoverage view={coverage} event={event} onJump={jump} />

              {previewError && <div className="flash flash-error">Could not check the review: {previewError}</div>}
              {preview && (
                <div className="preview">
                  <p>
                    <strong>{preview.inline_count}</strong> comment{preview.inline_count === 1 ? "" : "s"} on lines.
                    {preview.unanchored.length > 0 && (
                      <>
                        {" "}
                        <strong>{preview.unanchored.length}</strong> more will go in the summary as links. Their lines are
                        not in the diff.
                      </>
                    )}{" "}
                    Posting as <strong>@{preview.viewer}</strong>.
                  </p>
                  {outdated > 0 && (
                    <p className="color-muted" title="They are from an older commit. Re-place them in Pending comments to send them.">
                      {outdated} outdated comment{outdated === 1 ? "" : "s"} won't be sent
                    </p>
                  )}
                  {preview.unanchored.length > 0 && (
                    <ul className="unanchored">
                      {preview.unanchored.map((d) => (
                        <li key={d.id} className="mono small">
                          {d.path}:{d.start_line ? `${d.start_line}-` : ""}
                          {d.line}
                        </li>
                      ))}
                    </ul>
                  )}
                  {preview.head_changed && (
                    <div className="flash flash-warn">
                      <p>
                        The PR got new commits after Claude read it (
                        <code>{preview.analyzed_head.slice(0, 7)}</code> → <code>{preview.current_head.slice(0, 7)}</code>).
                        Comments on lines that changed may show as outdated. Run <code>/pr-prism</code> again to update
                        the review and move your comments to the right lines.
                      </p>
                      <label>
                        <input type="radio" checked={commit === "analyzed"} onChange={() => setCommit("analyzed")} /> Post
                        on the commit Claude read
                      </label>
                      <label>
                        <input type="radio" checked={commit === "current"} onChange={() => setCommit("current")} /> Post on
                        the newest commit
                      </label>
                    </div>
                  )}
                  <button className="btn-link small" onClick={() => setShowPayload((v) => !v)}>
                    {showPayload ? "▾" : "▸"} Show exactly what will be sent to GitHub
                  </button>
                  {showPayload && <pre className="payload">{JSON.stringify(preview.payload, null, 2)}</pre>}
                </div>
              )}
              {submitError && <div className="flash flash-error">{submitError}</div>}
            </div>
            <footer className="modal-foot">
              <span className="color-muted small">Nothing is sent until you press the green button.</span>
              <button className="btn" onClick={close}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={submitting || empty || !preview}
                title={empty ? "Write a summary or add at least one comment first" : undefined}
                onClick={() => void submit()}
              >
                {submitting ? "Sending…" : event === "PENDING" ? "Save on GitHub" : "Submit review"}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}
