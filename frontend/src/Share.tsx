import { useEffect, useRef, useState } from "react";
import { useModalFocus } from "./useModalFocus";
import type { ShareStatus } from "./types";

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  conversationId: number;
  onClose: () => void;
};

// Value is hours (as a string, for the <select>); "" means no expiry.
const TTL_OPTIONS = [
  { label: "Never expires", value: "" },
  { label: "24 hours", value: "24" },
  { label: "7 days", value: "168" },
  { label: "30 days", value: "720" },
];

function shareUrl(token: string): string {
  return `${window.location.origin}/shared/${token}`;
}

export function Share({ apiBase, getHeaders, conversationId, onClose }: Props) {
  const [status, setStatus] = useState<ShareStatus | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [copied, setCopied] = useState(false);
  const [ttlHours, setTtlHours] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/conversations/${conversationId}/share`, {
          headers: getHeaders(),
        });
        if (!res.ok) throw new Error(`Failed to load share status (${res.status})`);
        const view = (await res.json()) as ShareStatus;
        if (!cancelled) {
          setStatus(view);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load share status");
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

  async function createLink() {
    setLoading(true);
    try {
      const body: { ttl_hours?: number } = {};
      if (ttlHours) body.ttl_hours = Number(ttlHours);
      const res = await fetch(`${apiBase}/v1/conversations/${conversationId}/share`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`Failed to create share link (${res.status})`);
      const view = (await res.json()) as ShareStatus;
      setStatus(view);
      setError("");
      setCopied(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create share link");
    } finally {
      setLoading(false);
    }
  }

  async function revokeLink() {
    setLoading(true);
    try {
      const res = await fetch(`${apiBase}/v1/conversations/${conversationId}/share`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (!res.ok) throw new Error(`Failed to revoke share link (${res.status})`);
      const view = (await res.json()) as ShareStatus;
      setStatus(view);
      setError("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to revoke share link");
    } finally {
      setLoading(false);
    }
  }

  async function copyLink() {
    if (!status?.token) return;
    try {
      await navigator.clipboard.writeText(shareUrl(status.token));
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
        aria-label="Share conversation"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>🔗 Share</h2>
          <button className="link-button" onClick={onClose} aria-label="Close share">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Anyone with this link can view a read-only snapshot of this conversation —
          no account or API token needed. They can't ask follow-up questions, and
          they never see your spend, notes, or which model answered.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        {loading && status === null ? (
          <p className="settings-loading">Loading…</p>
        ) : status?.active && status.token ? (
          <div className="share-active">
            <div className="share-link-row">
              <input
                type="text"
                readOnly
                value={shareUrl(status.token)}
                aria-label="Share link"
                onFocus={(event) => event.target.select()}
              />
              <button type="button" className="secondary-button" onClick={() => void copyLink()}>
                {copied ? "✓ Copied" : "📋 Copy"}
              </button>
            </div>
            <p className="settings-readonly">
              {status.expires_at ? `Expires ${status.expires_at} UTC.` : "Never expires."}
            </p>
            <button
              type="button"
              className="secondary-button"
              onClick={() => void revokeLink()}
              disabled={loading}
            >
              Revoke link
            </button>
          </div>
        ) : (
          <div className="share-inactive">
            <label className="share-ttl-label">
              Link expiry
              <select
                value={ttlHours}
                onChange={(event) => setTtlHours(event.target.value)}
                aria-label="Link expiry"
              >
                {TTL_OPTIONS.map((option) => (
                  <option key={option.label} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="secondary-button"
              onClick={() => void createLink()}
              disabled={loading}
            >
              Create share link
            </button>
          </div>
        )}

        <footer className="settings-footer">
          <button className="secondary-button" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}
