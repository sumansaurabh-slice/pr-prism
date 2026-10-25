import { useEffect, useMemo, useRef, useState } from "react";
import { riskySeenText, useCoverage, useCoverageSync } from "../coverage";
import { IconCheck, IconChevronDown, IconChevronRight, IconEye, IconSearch, StatusIcon } from "../icons";
import { InlineText } from "../markdown";
import { fileHref, useRoute } from "../router";
import { fileMap, hiddenByPrefs, isStaleReview, splitPath, useStore, type ViewPrefs } from "../store";
import type { ChapterFile, FileEntry } from "../types";

const OTHER_ID = "_other"; // the extra group for files the reading order left out
const RISK_TEXT = { high: "High risk", medium: "Medium risk", low: "Low risk" } as const;

/** "2 of 5 risky hunks seen" on a high-risk file (design issue 22): text plus an icon, never color alone. */
function RiskySeen({ path }: { path: string }) {
  const v = useCoverage((s) => s.coverage?.high_risk_unseen.by_file[path]);
  if (!v || !v.total) return null;
  const all = v.seen >= v.total;
  return (
    <span
      className={`tree-seen${all ? " done" : ""}`}
      title="Changed blocks of this high-risk file that were on your screen for 3 seconds, or where you answered a review stop"
    >
      {all ? <IconCheck size={12} /> : <IconEye size={12} />}
      {riskySeenText(v)}
    </span>
  );
}

function FileRow({ file, why, active }: { file: FileEntry; why: string; active: boolean }) {
  const reviewedMap = useStore((s) => s.state.reviewed);
  const drafts = useStore((s) => s.state.drafts.filter((d) => d.path === file.path).length);
  const risky = useCoverage((s) => !!s.coverage?.high_risk_unseen.by_file[file.path]?.total);
  const ref = useRef<HTMLAnchorElement>(null);
  const viewed = file.path in reviewedMap;
  const stale = isStaleReview(reviewedMap, file);
  const { dir, base } = splitPath(file.path);
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest" });
  }, [active]);
  return (
    <a
      ref={ref}
      href={fileHref(file.path)}
      title={file.path}
      className={`tree-item${active ? " active" : ""}${viewed && !stale ? " viewed" : ""}`}
      aria-current={active ? "page" : undefined}
    >
      <span className="tree-line1">
        <StatusIcon status={file.status} />
        <span className="tree-name">{base}</span>
        {file.risk && <span className={`risk-dot risk-${file.risk}`} title={RISK_TEXT[file.risk]} />}
        {viewed && !stale && (
          <span className="tree-viewed" title="Viewed">
            <IconCheck size={14} />
          </span>
        )}
      </span>
      {(dir || drafts > 0 || stale || file.sensitive || file.is_test || file.is_docs || risky) && (
        <span className="tree-line2">
          {dir && <span className="tree-dir">{dir.replace(/\/$/, "")}</span>}
          {(file.is_test || file.is_docs) && (
            <span className="Label Label--secondary Label--small" title={file.kind_reason ?? undefined}>
              {file.is_test ? "test" : "docs"}
            </span>
          )}
          {file.sensitive && <span className="Label Label--attention Label--small">needs care</span>}
          {stale && (
            <span className="Label Label--attention Label--small" title="This file changed after you marked it viewed">
              changed
            </span>
          )}
          {drafts > 0 && (
            <span className="Label Label--accent Label--small">
              {drafts} pending
            </span>
          )}
          <RiskySeen path={file.path} />
        </span>
      )}
      {active && why && (
        <span className="tree-why">
          <InlineText text={why} />
        </span>
      )}
    </a>
  );
}

export function ReviewPath() {
  const analysis = useStore((s) => s.analysis);
  const reviewedMap = useStore((s) => s.state.reviewed);
  const route = useRoute();
  const [filter, setFilter] = useState("");
  const prefs = useStore((s) => s.prefs);
  const setPref = useStore((s) => s.setPref);
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const [openSkim, setOpenSkim] = useState<Record<string, boolean>>({});
  const activePath = route.view === "file" ? route.path : null;
  useCoverageSync(); // "2 of 5 risky hunks seen" on high-risk files

  const chapters = useMemo(() => {
    if (!analysis) return [];
    const files = fileMap(analysis);
    const seen = new Set<string>();
    const out = analysis.chapters.map((ch) => {
      const entries = ch.files
        .filter((cf) => files.has(cf.path) && !seen.has(cf.path) && (seen.add(cf.path), true))
        .map((cf) => ({ cf, file: files.get(cf.path)! }));
      return { ch, entries };
    });
    const leftovers = analysis.files.filter((f) => !seen.has(f.path));
    if (leftovers.length) {
      out.push({
        ch: { id: OTHER_ID, title: "Other files", goal: "", files: [] },
        entries: leftovers.map((f) => ({ cf: { path: f.path, why_now: "" } as ChapterFile, file: f })),
      });
    }
    return out;
  }, [analysis]);

  // Moving to a file (j/k, a link, the chat) opens its chapter if it was folded. This runs only when the
  // open file changes, not on every analysis poll, so a chapter you fold stays folded.
  useEffect(() => {
    const current = useStore.getState().analysis;
    if (!activePath || !current) return;
    const id = current.chapters.find((c) => c.files.some((f) => f.path === activePath))?.id ?? OTHER_ID;
    setClosed((c) => (c[id] ? { ...c, [id]: false } : c));
  }, [activePath]);

  if (!analysis) return <nav className="pane review-path" />;

  const needle = filter.trim().toLowerCase();
  const visible = (f: FileEntry) =>
    (!needle || f.path.toLowerCase().includes(needle)) &&
    (f.path === activePath || !hiddenByPrefs(f, prefs, reviewedMap));
  const total = analysis.files.length;
  const shown = analysis.files.filter(visible).length;
  const testCount = analysis.files.filter((f) => f.is_test).length;
  const docsCount = analysis.files.filter((f) => f.is_docs).length;
  const toggle = (key: keyof ViewPrefs, label: string, title: string, count?: number) => (
    <label className={`tree-toggle${count === 0 ? " disabled" : ""}`} title={title}>
      <input type="checkbox" checked={prefs[key]} disabled={count === 0} onChange={(e) => setPref(key, e.target.checked)} />
      {label}
      {count !== undefined && <span className="Counter">{count}</span>}
    </label>
  );

  return (
    <nav className="pane review-path" aria-label="Files in reading order">
      <div className="tree-tools">
        <div className="tree-heading">Reading order</div>
        <label className="input-with-icon">
          <IconSearch />
          <input
            className="input input-sm"
            placeholder="Filter changed files"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </label>
        <div className="tree-toggles">
          {toggle("hideViewed", "Hide viewed files", "Hide files you have marked as viewed")}
          {toggle("hideTests", "Hide test files", testCount
            ? "Hide tests, test helpers, fixtures and eval files. Hover the \"test\" tag on a file to see why it counts."
            : "No test files in this PR", testCount)}
          {toggle("hideDocs", "Hide docs files", docsCount
            ? "Hide READMEs, Markdown files, changelogs and docs folders. Hover the \"docs\" tag on a file to see why it counts."
            : "No docs files in this PR", docsCount)}
        </div>
        {shown < total && !needle && (
          <div className="tree-showing">
            Showing {shown} of {total} files
          </div>
        )}
      </div>
      <div className="tree-scroll">
        {chapters.map(({ ch, entries }, i) => {
          const main = entries.filter((e) => !e.file.generated && visible(e.file));
          const skim = entries.filter((e) => e.file.generated && visible(e.file));
          if (!main.length && !skim.length) return null;
          const done = entries.filter((e) => e.file.path in reviewedMap).length;
          const isClosed = !!closed[ch.id] && !needle;
          const skimOpen = openSkim[ch.id] || skim.some((e) => e.file.path === activePath) || !!needle;
          return (
            <section key={ch.id} className="tree-group">
              <button
                className="tree-group-head"
                title={needle ? "Clear the filter to fold this group" : ch.goal}
                aria-expanded={!isClosed}
                disabled={!!needle}
                onClick={() => setClosed((c) => ({ ...c, [ch.id]: !isClosed }))}
              >
                {isClosed ? <IconChevronRight size={12} /> : <IconChevronDown size={12} />}
                <span className="tree-group-num">{i + 1}</span>
                <span className="tree-group-title">
                  <InlineText text={ch.title} />
                </span>
                <span className={`tree-group-count${done === entries.length ? " done" : ""}`}>
                  {done}/{entries.length}
                </span>
              </button>
              {!isClosed && (
                <>
                  {ch.goal && (
                    <p className="tree-group-goal">
                      <InlineText text={ch.goal} />
                    </p>
                  )}
                  {main.map(({ cf, file }) => (
                    <FileRow key={file.path} file={file} why={cf.why_now} active={file.path === activePath} />
                  ))}
                  {skim.length > 0 && (
                    <div className="tree-skim">
                      <button
                        className="tree-skim-toggle"
                        onClick={() => setOpenSkim((s) => ({ ...s, [ch.id]: !skimOpen }))}
                      >
                        {skimOpen ? <IconChevronDown size={12} /> : <IconChevronRight size={12} />}
                        {skim.length} generated or lock file{skim.length > 1 ? "s" : ""} (skim)
                      </button>
                      {skimOpen &&
                        skim.map(({ cf, file }) => (
                          <FileRow key={file.path} file={file} why={cf.why_now} active={file.path === activePath} />
                        ))}
                    </div>
                  )}
                </>
              )}
            </section>
          );
        })}
      </div>
    </nav>
  );
}
