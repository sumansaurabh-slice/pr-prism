import { useState } from "react";
import { Avatar, DiffStat, IconCheck, IconChevronDown, IconChevronRight } from "../icons";
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

function SidebarItem({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="sidebar-item">
      <div className="sidebar-title">{title}</div>
      <div className="sidebar-body">{children}</div>
    </div>
  );
}

export function Overview() {
  const analysis = useStore((s) => s.analysis);
  const reviewed = useStore((s) => s.state.reviewed);
  const [showBody, setShowBody] = useState(false);
  if (!analysis) return <div className="blankslate">Loading…</div>;
  const { pr, summary } = analysis;
  const files = fileMap(analysis);
  const pending = analysis.status !== "complete" && analysis.status !== "failed";
  const viewed = analysis.files.filter((f) => f.path in reviewed).length;

  return (
    <div className="overview">
      <div className="overview-main">
        {summary ? (
          <section className="timeline-comment">
            <div className="tc-head">
              <Avatar name="Claude" kind="claude" size={24} />
              <strong>Claude</strong>
              <span className="color-muted">summarized this pull request</span>
              <span className="tc-head-right">
                <RiskBadge risk={summary.risk} />
              </span>
            </div>
            <div className="tc-body markdown-body">
              <Markdown text={summary.tldr} />
              <h3>Why this change was made</h3>
              <Markdown text={summary.why} />
              <h3>How it works</h3>
              <Markdown text={summary.how} />
              {summary.risk_reasons.length > 0 && (
                <>
                  <h3>What could go wrong</h3>
                  <ul>
                    {summary.risk_reasons.map((r, i) => (
                      <li key={i}>
                        <Markdown text={r} className="inline-md" />
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          </section>
        ) : (
          <section className="timeline-comment">
            <div className="tc-head">
              <Avatar name="Claude" kind="claude" size={24} />
              <strong>Claude</strong>
              <span className="color-muted">{pending ? "is reading this pull request" : "could not write a summary"}</span>
            </div>
            <div className="tc-body">
              <p className="color-muted">
                {pending
                  ? "You can already open the files and use the chat. The summary, data flows and impact will show up here when Claude is done."
                  : "You can still read the files and use the chat."}
              </p>
            </div>
          </section>
        )}

        <section className="Box">
          <div className="Box-header">
            <h3 className="Box-title">Reading order</h3>
            <span className="color-muted">Read the groups from top to bottom. Each one builds on the ones before it.</span>
          </div>
          <ol className="plan-list">
            {analysis.chapters.map((ch, i) => {
              const paths = ch.files.map((f) => f.path).filter((p) => files.has(p));
              const done = paths.filter((p) => p in reviewed).length;
              const mins = paths.reduce((a, p) => a + (files.get(p)?.minutes ?? 0), 0);
              const firstOpen = paths.find((p) => !(p in reviewed)) ?? paths[0];
              const complete = done === paths.length && paths.length > 0;
              return (
                <li key={ch.id} className={`plan-row${complete ? " done" : ""}`}>
                  <button className="plan-button" onClick={() => firstOpen && goFile(firstOpen)} disabled={!paths.length}>
                    <span className="plan-num">{complete ? <IconCheck size={14} /> : i + 1}</span>
                    <span className="plan-text">
                      <span className="plan-title">
                        <InlineText text={ch.title} />
                      </span>
                      {ch.goal && (
                        <span className="plan-goal">
                          <InlineText text={ch.goal} />
                        </span>
                      )}
                    </span>
                    <span className="plan-meta">
                      {done}/{paths.length} viewed · {fmtMinutes(mins)}
                    </span>
                  </button>
                </li>
              );
            })}
          </ol>
        </section>

        {analysis.questions.length > 0 && (
          <section className="Box">
            <div className="Box-header">
              <h3 className="Box-title">Questions to ask the author</h3>
            </div>
            <ol className="Box-body questions">
              {analysis.questions.map((q, i) => (
                <li key={i}>
                  <Markdown text={q.text} className="inline-md" />
                  <RefList refs={q.refs} />
                </li>
              ))}
            </ol>
          </section>
        )}

        <section className="timeline-comment">
          <button className="tc-head tc-head-button" onClick={() => setShowBody((v) => !v)} aria-expanded={showBody}>
            <Avatar name={pr.author} size={24} />
            <strong>{pr.author}</strong>
            <span className="color-muted">wrote the PR description</span>
            <span className="tc-head-right color-muted">
              {showBody ? <IconChevronDown /> : <IconChevronRight />} {showBody ? "Hide" : "Show"}
            </span>
          </button>
          {showBody && (
            <div className="tc-body markdown-body">
              {pr.body.trim() ? <Markdown text={pr.body} /> : <p className="color-muted">No description.</p>}
            </div>
          )}
        </section>

        <section className="Box">
          <div className="Box-header">
            <h3 className="Box-title">Commits</h3>
            <span className="Counter">{pr.commits.length}</span>
          </div>
          <ul className="commit-list">
            {pr.commits.map((c) => (
              <li key={c.sha} className="Box-row commit-row">
                <span className="commit-title">{c.headline}</span>
                <span className="color-muted">{c.author}</span>
                <code className="commit-sha">{c.sha.slice(0, 7)}</code>
              </li>
            ))}
          </ul>
        </section>

        {analysis.repair_log.length > 0 && (
          <details className="repair-log">
            <summary>Small fixes made to Claude's output ({analysis.repair_log.length})</summary>
            <ul>
              {analysis.repair_log.map((l, i) => (
                <li key={i}>{l}</li>
              ))}
            </ul>
          </details>
        )}
      </div>

      <aside className="overview-sidebar">
        {analysis.focus && (
          <SidebarItem title="Your focus">
            <span>{analysis.focus}</span>
          </SidebarItem>
        )}
        <SidebarItem title="Risk">
          {summary ? <RiskBadge risk={summary.risk} /> : <span className="color-muted">Not known yet</span>}
        </SidebarItem>
        <SidebarItem title="Time to review">
          <span>{fmtMinutes(totalMinutes(analysis))}</span>
        </SidebarItem>
        <SidebarItem title="Size">
          <span>
            {analysis.files.length} files · {pr.commits.length} commits
          </span>
          <DiffStat additions={pr.additions} deletions={pr.deletions} />
        </SidebarItem>
        <SidebarItem title="Your progress">
          <span>
            {viewed} of {analysis.files.length} files viewed
          </span>
          <span className="progress-track sidebar-progress">
            <span className="progress-fill" style={{ width: `${analysis.files.length ? (viewed / analysis.files.length) * 100 : 0}%` }} />
          </span>
        </SidebarItem>
        <SidebarItem title="Labels">
          {pr.labels.length ? (
            <span className="label-list">
              {pr.labels.map((l) => (
                <span key={l} className="IssueLabel">
                  {l}
                </span>
              ))}
            </span>
          ) : (
            <span className="color-muted">None yet</span>
          )}
        </SidebarItem>
      </aside>
    </div>
  );
}
