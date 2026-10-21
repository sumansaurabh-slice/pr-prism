import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { IconX } from "../icons";
import { useStore } from "../store";
import type { ReviewEvent, ReviewPreview } from "../types";

const EVENTS: { value: ReviewEvent; label: string; hint: string }[] = [
  { value: "COMMENT", label: "Comment", hint: "Share feedback without approving." },
  { value: "APPROVE", label: "Approve", hint: "Say the PR is good to merge." },
  { value: "REQUEST_CHANGES", label: "Request changes", hint: "Ask for changes before it can be merged." },
  { value: "PENDING", label: "Save on GitHub, don't send yet", hint: "Only you can see it. Finish it later on github.com." },
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
      toast("success", event === "PENDING" ? "Review saved on GitHub (not sent yet)" : "Review sent");
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
                        setBody(r.body);
                      } catch (e) {
                        toast("error", `Claude could not write it: ${(e as Error).message}`);
                      } finally {
                        setDrafting(false);
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
                          {blocked ? "You can't do this on your own PR." : ev.hint}
                        </span>
                      </span>
                    </label>
                  );
                })}
              </fieldset>

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
