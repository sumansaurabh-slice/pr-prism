// The Understand view ("Understand this PR"): the steps by chapter on the left, one step at a time in the middle,
// and the end screen that hands off to the review. Its keys (y or →, c, j, k) work here only.
import { useEffect, useRef, useState, type MutableRefObject } from "react";
import { registerUnderstandNav, type UnderstandNav } from "../keyboard";
import { navigate, replaceRoute, reviewHref, understandHref, type Route } from "../router";
import { useStore } from "../store";
import { firstToRead, stepTitle, useUnderstandModel, type UnderstandModel } from "../understand";
import { analysisRunning } from "../review";
import { StepList } from "./StepList";
import { UnderstandStepView } from "./UnderstandStep";

type UnderstandRoute = Extract<Route, { view: "understand" }>;

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** "You've walked through all N steps": the steps you skipped, and Start review. */
function UnderstandEnd({ model, navRef }: { model: UnderstandModel; navRef: MutableRefObject<Partial<UnderstandNav>> }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => headingRef.current?.focus(), []);
  const { steps, chapters, understood } = model;
  const skipped = steps.filter((s) => !understood[s.id]);
  const last = steps[steps.length - 1];
  navRef.current = { prev: () => last && navigate(understandHref(last.id)) };
  return (
    <section className="review-stop review-end" aria-labelledby="und-end-title">
      <header className="stop-head">
        <h2 id="und-end-title" className="stop-question" tabIndex={-1} ref={headingRef}>
          You've walked through all {plural(steps.length, "step")}
        </h2>
        <p className="color-muted">
          {steps.length - skipped.length} understood · {skipped.length} skipped
        </p>
      </header>

      {skipped.length > 0 && (
        <section aria-labelledby="und-skipped-title">
          <h3 id="und-skipped-title" className="finding-scenario-title small">
            Steps you skipped
          </h3>
          <ul className="stop-skipped">
            {skipped.map((s) => {
              const ch = chapters.find((c) => c.id === s.chapter_id);
              return (
                <li key={s.id}>
                  <a href={understandHref(s.id)}>
                    Step {steps.indexOf(s) + 1} · <code>{stepTitle(s)}</code>
                  </a>
                  {ch && (
                    <span className="color-muted">
                      {" "}
                      · Chapter {ch.number}: {ch.title.replace(/`/g, "")}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      <div>
        <button className="btn btn-primary" onClick={() => navigate(reviewHref())}>
          Start review
        </button>
      </div>
    </section>
  );
}

export function UnderstandView({ route }: { route: UnderstandRoute }) {
  const analysis = useStore((s) => s.analysis);
  const understand = useStore((s) => s.understand);
  const error = useStore((s) => s.understandError);
  const model = useUnderstandModel(route.stepId);
  const navRef = useRef<Partial<UnderstandNav>>({});
  const [announce, setAnnounce] = useState("");

  // The keys go to whatever is on screen (a step or the end screen); none of them leaks to the file keys.
  useEffect(() => {
    const call = (k: keyof UnderstandNav) => () => navRef.current[k]?.();
    registerUnderstandNav({ gotIt: call("gotIt"), chat: call("chat"), next: call("next"), prev: call("prev") });
    return () => registerUnderstandNav(null);
  }, []);

  // #/understand resumes: the first step not understood yet, or the end screen when every step is understood.
  const resumeTo = !route.stepId && !route.end && understand ? firstToRead(model.steps, model.understood) : null;
  useEffect(() => {
    if (resumeTo) replaceRoute(understandHref(resumeTo.id));
  }, [resumeTo?.id]); // once per target, not on every reload of the steps

  // "Step 4 of 23", read out on each move (the heading takes focus).
  const total = model.steps.length;
  useEffect(() => {
    if (route.stepId && model.index !== -1) setAnnounce(`Step ${model.index + 1} of ${total}`);
    else if (route.end) setAnnounce(`You've walked through all ${plural(total, "step")}`);
  }, [route.stepId, route.end, model.index !== -1]); // only on a move (or when the steps arrive), not on a reload

  navRef.current = {}; // the step or end screen below sets its own keys as it renders
  let main;
  if (!understand && error) {
    main = (
      <div className="blankslate" role="status">
        <p>Couldn't load the steps: {error}</p>
        <button className="btn" onClick={() => void useStore.getState().loadUnderstand()}>
          Try again
        </button>
      </div>
    );
  } else if (!understand || !analysis) main = <div className="blankslate">Loading the steps…</div>;
  else if (route.stepId && model.current) {
    main = <UnderstandStepView key={model.current.id} step={model.current} model={model} navRef={navRef} />;
  } else if (route.stepId) {
    main = (
      <div className="blankslate" role="status">
        <p>This step isn't part of the walk any more.</p>
        <a className="btn" href={understandHref()}>
          Go to the next step
        </a>
      </div>
    );
  } else if (!total) {
    main = (
      <div className="blankslate" role="status">
        {analysisRunning(analysis)
          ? "Claude is still reading the PR. The steps will show here once the reading order is ready."
          : "This PR has no files to walk through."}
      </div>
    );
  } else if (resumeTo) main = <div className="blankslate">Opening the next step…</div>;
  else main = <UnderstandEnd model={model} navRef={navRef} />;

  return (
    <>
      <StepList model={model} currentId={route.stepId} />
      <main className="pane center review-main">
        <div className="center-body">{main}</div>
        <div className="sr-only" aria-live="polite">
          {announce}
        </div>
      </main>
    </>
  );
}
