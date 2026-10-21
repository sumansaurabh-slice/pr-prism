import { useEffect, useMemo, useRef, useState } from "react";
import { IconCheck, IconChevronDown, IconChevronRight, IconSearch, StatusIcon } from "../icons";
import { InlineText } from "../markdown";
import { fileHref, useRoute } from "../router";
import { fileMap, isStaleReview, readLS, splitPath, useStore, writeLS } from "../store";
import type { ChapterFile, FileEntry } from "../types";

const RISK_TEXT = { high: "High risk", medium: "Medium risk", low: "Low risk" } as const;

function FileRow({ file, why, active }: { file: FileEntry; why: string; active: boolean }) {
  const reviewedMap = useStore((s) => s.state.reviewed);
  const drafts = useStore((s) => s.state.drafts.filter((d) => d.path === file.path).length);
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
      {(dir || drafts > 0 || stale || file.sensitive) && (
        <span className="tree-line2">
          {dir && <span className="tree-dir">{dir.replace(/\/$/, "")}</span>}
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
  const [hideViewed, setHideViewed] = useState(() => readLS("prr.hideReviewed") === "1");
  const [closed, setClosed] = useState<Record<string, boolean>>({});
  const [openSkim, setOpenSkim] = useState<Record<string, boolean>>({});
  const activePath = route.view === "file" ? route.path : null;

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
        ch: { id: "_other", title: "Other files", goal: "", files: [] },
        entries: leftovers.map((f) => ({ cf: { path: f.path, why_now: "" } as ChapterFile, file: f })),
      });
    }
    return out;
  }, [analysis]);

  // Moving to a file (j/k, a link, the chat) opens its chapter if it was folded.
  useEffect(() => {
    if (!activePath || !analysis) return;
    const ch = analysis.chapters.find((c) => c.files.some((f) => f.path === activePath));
    if (ch) setClosed((c) => (c[ch.id] ? { ...c, [ch.id]: false } : c));
  }, [activePath, analysis]);

  if (!analysis) return <nav className="pane review-path" />;

  const needle = filter.trim().toLowerCase();
  const visible = (f: FileEntry) =>
    (!needle || f.path.toLowerCase().includes(needle)) &&
    (!hideViewed || !(f.path in reviewedMap) || isStaleReview(reviewedMap, f) || f.path === activePath);

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
        <label className="tree-toggle" title="Hide files you have marked as viewed">
          <input
            type="checkbox"
            checked={hideViewed}
            onChange={(e) => {
              setHideViewed(e.target.checked);
              writeLS("prr.hideReviewed", e.target.checked ? "1" : "0");
            }}
          />
          Hide viewed files
        </label>
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
