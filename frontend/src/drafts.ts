// A conversation-id -> unsent-question-text map, so switching away from a
// half-typed question (or reloading the page) doesn't silently discard it —
// and, just as importantly, doesn't leave it sitting in a DIFFERENT
// conversation's composer where it could get sent to the wrong thread.

const DRAFTS_STORAGE_KEY = "ai_workbench_drafts";

export function loadDraftMap(): Record<string, string> {
  try {
    const raw = window.localStorage.getItem(DRAFTS_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as Record<string, string>) : {};
  } catch {
    return {};
  }
}

export function saveDraftMap(drafts: Record<string, string>) {
  try {
    if (Object.keys(drafts).length === 0) {
      window.localStorage.removeItem(DRAFTS_STORAGE_KEY);
    } else {
      window.localStorage.setItem(DRAFTS_STORAGE_KEY, JSON.stringify(drafts));
    }
  } catch {
    // Storage disabled (private browsing) or full — drafts just won't
    // survive a reload/switch this session; not worth interrupting the chat.
  }
}

export function setDraft(drafts: Record<string, string>, conversationId: number, text: string) {
  const key = String(conversationId);
  if (text.trim()) {
    drafts[key] = text;
  } else {
    delete drafts[key];
  }
}
