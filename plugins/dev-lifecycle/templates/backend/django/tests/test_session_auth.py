"""End-to-end tests for the DEFAULT authentication path: server-side
sessions, over the real DRF test client.

`tests/test_auth.py` and `tests/test_cookie_auth.py` cover the JWT paths
(bearer and refresh-cookie); this module covers the one a browser actually
gets, and specifically the properties that justify preferring it -- a
logout that takes effect on the very next request, a role revocation that
lands immediately, a password reset that kills every device, and CSRF
enforced on every unsafe method rather than only on `/auth/*`.

The Django counterpart of `backend/fastapi`'s `tests/test_session_auth.py`,
asserting the same behaviors against the same wire contract. Unlike that
suite, no explicit `https://` URL override is needed: Django's test client
sends whatever is in `client.cookies` on every request regardless of the
`Secure` flag or the request scheme -- see `tests/test_cookie_auth.py`'s
own module docstring for that difference and why it is safe to rely on.

Cookie flags are asserted off the rendered `response.cookies[name].output()`
string for the reason that same docstring gives: Django only WRITES a
Morsel key when the flag is truthy, so reading `["httponly"]` as a boolean
cannot distinguish "deliberately absent" from "explicitly false".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from asgiref.sync import async_to_sync
from rest_framework.test import APIClient

import core.security.auth.stores as stores
from core.models import RefreshToken, Session, User
from core.security.auth.stores import seed_admin

from .test_auth import _CapturingEmailSender, _register_and_verify

pytestmark = pytest.mark.django_db(transaction=True)

_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"
_ADMIN_EMAIL = "root@example.com"
_ADMIN_PASSWORD = "another correct horse battery staple"


@pytest.fixture()
def email_sender(monkeypatch: pytest.MonkeyPatch) -> _CapturingEmailSender:
    sender = _CapturingEmailSender()
    monkeypatch.setattr(stores, "get_email_sender", lambda: sender)
    return sender


def _session_login(client: APIClient, *, email: str = _EMAIL, password: str = _PASSWORD):
    """A DEFAULT-path login -- no `X-Auth-Mode` header at all, which is the
    whole point. The client's cookie jar keeps `session_id`/`csrf_token`
    for every following request."""
    response = client.post("/auth/login", {"email": email, "password": password}, format="json")
    assert response.status_code == 200, response.content
    return response


def _csrf(client: APIClient) -> dict:
    """The double-submit header every unsafe-method request must echo,
    read back out of the (deliberately non-HttpOnly) CSRF cookie exactly as
    a real SPA reads it from `document.cookie`."""
    return {"HTTP_X_CSRF_TOKEN": client.cookies["csrf_token"].value}


def _seed_verified_admin(email: str, password: str) -> str:
    user = async_to_sync(seed_admin)(email, password)
    User.objects.filter(id=uuid.UUID(user.id)).update(email_verified=True)
    return user.id


# ---------------------------------------------------------------------------
# Login: session is the default, and hands the client no token
# ---------------------------------------------------------------------------


def test_login_defaults_to_session_mode(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    body = _session_login(api_client).json()

    assert body["token_type"] == "session"
    # THE property: nothing token-shaped reaches the client. There is no
    # credential in the JS heap for an XSS payload to exfiltrate, because
    # the only credential is a cookie JS cannot read.
    assert body["access_token"] == ""
    assert body["refresh_token"] == ""


def test_login_sets_the_expected_session_cookie_flags(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    response = _session_login(api_client)

    session_cookie = response.cookies["session_id"].output()
    csrf_cookie = response.cookies["csrf_token"].output()

    # Unreadable to JS, HTTPS-only, and scoped to the whole app (it
    # authenticates every route, unlike the Path=/auth refresh cookie).
    assert "HttpOnly" in session_cookie
    assert "Secure" in session_cookie
    assert "Path=/;" in session_cookie or session_cookie.rstrip().endswith("Path=/")
    assert "SameSite=lax" in session_cookie

    # The CSRF cookie must be READABLE -- the SPA has to echo it back;
    # that echo is the entire double-submit mechanism.
    assert "HttpOnly" not in csrf_cookie
    assert "Secure" in csrf_cookie


def test_login_persists_one_session_row_and_no_refresh_token(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    assert Session.objects.count() == 1
    assert Session.objects.first().revoked is False
    # The session path calls AuthService.authenticate, NOT login, so it
    # leaves behind no RefreshToken row for a token no client holds.
    assert RefreshToken.objects.count() == 0


def test_every_login_mints_a_distinct_session_the_fixation_defense(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    first = _session_login(api_client).cookies["session_id"].value
    second = _session_login(api_client).cookies["session_id"].value
    assert first != second


def test_the_session_id_is_opaque_not_a_jwt(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    # No claims to read, no signature to misvalidate, no `alg` to confuse.
    assert "." not in _session_login(api_client).cookies["session_id"].value


def test_bad_credentials_create_no_session(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    response = api_client.post(
        "/auth/login", {"email": _EMAIL, "password": "wrong password"}, format="json"
    )
    assert response.status_code == 401
    assert Session.objects.count() == 0


# ---------------------------------------------------------------------------
# Authenticating a protected route with the session cookie
# ---------------------------------------------------------------------------


def test_the_session_cookie_authenticates_a_protected_route(api_client: APIClient, email_sender) -> None:
    registered = _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    response = api_client.get("/auth/me")
    assert response.status_code == 200, response.content
    assert response.json()["id"] == registered["id"]


def test_a_protected_route_401s_with_no_session(api_client: APIClient) -> None:
    response = api_client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_a_forged_session_id_401s(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    api_client.cookies["session_id"] = "not-a-real-session-id"

    assert api_client.get("/auth/me").status_code == 401


def test_an_unknown_and_a_revoked_session_are_indistinguishable(api_client: APIClient, email_sender) -> None:
    """Same "don't leak which specific reason" posture the whole component
    takes: a caller must not be able to tell "this was valid until you
    logged out" from "this was never a session"."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    api_client.post("/auth/logout", format="json", **_csrf(api_client))
    revoked = api_client.get("/auth/me")

    api_client.cookies["session_id"] = "never-issued-at-all"
    unknown = api_client.get("/auth/me")

    assert revoked.status_code == unknown.status_code == 401
    assert revoked.json() == unknown.json()


# ---------------------------------------------------------------------------
# Logout: revocation takes effect on the NEXT request
# ---------------------------------------------------------------------------


def test_logout_revokes_the_session_on_the_very_next_request(api_client: APIClient, email_sender) -> None:
    """THE load-bearing test for preferring sessions over JWTs. A bearer
    access token stays valid until its TTL elapses no matter how thoroughly
    the server "logs you out"; a session stops working immediately."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert api_client.get("/auth/me").status_code == 200

    logout = api_client.post("/auth/logout", format="json", **_csrf(api_client))
    assert logout.status_code == 204, logout.content

    assert api_client.get("/auth/me").status_code == 401


def test_logout_marks_the_row_revoked_rather_than_deleting_it(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    api_client.post("/auth/logout", format="json", **_csrf(api_client))

    assert Session.objects.count() == 1
    assert Session.objects.first().revoked is True


def test_logout_clears_both_cookies(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    response = api_client.post("/auth/logout", format="json", **_csrf(api_client))

    assert "Max-Age=0" in response.cookies["session_id"].output()
    assert "Max-Age=0" in response.cookies["csrf_token"].output()


def test_logout_is_idempotent(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    csrf = _csrf(api_client)
    session_id = api_client.cookies["session_id"].value

    assert api_client.post("/auth/logout", format="json", **csrf).status_code == 204
    # Re-present the same (now revoked) cookie: still 204, never an error,
    # so logout is safe to retry and is not an existence oracle.
    api_client.cookies["session_id"] = session_id
    api_client.cookies["csrf_token"] = csrf["HTTP_X_CSRF_TOKEN"]
    assert api_client.post("/auth/logout", format="json", **csrf).status_code == 204


def test_logout_needs_no_request_body(api_client: APIClient, email_sender) -> None:
    # A session client genuinely has no refresh token to send; being 422'd
    # for omitting a field that means nothing on its transport would be an
    # absurd contract.
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert api_client.post("/auth/logout", **_csrf(api_client)).status_code == 204


# ---------------------------------------------------------------------------
# CSRF: enforced on EVERY unsafe method, not just /auth/*
# ---------------------------------------------------------------------------


def test_logout_without_a_csrf_header_is_403_and_does_not_revoke(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    response = api_client.post("/auth/logout", format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    # Rejected BEFORE the revocation ran -- the session still works.
    assert api_client.get("/auth/me").status_code == 200


def test_a_mismatched_csrf_header_is_403(api_client: APIClient, email_sender) -> None:
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    response = api_client.post(
        "/auth/logout", format="json", HTTP_X_CSRF_TOKEN="not-the-cookie-value"
    )
    assert response.status_code == 403


def test_the_middleware_403_matches_the_serializer_envelope_shape(api_client: APIClient, email_sender) -> None:
    """`csrf_middleware._csrf_denied_response` builds its envelope as a
    literal dict rather than through `ErrorEnvelopeSerializer` (to stay
    importable from `MIDDLEWARE` without pulling DRF's machinery into
    settings import order) — so the shape is asserted here against the same
    envelope every other error in this block produces."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    denied = api_client.post("/auth/logout", format="json").json()
    unauthenticated = api_client.get("/auth/me", HTTP_COOKIE="").json()

    assert set(denied) == set(unauthenticated) == {"error"}
    assert set(denied["error"]) == set(unauthenticated["error"]) == {"code", "message", "details"}


def test_csrf_is_enforced_on_a_non_auth_route_too(api_client: APIClient) -> None:
    """The obligation session mode's `Path=/` cookie creates: EVERY
    unsafe-method view is a CSRF target, not just `/auth/*`. This is what
    `core/security/auth/csrf_middleware.py` exists to guarantee, and what a
    per-view `enforce_csrf` call would eventually forget on some new
    view."""
    _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    _session_login(api_client, email=_ADMIN_EMAIL, password=_ADMIN_PASSWORD)

    payload = {"title": "Hello", "body_json": {"type": "doc", "content": []}, "body_html": "<p>Hi</p>"}
    without_csrf = api_client.post("/admin/blog/posts", payload, format="json")
    assert without_csrf.status_code == 403, without_csrf.content

    with_csrf = api_client.post("/admin/blog/posts", payload, format="json", **_csrf(api_client))
    assert with_csrf.status_code == 201, with_csrf.content


def test_safe_methods_need_no_csrf_header(api_client: APIClient, email_sender) -> None:
    # Requiring a CSRF token on every read would break ordinary navigation
    # for zero benefit -- a safe method is not supposed to change state.
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert api_client.get("/auth/me").status_code == 200


def test_login_itself_is_exempt_from_csrf(api_client: APIClient, email_sender) -> None:
    """Re-login while holding a stale session cookie must work: login is
    authenticated by the body's credentials and is in the middle of
    REPLACING the session whose CSRF token it would otherwise have to
    echo."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert _session_login(api_client).status_code == 200


def test_a_bearer_client_is_not_asked_for_csrf(api_client: APIClient, email_sender) -> None:
    """A bearer token is attached explicitly by the client's own code, so
    it has no ambient-credential exposure and must not be required to echo
    a CSRF token it was never given."""
    _register_and_verify(api_client, email_sender)
    login = api_client.post(
        "/auth/login", {"email": _EMAIL, "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    refresh_token = login.json()["refresh_token"]
    api_client.cookies.clear()

    response = api_client.post("/auth/logout", {"refresh_token": refresh_token}, format="json")
    assert response.status_code == 204


# ---------------------------------------------------------------------------
# Live roles, and revoke-everywhere on password reset
# ---------------------------------------------------------------------------


def test_a_revoked_role_stops_authorizing_immediately(api_client: APIClient) -> None:
    """A `roles` claim baked into a JWT stays true until the token expires.
    A session reads roles live, so a demotion lands on the next request."""
    user_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    _session_login(api_client, email=_ADMIN_EMAIL, password=_ADMIN_PASSWORD)
    assert api_client.get("/admin/ping").status_code == 200

    User.objects.filter(id=uuid.UUID(user_id)).update(roles=[])

    # Same cookie, same session -- but no longer an admin.
    assert api_client.get("/admin/ping").status_code == 403


def test_a_password_reset_revokes_every_session(api_client: APIClient, email_sender) -> None:
    """`AccountService.reset_password` must kill BOTH transports. Wiring
    only the refresh-token half would leave a reset account half-revoked:
    an attacker's session cookie would keep authenticating."""
    from .test_cookie_auth import _token_from

    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert api_client.get("/auth/me").status_code == 200

    requested = api_client.post(
        "/auth/request-password-reset", {"email": _EMAIL}, format="json", **_csrf(api_client)
    )
    assert requested.status_code == 202

    reset = api_client.post(
        "/auth/reset-password",
        {"token": _token_from(email_sender.messages[-1]), "new_password": "an entirely different passphrase"},
        format="json",
        **_csrf(api_client),
    )
    assert reset.status_code == 204, reset.content

    # The pre-reset session is dead on the next request.
    assert api_client.get("/auth/me").status_code == 401
    assert all(row.revoked for row in Session.objects.all())


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_a_session_past_its_idle_deadline_stops_working(api_client: APIClient, email_sender, settings) -> None:
    """A real idle timeout, with no sleeping: the session's stored
    `last_seen_at` is aged backwards past the configured idle window, which
    is exactly the state a genuinely abandoned session reaches. A JWT has
    one fixed `exp` and no notion of "the user stopped using this" at all,
    so it has no equivalent behavior to test."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)
    assert api_client.get("/auth/me").status_code == 200

    row = Session.objects.get()
    Session.objects.filter(pk=row.pk).update(
        last_seen_at=row.last_seen_at - timedelta(seconds=settings.SESSION_IDLE_TTL_SECONDS + 60)
    )

    assert api_client.get("/auth/me").status_code == 401


def test_a_session_past_its_absolute_deadline_stops_working(api_client: APIClient, email_sender) -> None:
    """The ceiling sliding expiry cannot provide: a session kept warm by
    continuous use still dies at `absolute_ttl`. Simulated by pulling the
    ceiling back behind `last_seen_at`, so the session is well within its
    idle window (recently used) and past its absolute one."""
    _register_and_verify(api_client, email_sender)
    _session_login(api_client)

    row = Session.objects.get()
    Session.objects.filter(pk=row.pk).update(absolute_expires_at=row.last_seen_at - timedelta(seconds=1))

    assert api_client.get("/auth/me").status_code == 401


def test_session_ttl_settings_are_wired_through_to_the_service(settings) -> None:
    from core.security.auth.stores import build_session_service

    service = build_session_service()
    assert int(service.idle_ttl.total_seconds()) == settings.SESSION_IDLE_TTL_SECONDS
    assert int(service.absolute_ttl.total_seconds()) == settings.SESSION_ABSOLUTE_TTL_SECONDS
