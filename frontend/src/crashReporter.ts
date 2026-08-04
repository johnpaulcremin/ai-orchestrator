// Client-side crash reporter: forwards window.onerror /
// onunhandledrejection details (and ErrorBoundary catches — see
// ErrorBoundary.tsx) to POST /v1/client-errors, so a device that only shows
// a blank page — a phone, with devtools out of reach — still leaves a
// readable error server-side (GET /v1/client-errors).
//
// Design constraints, in order of importance:
//  1. Must never make anything worse: EVERY path — including the window
//     event listeners themselves, not just the fetch — is wrapped so a
//     failure inside the reporter (fetch unavailable, network down, a
//     non-stringifiable rejection reason) is swallowed silently.
//  2. Must not loop: a crashing render that fires the same error repeatedly
//     is capped per page load (MAX_REPORTS_PER_LOAD) and deduped by
//     message+stack, so the backend sees each distinct failure once.
//  3. Fire-and-forget: `keepalive` lets a report survive the page dying
//     right after (the exact scenario being debugged); the response is
//     never read. keepalive bodies share a 64 KiB browser quota, so an
//     over-large body falls back to a normal (non-keepalive) fetch rather
//     than being silently rejected — see reportClientError.

const API_BASE = "/api";

// Match the backend's stored caps (app/database.py record_client_error) so
// the client never sends more than will be kept anyway, and the common-case
// body stays comfortably under the keepalive quota (constraint 3).
const MESSAGE_MAX = 4_000;
const STACK_MAX = 30_000;
const URL_MAX = 2_000;

// Fetch's keepalive quota is 64 KiB shared across all in-flight keepalive
// requests; over that, the browser rejects with a network error. Stay well
// under it and fall back to a normal fetch for anything larger (a report
// arriving without keepalive is still far better than a dropped one).
const KEEPALIVE_BODY_LIMIT_BYTES = 60_000;

const MAX_REPORTS_PER_LOAD = 5;
let reportsSent = 0;
const seen = new Set<string>();

/** Replace the token in a `/shared/{token}` URL with a placeholder: a share
 * token is a secret capability, and this report lands in a store written by
 * an unauthenticated endpoint. The reporter is installed for the whole page
 * lifetime, including the public shared-conversation view (see main.tsx),
 * which is exactly where an error is most likely to carry one. */
function redactShareToken(url: string): string {
  return url.replace(/(\/shared\/)[^/?#]+/, "$1<redacted>");
}

export function reportClientError(message: string, stack?: string | null): void {
  try {
    if (reportsSent >= MAX_REPORTS_PER_LOAD) return;
    const key = `${message}\n${stack ?? ""}`;
    if (seen.has(key)) return;
    seen.add(key);
    reportsSent += 1;
    const body = JSON.stringify({
      message: String(message).slice(0, MESSAGE_MAX) || "(empty error message)",
      stack: stack ? String(stack).slice(0, STACK_MAX) : null,
      source_url: redactShareToken(window.location.href).slice(0, URL_MAX),
    });
    // TextEncoder measures real UTF-8 bytes (a stack of localized text or
    // many escaped newlines is heavier than its character count suggests).
    const tooBigForKeepalive =
      typeof TextEncoder !== "undefined" &&
      new TextEncoder().encode(body).length > KEEPALIVE_BODY_LIMIT_BYTES;
    void fetch(`${API_BASE}/v1/client-errors`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      keepalive: !tooBigForKeepalive,
      body,
    }).catch(() => {
      /* reporting must never throw — see design constraint 1 */
    });
  } catch {
    /* reporting must never throw — see design constraint 1 */
  }
}

/** Installs the window-level handlers. Returns an uninstall function —
 * unused in production (main.tsx installs once for the page's lifetime),
 * needed by tests, where jsdom's shared `window` would otherwise accumulate
 * one listener pair per test. */
export function installCrashReporter(): () => void {
  const onError = (event: ErrorEvent): void => {
    try {
      // A resource-load error (img/script tag) fires a plain Event with no
      // message on window; only report actual script errors.
      const error = event.error as Error | undefined;
      reportClientError(
        event.message || String(error?.message ?? "(unknown error)"),
        error?.stack ?? `at ${event.filename ?? "?"}:${event.lineno ?? "?"}:${event.colno ?? "?"}`,
      );
    } catch {
      /* constraint 1: the listener itself must never throw */
    }
  };
  const onRejection = (event: PromiseRejectionEvent): void => {
    try {
      const reason: unknown = event.reason;
      if (reason instanceof Error) {
        reportClientError(`Unhandled rejection: ${reason.message}`, reason.stack);
      } else {
        // String(reason) can itself throw (a null-prototype object, a
        // throwing Symbol.toPrimitive) — hence the surrounding try/catch.
        reportClientError(`Unhandled rejection: ${String(reason)}`);
      }
    } catch {
      reportClientError("Unhandled rejection: (unstringifiable reason)");
    }
  };
  try {
    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onRejection);
  } catch {
    /* reporting must never throw — see design constraint 1 */
  }
  return () => {
    window.removeEventListener("error", onError);
    window.removeEventListener("unhandledrejection", onRejection);
  };
}
