import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "../api";
import { useStore } from "../store";
import { makeAnalysis, makeFile, makePreview, makeState, PATH, resetStore } from "../test/fixtures";
import type { EarlierComment, ExistingComment, HeadStatus, SinceReview } from "../types";
import { FileView } from "./FileView";
import { Overview } from "./Overview";
import { NewCommitsBanner, ProgressBanner } from "./ProgressBanner";
import { ReviewPath } from "./ReviewPath";
import { SinceReviewBanner, SinceReviewToggle } from "./SinceReview";
import { SubmitModal } from "./SubmitModal";

const REVIEWED = "abc1234".padEnd(40, "0");
const HEAD = "def5678".padEnd(40, "0");
const OTHER = "src/other.py";

/** The interdiff of PATH: old (reviewed) lines 4-5, new lines 4-6. Change keys: N4, D5, I5, I6. The PR's own diff
 * (makeFile's hunk) covers new lines 1-5, so D5 (a line of the reviewed commit) and I6 can't take a line comment. */
const INTERDIFF = [
  `diff --git a/${PATH} b/${PATH}`,
  "index 2222222..3333333 100644",
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -4,2 +4,3 @@",
  " print(x)",
  "-print(os)",
  "+print(os, 1)",
  "+z = 9",
  "",
].join("\n");

const PR_DIFF = [
  `diff --git a/${PATH} b/${PATH}`,
  "index 1111111..3333333 100644",
  `--- a/${PATH}`,
  `+++ b/${PATH}`,
  "@@ -1,4 +1,5 @@",
  " import os",
  "-x = 1",
  "+x = 2",
  "+y = 3",
  " print(x)",
  " print(os, 1)",
  "",
].join("\n");

function makeSince(over: Partial<SinceReview> = {}): SinceReview {
  return {
    reviewed_head: REVIEWED, head: HEAD,
    files: [{ path: PATH, hunks: [{ old_start: 4, old_lines: 2, new_start: 4, new_lines: 3 }] }],
    patch: { [PATH]: INTERDIFF }, note: null, ok: true, full_pr: false, comments: [], comments_error: null,
    new_stop_ids: [], ...over,
  };
}

function makeHead(over: Partial<HeadStatus> = {}): HeadStatus {
  return {
    analyzed_head: "h", current_head: "e".repeat(40), head_changed: true, running: false,
    estimate: { minutes: 6, usd: 2.1 }, error: null, ...over,
  };
}

function ghComment(over: Partial<ExistingComment>): ExistingComment {
  return {
    id: 1, path: PATH, line: 5, side: "RIGHT", start_line: null, body: "A comment", user: "you",
    created_at: "2026-10-19T15:00:00Z", html_url: "https://github.com/o/r/pull/7#discussion_r1", in_reply_to_id: null,
    review_id: 700, ...over,
  };
}

const analysis = () => makeAnalysis({ files: [makeFile(), makeFile({ path: OTHER })] });

function treeNames(container: HTMLElement): string[] {
  return [...container.querySelectorAll(".tree-name")].map((e) => e.textContent ?? "");
}

beforeEach(() => {
  window.localStorage.clear();
  window.location.hash = "#/overview";
});
afterEach(() => vi.restoreAllMocks());

describe("the Changes since your review toggle", () => {
  it("is hidden with no reviewed head, or when the head hasn't changed", () => {
    resetStore({ analysis: analysis(), sinceReview: makeSince({ reviewed_head: null, files: [], patch: {} }) });
    const { rerender } = render(<SinceReviewToggle />);
    expect(screen.queryByRole("button", { name: /Changes since your review/ })).not.toBeInTheDocument();

    act(() => useStore.setState({ sinceReview: makeSince({ reviewed_head: HEAD, files: [], patch: {} }) }));
    rerender(<SinceReviewToggle />);
    expect(screen.queryByRole("button", { name: /Changes since your review/ })).not.toBeInTheDocument();
  });

  it("defaults to on when you reviewed an older head, with the number of changed files", () => {
    resetStore({ analysis: analysis(), sinceReview: makeSince() });
    render(<SinceReviewToggle />);
    const toggle = screen.getByRole("button", { name: /Changes since your review/ });
    expect(toggle).toHaveClass("btn", "btn-sm");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    expect(within(toggle).getByText("1")).toHaveClass("Counter");
  });

  it("is disabled, with the note as its tooltip, when git failed", () => {
    const note = "Couldn't compute changes since your review: git diff failed";
    resetStore({ analysis: analysis(), sinceReview: makeSince({ ok: false, files: [], patch: {}, note }) });
    render(
      <>
        <SinceReviewToggle />
        <SinceReviewBanner />
      </>,
    );
    const toggle = screen.getByRole("button", { name: /Changes since your review/ });
    expect(toggle).toBeDisabled();
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    expect(toggle).toHaveAttribute("title", note);
    expect(screen.queryByText(/Showing only changes since your review/)).not.toBeInTheDocument();
  });

  it("when on, filters the tree and shows the banner; Show the whole PR turns it off", () => {
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince() });
    const { container } = render(
      <>
        <SinceReviewToggle />
        <SinceReviewBanner />
        <ReviewPath />
      </>,
    );
    expect(treeNames(container)).toEqual(["core.py"]);
    expect(screen.getByText("Showing 1 of 2 files")).toBeInTheDocument();
    const banner = screen.getByRole("status", { name: "Changes since your review" });
    expect(banner).toHaveClass("flash");
    expect(banner).toHaveTextContent("Showing only changes since your review.");

    fireEvent.click(within(banner).getByRole("button", { name: "Show the whole PR" }));
    expect(treeNames(container)).toEqual(["core.py", "other.py"]);
    expect(screen.queryByText(/Showing only changes since your review/)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Changes since your review/ })).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(screen.getByRole("button", { name: /Changes since your review/ }));
    expect(treeNames(container)).toEqual(["core.py"]);
  });

  it("shows the API's note in the banner", () => {
    resetStore({ analysis: analysis(), sinceReview: makeSince({ note: "main moved; some changes may come from main" }) });
    render(<SinceReviewBanner />);
    expect(screen.getByRole("status")).toHaveTextContent(
      "Showing only changes since your review. main moved; some changes may come from main",
    );
  });

  it("keeps your choice per head", async () => {
    const since = vi.spyOn(api, "sinceReview").mockResolvedValue(makeSince());
    resetStore({ analysis: analysis() });
    await act(() => useStore.getState().loadSinceReview());
    const { unmount } = render(<SinceReviewToggle />);
    fireEvent.click(screen.getByRole("button", { name: /Changes since your review/ }));
    expect(screen.getByRole("button", { name: /Changes since your review/ })).toHaveAttribute("aria-pressed", "false");
    unmount();

    // A new page load on the same head: still off.
    resetStore({ analysis: analysis() });
    await act(() => useStore.getState().loadSinceReview());
    const again = render(<SinceReviewToggle />);
    expect(screen.getByRole("button", { name: /Changes since your review/ })).toHaveAttribute("aria-pressed", "false");
    again.unmount();

    // New commits since: on again by default.
    since.mockResolvedValue(makeSince({ head: "f".repeat(40) }));
    resetStore({ analysis: analysis() });
    await act(() => useStore.getState().loadSinceReview());
    render(<SinceReviewToggle />);
    expect(screen.getByRole("button", { name: /Changes since your review/ })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("the file view since your review", () => {
  beforeEach(() => {
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PR_DIFF });
  });

  const gutter = (container: HTMLElement, key: string) =>
    container.querySelectorAll(`td.diff-gutter[data-change-key="${key}"]`)[1] as HTMLElement;
  /** The diff's code lines, once drawn (syntax highlighting splits them into tokens). */
  const code = async (container: HTMLElement, line: string) => {
    const lines = () => [...container.querySelectorAll("td.diff-code")].map((td) => td.textContent);
    await vi.waitFor(() => expect(lines()).toContain(line));
    return lines();
  };

  it("shows the interdiff, and greys the gutter of lines outside the PR's own diff", async () => {
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince() });
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    expect(await code(container, "z = 9")).not.toContain("y = 3"); // the PR diff isn't shown

    for (const key of ["D5", "I6"]) {
      const grey = gutter(container, key).querySelector(".gutter-no-comment");
      expect(grey, key).toHaveAttribute("title", "Comment on this in the full PR view");
    }
    for (const key of ["N4", "I5"]) expect(gutter(container, key).querySelector(".gutter-no-comment"), key).toBeNull();
  });

  it("doesn't let a greyed line take a comment; a line in the PR's diff still can", async () => {
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince() });
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    await code(container, "z = 9");

    fireEvent.mouseDown(gutter(container, "I6"), { button: 0 });
    fireEvent.mouseUp(window);
    expect(useStore.getState().selection).toBeNull();
    expect(useStore.getState().toasts.at(-1)).toMatchObject({ text: "Comment on this in the full PR view." });

    fireEvent.mouseDown(gutter(container, "I5"), { button: 0 });
    fireEvent.mouseUp(window);
    expect(useStore.getState().selection).toMatchObject({ side: "RIGHT", start: 5, end: 5 });
    // Dragging on into a greyed line keeps the pick on the lines that work.
    fireEvent.mouseDown(gutter(container, "I5"), { button: 0 });
    fireEvent.mouseEnter(gutter(container, "I6"));
    fireEvent.mouseUp(window);
    expect(useStore.getState().selection).toMatchObject({ start: 5, end: 5 });
  });

  it("shows the PR diff, ungreyed, with the toggle off", async () => {
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince(), sinceChoice: { [HEAD]: false } });
    const { container } = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    expect(await code(container, "y = 3")).not.toContain("z = 9");
    expect(container.querySelector(".gutter-no-comment")).toBeNull();
  });

  it("shows your earlier comment's status on its GitHub thread", async () => {
    const comments: EarlierComment[] = [{ id: 1, path: PATH, line: 5, status: "fixed", note: "It now passes 1." }];
    resetStore({
      analysis: analysis(), state: makeState(), sinceReview: makeSince({ comments }),
      githubComments: [ghComment({ id: 1, body: "Why print os?" })],
    });
    render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    const thread = (await screen.findByText("Why print os?")).closest(".gh-thread") as HTMLElement;
    const status = within(thread).getByText("Since your review:").parentElement as HTMLElement;
    expect(within(status).getByText("Fixed")).toHaveClass("Label", "Label--success");
    expect(status).toHaveTextContent("It now passes 1.");
  });
});

describe("the Overview since your review", () => {
  const comments: EarlierComment[] = [
    { id: 1, path: PATH, line: 2, status: "fixed", note: "x is checked now." },
    { id: 2, path: PATH, line: 3, status: "fixed", note: null },
    { id: 3, path: OTHER, line: null, status: "still-open", note: "Still drops the error." },
    { id: 4, path: OTHER, line: 9, status: "line-unchanged", note: null },
    { id: 5, path: OTHER, line: 12, status: "line-unchanged", note: null },
  ];

  it("leads with the summary line and lists each earlier comment's status", () => {
    resetStore({
      analysis: analysis(), state: makeState(),
      sinceReview: makeSince({ files: [makeSince().files[0], { path: OTHER, hunks: [] }, { path: "c.py", hunks: [] }], comments }),
      githubComments: [ghComment({ id: 3, body: "Keep the last error\nmore text" })],
    });
    const { container } = render(<Overview />);
    const box = screen.getByRole("region", { name: "Since your review" });
    expect(container.querySelector(".overview-main")?.firstElementChild).toBe(box);
    expect(within(box).getByRole("heading")).toHaveTextContent(
      "Since your review (abc1234 → def5678): 3 files · your comments: 2 fixed, 1 still open, 2 unchanged",
    );
    expect(within(box).getAllByText("Fixed")).toHaveLength(2);
    const open = within(box).getByText("Still open").closest(".Box-row") as HTMLElement;
    expect(open).toHaveTextContent("Keep the last error");
    expect(open).toHaveTextContent(`${OTHER} (outdated)`);
    expect(open).toHaveTextContent("Still drops the error.");
    expect(within(box).getAllByText("Line unchanged")).toHaveLength(2);
  });

  it('shows "Checking…" while the PR is re-read, and why a comment was not checked', () => {
    const later: EarlierComment[] = [
      { id: 1, path: PATH, line: 2, status: "checking", note: null },
      { id: 2, path: PATH, line: 3, status: "not-checked", note: "1 not checked (limit; raise `recheck_max`)" },
    ];
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince({ comments: later }) });
    render(<Overview />);
    const box = screen.getByRole("region", { name: "Since your review" });
    expect(within(box).getByRole("heading")).toHaveTextContent(
      "your comments: 0 fixed, 0 still open, 0 unchanged, 1 checking…, 1 not checked",
    );
    expect(within(box).getByText("Checking…")).toBeInTheDocument();
    const row = within(box).getByText("Not checked").closest(".Box-row") as HTMLElement;
    expect(row).toHaveTextContent("1 not checked (limit; raise recheck_max)");
    expect(within(row).getByText("recheck_max").tagName).toBe("CODE");
  });

  it("is not there with the toggle off", () => {
    resetStore({ analysis: analysis(), state: makeState(), sinceReview: makeSince({ comments }), sinceChoice: { [HEAD]: false } });
    render(<Overview />);
    expect(screen.queryByRole("region", { name: "Since your review" })).not.toBeInTheDocument();
  });
});

describe("the New commits banner", () => {
  it("offers Re-read the PR with the last run's time and cost", () => {
    resetStore({ analysis: analysis(), head: makeHead() });
    render(<NewCommitsBanner />);
    const banner = screen.getByRole("status");
    expect(banner).toHaveClass("flash");
    expect(banner).toHaveTextContent(
      "New commits since you opened this·Re-read the PR(about 6 min and $2, based on the last run; you can keep working)",
    );
  });

  it("leaves out the estimate when no run has finished", () => {
    resetStore({ analysis: analysis(), head: makeHead({ estimate: null }) });
    render(<NewCommitsBanner />);
    expect(screen.getByRole("button", { name: "Re-read the PR" })).toBeInTheDocument();
    expect(screen.queryByText(/based on the last run/)).not.toBeInTheDocument();
  });

  it("is hidden while the head is the analyzed one", () => {
    resetStore({ analysis: analysis(), head: makeHead({ head_changed: false }) });
    render(<NewCommitsBanner />);
    expect(screen.queryByText(/New commits/)).not.toBeInTheDocument();
  });

  it("starts the re-read, hands over to the progress banner and keeps the view", async () => {
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    const reread = vi.spyOn(api, "reanalyze").mockResolvedValue({ started: true, estimate: { minutes: 6, usd: 2.1 } });
    const before = analysis();
    vi.spyOn(api, "cost").mockRejectedValue(new Error("no cost"));
    vi.spyOn(api, "analysis").mockResolvedValue(
      makeAnalysis({ pr: { ...before.pr, head_sha: "e".repeat(40) }, status: "fetched" }),
    );
    resetStore({ analysis: before, head: makeHead() });
    render(
      <>
        <ProgressBanner />
        <NewCommitsBanner />
      </>,
    );
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Re-read the PR" })));
    expect(reread).toHaveBeenCalledTimes(1);
    expect(useStore.getState().head?.running).toBe(true);
    expect(screen.queryByText(/New commits since you opened this/)).not.toBeInTheDocument();
    expect(screen.getByText("Re-reading the PR: getting the new commits…")).toBeInTheDocument();
    expect(window.location.hash).toBe(`#/file/${encodeURIComponent(PATH)}`);
    // The page keeps the analysis you were working with until the run ends.
    expect(useStore.getState().analysis?.pr.head_sha).toBe("h");
  });

  it("shows a 409's detail as a toast", async () => {
    const busy = "The PR is already being read. Wait for it to finish.";
    vi.spyOn(api, "reanalyze").mockRejectedValue(new ApiError(409, busy));
    vi.spyOn(api, "cost").mockRejectedValue(new Error("no cost"));
    vi.spyOn(api, "analysis").mockResolvedValue(analysis());
    resetStore({ analysis: analysis(), head: makeHead() });
    render(<NewCommitsBanner />);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Re-read the PR" })));
    expect(useStore.getState().toasts.at(-1)).toMatchObject({ text: busy });
  });

  it("is what Submit's head-changed warning says too", async () => {
    vi.spyOn(api, "previewReview").mockResolvedValue(
      makePreview({ analyzed_head: "a".repeat(40), current_head: "e".repeat(40), head_changed: true }),
    );
    resetStore({ analysis: analysis(), submitOpen: true, state: makeState() });
    render(<SubmitModal />);
    const warning = (await screen.findByText("New commits since you opened this")).closest(".flash");
    expect(warning).toHaveTextContent("New commits since you opened this (aaaaaaa → eeeeeee).");
  });

  it("shows the new analysis once the run ends", async () => {
    const after = makeAnalysis({ pr: { ...analysis().pr, head_sha: "e".repeat(40) } });
    vi.spyOn(api, "cost").mockRejectedValue(new Error("no cost"));
    vi.spyOn(api, "analysis").mockResolvedValue(after);
    vi.spyOn(api, "state").mockResolvedValue(makeState());
    vi.spyOn(api, "githubThreads").mockResolvedValue({ comments: [] });
    const head = vi.spyOn(api, "head").mockResolvedValue(makeHead({ head_changed: false, running: false }));
    resetStore({ analysis: analysis(), head: makeHead({ running: true }) });
    await act(() => useStore.getState().loadAnalysis());
    expect(useStore.getState().analysis?.pr.head_sha).toBe("h"); // still running: the old view stays
    await act(() => useStore.getState().loadHead());
    expect(head).toHaveBeenCalled();
    await vi.waitFor(() => expect(useStore.getState().analysis?.pr.head_sha).toBe("e".repeat(40)));
  });
});
