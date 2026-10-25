import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useGlobalKeys } from "../keyboard";
import { useRoute } from "../router";
import { makeAnalysis, makeFinding, makePreview, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import { makeStop, reviewAnalysis, stopId } from "../test/stops";
import { FileView } from "./FileView";
import { FindingsBox } from "./ReviewSignals";
import { ReviewView } from "./ReviewView";

// A question for the author (category "intent"): on the deleted line `x = 1`, side LEFT, always minor.
const ask = makeFinding({
  id: "q1", side: "LEFT", line: 2, anchor: "x = 1", anchored: true, severity: "minor", category: "intent",
  title: "Why was the start value removed?", impact: "This removes `x = 1`. Before, x started at 1. Was that on purpose?",
  comment_body: "This removes `x = 1`. Before, x started at 1. Was that on purpose?",
});
const bug = makeFinding({ id: "b1", severity: "minor", title: "y is never used" });

function row(container: HTMLElement, title: string): HTMLElement {
  return within(container).getByText(title).closest(".finding-row, .review-comment") as HTMLElement;
}

describe("questions for the author", () => {
  beforeEach(() => {
    resetStore({ analysis: makeAnalysis({ findings: [ask, bug] }), state: makeState() });
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  });
  afterEach(() => {
    vi.restoreAllMocks();
    window.location.hash = "";
  });

  it("show as a question in the Overview's findings box, counted apart from Should fix", () => {
    const { container } = render(<FindingsBox />);
    const r = row(container, "Why was the start value removed?");
    expect(within(r).getByText("Question for the author")).toHaveClass("Label");
    expect(within(r).queryByText("Should fix")).not.toBeInTheDocument();
    expect(within(row(container, "y is never used")).getByText("Should fix")).toBeInTheDocument();
    const counts = [...container.querySelectorAll(".finding-count")].map((c) => c.textContent);
    expect(counts).toEqual(["Should fix 1", "Question for the author 1"]);
  });

  it("show the label on the deleted line's card, without the severity or the category", async () => {
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    await screen.findByText("Why was the start value removed?");
    const card = row(container, "Why was the start value removed?");
    expect(within(card).getByText("Question for the author")).toHaveClass("Label--accent");
    expect(within(card).queryByText("Should fix")).not.toBeInTheDocument();
    expect(within(card).queryByText("intent")).not.toBeInTheDocument();
    expect(within(card).getByText(/old L2/)).toBeInTheDocument();
  });

  it("ask the question as the stop's heading in guided review", () => {
    const stop = makeStop({
      id: stopId(7), side: "LEFT", line: 2, question: ask.title, severity: "minor", category: "intent",
      finding_id: ask.id, comment_body: ask.comment_body, claude_view: ask.impact, order: 0,
    });
    resetStore({ analysis: reviewAnalysis({ findings: [ask], stops: [stop] }), state: makeState() });
    vi.spyOn(api, "coverage").mockResolvedValue({
      not_checked: [], unsure: [], skipped_areas: [], high_risk_unseen: { hunks: 0, files: 0, by_file: {} },
      low_confidence: 0, running: false, count: 0,
    });
    vi.spyOn(api, "previewReview").mockResolvedValue(makePreview());
    function Page() {
      useGlobalKeys();
      const route = useRoute();
      return route.view === "review" ? <ReviewView route={route} /> : null;
    }
    window.location.hash = `#/review/${stop.id}`;
    render(<Page />);
    const heading = screen.getByRole("heading", { level: 2 });
    expect(heading).toHaveTextContent("Why was the start value removed?");
    const labels = heading.parentElement!.querySelector(".label-list") as HTMLElement;
    expect(within(labels).getByText("Question for the author")).toBeInTheDocument();
    expect(within(labels).queryByText("Should fix")).not.toBeInTheDocument();
    expect(within(labels).queryByText("intent")).not.toBeInTheDocument();
  });
});
