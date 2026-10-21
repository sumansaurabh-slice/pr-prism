import type {
  Analysis, ChatEvent, ChatThread, ContextChip, Draft, ExistingComment, ReviewEvent, ReviewPreview,
  ReviewState, Side,
} from "./types";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data?.detail) message = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    } catch {
      /* non-JSON error body */
    }
    if (res.status === 401) message = "Your session ended. Open the link that /pr-review printed again.";
    throw new ApiError(res.status, message);
  }
  return (await res.json()) as T;
}

const q = encodeURIComponent;

export interface NewDraft {
  path: string; side: Side; line: number; start_line?: number | null; body: string;
  source: Draft["source"]; suggestion_id?: string | null;
}

export const api = {
  analysis: () => request<Analysis>("GET", "/api/analysis"),
  state: () => request<ReviewState>("GET", "/api/state"),
  diff: (path: string) => request<{ path: string; patch: string }>("GET", `/api/diff?path=${q(path)}`),
  file: (path: string, at: "head" | "base") =>
    request<{ path: string; at: string; content: string }>("GET", `/api/file?path=${q(path)}&at=${at}`),
  setReviewed: (path: string, reviewed: boolean) =>
    request<{ reviewed: Record<string, string> }>("PUT", "/api/reviewed", { path, reviewed }),
  createDraft: (d: NewDraft) => request<Draft>("POST", "/api/drafts", d),
  updateDraft: (id: string, body: string) => request<Draft>("PATCH", `/api/drafts/${q(id)}`, { body }),
  deleteDraft: (id: string) => request<{ ok: boolean }>("DELETE", `/api/drafts/${q(id)}`),
  dismissSuggestion: (id: string) =>
    request<{ dismissed_suggestions: string[] }>("POST", `/api/suggestions/${q(id)}/dismiss`),
  restoreSuggestion: (id: string) =>
    request<{ dismissed_suggestions: string[] }>("POST", `/api/suggestions/${q(id)}/restore`),
  githubThreads: () => request<{ comments: ExistingComment[] }>("GET", "/api/github/threads"),
  previewReview: (body: string, event: ReviewEvent) =>
    request<ReviewPreview>("POST", "/api/review/preview", { body, event }),
  draftSummary: () => request<{ body: string }>("POST", "/api/review/draft-summary"),
  submitReview: (body: string, event: ReviewEvent, commit: "analyzed" | "current") =>
    request<{ ok: boolean; review_id: number; html_url: string }>("POST", "/api/review/submit", { body, event, commit }),
  threads: () => request<ChatThread[]>("GET", "/api/chat/threads"),
  createThread: (title?: string) => request<ChatThread>("POST", "/api/chat/threads", { title }),
  deleteThread: (id: string) => request<{ ok: boolean }>("DELETE", `/api/chat/threads/${q(id)}`),
  stopChat: (id: string) => request<{ ok: boolean }>("POST", `/api/chat/threads/${q(id)}/stop`),
};

/** POST a chat message and stream SSE `data: <json>` frames back to `onEvent`. */
export async function streamChat(
  threadId: string,
  text: string,
  context: ContextChip[],
  onEvent: (e: ChatEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const res = await fetch(`/api/chat/threads/${q(threadId)}/messages`, {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({ text, context }),
    signal,
  });
  if (!res.ok || !res.body) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const data = await res.json();
      if (data?.detail) message = String(data.detail);
    } catch {
      /* ignore */
    }
    if (res.status === 409) message = "Claude is still answering in this thread. Wait or press Stop.";
    onEvent({ type: "error", message });
    onEvent({ type: "done" });
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let sawDone = false;
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let sep: number;
    while ((sep = buffer.search(/\r?\n\r?\n/)) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep).replace(/^\r?\n\r?\n/, "");
      const data = frame
        .split(/\r?\n/)
        .filter((l) => l.startsWith("data:"))
        .map((l) => l.slice(5).replace(/^ /, ""))
        .join("\n");
      if (!data) continue;
      try {
        const ev = JSON.parse(data) as ChatEvent;
        if (ev.type === "done") sawDone = true;
        onEvent(ev);
      } catch {
        /* malformed frame — skip */
      }
    }
  }
  if (!sawDone) onEvent({ type: "done" });
}
