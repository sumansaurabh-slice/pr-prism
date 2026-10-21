import { InlineText, Markdown } from "../markdown";
import { useStore } from "../store";
import type { ImpactKind, ImpactItem, Level } from "../types";
import { RefList } from "./Overview";

const KIND_LABEL: Record<ImpactKind, string> = {
  breaking: "Breaking changes",
  security: "Security",
  migration: "Migrations & data",
  callers: "Callers outside this PR",
  config: "Config & environment",
  perf: "Performance",
  tests: "Test coverage",
  sensitive: "Sensitive paths touched",
  other: "Other",
};
const KIND_ORDER: ImpactKind[] = ["breaking", "security", "migration", "callers", "config", "perf", "tests", "sensitive", "other"];
const SEV_RANK: Record<Level, number> = { high: 0, medium: 1, low: 2 };

export function Impact() {
  const analysis = useStore((s) => s.analysis);
  if (!analysis) return <div className="empty">Loading…</div>;
  const groups = new Map<ImpactKind, ImpactItem[]>();
  for (const item of analysis.impact) groups.set(item.kind, [...(groups.get(item.kind) ?? []), item]);
  const pending = analysis.status !== "complete" && analysis.status !== "failed";
  if (!analysis.impact.length) {
    return (
      <div className="empty">
        {pending ? "Impact analysis appears when Claude finishes analyzing the PR." : "No notable impact was identified."}
      </div>
    );
  }
  const counts = { high: 0, medium: 0, low: 0 } as Record<Level, number>;
  for (const i of analysis.impact) counts[i.severity] += 1;
  return (
    <div className="impact">
      <div className="impact-summary">
        {(["high", "medium", "low"] as Level[]).map((l) =>
          counts[l] ? (
            <span key={l} className={`risk-badge risk-${l}`}>
              {counts[l]} {l}
            </span>
          ) : null,
        )}
      </div>
      {KIND_ORDER.filter((k) => groups.has(k)).map((kind) => (
        <section key={kind} className="impact-group">
          <h3 className="section-title">{KIND_LABEL[kind]}</h3>
          {[...groups.get(kind)!]
            .sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity])
            .map((item, i) => (
              <article key={i} className={`impact-card sev-stripe-${item.severity}`}>
                <header>
                  <span className={`risk-badge risk-${item.severity}`}>{item.severity}</span>
                  <span className="impact-title">
                    <InlineText text={item.title} />
                  </span>
                </header>
                <Markdown text={item.detail} />
                <RefList refs={item.refs} />
              </article>
            ))}
        </section>
      ))}
    </div>
  );
}
