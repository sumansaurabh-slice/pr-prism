import { useEffect } from "react";
import { ChatPanel } from "./components/ChatPanel";
import { DraftsDrawer } from "./components/Drafts";
import { FileView } from "./components/FileView";
import { FlowView } from "./components/FlowView";
import { Header } from "./components/Header";
import { Impact } from "./components/Impact";
import { Overview } from "./components/Overview";
import { ProgressBanner } from "./components/ProgressBanner";
import { ReviewPath } from "./components/ReviewPath";
import { SubmitModal } from "./components/SubmitModal";
import { SHORTCUTS, useGlobalKeys } from "./keyboard";
import { fileHref, navigate, useRoute, type Route } from "./router";
import { reviewOrder, useStore } from "./store";

const POLL_MS = 3000;

function Tabs({ route }: { route: Route }) {
  const analysis = useStore((s) => s.analysis);
  const lastFile = route.view === "file" ? route.path : reviewOrder(analysis)[0];
  const flowCount = analysis?.flows.length ?? 0;
  const impactCount = analysis?.impact.length ?? 0;
  const tabs: { key: Route["view"]; label: string; href: string; badge?: number }[] = [
    { key: "overview", label: "Overview", href: "#/overview" },
    { key: "file", label: "Walkthrough", href: lastFile ? fileHref(lastFile) : "#/overview" },
    { key: "flow", label: "Data Flow", href: "#/flow", badge: flowCount },
    { key: "impact", label: "Impact", href: "#/impact", badge: impactCount },
  ];
  return (
    <div className="tabs" role="tablist">
      {tabs.map((t) => (
        <a key={t.key} role="tab" href={t.href} className={`tab${route.view === t.key ? " active" : ""}`}>
          {t.label}
          {!!t.badge && <span className="count">{t.badge}</span>}
        </a>
      ))}
    </div>
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
          <button className="btn btn-ghost btn-icon" onClick={() => setOpen(false)} aria-label="Close">
            ×
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
    if (analysis) document.title = `#${analysis.pr.number} ${analysis.pr.title} · PR Review`;
  }, [analysis?.pr.number, analysis?.pr.title]);

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
      <ProgressBanner />
      <div className="layout">
        <ReviewPath />
        <main className="pane center">
          <Tabs route={route} />
          <div className="center-body">{analysis ? main : <div className="empty">Loading the PR…</div>}</div>
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
