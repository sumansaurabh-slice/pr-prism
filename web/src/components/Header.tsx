import { fmtMinutes, totalMinutes, useStore } from "../store";

export function RiskBadge({ risk, label }: { risk: string | null | undefined; label?: boolean }) {
  if (!risk) return null;
  return <span className={`risk-badge risk-${risk}`}>{label === false ? risk : `risk: ${risk}`}</span>;
}

export function Header() {
  const analysis = useStore((s) => s.analysis);
  const state = useStore((s) => s.state);
  const setDraftsOpen = useStore((s) => s.setDraftsOpen);
  const setSubmitOpen = useStore((s) => s.setSubmitOpen);
  const setHelpOpen = useStore((s) => s.setHelpOpen);
  if (!analysis) return <header className="app-header" />;
  const { pr } = analysis;
  const total = analysis.files.length;
  const reviewed = analysis.files.filter((f) => f.path in state.reviewed).length;
  const pct = total ? Math.round((reviewed / total) * 100) : 0;
  const drafts = state.drafts.length;
  return (
    <header className="app-header">
      <div className="hdr-title">
        <a className="hdr-num" href={pr.url} target="_blank" rel="noreferrer noopener" title="Open on GitHub">
          #{pr.number}
        </a>
        <div className="hdr-text">
          <a className="hdr-pr-title" href={pr.url} target="_blank" rel="noreferrer noopener" title={pr.title}>
            {pr.title}
          </a>
          <div className="hdr-meta">
            <span>@{pr.author}</span>
            <span className="mono">
              {pr.base_ref} ← {pr.head_ref}
            </span>
            <span>
              <span className="add">+{pr.additions}</span> <span className="del">−{pr.deletions}</span> · {total} files
            </span>
            {pr.is_draft && <span className="pill">draft</span>}
          </div>
        </div>
      </div>
      <div className="hdr-right">
        <RiskBadge risk={analysis.summary?.risk} />
        <span className="hdr-minutes" title="Estimated review time">
          {fmtMinutes(totalMinutes(analysis))}
        </span>
        <div className="hdr-progress" title={`${reviewed} of ${total} files reviewed`}>
          <span className="hdr-progress-label">
            Reviewed {reviewed}/{total}
          </span>
          <span className="progress-track">
            <span className="progress-fill" style={{ width: `${pct}%` }} />
          </span>
        </div>
        <button className="btn" onClick={() => setDraftsOpen(true)}>
          Drafts <span className="count">{drafts}</span>
        </button>
        <button className="btn btn-primary" onClick={() => setSubmitOpen(true)}>
          Submit review
        </button>
        <button className="btn btn-icon" title="Keyboard shortcuts (?)" onClick={() => setHelpOpen(true)}>
          ?
        </button>
      </div>
    </header>
  );
}
