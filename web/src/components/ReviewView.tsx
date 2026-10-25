// The Review view (guided review, design D4-D7): the stop list on the left, one stop at a time in the middle,
// and the end screen. Its keys (y / n / ? / c / u / j / k) work here only.
import { useEffect, useRef, useState } from "react";
import { registerReviewNav, type ReviewNav } from "../keyboard";
import { analysisRunning, nextOpen, useReviewCounts, useReviewModel } from "../review";
import { REVIEW_END, fileHref, navigate, replaceRoute, reviewHref, type Route } from "../router";
import { fmtMinutes, reviewOrder, useStore } from "../store";
import { ReviewEnd } from "./ReviewEnd";
import { ReviewStop, undoLast } from "./ReviewStop";
import { StopList } from "./StopList";

type ReviewRoute = Extract<Route, { view: "review" }>;

/** No stops: "Nothing risky found" only once the analysis is complete with nothing skipped (design D7). */
function NoStops() {
  const analysis = useStore((s) => s.analysis)!;
  const includeLow = useStore((s) => s.includeLow);
  const low = analysis.stops.filter((s) => s.low_confidence && !s.dropped).length;
  const first = reviewOrder(analysis)[0];
  const skipped = analysis.skipped_areas ?? [];
  let body;
  if (analysisRunning(analysis)) {
    body = <p>Claude is still checking. Stops will appear here as each area finishes.</p>;
  } else if (skipped.length) {
    body = (
      <>
        <p>No stops, but Claude didn't check every area, so this isn't the same as nothing risky:</p>
        <ul className="stop-skipped">
          {skipped.map((a) => (
            <li key={a.title}>
              {a.title} <span className="color-muted">({a.reason})</span>
            </li>
          ))}
        </ul>
      </>
    );
  } else {
    body = (
      <p>
        <strong>Nothing risky found.</strong> {first && <a href={fileHref(first)}>Skim the reading order</a>}
      </p>
    );
  }
  return (
    <div className="blankslate" role="status">
      {body}
      {low > 0 && !includeLow && (
        <p className="small">
          {low} low-confidence finding{low > 1 ? "s are" : " is"} hidden. Turn on "Include low-confidence findings" to
          see {low > 1 ? "them" : "it"}.
        </p>
      )}
    </div>
  );
}

export function ReviewView({ route }: { route: ReviewRoute }) {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const model = useReviewModel(route.stopId);
  const navRef = useRef<Partial<ReviewNav>>({});
  const [announce, setAnnounce] = useState("");

  // The keys go to whatever is on screen (a stop or the end screen); none of them leaks to the file keys.
  useEffect(() => {
    const call = (k: keyof ReviewNav) => () => navRef.current[k]?.();
    registerReviewNav({
      real: call("real"), not: call("not"), unsure: call("unsure"), chat: call("chat"),
      next: call("next"), prev: call("prev"),
      undo: () => (navRef.current.undo ? navRef.current.undo() : undoLast()),
    });
    return () => registerReviewNav(null);
  }, []);

  // #/review resumes: the first stop not answered yet, or the end screen when every stop is answered.
  const resumeTo = !route.stopId && !route.end && analysis ? nextOpen(model.main, null, state) : null;
  useEffect(() => {
    if (resumeTo) replaceRoute(reviewHref(resumeTo.id));
  }, [resumeTo]);

  // "Stop 3 of 7", read out on each move (the heading takes focus).
  const total = model.main.length;
  useEffect(() => {
    if (route.stopId && model.index !== -1) setAnnounce(`Stop ${model.index + 1} of ${total}`);
    else if (route.end) setAnnounce("End of the review");
  }, [route.stopId, route.end]); // only on a move, not when a poll changes the total

  if (!route.stopId) navRef.current = {};
  let main;
  if (!analysis) main = <div className="blankslate">Loading the pull request…</div>;
  else if (route.stopId && model.current) {
    main = <ReviewStop key={model.current.id} stop={model.current} model={model} navRef={navRef} />;
  } else if (route.stopId) {
    main = (
      <div className="blankslate" role="status">
        <p>This stop isn't part of the review any more.</p>
        <a className="btn" href={reviewHref()}>
          Go to the next stop
        </a>
      </div>
    );
  } else if (!total && !route.end) main = <NoStops />;
  else if (resumeTo) main = <div className="blankslate">Opening the next stop…</div>;
  else main = <ReviewEnd model={model} navRef={navRef} />;

  return (
    <>
      <StopList model={model} currentId={route.stopId} />
      <main className="pane center review-main">
        <div className="center-body">{main}</div>
        <div className="sr-only" aria-live="polite">
          {announce}
        </div>
      </main>
    </>
  );
}

/** The Overview's plan box (design D4): the size of the job, and Start review / Resume review (3 left). */
export function PlanBox() {
  const analysis = useStore((s) => s.analysis);
  const counts = useReviewCounts();
  if (!analysis) return null;
  const { places, quiet, left, minutes, started } = counts;
  let line: string;
  if (!counts.total) {
    line = analysisRunning(analysis)
      ? "Claude is still checking. Stops will appear here as each area finishes."
      : (analysis.skipped_areas ?? []).length
        ? "No places flagged, but some areas weren't checked."
        : "Nothing risky found.";
  } else {
    line = [
      `${places} place${places === 1 ? " needs" : "s need"} a human`,
      `${quiet} file${quiet === 1 ? " has" : "s have"} nothing flagged`,
      left ? fmtMinutes(minutes) : "all answered",
    ].join(" · ");
  }
  const more = analysis.review_summary?.more_places ?? 0;
  const label = !counts.total ? null : !left ? "See your answers" : started ? `Resume review (${left} left)` : "Start review";
  return (
    <section className="Box" aria-label="Review plan">
      <div className="Box-row ci-line">
        <strong>{line}</strong>
        {more > 0 && counts.total > 0 && <span className="color-muted small">and {more} more past the list</span>}
        {label && (
          <span className="tc-head-right">
            {/* #/review opens the first stop not answered yet */}
            <button className="btn btn-primary" onClick={() => navigate(left ? reviewHref() : REVIEW_END)}>
              {label}
            </button>
          </span>
        )}
      </div>
    </section>
  );
}
