import { render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { makeAnalysis, makeFinding, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import type { Progress } from "../types";
import { FileView } from "./FileView";
import { Overview } from "./Overview";
import { ProgressBanner } from "./ProgressBanner";
import { FindingsBox } from "./ReviewSignals";

const deep = makeFinding({
  id: "d", title: "Old rows lose their total", sources: ["deep-check"], second_look: "pending",
  question: "Can rows saved before this PR still have `x = 1`?",
  scenario: ["A saved row has `x = 1`.", "The new code adds 2 to it.", "The page shows 3 instead of 2."],
  evidence_refs: [{ path: PATH, line: 2 }],
});
const twin = makeFinding({ id: "t", title: "Found twice", sources: ["annotate", "deep-check"], line: 2 });
const plain = makeFinding({ id: "p", title: "Plain finding", line: 4 });

function row(container: HTMLElement, title: string): HTMLElement {
  return within(container).getByText(title).closest(".finding-row, .review-comment") as HTMLElement;
}

describe("deep-check findings", () => {
  beforeEach(() => {
    resetStore({ analysis: makeAnalysis({ findings: [deep, twin, plain] }), state: makeState() });
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  });
  afterEach(() => vi.restoreAllMocks());

  it("finding cards show the question in bold, the numbered scenario and a deep check label", async () => {
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    await screen.findByText("Old rows lose their total");
    const card = row(container, "Old rows lose their total");
    expect(within(card).getByText("deep check")).toHaveClass("Label");
    const question = within(card).getByText(/Can rows saved before this PR still have/);
    expect(question.closest("strong")).not.toBeNull();
    expect(within(question.closest("strong") as HTMLElement).getByText("x = 1")).toContainHTML("<code>x = 1</code>");
    expect(within(card).getByText("How it could happen")).toBeInTheDocument();
    const steps = within(card).getAllByRole("listitem");
    expect(steps.map((s) => s.textContent)).toEqual([
      "A saved row has x = 1.", "The new code adds 2 to it.", "The page shows 3 instead of 2.",
    ]);
    expect(steps[0].closest("ol")).not.toBeNull();

    expect(within(row(container, "Found twice")).getByText("deep check")).toBeInTheDocument();
    const other = row(container, "Plain finding");
    expect(within(other).queryByText("deep check")).not.toBeInTheDocument();
    expect(within(other).queryByText("How it could happen")).not.toBeInTheDocument();
  });

  it("the Overview's findings box shows the label, the question and the scenario", () => {
    const { container } = render(<FindingsBox />);
    const r = row(container, "Old rows lose their total");
    expect(within(r).getByText("deep check")).toHaveClass("Label", "Label--small");
    const detail = r.nextElementSibling as HTMLElement;
    expect(detail).toHaveClass("finding-row-deep");
    expect(within(detail).getByText(/Can rows saved before/).closest("strong")).not.toBeNull();
    expect(within(detail).getByText("How it could happen").tagName).toBe("SUMMARY");
    expect(within(detail).getAllByRole("listitem")).toHaveLength(3);
    expect(container.querySelectorAll(".finding-row-deep")).toHaveLength(1);  // only findings with a scenario
    expect(within(row(container, "Found twice")).getByText("deep check")).toBeInTheDocument();
  });
});

describe("analysis warnings and deep check progress", () => {
  afterEach(() => vi.restoreAllMocks());

  it("the Overview lists what didn't run fully in a muted box", () => {
    const warnings = [
      'Deep check skipped "Docs and tests": it checks at most 4 areas per PR, riskiest first.',
      'Deep check couldn\'t finish "Payments" (area chapter:pay, model opus, try 2, 10m 0s): timed out after 10m 0s.',
    ];
    resetStore({ analysis: makeAnalysis({ warnings }), state: makeState() });
    render(<Overview />);
    const box = screen.getByText("Some checks didn't run fully").closest(".flash") as HTMLElement;
    expect(box).toHaveClass("flash-muted");
    expect(within(box).getAllByRole("listitem").map((li) => li.textContent)).toEqual(warnings);
  });

  it("the Overview shows no warnings box when everything ran", () => {
    resetStore({ analysis: makeAnalysis(), state: makeState() });
    render(<Overview />);
    expect(screen.queryByText("Some checks didn't run fully")).not.toBeInTheDocument();
  });

  function progress(over: Partial<Progress>) {
    const a = makeAnalysis({ status: "deepchecking" });
    resetStore({ analysis: { ...a, progress: { ...a.progress, status: "deepchecking", ...over } }, state: makeState() });
  }

  it("the progress banner counts the deep check's areas", () => {
    progress({ deepcheck_total: 4, deepcheck_done: 2, message: 'Deep check: "Payments" checked, nothing found' });
    const { container } = render(<ProgressBanner />);
    expect(screen.getByText("Deep check: 2 of 4 areas done")).toBeInTheDocument();
    expect(container.querySelector(".progress-fill")).toHaveStyle({ width: "50%" });
  });

  it("the progress banner says when areas are still being picked", () => {
    progress({ deepcheck_total: 0, deepcheck_done: 0 });
    render(<ProgressBanner />);
    expect(screen.getByText("Deep check: picking the riskiest areas…")).toBeInTheDocument();
  });
});
