// Your guided-review answer to a finding (design D20): shown on its card in the file view and in the Overview's
// problems box, so the same answer reads the same everywhere.
import { useMemo } from "react";
import { useStore } from "./store";
import type { Draft, Finding, Stop, StopVerdict, Verdict } from "./types";

export interface FindingAnswer {
  stopId: string | null;     // the finding's stop (stops[].finding_id), or an earlier answer's (anchor.finding_id)
  verdict: Verdict | null;   // your answer to that stop
  draft: Draft | null;       // your pending comment for it: made by "Real problem" (origin_id) or "Add to my review"
}

export function findingAnswer(
  f: Finding, stops: Stop[], verdicts: Record<string, StopVerdict>, drafts: Draft[],
): FindingAnswer {
  let stopId = stops.find((s) => s.finding_id === f.id)?.id ?? null;
  if (!stopId) stopId = Object.entries(verdicts).find(([, v]) => v.anchor?.finding_id === f.id)?.[0] ?? null;
  const verdict = stopId ? (verdicts[stopId]?.verdict ?? null) : null;
  const draft =
    (stopId ? drafts.find((d) => d.origin_id === stopId) : undefined) ??
    drafts.find((d) => d.suggestion_id === f.id) ??
    null;
  return { stopId, verdict, draft };
}

export function useFindingAnswer(f: Finding): FindingAnswer {
  const stops = useStore((s) => s.analysis?.stops);
  const verdicts = useStore((s) => s.state.stop_verdicts);
  const drafts = useStore((s) => s.state.drafts);
  return useMemo(() => findingAnswer(f, stops ?? [], verdicts ?? {}, drafts), [f, stops, verdicts, drafts]);
}

/** The label: "In your review", "You said: real problem" (its comment was deleted), "You said: not a problem",
 * "Not sure"; null when you haven't answered. Text always, never color alone. */
export function answerLabel(a: FindingAnswer): { text: string; variant: string; title: string } | null {
  if (a.draft) {
    return { text: "In your review", variant: "success", title: "You have a pending comment for this. It is sent with your review." };
  }
  switch (a.verdict) {
    case "real":
      return { text: "You said: real problem", variant: "success", title: "You answered Real problem; its comment was deleted." };
    case "not":
      return { text: "You said: not a problem", variant: "secondary", title: "You answered Not a problem in the review." };
    case "unsure":
      return { text: "Not sure", variant: "attention", title: "You answered Not sure. Ask in the chat, or answer it again in the review." };
    default:
      return null;
  }
}
