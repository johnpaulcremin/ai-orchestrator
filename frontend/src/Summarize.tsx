import { useEffect, useRef, useState } from "react";
import { authFailureMessage } from "./format";
import { useModalFocus } from "./useModalFocus";

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  conversationId: number;
  onClose: () => void;
  jwtEnabled: boolean;
};

export function Summarize({ apiBase, getHeaders, conversationId, onClose, jwtEnabled }: Props) {
  const [summary, setSummary] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/conversations/${conversationId}/summarize`, {
          method: "POST",
          headers: getHeaders(),
        });
        if (!res.ok) {
          if (res.status === 401) throw new Error(authFailureMessage(jwtEnabled));
          const body = await res.json().catch(() => null);
          throw new Error(
            (body && typeof body.detail === "string" && body.detail) ||
              `Failed to summarize conversation (${res.status})`,
          );
        }
        const view = (await res.json()) as { summary: string };
        if (!cancelled) {
          setSummary(view.summary);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to summarize conversation");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversationId]);

  async function copySummary() {
    if (!summary) return;
    try {
      await navigator.clipboard.writeText(summary);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setError("Failed to copy to clipboard.");
    }
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
        aria-label="Summarize conversation"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>🧾 Summary</h2>
          <button className="link-button" onClick={onClose} aria-label="Close summary">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          A short TL;DR of this conversation — key topics, decisions, and open
          questions. Generated fresh each time, not saved anywhere.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        {loading ? (
          <p className="settings-loading">Summarizing…</p>
        ) : summary ? (
          <p className="summary-text">{summary}</p>
        ) : null}

        <footer className="settings-footer">
          {summary ? (
            <button type="button" className="secondary-button" onClick={() => void copySummary()}>
              {copied ? "✓ Copied" : "📋 Copy"}
            </button>
          ) : null}
          <button className="secondary-button" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}
