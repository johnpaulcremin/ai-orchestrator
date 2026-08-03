import { useEffect, useRef, useState } from "react";
import { authFailureMessage } from "./format";
import { useModalFocus } from "./useModalFocus";

type LibraryDocument = {
  id: number;
  filename: string;
  mime_type: string;
  size_bytes: number;
  chunk_count: number;
  created_at: string;
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  jwtEnabled: boolean;
};

const ACCEPTED_MIMES = new Set(["application/pdf", "text/plain"]);
const MAX_FILE_BYTES = 10 * 1024 * 1024;

function isDocumentFile(file: File): boolean {
  if (ACCEPTED_MIMES.has(file.type)) {
    return true;
  }
  const name = file.name.toLowerCase();
  return file.type === "" && (name.endsWith(".txt") || name.endsWith(".md"));
}

function readAsDataUrl(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = () => resolve(typeof reader.result === "string" ? reader.result : null);
    reader.onerror = () => resolve(null);
    reader.readAsDataURL(file);
  });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function Library({ apiBase, getHeaders, onClose, jwtEnabled }: Props) {
  const [items, setItems] = useState<LibraryDocument[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [uploading, setUploading] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [enabled, setEnabled] = useState(true);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [documentsRes, settingsRes] = await Promise.all([
          fetch(`${apiBase}/v1/library/documents`, { headers: getHeaders() }),
          fetch(`${apiBase}/v1/settings`, { headers: getHeaders() }),
        ]);
        if (!documentsRes.ok) {
          throw new Error(
            documentsRes.status === 401
              ? authFailureMessage(jwtEnabled)
              : `Failed to load library documents (${documentsRes.status})`,
          );
        }
        const documents = (await documentsRes.json()) as LibraryDocument[];
        let flagEnabled = true;
        if (settingsRes.ok) {
          const settings = (await settingsRes.json()) as {
            features?: { key: string; effective_enabled: boolean }[];
          };
          const flag = settings.features?.find((f) => f.key === "RAG_LIBRARY");
          if (flag) flagEnabled = flag.effective_enabled;
        }
        if (!cancelled) {
          setItems(documents);
          setEnabled(flagEnabled);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load library");
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

  async function uploadFiles(fileList: FileList | null) {
    if (!fileList || fileList.length === 0) return;
    const files = Array.from(fileList).filter(isDocumentFile);
    if (files.length === 0) {
      setError("Only PDF and plain text files are supported.");
      return;
    }
    setUploading(true);
    setError("");
    try {
      for (const file of files) {
        if (file.size > MAX_FILE_BYTES) {
          setError(`${file.name} is too large (max 10MB).`);
          continue;
        }
        const dataUrl = await readAsDataUrl(file);
        if (!dataUrl) {
          setError(`Failed to read ${file.name}.`);
          continue;
        }
        const mime = ACCEPTED_MIMES.has(file.type) ? file.type : "text/plain";
        const normalized = dataUrl.replace(/^data:[^;]*;base64,/, `data:${mime};base64,`);
        const res = await fetch(`${apiBase}/v1/library/documents`, {
          method: "POST",
          headers: getHeaders({ "Content-Type": "application/json" }),
          body: JSON.stringify({ filename: file.name, data: normalized }),
        });
        if (!res.ok) {
          if (res.status === 401) throw new Error(authFailureMessage(jwtEnabled));
          const detail = await res.json().catch(() => null);
          throw new Error(
            (detail && typeof detail.detail === "string" && detail.detail) ||
              `Failed to upload ${file.name} (${res.status})`,
          );
        }
        const created = (await res.json()) as LibraryDocument;
        setItems((current) => (current ? [created, ...current] : [created]));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to upload document");
    } finally {
      setUploading(false);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  async function seedAppDocs() {
    setSeeding(true);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/library/seed-app-docs`, {
        method: "POST",
        headers: getHeaders(),
      });
      if (!res.ok) {
        if (res.status === 401) throw new Error(authFailureMessage(jwtEnabled));
        const detail = await res.json().catch(() => null);
        throw new Error(
          (detail && typeof detail.detail === "string" && detail.detail) ||
            `Failed to seed app docs (${res.status})`,
        );
      }
      const created = (await res.json()) as LibraryDocument[];
      if (created.length > 0) {
        setItems((current) => (current ? [...created, ...current] : created));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to seed app docs");
    } finally {
      setSeeding(false);
    }
  }

  async function deleteDocument(id: number) {
    setBusyId(id);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/library/documents/${id}`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (!res.ok) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(jwtEnabled)
            : `Failed to delete document (${res.status})`,
        );
      }
      setItems((current) => (current ? current.filter((entry) => entry.id !== id) : current));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete document");
    } finally {
      setBusyId(null);
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

  const totalSize = (items ?? []).reduce((sum, item) => sum + item.size_bytes, 0);

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
        aria-label="Document library"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Document library</h2>
          <button className="link-button" onClick={onClose} aria-label="Close document library">
            ✕
          </button>
        </header>

        <p className="settings-intro">
          Reference documents the model can automatically draw on across every conversation —
          distinct from a per-message attachment, which only exists for that one turn.
        </p>

        {!enabled ? (
          <p className="settings-readonly" role="status">
            The document library feature is currently off. Turn on "Document library (RAG)" in
            Settings to let the model actually use what you upload here.
          </p>
        ) : null}

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="library-upload-form">
          <input
            ref={fileInputRef}
            type="file"
            accept=".pdf,.txt,.md,application/pdf,text/plain"
            multiple
            aria-label="Upload document"
            onChange={(event) => void uploadFiles(event.target.files)}
            disabled={uploading}
          />
          {uploading ? <span className="settings-loading">Uploading…</span> : null}
        </div>

        <div className="library-seed-form">
          <button
            type="button"
            className="link-button"
            onClick={() => void seedAppDocs()}
            disabled={seeding}
          >
            {seeding ? "Seeding…" : "📚 Seed library with app docs"}
          </button>
          <p className="settings-hint">
            Ingests this app's own documentation (routing, configuration, features, API
            reference) so a "how does routing work?" style question can retrieve the real docs.
          </p>
        </div>

        {loading && !items ? (
          <p className="settings-loading">Loading…</p>
        ) : items ? (
          items.length === 0 ? (
            <p className="settings-readonly">No documents uploaded yet — add one above.</p>
          ) : (
            <>
              <div className="library-list">
                {items.map((item) => (
                  <div className="library-row" key={item.id}>
                    <div className="library-row-main">
                      <span className="library-filename">{item.filename}</span>
                      <p className="library-meta">
                        {formatSize(item.size_bytes)} · {item.chunk_count} chunk
                        {item.chunk_count === 1 ? "" : "s"}
                      </p>
                    </div>
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => void deleteDocument(item.id)}
                      disabled={busyId === item.id}
                      aria-label={`Delete ${item.filename}`}
                      title="Delete this document"
                    >
                      🗑️
                    </button>
                  </div>
                ))}
              </div>
              <p className="library-total">Total size: {formatSize(totalSize)}</p>
            </>
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
