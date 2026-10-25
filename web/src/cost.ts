// How spend reads in the app (design review issue 7). Every number comes from GET /api/cost or the chat's own
// answers, which the server computes from calls.jsonl (prr/cost.py).
import type { ChatMessage, CostSummary, RunEstimate, ThreadCost } from "./types";

/** "$0.42": always 2 decimals. */
export function usd(n: number): string {
  return `$${n.toFixed(2)}`;
}

/** "≥ $12.40" when part of the cost isn't known, so the number is only a lower bound. */
export function atLeast(n: number, unknown: boolean): string {
  return `${unknown ? "≥ " : ""}${usd(n)}`;
}

/** The header: "$12.40 spent", "≥ $12.40 spent", "$12.40 spent so far" while the analysis runs. */
export function spentText(c: CostSummary): string {
  return `${atLeast(c.total_usd, c.unknown)} spent${c.running ? " so far" : ""}`;
}

/** The header total's tooltip. */
export function breakdownText(c: CostSummary): string {
  const b = c.breakdown;
  return `Analysis ${usd(b.analysis)} · Chat ${usd(b.chat)} · Other checks ${usd(b.other)}`;
}

/** "over your $10 limit" ([budget] warn_usd). */
export function overLimitText(warn: number): string {
  return `over your ${Number.isInteger(warn) ? `$${warn}` : usd(warn)} limit`;
}

/** A thread's total in the thread picker, or "" for a thread with no answers yet. */
export function threadTotalText(t: ThreadCost | undefined): string {
  return t && t.turns > 0 ? atLeast(t.total_usd, t.unknown) : "";
}

/** "Re-read the PR"'s cost, from the last complete run (GET /api/head estimate): "about 6 min and $2". */
export function estimateText(e: RunEstimate): string {
  const m = Math.max(1, Math.round(e.minutes));
  const time = m < 60 ? `${m} min` : `${Math.floor(m / 60)}h${m % 60 ? ` ${m % 60}m` : ""}`;
  return `about ${time} and ${e.usd < 1 ? usd(e.usd) : `$${Math.round(e.usd)}`}`;
}

export interface SoFar { usd: number; unknown: boolean }

/** Each answer's "thread so far": the sum of the answers' own costs up to and including it (what calls.jsonl adds
 * up for the thread). It is a lower bound while an earlier answer's unknown cost hasn't been counted: that cost
 * folds into the next answer that has one (marked includes_stopped), or is lost when the session changed first. */
export function threadSoFar(messages: ChatMessage[]): (SoFar | null)[] {
  let total = 0;
  let waiting = false;
  let lost = false;
  return messages.map((m) => {
    if (m.role !== "assistant") return null;
    if (m.cost_usd == null) {
      waiting = true;
    } else {
      total += m.cost_usd;
      if (!m.includes_stopped && waiting) lost = true;
      waiting = false;
    }
    return { usd: total, unknown: waiting || lost };
  });
}
