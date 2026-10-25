import { useState } from "react";
import { estimateText } from "../cost";
import { IconX } from "../icons";
import { useStore } from "../store";

/** After `prr clean` and a new fetch: "Restored your work from Oct 23: 3 chats, …" until you dismiss it. */
export function RestoreBanner() {
  const restored = useStore((s) => s.state.restored);
  const dismiss = useStore((s) => s.dismissRestored);
  if (!restored) return null;
  return (
    <div className="flash flash-info strip" role="status">
      <span className="strip-text">{restored.message}</span>
      <button className="btn-octicon" onClick={() => void dismiss()} aria-label="Dismiss" title="Dismiss">
        <IconX />
      </button>
    </div>
  );
}

/** GitHub has commits the analysis hasn't read (design D12): "New commits since you opened this · [Re-read the PR]
 * (about 6 min and $2, based on the last run; you can keep working)". Hidden while a run goes: the progress banner
 * follows it. */
export function NewCommitsBanner() {
  const head = useStore((s) => s.head);
  const reanalyze = useStore((s) => s.reanalyze);
  const [busy, setBusy] = useState(false);
  if (!head?.head_changed || head.running) return null;
  return (
    <div className="flash flash-info strip" role="status">
      <span className="strip-text new-commits">
        <strong>New commits since you opened this</strong>
        <span aria-hidden>·</span>
        <button
          className="btn btn-sm"
          disabled={busy}
          title="Fetch the new commits and have Claude read the PR again, in the background"
          onClick={() => {
            setBusy(true);
            void reanalyze().finally(() => setBusy(false));
          }}
        >
          Re-read the PR
        </button>
        {head.estimate && (
          <span className="color-muted">({estimateText(head.estimate)}, based on the last run; you can keep working)</span>
        )}
      </span>
    </div>
  );
}

export function ProgressBanner() {
  const analysis = useStore((s) => s.analysis);
  const error = useStore((s) => s.analysisError);
  const rereading = useStore((s) => s.head?.running ?? false);
  if (error && !analysis) return <div className="flash flash-error">Could not load the review: {error}</div>;
  if (!analysis) return null;
  const p = analysis.progress;
  if (rereading && (p.status === "complete" || p.status === "failed")) {
    // "Re-read the PR" fetches first; the analysis (and its progress) starts once the new commits are in.
    return (
      <div className="flash flash-info">
        <span className="spinner" aria-hidden />
        <span className="flash-main">Re-reading the PR: getting the new commits…</span>
        <span className="flash-log">You can keep working. The page updates when Claude is done.</span>
      </div>
    );
  }
  if (p.status === "complete") return null;
  const last = p.log.length ? p.log[p.log.length - 1] : "";
  if (p.status === "failed") {
    return (
      <div className="flash flash-error">
        <strong>Claude could not finish reading this PR.</strong> {p.message || last} You can still read the diffs and use
        the chat. Run <code>/pr-prism</code> again to retry.
      </div>
    );
  }
  const deep = p.status === "deepchecking";
  const [done, total] = deep ? [p.deepcheck_done ?? 0, p.deepcheck_total ?? 0] : [p.clusters_done, p.clusters_total];
  const pct = total ? Math.round((done / total) * 100) : 0;
  const phase =
    p.status === "fetched"
      ? "Waiting for Claude to start…"
      : p.status === "synthesizing"
        ? "Claude is writing the summary, reading order and data flows…"
        : deep
          ? total
            ? `Deep check: ${done} of ${total} areas done`
            : "Deep check: picking the riskiest areas…"
          : `Claude is reading the files: ${p.clusters_done} of ${p.clusters_total} groups done`;
  return (
    <div className="flash flash-info">
      <span className="spinner" aria-hidden />
      <span className="flash-main">{phase}</span>
      {(p.status === "annotating" || (deep && total > 0)) && (
        <span className="progress-track flash-track">
          <span className="progress-fill" style={{ width: `${pct}%` }} />
        </span>
      )}
      {p.clusters_failed > 0 && <span className="Label Label--danger">{p.clusters_failed} failed</span>}
      <span className="flash-log" title={last}>
        {p.message || last}
      </span>
    </div>
  );
}
