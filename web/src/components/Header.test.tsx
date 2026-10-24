import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useStore } from "../store";
import { makeAnalysis, makeCost, resetStore } from "../test/fixtures";
import { Header } from "./Header";

describe("header spend", () => {
  beforeEach(() => resetStore({ analysis: makeAnalysis() }));
  afterEach(() => vi.restoreAllMocks());

  it("shows the total right after the review time, with the breakdown as its tooltip", () => {
    useStore.setState({ cost: makeCost() });
    render(<Header />);
    const spent = screen.getByText("$12.40 spent");
    expect(spent).toHaveAttribute("title", "Analysis $10.20 · Chat $2.00 · Other checks $0.20");
    expect(spent.previousElementSibling).toHaveTextContent(/to review$/);
    expect(screen.queryByText(/limit/)).not.toBeInTheDocument();
  });

  it('reads "≥ $12.40" when a cost is unknown', () => {
    useStore.setState({ cost: makeCost({ unknown: true }) });
    render(<Header />);
    expect(screen.getByText("≥ $12.40 spent")).toBeInTheDocument();
  });

  it('adds "so far" while the analysis runs', () => {
    useStore.setState({ cost: makeCost({ running: true, unknown: true }) });
    render(<Header />);
    expect(screen.getByText("≥ $12.40 spent so far")).toBeInTheDocument();
  });

  it("keeps an over-limit label while the spend is over warn_usd", async () => {
    useStore.setState({ cost: makeCost({ warn_usd: 10, over_budget: true }) });
    render(<Header />);
    const label = screen.getByText("over your $10 limit");
    expect(label).toHaveClass("Label", "Label--attention");
    expect(useStore.getState().toasts).toEqual([]); // a lasting label, not a toast

    // It is still there after the next refresh from /api/cost (the analysis poll).
    vi.spyOn(api, "cost").mockResolvedValue(makeCost({ total_usd: 13.1, warn_usd: 10, over_budget: true }));
    await act(() => useStore.getState().loadCost());
    expect(screen.getByText("$13.10 spent")).toBeInTheDocument();
    expect(screen.getByText("over your $10 limit")).toBeInTheDocument();
  });

  it("refreshes the total when the analysis is polled", async () => {
    vi.spyOn(api, "analysis").mockResolvedValue(makeAnalysis());
    const cost = vi.spyOn(api, "cost").mockResolvedValue(makeCost({ running: true }));
    render(<Header />);
    await act(() => useStore.getState().loadAnalysis());
    expect(cost).toHaveBeenCalled();
    expect(await screen.findByText("$12.40 spent so far")).toBeInTheDocument();
  });
});
