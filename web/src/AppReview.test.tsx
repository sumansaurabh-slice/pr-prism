// The Review view inside the whole app: its tab, the header, the shortcut help and the Undo toast.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import App from "./App";
import { useStore } from "./store";
import { makeCost, makeState, PATCH, PATH, resetStore } from "./test/fixtures";
import { finding, note, reviewAnalysis, verdictFor } from "./test/stops";

beforeEach(() => {
  resetStore();
  vi.spyOn(api, "analysis").mockResolvedValue(reviewAnalysis());
  vi.spyOn(api, "state").mockResolvedValue(makeState());
  vi.spyOn(api, "githubThreads").mockResolvedValue({ comments: [] });
  vi.spyOn(api, "getTrust").mockResolvedValue({ author: "a", association: "MEMBER", trusted: true, state: "trusted" });
  vi.spyOn(api, "cost").mockResolvedValue(makeCost());
  vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
});
afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("the Review view in the app", () => {
  it('puts "Review · N left" first in the tab row, and Submit stays the header\'s only primary button', async () => {
    window.location.hash = `#/review/${finding.id}`;
    render(<App />);
    await screen.findByRole("heading", { level: 2, name: "Can y be left unused?" });
    const tabs = screen.getAllByRole("tab");
    expect(tabs[0]).toHaveTextContent("Review4 left");
    expect(tabs[0]).toHaveAttribute("href", "#/review");
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs[1]).toHaveTextContent("Overview");
    const header = document.querySelector(".gh-header") as HTMLElement;
    expect([...header.querySelectorAll(".btn-primary")].map((b) => b.textContent)).toEqual(["Submit review"]);

    act(() => useStore.setState((s) => ({ state: { ...s.state, stop_verdicts: { [finding.id]: verdictFor(finding, "not") } } })));
    expect(tabs[0]).toHaveTextContent("Review3 left");
  });

  it("the Shortcuts help lists the Review keys while you are in the view", async () => {
    window.location.hash = `#/review/${finding.id}`;
    const { unmount } = render(<App />);
    await screen.findByRole("heading", { level: 2, name: "Can y be left unused?" });
    fireEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
    const help = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(help).getByText("In the Review")).toBeInTheDocument();
    expect(within(help).getByText(/^Real problem: opens the comment/)).toBeInTheDocument();
    expect(within(help).getByText("Not sure, and go to the next stop")).toBeInTheDocument();
    unmount();

    useStore.setState({ helpOpen: false });
    window.location.hash = "#/overview";
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Shortcuts" }));
    expect(within(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).queryByText("In the Review")).not.toBeInTheDocument();
  });

  it('shows "Marked not a problem · Undo (u)"; its Undo takes the answer back', async () => {
    vi.spyOn(api, "answerStop").mockResolvedValue({
      verdict: verdictFor(finding, "not"), draft: null, dismissed: "f1", review_body_draft: null,
    });
    const undo = vi.spyOn(api, "undoAnswer").mockResolvedValue({ ok: true, kept_draft: null, restored: "f1", review_body_draft: null });
    window.location.hash = `#/review/${finding.id}`;
    render(<App />);
    await screen.findByRole("heading", { level: 2, name: "Can y be left unused?" });
    fireEvent.keyDown(window, { key: "n" });
    await screen.findByRole("heading", { level: 2, name: /Is this safe\?/ });
    expect(window.location.hash).toBe(`#/review/${note.id}`);
    const toast = (await screen.findByText("Marked not a problem")).closest(".toast") as HTMLElement;
    expect(toast).toHaveTextContent("Marked not a problem · Undo (u)");
    fireEvent.click(within(toast).getByRole("button", { name: "Undo (u)" }));
    await waitFor(() => expect(undo).toHaveBeenCalledWith(finding.id));
    expect(await screen.findByRole("heading", { level: 2, name: "Can y be left unused?" })).toBeInTheDocument();
    expect(screen.queryByText("Marked not a problem")).not.toBeInTheDocument();
  });
});
