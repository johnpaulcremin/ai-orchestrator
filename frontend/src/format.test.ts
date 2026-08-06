import { describe, it, expect } from "vitest";
import { formatTimestamp, formatCost, modelBadgeLabel, shortModelName } from "./format";

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

describe("shortModelName", () => {
  it("leaves a plain model id alone", () => {
    expect(shortModelName("gpt-5")).toBe("gpt-5");
    expect(shortModelName("claude-sonnet-5")).toBe("claude-sonnet-5");
  });

  it("strips a LiteLLM provider path", () => {
    expect(shortModelName("gemini/gemini-flash-latest")).toBe("gemini-flash-latest");
    expect(shortModelName("mistral/mistral-large-latest")).toBe("mistral-large-latest");
    expect(shortModelName("ollama/llama3")).toBe("llama3");
  });

  it("strips a Bedrock vendor prefix as well as the path", () => {
    expect(shortModelName("bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0")).toBe(
      "claude-3-5-sonnet-20241022-v2:0",
    );
    expect(shortModelName("bedrock/meta.llama3-70b-instruct-v1:0")).toBe(
      "llama3-70b-instruct-v1:0",
    );
  });

  it("does not mistake a version number for a vendor prefix", () => {
    // "strip up to the first dot" would turn this into "1".
    expect(shortModelName("gpt-4.1")).toBe("gpt-4.1");
    expect(shortModelName("gpt-4.1-mini")).toBe("gpt-4.1-mini");
  });
});

describe("modelBadgeLabel", () => {
  it("labels the model for a routing mode that names only a tier", () => {
    expect(modelBadgeLabel("gpt-5-mini", "auto->fast")).toBe("gpt-5-mini");
    expect(modelBadgeLabel("gemini/gemini-flash-latest", "smart")).toBe(
      "gemini-flash-latest",
    );
    expect(modelBadgeLabel("gpt-5", "workflow(2 steps)")).toBe("gpt-5");
    expect(modelBadgeLabel("gpt-5", "auto->fast->fallback")).toBe("gpt-5");
  });

  it("renders nothing when the message has no recorded model", () => {
    // Every message persisted before the column existed. Nothing at all --
    // never an "unknown" placeholder for what is simply an older row.
    expect(modelBadgeLabel(null, "auto->fast")).toBe("");
    expect(modelBadgeLabel(undefined, "auto->fast")).toBe("");
    expect(modelBadgeLabel("", "auto->fast")).toBe("");
  });

  it("renders nothing when the mode badge already names the same model", () => {
    // The two routing shapes that embed the model id. Repeating it beside
    // them is noise -- and in the free case it would be a third copy, since
    // that path also renders "served free via <model>".
    expect(modelBadgeLabel("gpt-5", "forced:gpt-5")).toBe("");
    expect(modelBadgeLabel("gpt-5-mini", "auto->free:gpt-5-mini")).toBe("");
    expect(
      modelBadgeLabel("gemini/gemini-flash-latest", "forced:gemini/gemini-flash-latest"),
    ).toBe("");
  });

  it("still labels a model the mode only partly resembles", () => {
    // "auto->free:gpt-5-mini" does not contain "gpt-5-nano", so a genuinely
    // different model is never suppressed by a near-match.
    expect(modelBadgeLabel("gpt-5-nano", "auto->free:gpt-5-mini")).toBe("gpt-5-nano");
  });
});
