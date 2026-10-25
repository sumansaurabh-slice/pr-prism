import { InlineText, Markdown } from "../markdown";
import { summaryPending, useStore } from "../store";
import type { ImpactKind, ImpactItem, Level } from "../types";
import { RiskBadge } from "./Header";
import { RefList } from "./Overview";

const KIND_LABEL: Record<ImpactKind, string> = {
  breaking: "Changes that can break other code",
  security: "Security",
  migration: "Database and data changes",
  callers: "Other code that uses what changed",
  config: "Settings and environment",
  perf: "Speed and memory",
  tests: "Tests",
  sensitive: "Files that need extra care",
  other: "Other",
};
const KIND_ORDER: ImpactKind[] = ["breaking", "security", "migration", "callers", "config", "perf", "tests", "sensitive", "other"];
const SEV_RANK: Record<Level, number> = { high: 0, medium: 1, low: 2 };

export function Impact() {
  const analysis = useStore((s) => s.analysis);
  if (!analysis) return <div className="blankslate">Loading…</div>;
  const groups = new Map<ImpactKind, ImpactItem[]>();
  for (const item of analysis.impact) groups.set(item.kind, [...(groups.get(item.kind) ?? []), item]);
  const pending = summaryPending(analysis);
  if (!analysis.impact.length) {
    return (
      <div className="blankslate">
        {pending ? "This will show up when Claude has finished reading the PR." : "Claude did not find anything outside the PR that this change affects."}
      </div>
    );
  }
  return (
    <div className="impact">
      <p className="page-intro">What this PR could affect outside its own lines. The riskiest items in each group come first.</p>
      {KIND_ORDER.filter((k) => groups.has(k)).map((kind) => {
        const items = [...groups.get(kind)!].sort((a, b) => SEV_RANK[a.severity] - SEV_RANK[b.severity]);
        return (
          <section key={kind} className="Box">
            <div className="Box-header">
              <h3 className="Box-title">{KIND_LABEL[kind]}</h3>
              <span className="Counter">{items.length}</span>
            </div>
            {items.map((item, i) => (
              <article key={i} className="Box-row impact-row">
                <div className="impact-row-head">
                  <RiskBadge risk={item.severity} />
                  <span className="impact-title">
                    <InlineText text={item.title} />
                  </span>
                </div>
                <Markdown text={item.detail} />
                <RefList refs={item.refs} />
              </article>
            ))}
          </section>
        );
      })}
    </div>
  );
}
