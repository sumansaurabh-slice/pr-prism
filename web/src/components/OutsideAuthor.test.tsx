import { render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { useStore } from "../store";
import { makeAnalysis, makeState, makeThread, resetStore } from "../test/fixtures";
import type { Trust } from "../types";
import { ChatPanel } from "./ChatPanel";
import { Header } from "./Header";

const OUTSIDE: Trust = { author: "mallory", association: "CONTRIBUTOR", trusted: false, state: "outside" };
const UNKNOWN: Trust = { author: "mallory", association: null, trusted: false, state: "unknown" };
const MEMBER: Trust = { author: "mallory", association: "MEMBER", trusted: true, state: "trusted" };

describe("outside-author notice", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    const analysis = makeAnalysis();
    resetStore({
      analysis: { ...analysis, pr: { ...analysis.pr, author: "mallory" } }, chatOpen: true, activeThreadId: "t1",
      state: makeState({ threads: [makeThread()] }),
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("warns under the chat header, full width, on an outsider's PR", () => {
    useStore.setState({ trust: OUTSIDE });
    render(<ChatPanel />);
    const strip = screen.getByRole("note");
    expect(strip).toHaveTextContent(
      "Outside author: @mallory isn't a member or collaborator of this repo. Jira, Slack and other-repo lookups ask first.",
    );
    expect(strip).toHaveClass("flash", "flash-warn");
    expect(strip.previousElementSibling).toHaveClass("chat-head");
  });

  it("says so when the author couldn't be checked", () => {
    useStore.setState({ trust: UNKNOWN });
    render(<ChatPanel />);
    expect(screen.getByRole("note")).toHaveTextContent(/^Couldn't check the author; lookups ask first\.$/);
  });

  it("shows nothing for a member or collaborator", () => {
    useStore.setState({ trust: MEMBER });
    render(<ChatPanel />);
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(screen.queryByText(/Outside author|Couldn't check the author/)).not.toBeInTheDocument();
  });

  it('labels the author "outside author" in the PR header', () => {
    useStore.setState({ trust: OUTSIDE });
    render(<Header />);
    const label = screen.getByText("outside author");
    expect(label).toHaveClass("Label");
    expect(label.previousElementSibling).toHaveTextContent("mallory"); // next to the author's name
  });

  it("adds no header label for a member", () => {
    useStore.setState({ trust: MEMBER });
    render(<Header />);
    expect(screen.queryByText("outside author")).not.toBeInTheDocument();
  });

  it("loads the author's standing from /api/trust", async () => {
    vi.spyOn(api, "getTrust").mockResolvedValue(OUTSIDE);
    await useStore.getState().loadTrust();
    expect(useStore.getState().trust).toEqual(OUTSIDE);
  });
});
