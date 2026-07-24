"""Stage 5d (#46) — the full cookie/RBAC matrix, over real HTTP against the
hermetic client, exercising `app/api/routers/auth.py`'s cookie-mode login/
refresh/logout and `app/api/routers/admin.py`'s `GET /admin/ping`.

Reuses `tests/test_auth.py`'s own fixtures/helpers (`auth_client`,
`email_sender`, `_register_and_verify`, `_CapturingEmailSender`,
`_make_auth_client`) rather than duplicating that module's `make_client` ->
bespoke-`Settings` -> email-sender-override plumbing — see that module's
own docstring for why `auth_client` (not the plain `client` fixture) is
required for anything that calls a real auth endpoint (`JWT_SIGNING_KEY`
fail-closed) or needs `auth_require_email_verification`'s gate satisfied.

**Every cookie-mode request in this file uses an explicit `https://
testserver/...` URL**, never a bare relative path (`client.post("/auth/
login", ...)`, the shape every OTHER test file in this suite uses) — this
is required, not stylistic. `_cookies.py`'s cookie builders set
`secure=True` on both the refresh and CSRF cookies (see that module's own
docstring on why: a refresh/CSRF token must never travel over plaintext
HTTP), and httpx's own cookie jar — same as a real browser — refuses to
RE-ATTACH a `Secure` cookie to a subsequent request whose URL scheme is
plain `http`. The bare `TestClient` default base URL is `http://testserver`
(see `tests/test_security_composition.py`'s own `test_hsts_present_only_
over_https`, which discovered and documents this exact scheme-override
mechanism for its own HSTS test) — without the explicit `https://`
override, a SECOND request in the same test would silently see no cookies
at all (not a 403, not a proxy for "the cookie wasn't set" — an entirely
different, wrong failure shape from what this suite is actually proving).
`_BASE` below is that fixed prefix, applied at every call site in this
file for consistency, even where a specific test doesn't strictly depend
on it (e.g. the very first, unauthenticated request of a flow)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import get_sessionmaker
from app.core.security.auth.stores import seed_admin
from app.models.user import User

from .test_auth import _CapturingEmailSender, _make_auth_client, _register_and_verify

_BASE = "https://testserver"
_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"


@pytest.fixture()
def email_sender() -> _CapturingEmailSender:
    """Own instance, not an import of `test_auth.email_sender` — importing
    a `@pytest.fixture`-decorated function and ALSO using its name as a
    test parameter (the usual cross-module fixture-reuse pattern) reads,
    to a plain pyflakes-style checker, as an unused-name redefinition at
    every call site; defining a trivial, byte-identical fixture here
    avoids that noise entirely for a one-line body."""
    return _CapturingEmailSender()


@pytest.fixture()
def auth_client(make_client, email_sender: _CapturingEmailSender) -> TestClient:
    """Same build `test_auth.auth_client` uses (`_make_auth_client` —
    `jwt_signing_key` set, `get_email_sender` overridden to the capturing
    sender), bound to THIS module's own `make_client`/`email_sender`
    fixture instances rather than an import of the other module's already-
    bound fixture (pytest resolves fixtures per test-module unless shared
    via `conftest.py`)."""
    return _make_auth_client(make_client, email_sender)


def _cookie_login(
    client: TestClient,
    email_sender: _CapturingEmailSender,  # noqa: F811
    *,
    email: str = _EMAIL,
    password: str = _PASSWORD,
):
    """`_register_and_verify` + a cookie-mode `POST /auth/login` — the
    shared setup every refresh/logout cookie-path test below starts from.
    Returns the login `Response` so a caller can inspect its cookies/body
    directly; `client.cookies` (the jar) already holds the two cookies
    this response set, for every following request on the SAME client."""
    _register_and_verify(client, email_sender, email=email, password=password)
    response = client.post(
        f"{_BASE}/auth/login",
        json={"email": email, "password": password},
        headers={"X-Auth-Mode": "cookie"},
    )
    assert response.status_code == 200, response.text
    return response


def _set_cookie_header(response, name: str) -> str:
    """Finds the raw `Set-Cookie` header for `name` among possibly several
    on one response — `response.headers.get(...)` only returns the FIRST
    matching header, which is wrong here (every cookie-setting response in
    this file sets exactly two, `refresh_token` and `csrf_token`) —
    `get_list` (httpx's own multi-value header accessor, already used
    elsewhere in this catalog — see this module's own docstring) is what
    exposes all of them."""
    headers = response.headers.get_list("set-cookie")
    match = next((h for h in headers if h.startswith(f"{name}=")), None)
    assert match is not None, f"no Set-Cookie header for {name!r} in {headers!r}"
    return match


# ---------------------------------------------------------------------------
# Cookie-mode login: cookie flags, empty refresh_token in body
# ---------------------------------------------------------------------------


def test_cookie_login_sets_expected_cookie_flags_and_empty_refresh_in_body(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/login",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"X-Auth-Mode": "cookie"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    # THE cookie-mode wire contract: refresh_token is an empty string in
    # the body, not omitted (TokenResponse.refresh_token is a required
    # str) and not the real token -- the real one is ONLY in the cookie.
    assert body["refresh_token"] == ""

    set_cookie_headers = response.headers.get_list("set-cookie")
    assert len(set_cookie_headers) == 2

    refresh_header = _set_cookie_header(response, "refresh_token")
    assert "HttpOnly" in refresh_header
    assert "Secure" in refresh_header
    assert "SameSite=lax" in refresh_header
    assert "Path=/auth" in refresh_header

    csrf_header = _set_cookie_header(response, "csrf_token")
    # The ONE deliberate difference from the refresh cookie -- see
    # _cookies.py's build_csrf_cookie_kwargs docstring: the SPA must be
    # able to read this one via document.cookie.
    assert "HttpOnly" not in csrf_header
    assert "Secure" in csrf_header
    assert "SameSite=lax" in csrf_header
    assert "Path=/auth" in csrf_header

    # The two cookie values are independent -- CSRF is never derived from
    # the refresh token (see generate_csrf_token's own docstring).
    assert auth_client.cookies.get("refresh_token") != auth_client.cookies.get("csrf_token")


# ---------------------------------------------------------------------------
# Bearer-mode login: byte-for-byte unchanged (default, and any non-"cookie"
# X-Auth-Mode value)
# ---------------------------------------------------------------------------


def test_bearer_login_unchanged_real_refresh_token_and_no_set_cookie(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """Bearer mode is now opted into EXPLICITLY (`X-Auth-Mode: bearer`) --
    server-side sessions are the default. Everything about the bearer
    response itself is unchanged: real tokens in the body, no cookies."""
    _register_and_verify(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/login",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"X-Auth-Mode": "bearer"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refresh_token"] != ""
    assert body["access_token"]
    assert "set-cookie" not in {name.lower() for name in response.headers.keys()}


def test_login_with_an_unrecognized_x_auth_mode_value_falls_to_session_mode(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """An `X-Auth-Mode` header that is present but matches none of the
    three known values falls to SESSION mode — the default, and the safer
    direction for an unrecognized value to fall: the caller gets the
    revocable credential rather than a long-lived token it did not
    successfully ask for."""
    _register_and_verify(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/login",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"X-Auth-Mode": "banana"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["token_type"] == "session"
    # No token of any kind handed to the client -- the credential is the
    # HttpOnly session cookie, which is the entire point.
    assert body["refresh_token"] == ""
    assert body["access_token"] == ""
    assert "session_id" in response.cookies


# ---------------------------------------------------------------------------
# Cookie-mode refresh: conditional CSRF (missing/blank/mismatched -> 403,
# valid -> 200 + both cookies rotated)
# ---------------------------------------------------------------------------


def test_cookie_refresh_missing_csrf_header_is_403(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(auth_client, email_sender)
    response = auth_client.post(f"{_BASE}/auth/refresh", json={"refresh_token": "ignored-on-the-cookie-path"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_blank_csrf_header_is_403(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/refresh",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": ""},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_mismatched_csrf_header_is_403(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(auth_client, email_sender)
    response = auth_client.post(
        f"{_BASE}/auth/refresh",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": "definitely-not-the-real-csrf-token"},
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_with_valid_csrf_returns_200_and_rotates_both_cookies(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(auth_client, email_sender)
    old_refresh_cookie = auth_client.cookies.get("refresh_token")
    old_csrf_cookie = auth_client.cookies.get("csrf_token")

    response = auth_client.post(
        f"{_BASE}/auth/refresh",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": old_csrf_cookie},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] == ""

    set_cookie_headers = response.headers.get_list("set-cookie")
    assert len(set_cookie_headers) == 2

    new_refresh_cookie = auth_client.cookies.get("refresh_token")
    new_csrf_cookie = auth_client.cookies.get("csrf_token")
    assert new_refresh_cookie != old_refresh_cookie
    assert new_csrf_cookie != old_csrf_cookie


def test_reusing_a_rotated_out_refresh_cookie_401s(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """Proves `AuthService.refresh`'s reuse-detection state machine fires
    on the cookie path exactly as it already does on the bearer path
    (`tests/test_auth.py`'s own reuse tests) — after a successful
    rotation, the client's cookie jar is wound BACK to the OLD,
    already-rotated refresh/CSRF pair (`client.cookies.set(...)` mutates
    the jar directly, matching the current, non-deprecated httpx API —
    the per-request `cookies=` kwarg httpx also accepts is deprecated on
    this pinned version), simulating a stolen/replayed cookie. The
    replayed request's `X-CSRF-Token` matches the OLD CSRF cookie it's
    paired with, so this fails at `AuthService.refresh`'s reuse check, not
    at the CSRF gate — proving the RIGHT layer rejects it."""
    _cookie_login(auth_client, email_sender)
    old_refresh_cookie = auth_client.cookies.get("refresh_token")
    old_csrf_cookie = auth_client.cookies.get("csrf_token")

    rotate_response = auth_client.post(
        f"{_BASE}/auth/refresh",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": old_csrf_cookie},
    )
    assert rotate_response.status_code == 200, rotate_response.text

    auth_client.cookies.set("refresh_token", old_refresh_cookie)
    auth_client.cookies.set("csrf_token", old_csrf_cookie)
    reuse_response = auth_client.post(
        f"{_BASE}/auth/refresh",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": old_csrf_cookie},
    )
    assert reuse_response.status_code == 401
    assert reuse_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Bearer-mode refresh: unchanged -- no CSRF required
# ---------------------------------------------------------------------------


def test_bearer_refresh_without_csrf_header_still_succeeds(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    login = auth_client.post(
        f"{_BASE}/auth/login",
        json={"email": _EMAIL, "password": _PASSWORD},
        headers={"X-Auth-Mode": "bearer"},
    )
    refresh_token = login.json()["refresh_token"]
    assert refresh_token

    response = auth_client.post(f"{_BASE}/auth/refresh", json={"refresh_token": refresh_token})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["refresh_token"] != ""
    assert "set-cookie" not in {name.lower() for name in response.headers.keys()}


# ---------------------------------------------------------------------------
# Cookie-mode logout: clears both cookies, 204, idempotent; CSRF enforced
# ---------------------------------------------------------------------------


def test_cookie_logout_missing_csrf_is_403_before_revoking(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """JUDGMENT CALL (locked design): logout is state-changing, so the
    cookie path enforces CSRF too -- a missing/bad header 403s before
    `AuthService.logout` ever runs, distinct from that method's own
    idempotent-for-the-token posture (proven separately below)."""
    _cookie_login(auth_client, email_sender)
    response = auth_client.post(f"{_BASE}/auth/logout", json={"refresh_token": "ignored-on-the-cookie-path"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    # The CSRF gate rejected the request -- the refresh cookie the browser
    # still holds must NOT have been cleared by a call that never reached
    # AuthService.logout/clear_auth_cookies at all.
    assert auth_client.cookies.get("refresh_token") is not None


def test_cookie_logout_clears_both_cookies_and_204s_and_is_idempotent(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(auth_client, email_sender)
    csrf_cookie = auth_client.cookies.get("csrf_token")

    response = auth_client.post(
        f"{_BASE}/auth/logout",
        json={"refresh_token": "ignored-on-the-cookie-path"},
        headers={"X-CSRF-Token": csrf_cookie},
    )
    assert response.status_code == 204
    assert response.content == b""

    set_cookie_headers = response.headers.get_list("set-cookie")
    assert len(set_cookie_headers) == 2
    refresh_clear_header = _set_cookie_header(response, "refresh_token")
    csrf_clear_header = _set_cookie_header(response, "csrf_token")
    assert "Max-Age=0" in refresh_clear_header
    assert "Max-Age=0" in csrf_clear_header

    # The jar reflects the clear too -- no cookie left to send on a
    # following request.
    assert auth_client.cookies.get("refresh_token") is None
    assert auth_client.cookies.get("csrf_token") is None

    # Idempotent: a second logout, with no cookie left at all, falls onto
    # the BEARER path (no cookie present) rather than erroring -- still
    # 204, matching AuthService.logout's own idempotent-for-the-token
    # contract (tests/test_auth.py's bearer-path equivalent).
    second_response = auth_client.post(f"{_BASE}/auth/logout", json={"refresh_token": "already-logged-out"})
    assert second_response.status_code == 204


# ---------------------------------------------------------------------------
# RBAC admin example: GET /admin/ping -- 200 (admin) / 403 (authenticated
# non-admin) / 401 (unauthenticated)
# ---------------------------------------------------------------------------

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "correct horse battery staple 2"


async def _seed_verified_admin(email: str, password: str) -> None:
    """`seed_admin` (`app/core/security/auth/stores.py`) is the real
    admin-provisioning path this test exercises directly -- then marks
    the row `email_verified=True` by hand (bypassing the email flow
    entirely, same as `tests/test_auth.py`'s own `_soft_delete_user_by_
    email` helper does for its own direct-DB-write need) so `AuthService.
    login`'s `require_verification` gate (on by default -- `auth_client`
    never overrides it) doesn't block the login this test needs next."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        user = await seed_admin(session, email, password)
        result = await session.execute(select(User).where(User.id == uuid.UUID(user.id)))
        row = result.scalar_one()
        row.email_verified = True
        await session.commit()


def test_admin_ping_returns_200_for_a_seeded_admin(auth_client: TestClient) -> None:
    asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    login = auth_client.post(f"{_BASE}/auth/login", json={"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    response = auth_client.get(f"{_BASE}/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_ping_returns_403_for_an_authenticated_non_admin(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    login = auth_client.post(f"{_BASE}/auth/login", json={"email": _EMAIL, "password": _PASSWORD})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]

    response = auth_client.get(f"{_BASE}/admin/ping", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "permission_denied"


def test_admin_ping_returns_401_for_an_unauthenticated_caller(auth_client: TestClient) -> None:
    response = auth_client.get(f"{_BASE}/admin/ping")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"
