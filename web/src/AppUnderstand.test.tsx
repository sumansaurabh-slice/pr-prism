// "Understand this PR" inside the whole app: its tab, the shortcut help, and the chat chip back to a step.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./api";
import App from "./App";
import { useStore } from "./store";
import { makeCost, makeState, PATCH, PATH, resetStore } from "./test/fixtures";
import { finding, reviewAnalysis, verdictFor } from "./test/stops";
import { explanation, makeUnderstand, step1 } from "./test/understand";

beforeEach(() => {
  resetStore();
  vi.spyOn(api, "analysis").mockResolvedValue(reviewAnalysis());
  vi.spyOn(api, "state").mockResolvedValue(makeState());
  vi.spyOn(api, "githubThreads").mockResolvedValue({ comments: [] });
  vi.spyOn(api, "getTrust").mockResolvedValue({ author: "a", association: "MEMBER", trusted: true, state: "trusted" });
  vi.spyOn(api, "cost").mockResolvedValue(makeCost());
  vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  vi.spyOn(api, "understand").mockResolvedValue(makeUnderstand());
  vi.spyOn(api, "explainStep").mockImplementation(async (id, level) => explanation({ id, level }));
});
afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

const tabNames = () => screen.getAllByRole("tab").map((t) => t.textContent);

describe("Understand in the app", () => {
  it('puts "Understand · N steps" first until you start reviewing; then Review stays first', async () => {
    window.location.hash = "#/overview";
    render(<App />);
    await waitFor(() => expect(tabNames()[0]).toBe("Understand3 steps"));
    expect(tabNames()[1]).toBe("Review4 left");
    expect(screen.getAllByRole("tab")[0]).toHaveAttribute("href", "#/understand");

    act(() => useStore.setState((s) => ({ state: { ...s.state, stop_verdicts: { [finding.id]: verdictFor(finding, "not") } } })));
    expect(tabNames().slice(0, 3)).toEqual(["Review3 left", "Understand3 steps", "Overview"]);
  });

  it("shows no Understand tab when the server has no steps for it", async () => {
    vi.spyOn(api, "understand").mockRejectedValue(new Error("404 Not Found"));
    window.location.hash = "#/overview";
    render(<App />);
    await waitFor(() => expect(useStore.getState().understandError).toBe("404 Not Found"));
    expect(tabNames()[0]).toBe("Review4 left");
    expect(tabNames().some((t) => t?.startsWith("Understand"))).toBe(false);
  });

  it("the Shortcuts help lists the Understand keys while you are in the view", async () => {
    window.location.hash = `#/understand/${step1.id}`;
    const { unmount } = render(<App />);
    await screen.findByRole("heading", { level: 2, name: PATH });
    expect(document.querySelector(".app")).toHaveClass("review-mode"); // the Review's layout and rails
    fireEvent.click(screen.getByRole("button", { name: "Shortcuts" }));
    const help = screen.getByRole("dialog", { name: "Keyboard shortcuts" });
    expect(within(help).getByText("In Understand")).toBeInTheDocument();
    expect(within(help).getByText("Got it: mark this step understood and go to the next one")).toBeInTheDocument();
    expect(within(help).getByText("Ask about this step in the chat")).toBeInTheDocument();
    unmount();

    useStore.setState({ helpOpen: false });
    window.location.hash = "#/overview";
    render(<App />);
    fireEvent.click(await screen.findByRole("button", { name: "Shortcuts" }));
    expect(within(screen.getByRole("dialog", { name: "Keyboard shortcuts" })).queryByText("In Understand")).not.toBeInTheDocument();
  });

  it("Ask about this puts the step's chip in the chat; the chip goes back to the step", async () => {
    window.location.hash = `#/understand/${step1.id}`;
    render(<App />);
    await screen.findByRole("region", { name: "Before" });
    fireEvent.click(screen.getByRole("button", { name: "Ask about this (c)" }));
    const chip = await screen.findByRole("button", { name: "Step 1 · core.py" });
    expect(chip).toHaveAttribute("title", "Go back to this step in Understand");
    window.location.hash = "#/overview";
    await screen.findByRole("region", { name: "Review plan" });
    fireEvent.click(screen.getByRole("button", { name: "Step 1 · core.py" }));
    await waitFor(() => expect(window.location.hash).toBe(`#/understand/${step1.id}`));
  });
});
