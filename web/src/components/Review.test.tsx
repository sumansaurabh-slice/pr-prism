import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useGlobalKeys } from "../keyboard";
import { useRoute } from "../router";
import { useStore } from "../store";
import { makeDraft, makePreview, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import { ci, finding, lookFor, low, makeStop, more, note, reviewAnalysis, STOPS, stopId, verdictFor } from "../test/stops";
import type { Coverage } from "../types";
import { Overview } from "./Overview";
import { ReviewView } from "./ReviewView";
import { SubmitModal } from "./SubmitModal";

function Page() {
  useGlobalKeys();
  const route = useRoute();
  return (
    <>
      {route.view === "review" ? <ReviewView route={route} /> : <p>Somewhere else</p>}
      <SubmitModal />
    </>
  );
}

const coverage = (over: Partial<Coverage> = {}): Coverage => ({
  not_checked: [], unsure: [], skipped_areas: [], high_risk_unseen: { hunks: 0, files: 0, by_file: {} },
  low_confidence: 0, running: false, count: 0, ...over,
});

function open(hash: string) {
  window.location.hash = hash;
  return render(<Page />);
}

const heading = () => screen.getByRole("heading", { level: 2 });
const stopNav = () => screen.getByRole("navigation", { name: "Review stops" });

beforeEach(() => {
  resetStore({ analysis: reviewAnalysis(), state: makeState() });
  vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  vi.spyOn(api, "coverage").mockResolvedValue(coverage());
  vi.spyOn(api, "previewReview").mockResolvedValue(makePreview());
});
afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("the Overview's plan box (DT7)", () => {
  it("shows the counts from the stop data and Start review", () => {
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Review plan" });
    expect(box).toHaveTextContent("4 places need a human · 61 files have nothing flagged · ~8 min");
    expect(box).toHaveTextContent("and 1 more past the list");
    // It opens the Overview, above Claude's summary and the blocker banner.
    expect(box.closest(".overview-main")?.firstElementChild).toBe(box);
    const start = within(box).getByRole("button", { name: "Start review" });
    expect(start).toHaveClass("btn-primary");
    fireEvent.click(start);
    expect(window.location.hash).toBe("#/review");
  });

  it("turns into Resume review (N left) after the first answer, with the time left", () => {
    useStore.setState({ state: makeState({ stop_verdicts: { [finding.id]: verdictFor(finding, "not") } }) });
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Review plan" });
    expect(within(box).getByRole("button", { name: "Resume review (3 left)" })).toBeInTheDocument();
    expect(box).toHaveTextContent("~6 min");
  });

  it("says Claude is still checking while there are no stops yet", () => {
    useStore.setState({ analysis: reviewAnalysis({ stops: [], status: "deepchecking" }) });
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Review plan" });
    expect(box).toHaveTextContent("Claude is still checking. Stops will appear here as each area finishes.");
    expect(within(box).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("routes (DT7)", () => {
  it("a deep link opens its stop", async () => {
    open(`#/review/${note.id}`);
    expect(heading()).toHaveTextContent("Is this safe? x changes from 1 to 2.");
    const current = within(stopNav()).getByRole("link", { current: "step" });
    expect(current).toHaveAttribute("href", `#/review/${note.id}`);
    expect(current).toHaveTextContent(/^3/);
  });

  it("#/review resumes at the first stop not answered yet", async () => {
    useStore.setState({ state: makeState({ stop_verdicts: { [ci.id]: verdictFor(ci, "real") } }) });
    open("#/review");
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${finding.id}`));
    expect(await screen.findByRole("heading", { level: 2, name: "Can y be left unused?" })).toBeInTheDocument();
  });
});

describe("the stop list (DT8)", () => {
  it("rows show the number, kind, file:line and the answer as icon plus text", () => {
    useStore.setState({
      state: makeState({ stop_verdicts: { [finding.id]: verdictFor(finding, "real"), [note.id]: verdictFor(note, "unsure") } }),
    });
    open(`#/review/${ci.id}`);
    const rows = within(stopNav()).getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("#/review/"));
    expect(rows.map((r) => r.textContent)).toEqual([
      "1unit-testsCIOpen",
      "2core.py:3findingReal problem",
      "3core.py:2risk noteNot sure",
      "4core.py:2checkOpen",
    ]);
    // icon plus text: the answer never depends on color alone
    const real = within(rows[1]).getByText("Real problem").closest(".Label") as HTMLElement;
    expect(real.querySelector("svg")).not.toBeNull();
    expect(rows[0]).toHaveAttribute("aria-current", "step");
    expect(rows[1]).not.toHaveAttribute("aria-current");
  });

  it('folds "N more places", and the switch adds low-confidence findings', () => {
    open(`#/review/${finding.id}`);
    const nav = stopNav();
    const toggle = within(nav).getByRole("button", { name: "1 more place" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(nav).queryByText("core.py:4")).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(nav).getByText("core.py:4")).toBeInTheDocument();

    expect(within(nav).queryByText("core.py:1")).not.toBeInTheDocument();
    const lowSwitch = within(nav).getByRole("switch", { name: /Include low-confidence findings/ });
    fireEvent.click(lowSwitch);
    expect(useStore.getState().includeLow).toBe(true);
    expect(within(nav).getByText("core.py:1")).toBeInTheDocument();
    const rows = within(nav).getAllByRole("link").filter((a) => a.getAttribute("href")?.startsWith("#/review/"));
    expect(rows).toHaveLength(6); // 5 listed + the opened more place
  });

  it('ends with "N files have nothing flagged" linking to Files changed', () => {
    open(`#/review/${finding.id}`);
    const link = within(stopNav()).getByRole("link", { name: "61 files have nothing flagged →" });
    expect(link).toHaveAttribute("href", "#/file/docs%2Fnotes.md");
  });
});

describe("the stop view (DT9)", () => {
  it("reads question → hunk → How it could happen → Claude's view → action bar", async () => {
    const { container } = open(`#/review/${finding.id}`);
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull()); // the hunk is drawn
    const parts = [
      heading(),
      container.querySelector(".file-box"),
      screen.getByText("How it could happen"),
      screen.getByRole("region", { name: "Claude's view" }),
      container.querySelector(".stop-bar"),
    ] as HTMLElement[];
    for (let i = 1; i < parts.length; i++) {
      expect(parts[i - 1].compareDocumentPosition(parts[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    }
    expect(heading()).toHaveClass("stop-question");
    expect(screen.getByText("Must fix")).toHaveClass("Label");
    expect(screen.getByText("correctness")).toHaveClass("Label");
    expect(within(parts[2].closest("section")!).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      "A caller sets y.", "Nothing reads it.",
    ]);
    expect(screen.getByRole("region", { name: "Claude's view" })).toHaveTextContent("70% sure · second look 7/10");
    // the stop's line is marked in the unified hunk
    expect(screen.getByText("line 3 marked")).toBeInTheDocument();
  });

  it("has three equal default buttons with their keys, and no primary button in the bar", () => {
    const { container } = open(`#/review/${finding.id}`);
    const bar = container.querySelector(".stop-bar") as HTMLElement;
    const names = ["Real problem (y)", "Not a problem (n)", "Not sure (?)"];
    const buttons = names.map((name) => within(bar).getByRole("button", { name }));
    for (const b of buttons) expect(b.className).toBe("btn");
    expect(bar.querySelector(".btn-primary")).toBeNull();
    expect(bar).toHaveTextContent("Stop 2 of 4 · ~8 min left");
    expect(bar).toHaveTextContent("y real · n not · ? not sure · c ask in chat · u undo · j/k next/previous");
  });

  it('shows "second look pending" and "not second-checked"', () => {
    useStore.setState({
      analysis: reviewAnalysis({
        stops: [makeStop({ id: stopId(7), second_look: "pending", judge_score: null }), makeStop({ id: stopId(8), second_look: "none", order: 10 })],
      }),
    });
    const { unmount } = open(`#/review/${stopId(7)}`);
    expect(screen.getByRole("region", { name: "Claude's view" })).toHaveTextContent("70% sure · second look pending");
    unmount();
    open(`#/review/${stopId(8)}`);
    expect(screen.getByRole("region", { name: "Claude's view" })).toHaveTextContent("70% sure · not second-checked");
  });

  it('an answered stop shows "You said: Real problem · comment added [Change answer]"', () => {
    useStore.setState({
      state: makeState({
        stop_verdicts: { [finding.id]: verdictFor(finding, "real") },
        drafts: [makeDraft({ id: "dr", line: 3, origin_id: finding.id, body: "y is never used" })],
      }),
    });
    const { container } = open(`#/review/${finding.id}`);
    const bar = container.querySelector(".stop-bar") as HTMLElement;
    expect(bar).toHaveTextContent("You said: Real problem · comment added");
    expect(within(bar).getByRole("button", { name: "Change answer" })).toBeInTheDocument();
    expect(within(bar).queryByRole("button", { name: "Real problem (y)" })).not.toBeInTheDocument();
  });

  it("a CI stop shows the failing check instead of a hunk", () => {
    open(`#/review/${ci.id}`);
    expect(screen.getByRole("heading", { level: 3, name: "unit-tests" })).toBeInTheDocument();
    expect(screen.getByText("failing")).toHaveClass("Label");
  });
});

describe("live stops (DT11)", () => {
  it("a new stop sorted before you shows a link and leaves the current stop in place", async () => {
    open(`#/review/${note.id}`);
    expect(within(stopNav()).getByRole("link", { current: "step" })).toHaveTextContent(/^3/);
    const added = makeStop({ id: stopId(7), question: "Is the new import used?", line: 1, finding_id: "f7", order: 0.5 });
    vi.spyOn(api, "analysis").mockResolvedValue(reviewAnalysis({ stops: [...STOPS, added].sort((a, b) => a.order - b.order) }));
    vi.spyOn(api, "cost").mockRejectedValue(new Error("no cost"));
    await act(() => useStore.getState().loadAnalysis());

    expect(heading()).toHaveTextContent("Is this safe?");
    expect(window.location.hash).toBe(`#/review/${note.id}`);
    const current = within(stopNav()).getByRole("link", { current: "step" });
    expect(current).toHaveTextContent(/^4/); // one more stop above it now
    expect(within(stopNav()).getByText("new")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "1 new stop earlier in the list" }));
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${stopId(7)}`));
  });

  it('a stop the second look drops while you are on it stays, labelled "Second look dropped this"', async () => {
    open(`#/review/${finding.id}`);
    expect(screen.queryByText("Second look dropped this")).not.toBeInTheDocument();
    act(() => {
      useStore.setState({
        analysis: reviewAnalysis({ stops: STOPS.map((s) => (s.id === finding.id ? { ...s, low_confidence: true } : s)) }),
      });
    });
    expect(heading()).toHaveTextContent("Can y be left unused?");
    expect(screen.getByText("Second look dropped this")).toHaveClass("Label");
    expect(within(stopNav()).getByRole("link", { current: "step" })).toHaveTextContent(/^2/);
    // it can still be answered
    expect(screen.getByRole("button", { name: "Not a problem (n)" })).toBeEnabled();

    act(() => {
      useStore.setState({ analysis: reviewAnalysis({ stops: STOPS.filter((s) => s.id !== finding.id) }) });
    });
    expect(heading()).toHaveTextContent("Can y be left unused?");
    expect(screen.getByText("Second look dropped this")).toBeInTheDocument();
  });

  it("while Claude runs, progress reads answered · left · more coming (deep check N of M areas)", () => {
    const a = reviewAnalysis({ status: "deepchecking" });
    a.progress = { ...a.progress, status: "deepchecking", deepcheck_done: 2, deepcheck_total: 4 };
    useStore.setState({ analysis: a, state: makeState({ stop_verdicts: { [ci.id]: verdictFor(ci, "real") } }) });
    const { container } = open(`#/review/${finding.id}`);
    expect(container.querySelector(".stop-bar")).toHaveTextContent("1 answered · 3 left · more coming (deep check 2 of 4 areas)");
  });
});

describe("empty states (DT11)", () => {
  it("says Claude is still checking until the analysis is complete", () => {
    useStore.setState({ analysis: reviewAnalysis({ stops: [], status: "deepchecking" }) });
    open("#/review");
    expect(screen.getByRole("status")).toHaveTextContent("Claude is still checking. Stops will appear here as each area finishes.");
    expect(screen.queryByText(/Nothing risky found/)).not.toBeInTheDocument();
  });

  it('says "Nothing risky found" only when complete with nothing skipped', () => {
    useStore.setState({ analysis: reviewAnalysis({ stops: [] }) });
    const { unmount } = open("#/review");
    expect(screen.getByRole("status")).toHaveTextContent("Nothing risky found.");
    unmount();
    useStore.setState({ analysis: reviewAnalysis({ stops: [], skipped_areas: [{ title: "API surface", reason: "timed out" }] }) });
    open("#/review");
    expect(screen.queryByText(/Nothing risky found/)).not.toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("API surface (timed out)");
  });
});

describe("the end screen (DT13)", () => {
  it("with real problems: the comments, the not-sure ones, the coverage line and one Submit", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(coverage({ count: 2, not_checked: [
      { stop_id: lookFor.id, path: PATH, line: 2, question: lookFor.question, kind: "look_for" },
      { stop_id: more.id, path: PATH, line: 4, question: more.question, kind: "finding" },
    ] }));
    useStore.setState({
      state: makeState({
        stop_verdicts: {
          [ci.id]: verdictFor(ci, "not"), [finding.id]: verdictFor(finding, "real"), [note.id]: verdictFor(note, "unsure"),
        },
        drafts: [makeDraft({ id: "dr", line: 3, origin_id: finding.id, body: "Please drop `y`." })],
      }),
    });
    open("#/review/end");
    expect(heading()).toHaveTextContent("1 real problem is ready to post");
    const card = screen.getByText(/Please drop/).closest(".review-comment") as HTMLElement;
    expect(within(card).getByRole("button", { name: "Edit" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Go back to the 1 you weren't sure about" }));
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${note.id}`));
  });

  it('with none: "No problems in N stops", and Submit opens on Comment, never Approve', async () => {
    useStore.setState({
      state: makeState({ stop_verdicts: Object.fromEntries([ci, finding, note, lookFor].map((s) => [s.id, verdictFor(s, "not")])) }),
    });
    open("#/review/end");
    expect(heading()).toHaveTextContent("No problems in 4 stops");
    expect(await screen.findByText("You looked at every place that needed a human")).toBeInTheDocument();

    // Even after Approve was picked in an earlier opening, Submit from here starts on Comment.
    act(() => useStore.getState().setSubmitOpen(true));
    fireEvent.click(await screen.findByRole("radio", { name: /Approve/ }));
    act(() => useStore.getState().setSubmitOpen(false));
    const submits = screen.getAllByRole("button", { name: "Submit review" });
    expect(submits).toHaveLength(1);
    fireEvent.click(submits[0]);
    expect(useStore.getState().submitOpen).toBe(true);
    expect(await screen.findByRole("radio", { name: /Comment/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Approve/ })).not.toBeChecked();
  });

  it("shows the coverage line from GET /api/coverage and lists earlier answers", async () => {
    const gone = makeStop({ id: stopId(7), question: "Was `z` counted twice?", dropped: true, order: 6 });
    vi.spyOn(api, "coverage").mockResolvedValue(coverage({ count: 3, skipped_areas: [{ title: "API surface", reason: "timed out" }] }));
    useStore.setState({
      analysis: reviewAnalysis({ stops: [...STOPS, gone] }),
      state: makeState({ stop_verdicts: { [gone.id]: verdictFor(gone, "not") } }),
    });
    open("#/review/end");
    expect(await screen.findByText("3 places not checked yet")).toBeInTheDocument();
    expect(screen.getByText("Earlier answers")).toBeInTheDocument();
    expect(screen.getByText(/You said: Not a problem · Second look dropped this/)).toBeInTheDocument();
  });
});

describe("focus and landmarks (DT15)", () => {
  it("moving to another stop focuses its heading and reads out Stop N of M", async () => {
    open(`#/review/${finding.id}`);
    expect(heading()).toHaveFocus();
    expect(heading()).toHaveAttribute("tabindex", "-1");
    fireEvent.keyDown(window, { key: "j" });
    const next = await screen.findByRole("heading", { level: 2, name: /Is this safe\?/ });
    expect(next).toHaveFocus();
    expect(await screen.findByText("Stop 3 of 4", { selector: "[aria-live='polite']" })).toBeInTheDocument();
    expect(stopNav().tagName).toBe("NAV");
  });

  it("a click in the list moves too", async () => {
    open(`#/review/${finding.id}`);
    fireEvent.click(within(stopNav()).getByRole("link", { name: /^4/ }));
    window.location.hash = `#/review/${lookFor.id}`; // jsdom doesn't follow hash links on click
    expect(await screen.findByRole("heading", { level: 2, name: "Do callers handle x = 2?" })).toHaveFocus();
  });
});

describe("low-confidence findings", () => {
  it("are not in the list unless the switch is on", () => {
    open(`#/review/${finding.id}`);
    expect(screen.getByText("Stop 2 of 4 · ~8 min left")).toBeInTheDocument();
    act(() => useStore.getState().setIncludeLow(true));
    expect(screen.getByText(/Stop 2 of 5/)).toBeInTheDocument();
    expect(within(stopNav()).getByText("core.py:1")).toBeInTheDocument();
    act(() => useStore.getState().setIncludeLow(false));
    expect(low.low_confidence).toBe(true);
  });
});
