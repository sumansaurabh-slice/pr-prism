import { useEffect } from "react";
import { goFile, navigate, parseHash } from "./router";
import { fileByPath, reviewOrder, useStore } from "./store";

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

function isTyping(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  const tag = target.tagName;
  return tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || target.isContentEditable;
}

export function stepFile(delta: 1 | -1) {
  const { analysis } = useStore.getState();
  const order = reviewOrder(analysis);
  if (!order.length) return;
  const route = parseHash(window.location.hash);
  const idx = route.view === "file" ? order.indexOf(route.path) : -1;
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
        else if (st.composer) st.closeComposer();
        else if (st.selection) st.setSelection(null);
        return;
      }
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (isTyping(e.target)) return;
      if (st.submitOpen) return;
      const route = parseHash(window.location.hash);
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
  ["?", "Show or hide this list"],
  ["Esc", "Close a window or clear the picked lines"],
];
