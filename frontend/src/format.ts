/**
 * Render a backend UTC timestamp ("YYYY-MM-DD HH:MM:SS") in the viewer's local
 * time. Falls back to the raw string if it cannot be parsed.
 */
export function formatTimestamp(value: string): string {
  const parsed = new Date(value.replace(" ", "T") + "Z");
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString();
}

/** Render an estimated USD cost compactly, or null if unknown. */
export function formatCost(cost: number | null | undefined): string | null {
  if (cost == null) return null;
  if (cost === 0) return "$0";
  if (cost < 0.0001) return "<$0.0001";
  return "$" + cost.toFixed(4);
}

// Bedrock model ids carry the vendor as a dotted prefix
// ("anthropic.claude-3-5-sonnet-20241022-v2:0"). An explicit list rather than
// "strip up to the first dot", which would mangle a version-numbered name
// like "gpt-4.1" into "1".
const BEDROCK_VENDOR_PREFIX =
  /^(anthropic|amazon|meta|mistral|cohere|ai21|stability|deepseek|writer|luma)\./;

/**
 * A model id trimmed to the part that actually identifies the model, for the
 * per-message badge — LiteLLM routes carry a provider path
 * ("gemini/gemini-flash-latest") and Bedrock adds a vendor prefix on top
 * ("bedrock/anthropic.claude-3-5-sonnet-..."), neither of which distinguishes
 * one answer from another in a UI where the row is already dense. The full id
 * is never lost: the badge keeps it as its `title`.
 */
export function shortModelName(model: string): string {
  const afterPath = model.slice(model.lastIndexOf("/") + 1).trim();
  return afterPath.replace(BEDROCK_VENDOR_PREFIX, "") || afterPath;
}

/**
 * The label for the per-message model badge, or "" when it should not render
 * at all.
 *
 * Empty for a message with no recorded model (every message persisted before
 * the column existed) — nothing, never an "unknown" placeholder for what is
 * simply an older row.
 *
 * Also empty when `mode_used` already names the same model, which two of the
 * routing shapes do: `forced:<model>` and `auto->free:<model>`. Repeating it
 * would be pure noise — and in the free case it would be the THIRD copy on
 * one row, since that path also renders a "served free via <model>" badge.
 * The remaining shapes (`fast`, `auto->smart`, `workflow(2 steps)`, ...) name
 * a tier, not a model, and the tier→model map is configurable, so there the
 * badge is the only place the answering model appears.
 */
export function modelBadgeLabel(
  model: string | null | undefined,
  modeUsed: string | null | undefined,
): string {
  if (!model) return "";
  const short = shortModelName(model);
  if (!short) return "";
  return (modeUsed ?? "").includes(short) ? "" : short;
}

/**
 * The message to show for a 401 from an authenticated panel fetch, worded
 * for whichever credential this deployment actually uses — "sign in again"
 * would be confusing advice in a static-token-only deployment, which has no
 * sign-in form, just a token field (mirrors App.tsx's authFetch, which
 * already gets this right for its own calls; this covers the standalone
 * panels — Settings, Usage, Bookmarks, etc. — that fetch independently).
 */
export function authFailureMessage(jwtEnabled: boolean): string {
  return jwtEnabled
    ? "Your session has expired — please sign in again."
    : "Your API token was rejected — enter a valid one in the sidebar.";
}

/** Trigger a browser download of in-memory text content as a file. */
export function downloadTextFile(content: string, mime: string, filename: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
}
