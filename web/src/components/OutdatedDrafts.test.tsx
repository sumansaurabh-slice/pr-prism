import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { useGlobalKeys } from "../keyboard";
import { useStore } from "../store";
import { makeAnalysis, makeDraft, makePreview, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import type { Draft } from "../types";
import { DraftsDrawer } from "./Drafts";
import { FileView } from "./FileView";
import { RestoreBanner } from "./ProgressBanner";
import { SubmitModal } from "./SubmitModal";

const old = makeDraft({ id: "old", line: 7, body: "From before", outdated: true, commit_id: "0".repeat(40) });
const current = makeDraft({ id: "cur", line: 2, body: "Why 2?" });

function Page() {
  useGlobalKeys();
  return <FileView path={PATH} line={null} side="RIGHT" full={false} />;
}

/** Press on a line number in the diff and let go: one picked line. */
function pickLine(container: HTMLElement, changeKey: string) {
  const gutter = container.querySelectorAll(`td.diff-gutter[data-change-key="${changeKey}"]`)[1];
  fireEvent.mouseDown(gutter, { button: 0 });
  fireEvent.mouseUp(window);
}

function card(body: string): HTMLElement {
  return screen.getByText(body).closest(".review-comment") as HTMLElement;
}

describe("restored outdated comments in the file view", () => {
  beforeEach(() => {
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    resetStore({ analysis: makeAnalysis(), state: makeState({ drafts: [old, current] }) });
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows them read-only in the file's floating notes, with Re-place and Delete", async () => {
    const { container } = render(<Page />);
    await screen.findByText("Why 2?"); // the diff is drawn (the current comment sits on its line)
    const floating = container.querySelector(".floating-notes") as HTMLElement;
    const outdated = within(floating).getByText("From before").closest(".review-comment") as HTMLElement;
    expect(within(outdated).getByText("Outdated")).toBeInTheDocument();
    expect(within(outdated).getByText(/L7 on an older commit/)).toBeInTheDocument();
    expect(within(outdated).getByRole("button", { name: "Re-place" })).toBeInTheDocument();
    expect(within(outdated).getByRole("button", { name: "Delete" })).toBeInTheDocument();
    expect(within(outdated).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    // The current comment stays on its diff line, editable.
    expect(within(floating).queryByText("Why 2?")).not.toBeInTheDocument();
    expect(within(card("Why 2?")).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("Re-place moves the comment to the line you click", async () => {
    const moved: Draft = { ...old, line: 3, outdated: false, commit_id: "h" };
    const replace = vi.spyOn(api, "replaceDraft").mockResolvedValue(moved);
    const { container } = render(<Page />);
    await screen.findByText("Why 2?");
    fireEvent.click(within(card("From before")).getByRole("button", { name: "Re-place" }));
    expect(screen.getByRole("status")).toHaveTextContent("Click a line in the diff to move this comment there · Esc to cancel");

    await act(async () => pickLine(container, "I3"));
    expect(replace).toHaveBeenCalledWith("old", { side: "RIGHT", line: 3, start_line: null, start_side: null });
    expect(useStore.getState().replacing).toBeNull();
    expect(useStore.getState().state.drafts.find((d) => d.id === "old")).toEqual(moved);
    expect(screen.queryByText(/Click a line in the diff/)).not.toBeInTheDocument();
    // No longer outdated: it now sits on line 3 of the diff, editable, and out of the floating notes.
    expect(container.querySelector(".floating-notes")).toBeNull();
    expect(within(card("From before")).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("Esc cancels the pick, and a click afterwards just picks lines", async () => {
    const replace = vi.spyOn(api, "replaceDraft");
    const { container } = render(<Page />);
    await screen.findByText("Why 2?");
    fireEvent.click(within(card("From before")).getByRole("button", { name: "Re-place" }));
    fireEvent.keyDown(window, { key: "Escape" });
    expect(useStore.getState().replacing).toBeNull();
    expect(screen.queryByText(/Click a line in the diff/)).not.toBeInTheDocument();

    pickLine(container, "I3");
    expect(replace).not.toHaveBeenCalled();
    expect(useStore.getState().selection).toMatchObject({ side: "RIGHT", start: 3, end: 3 });
  });

  it("shows a failed re-place on the comment's card", async () => {
    vi.spyOn(api, "replaceDraft").mockRejectedValue(
      new ApiError(409, "This comment is being posted to GitHub right now. Try again in a moment."),
    );
    const { container } = render(<Page />);
    await screen.findByText("Why 2?");
    fireEvent.click(within(card("From before")).getByRole("button", { name: "Re-place" }));
    await act(async () => pickLine(container, "I2"));
    expect(within(card("From before")).getByRole("alert")).toHaveTextContent(
      "Could not move this comment: This comment is being posted to GitHub right now. Try again in a moment.",
    );
    expect(useStore.getState().state.drafts.find((d) => d.id === "old")?.outdated).toBe(true);
  });
});

describe("pending comments drawer", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    const another = { ...old, id: "old2", line: 9, body: "Also from before" };
    resetStore({ analysis: makeAnalysis(), draftsOpen: true, state: makeState({ drafts: [old, another, current] }) });
  });
  afterEach(() => vi.restoreAllMocks());

  it("says what outdated comments are and that they won't be sent", () => {
    render(<DraftsDrawer />);
    expect(
      screen.getByText("2 comments are from an older commit. Re-place or delete them; they won't be sent until you do."),
    ).toBeInTheDocument();
    expect(within(card("From before")).queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();
    expect(within(card("Why 2?")).getByRole("button", { name: "Edit" })).toBeInTheDocument();
  });

  it("Re-place opens the comment's file in pick mode", () => {
    render(<DraftsDrawer />);
    fireEvent.click(within(card("From before")).getByRole("button", { name: "Re-place" }));
    expect(useStore.getState().replacing).toEqual({ draftId: "old", path: PATH });
    expect(useStore.getState().draftsOpen).toBe(false);
    expect(window.location.hash).toBe(`#/file/${encodeURIComponent(PATH)}`);
  });

  it("Delete removes an outdated comment", async () => {
    const del = vi.spyOn(api, "deleteDraft").mockResolvedValue({ ok: true });
    render(<DraftsDrawer />);
    await act(async () => fireEvent.click(within(card("From before")).getByRole("button", { name: "Delete" })));
    expect(del).toHaveBeenCalledWith("old");
    expect(screen.queryByText("From before")).not.toBeInTheDocument();
  });

  it("shows the server's words when a comment is being posted", async () => {
    const busy = "This comment is being posted to GitHub right now. Try again in a moment.";
    vi.spyOn(api, "deleteDraft").mockRejectedValue(new ApiError(409, busy));
    vi.spyOn(api, "updateDraft").mockRejectedValue(new ApiError(409, busy));
    render(<DraftsDrawer />);
    await act(async () => fireEvent.click(within(card("Why 2?")).getByRole("button", { name: "Delete" })));
    expect(useStore.getState().toasts.at(-1)).toMatchObject({ kind: "error", text: busy });
    expect(screen.getByText("Why 2?")).toBeInTheDocument();

    fireEvent.click(within(card("Why 2?")).getByRole("button", { name: "Edit" }));
    await act(async () => fireEvent.click(within(card("Why 2?")).getByRole("button", { name: "Save" })));
    expect(useStore.getState().toasts.filter((t) => t.text === busy)).toHaveLength(2);
    expect(within(card("Why 2?")).getByRole("textbox")).toBeInTheDocument(); // still editing: nothing lost
  });
});

describe("submit dialog", () => {
  beforeEach(() => {
    const another = { ...old, id: "old2", line: 9 };
    resetStore({ analysis: makeAnalysis(), submitOpen: true, state: makeState({ drafts: [old, another, current] }) });
  });
  afterEach(() => vi.restoreAllMocks());

  it("says how many outdated comments won't be sent", async () => {
    vi.spyOn(api, "previewReview").mockResolvedValue(makePreview({ inline_count: 1 }));
    render(<SubmitModal />);
    expect(await screen.findByText("2 outdated comments won't be sent")).toBeInTheDocument();
    expect(screen.getByText(/comment on lines/)).toHaveTextContent("1 comment on lines.");
  });

  it("says nothing about outdated comments when there are none", async () => {
    useStore.setState({ state: makeState({ drafts: [current] }) });
    vi.spyOn(api, "previewReview").mockResolvedValue(makePreview({ inline_count: 1 }));
    render(<SubmitModal />);
    await screen.findByText(/comment on lines/);
    expect(screen.queryByText(/outdated/)).not.toBeInTheDocument();
  });
});

describe("restore banner", () => {
  const restored = {
    archived_at: "2026-10-23T17:38:00+00:00", restored_at: "2026-10-25T09:00:00+00:00", archive: "pr-7.json",
    chats: 3, pending_comments: 5, need_replacing: 2, viewed_marks: 12,
    message: "Restored your work from Oct 23: 3 chats, 5 pending comments (2 need re-placing), 12 viewed marks.",
  };
  beforeEach(() => resetStore({ state: makeState({ restored }) }));
  afterEach(() => vi.restoreAllMocks());

  it("shows what was restored until you dismiss it", async () => {
    const dismiss = vi.spyOn(api, "dismissRestored").mockResolvedValue({ ok: true });
    render(<RestoreBanner />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveClass("flash");
    expect(banner).toHaveTextContent(restored.message);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Dismiss" })));
    expect(dismiss).toHaveBeenCalled();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(useStore.getState().state.restored).toBeUndefined();
  });

  it("comes back if the dismiss fails", async () => {
    vi.spyOn(api, "dismissRestored").mockRejectedValue(new ApiError(502, "server went away"));
    render(<RestoreBanner />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Dismiss" })));
    expect(screen.getByRole("status")).toHaveTextContent(restored.message);
    expect(useStore.getState().toasts.at(-1)).toMatchObject({ kind: "error", text: "server went away" });
  });
});
