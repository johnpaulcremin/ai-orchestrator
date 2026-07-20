from __future__ import annotations

import threading
import time

# In-memory JWT revocation state. Two mechanisms:
#   * _revoked: jti -> unix expiry, for revoking one specific token (refresh
#     rotation). Entries are pruned lazily once the token would have expired.
#   * _user_epoch: username -> counter. A token embeds the user's epoch at issue
#     time; bumping it (on logout) invalidates EVERY token issued to that user so
#     far, including any that were refreshed onto a fresh jti — something per-jti
#     revocation alone cannot do.
# Both are per-process and cleared on restart; the short token TTL bounds the
# exposure of a still-live process. For a multi-worker / multi-host deployment,
# back this with a shared store (e.g. Redis) so a logout is seen by every worker.

_revoked: dict[str, int] = {}
_user_epoch: dict[str, int] = {}
_lock = threading.Lock()


def _prune_locked(now: int) -> None:
    # Strict `<` so an entry stays while jose still accepts the token: jose
    # treats now == exp as not-yet-expired, so `<=` would drop the entry a second
    # early and briefly honour a revoked token at its exp boundary.
    for jti in [j for j, exp in _revoked.items() if exp < now]:
        del _revoked[jti]


def revoke(jti: str, expires_at: int) -> None:
    """Revoke a single token id until the moment it would have expired anyway."""
    if not jti:
        return
    now = int(time.time())
    with _lock:
        _prune_locked(now)
        _revoked[str(jti)] = int(expires_at)


def is_revoked(jti: str) -> bool:
    if not jti:
        return False
    now = int(time.time())
    with _lock:
        exp = _revoked.get(str(jti))
        if exp is None:
            return False
        if exp < now:
            # Already expired on its own; drop it and treat as not-revoked.
            del _revoked[str(jti)]
            return False
        return True


def user_epoch(username: str) -> int:
    """The user's current session epoch (0 until they first log out)."""
    if not username:
        return 0
    with _lock:
        return _user_epoch.get(str(username), 0)


def bump_user_epoch(username: str) -> int:
    """Invalidate every token issued to this user so far; returns the new epoch."""
    if not username:
        return 0
    with _lock:
        nxt = _user_epoch.get(str(username), 0) + 1
        _user_epoch[str(username)] = nxt
        return nxt


def clear() -> None:
    """Empty the revocation state (used to keep tests hermetic)."""
    with _lock:
        _revoked.clear()
        _user_epoch.clear()
