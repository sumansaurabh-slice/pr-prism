import dagre from "@dagrejs/dagre";
import {
  Background, Controls, MarkerType, MiniMap, Position, ReactFlow, ReactFlowProvider, useReactFlow,
  type Edge, type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useRef, useState } from "react";
import { Markdown } from "../markdown";
import { goFile, navigate } from "../router";
import { fileMap, splitPath, useStore } from "../store";
import type { Flow, FlowNodeKind } from "../types";

const NODE_W = 210;
const NODE_H = 64;

const KIND_LABEL: Record<FlowNodeKind, string> = {
  entry: "entry point", logic: "logic", store: "data store", external: "external", test: "test",
};

type Dir = "LR" | "TB";
interface Layout { nodes: Node[]; edges: Edge[]; width: number; height: number }

function layout(flow: Flow, changed: Set<string>, dir: Dir): Layout {
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: dir, nodesep: dir === "LR" ? 28 : 36, ranksep: dir === "LR" ? 80 : 60, marginx: 16, marginy: 16 });
  g.setDefaultEdgeLabel(() => ({}));
  const ids = new Set(flow.nodes.map((n) => n.id));
  for (const n of flow.nodes) g.setNode(n.id, { width: NODE_W, height: NODE_H });
  const edgesIn = flow.edges.filter((e) => ids.has(e.source) && ids.has(e.target));
  for (const e of edgesIn) g.setEdge(e.source, e.target);
  dagre.layout(g);
  const horizontal = dir === "LR";
  let width = 0;
  let height = 0;
  const nodes: Node[] = flow.nodes.map((n) => {
    const pos = g.node(n.id);
    const x = (pos?.x ?? 0) - NODE_W / 2;
    const y = (pos?.y ?? 0) - NODE_H / 2;
    width = Math.max(width, x + NODE_W);
    height = Math.max(height, y + NODE_H);
    const inPR = n.path ? changed.has(n.path) : false;
    return {
      id: n.id,
      position: { x, y },
      sourcePosition: horizontal ? Position.Right : Position.Bottom,
      targetPosition: horizontal ? Position.Left : Position.Top,
      className: `flow-node fk-${n.kind}${n.path ? " clickable" : ""}${inPR ? " in-pr" : ""}`,
      style: { width: NODE_W },
      data: {
        label: (
          <div className="fn-inner" title={n.path ? `${n.path}${n.line ? `:${n.line}` : ""}` : undefined}>
            <div className="fn-label">{n.label}</div>
            <div className="fn-sub">
              <span className="fn-kind">{KIND_LABEL[n.kind]}</span>
              {n.path && (
                <span className="fn-path">
                  {splitPath(n.path).base}
                  {n.line ? `:${n.line}` : ""}
                </span>
              )}
            </div>
          </div>
        ),
      },
      connectable: false,
    };
  });
  const edges: Edge[] = edgesIn.map((e, i) => ({
    id: `e${i}`,
    source: e.source,
    target: e.target,
    type: "smoothstep",
    label: e.label ?? undefined,
    markerEnd: { type: MarkerType.ArrowClosed },
    className: "flow-edge",
    labelBgPadding: [4, 2] as [number, number],
    labelBgBorderRadius: 3,
  }));
  return { nodes, edges, width, height };
}

const fitZoom = (l: Layout, w: number, h: number) => Math.min(w / Math.max(l.width, 1), h / Math.max(l.height, 1));

function FlowCanvas({ flow, changed }: { flow: Flow; changed: Set<string> }) {
  const ref = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ w: 800, h: 500 });
  const rf = useReactFlow();
  const layouts = useMemo(() => ({ LR: layout(flow, changed, "LR"), TB: layout(flow, changed, "TB") }), [flow, changed]);
  // Pick whichever direction fits the pane at the larger zoom (wide panes → LR, narrow → TB).
  const dir: Dir = fitZoom(layouts.LR, size.w, size.h) >= fitZoom(layouts.TB, size.w, size.h) * 0.9 ? "LR" : "TB";
  const graph = layouts[dir];
  const byId = useMemo(() => new Map(flow.nodes.map((n) => [n.id, n])), [flow]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(([entry]) => {
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ w: width, h: height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  useEffect(() => {
    const t = window.setTimeout(() => void rf.fitView({ padding: 0.12, duration: 150 }), 30);
    return () => window.clearTimeout(t);
  }, [rf, graph, size.w, size.h]);

  return (
    <div className="flow-canvas" ref={ref}>
      <ReactFlow
        key={`${flow.id}-${dir}`}
        nodes={graph.nodes}
        edges={graph.edges}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        minZoom={0.2}
        maxZoom={1.6}
        colorMode="system"
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
        onNodeClick={(_, node) => {
          const n = byId.get(node.id);
          if (n?.path) goFile(n.path, n.line, "RIGHT", !changed.has(n.path));
        }}
      >
        <Background gap={18} size={1} />
        <Controls showInteractive={false} />
        {flow.nodes.length > 10 && <MiniMap pannable zoomable className="flow-minimap" />}
      </ReactFlow>
    </div>
  );
}

export function FlowView({ id }: { id: string | null }) {
  const analysis = useStore((s) => s.analysis);
  const flows = analysis?.flows ?? [];
  const flow = flows.find((f) => f.id === id) ?? flows[0];
  const changed = useMemo(() => new Set(fileMap(analysis).keys()), [analysis]);

  if (!analysis) return <div className="empty">Loading…</div>;
  if (!flow) {
    const pending = analysis.status !== "complete" && analysis.status !== "failed";
    return (
      <div className="empty">
        {pending ? "Data-flow diagrams appear when Claude finishes analyzing the PR." : "No data flows were identified for this PR."}
      </div>
    );
  }
  return (
    <div className="flow-view">
      {flows.length > 1 && (
        <div className="flow-tabs" role="tablist">
          {flows.map((f) => (
            <button
              key={f.id}
              role="tab"
              className={`tab${f.id === flow.id ? " active" : ""}`}
              onClick={() => navigate(`#/flow/${encodeURIComponent(f.id)}`)}
            >
              {f.title}
            </button>
          ))}
        </div>
      )}
      <div className="flow-body">
        <ReactFlowProvider>
          <FlowCanvas flow={flow} changed={changed} />
        </ReactFlowProvider>
        <aside className="flow-narrative">
          <h3>{flow.title}</h3>
          <Markdown text={flow.narrative} />
          <div className="flow-legend">
            {(Object.keys(KIND_LABEL) as FlowNodeKind[]).map((k) => (
              <span key={k} className={`legend-item fk-${k}`}>
                <span className="legend-swatch" />
                {KIND_LABEL[k]}
              </span>
            ))}
          </div>
          <p className="dim small">Click a node to open its code. Thick borders mark code changed in this PR.</p>
        </aside>
      </div>
    </div>
  );
}
