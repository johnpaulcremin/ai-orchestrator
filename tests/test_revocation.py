"""JWT revocation (app/revocation.py) and the logout/refresh flows built on
it (app/routers/auth.py, app/security.py).

The store is DB-backed: a revocation survives a process restart and binds
every worker sharing the database file — the gap the app's own self-critique
correctly flagged when this state was two in-process dicts. The
restart-simulation tests below are the discriminating ones: they reload the
module (resetting any module-level state it might have) and assert the
revocation still holds, which the in-memory implementation genuinely fails.
"""

from __future__ import annotations

import importlib
import sqlite3
import time
from pathlib import Path

import pytest

from app import revocation

JWT_SECRET = "test-secret-key"


def _enable_jwt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    monkeypatch.delenv("API_AUTH_TOKEN", raising=False)


def _login(client) -> str:
    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "supersecret"}
    )
    return client.post(
        "/v1/auth/login", json={"username": "alice", "password": "supersecret"}
    ).json()["access_token"]


# --- revocation store unit ---------------------------------------------------
# All take db_path: the store lives in the database now, so a unit test needs
# a schema-initialised file the same way any other DB-touching unit test does.


def test_store_revoke_and_check(db_path: Path) -> None:
    future = int(time.time()) + 60
    assert revocation.is_revoked("jti-1") is False
    revocation.revoke("jti-1", future)
    assert revocation.is_revoked("jti-1") is True


def test_store_ignores_already_expired(db_path: Path) -> None:
    past = int(time.time()) - 5
    revocation.revoke("old", past)
    # Expired on its own, so it reads as not-revoked.
    assert revocation.is_revoked("old") is False


def test_store_empty_jti_is_noop(db_path: Path) -> None:
    revocation.revoke("", int(time.time()) + 60)
    assert revocation.is_revoked("") is False


def test_store_exp_boundary_is_strict(db_path: Path) -> None:
    # is_revoked() alone is strict: at now == exp it still reports revoked.
    # In practice PyJWT's own decode already rejects a token at exp <= now,
    # so this exact boundary is never the deciding factor for a real
    # request — this only pins is_revoked()'s own behavior in isolation.
    now = int(time.time())
    revocation.revoke("boundary", now)  # exp == now
    assert revocation.is_revoked("boundary") is True


def test_user_epoch_bump(db_path: Path) -> None:
    assert revocation.user_epoch("u") == 0
    assert revocation.bump_user_epoch("u") == 1
    assert revocation.user_epoch("u") == 1
    assert revocation.user_epoch("other") == 0


def test_epoch_bumps_accumulate(db_path: Path) -> None:
    assert revocation.bump_user_epoch("u") == 1
    assert revocation.bump_user_epoch("u") == 2
    assert revocation.user_epoch("u") == 2


def test_expired_entries_are_pruned_on_the_next_revoke(db_path: Path) -> None:
    """The lazy-prune contract, now against the table: a revoke sweeps
    entries already past their own expiry, so the table cannot grow without
    bound."""
    revocation.revoke("short-lived", int(time.time()) - 10)
    revocation.revoke("fresh", int(time.time()) + 60)
    with sqlite3.connect(db_path) as conn:
        jtis = {row[0] for row in conn.execute("SELECT jti FROM revoked_tokens")}
    assert jtis == {"fresh"}


# --- persistence: the reason this store exists --------------------------------


def test_revocation_state_lives_in_the_database_not_the_process(
    db_path: Path,
) -> None:
    """The design, pinned: no module-level dicts to lose. The in-memory
    implementation kept _revoked/_user_epoch maps; their absence is what
    makes every other test in this section trivially true."""
    revocation.revoke("jti-p", int(time.time()) + 60)
    revocation.bump_user_epoch("alice")
    assert not hasattr(revocation, "_revoked")
    assert not hasattr(revocation, "_user_epoch")
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM revoked_tokens").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM user_epochs").fetchone()[0] == 1


def test_revocation_survives_a_module_reload(db_path: Path) -> None:
    """A process restart, simulated the honest way: importlib.reload
    re-executes the module, which resets any module-level state — exactly
    what killed the in-memory version's revocations."""
    revocation.revoke("jti-r", int(time.time()) + 60)
    revocation.bump_user_epoch("alice")
    importlib.reload(revocation)
    assert revocation.is_revoked("jti-r") is True
    assert revocation.user_epoch("alice") == 1


def test_logout_survives_a_simulated_restart(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: log out, 'restart', and the dead token stays dead. The
    in-memory implementation fails this at the reload line."""
    _enable_jwt(monkeypatch)
    token = _login(client)
    auth = {"Authorization": f"Bearer {token}"}
    assert client.get("/v1/conversations", headers=auth).status_code == 200

    assert client.post("/v1/auth/logout", headers=auth).status_code == 200
    importlib.reload(revocation)

    assert client.get("/v1/conversations", headers=auth).status_code == 401


# --- the refresh-vs-logout race (found by adversarial review) -----------------


def test_a_logout_landing_mid_rotate_leaves_the_fresh_token_dead(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TOCTOU, made deterministic. Refresh used to validate the old token
    (epoch read #1) and mint via a SECOND independent epoch read, so a logout
    committing between the two produced a fresh token embedding the POST-bump
    epoch — immortal against "log out everywhere". rotate_access_token now
    carries the validated claim into the mint, so the same interleaving mints
    a token that is dead on arrival.

    The interleaving is simulated by making the validation-time epoch read
    see the pre-bump value while the database already holds the post-bump one
    — exactly what a bump committing after the check produces.
    """
    from app import security

    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    old_token = security.create_access_token("alice")  # epoch claim 0

    # Capture BEFORE patching: security.revocation IS the revocation module,
    # so reading the attribute after the patch would just hand back the stub.
    real_user_epoch = revocation.user_epoch

    # The logout commits: DB epoch is now 1...
    revocation.bump_user_epoch("alice")

    # ...but the FIRST epoch read (rotate's validation check) sees the stale
    # pre-bump value, and any LATER read sees the truth — the precise
    # interleaving of a bump committing between check and mint. This stub is
    # what discriminates old from new: the old shape re-read the epoch at
    # mint time (a second call, here returning the real post-bump 1) and so
    # minted an immortal token; the fix never makes that second call.
    calls = {"n": 0}

    def racing_user_epoch(username: str) -> int:
        calls["n"] += 1
        return 0 if calls["n"] == 1 else real_user_epoch(username)

    monkeypatch.setattr(security.revocation, "user_epoch", racing_user_epoch)
    fresh = security.rotate_access_token(old_token)
    assert fresh is not None  # the race happened: rotation went through

    # Un-patch: the world now sees the real (post-bump) epoch.
    monkeypatch.setattr(security.revocation, "user_epoch", real_user_epoch)

    # The OLD implementation minted with a fresh epoch read (1) and this
    # passed forever. The fix mints with the validated claim (0), so the
    # escaped token fails the very next check.
    assert security.subject_from_token(fresh) is None


def test_ordinary_rotation_still_works_end_to_end(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The carried-claim mint must not break the normal case: refresh, then
    use the fresh token, then log out and see BOTH die."""
    _enable_jwt(monkeypatch)
    token = _login(client)
    fresh = client.post(
        "/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"}
    ).json()["access_token"]
    auth = {"Authorization": f"Bearer {fresh}"}
    assert client.get("/v1/conversations", headers=auth).status_code == 200
    assert client.post("/v1/auth/logout", headers=auth).status_code == 200
    assert client.get("/v1/conversations", headers=auth).status_code == 401


def test_maintenance_sweeps_expired_revocations(db_path: Path) -> None:
    """The periodic backstop to the lazy on-revoke prune: a burst of
    revocations with no later revoke otherwise lingers until one happens."""
    from app import database, retention

    revocation.revoke("burst-1", int(time.time()) - 100)
    revocation.revoke("burst-2", int(time.time()) - 50)
    revocation.revoke("alive", int(time.time()) + 3600)
    # The two expired rows survived their own inserts' lazy sweeps only in
    # part; make the state explicit: re-insert one expired row directly so
    # the sweep provably has work to do regardless of insert order.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO revoked_tokens (jti, expires_at) VALUES (?, ?)",
            ("stale-direct", int(time.time()) - 10),
        )
    counts = retention.rollup_and_prune()
    assert counts["revoked_tokens"] >= 1
    with sqlite3.connect(db_path) as conn:
        jtis = {row[0] for row in conn.execute("SELECT jti FROM revoked_tokens")}
    assert jtis == {"alive"}
    assert database.revoked_token_present("alive", int(time.time())) is True


# --- logout ------------------------------------------------------------------


def test_logout_revokes_api_access_and_ownership(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)
    auth = {"Authorization": f"Bearer {token}"}

    # The token works before logout.
    assert client.get("/v1/conversations", headers=auth).status_code == 200
    assert client.get("/v1/auth/me", headers=auth).json()["username"] == "alice"

    out = client.post("/v1/auth/logout", headers=auth)
    assert out.status_code == 200
    assert out.json()["status"] == "logged_out"

    # After logout the same token is rejected everywhere (access AND ownership).
    assert client.get("/v1/conversations", headers=auth).status_code == 401


def test_logout_without_token_is_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    assert client.post("/v1/auth/logout").status_code == 401
    assert (
        client.post(
            "/v1/auth/logout", headers={"Authorization": "Bearer garbage"}
        ).status_code
        == 401
    )


def test_logout_requires_jwt_enabled(client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JWT_SECRET", raising=False)
    assert client.post("/v1/auth/logout").status_code == 400


# --- refresh -----------------------------------------------------------------


def test_refresh_rotates_the_old_token(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)

    res = client.post("/v1/auth/refresh", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    fresh = res.json()["access_token"]
    assert fresh and fresh != token

    # Rotation: the OLD token stops working, the new one works.
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {token}"}
        ).status_code
        == 401
    )
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {fresh}"}
        ).status_code
        == 200
    )


def test_logout_revokes_all_of_a_users_sessions(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The security fix: logging out one session kills every token the user holds,
    # including a token that was refreshed onto a fresh jti (so a laundered
    # session can't outlive a logout).
    _enable_jwt(monkeypatch)
    client.post(
        "/v1/auth/register", json={"username": "alice", "password": "supersecret"}
    )

    def login() -> str:
        return client.post(
            "/v1/auth/login", json={"username": "alice", "password": "supersecret"}
        ).json()["access_token"]

    token_a = login()
    token_b = login()  # a second, independent session
    for t in (token_a, token_b):
        assert (
            client.get(
                "/v1/conversations", headers={"Authorization": f"Bearer {t}"}
            ).status_code
            == 200
        )

    # Log out via ONE session...
    assert (
        client.post(
            "/v1/auth/logout", headers={"Authorization": f"Bearer {token_a}"}
        ).status_code
        == 200
    )

    # ...and BOTH sessions are now dead.
    for t in (token_a, token_b):
        assert (
            client.get(
                "/v1/conversations", headers={"Authorization": f"Bearer {t}"}
            ).status_code
            == 401
        )

    # A fresh login after the logout works normally.
    assert (
        client.get(
            "/v1/conversations", headers={"Authorization": f"Bearer {login()}"}
        ).status_code
        == 200
    )


def test_refresh_rejects_a_revoked_token(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_jwt(monkeypatch)
    token = _login(client)
    auth = {"Authorization": f"Bearer {token}"}

    client.post("/v1/auth/logout", headers=auth)
    assert client.post("/v1/auth/refresh", headers=auth).status_code == 401


def test_refresh_without_token_is_401(client, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_jwt(monkeypatch)
    assert client.post("/v1/auth/refresh").status_code == 401
