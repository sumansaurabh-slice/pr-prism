import { useStore } from "../store";

export function ProgressBanner() {
  const analysis = useStore((s) => s.analysis);
  const error = useStore((s) => s.analysisError);
  if (error && !analysis) return <div className="banner banner-error">Could not load the analysis: {error}</div>;
  if (!analysis) return null;
  const p = analysis.progress;
  if (p.status === "complete") return null;
  const last = p.log.length ? p.log[p.log.length - 1] : "";
  if (p.status === "failed") {
    return (
      <div className="banner banner-error">
        <strong>Analysis failed.</strong> {p.message || last} — the diff and review path still work; re-run{" "}
        <code>/pr-review</code> to retry.
      </div>
    );
  }
  const pct = p.clusters_total ? Math.round((p.clusters_done / p.clusters_total) * 100) : 0;
  const phase =
    p.status === "fetched"
      ? "Waiting for analysis to start…"
      : p.status === "synthesizing"
        ? "Claude is writing the overview, review path and data flows…"
        : `Claude is annotating files… ${p.clusters_done}/${p.clusters_total} batches`;
  return (
    <div className="banner banner-info">
      <span className="spinner" aria-hidden />
      <span className="banner-main">{phase}</span>
      {p.status === "annotating" && (
        <span className="progress-track banner-track">
          <span className="progress-fill" style={{ width: `${pct}%` }} />
        </span>
      )}
      {p.clusters_failed > 0 && <span className="banner-warn">{p.clusters_failed} failed</span>}
      <span className="banner-log" title={last}>
        {p.message || last}
      </span>
    </div>
  );
}
