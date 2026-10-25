// The Review view's left pane (design D5): the stops in order, each with its number, kind, file:line and your
// answer; "N more places" folded at the bottom; the low-confidence switch; and the files with nothing flagged.
// Below 1100 px it is a rail of numbers and answer icons that opens on hover or focus.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { IconAlert, IconCheck, IconChevronDown, IconChevronRight, IconCircle, IconQuestion } from "../icons";
import { KIND_TEXT, VERDICT_TEXT, stopDone, whereText, type ReviewModel } from "../review";
import { fileHref, reviewHref } from "../router";
import { reviewOrder, useStore } from "../store";
import type { Stop, Verdict } from "../types";

const VERDICT_LOOK: Record<Verdict, { icon: ReactNode; variant: string }> = {
  real: { icon: <IconAlert size={12} />, variant: "danger" },
  not: { icon: <IconCheck size={12} />, variant: "success" },
  unsure: { icon: <IconQuestion size={12} />, variant: "attention" },
};

/** Your answer as icon plus text ("Real problem", "Not a problem", "Not sure", or "Open"). */
export function AnswerLabel({ stop }: { stop: Stop }) {
  const state = useStore((s) => s.state);
  const v = state.stop_verdicts[stop.id]?.verdict;
  if (v) {
    const look = VERDICT_LOOK[v];
    return (
      <span className={`Label Label--${look.variant} Label--small stop-answer`}>
        {look.icon}
        <span className="stop-answer-text">{VERDICT_TEXT[v]}</span>
      </span>
    );
  }
  const done = stopDone(stop, state); // its finding was dismissed elsewhere
  return (
    <span className="Label Label--secondary Label--small stop-answer">
      {done ? <IconCheck size={12} /> : <IconCircle size={12} />}
      <span className="stop-answer-text">{done ? "Dismissed" : "Open"}</span>
    </span>
  );
}

function StopRow({ stop, n, current, fresh }: { stop: Stop; n: number; current: boolean; fresh: boolean }) {
  const ref = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    if (current) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [current]);
  return (
    <a
      ref={ref}
      href={reviewHref(stop.id)}
      className={`tree-item stop-row${current ? " active" : ""}`}
      aria-current={current ? "step" : undefined}
      title={stop.question.replace(/`/g, "")}
    >
      <span className="tree-line1">
        <span className="tree-group-num">{n}</span>
        <span className="tree-name stop-row-text">{whereText(stop)}</span>
        <span className="Label Label--secondary Label--small stop-row-text">{KIND_TEXT[stop.kind]}</span>
      </span>
      <span className="tree-line2">
        <AnswerLabel stop={stop} />
        {fresh && <span className="Label Label--accent Label--small stop-row-text">new</span>}
        {stop.dropped && <span className="Label Label--attention Label--small stop-row-text">dropped</span>}
      </span>
    </a>
  );
}

export function StopList({ model, currentId }: { model: ReviewModel; currentId: string | null }) {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const includeLow = useStore((s) => s.includeLow);
  const setIncludeLow = useStore((s) => s.setIncludeLow);
  const fresh = useStore((s) => s.freshStops);
  const [openMore, setOpenMore] = useState(false);
  const [openEarlier, setOpenEarlier] = useState(false);
  if (!analysis) return <nav className="pane review-path stop-list" aria-label="Review stops" />;

  const { main, more, earlier } = model;
  const answered = main.filter((s) => stopDone(s, state)).length;
  const moreOpen = openMore || more.some((s) => s.id === currentId);
  const earlierOpen = openEarlier || earlier.some((s) => s.id === currentId);
  // "61 files have nothing flagged →": the first such file in the reading order, in Files changed.
  const quiet = analysis.review_summary?.quiet_files ?? 0;
  const flagged = new Set(analysis.stops.map((s) => s.path));
  const firstQuiet = reviewOrder(analysis).find((p) => !flagged.has(p));

  return (
    <nav className="pane review-path stop-list" aria-label="Review stops">
      <div className="stop-list-panel">
        <div className="tree-tools stop-row-text">
          <div className="tree-heading">Review stops</div>
          <div className="tree-showing">
            {answered} of {main.length} answered
          </div>
          <div className="tree-toggles">
            <label className={`tree-toggle${model.low === 0 ? " disabled" : ""}`} title="Findings the second look scored low or wasn't sure about. Files changed is not affected.">
              <input
                type="checkbox"
                role="switch"
                checked={includeLow}
                disabled={model.low === 0 && !includeLow}
                onChange={(e) => setIncludeLow(e.target.checked)}
              />
              Include low-confidence findings
              <span className="Counter">{model.low}</span>
            </label>
          </div>
        </div>
        <div className="tree-scroll">
          {main.map((s, i) => (
            <StopRow key={s.id} stop={s} n={i + 1} current={s.id === currentId} fresh={fresh.includes(s.id)} />
          ))}
          {more.length > 0 && (
            <div className="tree-skim">
              <button className="tree-skim-toggle" aria-expanded={moreOpen} onClick={() => setOpenMore(!moreOpen)}>
                {moreOpen ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                <span className="stop-row-text">
                  {more.length} more place{more.length > 1 ? "s" : ""}
                </span>
              </button>
              {moreOpen &&
                more.map((s, i) => (
                  <StopRow key={s.id} stop={s} n={main.length + i + 1} current={s.id === currentId} fresh={fresh.includes(s.id)} />
                ))}
            </div>
          )}
          {earlier.length > 0 && (
            <div className="tree-skim">
              <button className="tree-skim-toggle" aria-expanded={earlierOpen} onClick={() => setOpenEarlier(!earlierOpen)}>
                {earlierOpen ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                <span className="stop-row-text">
                  {earlier.length} earlier answer{earlier.length > 1 ? "s" : ""}
                </span>
              </button>
              {earlierOpen &&
                earlier.map((s, i) => (
                  <StopRow key={s.id} stop={s} n={main.length + more.length + i + 1} current={s.id === currentId} fresh={false} />
                ))}
            </div>
          )}
          {quiet > 0 && firstQuiet && (
            <a className="tree-showing stop-quiet stop-row-text" href={fileHref(firstQuiet)}>
              {quiet} file{quiet === 1 ? " has" : "s have"} nothing flagged →
            </a>
          )}
        </div>
      </div>
    </nav>
  );
}
