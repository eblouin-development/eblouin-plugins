"""End-to-end tests for the DEFAULT authentication path: server-side
sessions, over real HTTP against the hermetic client.

`tests/test_auth.py` and `tests/test_cookie_auth.py` cover the JWT paths
(bearer and refresh-cookie); this module covers the one a browser actually
gets, and specifically the properties that justify preferring it — a
logout that takes effect on the very next request, a role revocation that
lands immediately, a password reset that kills every device, and CSRF
enforced on every unsafe method rather than only on `/auth/*`.

**Every request in this file uses an explicit `https://testserver/...`
URL**, never a bare relative path — required, not stylistic, for the exact
reason `tests/test_cookie_auth.py`'s own docstring spells out: the session
and CSRF cookies are `secure=True`, and httpx's cookie jar (like a real
browser) refuses to re-attach a `Secure` cookie to a plain-`http` request,
so without the override every request after login would silently carry no
cookies at all.

Reuses `tests/test_auth.py`'s fixtures/helpers (`_make_auth_client`,
`_register_and_verify`, `_CapturingEmailSender`) rather than duplicating
that module's `make_client` -> bespoke-`Settings` -> email-sender-override
plumbing.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security.auth.stores import seed_admin
from app.models.session import Session
from app.models.user import User

from .test_auth import _CapturingEmailSender, _make_auth_client, _register_and_verify

_BASE = "https://testserver"
_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"
_ADMIN_EMAIL = "root@example.com"
_ADMIN_PASSWORD = "another correct horse battery staple"


@pytest.fixture()
def email_sender() -> _CapturingEmailSender:
    return _CapturingEmailSender()


@pytest.fixture()
def auth_client(make_client, email_sender: _CapturingEmailSender) -> TestClient:
    return _make_auth_client(make_client, email_sender)


def _login_session(client: TestClient, email: str = _EMAIL, password: str = _PASSWORD):
    """Logs in on the DEFAULT (session) path — no `X-Auth-Mode` header at
    all, which is the whole point — and returns the response. The client's
    cookie jar keeps `session_id`/`csrf_token` for subsequent requests."""
    response = client.post(f"{_BASE}/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    return response


def _csrf(client: TestClient) -> dict:
    """The double-submit header every unsafe-method request must echo,
    read back out of the (deliberately non-HttpOnly) CSRF cookie exactly
    as a real SPA reads it from `document.cookie`."""
    return {"X-CSRF-Token": client.cookies["csrf_token"]}


async def _seed_verified_admin(email: str, password: str) -> str:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        user = await seed_admin(session, email, password)
        result = await session.execute(select(User).where(User.id == uuid.UUID(user.id)))
        row = result.scalar_one()
        row.email_verified = True
        await session.commit()
        return user.id


async def _session_rows() -> list[Session]:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        result = await session.execute(select(Session))
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Login: session is the default, and hands the client no token
# ---------------------------------------------------------------------------


def test_login_defaults_to_session_mode(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    body = _login_session(auth_client).json()

    assert body["token_type"] == "session"
    # THE property: nothing token-shaped reaches the client. There is no
    # credential in the JS heap for an XSS payload to exfiltrate, because
    # the only credential is a cookie JS cannot read.
    assert body["access_token"] == ""
    assert body["refresh_token"] == ""


def test_login_sets_the_expected_session_cookie_flags(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    response = _login_session(auth_client)

    set_cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in set_cookies if c.startswith("session_id="))
    csrf_cookie = next(c for c in set_cookies if c.startswith("csrf_token="))

    # The session cookie must be unreadable to JS, HTTPS-only, and scoped
    # to the whole app (it authenticates every route, unlike the
    # Path=/auth refresh cookie).
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "Path=/;" in session_cookie or session_cookie.endswith("Path=/")
    assert "SameSite=lax" in session_cookie.lower().replace("samesite=lax", "SameSite=lax")

    # The CSRF cookie must be READABLE -- the SPA has to echo it back;
    # that echo is the entire double-submit mechanism.
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie


def test_login_persists_exactly_one_session_row_and_no_refresh_token(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)

    rows = asyncio.run(_session_rows())
    assert len(rows) == 1
    assert rows[0].revoked is False
    # The session path calls AuthService.authenticate, NOT login, so it
    # must not leave behind a RefreshToken row for a token no client holds
    # (see that method's own docstring).
    from app.models.refresh_token import RefreshToken

    async def _refresh_rows():
        async with get_sessionmaker()() as session:
            return list((await session.execute(select(RefreshToken))).scalars().all())

    assert asyncio.run(_refresh_rows()) == []


def test_every_login_mints_a_distinct_session_the_fixation_defense(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    first = _login_session(auth_client).cookies["session_id"]
    second = _login_session(auth_client).cookies["session_id"]
    assert first != second


def test_the_session_id_is_opaque_not_a_jwt(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    session_id = _login_session(auth_client).cookies["session_id"]
    # No claims to read, no signature to misvalidate, no `alg` to confuse.
    assert "." not in session_id


def test_bad_credentials_create_no_session(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/login", json={"email": _EMAIL, "password": "wrong password"}
    )
    assert response.status_code == 401
    assert asyncio.run(_session_rows()) == []


# ---------------------------------------------------------------------------
# Authenticating a protected route with the session cookie
# ---------------------------------------------------------------------------


def test_the_session_cookie_authenticates_a_protected_route(auth_client, email_sender) -> None:
    registered = _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)

    response = auth_client.get(f"{_BASE}/auth/me")
    assert response.status_code == 200, response.text
    assert response.json()["id"] == registered["id"]


def test_a_protected_route_401s_with_no_session(auth_client) -> None:
    response = auth_client.get(f"{_BASE}/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_forged_session_id_401s(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    auth_client.cookies.clear()
    auth_client.cookies.set("session_id", "not-a-real-session-id", domain="testserver", path="/")

    response = auth_client.get(f"{_BASE}/auth/me")
    assert response.status_code == 401


def test_an_unknown_and_a_revoked_session_are_indistinguishable(auth_client, email_sender) -> None:
    """Same "don't leak which specific reason" posture the whole component
    takes: a caller must not be able to tell "this was valid until you
    logged out" from "this was never a session"."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    auth_client.post(f"{_BASE}/auth/logout", headers=_csrf(auth_client))
    revoked = auth_client.get(f"{_BASE}/auth/me")

    auth_client.cookies.clear()
    auth_client.cookies.set("session_id", "never-issued-at-all", domain="testserver", path="/")
    unknown = auth_client.get(f"{_BASE}/auth/me")

    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json()


# ---------------------------------------------------------------------------
# Logout: revocation takes effect on the NEXT request
# ---------------------------------------------------------------------------


def test_logout_revokes_the_session_on_the_very_next_request(auth_client, email_sender) -> None:
    """THE load-bearing test for preferring sessions over JWTs. A bearer
    access token stays valid until its TTL elapses no matter how thoroughly
    the server "logs you out"; a session stops working immediately."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200

    logout = auth_client.post(
        f"{_BASE}/auth/logout", headers=_csrf(auth_client)
    )
    assert logout.status_code == 204

    assert auth_client.get(f"{_BASE}/auth/me").status_code == 401


def test_logout_marks_the_row_revoked_rather_than_deleting_it(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    auth_client.post(f"{_BASE}/auth/logout", headers=_csrf(auth_client))

    rows = asyncio.run(_session_rows())
    assert len(rows) == 1
    assert rows[0].revoked is True


def test_logout_clears_both_cookies(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    response = auth_client.post(
        f"{_BASE}/auth/logout", headers=_csrf(auth_client)
    )

    cleared = response.headers.get_list("set-cookie")
    assert any(c.startswith("session_id=") and "Max-Age=0" in c for c in cleared)
    assert any(c.startswith("csrf_token=") and "Max-Age=0" in c for c in cleared)


def test_logout_is_idempotent(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    csrf = _csrf(auth_client)
    session_id = auth_client.cookies["session_id"]

    assert auth_client.post(f"{_BASE}/auth/logout", headers=csrf).status_code == 204
    # Re-present the same (now revoked) cookie: still 204, never an error,
    # so logout is safe to retry and is not an existence oracle.
    auth_client.cookies.clear()
    auth_client.cookies.set("session_id", session_id, domain="testserver", path="/")
    auth_client.cookies.set("csrf_token", csrf["X-CSRF-Token"], domain="testserver", path="/")
    assert auth_client.post(f"{_BASE}/auth/logout", headers=csrf).status_code == 204


# ---------------------------------------------------------------------------
# CSRF: enforced on EVERY unsafe method, not just /auth/*
# ---------------------------------------------------------------------------


def test_logout_without_a_csrf_header_is_403_and_does_not_revoke(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)

    response = auth_client.post(f"{_BASE}/auth/logout")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    # Rejected BEFORE the revocation ran -- the session still works.
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200


def test_a_mismatched_csrf_header_is_403(auth_client, email_sender) -> None:
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)

    response = auth_client.post(
        f"{_BASE}/auth/logout",
        headers={"X-CSRF-Token": "not-the-cookie-value"},
    )
    assert response.status_code == 403


def test_csrf_is_enforced_on_a_non_auth_route_too(auth_client, email_sender) -> None:
    """The obligation session mode's `Path=/` cookie creates: EVERY
    unsafe-method route is a CSRF target, not just `/auth/*`. This is what
    `app/api/middleware/csrf.py` exists to guarantee, and what a per-route
    `enforce_csrf` call would eventually forget on some new route."""
    asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    _login_session(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)

    payload = {"title": "Hello", "body_json": {"type": "doc", "content": []}, "body_html": "<p>Hi</p>"}
    without_csrf = auth_client.post(f"{_BASE}/admin/blog/posts", json=payload)
    assert without_csrf.status_code == 403, without_csrf.text

    with_csrf = auth_client.post(f"{_BASE}/admin/blog/posts", json=payload, headers=_csrf(auth_client))
    assert with_csrf.status_code == 201, with_csrf.text


def test_safe_methods_need_no_csrf_header(auth_client, email_sender) -> None:
    # Requiring a CSRF token on every read would break ordinary navigation
    # for zero benefit -- a safe method is not supposed to change state.
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200


def test_login_itself_is_exempt_from_csrf(auth_client, email_sender) -> None:
    """Re-login while holding a stale session cookie must work: login is
    authenticated by the body's credentials and is in the middle of
    REPLACING the session whose CSRF token it would otherwise have to
    echo."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    # Second login, no CSRF header, stale cookies still in the jar.
    assert _login_session(auth_client).status_code == 200


def test_a_bearer_client_is_not_asked_for_csrf(auth_client, email_sender) -> None:
    """A bearer token is attached explicitly by the client's own code, so
    it has no ambient-credential exposure and must not be required to echo
    a CSRF token it was never given."""
    _register_and_verify(auth_client, email_sender)
    login = auth_client.post(
        f"{_BASE}/auth/login",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"X-Auth-Mode": "bearer"},
    )
    token = login.json()["access_token"]
    auth_client.cookies.clear()

    response = auth_client.post(
        f"{_BASE}/auth/logout",
        json={"refresh_token": login.json()["refresh_token"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Live roles, and revoke-everywhere on password reset
# ---------------------------------------------------------------------------


def test_a_revoked_role_stops_authorizing_immediately(auth_client, email_sender) -> None:
    """A `roles` claim baked into a JWT stays true until the token expires.
    A session reads roles live, so a demotion lands on the next request."""
    user_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    _login_session(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    assert auth_client.get(f"{_BASE}/admin/ping").status_code == 200

    async def _demote() -> None:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
            result.scalar_one().roles = []
            await session.commit()

    asyncio.run(_demote())

    # Same cookie, same session -- but no longer an admin.
    assert auth_client.get(f"{_BASE}/admin/ping").status_code == 403


def test_a_deleted_user_stops_authenticating_immediately(auth_client, email_sender) -> None:
    registered = _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200

    async def _delete() -> None:
        async with get_sessionmaker()() as session:
            result = await session.execute(select(User).where(User.id == uuid.UUID(registered["id"])))
            await session.delete(result.scalar_one())
            await session.commit()

    asyncio.run(_delete())

    assert auth_client.get(f"{_BASE}/auth/me").status_code == 401


def test_a_password_reset_revokes_every_session(auth_client, email_sender) -> None:
    """`AccountService.reset_password` must kill BOTH transports. Wiring
    only the refresh-token half would leave a reset account half-revoked:
    an attacker's session cookie would keep authenticating."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200

    # A POST while holding a session cookie -- so it needs the CSRF header
    # like every other unsafe-method request. That the reset flow is NOT
    # special-cased is the point of enforcing this in middleware.
    requested = auth_client.post(
        f"{_BASE}/auth/request-password-reset",
        json={"email": _EMAIL},
        headers=_csrf(auth_client),
    )
    assert requested.status_code == 202
    raw_token = email_sender.messages[-1].body.split("#token=")[1].split()[0]

    reset = auth_client.post(
        f"{_BASE}/auth/reset-password",
        json={"token": raw_token, "new_password": "an entirely different passphrase"},
        headers=_csrf(auth_client),
    )
    assert reset.status_code == 204

    # The pre-reset session is dead on the next request.
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 401
    assert all(row.revoked for row in asyncio.run(_session_rows()))


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_a_session_past_its_idle_deadline_stops_working(auth_client, email_sender) -> None:
    """A real idle timeout, with no sleeping: the session's stored
    `last_seen_at` is aged backwards past the configured idle window, which
    is exactly the state a genuinely abandoned session reaches. A JWT has
    one fixed `exp` and no notion of "the user stopped using this" at all,
    so it has no equivalent behavior to test."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)
    assert auth_client.get(f"{_BASE}/auth/me").status_code == 200

    idle_ttl = auth_client.app.state.settings.session_idle_ttl_seconds

    async def _age_past_the_idle_window() -> None:
        async with get_sessionmaker()() as session:
            row = (await session.execute(select(Session))).scalar_one()
            row.last_seen_at = row.last_seen_at - timedelta(seconds=idle_ttl + 60)
            await session.commit()

    asyncio.run(_age_past_the_idle_window())

    assert auth_client.get(f"{_BASE}/auth/me").status_code == 401


def test_a_session_past_its_absolute_deadline_stops_working(auth_client, email_sender) -> None:
    """The ceiling sliding expiry cannot provide: a session kept warm by
    continuous use still dies at `absolute_ttl`. Simulated by aging BOTH
    timestamps, so the session is well within its idle window (recently
    used) and past its absolute one."""
    _register_and_verify(auth_client, email_sender)
    _login_session(auth_client)

    async def _age_past_the_ceiling() -> None:
        async with get_sessionmaker()() as session:
            row = (await session.execute(select(Session))).scalar_one()
            row.absolute_expires_at = row.last_seen_at - timedelta(seconds=1)
            await session.commit()

    asyncio.run(_age_past_the_ceiling())

    assert auth_client.get(f"{_BASE}/auth/me").status_code == 401


def test_session_ttl_settings_are_wired_through_to_the_service(auth_client, email_sender) -> None:
    from app.core.security.auth.stores import build_session_service

    settings = auth_client.app.state.settings
    service = build_session_service(settings, session=None)
    assert int(service.idle_ttl.total_seconds()) == settings.session_idle_ttl_seconds
    assert int(service.absolute_ttl.total_seconds()) == settings.session_absolute_ttl_seconds
