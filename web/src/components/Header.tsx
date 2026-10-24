import { breakdownText, overLimitText, spentText } from "../cost";
import { IconMerged, IconPullRequest } from "../icons";
import { fmtMinutes, totalMinutes, useStore } from "../store";
import type { PRInfo } from "../types";

const RISK_VARIANT: Record<string, string> = { high: "danger", medium: "attention", low: "success" };

/** Risk as a GitHub-style label, e.g. "High risk". */
export function RiskBadge({ risk, label }: { risk: string | null | undefined; label?: boolean }) {
  if (!risk) return null;
  const text = label === false ? risk : `${risk[0].toUpperCase()}${risk.slice(1)} risk`;
  return <span className={`Label Label--${RISK_VARIANT[risk] ?? "secondary"}`}>{text}</span>;
}

function StateLabel({ pr }: { pr: PRInfo }) {
  if (pr.is_draft && pr.state === "OPEN") {
    return (
      <span className="State State--draft">
        <IconPullRequest /> Draft
      </span>
    );
  }
  if (pr.state === "MERGED") {
    return (
      <span className="State State--merged">
        <IconMerged /> Merged
      </span>
    );
  }
  if (pr.state === "CLOSED") {
    return (
      <span className="State State--closed">
        <IconPullRequest /> Closed
      </span>
    );
  }
  return (
    <span className="State State--open">
      <IconPullRequest /> Open
    </span>
  );
}

/** What Claude has cost on this PR so far: "$12.40 spent", with the analysis / chat / other split as a tooltip.
 * Over the [budget] limit it also shows a lasting "over your $10 limit" label. */
function Spend() {
  const cost = useStore((s) => s.cost);
  if (!cost) return null;
  return (
    <>
      <span className="gh-meta-muted" title={breakdownText(cost)}>
        {spentText(cost)}
      </span>
      {cost.over_budget && (
        <span className="Label Label--attention" title="A warning only: nothing is stopped. The limit is warn_usd under [budget] in config.toml.">
          {overLimitText(cost.warn_usd)}
        </span>
      )}
    </>
  );
}

export function Header() {
  const analysis = useStore((s) => s.analysis);
  const drafts = useStore((s) => s.state.drafts.length);
  const setDraftsOpen = useStore((s) => s.setDraftsOpen);
  const setSubmitOpen = useStore((s) => s.setSubmitOpen);
  const setHelpOpen = useStore((s) => s.setHelpOpen);
  if (!analysis) return <header className="gh-header" />;
  const { pr } = analysis;
  const commits = pr.commits.length;
  const verb = pr.state === "MERGED" ? "merged" : "wants to merge";
  const blockers = analysis.findings.filter((f) => f.severity === "blocker" && !f.low_confidence).length;
  const failingCI = (analysis.ci?.checks ?? []).filter((c) => c.state === "fail").length;
  return (
    <header className="gh-header">
      <div className="gh-header-row">
        <h1 className="gh-title">
          <a href={pr.url} target="_blank" rel="noreferrer noopener" title="Open on GitHub">
            {pr.title}
          </a>{" "}
          <span className="gh-num">#{pr.number}</span>
        </h1>
        <div className="gh-actions">
          <button className="btn btn-sm" title="Keyboard shortcuts (?)" onClick={() => setHelpOpen(true)}>
            Shortcuts
          </button>
          <button className="btn btn-sm" onClick={() => setDraftsOpen(true)} title="Comments you have written but not sent yet">
            Pending comments <span className="Counter">{drafts}</span>
          </button>
          <button className="btn btn-sm btn-primary" onClick={() => setSubmitOpen(true)}>
            Submit review
          </button>
        </div>
      </div>
      <div className="gh-meta">
        <StateLabel pr={pr} />
        <span className="gh-meta-text">
          <strong>{pr.author}</strong> {verb} {commits} commit{commits === 1 ? "" : "s"} into{" "}
          <span className="branch-name">{pr.base_ref}</span> from <span className="branch-name">{pr.head_ref}</span>
        </span>
        <RiskBadge risk={analysis.summary?.risk} />
        {blockers > 0 && (
          <a className="Label Label--danger" href="#/overview" title="Problems that should block this merge">
            {blockers} blocking
          </a>
        )}
        {failingCI > 0 && (
          <a className="Label Label--danger" href="#/overview" title="Failing CI checks">
            CI failing ({failingCI})
          </a>
        )}
        <span className="gh-meta-muted" title="Rough time to review every file">
          {fmtMinutes(totalMinutes(analysis))} to review
        </span>
        <Spend />
      </div>
    </header>
  );
}
