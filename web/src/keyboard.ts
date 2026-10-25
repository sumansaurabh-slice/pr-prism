import { useEffect } from "react";
import { goNextUnchecked } from "./coverage";
import { goFile, navigate, parseHash } from "./router";
import { fileByPath, fileMap, hiddenByPrefs, reviewOrder, sinceView, useStore } from "./store";

/** The Walkthrough registers these while mounted so global keys can reach it. */
export interface FileNav {
  nextNote: () => void;
  prevNote: () => void;
  commentOnSelection: () => void;
}
let fileNav: FileNav | null = null;
export function registerFileNav(nav: FileNav | null) {
  fileNav = nav;
}

/** The Review view registers these while mounted. Its keys apply there only; elsewhere n, ? and c keep their
 * meanings (next note, this list, comment). */
export interface ReviewNav {
  real: () => void;    // y
  not: () => void;     // n
  unsure: () => void;  // ?
  chat: () => void;    // c
  next: () => void;    // j
  prev: () => void;    // k
  undo: () => void;    // u
}
let reviewNav: ReviewNav | null = null;
export function registerReviewNav(nav: ReviewNav | null) {
  reviewNav = nav;
}
const REVIEW_KEYS: Record<string, keyof ReviewNav> = {
  y: "real", n: "not", "?": "unsure", c: "chat", j: "next", k: "prev", u: "undo",
};

/** The Understand view registers these while mounted. Its keys apply there only; elsewhere y, c, j, k and → keep
 * their meanings (c comments, j / k move between files, y and → do nothing). */
export interface UnderstandNav {
  gotIt: () => void;  // y or →: mark the step understood and go on
  chat: () => void;   // c: ask about this step
  next: () => void;   // j
  prev: () => void;   // k
}
let understandNav: UnderstandNav | null = null;
export function registerUnderstandNav(nav: UnderstandNav | null) {
  understandNav = nav;
}
const UNDERSTAND_KEYS: Record<string, keyof UnderstandNav> = {
  y: "gotIt", ArrowRight: "gotIt", c: "chat", j: "next", k: "prev",
};

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

/** Keys pressed on a chat approval card stay there: no shortcut acts from it, and no key approves anything. */
function onApprovalCard(target: EventTarget | null): boolean {
  return target instanceof Element && target.closest(".approval") !== null;
}

/** Next/previous file in the reading order, skipping files the "Hide …" checkboxes hide (and, with "Changes since
 * your review" on, files that didn't change since then). */
export function stepFile(delta: 1 | -1) {
  const { analysis, prefs, state, sinceReview, sinceChoice } = useStore.getState();
  const files = fileMap(analysis);
  const since = sinceView(sinceReview, sinceChoice);
  const route = parseHash(window.location.hash);
  const current = route.view === "file" ? route.path : null;
  const order = reviewOrder(analysis).filter(
    (p) =>
      p === current ||
      ((!files.has(p) || !hiddenByPrefs(files.get(p)!, prefs, state.reviewed)) && (!since.on || since.files.has(p))),
  );
  if (!order.length) return;
  const idx = current ? order.indexOf(current) : -1;
  const next = idx === -1 ? (delta === 1 ? 0 : order.length - 1) : idx + delta;
  if (next >= 0 && next < order.length) goFile(order[next]);
}

export function useGlobalKeys() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const st = useStore.getState();
      if (e.key === "Escape") {
        if (st.helpOpen) st.setHelpOpen(false);
        else if (st.submitOpen) st.setSubmitOpen(false);
        else if (st.draftsOpen) st.setDraftsOpen(false);
        else if (st.replacing) st.cancelReplace();
        else if (st.composer) st.closeComposer();
        else if (st.selection) st.setSelection(null);
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e.target) || onApprovalCard(e.target)) return;
      if (st.submitOpen) return;
      const route = parseHash(window.location.hash);
      if (route.view === "review" && reviewNav && REVIEW_KEYS[e.key]) {
        if (st.helpOpen || st.draftsOpen) return; // a window is open over the stop: no answer from behind it
        reviewNav[REVIEW_KEYS[e.key]]();
        e.preventDefault();
        return;
      }
      const understandKey = e.key === "ArrowRight" && e.shiftKey ? undefined : UNDERSTAND_KEYS[e.key];
      if (route.view === "understand" && understandNav && understandKey) {
        if (st.helpOpen || st.draftsOpen) return; // a window is open over the step
        understandNav[understandKey]();
        e.preventDefault();
        return;
      }
      switch (e.key) {
        case "j":
          stepFile(1);
          break;
        case "k":
          stepFile(-1);
          break;
        case "n":
          fileNav?.nextNote();
          break;
        case "p":
          fileNav?.prevNote();
          break;
        case "c":
          fileNav?.commentOnSelection();
          break;
        case "/":
          st.askClaude();
          break;
        case "r":
          if (route.view === "file" && fileByPath(st.analysis, route.path)) {
            void st.setReviewed(route.path, !(route.path in st.state.reviewed));
          }
          break;
        case "o":
          navigate("#/overview");
          break;
        case "]":
          void goNextUnchecked();
          break;
        case "?":
          st.setHelpOpen(!st.helpOpen);
          break;
        default:
          return;
      }
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}

export const SHORTCUTS: [string, string][] = [
  ["j / k", "Next / previous file in the reading order"],
  ["n / p", "Next / previous Claude note in this file"],
  ["click a line number", "Pick a line"],
  ["drag, or shift-click", "Pick more lines (also from a red line to a green one)"],
  ["c", "Comment on the picked lines"],
  ["/", "Go to the chat box"],
  ["r", "Mark this file as viewed, or not viewed"],
  ["o", "Go to the overview"],
  ["]", "Next place you haven't checked yet"],
  ["?", "Show or hide this list"],
  ["Esc", "Close a window, stop moving a comment, or clear the picked lines"],
];

/** The Review view's keys; the help lists them first while you are in the view. */
export const REVIEW_SHORTCUTS: [string, string][] = [
  ["y", "Real problem: opens the comment (a CI failure goes in your review summary)"],
  ["n", "Not a problem, and go to the next stop"],
  ["?", "Not sure, and go to the next stop"],
  ["c", "Ask in chat about this stop"],
  ["u", "Undo your last answer while its message shows"],
  ["j / k", "Next / previous stop"],
  ["⌘/Ctrl + Enter", "Save the comment and go to the next stop"],
  ["Esc", "Keep the comment and stay on this stop"],
];

/** The Understand view's keys; the help lists them first while you are in the view. */
export const UNDERSTAND_SHORTCUTS: [string, string][] = [
  ["y or →", "Got it: mark this step understood and go to the next one"],
  ["c", "Ask about this step in the chat"],
  ["j / k", "Next / previous step"],
];
