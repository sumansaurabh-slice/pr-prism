import { useStore } from "../store";

export function ProgressBanner() {
  const analysis = useStore((s) => s.analysis);
  const error = useStore((s) => s.analysisError);
  if (error && !analysis) return <div className="flash flash-error">Could not load the review: {error}</div>;
  if (!analysis) return null;
  const p = analysis.progress;
  if (p.status === "complete") return null;
  const last = p.log.length ? p.log[p.log.length - 1] : "";
  if (p.status === "failed") {
    return (
      <div className="flash flash-error">
        <strong>Claude could not finish reading this PR.</strong> {p.message || last} You can still read the diffs and use
        the chat. Run <code>/pr-review</code> again to retry.
      </div>
    );
  }
  const pct = p.clusters_total ? Math.round((p.clusters_done / p.clusters_total) * 100) : 0;
  const phase =
    p.status === "fetched"
      ? "Waiting for Claude to start…"
      : p.status === "synthesizing"
        ? "Claude is writing the summary, reading order and data flows…"
        : `Claude is reading the files: ${p.clusters_done} of ${p.clusters_total} groups done`;
  return (
    <div className="flash flash-info">
      <span className="spinner" aria-hidden />
      <span className="flash-main">{phase}</span>
      {p.status === "annotating" && (
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
