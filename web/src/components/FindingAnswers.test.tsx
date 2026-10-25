import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useStore } from "../store";
import { makeStop, makeVerdict, sid } from "../test/coverageFixtures";
import { makeAnalysis, makeDraft, makeFinding, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import type { Draft, Finding, Stop } from "../types";
import { FileView } from "./FileView";
import { FindingsBox } from "./ReviewSignals";

const finding = makeFinding({ id: "f1", title: "y is never used" });
const stop = makeStop({ id: sid("a"), finding_id: "f1" });
/** The comment "Real problem" made from the stop. */
const fromStop = makeDraft({
  id: "d9", line: 3, body: "y is never used", source: "suggestion", suggestion_id: "f1", origin_id: sid("a"),
});

function setup({ stops = [stop], findings = [finding], ...state }: Partial<ReturnType<typeof makeState>> & {
  stops?: Stop[]; findings?: Finding[];
} = {}) {
  resetStore({ analysis: makeAnalysis({ findings, stops }), state: makeState(state) });
}

function renderFile() {
  return render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
}

/** The finding's card in the file view (not your comment's). */
async function card(title = "y is never used"): Promise<HTMLElement> {
  const titles = await screen.findAllByText(title, { selector: ".finding-title" });
  return titles[0].closest(".review-comment") as HTMLElement;
}

describe("your answer on finding cards", () => {
  beforeEach(() => {
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => vi.restoreAllMocks());

  it("Not sure", async () => {
    setup({ stop_verdicts: { [stop.id]: makeVerdict(stop, "unsure") } });
    renderFile();
    const c = await card();
    expect(within(c).getByText("Not sure")).toHaveClass("Label", "Label--attention");
    expect(within(c).getByRole("button", { name: "Add to my review" })).toBeInTheDocument();
  });

  it("In your review, with Open your comment in place of Add to my review", async () => {
    setup({ stop_verdicts: { [stop.id]: makeVerdict(stop, "real") }, drafts: [fromStop] });
    const create = vi.spyOn(api, "createDraft");
    renderFile();
    const c = await card();
    expect(within(c).getByText("In your review")).toHaveClass("Label", "Label--success");
    expect(within(c).queryByRole("button", { name: "Add to my review" })).not.toBeInTheDocument();
    expect(within(c).queryByRole("button", { name: "Dismiss" })).not.toBeInTheDocument();
    fireEvent.click(within(c).getByRole("button", { name: "Open your comment" }));
    expect(create).not.toHaveBeenCalled();
    // Your comment is the one on the page; Open focuses it.
    const mine = document.querySelector('[data-draft="d9"]') as HTMLElement;
    expect(mine).toContainElement(document.activeElement as HTMLElement);
  });

  it("finds a comment made from the stop by origin_id alone", async () => {
    const plain = { ...fromStop, suggestion_id: null, source: "user" as Draft["source"] };
    setup({ stop_verdicts: { [stop.id]: makeVerdict(stop, "real") }, drafts: [plain] });
    renderFile();
    expect(within(await card()).getByRole("button", { name: "Open your comment" })).toBeInTheDocument();
  });

  it("Add to my review twice makes one comment: the second click opens it", async () => {
    setup();
    let resolve!: (d: Draft) => void;
    const create = vi.spyOn(api, "createDraft").mockReturnValue(new Promise<Draft>((r) => (resolve = r)));
    renderFile();
    const c = await card();
    const add = within(c).getByRole("button", { name: "Add to my review" });
    fireEvent.click(add);
    fireEvent.click(add); // a double-click while the first is saving
    const made = makeDraft({ id: "d1", line: 3, body: finding.comment_body, source: "suggestion", suggestion_id: "f1" });
    await act(async () => resolve(made));
    expect(create).toHaveBeenCalledTimes(1);

    const after = await card();
    expect(within(after).getByText("In your review")).toBeInTheDocument();
    fireEvent.click(within(after).getByRole("button", { name: "Open your comment" }));
    expect(create).toHaveBeenCalledTimes(1);
    expect(useStore.getState().state.drafts).toEqual([made]);
  });

  it("You said: not a problem, on the dismissed finding", async () => {
    setup({ stop_verdicts: { [stop.id]: makeVerdict(stop, "not") }, dismissed_suggestions: ["f1"] });
    renderFile();
    fireEvent.click(await screen.findByRole("button", { name: /Show 1 dismissed suggestion/ }));
    const item = screen.getByText("y is never used").closest(".dismissed-item") as HTMLElement;
    expect(within(item).getByText("You said: not a problem")).toHaveClass("Label");
  });
});

describe("your answer in the Overview's problems box", () => {
  const real = makeFinding({ id: "f1", title: "Real one", severity: "major" });
  const not = makeFinding({ id: "f2", title: "Not one", severity: "blocker" });
  const unsure = makeFinding({ id: "f3", title: "Unsure one", severity: "minor" });
  const stops = [
    makeStop({ id: sid("a"), finding_id: "f1" }), makeStop({ id: sid("b"), finding_id: "f2" }),
    makeStop({ id: sid("c"), finding_id: "f3" }),
  ];

  beforeEach(() => {
    window.location.hash = "#/overview";
    setup({
      stops, findings: [real, not, unsure], drafts: [fromStop], dismissed_suggestions: ["f2"],
      stop_verdicts: {
        [sid("a")]: makeVerdict(stops[0], "real"), [sid("b")]: makeVerdict(stops[1], "not"),
        [sid("c")]: makeVerdict(stops[2], "unsure"),
      },
    });
  });

  function row(container: HTMLElement, title: string): HTMLElement {
    return within(container).getByText(title).closest(".finding-row") as HTMLElement;
  }

  it("labels each row, with a link to your comment", () => {
    const { container } = render(<FindingsBox />);
    const r = row(container, "Real one");
    expect(within(r).getByText("In your review")).toHaveClass("Label");
    expect(within(r).getByRole("link", { name: "Open your comment" })).toHaveAttribute(
      "href", "#/file/src%2Fcore.py?line=3",
    );
    expect(within(row(container, "Not one")).getByText("You said: not a problem")).toHaveClass("Label");
    expect(within(row(container, "Unsure one")).getByText("Not sure")).toHaveClass("Label");
  });

  it("keeps a finding you said isn't a problem, last, outside the counts", () => {
    const { container } = render(<FindingsBox />);
    const titles = [...container.querySelectorAll(".finding-row-title")].map((t) => t.textContent);
    expect(titles).toEqual(["Real one", "Unsure one", "Not one"]);
    expect(container.querySelector(".Box-header")).not.toHaveTextContent("Blocks merge");
  });

  it("Open your comment goes to it", () => {
    const { container } = render(<FindingsBox />);
    fireEvent.click(within(row(container, "Real one")).getByRole("link", { name: "Open your comment" }));
    expect(window.location.hash).toBe("#/file/src%2Fcore.py?line=3");
  });
});
