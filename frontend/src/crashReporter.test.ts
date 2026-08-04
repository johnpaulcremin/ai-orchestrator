import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// The reporter keeps per-page-load state (sent count, dedupe set) at module
// level, so every test re-imports a FRESH copy via resetModules + dynamic
// import — otherwise one test's reports would eat into the next test's cap.
async function freshReporter() {
  vi.resetModules();
  return await import("./crashReporter");
}

describe("crashReporter", () => {
  const fetchMock = vi.fn(() => Promise.resolve(new Response(null, { status: 204 })));
  let uninstall: (() => void) | null = null;

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    // jsdom's window is shared across tests — remove any listeners this
    // test installed so the next test's dispatches hit only its own.
    uninstall?.();
    uninstall = null;
    vi.unstubAllGlobals();
    fetchMock.mockClear();
  });

  function sentBodies(): Array<{ message: string; stack: string | null; source_url: string }> {
    return fetchMock.mock.calls.map((call) =>
      JSON.parse((call as unknown as [string, RequestInit])[1].body as string),
    );
  }

  it("POSTs a report to /api/v1/client-errors with message, stack and URL", async () => {
    const { reportClientError } = await freshReporter();
    reportClientError("TypeError: boom", "at App.tsx:1");

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/v1/client-errors");
    expect(init.method).toBe("POST");
    expect(init.keepalive).toBe(true);
    const body = sentBodies()[0];
    expect(body.message).toBe("TypeError: boom");
    expect(body.stack).toBe("at App.tsx:1");
    expect(body.source_url).toContain("http");
  });

  it("dedupes an identical error within one page load", async () => {
    const { reportClientError } = await freshReporter();
    reportClientError("same error", "same stack");
    reportClientError("same error", "same stack");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("caps reports per page load so an error loop cannot flood the backend", async () => {
    const { reportClientError } = await freshReporter();
    for (let n = 0; n < 20; n += 1) {
      reportClientError(`distinct error ${n}`);
    }
    expect(fetchMock).toHaveBeenCalledTimes(5);
  });

  it("never throws even when fetch itself is unavailable", async () => {
    vi.stubGlobal("fetch", undefined);
    const { reportClientError } = await freshReporter();
    expect(() => reportClientError("boom")).not.toThrow();
  });

  it("installCrashReporter forwards window error events", async () => {
    const { installCrashReporter } = await freshReporter();
    uninstall = installCrashReporter();
    window.dispatchEvent(
      new ErrorEvent("error", {
        message: "Uncaught TypeError: x is not a function",
        filename: "https://device.ts.net/assets/index-abc.js",
        lineno: 10,
        colno: 5,
      }),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentBodies()[0].message).toBe("Uncaught TypeError: x is not a function");
  });

  it("installCrashReporter forwards unhandled promise rejections", async () => {
    const { installCrashReporter } = await freshReporter();
    uninstall = installCrashReporter();
    // jsdom has no PromiseRejectionEvent constructor — a plain Event with the
    // right type exercises the same listener (reason reads as undefined).
    window.dispatchEvent(new Event("unhandledrejection"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentBodies()[0].message).toContain("Unhandled rejection");
  });

  it("truncates a pathologically long message client-side", async () => {
    const { reportClientError } = await freshReporter();
    reportClientError("M".repeat(50_000));
    // Capped to the backend's stored message limit (4000), so the client
    // never sends more than will be kept.
    expect(sentBodies()[0].message.length).toBe(4_000);
  });

  it("redacts a /shared/{token} token from the reported URL", async () => {
    const original = window.location.href;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { href: "https://host.ts.net/shared/SECRET-TOKEN-123?x=1" },
    });
    try {
      const { reportClientError } = await freshReporter();
      reportClientError("boom on shared page");
      expect(sentBodies()[0].source_url).toBe(
        "https://host.ts.net/shared/<redacted>?x=1",
      );
    } finally {
      Object.defineProperty(window, "location", {
        configurable: true,
        value: { href: original },
      });
    }
  });

  it("uses keepalive for a normal report but falls back for an over-quota body", async () => {
    const { reportClientError } = await freshReporter();
    reportClientError("small", "at App.tsx:1");
    reportClientError("big", "S".repeat(30_000).replace(/S/g, "é")); // multi-byte, well over 60KB

    const inits = fetchMock.mock.calls.map(
      (call) => (call as unknown as [string, RequestInit])[1],
    );
    expect(inits[0].keepalive).toBe(true);
    expect(inits[1].keepalive).toBe(false);
  });

  it("never throws when a rejection reason is a null-prototype object", async () => {
    const { installCrashReporter } = await freshReporter();
    uninstall = installCrashReporter();
    // A null-prototype object throws on String() — the listener must survive.
    const event = new Event("unhandledrejection") as Event & { reason?: unknown };
    event.reason = Object.create(null);
    expect(() => window.dispatchEvent(event)).not.toThrow();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(sentBodies()[0].message).toContain("unstringifiable reason");
  });
});
