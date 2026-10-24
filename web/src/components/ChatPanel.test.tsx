import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, streamChat } from "../api";
import { useStore } from "../store";
import { answer, ask, makeAnalysis, makeCost, makeState, makeThread, resetStore } from "../test/fixtures";
import type { ChatEvent, ContextChip, ThreadCost } from "../types";
import { ChatPanel } from "./ChatPanel";

vi.mock("../api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../api")>();
  return { ...mod, streamChat: vi.fn() };
});

const STRIP =
  "This thread is long: each answer re-reads it (this one cost $0.80). Start a fresh one? Your current thread stays in the list.";
const chip: ContextChip = { path: "src/core.py", side: "RIGHT", start: 3, end: 5, text: "y = 3", label: "core.py:3-5" };

function result(cost: number | null, thread: Partial<ThreadCost>): ChatEvent {
  return {
    type: "result", session_id: "s1", duration_ms: 5, cost_usd: cost, cost_unknown: cost == null, includes_stopped: false,
    session_total_usd: 3.2, thread: { total_usd: 3.2, turns: 15, unknown: false, long: false, ...thread },
  };
}

/** The next answer streams `events`, then waits until the returned `finish` is called before it ends. */
function nextAnswer(events: ChatEvent[]) {
  let finish!: () => void;
  const ended = new Promise<void>((r) => (finish = r));
  vi.mocked(streamChat).mockImplementationOnce(async (_tid, _text, _ctx, onEvent) => {
    for (const ev of events) onEvent(ev);
    await ended;
    onEvent({ type: "done" });
  });
  return finish;
}

function sendQuestion(text: string) {
  fireEvent.change(screen.getByPlaceholderText(/Ask a question about this PR/), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

describe("chat cost", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    resetStore({ analysis: makeAnalysis(), chatOpen: true, activeThreadId: "t1" });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows each answer's own cost, with the thread so far as its tooltip", () => {
    const thread = makeThread({
      messages: [
        ask("q1"), answer("a1", { cost_usd: 5 }),
        ask("q2"), answer("", { cost_unknown: true, error: "You stopped this answer." }),
        ask("q3"), answer("a3", { cost_usd: 0.42, includes_stopped: true }),
      ],
    });
    useStore.setState({ state: makeState({ threads: [thread] }) });
    render(<ChatPanel />);
    expect(screen.getByText("$5.00")).toHaveAttribute("title", "This answer · thread so far $5.00");
    expect(screen.getByText("cost unknown")).toBeInTheDocument();
    const last = screen.getByText("$0.42");
    expect(last).toHaveAttribute("title", "This answer · thread so far $5.42");
    expect(last).toHaveTextContent("$0.42 (includes the earlier answer's cost)");
  });

  it('marks "thread so far" as a lower bound once an unknown cost can no longer be counted', () => {
    // a2's cost was never reported, and a3 came from a new session, so a2's cost is in no answer.
    const thread = makeThread({
      messages: [
        ask("q1"), answer("a1", { cost_usd: 1 }),
        ask("q2"), answer("a2", { cost_unknown: true }),
        ask("q3"), answer("a3", { cost_usd: 0.5, includes_stopped: false }),
      ],
    });
    useStore.setState({ state: makeState({ threads: [thread] }) });
    render(<ChatPanel />);
    expect(screen.getByText("$1.00")).toHaveAttribute("title", "This answer · thread so far $1.00");
    expect(screen.getByText("cost unknown")).toBeInTheDocument();
    expect(screen.getByText("$0.50")).toHaveAttribute("title", "This answer · thread so far ≥ $1.50");
  });

  it("shows each thread's total in the thread picker", () => {
    useStore.setState({
      state: makeState({ threads: [makeThread(), makeThread({ id: "t2", title: "Fresh" })] }),
      cost: makeCost({
        threads: {
          t1: { total_usd: 5.42, turns: 3, unknown: true, long: false },
          t2: { total_usd: 0, turns: 0, unknown: false, long: false },
        },
      }),
    });
    render(<ChatPanel />);
    const picker = screen.getByRole("combobox");
    expect(within(picker).getByRole("option", { name: "Review chat · ≥ $5.42" })).toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: "Fresh" })).toBeInTheDocument();
  });
});

describe("long-thread strip", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    const earlier = makeThread({ messages: [ask("q1", { context: [chip] }), answer("a1", { cost_usd: 2.4 })] });
    resetStore({ analysis: makeAnalysis(), chatOpen: true, activeThreadId: "t1", state: makeState({ threads: [earlier] }) });
    // After each answer the panel reloads the threads and the state from the server: hand back what it has.
    vi.spyOn(api, "threads").mockImplementation(async () => useStore.getState().state.threads);
    vi.spyOn(api, "state").mockImplementation(async () => useStore.getState().state);
    vi.spyOn(api, "githubThreads").mockResolvedValue({ comments: [] });
    vi.spyOn(api, "cost").mockResolvedValue(makeCost());
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows only after the answer that makes the thread long, never mid-stream", async () => {
    const finish = nextAnswer([{ type: "text", text: "Sure." }, result(0.8, { long: true })]);
    render(<ChatPanel />);
    useStore.setState({ chips: [chip] });
    sendQuestion("one more");
    await screen.findByText("Sure.");
    expect(api.cost).toHaveBeenCalled(); // the header and thread totals refresh on the result
    expect(screen.queryByText(/This thread is long/)).not.toBeInTheDocument();

    await act(async () => finish());
    const strip = await screen.findByRole("status");
    expect(strip).toHaveTextContent(STRIP);
    expect(strip).toHaveClass("flash");
    expect(strip.parentElement).toHaveClass("chat-compose"); // the input area, not the message stream
  });

  it("stays hidden after an answer that isn't long", async () => {
    const finish = nextAnswer([{ type: "text", text: "Sure." }, result(0.1, { long: false, turns: 2 })]);
    render(<ChatPanel />);
    sendQuestion("one more");
    await act(async () => finish());
    await screen.findByText("$0.10");
    expect(screen.queryByText(/This thread is long/)).not.toBeInTheDocument();
  });

  it("New thread starts a fresh thread that carries the context chips", async () => {
    const created = makeThread({ id: "t2", title: "New thread" });
    vi.spyOn(api, "createThread").mockResolvedValue(created);
    const finish = nextAnswer([{ type: "text", text: "Sure." }, result(0.8, { long: true })]);
    render(<ChatPanel />);
    useStore.setState({ chips: [chip] });
    sendQuestion("one more");
    await act(async () => finish());
    expect(useStore.getState().chips).toEqual([]); // sent with the question

    fireEvent.click(await screen.findByRole("button", { name: "New thread" }));
    await screen.findByRole("button", { name: "core.py:3-5" });
    expect(useStore.getState().activeThreadId).toBe("t2");
    expect(useStore.getState().chips).toEqual([chip]);
    expect(useStore.getState().state.threads.map((t) => t.id)).toEqual(["t1", "t2"]); // the old one stays
    expect(screen.queryByText(/This thread is long/)).not.toBeInTheDocument();
  });

  it("Keep going hides the strip for this thread and tells the server", async () => {
    const dismiss = vi.spyOn(api, "dismissLongHint").mockResolvedValue({ ok: true });
    const finish = nextAnswer([{ type: "text", text: "Sure." }, result(0.8, { long: true })]);
    render(<ChatPanel />);
    sendQuestion("one more");
    await act(async () => finish());
    fireEvent.click(await screen.findByRole("button", { name: "Keep going" }));
    expect(dismiss).toHaveBeenCalledWith("t1");
    expect(screen.queryByText(/This thread is long/)).not.toBeInTheDocument();
    expect(useStore.getState().state.threads[0].long_hint_dismissed).toBe(true);

    // The next long answer in this thread doesn't bring it back.
    const again = nextAnswer([{ type: "text", text: "Again." }, result(0.9, { long: true, turns: 17 })]);
    sendQuestion("and another");
    await act(async () => again());
    await screen.findByText("$0.90");
    expect(screen.queryByText(/This thread is long/)).not.toBeInTheDocument();
  });
});
