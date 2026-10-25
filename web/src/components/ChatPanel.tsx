import { useEffect, useMemo, useRef, useState } from "react";
import { api, streamChat } from "../api";
import { atLeast, threadSoFar, threadTotalText, usd, type SoFar } from "../cost";
import { Avatar, IconSparkle, IconX } from "../icons";
import { Markdown } from "../markdown";
import { goFile, useRoute } from "../router";
import { chipLabel, fileByPath, splitPath, useStore } from "../store";
import type { ApprovalDecision, ChatEvent, ChatMessage, ChatThread, ContextChip, ToolEvent } from "../types";
import { announceText, ApprovalCard, ApprovalRecords, toRecord, type LiveApproval } from "./ApprovalCard";
import { OutsideAuthorStrip } from "./OutsideAuthor";

type ResultEvent = Extract<ChatEvent, { type: "result" }>;

interface Live {
  threadId: string;
  text: string;
  tools: ToolEvent[];
  approvals: LiveApproval[]; // in arrival order
  thinking: boolean;
  error: string | null;
}

const QUICK_PROMPTS: { label: string; prompt: string }[] = [
  { label: "Explain this", prompt: "Explain what this code does and why it changed in this PR. Use simple words." },
  {
    label: "What could break?",
    prompt:
      "What could break because of this change? Check code outside the PR too, odd inputs, error cases and old users of this code. Point to file:line.",
  },
  {
    label: "Show the data flow",
    prompt: "Show step by step how data moves through this change. Point to `path:line` for each step.",
  },
  {
    label: "Write a review comment",
    prompt: "Write a short, kind review comment for the selected code. Reply with only the comment text.",
  },
];

function now() {
  return new Date().toISOString();
}

function ChipView({ chip, auto, onRemove }: { chip: ContextChip; auto?: boolean; onRemove?: () => void }) {
  return (
    <span className={`chip${auto ? " chip-auto" : ""}`} title={`${chip.path}${chip.start ? `:${chip.start}-${chip.end}` : " (whole file)"}`}>
      <button
        className="chip-label"
        onClick={() => goFile(chip.path, chip.start || null, chip.side)}
        title="Open this code"
      >
        {auto && <span className="chip-auto-tag">file</span>}
        {chip.label || chipLabel(chip)}
      </button>
      {onRemove && (
        <button className="chip-x" onClick={onRemove} aria-label="Remove context">
          ×
        </button>
      )}
    </span>
  );
}

function ToolList({ tools, live }: { tools: ToolEvent[]; live?: boolean }) {
  const [open, setOpen] = useState(false);
  if (!tools.length) return null;
  const shown = live ? tools.slice(-4) : open ? tools : [];
  return (
    <div className="tool-list">
      {!live && (
        <button className="btn-link small" onClick={() => setOpen((v) => !v)}>
          {open ? "▾" : "▸"} Claude looked at the code {tools.length} time{tools.length > 1 ? "s" : ""}
        </button>
      )}
      {shown.map((t, i) => (
        <span key={i} className="tool-chip">
          <span className="tool-name">{t.name}</span> {t.summary}
        </span>
      ))}
    </div>
  );
}

/** "$0.42" (tooltip "This answer · thread so far $5.42"), or "cost unknown". */
function AnswerCost({ m, soFar }: { m: ChatMessage; soFar: SoFar | null }) {
  if (m.cost_usd != null) {
    return (
      <span className="cost" title={`This answer${soFar ? ` · thread so far ${atLeast(soFar.usd, soFar.unknown)}` : ""}`}>
        {usd(m.cost_usd)}
        {m.includes_stopped && <span> (includes the earlier answer's cost)</span>}
      </span>
    );
  }
  if (m.cost_unknown) return <span className="cost">cost unknown</span>;
  return null;
}

function MessageView({
  m, prevUser, soFar, onComment,
}: {
  m: ChatMessage; prevUser: ChatMessage | null; soFar: SoFar | null;
  onComment: (text: string, chips: ContextChip[]) => void;
}) {
  const [copied, setCopied] = useState(false);
  if (m.role === "user") {
    return (
      <div className="msg msg-user">
        {m.context.length > 0 && (
          <div className="msg-chips">
            {m.context.map((c, i) => (
              <ChipView key={i} chip={c} />
            ))}
          </div>
        )}
        <div className="msg-text">{m.text}</div>
      </div>
    );
  }
  return (
    <div className="msg msg-assistant">
      <div className="msg-author">
        <Avatar name="Claude" kind="claude" size={20} />
        <strong>Claude</strong>
      </div>
      <ToolList tools={m.tools} />
      <ApprovalRecords records={m.approvals} />
      {m.text && <Markdown text={m.text} />}
      {m.error && <div className="msg-error">{m.error}</div>}
      <div className="msg-actions">
        {m.text && (
          <>
            <button className="btn-link small" onClick={() => onComment(m.text, prevUser?.context ?? [])}>
              Use as review comment
            </button>
            <button
              className="btn-link small"
              onClick={() => {
                navigator.clipboard?.writeText(m.text).then(
                  () => {
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1500);
                  },
                  () => undefined,
                );
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </>
        )}
        <AnswerCost m={m} soFar={soFar} />
      </div>
    </div>
  );
}

export function ChatPanel() {
  const analysis = useStore((s) => s.analysis);
  const threads = useStore((s) => s.state.threads);
  const chips = useStore((s) => s.chips);
  const dismissedAuto = useStore((s) => s.dismissedAutoChip);
  const chatOpen = useStore((s) => s.chatOpen);
  const focusTick = useStore((s) => s.chatFocusTick);
  const input = useStore((s) => s.chatInput);
  const activeId = useStore((s) => s.activeThreadId);
  const selection = useStore((s) => s.selection);
  const threadCosts = useStore((s) => s.cost?.threads);
  const {
    setChatOpen, setActiveThread, upsertThread, setThreads, addChip, removeChip, clearChips, dismissAutoChip, setChatInput,
    addDraft, toast,
  } = useStore.getState();
  const route = useRoute();
  const [live, setLive] = useState<Live | null>(null);
  const liveRef = useRef<Live | null>(null); // the streaming answer, so a card's decision survives the next event
  // Read out (aria-live="polite") when a card arrives; a card never takes focus from the message box.
  const [announce, setAnnounce] = useState("");
  // The long-thread hint, set after the answer that makes a thread long (15 answers or $3). Never mid-stream.
  const [longHint, setLongHint] = useState<{ threadId: string; cost: number | null } | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const stickToBottom = useRef(true);

  const thread: ChatThread | undefined = threads.find((t) => t.id === activeId) ?? threads[threads.length - 1];

  const routePath = route.view === "file" ? route.path : null;
  const autoChip: ContextChip | null = useMemo(() => {
    if (!routePath || dismissedAuto[routePath]) return null;
    return { path: routePath, side: "RIGHT", start: 0, end: 0, text: "", label: splitPath(routePath).base };
  }, [routePath, dismissedAuto]);

  useEffect(() => {
    if (!focusTick) return;
    const t = window.setTimeout(() => inputRef.current?.focus(), 30);
    return () => window.clearTimeout(t);
  }, [focusTick]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [thread?.messages.length, live?.text, live?.tools.length, live?.approvals.length, thread?.id]);

  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [input]);

  const busy = !!live;

  const send = async (text: string) => {
    const body = text.trim();
    if (!body || busy) return;
    let t = thread;
    if (!t) {
      try {
        t = await api.createThread(body.slice(0, 60));
        upsertThread(t);
        setActiveThread(t.id);
      } catch (e) {
        toast("error", `Could not start the chat: ${(e as Error).message}`);
        return;
      }
    }
    const context = [...(autoChip ? [autoChip] : []), ...chips];
    const userMsg: ChatMessage = { role: "user", text: body, context, tools: [], cost_usd: null, error: null, ts: now() };
    const base = t;
    upsertThread({ ...base, messages: [...base.messages, userMsg] });
    setChatInput("");
    clearChips();
    stickToBottom.current = true;
    const acc: Live = { threadId: base.id, text: "", tools: [], approvals: [], thinking: false, error: null };
    let result = null as ResultEvent | null;
    liveRef.current = acc;
    setLive({ ...acc });
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    try {
      await streamChat(
        base.id,
        body,
        context,
        (ev) => {
          switch (ev.type) {
            case "text":
              acc.text += ev.text;
              acc.thinking = false;
              break;
            case "thinking":
              acc.thinking = true;
              break;
            case "tool":
              acc.tools = [...acc.tools, { name: ev.name, summary: ev.summary }];
              acc.thinking = false;
              break;
            case "result":
              result = ev;
              void useStore.getState().loadCost(); // the header and thread totals now include this answer
              break;
            case "error":
              acc.error = ev.message;
              break;
            case "approval": {
              const { type: _type, ...card } = ev;
              acc.approvals = [...acc.approvals, { ...card, decision: null, why: null }];
              acc.thinking = false;
              setAnnounce(announceText(card));
              break;
            }
            case "approval_done":
              // answered, covered ("Allowed by your earlier choice"), timeout (read-only) or closed
              acc.approvals = acc.approvals.map((a) => (a.id === ev.id ? { ...a, decision: ev.decision, why: ev.why } : a));
              break;
            case "done":
              break;
          }
          setLive({ ...acc });
        },
        ctrl.signal,
      );
    } catch (e) {
      if ((e as Error).name !== "AbortError") acc.error = (e as Error).message;
      else acc.error = acc.error ?? "You stopped this answer.";
    }
    const assistant: ChatMessage = {
      role: "assistant", text: acc.text, context: [], tools: acc.tools, error: acc.error, ts: now(),
      cost_usd: result?.cost_usd ?? null, cost_unknown: result ? result.cost_unknown : true,
      includes_stopped: result?.includes_stopped ?? false, session_total_usd: result?.session_total_usd ?? null,
      ...(acc.approvals.length ? { approvals: acc.approvals.map(toRecord) } : {}), // each card becomes one line
    };
    upsertThread({ ...base, messages: [...base.messages, userMsg, assistant] });
    liveRef.current = null;
    setLive(null);
    setAnnounce("");
    abortRef.current = null;
    if (result?.thread?.long && !base.long_hint_dismissed) setLongHint({ threadId: base.id, cost: result.cost_usd });
    // The server persists the turn — reconcile with its copy. Claude may also have added pending comments or
    // posted to GitHub, so reload those too.
    api.threads().then(
      (ts) => setThreads(ts),
      () => undefined,
    );
    void useStore.getState().loadState();
    void useStore.getState().loadGithubComments();
  };

  // A card's button: show the choice at once, tell the server, and give focus back to the message box (the button
  // goes away). The server's approval_done confirms it, or says it had already timed out.
  const decide = (id: string, decision: ApprovalDecision) => {
    const acc = liveRef.current;
    if (acc) {
      acc.approvals = acc.approvals.map((x) => (x.id === id && !x.decision ? { ...x, decision, why: "answered" } : x));
      setLive({ ...acc });
    }
    inputRef.current?.focus();
    api.decideApproval(id, decision).catch((e) => toast("error", (e as Error).message));
  };

  const stop = () => {
    if (!live) return;
    void api.stopChat(live.threadId).catch(() => undefined);
    abortRef.current?.abort();
  };

  const turnIntoComment = async (text: string, msgChips: ContextChip[]) => {
    const anchor =
      msgChips.find((c) => c.start > 0) ??
      (selection && selection.start > 0
        ? { path: selection.path, side: selection.side, start: selection.start, end: selection.end, startSide: selection.startSide }
        : null);
    if (!anchor) {
      toast("info", "First pick the lines in the diff (click a line number). Then try again.");
      return;
    }
    if (!fileByPath(analysis, anchor.path)) {
      toast("info", "That file is not part of this PR, so you can't comment on it.");
      return;
    }
    const startSide = "startSide" in anchor && anchor.startSide ? anchor.startSide : anchor.side;
    const mixed = startSide !== anchor.side;
    const d = await addDraft({
      path: anchor.path, side: anchor.side, line: anchor.end,
      start_line: mixed || anchor.start < anchor.end ? anchor.start : null,
      start_side: mixed ? startSide : null, body: text, source: "chat",
    });
    if (d) goFile(d.path, d.start_line ?? d.line, d.side);
  };

  // "New thread" on the long-thread hint: a fresh thread that keeps the context chips. Chips still in the box stay
  // (they belong to the box, not the thread), and the code the last question was about comes back as chips.
  const startFresh = async (from: ChatThread) => {
    const lastAsk = [...from.messages].reverse().find((m) => m.role === "user");
    try {
      const t = await api.createThread();
      upsertThread(t);
      setActiveThread(t.id);
    } catch (e) {
      toast("error", `Could not start a new thread: ${(e as Error).message}`);
      return;
    }
    for (const c of lastAsk?.context ?? []) {
      const isAuto = !c.start && autoChip?.path === c.path; // the open file is added anyway
      if (!isAuto) addChip(c);
    }
    setLongHint(null);
    inputRef.current?.focus();
  };

  // "Keep going": hide the hint for this thread, for good (the server keeps the choice).
  const keepGoing = async (from: ChatThread) => {
    setLongHint(null);
    upsertThread({ ...from, long_hint_dismissed: true });
    try {
      await api.dismissLongHint(from.id);
    } catch (e) {
      toast("error", (e as Error).message);
    }
  };

  if (!chatOpen) {
    return (
      <aside className="chat-collapsed">
        <button className="chat-expand" onClick={() => setChatOpen(true)} title="Open the chat (/)">
          <span className="vertical">Ask Claude</span>
        </button>
      </aside>
    );
  }

  const messages = thread?.messages ?? [];
  const soFar = threadSoFar(messages);
  const contextChips = [...(autoChip ? [autoChip] : []), ...chips];
  const hint = !busy && thread && longHint?.threadId === thread.id && !thread.long_hint_dismissed ? longHint : null;

  return (
    <aside className="pane chat-panel" aria-label="Chat with Claude">
      <div className="chat-head">
        <span className="chat-title">
          <IconSparkle /> Ask Claude
        </span>
        <select
          className="input chat-thread-select"
          value={thread?.id ?? ""}
          onChange={(e) => setActiveThread(e.target.value)}
          disabled={!threads.length || busy}
        >
          {!threads.length && <option value="">New chat</option>}
          {threads.map((t) => {
            const total = threadTotalText(threadCosts?.[t.id]);
            return (
              <option key={t.id} value={t.id}>
                {`${t.title || "Untitled"}${total ? ` · ${total}` : ""}`}
              </option>
            );
          })}
        </select>
        <button
          className="btn btn-sm"
          disabled={busy}
          title="Start a new chat"
          onClick={async () => {
            try {
              const t = await api.createThread();
              upsertThread(t);
              setActiveThread(t.id);
            } catch (e) {
              toast("error", (e as Error).message);
            }
          }}
        >
          New
        </button>
        {thread && (
          <button
            className="btn btn-invisible btn-sm"
            disabled={busy}
            title="Delete this chat"
            onClick={async () => {
              try {
                await api.deleteThread(thread.id);
                setThreads(threads.filter((t) => t.id !== thread.id));
                setActiveThread(null);
              } catch (e) {
                toast("error", (e as Error).message);
              }
            }}
          >
            Delete
          </button>
        )}
        <button className="btn-octicon" title="Hide the chat" onClick={() => setChatOpen(false)}>
          <IconX />
        </button>
      </div>
      <OutsideAuthorStrip />
      <div className="sr-only" aria-live="polite">
        {announce}
      </div>

      <div
        className="chat-scroll"
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
        }}
      >
        {!messages.length && !live && (
          <div className="chat-empty">
            <p>
              <strong>Ask Claude anything about this PR.</strong> This is full Claude Code, running in a copy of the
              PR's code. It can read code, run commands like <code>git blame</code>, use your connected tools such as
              Jira and Slack, and add or post review comments. Before it runs a command or changes anything, it asks
              you here first.
            </p>
            <p className="color-muted">
              The file you have open is added to your question for you. To ask about exact lines, click their line
              numbers in the diff and press <em>Ask Claude</em>.
            </p>
          </div>
        )}
        {messages.map((m, i) => (
          <MessageView
            key={`${m.ts}-${i}`}
            m={m}
            prevUser={m.role === "assistant" ? ([...messages.slice(0, i)].reverse().find((x) => x.role === "user") ?? null) : null}
            soFar={soFar[i]}
            onComment={(text, c) => void turnIntoComment(text, c)}
          />
        ))}
        {live && live.threadId === thread?.id && (
          <div className="msg msg-assistant live">
            <div className="msg-author">
              <Avatar name="Claude" kind="claude" size={20} />
              <strong>Claude</strong>
            </div>
            <ToolList tools={live.tools} live />
            {live.approvals.map((a) => (
              <ApprovalCard key={a.id} a={a} onDecide={(decision) => decide(a.id, decision)} />
            ))}
            {live.text ? <Markdown text={live.text} /> : null}
            {(live.thinking || (!live.text && !live.error)) && (
              <div className="thinking">
                <span className="dot" />
                <span className="dot" />
                <span className="dot" />
                <span className="color-muted small">
                  {live.approvals.some((a) => !a.decision)
                    ? "Waiting for you to allow or deny…"
                    : live.tools.length
                      ? "Working…"
                      : "Thinking…"}
                </span>
              </div>
            )}
            {live.error && <div className="msg-error">{live.error}</div>}
          </div>
        )}
      </div>

      <div className="chat-compose">
        {hint && thread && (
          <div className="flash flash-info strip" role="status">
            <span className="strip-text">
              This thread is long: each answer re-reads it{hint.cost != null ? ` (this one cost ${usd(hint.cost)})` : ""}.
              Start a fresh one? Your current thread stays in the list.
            </span>
            <span className="strip-actions">
              <button className="btn btn-sm" onClick={() => void startFresh(thread)}>
                New thread
              </button>
              <button className="btn btn-sm btn-invisible" onClick={() => void keepGoing(thread)}>
                Keep going
              </button>
            </span>
          </div>
        )}
        {contextChips.length > 0 && (
          <div className="chips">
            {autoChip && <ChipView chip={autoChip} auto onRemove={() => dismissAutoChip(autoChip.path)} />}
            {chips.map((c, i) => (
              <ChipView key={i} chip={c} onRemove={() => removeChip(i)} />
            ))}
          </div>
        )}
        <div className="quick-prompts">
          {QUICK_PROMPTS.map((q) => (
            <button key={q.label} className="qp" disabled={busy} onClick={() => void send(q.prompt)}>
              {q.label}
            </button>
          ))}
        </div>
        <div className="chat-input-row">
          <textarea
            ref={inputRef}
            className="input chat-input"
            rows={2}
            placeholder={busy ? "Claude is answering…" : "Ask a question about this PR (Enter to send, Shift+Enter for a new line)"}
            value={input}
            onChange={(e) => setChatInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void send(input);
              }
            }}
          />
          {busy ? (
            <button className="btn btn-danger-outline" onClick={stop}>
              Stop
            </button>
          ) : (
            <button className="btn btn-primary" disabled={!input.trim()} onClick={() => void send(input)}>
              Send
            </button>
          )}
        </div>
      </div>
    </aside>
  );
}
