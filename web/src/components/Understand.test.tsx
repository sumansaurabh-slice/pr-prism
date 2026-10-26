// "Understand this PR": the plan box, the routes, the step list, a step's sections, loading and errors, the keys,
// the level switch, the end screen, and the Review's link to a step.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api, ApiError } from "../api";
import { registerFileNav, registerReviewNav, registerUnderstandNav, useGlobalKeys } from "../keyboard";
import { useRoute } from "../router";
import { useStore } from "../store";
import { makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import { finding, reviewAnalysis, verdictFor } from "../test/stops";
import { explanation, makeUnderstand, step1, step2, step3, STEPS } from "../test/understand";
import type { StepExplanation, UnderstandLevel } from "../types";
import { Overview } from "./Overview";
import { ReviewView } from "./ReviewView";
import { UnderstandView } from "./UnderstandView";

function Page() {
  useGlobalKeys();
  const route = useRoute();
  if (route.view === "understand") return <UnderstandView route={route} />;
  if (route.view === "review") return <ReviewView route={route} />;
  return <p>Somewhere else</p>;
}

const press = (key: string, extra: object = {}) =>
  act(() => {
    fireEvent.keyDown(window, { key, ...extra });
  });
const hashOf = (id: string) => `#/understand/${id}`;
const heading = () => screen.getByRole("heading", { level: 2 });
const stepNav = () => screen.getByRole("navigation", { name: "Understand steps" });

function open(hash: string) {
  window.location.hash = hash;
  return render(<Page />);
}

let explain: MockInstance<typeof api.explainStep>;
let understood: MockInstance<typeof api.setUnderstood>;

beforeEach(() => {
  resetStore({ analysis: reviewAnalysis(), state: makeState(), understand: makeUnderstand() });
  vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  explain = vi.spyOn(api, "explainStep").mockImplementation(async (id, level) => explanation({ id, level }));
  understood = vi.spyOn(api, "setUnderstood").mockImplementation(async (_id, on) => ({ understood: on }));
});
afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("the Overview's plan box", () => {
  it("offers Understand the PR first, with ~N min to read, then Start review", () => {
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Review plan" });
    expect(within(box).getAllByRole("button").map((b) => b.textContent)).toEqual(["Understand the PR", "Start review"]);
    expect(box).toHaveTextContent("Understand the PR~3 min to read");
    fireEvent.click(within(box).getByRole("button", { name: "Understand the PR" }));
    expect(window.location.hash).toBe("#/understand");
    fireEvent.click(within(box).getByRole("button", { name: "Start review" }));
    expect(window.location.hash).toBe("#/review");
  });

  it("has no Understand button until the steps are in", () => {
    useStore.setState({ understand: null });
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Review plan" });
    expect(within(box).queryByRole("button", { name: "Understand the PR" })).not.toBeInTheDocument();
  });
});

describe("routes", () => {
  it("#/understand opens the first step not understood yet", async () => {
    useStore.setState({ understand: makeUnderstand({ understood: { [step1.id]: "t" } }) });
    open("#/understand");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step2.id)));
    expect(await screen.findByRole("heading", { level: 2, name: "src/util.py" })).toBeInTheDocument();
  });

  it("#/understand shows the end screen when every step is understood", () => {
    useStore.setState({ understand: makeUnderstand({ understood: Object.fromEntries(STEPS.map((s) => [s.id, "t"])) }) });
    open("#/understand");
    expect(heading()).toHaveTextContent("You've walked through all 3 steps");
    expect(window.location.hash).toBe("#/understand");
  });

  it("a deep link opens its step", () => {
    open(hashOf(step3.id));
    expect(heading()).toHaveTextContent("src/api.py");
    expect(within(stepNav()).getByRole("link", { current: "step" })).toHaveAttribute("href", hashOf(step3.id));
  });

  it("an id that isn't a step says so and offers the next step", () => {
    open(hashOf("und-0000000000000000000000000000000000000000"));
    expect(screen.getByRole("status")).toHaveTextContent("This step isn't part of the walk any more.");
    expect(screen.getByRole("link", { name: "Go to the next step" })).toHaveAttribute("href", "#/understand");
  });
});

describe("the step list", () => {
  it("groups the steps by chapter; rows show the file, its role, a ✓ once understood, and aria-current", () => {
    useStore.setState({ understand: makeUnderstand({ understood: { [step1.id]: "t" } }) });
    open(hashOf(step2.id));
    const nav = stepNav();
    expect(nav.tagName).toBe("NAV");
    expect(within(nav).getByText("1 of 3 understood")).toBeInTheDocument();
    const chapters = within(nav).getAllByRole("heading", { level: 3 });
    expect(chapters.map((h) => h.textContent)).toEqual(["Chapter 1 · The data", "Chapter 2 · The API"]);
    const rows = within(nav).getAllByRole("link");
    expect(rows.map((r) => r.textContent)).toEqual([
      "1core.pyunderstoodHolds x and y.",
      "2util.pySmall helpers.",
      "3api.py",
    ]);
    // ✓ plus text, never colour alone
    expect(within(rows[0]).getByTitle("Understood").querySelector("svg")).not.toBeNull();
    expect(rows[1]).toHaveAttribute("aria-current", "step");
    expect(rows[0]).not.toHaveAttribute("aria-current");
    expect(chapters[0].closest("section")).toContainElement(rows[1]);
    expect(chapters[1].closest("section")).toContainElement(rows[2]);
  });
});

describe("a step", () => {
  it("reads path + role → Before → What changes → diff → Why → Where it connects → Terms → Tested by → Skim", async () => {
    const { container } = open(hashOf(step1.id));
    expect(heading()).toHaveTextContent(PATH);
    expect(await screen.findByRole("region", { name: "Before" })).toHaveTextContent("x was 1, and nothing else was set.");
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
    const parts = [
      heading(),
      container.querySelector(".und-role-line"),
      screen.getByRole("region", { name: "Before" }),
      screen.getByRole("region", { name: "What changes" }),
      container.querySelector(".file-box"),
      screen.getByRole("region", { name: "Why" }),
      screen.getByRole("region", { name: "Where it connects" }),
      screen.getByRole("region", { name: "Terms" }),
      screen.getByText(/^Tested by:/),
      screen.getByText(/^Skim/),
      container.querySelector(".stop-bar"),
    ] as HTMLElement[];
    for (let i = 1; i < parts.length; i++) {
      expect(parts[i - 1].compareDocumentPosition(parts[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(parts[1]).toHaveTextContent("Holds x and y.");
    expect(screen.getByRole("region", { name: "What changes" })).toHaveTextContent("x becomes 2 and a new y is set to 3.");
    const why = screen.getByRole("region", { name: "Why" });
    expect(why).toHaveTextContent("The description asks for a bigger x.");
    expect(within(why).getByText("Source: PR description")).toHaveClass("color-muted");

    const links = within(screen.getByRole("region", { name: "Where it connects" })).getAllByRole("link");
    expect(links.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      [`${PATH}:4`, "#/file/src%2Fcore.py?line=4"],
      ["src/other.py:10", "#/file/src%2Fother.py?line=10&full=1"], // not in the PR: the whole file
    ]);
    const terms = within(screen.getByRole("region", { name: "Terms" }));
    expect(terms.getByText("x").closest("dt")).not.toBeNull();
    expect(terms.getByText("How big the batch is.").tagName).toBe("DD");
    expect(screen.getByText(/^Tested by:/)).toContainElement(screen.getByRole("link", { name: "tests/test_core.py" }));
    expect(screen.getByText(/^Skim/)).toContainElement(screen.getByRole("link", { name: "poetry.lock" }));
    expect(explain).toHaveBeenCalledWith(step1.id, "new");
  });

  it("marks the look_at lines in the diff with a background and a ▶, not colour alone", async () => {
    const { container } = open(hashOf(step1.id));
    await screen.findByRole("region", { name: "Before" });
    await waitFor(() => expect(container.querySelector(".diff-code-selected")).not.toBeNull());
    const marked = [...container.querySelectorAll(".diff-code-selected")];
    expect(marked.map((td) => td.textContent)).toEqual(["y = 3"]);
    const row = marked[0].closest("tr")!;
    expect(row.querySelector(".look-mark")).toHaveTextContent("▶");
    expect(row).toHaveTextContent("Look here.");
    expect(screen.getByText("1 line to look at, marked ▶")).toBeInTheDocument();
  });

  it("while the text is made: the diff at once, and Reading the code around this change…", async () => {
    explain.mockImplementation(() => new Promise<StepExplanation>(() => undefined));
    const { container } = open(hashOf(step1.id));
    expect(screen.getByRole("status")).toHaveTextContent("Reading the code around this change…");
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
    expect(screen.queryByRole("region", { name: "Before" })).not.toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Why" })).not.toBeInTheDocument();
    expect(screen.getByText(/^Tested by:/)).toBeInTheDocument(); // known without Claude
  });

  it("fetches the next step ahead once this one is in", async () => {
    open(hashOf(step1.id));
    await screen.findByRole("region", { name: "Before" });
    await waitFor(() => expect(explain).toHaveBeenCalledWith(step2.id, "new"));
    expect(explain).not.toHaveBeenCalledWith(step3.id, expect.anything());
  });

  it('a failed step says "Couldn\'t explain this step: <detail> · Try again", keeps the diff, and Try again works', async () => {
    explain.mockRejectedValueOnce(new ApiError(502, "Claude stopped before it finished"));
    const { container } = open(hashOf(step1.id));
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Couldn't explain this step: Claude stopped before it finished · Try again");
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
    expect(explain).not.toHaveBeenCalledWith(step2.id, expect.anything()); // nothing fetched ahead of a failure
    fireEvent.click(within(alert).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("region", { name: "Before" })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(explain).toHaveBeenCalledTimes(3); // the failure, Try again, then the next step fetched ahead
  });

  it('says "Not explained in the PR" when nothing says why', async () => {
    explain.mockImplementation(async (id, level) =>
      explanation({ id, level, why: null, why_source: null, why_missing: true }),
    );
    open(hashOf(step1.id));
    const why = await screen.findByRole("region", { name: "Why" });
    expect(within(why).getByText("Not explained in the PR. Ask the author, or ask in the chat.")).toHaveClass("color-muted");
    expect(why).not.toHaveTextContent("Source:");
  });

  it('shows the progress "Step 2 of 3 · Chapter 1 of 2" and the keys', () => {
    const { container } = open(hashOf(step2.id));
    const bar = container.querySelector(".stop-bar") as HTMLElement;
    expect(bar).toHaveTextContent("Step 2 of 3 · Chapter 1 of 2");
    expect(bar).toHaveTextContent("y or → got it · c ask about this · j/k next/previous");
    expect(within(bar).getByRole("button", { name: "Got it, next (y)" })).toHaveClass("btn-primary");
  });
});

describe("Viewed: collapsing a file", () => {
  afterEach(() => window.localStorage.clear());

  it("collapses the file's diff, and the choice is kept in this browser", async () => {
    const { container, unmount } = open(hashOf(step1.id));
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
    const box = container.querySelector(".file-box") as HTMLElement;
    fireEvent.click(within(box).getByRole("checkbox", { name: "Viewed" }));
    expect(container.querySelector(".diff")).toBeNull();
    expect(box).toHaveClass("collapsed");
    expect(within(box).getByText(PATH.split("/").pop()!, { exact: false })).toBeInTheDocument();   // the header stays
    unmount();

    const again = open(hashOf(step1.id));                                    // a reload: still collapsed
    expect(within(again.container.querySelector(".file-box") as HTMLElement).getByRole("checkbox", { name: "Viewed" }))
      .toBeChecked();
    expect(again.container.querySelector(".diff")).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Viewed" }));     // uncheck: the diff is back
    await waitFor(() => expect(again.container.querySelector(".diff")).not.toBeNull());
  });

  it("opens again when the file changes in a later commit", async () => {
    window.location.hash = hashOf(step1.id);
    const a = useStore.getState().analysis!;
    window.localStorage.setItem(`prr.understand.viewed.${a.pr.owner}/${a.pr.repo}#${a.pr.number}`,
                                JSON.stringify({ [PATH]: "an-older-blob" }));
    const { container } = render(<Page />);
    expect(screen.getByRole("checkbox", { name: "Viewed" })).not.toBeChecked();
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
  });
});

describe("Got it, next and the keys", () => {
  it("Got it, next marks the step understood and goes to the next one", async () => {
    open(hashOf(step1.id));
    fireEvent.click(screen.getByRole("button", { name: "Got it, next (y)" }));
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step2.id)));
    expect(understood).toHaveBeenCalledWith(step1.id, true);
    expect(useStore.getState().understand?.understood[step1.id]).toBeTruthy();
    expect(within(stepNav()).getAllByRole("link")[0]).toHaveTextContent("understood");
  });

  it("y and → do the same, skipping steps already understood; after the last one, the end screen", async () => {
    useStore.setState({ understand: makeUnderstand({ understood: { [step2.id]: "t" } }) });
    open(hashOf(step1.id));
    await press("y");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step3.id)));
    await press("ArrowRight");
    await waitFor(() => expect(window.location.hash).toBe("#/understand/end"));
    expect(understood.mock.calls).toEqual([[step1.id, true], [step3.id, true]]);
    expect(await screen.findByRole("heading", { level: 2, name: "You've walked through all 3 steps" })).toBeInTheDocument();
  });

  it("shift+→ is left alone (it extends a text selection)", async () => {
    open(hashOf(step1.id));
    await press("ArrowRight", { shiftKey: true });
    expect(understood).not.toHaveBeenCalled();
    expect(window.location.hash).toBe(hashOf(step1.id));
  });

  it("j and k step through the steps without marking them; j after the last opens the end screen", async () => {
    open(hashOf(step2.id));
    await press("j");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step3.id)));
    await press("j");
    await waitFor(() => expect(window.location.hash).toBe("#/understand/end"));
    await press("k");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step3.id)));
    await press("k");
    await press("k");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step1.id)));
    await press("k"); // nothing before the first step
    expect(window.location.hash).toBe(hashOf(step1.id));
    expect(understood).not.toHaveBeenCalled();
  });

  it("c opens the chat with a chip carrying the step's path, hunks and explanation", async () => {
    open(hashOf(step1.id));
    await screen.findByRole("region", { name: "Before" });
    await press("c");
    const st = useStore.getState();
    expect(st.chatOpen).toBe(true);
    expect(st.chips).toEqual([
      expect.objectContaining({ path: PATH, side: "RIGHT", start: 1, end: 5, step_id: step1.id, label: "Step 1 · core.py" }),
    ]);
    const text = st.chips[0].text;
    expect(text).toContain("@@ -1,4 +1,5 @@");
    expect(text).toContain("Before: `x` was 1, and nothing else was set.");
    expect(text).toContain("What changes: `x` becomes 2 and a new `y` is set to 3.");
    expect(text).toContain("Why: The description asks for a bigger `x`. (Source: PR description)");
    expect(text).toContain("- src/other.py:10: reads `x` (outside this PR)");
    expect(text).toContain("- x: How big the batch is.");
    expect(window.location.hash).toBe(hashOf(step1.id));
  });

  it("the Ask about this button does the same", () => {
    open(hashOf(step3.id));
    fireEvent.click(screen.getByRole("button", { name: "Ask about this (c)" }));
    expect(useStore.getState().chips[0]).toMatchObject({ path: "src/api.py", start: 8, end: 13, step_id: step3.id, label: "Step 3 · api.py" });
  });

  it("moving focuses the step heading and reads out Step N of M", async () => {
    open(hashOf(step1.id));
    expect(heading()).toHaveFocus();
    expect(heading()).toHaveAttribute("tabindex", "-1");
    await press("j");
    const next = await screen.findByRole("heading", { level: 2, name: "src/util.py" });
    expect(next).toHaveFocus();
    expect(await screen.findByText("Step 2 of 3", { selector: "[aria-live='polite']" })).toBeInTheDocument();
  });

  it("outside the view every key keeps its meaning", async () => {
    const commentOnSelection = vi.fn();
    const nav = { gotIt: vi.fn(), chat: vi.fn(), next: vi.fn(), prev: vi.fn() };
    const real = vi.fn();
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    function Keys() {
      useGlobalKeys();
      return null;
    }
    render(<Keys />);
    registerFileNav({ nextNote: vi.fn(), prevNote: vi.fn(), commentOnSelection });
    registerUnderstandNav(nav);
    registerReviewNav({ real, not: vi.fn(), unsure: vi.fn(), chat: vi.fn(), next: vi.fn(), prev: vi.fn(), undo: vi.fn() });
    for (const key of ["y", "ArrowRight", "c", "j", "k"]) await press(key);
    expect(commentOnSelection).toHaveBeenCalledTimes(1); // c comments on the picked lines
    expect(Object.values(nav).every((f) => f.mock.calls.length === 0)).toBe(true);

    window.location.hash = `#/review/${finding.id}`;
    await press("y");
    expect(real).toHaveBeenCalledTimes(1); // y is Real problem in the Review
    expect(nav.gotIt).not.toHaveBeenCalled();

    window.location.hash = hashOf(step1.id);
    await press("y");
    await press("c");
    expect(nav.gotIt).toHaveBeenCalledTimes(1);
    expect(nav.chat).toHaveBeenCalledTimes(1);
    expect(commentOnSelection).toHaveBeenCalledTimes(1);
    registerFileNav(null);
    registerUnderstandNav(null);
    registerReviewNav(null);
  });
});

describe("the level switch", () => {
  it("I know this area saves the level and fetches the step again at that level", async () => {
    const save = vi.spyOn(api, "setUnderstandLevel").mockImplementation(async (level: UnderstandLevel) => ({ level }));
    open(hashOf(step1.id));
    await screen.findByRole("region", { name: "Terms" });
    const group = screen.getByRole("group", { name: "Background" });
    expect(within(group).getByRole("button", { name: "I'm new to this area" })).toHaveAttribute("aria-pressed", "true");
    explain.mockImplementation(async (id, level) => explanation({ id, level, before: "`x` was 1.", terms: [] }));
    fireEvent.click(within(group).getByRole("button", { name: "I know this area" }));
    expect(save).toHaveBeenCalledWith("familiar");
    await waitFor(() => expect(explain).toHaveBeenCalledWith(step1.id, "familiar"));
    expect(await screen.findByRole("region", { name: "Before" })).toHaveTextContent(/^Beforex was 1\.$/);
    expect(screen.queryByRole("region", { name: "Terms" })).not.toBeInTheDocument();
    expect(within(group).getByRole("button", { name: "I know this area" })).toHaveAttribute("aria-pressed", "true");
  });

  it("goes back to the old level when saving fails", async () => {
    vi.spyOn(api, "setUnderstandLevel").mockRejectedValue(new Error("disk full"));
    open(hashOf(step1.id));
    fireEvent.click(screen.getByRole("button", { name: "I know this area" }));
    await waitFor(() => expect(useStore.getState().toasts.at(-1)?.text).toBe("Could not save the level: disk full"));
    expect(useStore.getState().understand?.level).toBe("new");
  });
});

describe("the end screen", () => {
  it("lists the steps you skipped and hands off to Start review", async () => {
    useStore.setState({ understand: makeUnderstand({ understood: { [step1.id]: "t" } }) });
    open("#/understand/end");
    expect(heading()).toHaveTextContent("You've walked through all 3 steps");
    expect(heading()).toHaveFocus();
    expect(screen.getByText("1 understood · 2 skipped")).toBeInTheDocument();
    const skipped = within(screen.getByRole("region", { name: "Steps you skipped" })).getAllByRole("listitem");
    expect(skipped.map((li) => li.textContent)).toEqual([
      "Step 2 · src/util.py · Chapter 1: The data",
      "Step 3 · src/api.py · Chapter 2: The API",
    ]);
    expect(within(skipped[0]).getByRole("link")).toHaveAttribute("href", hashOf(step2.id));
    fireEvent.click(screen.getByRole("button", { name: "Start review" }));
    expect(window.location.hash).toBe("#/review");
  });

  it("k goes back to the last step", async () => {
    open("#/understand/end");
    await press("k");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(step3.id)));
  });
});

describe("the Review's link to a step", () => {
  it('a stop inside a step\'s hunks shows "How this code works (step N)"', () => {
    open(`#/review/${finding.id}`);
    expect(screen.getByRole("link", { name: "How this code works (step 1)" })).toHaveAttribute("href", hashOf(step1.id));
  });

  it("a stop outside every step's hunks shows no link", () => {
    useStore.setState({
      understand: makeUnderstand({ steps: [{ ...step1, hunks: [{ old_start: 20, old_lines: 2, new_start: 20, new_lines: 4 }] }] }),
      state: makeState({ stop_verdicts: { [finding.id]: verdictFor(finding, "not") } }),
    });
    open(`#/review/${finding.id}`);
    expect(screen.queryByRole("link", { name: /How this code works/ })).not.toBeInTheDocument();
  });
});
