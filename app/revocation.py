"""Persisted JWT revocation: a logout or token rotation is written to the
database, so it survives a restart and binds every worker sharing the file.

Until v0.3.x this state was two in-process dicts, and the module's own
docstring named the consequence: a restart un-revoked, and a multi-worker
deployment revoked on one worker only. The app's self-critique then flagged
exactly that ("JWT revocation is in-memory, so revokes don't survive
restarts") — a finding it could only make because the limitation was stated
here, and one that was true. The state now lives in the same SQLite file as
everything else (see database.py's revoked_tokens/user_epochs tables), which
is this app's one shared store — reaching for Redis to persist two tiny
tables would add a second infrastructure dependency to an app whose whole
storage design is "one local file". The honest limit that REMAINS: multiple
hosts that do not share the database file still each see only their own
revocations, same as every other table here.

Two shapes, unchanged in meaning from the in-memory version:

Per-jti revocation (revoke/is_revoked) retires a single token — what refresh
rotation needs, so a leaked token cannot be replayed after the user has
traded it in. Entries are pruned lazily once the token would have expired
anyway, so the table cannot grow without bound.

Per-user epochs (user_epoch/bump_user_epoch) are the "log out everywhere"
mechanism. A token embeds its user's epoch at issue time; bumping the
counter invalidates every token issued to that user so far, including ones
that were refreshed onto a fresh jti — something a per-jti list cannot
express, since it never saw those ids.

Deliberately no in-memory cache in front of the reads: a negative cache
("this jti is fine") is exactly the entry another worker's logout must be
able to falsify, and a per-request pair of point SELECTs on a local SQLite
file costs microseconds against the model call it gates. No module-level
state also means there is nothing to keep test-hermetic between tests —
each test's throwaway DATABASE_PATH isolates this the same way it isolates
every other table.

A broken database makes these raise, and that propagates to a 500 rather
than being caught: failing OPEN (treat as not-revoked) is the one wrong
answer for a revocation check, and an app whose database is down cannot
serve the request anyway.
"""

from __future__ import annotations

import time

from . import database


def revoke(jti: str, expires_at: int) -> None:
    """Revoke a single token id until the moment it would have expired anyway."""
    if not jti:
        return
    database.revoked_token_add(str(jti), int(expires_at), int(time.time()))


def is_revoked(jti: str) -> bool:
    """Whether this token id has been revoked and not yet self-expired.

    The expiry boundary is strict the same way the in-memory version's was
    (an entry at exactly its expiry second still reads revoked): PyJWT's own
    decode already rejects a token at exp <= now, so this boundary is never
    the thing standing between a revoked token and acceptance — see
    database.revoked_token_present.
    """
    if not jti:
        return False
    return database.revoked_token_present(str(jti), int(time.time()))


def user_epoch(username: str) -> int:
    """The user's current session epoch (0 until they first log out)."""
    if not username:
        return 0
    return database.user_epoch_get(str(username))


def bump_user_epoch(username: str) -> int:
    """Invalidate every token issued to this user so far; returns the new epoch."""
    if not username:
        return 0
    return database.user_epoch_bump(str(username))
