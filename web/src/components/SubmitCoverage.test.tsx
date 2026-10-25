import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useCoverage } from "../coverage";
import { useStore } from "../store";
import { makeCoverage, makeStop, place, sid } from "../test/coverageFixtures";
import { makeAnalysis, makeDraft, makePreview, makeState, resetStore } from "../test/fixtures";
import type { Coverage } from "../types";
import { SAVE_BODY_MS, SubmitModal } from "./SubmitModal";

const two = makeCoverage({
  not_checked: [
    place("a", "app/delivery/retry.py", 14, "Should a 4xx answer stop the retries at once?"),
    place("b", null, null, "Is this failure caused by this PR? `lint` fails."),
  ],
});

function openWith(
  coverage: Coverage | Promise<Coverage> | Error, state = makeState({ drafts: [makeDraft()] }), analysis = makeAnalysis(),
) {
  resetStore({ analysis, submitOpen: true, state });
  useCoverage.setState({ coverage: null, error: null });
  vi.spyOn(api, "previewReview").mockResolvedValue(makePreview({ inline_count: 1 }));
  vi.spyOn(api, "saveReviewBody").mockResolvedValue({ ok: true });
  if (coverage instanceof Error) vi.spyOn(api, "coverage").mockRejectedValue(coverage);
  else vi.spyOn(api, "coverage").mockReturnValue(Promise.resolve(coverage));
  return render(<SubmitModal />);
}

const pickApprove = () => fireEvent.click(screen.getByRole("radio", { name: /^Approve/ }));
const submitButton = () => screen.getByRole("button", { name: "Submit review" });

describe("coverage in the Submit dialog", () => {
  afterEach(() => vi.restoreAllMocks());

  it("says it is checking while coverage loads", async () => {
    openWith(new Promise<Coverage>(() => {}));
    expect(screen.getByText("Checking what you've looked at…")).toBeInTheDocument();
    await screen.findByText(/comment on lines/);
  });

  it("says it couldn't check, and Submit stays enabled", async () => {
    openWith(new Error("502 Bad Gateway"));
    expect(await screen.findByText("Couldn't check coverage")).toBeInTheDocument();
    await screen.findByText(/comment on lines/);
    expect(submitButton()).toBeEnabled();
  });

  it("says you looked at everything when nothing is left", async () => {
    openWith(makeCoverage());
    expect(await screen.findByText("You looked at every place that needed a human")).toBeInTheDocument();
    pickApprove();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("says the list may grow while the analysis runs", async () => {
    openWith(makeCoverage({ ...two, running: true }));
    expect(await screen.findByText("Analysis still running; this list may grow")).toBeInTheDocument();
  });

  it("with Comment: a muted line, and the places behind [show]", async () => {
    openWith(two);
    const line = await screen.findByText(/places not checked yet/);
    expect(line).toHaveTextContent("2 places not checked yet show");
    expect(line).toHaveClass("color-muted");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "Places not checked yet" })).not.toBeInTheDocument();

    fireEvent.click(within(line).getByRole("button", { name: "show" }));
    const list = screen.getByRole("list", { name: "Places not checked yet" });
    const links = within(list).getAllByRole("link");
    expect(links.map((a) => a.textContent)).toEqual([
      "retry.py:14 · Should a 4xx answer stop the retries at once?",
      "CI · Is this failure caused by this PR? lint fails.",
    ]);
    expect(links[0]).toHaveAttribute("href", `#/review/${sid("a")}`);
    await screen.findByText(/comment on lines/);
    expect(submitButton()).toBeEnabled(); // a warning only
  });

  it("with Approve: a warning that names how many risky places are left", async () => {
    openWith(two);
    await screen.findByText(/places not checked yet/);
    pickApprove();
    const warning = screen.getByRole("alert");
    expect(warning).toHaveClass("flash", "flash-warn");
    expect(warning).toHaveTextContent("You're approving with 2 risky places not checked");
    expect(within(warning).getAllByRole("link")).toHaveLength(2);
    expect(screen.queryByText(/places not checked yet/)).not.toBeInTheDocument();
    // Back to Comment: the muted line again.
    fireEvent.click(screen.getByRole("radio", { name: /^Comment/ }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByText(/2 places not checked yet/)).toBeInTheDocument();
  });

  it("lists at most 5 places, then 'and N more'; skipped deep-check areas count too", async () => {
    const tags = ["1", "2", "3", "4", "5", "6"];
    openWith(
      makeCoverage({
        not_checked: tags.map((t) => place(t, `src/f${t}.py`, Number(t), `Question ${t}?`)),
        skipped_areas: [{ title: "Behavior & API surface", reason: "timed out after 10m 0s" }],
      }),
    );
    await screen.findByText(/not checked yet/);
    pickApprove();
    expect(screen.getByRole("alert")).toHaveTextContent("You're approving with 7 risky places not checked");
    const list = screen.getByRole("list", { name: "Places not checked yet" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(6);
    expect(within(list).getAllByRole("link")).toHaveLength(5);
    expect(rows[5]).toHaveTextContent("and 2 more");
  });

  it("shows unseen high-risk code and low-confidence findings on their own collapsed lines, outside the count", async () => {
    const low = makeStop({ id: sid("c"), low_confidence: true, finding_id: "f9", question: "Is this a real problem? x" });
    openWith(
      makeCoverage({
        not_checked: [place("a", "src/a.py", 4, "Is a nil map safe here?")],
        high_risk_unseen: {
          hunks: 12, files: 3,
          by_file: { "src/a.py": { seen: 2, total: 5 }, "src/b.py": { seen: 0, total: 4 }, "src/c.py": { seen: 1, total: 4 }, "src/d.py": { seen: 3, total: 3 } },
        },
        low_confidence: 1,
      }),
      makeState({ drafts: [makeDraft()] }),
      makeAnalysis({ stops: [low] }),
    );
    expect(await screen.findByText(/1 place not checked yet/)).toBeInTheDocument();
    pickApprove();
    expect(screen.getByRole("alert")).toHaveTextContent("You're approving with 1 risky place not checked");

    const high = screen.getByText("High-risk code not seen: 12 hunks in 3 files");
    expect(high.tagName).toBe("SUMMARY");
    const highBox = high.closest("details") as HTMLDetailsElement;
    expect(highBox.open).toBe(false);
    expect(within(highBox).getAllByRole("listitem", { hidden: true }).map((li) => li.textContent)).toEqual([
      "a.py · 2 of 5 risky hunks seen", "b.py · 0 of 4 risky hunks seen", "c.py · 1 of 4 risky hunks seen",
    ]);

    const lowLine = screen.getByText("Low confidence, not checked: 1");
    expect(lowLine.tagName).toBe("SUMMARY");
    expect((lowLine.closest("details") as HTMLDetailsElement).open).toBe(false);
    expect(within(lowLine.closest("details")!).getByRole("link", { hidden: true })).toHaveAttribute(
      "href", "#/file/src%2Fcore.py?line=3",
    );
  });

  it("a jump link saves the summary, closes the dialog and goes to the stop", async () => {
    vi.useFakeTimers();
    try {
      openWith(two);
      await act(async () => {});
      fireEvent.change(screen.getByRole("textbox", { name: "Summary" }), { target: { value: "Half written" } });
      fireEvent.click(screen.getByRole("button", { name: "show" }));
      const link = screen.getByRole("link", { name: /retry\.py:14/ });
      await act(async () => fireEvent.click(link));
      expect(api.saveReviewBody).toHaveBeenCalledWith("Half written");
      expect(useStore.getState().submitOpen).toBe(false);
      expect(window.location.hash).toBe(`#/review/${sid("a")}`);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("the review summary autosaves", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("opens with the saved summary", async () => {
    openWith(makeCoverage(), makeState({ review_body_draft: "Looks good apart from the retry loop." }));
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Looks good apart from the retry loop.");
    await screen.findByText(/comment on lines/);
  });

  it("saves once you stop typing", async () => {
    vi.useFakeTimers();
    openWith(makeCoverage());
    await act(async () => {});
    const box = screen.getByRole("textbox", { name: "Summary" });
    for (const text of ["L", "Lo", "Look"]) {
      fireEvent.change(box, { target: { value: text } });
      act(() => vi.advanceTimersByTime(SAVE_BODY_MS - 100));
    }
    expect(api.saveReviewBody).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTime(100));
    expect(api.saveReviewBody).toHaveBeenCalledTimes(1);
    expect(api.saveReviewBody).toHaveBeenCalledWith("Look");
    expect(useStore.getState().state.review_body_draft).toBe("Look");
  });

  it("saves what's left when the dialog closes", async () => {
    vi.useFakeTimers();
    openWith(makeCoverage());
    await act(async () => {});
    fireEvent.change(screen.getByRole("textbox", { name: "Summary" }), { target: { value: "Closing now" } });
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Cancel" })));
    expect(api.saveReviewBody).toHaveBeenCalledWith("Closing now");
  });

  it("shows text added elsewhere (a CI stop's Real problem) while it is open", async () => {
    openWith(makeCoverage(), makeState({ drafts: [makeDraft()], review_body_draft: "Mine." }));
    await screen.findByText(/comment on lines/);
    act(() => useStore.setState((s) => ({ state: { ...s.state, review_body_draft: "Mine.\n\nCI: lint fails here." } })));
    expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Mine.\n\nCI: lint fails here.");
  });

  it("re-reads the saved summary when it opens (a CI answer or another tab may have added to it)", async () => {
    vi.spyOn(api, "state").mockResolvedValue(makeState({ review_body_draft: "Mine.\n\nCI: lint fails here." }));
    openWith(makeCoverage(), makeState({ drafts: [makeDraft()], review_body_draft: "Mine." }));
    await waitFor(() =>
      expect(screen.getByRole("textbox", { name: "Summary" })).toHaveValue("Mine.\n\nCI: lint fails here."),
    );
    expect(api.saveReviewBody).not.toHaveBeenCalled();
  });

  it("is cleared after the review is posted", async () => {
    vi.spyOn(api, "state")
      .mockResolvedValueOnce(makeState({ drafts: [makeDraft()], review_body_draft: "Ship it." }))
      .mockResolvedValue(makeState({ review_body_draft: "" }));
    openWith(makeCoverage(), makeState({ drafts: [makeDraft()], review_body_draft: "Ship it." }));
    vi.spyOn(api, "submitReview").mockResolvedValue({ ok: true, review_id: 1, html_url: "https://github.com/o/r/pull/7#review-1" });
    await screen.findByText(/comment on lines/);
    await act(async () => fireEvent.click(submitButton()));
    expect(api.submitReview).toHaveBeenCalledWith("Ship it.", "COMMENT", "analyzed");
    await waitFor(() => expect(api.saveReviewBody).toHaveBeenCalledWith(""));
    expect(useStore.getState().state.review_body_draft).toBe("");
  });
});

beforeEach(() => {
  window.location.hash = "#/overview";
});
