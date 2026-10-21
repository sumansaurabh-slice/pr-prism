import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { useStore } from "../store";
import type { ReviewEvent, ReviewPreview } from "../types";

const EVENTS: { value: ReviewEvent; label: string; hint: string }[] = [
  { value: "COMMENT", label: "Comment", hint: "General feedback without explicit approval." },
  { value: "APPROVE", label: "Approve", hint: "Approve merging these changes." },
  { value: "REQUEST_CHANGES", label: "Request changes", hint: "Feedback that must be addressed before merging." },
  { value: "PENDING", label: "Save as pending on GitHub", hint: "Creates a pending review only you can see. Finish it on github.com." },
];

export function SubmitModal() {
  const open = useStore((s) => s.submitOpen);
  const setOpen = useStore((s) => s.setSubmitOpen);
  const drafts = useStore((s) => s.state.drafts);
  const loadState = useStore((s) => s.loadState);
  const toast = useStore((s) => s.toast);
  const [body, setBody] = useState("");
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
  // GitHub requires a body or at least one comment for everything except a bare approval.
  const empty = event !== "APPROVE" && !body.trim() && drafts.length === 0;

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      const res = await api.submitReview(body, event, commit);
      setDone({ html_url: res.html_url });
      setBody("");
      await loadState();
      toast("success", event === "PENDING" ? "Pending review saved on GitHub" : "Review submitted");
    } catch (e) {
      setSubmitError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="overlay overlay-center" onClick={close}>
      <div className="modal" role="dialog" aria-modal="true" aria-label="Submit review" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>{done ? "Review published" : "Submit review"}</h2>
          <button className="btn btn-ghost btn-icon" onClick={close} aria-label="Close">
            ×
          </button>
        </header>

        {done ? (
          <div className="modal-body">
            <p>
              {event === "PENDING" ? "Your pending review is on GitHub. Open it there to finish." : "Your review is on GitHub."}
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
                    Review summary
                  </label>
                  <button
                    className="btn btn-sm"
                    disabled={drafting}
                    onClick={async () => {
                      setDrafting(true);
                      try {
                        const r = await api.draftSummary();
                        setBody(r.body);
                      } catch (e) {
                        toast("error", `Claude couldn't draft it: ${(e as Error).message}`);
                      } finally {
                        setDrafting(false);
                      }
                    }}
                  >
                    {drafting ? "Claude is drafting…" : "Ask Claude to draft"}
                  </button>
                </div>
                <textarea
                  id="review-body"
                  className="input"
                  rows={6}
                  value={body}
                  placeholder="Overall feedback (Markdown). Optional when you have inline comments."
                  onChange={(e) => setBody(e.target.value)}
                />
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
                          {blocked ? "Not allowed on your own PR." : ev.hint}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </fieldset>

              {previewError && <div className="banner banner-error">Preview failed: {previewError}</div>}
              {preview && (
                <div className="preview">
                  <p>
                    <strong>{preview.inline_count}</strong> inline comment{preview.inline_count === 1 ? "" : "s"}
                    {preview.unanchored.length > 0 && (
                      <>
                        {" "}
                        + <strong>{preview.unanchored.length}</strong> folded into the body as permalinks
                      </>
                    )}{" "}
                    · as <strong>@{preview.viewer}</strong>
                  </p>
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
                    <div className="banner banner-warn">
                      <p>
                        The PR has new commits since the analysis (
                        <code>{preview.analyzed_head.slice(0, 7)}</code> → <code>{preview.current_head.slice(0, 7)}</code>).
                        Comments on lines that changed may show as outdated. Re-run <code>/pr-review</code> to refresh and
                        remap your drafts.
                      </p>
                      <label>
                        <input type="radio" checked={commit === "analyzed"} onChange={() => setCommit("analyzed")} /> Comment
                        on the analyzed commit
                      </label>
                      <label>
                        <input type="radio" checked={commit === "current"} onChange={() => setCommit("current")} /> Comment
                        on the latest commit
                      </label>
                    </div>
                  )}
                  <button className="link-btn small" onClick={() => setShowPayload((v) => !v)}>
                    {showPayload ? "▾" : "▸"} Exact payload sent to GitHub
                  </button>
                  {showPayload && <pre className="payload">{JSON.stringify(preview.payload, null, 2)}</pre>}
                </div>
              )}
              {submitError && <div className="banner banner-error">{submitError}</div>}
            </div>
            <footer className="modal-foot">
              <span className="dim small">Nothing is posted until you confirm.</span>
              <button className="btn" onClick={close}>
                Cancel
              </button>
              <button
                className="btn btn-primary"
                disabled={submitting || empty || !preview}
                title={empty ? "Add a summary or at least one comment" : undefined}
                onClick={() => void submit()}
              >
                {submitting ? "Publishing…" : event === "PENDING" ? "Save pending review" : "Publish review"}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}
