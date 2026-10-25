import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useStore } from "../store";
import { makeAnalysis, makeDraft, makePreview, makeState, resetStore } from "../test/fixtures";
import { SubmitModal } from "./SubmitModal";

const SAVE_FAILED = "Couldn't save this answer for eval";

function toastTexts(): string[] {
  return useStore.getState().toasts.map((t) => t.text);
}

describe("an eval label that couldn't be saved", () => {
  afterEach(() => vi.restoreAllMocks());

  it("Submit still sends the review, and says so in a toast", async () => {
    resetStore({ analysis: makeAnalysis(), submitOpen: true, state: makeState({ drafts: [makeDraft({ id: "d1" })] }) });
    vi.spyOn(api, "previewReview").mockResolvedValue(makePreview({ inline_count: 1 }));
    vi.spyOn(api, "state").mockResolvedValue(makeState());
    const submit = vi.spyOn(api, "submitReview").mockResolvedValue({
      ok: true, review_id: 1, html_url: "https://github.com/o/r/pull/7#review-1", warning: SAVE_FAILED,
    });
    render(<SubmitModal />);
    await screen.findByText(/comment on lines/);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Submit review" })));
    expect(submit).toHaveBeenCalled();
    expect(await screen.findByText("Your review is now on GitHub.")).toBeInTheDocument();
    expect(toastTexts()).toEqual(["Review sent", SAVE_FAILED]);
  });

  it("deleting a pending comment still deletes it, and says so in a toast", async () => {
    resetStore({ state: makeState({ drafts: [makeDraft({ id: "d1", origin_id: "s-1" })] }) });
    vi.spyOn(api, "deleteDraft").mockResolvedValue({ ok: true, warning: SAVE_FAILED });
    await act(() => useStore.getState().deleteDraft("d1"));
    expect(useStore.getState().state.drafts).toEqual([]);
    expect(toastTexts()).toEqual([SAVE_FAILED]);
  });

  it("no toast when the label was saved", async () => {
    resetStore({ state: makeState({ drafts: [makeDraft({ id: "d1" })] }) });
    vi.spyOn(api, "deleteDraft").mockResolvedValue({ ok: true });
    await act(() => useStore.getState().deleteDraft("d1"));
    expect(toastTexts()).toEqual([]);
  });
});
