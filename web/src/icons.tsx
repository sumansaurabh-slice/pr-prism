// Small 16px line icons in the style of GitHub's Octicons, drawn by hand so we ship no icon font.
import type { ReactNode } from "react";
import type { FileStatus } from "./types";

interface IconProps {
  className?: string;
  size?: number;
  title?: string;
}

function Svg({ children, className, size = 16, title, fill }: IconProps & { children: ReactNode; fill?: boolean }) {
  return (
    <svg
      className={`octicon${className ? ` ${className}` : ""}`}
      width={size}
      height={size}
      viewBox="0 0 16 16"
      role={title ? "img" : undefined}
      aria-hidden={title ? undefined : true}
      fill={fill ? "currentColor" : "none"}
      stroke={fill ? "none" : "currentColor"}
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {title && <title>{title}</title>}
      {children}
    </svg>
  );
}

const box = <rect x="1.75" y="1.75" width="12.5" height="12.5" rx="2" />;

/** The coloured square GitHub shows next to each changed file. */
export function StatusIcon({ status, className }: { status: FileStatus; className?: string }) {
  const title = { added: "Added", deleted: "Deleted", modified: "Changed", renamed: "Renamed", copied: "Copied" }[status];
  return (
    <Svg className={`status-icon st-${status}${className ? ` ${className}` : ""}`} title={title}>
      {box}
      {status === "added" && <path d="M8 5v6M5 8h6" />}
      {status === "deleted" && <path d="M5 8h6" />}
      {status === "modified" && <circle cx="8" cy="8" r="1.6" fill="currentColor" stroke="none" />}
      {(status === "renamed" || status === "copied") && <path d="M5 8h5.5M8.5 5.5 11 8l-2.5 2.5" />}
    </Svg>
  );
}

export const IconCheck = (p: IconProps) => (
  <Svg {...p}>
    <path d="M13.25 4.75 6.5 11.5 2.75 7.75" />
  </Svg>
);

export const IconChevronDown = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 6l4 4 4-4" />
  </Svg>
);

export const IconChevronRight = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6 4l4 4-4 4" />
  </Svg>
);

export const IconPullRequest = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="4" cy="3.5" r="1.75" />
    <circle cx="4" cy="12.5" r="1.75" />
    <circle cx="12" cy="12.5" r="1.75" />
    <path d="M4 5.25v5.5M12 10.75V6.25A2.25 2.25 0 0 0 9.75 4H7.5M9 2.25 7.25 4 9 5.75" />
  </Svg>
);

export const IconMerged = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="4" cy="3.5" r="1.75" />
    <circle cx="4" cy="12.5" r="1.75" />
    <circle cx="12" cy="8.5" r="1.75" />
    <path d="M4 5.25v5.5M4 5.5c0 2.25 2 3 6.25 3" />
  </Svg>
);

export const IconComment = (p: IconProps) => (
  <Svg {...p}>
    <path d="M2.75 2.5h10.5c.69 0 1.25.56 1.25 1.25v6.5c0 .69-.56 1.25-1.25 1.25H8.5L5.25 14v-2.5h-2.5c-.69 0-1.25-.56-1.25-1.25v-6.5c0-.69.56-1.25 1.25-1.25z" />
  </Svg>
);

export const IconFileDiff = (p: IconProps) => (
  <Svg {...p}>
    <path d="M9.5 1.75H4.25c-.69 0-1.25.56-1.25 1.25v10c0 .69.56 1.25 1.25 1.25h7.5c.69 0 1.25-.56 1.25-1.25V5.25z" />
    <path d="M8 5.5v4M6 7.5h4M6 11.5h4" />
  </Svg>
);

export const IconWorkflow = (p: IconProps) => (
  <Svg {...p}>
    <rect x="1.75" y="1.75" width="4.5" height="4.5" rx="1" />
    <rect x="9.75" y="9.75" width="4.5" height="4.5" rx="1" />
    <path d="M6.25 4h2.5a3.25 3.25 0 0 1 3.25 3.25v2.5" />
  </Svg>
);

export const IconPulse = (p: IconProps) => (
  <Svg {...p}>
    <path d="M1 8h3l2-5 4 10 2-5h3" />
  </Svg>
);

export const IconSearch = (p: IconProps) => (
  <Svg {...p}>
    <circle cx="7" cy="7" r="4.25" />
    <path d="M10.25 10.25 14 14" />
  </Svg>
);

export const IconSparkle = (p: IconProps) => (
  <Svg {...p} fill>
    <path d="M8 1.25 9.5 6.5 14.75 8 9.5 9.5 8 14.75 6.5 9.5 1.25 8 6.5 6.5Z" />
  </Svg>
);

export const IconX = (p: IconProps) => (
  <Svg {...p}>
    <path d="M4 4l8 8M12 4l-8 8" />
  </Svg>
);

export const IconLink = (p: IconProps) => (
  <Svg {...p}>
    <path d="M6.5 9.5a2.75 2.75 0 0 0 3.9 0l2.35-2.35a2.75 2.75 0 0 0-3.9-3.9l-.85.85M9.5 6.5a2.75 2.75 0 0 0-3.9 0L3.25 8.85a2.75 2.75 0 0 0 3.9 3.9l.85-.85" />
  </Svg>
);

/** GitHub's row of five green / red / grey squares that shows the size of a change. */
export function DiffStat({ additions, deletions }: { additions: number; deletions: number }) {
  const total = additions + deletions;
  let green = total ? Math.round((additions / total) * 5) : 0;
  let red = total ? Math.round((deletions / total) * 5) : 0;
  // A side that changed at least one line always gets at least one block (e.g. +1 −100).
  if (additions && !green) green = 1;
  if (deletions && !red) red = 1;
  if (green + red > 5) {
    if (green > red) green = 5 - red;
    else red = 5 - green;
  }
  return (
    <span className="diffstat" title={`${additions} added, ${deletions} removed`}>
      <span className="add">+{additions.toLocaleString()}</span>
      <span className="del">−{deletions.toLocaleString()}</span>
      <span className="diffstat-blocks" aria-hidden>
        {Array.from({ length: 5 }, (_, i) => (
          <span key={i} className={i < green ? "blk-add" : i < green + red ? "blk-del" : "blk-neutral"} />
        ))}
      </span>
    </span>
  );
}

/** A round avatar with initials (remote avatar images are blocked by the page's security policy). */
export function Avatar({ name, kind, size = 20 }: { name: string; kind?: "claude" | "you" | "user"; size?: number }) {
  const initials = name.replace(/^@/, "").slice(0, 1).toUpperCase() || "?";
  return (
    <span className={`avatar avatar-${kind ?? "user"}`} style={{ width: size, height: size, fontSize: size * 0.5 }} aria-hidden>
      {kind === "claude" ? <IconSparkle size={Math.round(size * 0.6)} /> : initials}
    </span>
  );
}
