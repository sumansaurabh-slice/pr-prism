import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../api";
import { resetSeenForTests, SEEN_AFTER_MS, SEEN_SEND_MS, useCoverage } from "../coverage";
import { SHORTCUTS, useGlobalKeys } from "../keyboard";
import { useStore } from "../store";
import { makeCoverage, makeStop, place, sid } from "../test/coverageFixtures";
import { makeAnalysis, makeFile, makeState, PATCH, PATH, resetStore } from "../test/fixtures";
import { FileView } from "./FileView";
import { ReviewPath } from "./ReviewPath";

/** A stand-in IntersectionObserver: the test says when an element comes on or goes off screen. */
class FakeObserver {
  static all: FakeObserver[] = [];
  observed = new Set<Element>();
  constructor(private cb: IntersectionObserverCallback) {
    FakeObserver.all.push(this);
  }
  observe(el: Element) {
    this.observed.add(el);
  }
  unobserve(el: Element) {
    this.observed.delete(el);
  }
  disconnect() {
    this.observed.clear();
  }
  show(el: Element, on: boolean) {
    const entry = {
      target: el, isIntersecting: on, intersectionRatio: on ? 1 : 0,
      intersectionRect: { height: on ? 120 : 0 }, rootBounds: { height: 800 },
    } as unknown as IntersectionObserverEntry;
    this.cb([entry], this as unknown as IntersectionObserver);
  }
}

function hunkEl(container: HTMLElement): Element {
  return container.querySelector("tbody.diff-hunk")!;
}
function watcher(el: Element): FakeObserver {
  return FakeObserver.all.find((o) => o.observed.has(el))!;
}

describe("seen hunks", () => {
  beforeEach(() => {
    FakeObserver.all = [];
    vi.stubGlobal("IntersectionObserver", FakeObserver);
    resetSeenForTests();
    window.location.hash = `#/file/${encodeURIComponent(PATH)}`;
    resetStore({ analysis: makeAnalysis({ files: [makeFile({ risk: "high" })] }), state: makeState() });
    vi.spyOn(api, "diff").mockResolvedValue({ path: PATH, patch: PATCH });
    vi.spyOn(api, "coverage").mockResolvedValue(makeCoverage());
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  async function renderDiff() {
    const view = render(<FileView path={PATH} line={null} side="RIGHT" full={false} />);
    await waitFor(() => expect(hunkEl(view.container)).not.toBeNull());
    vi.useFakeTimers();
    return view;
  }

  it("marks a hunk seen after 3 s on screen, with the backend's hunk id", async () => {
    const seen = vi.spyOn(api, "markSeen").mockResolvedValue({ ok: true });
    const { container } = await renderDiff();
    const el = hunkEl(container);
    act(() => watcher(el).show(el, true));
    // 3 s on screen, then sent with whatever else was seen within a second.
    await act(async () => vi.advanceTimersByTime(SEEN_AFTER_MS + SEEN_SEND_MS - 1));
    expect(seen).not.toHaveBeenCalled();
    await act(async () => vi.advanceTimersByTime(1));
    expect(seen).toHaveBeenCalledTimes(1);
    expect(seen).toHaveBeenCalledWith(PATH, ["RIGHT:1-5"]); // @@ -1,4 +1,5 @@
    expect(useStore.getState().state.seen_hunks[PATH]).toEqual(["RIGHT:1-5"]);
    expect(watcher(el)).toBeUndefined(); // seen once is enough: no longer watched
  });

  it("doesn't count a hunk that left the screen before 3 s", async () => {
    const seen = vi.spyOn(api, "markSeen").mockResolvedValue({ ok: true });
    const { container } = await renderDiff();
    const el = hunkEl(container);
    act(() => watcher(el).show(el, true));
    act(() => vi.advanceTimersByTime(SEEN_AFTER_MS - 500));
    act(() => watcher(el).show(el, false));
    await act(async () => vi.advanceTimersByTime(SEEN_AFTER_MS + SEEN_SEND_MS));
    expect(seen).not.toHaveBeenCalled();
  });

  it("doesn't watch a hunk that was already seen", async () => {
    useStore.setState({ state: makeState({ seen_hunks: { [PATH]: ["RIGHT:1-5"] } }) });
    const { container } = await renderDiff();
    expect(watcher(hunkEl(container))).toBeUndefined();
  });
});

describe("risky hunks seen on file rows", () => {
  beforeEach(() => {
    window.location.hash = "#/overview";
    const files = [makeFile({ risk: "high" }), makeFile({ path: "src/quiet.py", risk: "low" })];
    resetStore({ analysis: makeAnalysis({ files }), state: makeState() });
    useCoverage.setState({ coverage: null, error: null });
  });
  afterEach(() => vi.restoreAllMocks());

  it("shows '2 of 5 risky hunks seen' as text with an icon on high-risk files only", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(
      makeCoverage({ high_risk_unseen: { hunks: 3, files: 1, by_file: { [PATH]: { seen: 2, total: 5 } } } }),
    );
    render(<ReviewPath />);
    const seen = await screen.findByText("2 of 5 risky hunks seen");
    expect(seen).toHaveClass("tree-seen");
    expect(seen.querySelector("svg")).not.toBeNull();
    const quiet = screen.getByTitle("src/quiet.py");
    expect(within(quiet).queryByText(/risky hunks seen/)).not.toBeInTheDocument();
  });

  it("marks a fully seen file done", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(
      makeCoverage({ high_risk_unseen: { hunks: 0, files: 0, by_file: { [PATH]: { seen: 1, total: 1 } } } }),
    );
    render(<ReviewPath />);
    expect(await screen.findByText("1 of 1 risky hunk seen")).toHaveClass("tree-seen", "done");
  });
});

describe("the ] key", () => {
  function Keys() {
    useGlobalKeys();
    return null;
  }
  const a = place("a", "src/a.py", 3, "First?");
  const b = place("b", "src/b.py", 9, "Second?");

  beforeEach(() => {
    window.location.hash = "#/overview";
    resetStore({
      analysis: makeAnalysis({
        stops: [makeStop({ id: sid("a"), order: 1 }), makeStop({ id: sid("c"), order: 2 }), makeStop({ id: sid("b"), order: 3 })],
      }),
      state: makeState(),
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("goes to the next unchecked place, and wraps around", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(makeCoverage({ not_checked: [a, b] }));
    render(<Keys />);
    fireEvent.keyDown(window, { key: "]" });
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${sid("a")}`));
    fireEvent.keyDown(window, { key: "]" });
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${sid("b")}`));
    fireEvent.keyDown(window, { key: "]" });
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${sid("a")}`));
  });

  it("from an answered stop, goes to the next unchecked one after it", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(makeCoverage({ not_checked: [a, b] }));
    window.location.hash = `#/review/${sid("c")}`;
    render(<Keys />);
    fireEvent.keyDown(window, { key: "]" });
    await waitFor(() => expect(window.location.hash).toBe(`#/review/${sid("b")}`));
  });

  it("says so when everything is checked", async () => {
    vi.spyOn(api, "coverage").mockResolvedValue(makeCoverage());
    render(<Keys />);
    fireEvent.keyDown(window, { key: "]" });
    await waitFor(() =>
      expect(useStore.getState().toasts.at(-1)?.text).toBe("You looked at every place that needed a human"),
    );
    expect(window.location.hash).toBe("#/overview");
  });

  it("is listed in the shortcuts", () => {
    expect(SHORTCUTS).toContainEqual(["]", "Next place you haven't checked yet"]);
  });
});
