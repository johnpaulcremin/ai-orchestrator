import { useEffect, useRef, useState } from "react";
import { formatTimestamp, downloadTextFile } from "./format";
import { useModalFocus } from "./useModalFocus";

type BookmarkedMessage = {
  id: number;
  conversation_id: number;
  conversation_title: string;
  role: string;
  content: string;
  created_at: string;
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  onSelectMessage: (conversationId: number, messageId: number) => void;
};

export function Bookmarks({ apiBase, getHeaders, onClose, onSelectMessage }: Props) {
  const [items, setItems] = useState<BookmarkedMessage[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [query, setQuery] = useState("");

  async function removeBookmark(item: BookmarkedMessage) {
    setRemovingId(item.id);
    setError("");
    try {
      const res = await fetch(
        `${apiBase}/v1/conversations/${item.conversation_id}/messages/${item.id}/bookmark`,
        {
          method: "PUT",
          headers: getHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ bookmarked: false }),
        },
      );
      if (!res.ok) throw new Error(`Failed to remove bookmark (${res.status})`);
      setItems((current) => (current ? current.filter((entry) => entry.id !== item.id) : current));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to remove bookmark");
    } finally {
      setRemovingId(null);
    }
  }

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/bookmarks`, { headers: getHeaders() });
        if (!res.ok) throw new Error(`Failed to load bookmarks (${res.status})`);
        const view = (await res.json()) as BookmarkedMessage[];
        if (!cancelled) {
          setItems(view);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load bookmarks");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const trimmedQuery = query.trim().toLowerCase();
  const visibleItems = items
    ? trimmedQuery
      ? items.filter(
          (item) =>
            item.conversation_title.toLowerCase().includes(trimmedQuery) ||
            item.content.toLowerCase().includes(trimmedQuery),
        )
      : items
    : null;

  function exportBookmarks() {
    if (!visibleItems || visibleItems.length === 0) {
      return;
    }
    const lines: string[] = ["# Bookmarked messages", ""];
    for (const item of visibleItems) {
      lines.push(`## ${item.conversation_title}`);
      lines.push(`_${formatTimestamp(item.created_at)}_`);
      lines.push("");
      lines.push(item.content);
      lines.push("");
      lines.push("---");
      lines.push("");
    }
    downloadTextFile(lines.join("\n"), "text/markdown", "ai-workbench-bookmarks.md");
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  return (
    <div
      className="settings-overlay"
      role="presentation"
      onClick={onClose}
      onKeyDown={(event) => {
        if (event.key === "Escape") onClose();
      }}
    >
      <div
        ref={dialogRef}
        className="settings-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="Bookmarks"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Bookmarks</h2>
          <button className="link-button" onClick={onClose} aria-label="Close bookmarks">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Every message you've bookmarked, across every conversation. Click one to jump straight
          to it.
        </p>

        <div className="bookmark-toolbar">
          <input
            type="search"
            className="bookmark-search"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search bookmarks…"
            aria-label="Search bookmarks"
            disabled={!items || items.length === 0}
          />
          <button
            type="button"
            className="secondary-button"
            onClick={exportBookmarks}
            disabled={!visibleItems || visibleItems.length === 0}
          >
            ⬇️ Export
          </button>
        </div>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        {loading && !items ? (
          <p className="settings-loading">Loading…</p>
        ) : items && visibleItems ? (
          items.length === 0 ? (
            <p className="settings-readonly">
              No bookmarks yet — click 🏷️ on a message to bookmark it.
            </p>
          ) : visibleItems.length === 0 ? (
            <p className="settings-readonly">No bookmarks match "{query.trim()}".</p>
          ) : (
            <div className="bookmark-list">
              {visibleItems.map((item) => (
                <div className="bookmark-row" key={item.id}>
                  <button
                    type="button"
                    className="bookmark-row-main"
                    onClick={() => {
                      onSelectMessage(item.conversation_id, item.id);
                      onClose();
                    }}
                  >
                    <div className="bookmark-row-header">
                      <span className="bookmark-conversation-title">
                        {item.conversation_title}
                      </span>
                      <span className="bookmark-timestamp">
                        {formatTimestamp(item.created_at)}
                      </span>
                    </div>
                    <p className="bookmark-snippet">
                      {item.content.length > 200 ? `${item.content.slice(0, 200)}…` : item.content}
                    </p>
                  </button>
                  <button
                    type="button"
                    className="link-button bookmark-remove"
                    onClick={() => void removeBookmark(item)}
                    disabled={removingId === item.id}
                    aria-label={`Remove bookmark from ${item.conversation_title}`}
                    title="Remove this bookmark"
                  >
                    🏷️
                  </button>
                </div>
              ))}
            </div>
          )
        ) : null}

        <footer className="settings-footer">
          <button className="secondary-button" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}
