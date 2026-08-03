import { useEffect, useState } from "react";
import { authFailureMessage } from "./format";

export type AdminUser = {
  id: number;
  username: string;
  created_at: string;
  is_active: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
};

type Props = {
  apiBase: string;
  getHeaders: (extra?: Record<string, string>) => Record<string, string>;
};

// Always JWT mode, not a prop: this section only ever renders for an admin
// account (see Settings.tsx's `data.is_admin` gate), and being an admin
// requires JWT auth to be enabled in the first place (app/auth.py's
// is_admin()) — there is no static-token-only path that reaches here.
const USERS_JWT_ENABLED = true;

// Admin-only account management, embedded as a section inside Settings
// (never its own modal) — see app/routers/users.py for the endpoints this
// drives. Every mutation reloads the list from the server rather than
// patching local state, so is_active/must_change_password/last_login_at
// always reflect what the backend actually persisted.
export function Users({ apiBase, getHeaders }: Props) {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [busyUsername, setBusyUsername] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);

  const [newUsername, setNewUsername] = useState("");
  const [creating, setCreating] = useState(false);
  // The one-time reveal after a create/reset — cleared on "I've saved it".
  const [revealedPassword, setRevealedPassword] = useState<{
    username: string;
    password: string;
    copied: boolean;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const res = await fetch(`${apiBase}/v1/users`, { headers: getHeaders() });
        if (!res.ok) {
          throw new Error(
            res.status === 401
              ? authFailureMessage(USERS_JWT_ENABLED)
              : `Failed to load users (${res.status})`,
          );
        }
        const list = (await res.json()) as AdminUser[];
        if (!cancelled) {
          setUsers(list);
          setError("");
          setLoading(false);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load users");
          setLoading(false);
        }
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reloadNonce]);

  async function createUser() {
    const username = newUsername.trim();
    if (!username) return;
    setCreating(true);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/users`, {
        method: "POST",
        headers: getHeaders({ "Content-Type": "application/json" }),
        body: JSON.stringify({ username }),
      });
      const body = (await res.json().catch(() => ({}))) as {
        user?: AdminUser;
        temporary_password?: string;
        detail?: string;
      };
      if (!res.ok || !body.user || !body.temporary_password) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(USERS_JWT_ENABLED)
            : (body.detail ?? `Failed to create user (${res.status})`),
        );
      }
      setRevealedPassword({
        username: body.user.username,
        password: body.temporary_password,
        copied: false,
      });
      setNewUsername("");
      setReloadNonce((n) => n + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create user");
    } finally {
      setCreating(false);
    }
  }

  async function resetPassword(username: string) {
    setBusyUsername(username);
    setError("");
    try {
      const res = await fetch(`${apiBase}/v1/users/${encodeURIComponent(username)}/reset-password`, {
        method: "POST",
        headers: getHeaders(),
      });
      const body = (await res.json().catch(() => ({}))) as {
        temporary_password?: string;
        detail?: string;
      };
      if (!res.ok || !body.temporary_password) {
        throw new Error(
          res.status === 401
            ? authFailureMessage(USERS_JWT_ENABLED)
            : (body.detail ?? `Failed to reset password (${res.status})`),
        );
      }
      setRevealedPassword({ username, password: body.temporary_password, copied: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to reset password");
    } finally {
      setBusyUsername(null);
    }
  }

  async function setActive(username: string, active: boolean) {
    setBusyUsername(username);
    setError("");
    try {
      const res = await fetch(
        `${apiBase}/v1/users/${encodeURIComponent(username)}/${active ? "reactivate" : "deactivate"}`,
        { method: "POST", headers: getHeaders() },
      );
      if (!res.ok) {
        if (res.status === 401) throw new Error(authFailureMessage(USERS_JWT_ENABLED));
        const body = (await res.json().catch(() => ({}))) as { detail?: string };
        throw new Error(body.detail ?? `Failed to update user (${res.status})`);
      }
      setReloadNonce((n) => n + 1);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to update user");
    } finally {
      setBusyUsername(null);
    }
  }

  async function copyPassword() {
    if (!revealedPassword) return;
    try {
      await navigator.clipboard.writeText(revealedPassword.password);
      setRevealedPassword({ ...revealedPassword, copied: true });
    } catch {
      // Clipboard access can fail (permissions, insecure context); the
      // password stays visible on-screen for a manual copy either way.
    }
  }

  return (
    <section className="settings-section users-section">
      <h3>Users</h3>
      <p className="settings-section-hint">
        Accounts on this deployment. New and reset accounts get a random temporary password,
        shown here exactly once — the account must set its own password on next sign-in.
      </p>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      {revealedPassword ? (
        <div className="users-reveal" role="alert">
          <p>
            Temporary password for <strong>{revealedPassword.username}</strong> — write this down
            now, it won't be shown again:
          </p>
          <div className="users-reveal-row">
            <code>{revealedPassword.password}</code>
            <button type="button" className="secondary-button" onClick={() => void copyPassword()}>
              {revealedPassword.copied ? "Copied!" : "Copy"}
            </button>
          </div>
          <button
            type="button"
            className="link-button"
            onClick={() => setRevealedPassword(null)}
          >
            I've saved it
          </button>
        </div>
      ) : null}

      <div className="users-add-row">
        <input
          type="text"
          value={newUsername}
          placeholder="New username"
          aria-label="New username"
          disabled={creating}
          onChange={(event) => setNewUsername(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              void createUser();
            }
          }}
        />
        <button
          type="button"
          className="secondary-button"
          onClick={() => void createUser()}
          disabled={creating || newUsername.trim().length < 3}
        >
          {creating ? "Adding…" : "Add user"}
        </button>
      </div>

      {loading ? (
        <p className="settings-loading">Loading…</p>
      ) : users.length === 0 ? (
        <p className="settings-readonly">No users yet.</p>
      ) : (
        <table className="users-table">
          <thead>
            <tr>
              <th>Username</th>
              <th>Created</th>
              <th>Status</th>
              <th>Last seen</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((user) => (
              <tr key={user.id}>
                <td>{user.username}</td>
                <td>{user.created_at}</td>
                <td>
                  {user.is_active ? "Active" : "Deactivated"}
                  {user.must_change_password ? " · must change password" : ""}
                </td>
                <td>{user.last_login_at ?? "never"}</td>
                <td className="users-row-actions">
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => void resetPassword(user.username)}
                    disabled={busyUsername === user.username}
                  >
                    Reset password
                  </button>
                  <button
                    type="button"
                    className="link-button"
                    onClick={() => void setActive(user.username, !user.is_active)}
                    disabled={busyUsername === user.username}
                  >
                    {user.is_active ? "Deactivate" : "Reactivate"}
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
