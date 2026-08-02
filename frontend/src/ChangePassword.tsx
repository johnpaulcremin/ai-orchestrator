import { useRef, useState } from "react";
import { useModalFocus } from "./useModalFocus";

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
  username: string;
  onChanged: () => void;
  onSignOut: () => void;
};

// Blocks the app for an account flagged must_change_password (admin-created
// or reset) until it sets its own password — no onClose/Escape-to-dismiss,
// unlike every other panel here, since this step isn't optional.
export function ChangePassword({ apiBase, getHeaders, username, onChanged, onSignOut }: Props) {
  const [currentPassword, setCurrentPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const dialogRef = useRef<HTMLDivElement | null>(null);
  useModalFocus(dialogRef);

  async function submit() {
    setError("");
    if (newPassword.length < 8) {
      setError("New password must be at least 8 characters.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setError("New password and confirmation don't match.");
      return;
    }
    setBusy(true);
    try {
      const res = await fetch(`${apiBase}/v1/auth/change-password`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? "Failed to change password");
      }
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to change password");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="settings-overlay" role="presentation">
      <div
        ref={dialogRef}
        className="settings-modal change-password-modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        aria-label="Set a new password"
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            void submit();
          }
        }}
      >
        <header className="settings-header">
          <h2>Set a new password</h2>
        </header>

        <p className="settings-intro">
          Signed in as <strong>{username}</strong> with a temporary password. Choose your own
          password before continuing.
        </p>

        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}

        <div className="setting-row">
          <label htmlFor="change-password-current">Temporary / current password</label>
          <input
            id="change-password-current"
            type="password"
            value={currentPassword}
            autoComplete="current-password"
            disabled={busy}
            onChange={(event) => setCurrentPassword(event.target.value)}
          />
        </div>
        <div className="setting-row">
          <label htmlFor="change-password-new">New password</label>
          <input
            id="change-password-new"
            type="password"
            value={newPassword}
            autoComplete="new-password"
            minLength={8}
            disabled={busy}
            onChange={(event) => setNewPassword(event.target.value)}
          />
        </div>
        <div className="setting-row">
          <label htmlFor="change-password-confirm">Confirm new password</label>
          <input
            id="change-password-confirm"
            type="password"
            value={confirmPassword}
            autoComplete="new-password"
            minLength={8}
            disabled={busy}
            onChange={(event) => setConfirmPassword(event.target.value)}
          />
        </div>

        <footer className="settings-footer">
          <button type="button" className="link-button" onClick={onSignOut} disabled={busy}>
            Sign out instead
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => void submit()}
            disabled={busy || !currentPassword || !newPassword || !confirmPassword}
          >
            {busy ? "Saving…" : "Save password"}
          </button>
        </footer>
      </div>
    </div>
  );
}
