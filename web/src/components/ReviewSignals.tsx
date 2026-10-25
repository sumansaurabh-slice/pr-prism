import { useState } from "react";
import { api } from "../api";
import { IconCheck, IconX } from "../icons";
import { Markdown } from "../markdown";
import { fileHref } from "../router";
import { splitPath, useStore } from "../store";
import type { CICheck, Finding, Severity } from "../types";
import { findingAnswer, type FindingAnswer } from "../answers";
import {
  AnswerLabel, commentHref, DeepCheckDetail, DeepCheckLabel, openComment, SecondLookLabel, SeverityLabel,
} from "./FileView";
import { RefList } from "./Overview";

const RANK: Record<Severity, number> = { blocker: 0, major: 1, minor: 2, nit: 3 };

function where(path: string, line: number | null) {
  return `${splitPath(path).base}${line ? `:${line}` : ""}`;
}

/** Red banner when a rule-checked blocker was found. */
export function BlockerBanner() {
  const findings = useStore((s) => s.analysis?.findings ?? []);
  const blockers = findings.filter((f) => f.severity === "blocker" && !f.low_confidence);
  if (!blockers.length) return null;
  return (
    <div className="flash flash-error blocker-banner">
      <IconX />
      <div>
        <strong>
          {blockers.length} problem{blockers.length > 1 ? "s" : ""} should block this merge
        </strong>
        <ul>
          {blockers.map((f) => (
            <li key={f.id}>
              <a href={fileHref(f.path, f.line, f.side)}>{f.title}</a> <span className="color-muted">({where(f.path, f.line)})</span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}

/** One problem: the title's link covers the whole row, and "Open your comment" sits above it. */
function FindingRow({ f, answer, muted }: { f: Finding; answer: FindingAnswer; muted: boolean }) {
  const mine = answer.draft;
  return (
    <>
      <div className={`Box-row finding-row${muted ? " answered-not" : ""}`}>
        <SeverityLabel severity={f.severity} />
        <a className="finding-row-title finding-row-link" href={fileHref(f.path, f.line, f.side)}>
          {f.title}
        </a>
        <DeepCheckLabel f={f} small />
        <SecondLookLabel look={f.second_look} small />
        {f.status === "still-open" && <span className="Label Label--attention Label--small">still open</span>}
        <AnswerLabel answer={answer} small />
        {mine && (
          <a
            className="finding-row-answer"
            href={commentHref(mine)}
            onClick={(e) => {
              e.preventDefault();
              openComment(mine);
            }}
          >
            Open your comment
          </a>
        )}
        <span className="color-muted small mono">{where(f.path, f.line)}</span>
      </div>
      {(f.question || (f.scenario ?? []).length > 0) && (
        <div className="finding-row-deep">
          <DeepCheckDetail f={f} collapsed />
        </div>
      )}
    </>
  );
}

export function FindingsBox() {
  const findings = useStore((s) => s.analysis?.findings ?? []);
  const dismissed = useStore((s) => s.state.dismissed_suggestions);
  const stops = useStore((s) => s.analysis?.stops);
  const verdicts = useStore((s) => s.state.stop_verdicts);
  const drafts = useStore((s) => s.state.drafts);
  const [showAll, setShowAll] = useState(false);
  // Your answer shows on each row (design D20). "Not a problem" dismissed the finding; it stays listed, last.
  const answers = new Map(findings.map((f) => [f.id, findingAnswer(f, stops ?? [], verdicts ?? {}, drafts)]));
  const saidNot = (f: Finding) => answers.get(f.id)?.verdict === "not" && !answers.get(f.id)?.draft;
  const shown = findings.filter((f) => !f.low_confidence && (!dismissed.includes(f.id) || saidNot(f)));
  const low = findings.length - shown.length;
  if (!findings.length) return null;
  const open = shown.filter((f) => !saidNot(f));
  const counts = (["blocker", "major", "minor", "nit"] as Severity[]).map((s) => [s, open.filter((f) => f.severity === s).length] as const);
  const list = [...shown].sort((a, b) => Number(saidNot(a)) - Number(saidNot(b)) || RANK[a.severity] - RANK[b.severity]);
  const visible = showAll ? list : list.slice(0, 8);
  return (
    <section className="Box">
      <div className="Box-header">
        <h3 className="Box-title">Problems Claude found</h3>
        {counts.filter(([, n]) => n).map(([s, n]) => (
          <span key={s} className="finding-count">
            <SeverityLabel severity={s} /> {n}
          </span>
        ))}
        {low > 0 && <span className="color-muted small">+{low} low confidence (hidden)</span>}
      </div>
      {visible.map((f) => (
        <FindingRow key={f.id} f={f} answer={answers.get(f.id)!} muted={saidNot(f)} />
      ))}
      {list.length > 8 && (
        <div className="Box-row">
          <button className="btn-link" onClick={() => setShowAll((v) => !v)}>
            {showAll ? "Show fewer" : `Show all ${list.length}`}
          </button>
        </div>
      )}
    </section>
  );
}

function CheckIcon({ state }: { state: CICheck["state"] }) {
  if (state === "pass") return <span className="ci-icon pass"><IconCheck size={14} /></span>;
  if (state === "fail") return <span className="ci-icon fail"><IconX size={14} /></span>;
  return <span className={`ci-icon ${state}`}>●</span>;
}

const CAUSED_TEXT = { yes: "Caused by this PR", no: "Not caused by this PR", unclear: "Not clear if this PR caused it" } as const;

export function CIBox() {
  const ci = useStore((s) => s.analysis?.ci);
  const loadAnalysis = useStore((s) => s.loadAnalysis);
  const toast = useStore((s) => s.toast);
  const [busy, setBusy] = useState(false);
  const [showPassing, setShowPassing] = useState(false);
  const refresh = async () => {
    setBusy(true);
    try {
      await api.refreshCI();
      await loadAnalysis();
    } catch (e) {
      toast("error", (e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  const checks = ci?.checks ?? [];
  const failing = checks.filter((c) => c.state === "fail");
  const pending = checks.filter((c) => c.state === "pending");
  const rest = checks.filter((c) => c.state !== "fail" && c.state !== "pending");
  return (
    <section className="Box">
      <div className="Box-header">
        <h3 className="Box-title">CI checks</h3>
        {ci && (
          <span className="color-muted small">
            {failing.length} failing · {pending.length} running · {rest.filter((c) => c.state === "pass").length} passing
          </span>
        )}
        <button className="btn btn-sm ci-refresh" disabled={busy} onClick={() => void refresh()}>
          {busy ? "Reading…" : "Refresh"}
        </button>
      </div>
      {!ci && <div className="Box-body color-muted">Not read yet. Press Refresh, or wait for the analysis to finish.</div>}
      {[...failing, ...pending].map((c) => (
        <div key={c.name} className="Box-row ci-row">
          <div className="ci-line">
            <CheckIcon state={c.state} />
            <strong>{c.name}</strong>
            <span className="color-muted small">{c.app || (c.kind === "status" ? "status" : "check")}</span>
            {c.url && (
              <a className="small ci-link" href={c.url} target="_blank" rel="noreferrer noopener">
                Details
              </a>
            )}
          </div>
          {c.explanation ? (
            <div className="ci-explain">
              <span className={`Label Label--${c.explanation.caused_by_pr === "yes" ? "danger" : c.explanation.caused_by_pr === "no" ? "success" : "secondary"}`}>
                {CAUSED_TEXT[c.explanation.caused_by_pr]}
              </span>
              <Markdown text={c.explanation.summary} />
              <Markdown className="color-muted" text={c.explanation.cause} />
              <div>
                <strong>Fix:</strong> <Markdown className="inline-md" text={c.explanation.fix} />
              </div>
              <RefList refs={c.explanation.related.map((r) => ({ path: r.path, line: r.line, at: "head" as const }))} />
            </div>
          ) : (
            c.summary && <Markdown className="color-muted ci-summary" text={c.summary.slice(0, 600)} />
          )}
          {c.log_tail && (
            <details className="ci-log">
              <summary>End of the log</summary>
              <pre>{c.log_tail}</pre>
            </details>
          )}
        </div>
      ))}
      {rest.length > 0 && (
        <div className="Box-row">
          <button className="btn-link" onClick={() => setShowPassing((v) => !v)}>
            {showPassing ? "Hide" : "Show"} {rest.length} other check{rest.length > 1 ? "s" : ""}
          </button>
          {showPassing && (
            <ul className="ci-rest">
              {rest.map((c) => (
                <li key={c.name}>
                  <CheckIcon state={c.state} /> {c.name}{" "}
                  {c.url && (
                    <a className="small" href={c.url} target="_blank" rel="noreferrer noopener">
                      Details
                    </a>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  );
}

const VERDICT_TEXT = { agree: "Claude agrees", disagree: "Claude disagrees", unsure: "Claude isn't sure" } as const;
const VERDICT_VARIANT = { agree: "success", disagree: "danger", unsure: "secondary" } as const;

export function OtherToolsBox() {
  const found = useStore((s) => s.analysis?.bot_findings ?? []);
  if (!found.length) return null;
  const bots = [...new Set(found.map((b) => b.bot))];
  return (
    <section className="Box">
      <div className="Box-header">
        <h3 className="Box-title">Comments from other review tools</h3>
        <span className="color-muted small">{bots.map((b) => b.replace(/\[bot\]$/, "")).join(", ")}</span>
      </div>
      {found.map((b) => (
        <div key={b.comment_id} className="Box-row bot-row">
          <div className="bot-line">
            {b.severity && <SeverityLabel severity={b.severity} />}
            <a href={b.line ? fileHref(b.path, b.line, b.side ?? "RIGHT") : fileHref(b.path)}>{b.title}</a>
            <span className="color-muted small mono">{where(b.path, b.line)}</span>
            <a className="small" href={b.html_url} target="_blank" rel="noreferrer noopener">
              On GitHub
            </a>
          </div>
          {b.verdict && (
            <div className="bot-verdict">
              <span className={`Label Label--${VERDICT_VARIANT[b.verdict]}`}>{VERDICT_TEXT[b.verdict]}</span>
              {b.verdict_reason && <Markdown className="inline-md" text={b.verdict_reason} />}
            </div>
          )}
        </div>
      ))}
    </section>
  );
}

export function RecheckedBox() {
  const items = useStore((s) => s.analysis?.rechecked ?? []);
  if (!items.length) return null;
  const fixed = items.filter((r) => r.status === "fixed").length;
  return (
    <section className="Box">
      <div className="Box-header">
        <h3 className="Box-title">Earlier problems, re-checked on the latest commit</h3>
        <span className="color-muted small">
          {fixed} fixed · {items.length - fixed} still open
        </span>
      </div>
      {items.map((r, i) => (
        <div key={i} className="Box-row recheck-row">
          <span className={`Label Label--${r.status === "fixed" ? "success" : "attention"}`}>
            {r.status === "fixed" ? "Fixed" : "Still open"}
          </span>
          <span className="recheck-title">{r.title}</span>
          <span className="color-muted small mono">{where(r.path, r.line)}</span>
          <span className="color-muted small">
            {r.how}
            {r.author ? ` · comment by ${r.author}` : ""}
          </span>
          {r.html_url && (
            <a className="small" href={r.html_url} target="_blank" rel="noreferrer noopener">
              On GitHub
            </a>
          )}
        </div>
      ))}
    </section>
  );
}
