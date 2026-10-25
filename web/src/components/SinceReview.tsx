// "Changes since your review" (E4, design issue 24): the toggle in the tabs bar, its banner, the Overview's lead box,
// and the status of your earlier comments. Data: GET /api/since-review (store.sinceReview).
import { useEffect } from "react";
import { InlineText } from "../markdown";
import { fileHref } from "../router";
import { useSinceView, useStore } from "../store";
import type { EarlierComment, EarlierCommentStatus, SinceReview } from "../types";

const STATUS: Record<EarlierCommentStatus, { text: string; variant: string }> = {
  fixed: { text: "Fixed", variant: "success" },
  "still-open": { text: "Still open", variant: "attention" },
  "line-unchanged": { text: "Line unchanged", variant: "secondary" },
  checking: { text: "Checking…", variant: "secondary" },
  "not-checked": { text: "Not checked", variant: "secondary" },
};

const short = (sha: string | null) => (sha ?? "").slice(0, 7);
const plural = (n: number, one: string) => `${n} ${one}${n === 1 ? "" : "s"}`;

/** "Since your review (abc1234 → def5678): 3 files · your comments: 2 fixed, 1 still open, 2 unchanged". */
export function sinceLine(sr: SinceReview): string {
  const count = (s: EarlierCommentStatus) => sr.comments.filter((c) => c.status === s).length;
  let line = `Since your review (${short(sr.reviewed_head)} → ${short(sr.head)}): ${plural(sr.files.length, "file")}`;
  if (sr.comments.length) {
    const parts = [`${count("fixed")} fixed`, `${count("still-open")} still open`, `${count("line-unchanged")} unchanged`];
    if (count("checking")) parts.push(`${count("checking")} checking…`);
    if (count("not-checked")) parts.push(`${count("not-checked")} not checked`);
    line += ` · your comments: ${parts.join(", ")}`;
  }
  return line;
}

/** Keeps GET /api/since-review current: on load, on a new head or analysis stage (statuses come from the re-check),
 * after you submit a review (the reviewed head moves), and when a re-read starts or ends ("Checking…"). */
export function useSinceReviewSync() {
  const headSha = useStore((s) => s.analysis?.pr.head_sha);
  const status = useStore((s) => s.analysis?.status);
  const reviewedHead = useStore((s) => s.state.reviewed_head);
  const rereading = useStore((s) => s.head?.running ?? false);
  useEffect(() => {
    void useStore.getState().loadSinceReview();
  }, [headSha, status, reviewedHead, rereading]);
}

/** The toggle in the tabs bar, next to the files-viewed count. Hidden with no reviewed head or the same head. */
export function SinceReviewToggle() {
  const since = useSinceView();
  const setOn = useStore((s) => s.setSinceOn);
  if (!since.shown) return null;
  const n = since.files.size;
  return (
    <button
      className="btn btn-sm since-toggle"
      aria-pressed={since.on}
      disabled={!since.ok}
      title={since.ok ? `Show only what changed since your last submitted review (${plural(n, "file")})` : (since.review?.note ?? undefined)}
      onClick={() => setOn(!since.on)}
    >
      Changes since your review
      {since.ok && <span className="Counter">{n}</span>}
    </button>
  );
}

/** While the toggle is on: "Showing only changes since your review. [Show the whole PR]", plus the API's note. */
export function SinceReviewBanner() {
  const since = useSinceView();
  const setOn = useStore((s) => s.setSinceOn);
  if (!since.on) return null;
  const note = since.review?.note;
  return (
    <div className="flash flash-info strip" role="status" aria-label="Changes since your review">
      <span className="strip-text">
        Showing only changes since your review.
        {note && <span className="since-note"> {note}</span>}
      </span>
      <span className="strip-actions">
        <button className="btn btn-sm" onClick={() => setOn(false)}>
          Show the whole PR
        </button>
      </span>
    </div>
  );
}

/** A comment's status since your review, as a label (text, not color alone). */
export function CommentStatusLabel({ c, small }: { c: EarlierComment; small?: boolean }) {
  const s = STATUS[c.status];
  return <span className={`Label Label--${s.variant}${small ? " Label--small" : ""}`}>{s.text}</span>;
}

/** The status of one of your earlier comments on its GitHub thread in the file view, with the re-check's note. */
export function CommentStatusLine({ c }: { c: EarlierComment }) {
  return (
    <div className="bot-verdict comment-status">
      <span className="color-muted">Since your review:</span>
      <CommentStatusLabel c={c} />
      {c.note && <InlineText text={c.note} />}
    </div>
  );
}

/** The Overview's lead while the toggle is on: the one-line summary, then each earlier comment with its status. */
export function SinceReviewBox() {
  const since = useSinceView();
  const ghComments = useStore((s) => s.githubComments);
  const sr = since.review;
  if (!since.on || !sr) return null;
  return (
    <section className="Box since-box" aria-label="Since your review">
      <div className="Box-header">
        <h3 className="Box-title">{sinceLine(sr)}</h3>
      </div>
      {sr.comments.map((c) => {
        const body = ghComments.find((g) => g.id === c.id)?.body.split("\n")[0];
        return (
          <div key={c.id} className="Box-row recheck-row since-comment">
            <CommentStatusLabel c={c} />
            <span className="recheck-title">{body ? <InlineText text={body} /> : <span className="color-muted">Your comment</span>}</span>
            <a className="color-muted small mono" href={fileHref(c.path, c.line)}>
              {c.path}
              {c.line ? `:${c.line}` : " (outdated)"}
            </a>
            {c.note && (
              <span className="color-muted small since-comment-note">
                <InlineText text={c.note} />
              </span>
            )}
          </div>
        );
      })}
      {sr.comments_error && (
        <div className="Box-row color-muted small">Couldn't read your comments from GitHub: {sr.comments_error}</div>
      )}
    </section>
  );
}
