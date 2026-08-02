import { formatTimestamp } from "./format";
import type { Conversation, Message } from "./types";

// Shared by the Markdown file export and the clipboard copy — same content,
// different destination.
export function buildConversationMarkdown(
  conversation: Conversation,
  conversationMessages: Message[],
): string {
  const lines: string[] = [`# ${conversation.title}`, ""];
  for (const message of conversationMessages) {
    lines.push(`## ${message.role === "user" ? "User" : "Assistant"} — ${formatTimestamp(message.created_at)}`, "");
    lines.push(message.content, "");
    if (message.sources && message.sources.length > 0) {
      lines.push("**Sources:**");
      for (const source of message.sources) {
        lines.push(`- [${source.title || source.url}](${source.url})`);
      }
      lines.push("");
    }
    if (message.images && message.images.length > 0) {
      lines.push(`_${message.images.length} image(s) attached — omitted from this export._`, "");
    }
    if (message.code_results && message.code_results.length > 0) {
      for (const result of message.code_results) {
        lines.push("```python", result.code, "```", "");
        if (result.logs) {
          lines.push("```", result.logs, "```", "");
        }
      }
    }
    if (message.fact_checks && message.fact_checks.length > 0) {
      lines.push("**Fact checks:**");
      for (const result of message.fact_checks) {
        const rating = result.rating ? `${result.rating} — ` : "";
        const source = result.url ? ` ([${result.publisher || result.url}](${result.url}))` : "";
        lines.push(`- ${rating}${result.claim}${source}`);
      }
      lines.push("");
    }
    if (message.academic_results && message.academic_results.length > 0) {
      lines.push("**Academic search:**");
      for (const result of message.academic_results) {
        const meta = [result.authors, result.year].filter(Boolean).join(", ");
        const source = result.url ? ` ([${result.venue || result.url}](${result.url}))` : "";
        lines.push(`- ${result.title}${meta ? ` (${meta})` : ""}${source}`);
      }
      lines.push("");
    }
    if (message.math_results && message.math_results.length > 0) {
      lines.push("**Computed:**");
      for (const result of message.math_results) {
        const value = result.result ? `= ${result.result}` : `(${result.error})`;
        lines.push(`- \`${result.expression}\` ${value}`);
      }
      lines.push("");
    }
    if (message.files && message.files.length > 0) {
      lines.push(`**Attached files:** ${message.files.map((file) => file.filename).join(", ")}`, "");
    }
  }
  return lines.join("\n");
}
