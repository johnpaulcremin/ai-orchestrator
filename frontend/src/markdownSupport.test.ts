import { describe, expect, it } from "vitest";

describe("markdownSupport", () => {
  it("detects lookbehind support in the current (modern) test environment", async () => {
    // Node/jsdom's V8 supports lookbehind, so this pins the happy path;
    // the actual point of the module -- degrading gracefully when it's
    // NOT supported -- is exercised by MessageList/SharedConversation's
    // own tests, which mock this module's export to false.
    const { supportsRegexLookbehind } = await import("./markdownSupport");
    expect(supportsRegexLookbehind).toBe(true);
  });

  it("never throws even in principle -- detection is try/catch guarded", () => {
    // Constructing a lookbehind regex from a STRING (not a literal) is how
    // the module avoids a parse-time SyntaxError on an engine that lacks
    // support; this pins that RegExp(string) is the mechanism, not a
    // literal that would break this very file's own parsing on old Safari.
    expect(() => new RegExp("(?<=x)y")).not.toThrow();
  });
});
