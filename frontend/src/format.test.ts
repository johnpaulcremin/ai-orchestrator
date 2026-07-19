import { describe, it, expect } from "vitest";
import { formatTimestamp, formatCost } from "./format";

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

describe("formatCost", () => {
  it("returns null when cost is missing", () => {
    expect(formatCost(null)).toBeNull();
    expect(formatCost(undefined)).toBeNull();
  });

  it("shows $0 for zero", () => {
    expect(formatCost(0)).toBe("$0");
  });

  it("shows <$0.0001 for sub-fraction-of-a-cent costs", () => {
    expect(formatCost(0.0000244)).toBe("<$0.0001");
  });

  it("shows four decimals for small costs", () => {
    expect(formatCost(0.000612)).toBe("$0.0006");
  });

  it("shows four decimals for larger costs too", () => {
    expect(formatCost(1.2345)).toBe("$1.2345");
  });
});
