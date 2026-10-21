import { memo, type ReactNode } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";
import { goFile } from "./router";
import { fileMap, useStore } from "./store";
import type { Side } from "./types";

// `path`, `path:12`, `path:12-40`, `path#L12`, `path#L12-L40`
const REF_RE = /^([\w@.+\-/]+?)(?:(?::|#L)(\d+)(?:-L?(\d+))?)?$/;

export interface CodeRef { path: string; line: number | null; end: number | null }

export function parseCodeRef(text: string, known: Map<string, unknown>): CodeRef | null {
  const m = REF_RE.exec(text.trim());
  if (!m) return null;
  let path = m[1].replace(/^\.\//, "").replace(/^[ab]\//, "");
  if (!known.has(path)) {
    // Accept a unique suffix match (Claude sometimes drops a leading directory).
    if (path.includes("/") || /\.\w+$/.test(path)) {
      const hits = [...known.keys()].filter((k) => k === path || k.endsWith(`/${path}`));
      if (hits.length === 1) path = hits[0];
      else if (!(path.includes("/") && /\.\w+$/.test(path))) return null;
      // else: a plausible repo path outside the diff → open the full-file view
    } else {
      return null;
    }
  }
  return { path, line: m[2] ? Number(m[2]) : null, end: m[3] ? Number(m[3]) : null };
}

export function RefLink({ refInfo, side, children }: { refInfo: CodeRef; side?: Side; children: ReactNode }) {
  const files = fileMap(useStore.getState().analysis);
  const changed = files.has(refInfo.path);
  return (
    <a
      className={`code-ref${changed ? "" : " code-ref-outside"}`}
      href="#"
      title={changed ? "Open in walkthrough" : "Not changed in this PR — open the full file"}
      onClick={(e) => {
        e.preventDefault();
        goFile(refInfo.path, refInfo.line, side, !changed);
      }}
    >
      {children}
    </a>
  );
}

function MarkdownImpl({ text, className }: { text: string; className?: string }) {
  const analysis = useStore((s) => s.analysis);
  const known = fileMap(analysis);
  const components: Components = {
    code({ className: cls, children, ...rest }) {
      const raw = String(children ?? "");
      const isBlock = /language-/.test(cls || "") || raw.includes("\n");
      if (!isBlock) {
        const ref = parseCodeRef(raw, known);
        if (ref) {
          return (
            <RefLink refInfo={ref}>
              <code>{raw}</code>
            </RefLink>
          );
        }
      }
      return (
        <code className={cls} {...rest}>
          {children}
        </code>
      );
    },
    a({ href, children }) {
      const safe = href && /^(https?:|mailto:|#)/.test(href) ? href : undefined;
      return (
        <a href={safe} target={safe?.startsWith("#") ? undefined : "_blank"} rel="noreferrer noopener">
          {children}
        </a>
      );
    },
    img({ alt }) {
      // CSP blocks remote images; show the alt text instead of a broken image.
      return <span className="md-img-alt">[image: {alt}]</span>;
    },
  };
  return (
    <div className={`md ${className ?? ""}`}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export const Markdown = memo(MarkdownImpl);

/** Cheap inline renderer for short labels: only `code spans` (linkified when they name a file). */
export function InlineText({ text }: { text: string }) {
  const analysis = useStore((s) => s.analysis);
  const known = fileMap(analysis);
  const parts = text.split(/`([^`]+)`/);
  return (
    <>
      {parts.map((part, i) => {
        if (i % 2 === 0) return part;
        const ref = parseCodeRef(part, known);
        return ref ? (
          <RefLink key={i} refInfo={ref}>
            <code>{part}</code>
          </RefLink>
        ) : (
          <code key={i}>{part}</code>
        );
      })}
    </>
  );
}
