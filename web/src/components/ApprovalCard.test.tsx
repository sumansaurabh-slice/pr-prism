import { act, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, streamChat } from "../api";
import { useGlobalKeys } from "../keyboard";
import { useStore } from "../store";
import { answer, ask, makeAnalysis, makeCost, makeState, makeThread, resetStore } from "../test/fixtures";
import type { ApprovalRequest, ChatEvent } from "../types";
import { ChatPanel } from "./ChatPanel";

vi.mock("../api", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../api")>();
  return { ...mod, streamChat: vi.fn() };
});

const READ = "Read ~/code/billing/config.py";

/** A card as the server sends it: an outside read that may offer "Always allow reads in …". */
function card(over: Partial<ApprovalRequest> = {}): ChatEvent {
  return {
    type: "approval", id: "a1", tool: "Read", title: "Read a file", summary: "~/code/billing/config.py",
    detail: '{\n "file_path": "/Users/me/code/billing/config.py"\n}',
    reason: { kind: "outside_read", text: "Outside this PR's code", detail: "~/code/billing/config.py" },
    causes: [], guard: false, body: null, always: true, always_text: "Always allow reads in ~/code/billing",
    always_title: "Don't ask again in this chat for reads in /Users/me/code/billing", always_label: "/Users/me/code/billing",
    ...over,
  };
}

/** A post in a marked turn: a guard card with the comment as Markdown. */
const postCard = card({
  id: "p1", tool: "mcp__prprism__post_comment", title: "Post a comment on GitHub", summary: "src/a.py:42",
  detail: '{"path": "src/a.py", "line": 42, "body": "**Looks** off"}',
  reason: { kind: "marked_turn", text: "This comment may include content from Jira/Slack/other files read this turn. Post it?" },
  causes: [
    { kind: "lookup", text: "Jira ABC-123" }, { kind: "outside_read", text: "~/other/x.py" },
    { kind: "approved", text: "kubectl get secret db" },
  ],
  guard: true, body: "**Looks** off: `reset()` is never called.", always: false, always_text: "", always_title: "",
  always_label: "",
});

const done = (id: string, decision: "allow" | "always" | "deny", why: "answered" | "covered" | "timeout" | "closed"): ChatEvent => ({
  type: "approval_done", id, decision, why,
});

/** The next answer stays open: `emit` streams events into it, `finish` ends the turn. */
function liveAnswer() {
  let onEvent: ((e: ChatEvent) => void) | null = null;
  let end!: () => void;
  const ended = new Promise<void>((r) => (end = r));
  vi.mocked(streamChat).mockImplementationOnce(async (_tid, _text, _ctx, cb) => {
    onEvent = cb;
    await ended;
    cb({ type: "done" });
  });
  return {
    emit: (...events: ChatEvent[]) => act(() => events.forEach((e) => onEvent!(e))),
    finish: () => act(async () => end()),
  };
}

function sendQuestion(text: string) {
  fireEvent.change(screen.getByRole("textbox"), { target: { value: text } });
  fireEvent.click(screen.getByRole("button", { name: "Send" }));
}

const cardFor = (action: string) => screen.getByRole("group", { name: `Claude asks first: ${action}` });

/** Each element comes after the one before it in the page. */
function expectInOrder(els: HTMLElement[]) {
  for (let i = 1; i < els.length; i++) {
    expect(els[i - 1].compareDocumentPosition(els[i]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  }
}

function WithKeys() {
  useGlobalKeys();
  return <ChatPanel />;
}

beforeEach(() => {
  window.location.hash = "#/overview";
  resetStore({
    analysis: makeAnalysis(), chatOpen: true, activeThreadId: "t1",
    state: makeState({ threads: [makeThread({ messages: [ask("q1"), answer("a1", { cost_usd: 0.1 })] })] }),
  });
  // After each answer the panel reloads the threads and the state from the server: hand back what it has.
  vi.spyOn(api, "threads").mockImplementation(async () => useStore.getState().state.threads);
  vi.spyOn(api, "state").mockImplementation(async () => useStore.getState().state);
  vi.spyOn(api, "githubThreads").mockResolvedValue({ comments: [] });
  vi.spyOn(api, "cost").mockResolvedValue(makeCost());
  vi.spyOn(api, "decideApproval").mockResolvedValue({ ok: true });
});
afterEach(() => vi.restoreAllMocks());

describe("approval card (reason first)", () => {
  it("reads action, then why it asks, then the body, then the buttons", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("check the config");
    turn.emit(card());
    const c = cardFor(READ);
    const action = within(c).getByText(READ);
    const reason = within(c).getByText("Outside this PR's code");
    const body = within(c).getByRole("button", { name: "Show input" });
    const buttons = within(c).getAllByRole("button").slice(1);
    expect(reason).toHaveClass("Label", "Label--attention");
    expect(within(c).getByText("~/code/billing/config.py")).toBeInTheDocument(); // the reason's detail
    expectInOrder([action, reason, body, ...buttons]);
    expect(buttons.map((b) => b.textContent)).toEqual(["Deny", "Always allow reads in ~/code/billing", "Allow"]);

    // Other tools' raw input stays behind "Show input".
    expect(within(c).queryByText(/"file_path"/)).not.toBeInTheDocument();
    fireEvent.click(body);
    expect(within(c).getByText(/"file_path"/)).toBeInTheDocument();
    await turn.finish();
  });

  it("renders a post's comment as Markdown, with the causes, and offers only Deny and Post", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("post it");
    turn.emit(postCard);
    const c = cardFor("Post a comment on src/a.py:42");
    const reason = within(c).getByText(/^This comment may include content/);
    const read = within(c).getByText("Earlier this turn Claude read: Jira ABC-123 · ~/other/x.py");
    const approved = within(c).getByText("Earlier this turn you approved: kubectl get secret db");
    const bold = within(c).getByText("Looks");
    expect(bold.tagName).toBe("STRONG"); // Markdown, as it will be published
    expect(within(c).getByText("reset()").tagName).toBe("CODE");
    const buttons = within(c).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["Deny", "Post"]);
    expect(within(c).queryByRole("button", { name: /Always allow/ })).not.toBeInTheDocument();
    expectInOrder([within(c).getByText("Post a comment on src/a.py:42"), reason, read, approved, bold, buttons[0]]);
    expect(buttons[1]).toHaveAccessibleName("Post: Post a comment on src/a.py:42");
    await turn.finish();
  });

  it("offers no Always button on guard cards, even when the card says it may", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("fetch it");
    turn.emit(card({
      id: "w1", tool: "WebFetch", title: "Open a web page", summary: "https://x.example/a?q=1",
      detail: '{"url": "https://x.example/a?q=1", "prompt": "read it"}',
      reason: { kind: "marked_turn", text: "This could send out content from Jira/Slack/other files read this turn." },
      causes: [{ kind: "lookup", text: "Jira ABC-123" }], guard: true, always: true,
    }));
    const c = cardFor("Open https://x.example/a?q=1");
    expect(within(c).getAllByRole("button").map((b) => b.textContent)).toEqual(["Deny", "Allow once"]);
    expect(within(c).getByText("https://x.example/a?q=1")).toHaveClass("mono"); // the URL in mono
    await turn.finish();
  });

  it("labels Always allow with always_text and its tooltip, and names the action on every button", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("run tests");
    turn.emit(card({
      id: "b1", tool: "Bash", title: "Run a command", summary: "npm test", detail: "npm test",
      reason: { kind: "action", text: "This command can change files or run code" },
      always_text: "Always allow npm test", always_title: "Don't ask again in this chat for commands that use only: npm test",
    }));
    const always = screen.getByRole("button", { name: "Always allow npm test: Run npm test" });
    expect(always).toHaveTextContent("Always allow npm test");
    expect(always).toHaveAttribute("title", "Don't ask again in this chat for commands that use only: npm test");
    expect(screen.getByRole("button", { name: "Deny: Run npm test" })).toHaveTextContent("Deny");
    expect(screen.getByRole("button", { name: "Allow: Run npm test" })).toHaveTextContent("Allow");
    expect(within(cardFor("Run npm test")).getByText("$ npm test")).toBeInTheDocument();
    await turn.finish();
  });
});

describe("approval card accessibility", () => {
  it("tabs Deny → Always allow → Allow", async () => {
    const user = userEvent.setup();
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("check the config");
    turn.emit(card());
    const deny = screen.getByRole("button", { name: `Deny: ${READ}` });
    const always = screen.getByRole("button", { name: `Always allow reads in ~/code/billing: ${READ}` });
    const allow = screen.getByRole("button", { name: `Allow: ${READ}` });
    act(() => deny.focus());
    await user.tab();
    expect(always).toHaveFocus();
    await user.tab();
    expect(allow).toHaveFocus();
    await turn.finish();
  });

  it("announces a new card without taking focus from the message box", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("check the config");
    const box = screen.getByRole("textbox");
    act(() => box.focus());
    const live = screen.getByRole("complementary", { name: "Chat with Claude" }).querySelector('[aria-live="polite"]');
    expect(live).toHaveTextContent(/^$/);
    turn.emit({ type: "text", text: "Let me look." }, card());
    expect(box).toHaveFocus();
    expect(live).toHaveTextContent(`Claude asks first: ${READ}. Outside this PR's code`);
    await turn.finish();
  });

  it("approves nothing from the keyboard except the card's own buttons", async () => {
    window.location.hash = "#/impact";
    const turn = liveAnswer();
    render(<WithKeys />);
    sendQuestion("check the config");
    turn.emit(card());
    for (const key of ["y", "a", "Enter", " ", "Y"]) fireEvent.keyDown(window, { key });
    const allow = screen.getByRole("button", { name: `Allow: ${READ}` });
    fireEvent.keyDown(allow, { key: "o" }); // a global shortcut, pressed on the card: it stays there
    fireEvent.keyDown(allow, { key: "?" });
    expect(window.location.hash).toBe("#/impact");
    expect(useStore.getState().helpOpen).toBe(false);
    expect(api.decideApproval).not.toHaveBeenCalled();
    expect(within(cardFor(READ)).getAllByRole("button")).toHaveLength(4); // still waiting: Show input + 3 choices
    await turn.finish();
  });
});

describe("approval card lifecycle", () => {
  it("stacks cards, settles covered and timed-out ones, then collapses each to one line", async () => {
    const turn = liveAnswer();
    render(<ChatPanel />);
    sendQuestion("check billing");
    const rates = card({ id: "a2", summary: "~/code/billing/rates.py" });
    const tests = card({
      id: "b1", tool: "Bash", title: "Run a command", summary: "npm test", detail: "npm test",
      reason: { kind: "action", text: "This command can change files or run code" }, always_text: "Always allow npm test",
    });
    const write = card({ id: "e1", tool: "Write", title: "Write a file", summary: "notes.md", detail: "{}", always: false });
    turn.emit(card(), rates, tests, write);
    const groups = screen.getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("aria-label"))).toEqual([
      `Claude asks first: ${READ}`, "Claude asks first: Read ~/code/billing/rates.py", "Claude asks first: Run npm test",
      "Claude asks first: Write notes.md",
    ]);

    // "Always allow" on the first card covers the second (same folder): the server settles both.
    fireEvent.click(screen.getByRole("button", { name: `Always allow reads in ~/code/billing: ${READ}` }));
    expect(api.decideApproval).toHaveBeenCalledWith("a1", "always");
    expect(screen.getByRole("textbox")).toHaveFocus(); // the button is gone: focus goes back to the message box
    turn.emit(done("a1", "always", "answered"), done("a2", "allow", "covered"));
    expect(within(cardFor(READ)).getByText("Allowed for this chat")).toBeInTheDocument();
    const covered = cardFor("Read ~/code/billing/rates.py");
    expect(within(covered).getByText("Allowed by your earlier choice")).toBeInTheDocument();
    expect(within(covered).queryByRole("button", { name: /^(Deny|Allow)/ })).not.toBeInTheDocument();

    // Nobody answered the command in time: read-only.
    turn.emit(done("b1", "deny", "timeout"));
    const timedOut = cardFor("Run npm test");
    expect(within(timedOut).getByText("Timed out · Claude was told no")).toBeInTheDocument();
    expect(within(timedOut).queryByRole("button")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Deny: Write notes.md" }));
    expect(within(cardFor("Write notes.md")).getByText("Denied")).toBeInTheDocument();
    turn.emit({ type: "text", text: "Done." }); // a later event keeps the choice
    expect(within(cardFor("Write notes.md")).getByText("Denied")).toBeInTheDocument();
    turn.emit(done("e1", "deny", "answered"));

    await turn.finish();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
    const records = screen.getByRole("list", { name: "What Claude asked first" });
    expect(within(records).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      `Allowed: ${READ}`, "Allowed: Read ~/code/billing/rates.py", "Timed out: Run npm test", "Denied: Write notes.md",
    ]);
    expect(useStore.getState().state.threads[0].messages.at(-1)?.approvals).toHaveLength(4);
  });

  it("shows the saved one-line records of earlier answers", () => {
    useStore.setState({
      state: makeState({
        threads: [makeThread({
          messages: [ask("q1"), answer("Here it is.", {
            approvals: [
              { id: "a1", tool: "Read", title: "Read a file", summary: "~/code/billing/config.py", decision: "allow", why: "answered" },
              { id: "a2", tool: "WebFetch", title: "Open a web page", summary: "https://x.example", decision: "deny", why: "closed" },
            ],
          })],
        })],
      }),
    });
    render(<ChatPanel />);
    const records = screen.getByRole("list", { name: "What Claude asked first" });
    expect(within(records).getAllByRole("listitem").map((li) => li.textContent)).toEqual([
      `Allowed: ${READ}`, "Not answered: Open https://x.example",
    ]);
  });
});
