import { describe, it, expect } from "vitest";
import { formatTimestamp } from "./format";

describe("formatTimestamp", () => {
  it("parses a backend UTC timestamp as UTC (not local time)", () => {
    // "2026-07-18 11:00:32" is UTC; the parsed instant must match that moment.
    const out = formatTimestamp("2026-07-18 11:00:32");
    const expected = new Date(Date.UTC(2026, 6, 18, 11, 0, 32)).toLocaleString();
    expect(out).toBe(expected);
  });

  it("returns the raw string when the value cannot be parsed", () => {
    expect(formatTimestamp("not a date")).toBe("not a date");
  });

  it("returns the raw string for an empty value", () => {
    expect(formatTimestamp("")).toBe("");
  });
});
