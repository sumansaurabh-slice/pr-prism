// The three fixed "changed" phrases (design review issue 16), each meaning one thing everywhere:
// "Changed since you viewed" (one file), "Changes since your review" (the PR since your last submitted review),
// "New commits since you opened this" (updates that arrive while the page is open).
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const SRC = __dirname; // web/src

function sources(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name);
    return statSync(p).isDirectory() ? sources(p) : /\.(tsx?|css)$/.test(name) ? [p] : [];
  });
}

/** Files under web/src whose text matches `re`, as "path:line". */
function find(re: RegExp): string[] {
  return sources(SRC).flatMap((p) =>
    readFileSync(p, "utf8")
      .split("\n")
      .flatMap((l, i) => (re.test(l) ? [`${p.slice(SRC.length + 1)}:${i + 1}`] : [])),
  );
}

// The toggle's old name, spelled in pieces so this file doesn't match itself.
const OLD_TOGGLE = ["since", "my", "review"];

describe("the fixed changed phrases", () => {
  it(`never says "${OLD_TOGGLE.join(" ")}"`, () => {
    expect(find(new RegExp(OLD_TOGGLE.join("\\s+"), "i"))).toEqual([]);
  });

  it("uses the old wordings nowhere", () => {
    expect(find(new RegExp(["Changed since you viewed", "it"].join(" ")))).toEqual([]);
    expect(find(new RegExp(["got new commits", "after"].join(" "), "i"))).toEqual([]);
  });
});
