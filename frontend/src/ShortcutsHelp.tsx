import { useEffect, useRef } from "react";
import { useModalFocus } from "./useModalFocus";

type Props = {
  isMac: boolean;
  onClose: () => void;
};

export function ShortcutsHelp({ isMac, onClose }: Props) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  const shortcuts = [
    { keys: isMac ? "⌘K" : "Ctrl+K", description: "Jump into conversation search" },
    { keys: isMac ? "⌥N" : "Alt+N", description: "Start a new conversation and focus the composer" },
    { keys: isMac ? "⌥B" : "Alt+B", description: "Open Bookmarks" },
    { keys: "Escape", description: "Close the open panel, cancel an edit, or clear an active search" },
    { keys: "?", description: "Show this shortcuts list" },
  ];

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
        aria-label="Keyboard shortcuts"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="settings-header">
          <h2>Keyboard shortcuts</h2>
          <button className="link-button" onClick={onClose} aria-label="Close keyboard shortcuts">
            ✕
          </button>
        </header>

        <table className="shortcuts-table">
          <tbody>
            {shortcuts.map((shortcut) => (
              <tr key={shortcut.keys}>
                <td>
                  <kbd>{shortcut.keys}</kbd>
                </td>
                <td>{shortcut.description}</td>
              </tr>
            ))}
          </tbody>
        </table>

        <footer className="settings-footer">
          <button className="secondary-button" onClick={onClose}>
            Done
          </button>
        </footer>
      </div>
    </div>
  );
}
