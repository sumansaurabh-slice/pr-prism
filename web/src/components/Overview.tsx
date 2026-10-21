import { useState } from "react";
import { InlineText, Markdown, RefLink } from "../markdown";
import { goFile } from "../router";
import { fileMap, fmtMinutes, splitPath, totalMinutes, useStore } from "../store";
import type { Ref } from "../types";
import { RiskBadge } from "./Header";

export function RefList({ refs }: { refs: Ref[] }) {
  if (!refs.length) return null;
  return (
    <span className="ref-list">
      {refs.map((r, i) => (
        <RefLink key={i} refInfo={{ path: r.path, line: r.line, end: null }} side={r.at === "base" ? "LEFT" : "RIGHT"}>
          <code>
            {splitPath(r.path).base}
            {r.line ? `:${r.line}` : ""}
          </code>
        </RefLink>
      ))}
    </span>
  );
}

export function Overview() {
  const analysis = useStore((s) => s.analysis);
  const reviewed = useStore((s) => s.state.reviewed);
  const [showBody, setShowBody] = useState(false);
  if (!analysis) return <div className="empty">Loading…</div>;
  const { pr, summary } = analysis;
  const files = fileMap(analysis);
  const pending = analysis.status !== "complete" && analysis.status !== "failed";

  return (
    <div className="overview">
      {analysis.focus && (
        <div className="focus-box">
          <span className="section-label">Your focus</span> {analysis.focus}
        </div>
      )}

      {summary ? (
        <>
          <section className="ov-tldr">
            <div className="ov-tldr-head">
              <h2>TL;DR</h2>
              <RiskBadge risk={summary.risk} />
            </div>
            <Markdown text={summary.tldr} />
          </section>
          <div className="ov-grid">
            <section className="ov-card">
              <h3>Why this change</h3>
              <Markdown text={summary.why} />
            </section>
            <section className="ov-card">
              <h3>How it works</h3>
              <Markdown text={summary.how} />
            </section>
          </div>
          {summary.risk_reasons.length > 0 && (
            <section className="ov-card ov-risk">
              <h3>
                Risk <RiskBadge risk={summary.risk} label={false} />
              </h3>
              <ul>
                {summary.risk_reasons.map((r, i) => (
                  <li key={i}>
                    <Markdown text={r} className="inline-md" />
                  </li>
                ))}
              </ul>
            </section>
          )}
        </>
      ) : (
        <section className="ov-card ov-pending">
          <h3>{pending ? "Analysis in progress" : "No summary"}</h3>
          <p className="dim">
            {pending
              ? "Claude is still reading the PR. The review path and diffs work now. The summary, data flows and impact appear here when the analysis finishes."
              : "The analysis didn’t produce a summary. The diffs and the chat still work."}
          </p>
        </section>
      )}

      <section className="ov-stats">
        <div className="stat">
          <span className="stat-num">{analysis.files.length}</span>
          <span className="stat-label">files</span>
        </div>
        <div className="stat">
          <span className="stat-num add">+{pr.additions}</span>
          <span className="stat-label">added</span>
        </div>
        <div className="stat">
          <span className="stat-num del">−{pr.deletions}</span>
          <span className="stat-label">deleted</span>
        </div>
        <div className="stat">
          <span className="stat-num">{fmtMinutes(totalMinutes(analysis))}</span>
          <span className="stat-label">est. review</span>
        </div>
        <div className="stat">
          <span className="stat-num">{pr.commits.length}</span>
          <span className="stat-label">commits</span>
        </div>
      </section>

      <section>
        <h3 className="section-title">Review roadmap</h3>
        <div className="roadmap">
          {analysis.chapters.map((ch, i) => {
            const paths = ch.files.map((f) => f.path).filter((p) => files.has(p));
            const done = paths.filter((p) => p in reviewed).length;
            const mins = paths.reduce((a, p) => a + (files.get(p)?.minutes ?? 0), 0);
            const firstOpen = paths.find((p) => !(p in reviewed)) ?? paths[0];
            return (
              <button
                key={ch.id}
                className={`roadmap-card${done === paths.length && paths.length ? " done" : ""}`}
                onClick={() => firstOpen && goFile(firstOpen)}
                disabled={!paths.length}
              >
                <span className="rm-num">{i + 1}</span>
                <span className="rm-body">
                  <span className="rm-title">
                    <InlineText text={ch.title} />
                  </span>
                  {ch.goal && (
                    <span className="rm-goal">
                      <InlineText text={ch.goal} />
                    </span>
                  )}
                  <span className="rm-meta">
                    {paths.length} file{paths.length === 1 ? "" : "s"} · {fmtMinutes(mins)} · {done}/{paths.length} reviewed
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {analysis.questions.length > 0 && (
        <section className="ov-card">
          <h3>Questions for the author</h3>
          <ol className="questions">
            {analysis.questions.map((q, i) => (
              <li key={i}>
                <Markdown text={q.text} className="inline-md" />
                <RefList refs={q.refs} />
              </li>
            ))}
          </ol>
        </section>
      )}

      <section className="ov-card">
        <h3>Commits</h3>
        <ul className="commits">
          {pr.commits.map((c) => (
            <li key={c.sha}>
              <code className="dim">{c.sha.slice(0, 7)}</code> {c.headline} <span className="dim">— {c.author}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="ov-card">
        <button className="link-btn" onClick={() => setShowBody((v) => !v)}>
          {showBody ? "▾" : "▸"} PR description
        </button>
        {showBody && (pr.body.trim() ? <Markdown text={pr.body} /> : <p className="dim">No description.</p>)}
      </section>

      {analysis.repair_log.length > 0 && (
        <details className="repair-log">
          <summary>Analysis repair log ({analysis.repair_log.length})</summary>
          <ul>
            {analysis.repair_log.map((l, i) => (
              <li key={i}>{l}</li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
