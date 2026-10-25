import { useState } from "react";
import { Markdown } from "../markdown";
import type { ApprovalDecision, ApprovalRecord, ApprovalRequest, ApprovalWhy } from "../types";

/** A card in the live answer: what the server sent, plus how it was settled (null while it waits). */
export interface LiveApproval extends ApprovalRequest {
  decision: ApprovalDecision | null;
  why: ApprovalWhy | null;
}

type Settled = Pick<ApprovalRecord, "decision" | "why">;

// The verb of "the action in plain words"; other tools (connectors, LS, post_review) use the card's title.
const VERBS: Record<string, string> = {
  Read: "Read", NotebookRead: "Read", Grep: "Search for", Glob: "List files matching",
  Edit: "Edit", MultiEdit: "Edit", Write: "Write", NotebookEdit: "Edit",
  Bash: "Run", WebFetch: "Open", WebSearch: "Search the web for",
  mcp__prprism__post_comment: "Post a comment on",
};

/** The action in plain words: "Read ~/code/billing/config.py", "Run npm test", "Post a comment on src/a.py:42". */
export function actionText(a: Pick<ApprovalRecord, "tool" | "title" | "summary">): string {
  const verb = VERBS[a.tool];
  return verb && a.summary ? `${verb} ${a.summary}` : a.title;
}

const DECISION_TEXT: Record<ApprovalDecision, string> = { allow: "Allowed", always: "Allowed for this chat", deny: "Denied" };

/** What a settled card says in place of its buttons. */
function settledText(a: Settled): { text: string; variant: string } {
  if (a.why === "covered") return { text: "Allowed by your earlier choice", variant: "success" };
  if (a.why === "timeout") return { text: "Timed out · Claude was told no", variant: "secondary" };
  if (a.why === "closed" || !a.decision) return { text: "Not answered · Claude was told no", variant: "secondary" };
  return { text: DECISION_TEXT[a.decision], variant: a.decision === "deny" ? "danger" : "success" };
}

/** The first word of a card's one-line record once the turn ends. */
function verdict(r: Settled): "Allowed" | "Denied" | "Timed out" | "Not answered" {
  if (r.why === "timeout") return "Timed out";
  if (r.why === "closed" || !r.decision) return "Not answered";
  return r.decision === "deny" ? "Denied" : "Allowed";
}

/** A card as it is saved with the answer. */
export function toRecord(a: LiveApproval): ApprovalRecord {
  return { id: a.id, tool: a.tool, title: a.title, summary: a.summary, decision: a.decision, why: a.why };
}

/** WebFetch's full URL (the summary is cut at 200 characters). */
function fetchUrl(a: ApprovalRequest): string {
  try {
    const url = (JSON.parse(a.detail) as { url?: unknown }).url;
    if (typeof url === "string" && url) return url;
  } catch {
    /* the detail is cut at 3000 characters: fall back to the summary */
  }
  return a.summary;
}

/** What the card would send or run: a post's comment as Markdown, a URL or a command in mono, else the raw input
 * behind "Show input". */
function CardBody({ a }: { a: ApprovalRequest }) {
  const [open, setOpen] = useState(false);
  if (a.body !== null) {
    return a.body.trim() ? <Markdown text={a.body} className="approval-body" /> : null;
  }
  if (a.tool === "WebFetch") return <p className="approval-url mono">{fetchUrl(a)}</p>;
  if (a.tool === "Bash") return <pre className="approval-detail">{`$ ${a.detail}`}</pre>;
  if (!a.detail) return null;
  return (
    <div className="approval-input">
      <button className="btn-link small" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        {open ? "Hide input" : "Show input"}
      </button>
      {open && <pre className="approval-detail">{a.detail}</pre>}
    </div>
  );
}

/** "Claude asks first", read top to bottom: the action, why it asks (and, on guard cards, what marked the turn),
 * what it would send, then Deny → Always allow → Allow. No key approves a card: only its buttons do. */
export function ApprovalCard({ a, onDecide }: { a: LiveApproval; onDecide: (decision: ApprovalDecision) => void }) {
  const action = actionText(a);
  const allow = a.body !== null ? "Post" : a.guard ? "Allow once" : "Allow";
  const settled = a.decision ? settledText(a) : null;
  const read = a.causes.filter((c) => c.kind !== "approved").map((c) => c.text);
  const approved = a.causes.filter((c) => c.kind === "approved").map((c) => c.text);
  return (
    <div className={`approval${settled ? " decided" : ""}`} role="group" aria-label={`Claude asks first: ${action}`}>
      <p className="approval-action">{action}</p>
      <div className="approval-why">
        <span className="Label Label--attention approval-reason">{a.reason.text}</span>
        {a.reason.detail && <span className="approval-reason-detail">{a.reason.detail}</span>}
        {(read.length > 0 || approved.length > 0) && (
          <ul className="approval-causes">
            {read.length > 0 && <li>Earlier this turn Claude read: {read.join(" · ")}</li>}
            {approved.length > 0 && <li>Earlier this turn you approved: {approved.join(" · ")}</li>}
          </ul>
        )}
      </div>
      <CardBody a={a} />
      {settled ? (
        <p className="approval-status">
          <span className={`Label Label--${settled.variant}`}>{settled.text}</span>
        </p>
      ) : (
        <div className="approval-actions">
          <button className="btn btn-sm" aria-label={`Deny: ${action}`} onClick={() => onDecide("deny")}>
            Deny
          </button>
          {a.always && !a.guard && (
            <button
              className="btn btn-sm"
              aria-label={`${a.always_text || "Always allow"}: ${action}`}
              title={a.always_title}
              onClick={() => onDecide("always")}
            >
              {a.always_text || "Always allow"}
            </button>
          )}
          <button className="btn btn-sm btn-primary" aria-label={`${allow}: ${action}`} onClick={() => onDecide("allow")}>
            {allow}
          </button>
        </div>
      )}
    </div>
  );
}

/** After the turn: one line per card, "Allowed: Read ~/code/billing/config.py" / "Denied: …" / "Timed out: …". */
export function ApprovalRecords({ records }: { records: ApprovalRecord[] | undefined }) {
  if (!records?.length) return null;
  return (
    <ul className="approval-records" aria-label="What Claude asked first">
      {records.map((r) => {
        const v = verdict(r);
        return (
          <li key={r.id} className="approval-record" title={settledText(r).text}>
            <span className={`approval-verdict approval-verdict-${v === "Allowed" ? "yes" : v === "Denied" ? "no" : "none"}`}>
              {v}:
            </span>{" "}
            {actionText(r)}
          </li>
        );
      })}
    </ul>
  );
}

/** What the chat's live region says when a card arrives. */
export function announceText(a: ApprovalRequest): string {
  return `Claude asks first: ${actionText(a)}. ${a.reason.text}`;
}
