import { describe, expect, it } from "vitest";
import { hunkId } from "./api";

describe("hunkId", () => {
  it("names a hunk by its new lines, like prr/coverage.py hunk_id", () => {
    expect(hunkId({ old_start: 10, old_lines: 3, new_start: 12, new_lines: 5 })).toBe("RIGHT:12-16");
    expect(hunkId({ old_start: 1, old_lines: 0, new_start: 1, new_lines: 1 })).toBe("RIGHT:1-1");
  });

  it("names a hunk that only deletes lines by its old lines", () => {
    expect(hunkId({ old_start: 10, old_lines: 3, new_start: 9, new_lines: 0 })).toBe("LEFT:10-12");
  });
});
