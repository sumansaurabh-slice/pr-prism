// The end of the guided review (design D16): hand off to posting. In order: the real problems ready to post
// (their comments, editable), the stops you weren't sure about, what isn't checked yet, and one Submit button,
// which opens on Comment (never Approve).
import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { api } from "../api";
import type { ReviewNav } from "../keyboard";
import { InlineText } from "../markdown";
import { VERDICT_TEXT, stopDraft, whereText, type ReviewModel } from "../review";
import { navigate, reviewHref } from "../router";
import { useStore } from "../store";
import type { Coverage, Stop } from "../types";
import { DraftCard } from "./FileView";

function CoverageLine() {
  const verdicts = useStore((s) => s.state.stop_verdicts);
  const [coverage, setCoverage] = useState<Coverage | null>(null);
  const [error, setError] = useState(false);
  useEffect(() => {
    let cancelled = false;
    api.coverage().then(
      (c) => {
        if (cancelled) return;
        setCoverage(c);
        setError(false);
      },
      () => !cancelled && setError(true),
    );
    return () => {
      cancelled = true;
    };
  }, [verdicts]);
  if (error) return <p className="color-muted">Couldn't check coverage</p>;
  if (!coverage) return <p className="color-muted">Checking what you've looked at…</p>;
  const n = coverage.count;
  const rows = coverage.not_checked.slice(0, 5);
  return (
    <div>
      <p className={n ? undefined : "color-muted"}>
        {n === 0 ? "You looked at every place that needed a human" : `${n} place${n === 1 ? "" : "s"} not checked yet`}
        {coverage.running && <span className="color-muted"> · Analysis still running; this list may grow</span>}
      </p>
      {n > 0 && (
        <ul className="stop-skipped">
          {rows.map((r) => (
            <li key={r.stop_id}>
              <a href={reviewHref(r.stop_id)}>
                <code>{whereText({ path: r.path, line: r.line, kind: r.kind, check_name: null })}</code> ·{" "}
                <InlineText text={r.question} />
              </a>
            </li>
          ))}
          {coverage.skipped_areas.map((a) => (
            <li key={a.title}>
              Deep check area not checked: {a.title} <span className="color-muted">({a.reason})</span>
            </li>
          ))}
          {coverage.not_checked.length > rows.length && <li className="color-muted">and {coverage.not_checked.length - rows.length} more</li>}
        </ul>
      )}
    </div>
  );
}

export function ReviewEnd({ model, navRef }: { model: ReviewModel; navRef: MutableRefObject<Partial<ReviewNav>> }) {
  const state = useStore((s) => s.state);
  const setSubmitOpen = useStore((s) => s.setSubmitOpen);
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);

  const listed = [...model.main, ...model.more];
  const said = (v: string) => listed.filter((s) => state.stop_verdicts[s.id]?.verdict === v);
  const real = said("real");
  const not = said("not");
  const unsure = said("unsure");
  const comments = real.map((s) => stopDraft(s, state.drafts)).filter((d) => d !== null);
  const ci = real.filter((s) => s.kind === "ci");
  const last = model.main[model.main.length - 1];

  navRef.current = { prev: () => last && navigate(reviewHref(last.id)) };

  const go = (s: Stop) => navigate(reviewHref(s.id));
  return (
    <section className="review-stop" aria-labelledby="review-end-title">
      <header className="stop-head">
        <h2 id="review-end-title" className="stop-question" tabIndex={-1} ref={headingRef}>
          {real.length === 0
            ? `No problems in ${model.main.length} stop${model.main.length === 1 ? "" : "s"}`
            : `${real.length} real problem${real.length === 1 ? " is" : "s are"} ready to post`}
        </h2>
        <p className="color-muted">
          {real.length} {real.length === 1 ? "real problem" : "real problems"} · {not.length} not a problem · {unsure.length} not sure
        </p>
      </header>

      {(comments.length > 0 || ci.length > 0) && (
        <div className="widget-stack">
          {comments.map((d) => (
            <DraftCard key={d.id} d={d} compact />
          ))}
          {ci.map((s) => (
            <p key={s.id} className="color-muted">
              <code>{s.check_name}</code> failing: added to your review summary.
            </p>
          ))}
        </div>
      )}

      {unsure.length > 0 && (
        <div>
          <button className="btn" onClick={() => go(unsure[0])}>
            Go back to the {unsure.length} you weren't sure about
          </button>
        </div>
      )}

      <CoverageLine />

      {model.earlier.length > 0 && (
        <section>
          <h3 className="finding-scenario-title small">Earlier answers</h3>
          <ul className="stop-skipped">
            {model.earlier.map((s) => {
              const v = state.stop_verdicts[s.id]?.verdict;
              return (
                <li key={s.id}>
                  <a href={reviewHref(s.id)}>
                    <InlineText text={s.question} />
                  </a>{" "}
                  <span className="color-muted">
                    · {whereText(s)} · {v ? `You said: ${VERDICT_TEXT[v]}` : "taken back"} · Second look dropped this
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <div>
        <button className="btn btn-primary" onClick={() => setSubmitOpen(true)}>
          Submit review
        </button>
      </div>
    </section>
  );
}
