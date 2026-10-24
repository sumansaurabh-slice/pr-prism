import { useEffect, type CSSProperties, type ReactNode } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { DraftsDrawer } from "./components/Drafts";
import { FileView } from "./components/FileView";
import { FlowView } from "./components/FlowView";
import { Header } from "./components/Header";
import { Impact } from "./components/Impact";
import { Overview } from "./components/Overview";
import { ProgressBanner, RestoreBanner } from "./components/ProgressBanner";
import { ReviewPath } from "./components/ReviewPath";
import { SubmitModal } from "./components/SubmitModal";
import { DiffStat, IconComment, IconFileDiff, IconPulse, IconWorkflow, IconX } from "./icons";
import { SHORTCUTS, useGlobalKeys } from "./keyboard";
import { fileHref, navigate, useRoute, type Route } from "./router";
import { reviewOrder, useStore } from "./store";

const POLL_MS = 3000;

function Tabs({ route }: { route: Route }) {
  const analysis = useStore((s) => s.analysis);
  const reviewedMap = useStore((s) => s.state.reviewed);
  const lastFile = route.view === "file" ? route.path : reviewOrder(analysis)[0];
  const files = analysis?.files ?? [];
  const viewed = files.filter((f) => f.path in reviewedMap).length;
  const tabs: { key: Route["view"]; label: string; href: string; icon: ReactNode; count?: number }[] = [
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

function HelpOverlay() {
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
          <table className="shortcuts">
            <tbody>
              {SHORTCUTS.map(([k, d]) => (
                <tr key={k}>
                  <td>
                    <kbd>{k}</kbd>
                  </td>
                  <td>{d}</td>
                </tr>
              ))}
            </tbody>
          </table>
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
        </div>
      ))}
    </div>
  );
}

export default function App() {
  const analysis = useStore((s) => s.analysis);
  const chatOpen = useStore((s) => s.chatOpen);
  const route = useRoute();
  useGlobalKeys();

  useEffect(() => {
    const { loadAnalysis, loadState, loadGithubComments } = useStore.getState();
    void loadAnalysis();
    void loadState();
    void loadGithubComments();
  }, []);

  // Poll while Claude is still analyzing; deterministic data is already on screen.
  const status = analysis?.status;
  const hasAnalysis = !!analysis;
  useEffect(() => {
    if (hasAnalysis && (status === "complete" || status === "failed")) return;
    const t = window.setInterval(() => void useStore.getState().loadAnalysis(), POLL_MS);
    return () => window.clearInterval(t);
  }, [status, hasAnalysis]);

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
    <div className={`app${chatOpen ? "" : " chat-closed"}`}>
      <Header />
      <Tabs route={route} />
      <ProgressBanner />
      <RestoreBanner />
      <div className="layout">
        <ReviewPath />
        <main className="pane center">
          <div className="center-body">{analysis ? main : <div className="blankslate">Loading the pull request…</div>}</div>
        </main>
        <ChatPanel />
      </div>
      <DraftsDrawer />
      <SubmitModal />
      <HelpOverlay />
      <Toasts />
    </div>
  );
}
