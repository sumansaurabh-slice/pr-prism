// The stop loop (DT10): y / n / ? / c / u / j / k, Undo, Change answer, and the CI and look_for answers.
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from "vitest";
import { api } from "../api";
import { registerFileNav, registerReviewNav, useGlobalKeys } from "../keyboard";
import { useRoute } from "../router";
import { useStore } from "../store";
import { makeDraft, PATCH, PATH, resetStore, makeState } from "../test/fixtures";
import { ci, finding, lookFor, note, reviewAnalysis, verdictFor } from "../test/stops";
import type { Draft, Stop, Verdict, VerdictResult } from "../types";
import { ReviewView } from "./ReviewView";

function Page() {
  useGlobalKeys();
  const route = useRoute();
  return route.view === "review" ? <ReviewView route={route} /> : <p>Somewhere else</p>;
}

const press = (key: string, target: Element | Window = window, extra: object = {}) =>
  act(() => {
    fireEvent.keyDown(target, { key, ...extra });
  });
const hashOf = (s: Stop) => `#/review/${s.id}`;
const heading = () => screen.getByRole("heading", { level: 2 });

function open(stop: Stop) {
  window.location.hash = hashOf(stop);
  return render(<Page />);
}

const madeDraft = makeDraft({ id: "dr", line: 3, origin_id: finding.id, body: "y is never used", source: "suggestion" });

/** POST /api/stops/{id}/verdict as the server answers it. */
function answered(stop: Stop, verdict: Verdict): VerdictResult {
  return {
    verdict: verdictFor(stop, verdict),
    draft: verdict === "real" && stop.kind !== "ci" && stop.kind !== "look_for" ? madeDraft : null,
    dismissed: verdict === "not" ? stop.finding_id : null,
    review_body_draft: verdict === "real" && stop.kind === "ci" ? stop.comment_body : null,
  };
}

let answer: MockInstance<typeof api.answerStop>;
let undo: MockInstance<typeof api.undoAnswer>;

beforeEach(() => {
  resetStore({ analysis: reviewAnalysis(), state: makeState() });
  vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
  answer = vi.spyOn(api, "answerStop").mockImplementation(async (id, verdict) =>
    answered([ci, finding, note, lookFor].find((s) => s.id === id)!, verdict),
  );
  undo = vi.spyOn(api, "undoAnswer").mockResolvedValue({ ok: true, kept_draft: null, restored: null, review_body_draft: null });
});
afterEach(() => {
  vi.restoreAllMocks();
  window.location.hash = "";
});

describe("n and ?", () => {
  it("n moves on at once and toasts Marked not a problem · Undo (u) for 5 seconds", async () => {
    open(finding);
    await press("n");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(note)));
    expect(answer).toHaveBeenCalledWith(finding.id, "not", expect.any(String));
    const t = useStore.getState().toasts.at(-1)!;
    expect(t.text).toBe("Marked not a problem");
    expect(t.actions?.map((a) => a.label)).toEqual(["Undo (u)"]);
    expect(useStore.getState().state.dismissed_suggestions).toEqual(["f1"]);
    expect(useStore.getState().lastAnswer).toMatchObject({ stopId: finding.id, verdict: "not" });
  });

  it("u undoes it: DELETE, the finding comes back, and you are back on the stop", async () => {
    undo.mockResolvedValue({ ok: true, kept_draft: null, restored: "f1", review_body_draft: null });
    open(finding);
    await press("n");
    await waitFor(() => expect(useStore.getState().lastAnswer).not.toBeNull());
    await press("u");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(finding)));
    expect(undo).toHaveBeenCalledWith(finding.id);
    const st = useStore.getState();
    expect(st.state.stop_verdicts[finding.id]).toBeUndefined();
    expect(st.state.dismissed_suggestions).toEqual([]);
    expect(st.toasts.some((t) => t.text === "Marked not a problem")).toBe(false);
    expect(await screen.findByRole("button", { name: "Not a problem (n)" })).toBeInTheDocument();
  });

  it("? marks Not sure, moves on, and offers Ask in chat (c) · Undo (u)", async () => {
    open(finding);
    await press("?");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(note)));
    expect(answer).toHaveBeenCalledWith(finding.id, "unsure", expect.any(String));
    await waitFor(() => expect(useStore.getState().toasts.at(-1)?.text).toBe("Marked not sure"));
    expect(useStore.getState().toasts.at(-1)?.actions?.map((a) => a.label)).toEqual(["Ask in chat (c)", "Undo (u)"]);
    expect(useStore.getState().chips).toEqual([]); // the chat waits for c

    // c (while that toast shows) goes back to the not-sure stop and opens the chat with it attached
    await press("c");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(finding)));
    const st = useStore.getState();
    expect(st.chatOpen).toBe(true);
    expect(st.chips).toEqual([
      expect.objectContaining({ path: PATH, side: "RIGHT", start: 3, end: 3, stop_id: finding.id, label: "Stop 2 · core.py:3" }),
    ]);
    expect(st.chatInput).toContain("Can `y` be left unused?");
  });
});

describe("c, j and k", () => {
  it("c asks in chat about the stop you are on, with its code", async () => {
    const { container } = open(finding);
    await waitFor(() => expect(container.querySelector(".diff")).not.toBeNull());
    await press("c");
    expect(useStore.getState().chips[0]).toMatchObject({ stop_id: finding.id, text: "y = 3" });
    expect(window.location.hash).toBe(hashOf(finding));
  });

  it("the stop's Ask in chat link does the same", () => {
    open(note);
    fireEvent.click(screen.getByRole("button", { name: "Ask in chat (c)" }));
    expect(useStore.getState().chips[0]).toMatchObject({ stop_id: note.id, label: "Stop 3 · core.py:2" });
  });

  it("j and k step through the stops; j after the last one opens the end screen", async () => {
    open(note);
    await press("j");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(lookFor)));
    await press("j");
    await waitFor(() => expect(window.location.hash).toBe("#/review/end"));
    await press("k");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(lookFor)));
    await press("k");
    await waitFor(() => expect(window.location.hash).toBe(hashOf(note)));
  });

  it("outside the Review view, c still means comment and n the next note", async () => {
    const commentOnSelection = vi.fn();
    const nextNote = vi.fn();
    const chat = vi.fn();
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    function Keys() {
      useGlobalKeys();
      return null;
    }
    render(<Keys />);
    registerFileNav({ nextNote, prevNote: vi.fn(), commentOnSelection });
    registerReviewNav({ real: vi.fn(), not: vi.fn(), unsure: vi.fn(), chat, next: vi.fn(), prev: vi.fn(), undo: vi.fn() });
    await press("c");
    await press("n");
    expect(commentOnSelection).toHaveBeenCalledTimes(1);
    expect(nextNote).toHaveBeenCalledTimes(1);
    expect(chat).not.toHaveBeenCalled();

    window.location.hash = hashOf(finding);
    await press("c");
    expect(chat).toHaveBeenCalledTimes(1);
    expect(commentOnSelection).toHaveBeenCalledTimes(1);
    registerFileNav(null);
    registerReviewNav(null);
  });
});

describe("y", () => {
  it("on a finding: makes the comment and opens the editor with the cursor in it; ⌘+Enter saves and moves on", async () => {
    const update = vi.spyOn(api, "updateDraft").mockImplementation(async (id, body) => ({ ...madeDraft, id, body }));
    open(finding);
    await press("y");
    const box = await screen.findByRole("textbox", { name: "Comment" });
    expect(box).toHaveFocus();
    expect(box).toHaveValue("y is never used");
    expect(answer).toHaveBeenCalledWith(finding.id, "real", expect.any(String));
    expect(useStore.getState().state.drafts).toEqual([madeDraft]);

    fireEvent.change(box, { target: { value: "Please remove `y`." } });
    await press("Enter", box, { metaKey: true });
    await waitFor(() => expect(window.location.hash).toBe(hashOf(note)));
    expect(update).toHaveBeenCalledWith("dr", "Please remove `y`.");
  });

  it("Esc keeps the comment, stays, and puts focus back on the question", async () => {
    const del = vi.spyOn(api, "deleteDraft");
    open(finding);
    await press("y");
    const box = await screen.findByRole("textbox", { name: "Comment" });
    await press("Escape", box);
    expect(screen.queryByRole("textbox", { name: "Comment" })).not.toBeInTheDocument();
    expect(heading()).toHaveFocus();
    expect(window.location.hash).toBe(hashOf(finding));
    expect(del).not.toHaveBeenCalled();
    expect(screen.getByText(/comment added/)).toBeInTheDocument();
  });

  it('on a CI stop: "Added to your review summary · Undo" instead of an editor', async () => {
    open(ci);
    await press("y");
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent("Added to your review summary · Undo");
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(useStore.getState().state.review_body_draft).toBe(ci.comment_body);

    undo.mockResolvedValue({ ok: true, kept_draft: null, restored: null, review_body_draft: "" });
    fireEvent.click(within(status).getByRole("button", { name: "Undo" }));
    await waitFor(() => expect(undo).toHaveBeenCalledWith(ci.id));
    await waitFor(() => expect(useStore.getState().state.review_body_draft).toBe(""));
    expect(await screen.findByRole("button", { name: "Real problem (y)" })).toBeInTheDocument();
  });

  it("on a look_for stop: the editor makes the comment on save, with origin_id", async () => {
    const create = vi.spyOn(api, "createDraft").mockImplementation(async (d) => makeDraft({ ...d, id: "new" }) as Draft);
    open(lookFor);
    await press("y");
    expect(answer).not.toHaveBeenCalled(); // nothing is saved until you save
    const box = await screen.findByRole("textbox", { name: "Comment" });
    expect(box).toHaveFocus();
    fireEvent.change(box, { target: { value: "Check the callers." } });
    await press("Enter", box, { ctrlKey: true });
    await waitFor(() => expect(answer).toHaveBeenCalledWith(lookFor.id, "real", expect.any(String)));
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      path: PATH, side: "RIGHT", line: 2, body: "Check the callers.", source: "user", origin_id: lookFor.id,
    }));
    expect(create.mock.invocationCallOrder[0]).toBeLessThan(answer.mock.invocationCallOrder[0]);
  });

  it("on a look_for stop: cancelling the editor records Not sure", async () => {
    const create = vi.spyOn(api, "createDraft");
    open(lookFor);
    await press("y");
    const box = await screen.findByRole("textbox", { name: "Comment" });
    await press("Escape", box);
    await waitFor(() => expect(answer).toHaveBeenCalledWith(lookFor.id, "unsure", expect.any(String)));
    expect(create).not.toHaveBeenCalled();
    expect(useStore.getState().state.stop_verdicts[lookFor.id].verdict).toBe("unsure");
  });
});

describe("Change answer", () => {
  const realState = (body: string) =>
    makeState({
      stop_verdicts: { [finding.id]: verdictFor(finding, "real") },
      drafts: [makeDraft({ id: "dr", line: 3, origin_id: finding.id, body })],
    });

  it("Real → Not deletes an unedited comment (DELETE, then POST)", async () => {
    useStore.setState({ state: realState("y is never used") });
    open(finding);
    fireEvent.click(screen.getByRole("button", { name: "Change answer" }));
    expect(screen.getByRole("button", { name: "Real problem (y)" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Not a problem (n)" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(finding.id, "not", expect.any(String)));
    expect(undo.mock.invocationCallOrder[0]).toBeLessThan(answer.mock.invocationCallOrder[0]);
    expect(screen.queryByText("Keep your comment?")).not.toBeInTheDocument();
    expect(useStore.getState().state.drafts).toEqual([]);
    expect(useStore.getState().state.stop_verdicts[finding.id].verdict).toBe("not");
  });

  it('Real → Not on an edited comment asks "Keep your comment?"; Delete it removes it before the new answer', async () => {
    const edited = makeDraft({ id: "dr", line: 3, origin_id: finding.id, body: "My own words" });
    useStore.setState({ state: realState("My own words") });
    undo.mockResolvedValue({ ok: true, kept_draft: edited, restored: null, review_body_draft: null });
    const del = vi.spyOn(api, "deleteDraft").mockResolvedValue({ ok: true });
    open(finding);
    fireEvent.click(screen.getByRole("button", { name: "Change answer" }));
    fireEvent.click(screen.getByRole("button", { name: "Not a problem (n)" }));
    const ask = await screen.findByRole("group", { name: "Keep your comment?" });
    expect(answer).not.toHaveBeenCalled();
    expect(within(ask).getByRole("button", { name: "Keep it" })).toHaveFocus();
    fireEvent.click(within(ask).getByRole("button", { name: "Delete it" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(finding.id, "not", expect.any(String)));
    expect(del).toHaveBeenCalledWith("dr");
    expect(del.mock.invocationCallOrder[0]).toBeLessThan(answer.mock.invocationCallOrder[0]);
    expect(useStore.getState().state.drafts).toEqual([]);
  });

  it("Keep it keeps the edited comment and saves the new answer", async () => {
    const edited = makeDraft({ id: "dr", line: 3, origin_id: finding.id, body: "My own words" });
    useStore.setState({ state: realState("My own words") });
    undo.mockResolvedValue({ ok: true, kept_draft: edited, restored: null, review_body_draft: null });
    const del = vi.spyOn(api, "deleteDraft");
    open(finding);
    fireEvent.click(screen.getByRole("button", { name: "Change answer" }));
    fireEvent.click(screen.getByRole("button", { name: "Not sure (?)" }));
    fireEvent.click(await screen.findByRole("button", { name: "Keep it" }));
    await waitFor(() => expect(answer).toHaveBeenCalledWith(finding.id, "unsure", expect.any(String)));
    expect(del).not.toHaveBeenCalled();
    expect(useStore.getState().state.drafts).toEqual([edited]);
  });
});
