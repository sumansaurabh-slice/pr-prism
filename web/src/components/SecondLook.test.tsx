import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { makeAnalysis, makeFinding, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import { FileView } from "./FileView";
import { FindingsBox } from "./ReviewSignals";

const findings = [
  makeFinding({ id: "j", title: "Judged", second_look: "judge", judge_score: 7, confidence: 70 }),
  makeFinding({ id: "n", title: "Judge failed", second_look: "none", confidence: 35 }),
  makeFinding({ id: "r", title: "Rule blocker", second_look: "rule", severity: "blocker", source: "rule:secrets", confidence: null }),
  makeFinding({ id: "p", title: "Still waiting", second_look: "pending", confidence: 60 }),
  makeFinding({ id: "c", title: "From last round", second_look: "carried", status: "still-open", judge_score: 8 }),
];

function row(container: HTMLElement, title: string): HTMLElement {
  return within(container).getByText(title).closest(".finding-row, .review-comment") as HTMLElement;
}

describe("second-look labels", () => {
  beforeEach(() => {
    resetStore({ analysis: makeAnalysis({ findings }), state: makeState() });
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  });
  afterEach(() => vi.restoreAllMocks());

  it("label findings in the Overview's findings box", () => {
    const { container } = render(<FindingsBox />);
    expect(within(row(container, "Judge failed")).getByText("not second-checked")).toHaveClass("Label", "Label--attention");
    expect(within(row(container, "Rule blocker")).getByText("confirmed by rule")).toHaveClass("Label");
    expect(within(row(container, "Still waiting")).getByText("second look pending")).toHaveClass("Label");
    for (const title of ["Judged", "From last round"]) {
      expect(within(row(container, title)).queryByText(/second|rule/)).not.toBeInTheDocument();
    }
  });

  it("label finding cards in the file view, next to the % sure", async () => {
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    await screen.findByText("Judge failed");
    const failed = row(container, "Judge failed");
    expect(within(failed).getByText("not second-checked")).toHaveClass("Label", "Label--attention");
    expect(within(failed).getByText(/35% sure/)).toBeInTheDocument();
    expect(within(row(container, "Rule blocker")).getByText("confirmed by rule")).toBeInTheDocument();
    expect(within(row(container, "Still waiting")).getByText("second look pending")).toBeInTheDocument();
    const judged = row(container, "Judged");
    expect(within(judged).getByText(/70% sure/)).toHaveTextContent("Claude · 70% sure · second look 7/10");
    expect(within(judged).queryByText(/not second-checked|confirmed by rule|second look pending/)).not.toBeInTheDocument();
  });
});
