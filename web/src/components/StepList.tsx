// The Understand view's left pane: the steps grouped by chapter, each with its number, file, role and a ✓ once
// you said "Got it". Below 1100 px it is a rail of numbers and ✓ that opens on hover or focus (the Review's rail).
import { useEffect, useRef } from "react";
import { IconCheck } from "../icons";
import { InlineText } from "../markdown";
import { understandHref } from "../router";
import { partText, stepName, type UnderstandModel } from "../understand";
import type { UnderstandStep } from "../types";

function StepRow({ step, n, current, done, part }: {
  step: UnderstandStep; n: number; current: boolean; done: boolean; part: string | null;
}) {
  const ref = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    if (current) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [current]);
  // plain text: a row is a link, so the role's `code` can't hold file links of its own
  const role = step.kind === "group" ? `${step.files.length} file${step.files.length === 1 ? "" : "s"}` : step.role?.replace(/`|\*\*/g, "");
  const where = step.kind === "group" ? step.files.join("\n") : step.path;
  return (
    <a
      ref={ref}
      href={understandHref(step.id)}
      className={`tree-item stop-row und-row${current ? " active" : ""}${done ? " viewed" : ""}`}
      aria-current={current ? "step" : undefined}
      title={role ? `${where}\n${role}` : where}
    >
      <span className="tree-line1">
        <span className="tree-group-num">{n}</span>
        <span className="tree-name stop-row-text">{stepName(step)}</span>
        {part && <span className="Label Label--secondary Label--small stop-row-text">{part}</span>}
        {done && (
          <span className="tree-viewed" title="Understood">
            <IconCheck size={14} />
            <span className="sr-only">understood</span>
          </span>
        )}
      </span>
      {role && <span className="tree-line2 und-role stop-row-text">{role}</span>}
    </a>
  );
}

export function StepList({ model, currentId }: { model: UnderstandModel; currentId: string | null }) {
  const { steps, chapters, understood } = model;
  const done = steps.filter((s) => understood[s.id]).length;
  return (
    <nav className="pane review-path stop-list" aria-label="Understand steps">
      <div className="stop-list-panel">
        <div className="tree-tools stop-row-text">
          <div className="tree-heading">Understand the PR</div>
          <div className="tree-showing">
            {done} of {steps.length} understood
          </div>
        </div>
        <div className="tree-scroll">
          {chapters.map((ch) => (
            <section key={ch.id} className="und-chapter" aria-labelledby={`und-ch-${ch.id}`}>
              <h3 id={`und-ch-${ch.id}`} className="und-chapter-title stop-row-text" title={ch.goal ?? undefined}>
                Chapter {ch.number} · <InlineText text={ch.title} />
              </h3>
              {ch.steps.map((s) => (
                <StepRow
                  key={s.id}
                  step={s}
                  n={steps.indexOf(s) + 1}
                  current={s.id === currentId}
                  done={!!understood[s.id]}
                  part={partText(steps, s)}
                />
              ))}
            </section>
          ))}
        </div>
      </div>
    </nav>
  );
}
