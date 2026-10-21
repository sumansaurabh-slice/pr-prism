import { useEffect, useMemo, useRef, useState } from "react";
import { InlineText } from "../markdown";
import { fileHref, useRoute } from "../router";
import { fileMap, isStaleReview, readLS, splitPath, useStore, writeLS } from "../store";
import type { ChapterFile, FileEntry, FileStatus } from "../types";

const STATUS_LETTER: Record<FileStatus, string> = { added: "A", modified: "M", deleted: "D", renamed: "R", copied: "C" };

function FileRow({ file, why, active }: { file: FileEntry; why: string; active: boolean }) {
  const reviewedMap = useStore((s) => s.state.reviewed);
  const setReviewed = useStore((s) => s.setReviewed);
  const drafts = useStore((s) => s.state.drafts.filter((d) => d.path === file.path).length);
  const ref = useRef<HTMLDivElement>(null);
  const reviewed = file.path in reviewedMap;
  const stale = isStaleReview(reviewedMap, file);
  const { dir, base } = splitPath(file.path);
  useEffect(() => {
    if (active) ref.current?.scrollIntoView({ block: "nearest" });
  }, [active]);
  return (
    <div ref={ref} className={`rp-row${active ? " active" : ""}${reviewed && !stale ? " done" : ""}`}>
      <input
        type="checkbox"
        className="rp-check"
        checked={reviewed}
        title={reviewed ? "Mark as not reviewed" : "Mark as reviewed (r)"}
        onChange={(e) => void setReviewed(file.path, e.target.checked)}
      />
      <a className="rp-link" href={fileHref(file.path)} title={file.path}>
        <span className="rp-line1">
          <span className={`status-letter st-${file.status}`}>{STATUS_LETTER[file.status]}</span>
          <span className="rp-name">
            <span className="rp-dir">{dir}</span>
            <span className="rp-base">{base}</span>
          </span>
          {file.risk && <span className={`risk-dot risk-${file.risk}`} title={`risk: ${file.risk}`} />}
        </span>
        <span className="rp-line2">
          <span className="add">+{file.additions}</span>
          <span className="del">−{file.deletions}</span>
          {file.sensitive && <span className="tag tag-warn">sensitive</span>}
          {stale && <span className="tag tag-warn" title="The file changed after you marked it reviewed">changed since review</span>}
          {drafts > 0 && <span className="tag">{drafts} draft{drafts > 1 ? "s" : ""}</span>}
          {!file.annotated && !file.generated && <span className="tag tag-dim">not annotated</span>}
        </span>
        {active && why && (
          <span className="rp-why">
            <InlineText text={why} />
          </span>
        )}
      </a>
    </div>
  );
}

export function ReviewPath() {
  const analysis = useStore((s) => s.analysis);
  const reviewedMap = useStore((s) => s.state.reviewed);
  const route = useRoute();
  const [filter, setFilter] = useState("");
  const [hideReviewed, setHideReviewed] = useState(() => readLS("prr.hideReviewed") === "1");
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

  if (!analysis) return <nav className="pane review-path" />;

  const needle = filter.trim().toLowerCase();
  const visible = (f: FileEntry) =>
    (!needle || f.path.toLowerCase().includes(needle)) &&
    (!hideReviewed || !(f.path in reviewedMap) || isStaleReview(reviewedMap, f) || f.path === activePath);

  return (
    <nav className="pane review-path" aria-label="Review path">
      <div className="rp-tools">
        <input
          className="input rp-filter"
          placeholder="Filter files…"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
        />
        <label className="rp-toggle" title="Hide files you've marked reviewed">
          <input
            type="checkbox"
            checked={hideReviewed}
            onChange={(e) => {
              setHideReviewed(e.target.checked);
              writeLS("prr.hideReviewed", e.target.checked ? "1" : "0");
            }}
          />
          Hide reviewed
        </label>
      </div>
      <div className="rp-scroll">
        {chapters.map(({ ch, entries }, i) => {
          const main = entries.filter((e) => !e.file.generated && visible(e.file));
          const skim = entries.filter((e) => e.file.generated && visible(e.file));
          if (!main.length && !skim.length) return null;
          const done = entries.filter((e) => e.file.path in reviewedMap).length;
          const skimOpen = openSkim[ch.id] || skim.some((e) => e.file.path === activePath) || !!needle;
          return (
            <section key={ch.id} className="rp-chapter">
              <header className="rp-ch-head" title={ch.goal}>
                <span className="rp-ch-num">{i + 1}</span>
                <span className="rp-ch-title">
                  <InlineText text={ch.title} />
                </span>
                <span className="rp-ch-count">
                  {done}/{entries.length}
                </span>
              </header>
              {ch.goal && (
                <p className="rp-ch-goal">
                  <InlineText text={ch.goal} />
                </p>
              )}
              {main.map(({ cf, file }) => (
                <FileRow key={file.path} file={file} why={cf.why_now} active={file.path === activePath} />
              ))}
              {skim.length > 0 && (
                <div className="rp-skim">
                  <button
                    className="rp-skim-toggle"
                    onClick={() => setOpenSkim((s) => ({ ...s, [ch.id]: !skimOpen }))}
                  >
                    {skimOpen ? "▾" : "▸"} Skim: {skim.length} generated / vendored file{skim.length > 1 ? "s" : ""}
                  </button>
                  {skimOpen &&
                    skim.map(({ cf, file }) => (
                      <FileRow key={file.path} file={file} why={cf.why_now} active={file.path === activePath} />
                    ))}
                </div>
              )}
            </section>
          );
        })}
      </div>
    </nav>
  );
}
