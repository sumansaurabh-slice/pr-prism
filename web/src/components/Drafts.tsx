import { useMemo } from "react";
import { reviewOrder, useStore } from "../store";
import type { Draft } from "../types";
import { IconX } from "../icons";
import { DraftCard } from "./FileView";

export function DraftsDrawer() {
  const open = useStore((s) => s.draftsOpen);
  const drafts = useStore((s) => s.state.drafts);
  const analysis = useStore((s) => s.analysis);
  const setOpen = useStore((s) => s.setDraftsOpen);
  const setSubmitOpen = useStore((s) => s.setSubmitOpen);

  const grouped = useMemo(() => {
    const order = reviewOrder(analysis);
    const rank = new Map(order.map((p, i) => [p, i]));
    const byPath = new Map<string, Draft[]>();
    for (const d of drafts) byPath.set(d.path, [...(byPath.get(d.path) ?? []), d]);
    return [...byPath.entries()]
      .sort((a, b) => (rank.get(a[0]) ?? 1e9) - (rank.get(b[0]) ?? 1e9))
      .map(([path, ds]) => [path, ds.sort((a, b) => a.line - b.line)] as const);
  }, [drafts, analysis]);

  if (!open) return null;
  const unanchored = drafts.filter((d) => !d.anchored && !d.outdated).length;
  const outdated = drafts.filter((d) => d.outdated).length;
  return (
    <div className="overlay" onClick={() => setOpen(false)}>
      <aside className="drawer" onClick={(e) => e.stopPropagation()} aria-label="Pending comments">
        <header className="drawer-head">
          <h2>Pending comments ({drafts.length})</h2>
          <button className="btn-octicon" onClick={() => setOpen(false)} aria-label="Close">
            <IconX />
          </button>
        </header>
        {(unanchored > 0 || outdated > 0) && (
          <div className="drawer-note">
            {unanchored > 0 && (
              <p>
                {unanchored} comment{unanchored > 1 ? "s are" : " is"} on lines that are not in the diff. GitHub can't pin
                {unanchored > 1 ? " them" : " it"} to a line, so {unanchored > 1 ? "they go" : "it goes"} into your review
                summary as a link.
              </p>
            )}
            {outdated > 0 && (
              <p>
                {outdated > 1
                  ? `${outdated} comments are from an older commit. Re-place or delete them; they won't be sent until you do.`
                  : "1 comment is from an older commit. Re-place or delete it; it won't be sent until you do."}
              </p>
            )}
          </div>
        )}
        <div className="drawer-body">
          {!drafts.length && (
            <p className="color-muted">
              No comments yet. Click a line number in the diff and press <kbd>c</kbd>. Or add one of Claude's suggested
              comments to your review.
            </p>
          )}
          {grouped.map(([path, ds]) => (
            <section key={path} className="drawer-group">
              <h3 className="mono drawer-path">{path}</h3>
              {ds.map((d) => (
                <DraftCard key={d.id} d={d} compact />
              ))}
            </section>
          ))}
        </div>
        <footer className="drawer-foot">
          <button
            className="btn btn-primary"
            onClick={() => {
              setOpen(false);
              setSubmitOpen(true);
            }}
          >
            Submit review…
          </button>
        </footer>
      </aside>
    </div>
  );
}
