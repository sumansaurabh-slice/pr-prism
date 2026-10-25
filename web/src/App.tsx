import { useEffect, useState, type CSSProperties, type ReactNode } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { DraftsDrawer } from "./components/Drafts";
import { FileView } from "./components/FileView";
import { FlowView } from "./components/FlowView";
import { Header } from "./components/Header";
import { Impact } from "./components/Impact";
import { Overview } from "./components/Overview";
import { NewCommitsBanner, ProgressBanner, RestoreBanner } from "./components/ProgressBanner";
import { ReviewPath } from "./components/ReviewPath";
import { ReviewView } from "./components/ReviewView";
import { UnderstandView } from "./components/UnderstandView";
import { SinceReviewBanner, SinceReviewToggle, useSinceReviewSync } from "./components/SinceReview";
import { SubmitModal } from "./components/SubmitModal";
import { DiffStat, IconBook, IconChecklist, IconComment, IconFileDiff, IconPulse, IconWorkflow, IconX } from "./icons";
import { REVIEW_SHORTCUTS, SHORTCUTS, UNDERSTAND_SHORTCUTS, useGlobalKeys } from "./keyboard";
import { useReviewCounts } from "./review";
import { fileHref, navigate, reviewHref, understandHref, useRoute, type Route } from "./router";
import { reviewOrder, useStore } from "./store";

const POLL_MS = 3000;
const HEAD_POLL_MS = 60_000; // GitHub's head is read at most once a minute anyway (GET /api/head)

function Tabs({ route }: { route: Route }) {
  const analysis = useStore((s) => s.analysis);
  const reviewedMap = useStore((s) => s.state.reviewed);
  const lastFile = route.view === "file" ? route.path : reviewOrder(analysis)[0];
  const files = analysis?.files ?? [];
  const viewed = files.filter((f) => f.path in reviewedMap).length;
  const review = useReviewCounts();
  const steps = useStore((s) => s.understand?.steps.length ?? 0);
  type Tab = { key: Route["view"]; label: string; href: string; icon: ReactNode; count?: number | string };
  const reviewTab: Tab = {
    key: "review", label: "Review", href: reviewHref(), icon: <IconChecklist />,
    count: review.total ? (review.left ? `${review.left} left` : "done") : undefined,
  };
  // "Understand · N steps" sits next to Review: first until you start reviewing, then Review stays first.
  const understandTab: Tab = { key: "understand", label: "Understand", href: understandHref(), icon: <IconBook />, count: `${steps} step${steps === 1 ? "" : "s"}` };
  const guided = !steps ? [reviewTab] : review.started ? [reviewTab, understandTab] : [understandTab, reviewTab];
  const tabs: Tab[] = [
    ...guided,
    { key: "overview", label: "Overview", href: "#/overview", icon: <IconComment /> },
    {
      key: "file", label: "Files changed", href: lastFile ? fileHref(lastFile) : "#/overview", icon: <IconFileDiff />,
      count: files.length,
    },
    { key: "flow", label: "Data flow", href: "#/flow", icon: <IconWorkflow />, count: analysis?.flows.length ?? 0 },
    { key: "impact", label: "Impact", href: "#/impact", icon: <IconPulse />, count: analysis?.impact.length ?? 0 },
  ];
  return (
    <nav className="tabs" aria-label="Pull request views">
      <div className="tabs-list" role="tablist">
        {tabs.map((t) => (
          <a
            key={t.key}
            role="tab"
            aria-selected={route.view === t.key}
            href={t.href}
            className={`tab${route.view === t.key ? " active" : ""}`}
          >
            {t.icon}
            {t.label}
            {t.count !== undefined && <span className="Counter">{t.count}</span>}
          </a>
        ))}
      </div>
      {analysis && (
        <div className="tabs-right">
          <SinceReviewToggle />
          <span className="viewed-count" title="Files you have marked as viewed">
            <span className="viewed-ring" style={{ "--pct": `${files.length ? (viewed / files.length) * 100 : 0}%` } as CSSProperties} />
            {viewed} / {files.length} files viewed
          </span>
          <DiffStat additions={analysis.pr.additions} deletions={analysis.pr.deletions} />
        </div>
      )}
    </nav>
  );
}

function ShortcutRows({ rows }: { rows: [string, string][] }) {
  return (
    <table className="shortcuts">
      <tbody>
        {rows.map(([k, d]) => (
          <tr key={k}>
            <td>
              <kbd>{k}</kbd>
            </td>
            <td>{d}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function HelpOverlay({ mode }: { mode: "review" | "understand" | null }) {
  const open = useStore((s) => s.helpOpen);
  const setOpen = useStore((s) => s.setHelpOpen);
  if (!open) return null;
  return (
    <div className="overlay overlay-center" onClick={() => setOpen(false)}>
      <div className="modal modal-sm" role="dialog" aria-label="Keyboard shortcuts" onClick={(e) => e.stopPropagation()}>
        <header className="modal-head">
          <h2>Keyboard shortcuts</h2>
          <button className="btn-octicon" onClick={() => setOpen(false)} aria-label="Close">
            <IconX />
          </button>
        </header>
        <div className="modal-body">
          {mode ? (
            <>
              <h3 className="Box-title">{mode === "review" ? "In the Review" : "In Understand"}</h3>
              <ShortcutRows rows={mode === "review" ? REVIEW_SHORTCUTS : UNDERSTAND_SHORTCUTS} />
              <h3 className="Box-title">Everywhere else</h3>
              <ShortcutRows rows={SHORTCUTS} />
            </>
          ) : (
            <ShortcutRows rows={SHORTCUTS} />
          )}
        </div>
      </div>
    </div>
  );
}

function Toasts() {
  const toasts = useStore((s) => s.toasts);
  const drop = useStore((s) => s.dropToast);
  return (
    <div className="toasts" aria-live="polite">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.kind}`} onClick={() => drop(t.id)}>
          {t.text}
          {t.actions?.map((a) => (
            <span key={a.label}>
              {" · "}
              <button
                className="btn-link"
                onClick={(e) => {
                  e.stopPropagation();
                  drop(t.id);
                  a.run();
                }}
              >
                {a.label}
              </button>
            </span>
          ))}
        </div>
      ))}
    </div>
  );
}

/** True while the media query matches (false where matchMedia is missing, as in tests). */
function useMedia(query: string): boolean {
  const get = () => typeof window.matchMedia === "function" && window.matchMedia(query).matches;
  const [on, setOn] = useState(get);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(query);
    const change = () => setOn(mq.matches);
    change();
    mq.addEventListener("change", change);
    return () => mq.removeEventListener("change", change);
  }, [query]);
  return on;
}

export default function App() {
  const analysis = useStore((s) => s.analysis);
  const chatOpen = useStore((s) => s.chatOpen);
  const route = useRoute();
  useGlobalKeys();
  // The Review and Understand views share the guided layout: the list on the left, one item in the middle, a
  // sticky action bar, and the rails below.
  const guided = route.view === "review" || route.view === "understand" ? route.view : null;

  // The Review view below 1280 px: the chat is a 40 px rail that opens as an overlay (c, or a click).
  const narrow = useMedia("(max-width: 1279.98px)");
  const chatRail = !!guided && narrow;
  useEffect(() => {
    if (useStore.getState().chatRail !== chatRail) useStore.getState().setChatRail(chatRail);
  }, [chatRail]);

  useSinceReviewSync();

  useEffect(() => {
    const { loadAnalysis, loadState, loadGithubComments, loadTrust, loadHead } = useStore.getState();
    void loadAnalysis();
    void loadState();
    void loadGithubComments();
    void loadTrust();
    void loadHead();
  }, []);

  // Poll while Claude is still analyzing (or re-reading the PR); deterministic data is already on screen. The PR's
  // head rides along ("New commits since you opened this"), and is still checked once a minute after that.
  const status = analysis?.status;
  const hasAnalysis = !!analysis;
  const rereading = useStore((s) => s.head?.running ?? false);
  const analyzing = !hasAnalysis || (status !== "complete" && status !== "failed") || rereading;
  useEffect(() => {
    const poll = () => {
      const st = useStore.getState();
      if (analyzing) void st.loadAnalysis();
      void st.loadHead();
    };
    const t = window.setInterval(poll, analyzing ? POLL_MS : HEAD_POLL_MS);
    return () => window.clearInterval(t);
  }, [analyzing]);

  // Understand's steps follow the analysis (the reading order and the file summaries): read them again when it
  // moves on or a re-read brings new commits.
  const head = analysis?.pr.head_sha;
  useEffect(() => {
    if (hasAnalysis) void useStore.getState().loadUnderstand();
  }, [hasAnalysis, status, head]);

  useEffect(() => {
    if (analysis) document.title = `${analysis.pr.title} · #${analysis.pr.number} · PR Prism`;
  }, [analysis?.pr.number, analysis?.pr.title]);

  // Leaving the file of a comment being re-placed ends the pick (it only works in that file's diff).
  const routeFile = route.view === "file" ? route.path : null;
  useEffect(() => {
    const { replacing, cancelReplace } = useStore.getState();
    if (replacing && replacing.path !== routeFile) cancelReplace();
  }, [routeFile]);

  // Default landing: Overview.
  useEffect(() => {
    if (!window.location.hash) navigate("#/overview");
  }, []);

  let main;
  switch (route.view) {
    case "file":
      main = <FileView key={route.path} path={route.path} line={route.line} side={route.side} full={route.full} />;
      break;
    case "flow":
      main = <FlowView id={route.id} />;
      break;
    case "impact":
      main = <Impact />;
      break;
    default:
      main = <Overview />;
  }

  return (
    <div className={`app${chatOpen ? "" : " chat-closed"}${guided ? " review-mode" : ""}`}>
      <Header />
      <Tabs route={route} />
      <ProgressBanner />
      <NewCommitsBanner />
      <SinceReviewBanner />
      <RestoreBanner />
      <div className="layout">
        {route.view === "review" ? (
          // the stop list (left pane) and the stop or end screen (main column)
          <ReviewView route={route} />
        ) : route.view === "understand" ? (
          // the steps by chapter (left pane) and the step or end screen (main column)
          <UnderstandView route={route} />
        ) : (
          <>
            <ReviewPath />
            <main className="pane center">
              <div className="center-body">{analysis ? main : <div className="blankslate">Loading the pull request…</div>}</div>
            </main>
          </>
        )}
        <ChatPanel />
      </div>
      <DraftsDrawer />
      <SubmitModal />
      <HelpOverlay mode={guided} />
      <Toasts />
    </div>
  );
}
