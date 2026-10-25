import { useSyncExternalStore } from "react";
import type { Side } from "./types";

export type Route =
  | { view: "overview" }
  | { view: "file"; path: string; line: number | null; side: Side; full: boolean }
  | { view: "flow"; id: string | null }
  | { view: "impact" }
  // Guided review: #/review (resume: the first open stop, or the end screen), #/review/<stopId>, #/review/end
  | { view: "review"; stopId: string | null; end: boolean };

export function parseHash(hash: string): Route {
  const raw = hash.replace(/^#\/?/, "");
  const [pathPart, query = ""] = raw.split("?");
  const params = new URLSearchParams(query);
  const [head, ...rest] = pathPart.split("/");
  switch (head) {
    case "file": {
      const path = decodeURIComponent(rest.join("/"));
      if (!path) return { view: "overview" };
      const line = Number(params.get("line"));
      return {
        view: "file",
        path,
        line: Number.isFinite(line) && line > 0 ? line : null,
        side: params.get("side") === "LEFT" ? "LEFT" : "RIGHT",
        full: params.get("full") === "1",
      };
    }
    case "flow":
      return { view: "flow", id: rest.length ? decodeURIComponent(rest.join("/")) : null };
    case "impact":
      return { view: "impact" };
    case "review": {
      const id = rest.length ? decodeURIComponent(rest.join("/")) : "";
      if (id === "end") return { view: "review", stopId: null, end: true };
      return { view: "review", stopId: id || null, end: false };
    }
    default:
      return { view: "overview" };
  }
}

export function fileHref(path: string, line?: number | null, side?: Side, full?: boolean): string {
  const params = new URLSearchParams();
  if (line) params.set("line", String(line));
  if (line && side === "LEFT") params.set("side", "LEFT");
  if (full) params.set("full", "1");
  const qs = params.toString();
  return `#/file/${encodeURIComponent(path)}${qs ? `?${qs}` : ""}`;
}

/** `#/review/<stopId>`; `#/review` resumes at the first open stop; `#/review/end` is the end screen. */
export function reviewHref(stopId?: string | null): string {
  return stopId ? `#/review/${encodeURIComponent(stopId)}` : "#/review";
}
export const REVIEW_END = "#/review/end";

export function navigate(href: string) {
  if (window.location.hash === href) {
    // Same target: re-fire so views can re-scroll to the line.
    window.dispatchEvent(new HashChangeEvent("hashchange"));
  } else {
    window.location.hash = href;
  }
}

export const goFile = (path: string, line?: number | null, side?: Side, full?: boolean) =>
  navigate(fileHref(path, line, side, full));

/** Swap the address without a new history entry (#/review → #/review/<first open stop>), then tell the views. */
export function replaceRoute(href: string) {
  window.history.replaceState(window.history.state, "", href);
  window.dispatchEvent(new HashChangeEvent("hashchange"));
}

function subscribe(cb: () => void) {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

let navTick = 0;
window.addEventListener("hashchange", () => {
  navTick += 1;
});

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parseHash(hash);
}

/** Increments on every hashchange (including same-hash re-navigation). */
export function useNavTick(): number {
  return useSyncExternalStore(subscribe, () => navTick);
}
