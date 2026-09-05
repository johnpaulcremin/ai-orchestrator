import { useEffect, useRef, useState } from "react";
import { Pencil, Trash2, X } from "lucide-react";
import { Button } from "./Button";
import { authFailureMessage } from "./format";
import { useModalFocus } from "./useModalFocus";

type Template = {
  id: number;
  name: string;
  content: string;
  created_at: string;
  updated_at: string;
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  onClose: () => void;
  onInsert: (content: string) => void;
  jwtEnabled: boolean;
};

export function Templates({ apiBase, getHeaders, onClose, onInsert, jwtEnabled }: Props) {
  const [items, setItems] = useState<Template[] | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [newContent, setNewContent] = useState("");
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editContent, setEditContent] = useState("");

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/templates`, { headers: getHeaders() });
        if (!res.ok) {
          throw new Error(
            res.status === 401
              ? authFailureMessage(jwtEnabled)
              : `Failed to load templates (${res.status})`,
          );
        }
        const view = (await res.json()) as Template[];
        if (!cancelled) {
          setItems(view);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load templates");
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

  async function createTemplate() {
    const name = newName.trim();
    const content = newContent.trim();
    if (!name || !content) return;
    setCreating(true);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/templates`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ name, content }),
      });
      if (!res.ok) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(jwtEnabled)
            : `Failed to save template (${res.status})`,
        );
      }
      const created = (await res.json()) as Template;
      setItems((current) => (current ? [created, ...current] : [created]));
      setNewName("");
      setNewContent("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to save template");
    } finally {
      setCreating(false);
    }
  }

  function startEdit(item: Template) {
    setEditingId(item.id);
    setEditName(item.name);
    setEditContent(item.content);
  }

  async function saveEdit(id: number) {
    const name = editName.trim();
    const content = editContent.trim();
    if (!name || !content) return;
    setBusyId(id);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/templates/${id}`, {
        method: "PATCH",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ name, content }),
      });
      if (!res.ok) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(jwtEnabled)
            : `Failed to update template (${res.status})`,
        );
      }
      const updated = (await res.json()) as Template;
      setItems((current) =>
        current ? current.map((entry) => (entry.id === id ? updated : entry)) : current,
      );
      setEditingId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update template");
    } finally {
      setBusyId(null);
    }
  }

  async function deleteTemplate(id: number) {
    setBusyId(id);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/templates/${id}`, {
        method: "DELETE",
        headers: getHeaders(),
      });
      if (!res.ok) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(jwtEnabled)
            : `Failed to delete template (${res.status})`,
        );
      }
      setItems((current) => (current ? current.filter((entry) => entry.id !== id) : current));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to delete template");
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
        aria-label="Templates"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Templates</h2>
          <Button
            iconOnly
            size="sm"
            variant="ghost"
            onClick={onClose}
            aria-label="Close templates"
            icon={<X size={18} />}
          />
        </header>

        <p className="settings-intro">
          Reusable prompt snippets you can insert into any conversation's composer. Different from
          a conversation's own Custom Instructions, which apply automatically to just that one
          conversation.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="template-new-form">
          <input
            type="text"
            className="template-name-input"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
            placeholder="Template name…"
            aria-label="New template name"
            maxLength={80}
          />
          <textarea
            className="template-content-input"
            value={newContent}
            onChange={(event) => setNewContent(event.target.value)}
            placeholder="Template content…"
            aria-label="New template content"
            maxLength={4000}
            rows={3}
          />
          <Button
            type="button"
            onClick={() => void createTemplate()}
            disabled={creating || !newName.trim() || !newContent.trim()}
          >
            {creating ? "Saving…" : "+ Save template"}
          </Button>
        </div>

        {loading && !items ? (
          <p className="settings-loading">Loading…</p>
        ) : items ? (
          items.length === 0 ? (
            <p className="settings-readonly">No saved templates yet — add one above.</p>
          ) : (
            <div className="template-list">
              {items.map((item) =>
                editingId === item.id ? (
                  <div className="template-row template-row-editing" key={item.id}>
                    <input
                      type="text"
                      className="template-name-input"
                      value={editName}
                      onChange={(event) => setEditName(event.target.value)}
                      aria-label="Template name"
                      maxLength={80}
                    />
                    <textarea
                      className="template-content-input"
                      value={editContent}
                      onChange={(event) => setEditContent(event.target.value)}
                      aria-label="Template content"
                      maxLength={4000}
                      rows={3}
                    />
                    <div className="template-row-actions">
                      <Button
                        type="button"
                        onClick={() => void saveEdit(item.id)}
                        disabled={busyId === item.id || !editName.trim() || !editContent.trim()}
                      >
                        Save
                      </Button>
                      <Button
                        type="button"
                        onClick={() => setEditingId(null)}
                        disabled={busyId === item.id}
                      >
                        Cancel
                      </Button>
                    </div>
                  </div>
                ) : (
                  <div className="template-row" key={item.id}>
                    {/* Kept raw: .btn-sm's fixed 32px height would squash this two-line row. */}
                    <button
                      type="button"
                      className="template-row-main"
                      onClick={() => {
                        onInsert(item.content);
                        onClose();
                      }}
                      title="Insert into the composer"
                    >
                      <span className="template-name">{item.name}</span>
                      <p className="template-snippet">
                        {item.content.length > 160
                          ? `${item.content.slice(0, 160)}…`
                          : item.content}
                      </p>
                    </button>
                    <div className="template-row-actions">
                      <Button
                        iconOnly
                        size="sm"
                        variant="ghost"
                        type="button"
                        onClick={() => startEdit(item)}
                        aria-label={`Rename or edit ${item.name}`}
                        title="Rename or edit"
                        icon={<Pencil size={16} />}
                      />
                      <Button
                        iconOnly
                        size="sm"
                        variant="ghost"
                        type="button"
                        onClick={() => void deleteTemplate(item.id)}
                        disabled={busyId === item.id}
                        aria-label={`Delete ${item.name}`}
                        title="Delete this template"
                        icon={<Trash2 size={16} />}
                      />
                    </div>
                  </div>
                ),
              )}
            </div>
          )
        ) : null}

        <footer className="settings-footer">
          <Button onClick={onClose}>
            Done
          </Button>
        </footer>
      </div>
    </div>
  );
}
