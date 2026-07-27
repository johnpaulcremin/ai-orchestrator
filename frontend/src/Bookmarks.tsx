import { useEffect, useRef, useState } from "react";
import { formatTimestamp } from "./format";
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
  onSelectConversation: (conversationId: number) => void;
};

export function Bookmarks({ apiBase, getHeaders, onClose, onSelectConversation }: Props) {
  const [items, setItems] = useState<BookmarkedMessage[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

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
          Every message you've bookmarked, across every conversation.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        {loading && !items ? (
          <p className="settings-loading">Loading…</p>
        ) : items ? (
          items.length === 0 ? (
            <p className="settings-readonly">
              No bookmarks yet — click 🏷️ on a message to bookmark it.
            </p>
          ) : (
            <div className="bookmark-list">
              {items.map((item) => (
                <button
                  type="button"
                  className="bookmark-row"
                  key={item.id}
                  onClick={() => {
                    onSelectConversation(item.conversation_id);
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
