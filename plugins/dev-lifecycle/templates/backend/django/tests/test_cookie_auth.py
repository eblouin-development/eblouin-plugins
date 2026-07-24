"""Stage 5d (#46) — the full cookie/RBAC matrix, over real HTTP against
`rest_framework.test.APIClient`, exercising `core/views.py`'s cookie-mode
`LoginView`/`RefreshView`/`LogoutView` and `AdminPingView`'s `GET
/admin/ping`. The DRF counterpart to `backend/fastapi`'s
`tests/test_cookie_auth.py` — see each test's own docstring for what it
proves; most carry over that module's scenarios near-verbatim, translated
to this block's Django-specific mechanics.

**`@pytest.mark.django_db(transaction=True)` is REQUIRED on every test
here** — same reasons `tests/test_auth.py`'s own module docstring
documents in full (durability of `DjangoRefreshTokenStore`'s autocommit
writes under real reuse-detection timing, and Django's async ORM under a
rolled-back `atomic()` block being a known source of
`SynchronousOnlyOperation`/connection flakiness).

**No `https://testserver/...` URL trick needed here**, unlike the FastAPI
counterpart's own httpx-based `TestClient`. That trick exists there
because httpx's cookie jar — like a real browser — refuses to re-attach a
`Secure` cookie to a follow-up request whose URL scheme is plain `http`.
Django's `test.Client` (which `APIClient` wraps) has no such scheme-aware
filtering: `Client.request()` unconditionally does `self.cookies.update(
response.cookies)` after every call and sends whatever is in `self.cookies`
on the next one, regardless of the `Secure` flag or the request's own
`wsgi.url_scheme` — so a bare relative-path `api_client.post("/auth/
refresh", ...)` already carries forward any cookie a prior response in the
same test set, exactly like every other test in this suite.

Cookie flags are asserted off the real `response.cookies[name].output()`
string (a genuine `"Set-Cookie: ..."` line `http.cookies.Morsel` renders
from whatever `core/security/auth/django.py`'s `set_auth_cookies`/
`clear_auth_cookies` actually called `response.set_cookie(...)` with) —
not off `response.cookies[name]["httponly"]`/`["secure"]`/... read as
booleans directly, because Django's own `HttpResponseBase.set_cookie` only
ever WRITES those Morsel keys when the flag is truthy (see that method's
own source: `if secure: ...["secure"] = True`, `if httponly: ...` — a
`False` flag is simply never assigned, leaving the Morsel's own falsy
default rather than an explicit `False`), so asserting the rendered
`Set-Cookie` text is the one representation that reliably distinguishes
"deliberately absent" from "explicitly false" either way, and is the exact
textual assertion style `test_cookie_auth.py`'s own FastAPI counterpart
already uses against its real header."""

from __future__ import annotations

import re
import uuid

import pytest
from asgiref.sync import async_to_sync
from rest_framework.test import APIClient

import core.security.auth.stores as stores
from core.models import User
from core.security.auth import EmailMessage
from core.security.auth.stores import seed_admin

from .test_auth import _CapturingEmailSender, _register_and_verify

pytestmark = pytest.mark.django_db(transaction=True)

_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"


@pytest.fixture()
def email_sender(monkeypatch: pytest.MonkeyPatch) -> _CapturingEmailSender:
    """Own instance, not an import of `test_auth.email_sender` — see that
    fixture's own docstring: importing a `@pytest.fixture`-decorated
    function and ALSO using its name as a test parameter reads, to a plain
    pyflakes-style checker, as an unused-name redefinition at every call
    site; a byte-identical, locally-defined fixture avoids that noise
    entirely. Monkeypatches `core.security.auth.stores.get_email_sender`
    the same way `test_auth.py`'s own fixture does."""
    sender = _CapturingEmailSender()
    monkeypatch.setattr(stores, "get_email_sender", lambda: sender)
    return sender


_TOKEN_LINE = re.compile(r"code if your client stripped the link: (\S+)")


def _token_from(message: EmailMessage) -> str:
    match = _TOKEN_LINE.search(message.body)
    assert match, f"no token line found in email body: {message.body!r}"
    return match.group(1)


def _cookie_login(
    client: APIClient,
    email_sender: _CapturingEmailSender,  # noqa: F811
    *,
    email: str = _EMAIL,
    password: str = _PASSWORD,
):
    """`_register_and_verify` + a cookie-mode `POST /auth/login` — the
    shared setup every refresh/logout cookie-path test below starts from.
    Returns the login `Response` so a caller can inspect its
    `.cookies`/body directly; `client.cookies` (Django's test-client jar)
    already holds the two cookies this response set, for every following
    request on the SAME client (see this module's own docstring on why no
    explicit `https://` URL override is needed here)."""
    _register_and_verify(client, email_sender, email=email, password=password)
    response = client.post(
        "/auth/login",
        {"email": email, "password": password},
        format="json",
        HTTP_X_AUTH_MODE="cookie",
    )
    assert response.status_code == 200, response.content
    return response


def _cookie(response, name: str):
    morsel = response.cookies.get(name)
    assert morsel is not None, f"no cookie named {name!r} in response.cookies: {response.cookies!r}"
    return morsel


# ---------------------------------------------------------------------------
# Cookie-mode login: cookie flags, empty refresh_token in body
# ---------------------------------------------------------------------------


def test_cookie_login_sets_expected_cookie_flags_and_empty_refresh_in_body(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    response = api_client.post(
        "/auth/login",
        {"email": _EMAIL, "password": _PASSWORD},
        format="json",
        HTTP_X_AUTH_MODE="cookie",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    # THE cookie-mode wire contract: refresh_token is an empty string in
    # the body, not omitted (TokenResponseSerializer.refresh_token is a
    # required str) and not the real token -- the real one is ONLY in the
    # cookie.
    assert body["refresh_token"] == ""

    assert set(response.cookies.keys()) == {"refresh_token", "csrf_token"}

    refresh_header = _cookie(response, "refresh_token").output()
    assert "HttpOnly" in refresh_header
    assert "Secure" in refresh_header
    assert "SameSite=lax" in refresh_header
    assert "Path=/auth" in refresh_header

    csrf_header = _cookie(response, "csrf_token").output()
    # The ONE deliberate difference from the refresh cookie -- see
    # _cookies.py's build_csrf_cookie_kwargs docstring: the SPA must be
    # able to read this one via document.cookie.
    assert "HttpOnly" not in csrf_header
    assert "Secure" in csrf_header
    assert "SameSite=lax" in csrf_header
    assert "Path=/auth" in csrf_header

    # The two cookie values are independent -- CSRF is never derived from
    # the refresh token (see generate_csrf_token's own docstring).
    assert _cookie(response, "refresh_token").value != _cookie(response, "csrf_token").value


# ---------------------------------------------------------------------------
# Bearer-mode login: byte-for-byte unchanged (default, and any non-"cookie"
# X-Auth-Mode value)
# ---------------------------------------------------------------------------


def test_bearer_login_unchanged_real_refresh_token_and_no_set_cookie(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    # Bearer mode is now opted into EXPLICITLY -- server-side sessions are
    # the default. Everything about the bearer response itself is
    # unchanged: real tokens in the body, no cookies.
    response = api_client.post(
        "/auth/login", {"email": _EMAIL, "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["refresh_token"] != ""
    assert body["access_token"]
    assert not response.cookies


def test_login_with_an_unrecognized_x_auth_mode_value_falls_to_session_mode(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """An `X-Auth-Mode` header that is present but matches none of the
    three known values falls to SESSION mode — the default, and the safer
    direction for an unrecognized value to fall: the caller gets the
    revocable credential rather than a long-lived token it did not
    successfully ask for."""
    _register_and_verify(api_client, email_sender)
    response = api_client.post(
        "/auth/login",
        {"email": _EMAIL, "password": _PASSWORD},
        format="json",
        HTTP_X_AUTH_MODE="banana",
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["token_type"] == "session"
    # No token of any kind handed to the client -- the credential is the
    # HttpOnly session cookie, which is the entire point.
    assert body["access_token"] == ""
    assert body["refresh_token"] == ""
    assert "session_id" in response.cookies


# ---------------------------------------------------------------------------
# Cookie-mode refresh: conditional CSRF (missing/blank/mismatched -> 403,
# valid -> 200 + both cookies rotated)
# ---------------------------------------------------------------------------


def test_cookie_refresh_missing_csrf_header_is_403(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    _cookie_login(api_client, email_sender)
    response = api_client.post("/auth/refresh", {"refresh_token": "ignored-on-the-cookie-path"}, format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_blank_csrf_header_is_403(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    _cookie_login(api_client, email_sender)
    response = api_client.post(
        "/auth/refresh",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN="",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_mismatched_csrf_header_is_403(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _cookie_login(api_client, email_sender)
    response = api_client.post(
        "/auth/refresh",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN="definitely-not-the-real-csrf-token",
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_cookie_refresh_with_valid_csrf_returns_200_and_rotates_both_cookies(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    login_response = _cookie_login(api_client, email_sender)
    old_refresh_cookie = _cookie(login_response, "refresh_token").value
    old_csrf_cookie = _cookie(login_response, "csrf_token").value

    response = api_client.post(
        "/auth/refresh",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN=old_csrf_cookie,
    )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["access_token"]
    assert body["refresh_token"] == ""

    assert set(response.cookies.keys()) == {"refresh_token", "csrf_token"}

    new_refresh_cookie = _cookie(response, "refresh_token").value
    new_csrf_cookie = _cookie(response, "csrf_token").value
    assert new_refresh_cookie != old_refresh_cookie
    assert new_csrf_cookie != old_csrf_cookie


def test_reusing_a_rotated_out_refresh_cookie_401s(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """Proves `AuthService.refresh`'s reuse-detection state machine fires
    on the cookie path exactly as it already does on the bearer path
    (`tests/test_auth.py`'s own reuse tests) — after a successful
    rotation, `api_client.cookies` (the test-client jar) is wound BACK to
    the OLD, already-rotated refresh/CSRF pair, simulating a stolen/
    replayed cookie. The replayed request's `X-CSRF-Token` matches the OLD
    CSRF cookie it's paired with, so this fails at `AuthService.refresh`'s
    reuse check, not at the CSRF gate — proving the RIGHT layer rejects
    it."""
    login_response = _cookie_login(api_client, email_sender)
    old_refresh_cookie = _cookie(login_response, "refresh_token").value
    old_csrf_cookie = _cookie(login_response, "csrf_token").value

    rotate_response = api_client.post(
        "/auth/refresh",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN=old_csrf_cookie,
    )
    assert rotate_response.status_code == 200, rotate_response.content

    api_client.cookies["refresh_token"] = old_refresh_cookie
    api_client.cookies["csrf_token"] = old_csrf_cookie
    reuse_response = api_client.post(
        "/auth/refresh",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN=old_csrf_cookie,
    )
    assert reuse_response.status_code == 401
    assert reuse_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Bearer-mode refresh: unchanged -- no CSRF required
# ---------------------------------------------------------------------------


def test_bearer_refresh_without_csrf_header_still_succeeds(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    login = api_client.post(
        "/auth/login", {"email": _EMAIL, "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    refresh_token = login.json()["refresh_token"]
    assert refresh_token

    response = api_client.post("/auth/refresh", {"refresh_token": refresh_token}, format="json")
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["refresh_token"] != ""
    assert not response.cookies


# ---------------------------------------------------------------------------
# Cookie-mode logout: clears both cookies, 204, idempotent; CSRF enforced
# ---------------------------------------------------------------------------


def test_cookie_logout_missing_csrf_is_403_before_revoking(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """JUDGMENT CALL (locked design): logout is state-changing, so the
    cookie path enforces CSRF too -- a missing/bad header 403s before
    `AuthService.logout` ever runs, distinct from that method's own
    idempotent-for-the-token posture (proven separately below)."""
    _cookie_login(api_client, email_sender)
    response = api_client.post("/auth/logout", {"refresh_token": "ignored-on-the-cookie-path"}, format="json")
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"
    # The CSRF gate rejected the request -- the refresh cookie the client
    # still holds must NOT have been cleared by a call that never reached
    # AuthService.logout/clear_auth_cookies at all.
    assert api_client.cookies.get("refresh_token") is not None


def test_cookie_logout_clears_both_cookies_and_204s_and_is_idempotent(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    login_response = _cookie_login(api_client, email_sender)
    csrf_cookie = _cookie(login_response, "csrf_token").value

    response = api_client.post(
        "/auth/logout",
        {"refresh_token": "ignored-on-the-cookie-path"},
        format="json",
        HTTP_X_CSRF_TOKEN=csrf_cookie,
    )
    assert response.status_code == 204
    assert response.content == b""

    assert set(response.cookies.keys()) == {"refresh_token", "csrf_token"}
    refresh_clear_header = _cookie(response, "refresh_token").output()
    csrf_clear_header = _cookie(response, "csrf_token").output()
    assert "Max-Age=0" in refresh_clear_header
    assert "Max-Age=0" in csrf_clear_header

    # The jar reflects the clear too -- Django's `Client.request()`
    # unconditionally does `self.cookies.update(response.cookies)`, so the
    # jar now holds a Max-Age=0, empty-value Morsel for each name (proven
    # above) rather than a real credential.
    assert api_client.cookies.get("refresh_token").value == ""
    assert api_client.cookies.get("csrf_token").value == ""

    # A GENUINE difference from the FastAPI/httpx counterpart's own
    # identical test, worth calling out rather than silently working
    # around: httpx's cookie jar, like a real browser, actually DELETES a
    # cookie from the jar once it receives a `Max-Age=0` clear -- a
    # following request from that jar carries no `refresh_token` cookie AT
    # ALL, so `read_refresh_cookie` returns `None` and the request falls
    # onto the bearer path. Django's `test.Client` does not model that:
    # `self.cookies.update(...)` merges the CLEARED Morsel (empty value,
    # `Max-Age=0`) into the jar rather than removing the key, so the jar
    # still holds a `refresh_token` entry -- an EMPTY one, but present, so
    # `request.COOKIES.get("refresh_token")` still returns `""` (falsy,
    # but not `None`) and `read_refresh_cookie`'s plain `.get()` still
    # takes the COOKIE path, which then requires (and, deliberately, does
    # not receive) a matching `X-CSRF-Token`. This is a test-client
    # fidelity gap, not a real behavioral one -- a genuine browser HONORS
    # `Max-Age=0` and stops sending the cookie entirely, exactly like
    # httpx's jar already does; the workaround below (`del
    # api_client.cookies[...]`) makes the jar match what a real browser
    # would actually do at this point, so the assertion below proves the
    # SAME idempotency `AuthService.logout`'s own contract (and
    # `tests/test_auth.py`'s bearer-path equivalent) guarantees, over the
    # BEARER path this now correctly falls onto.
    del api_client.cookies["refresh_token"]
    del api_client.cookies["csrf_token"]
    second_response = api_client.post("/auth/logout", {"refresh_token": "already-logged-out"}, format="json")
    assert second_response.status_code == 204


# ---------------------------------------------------------------------------
# RBAC admin example: GET /admin/ping -- 200 (admin) / 403 (authenticated
# non-admin) / 401 (unauthenticated)
# ---------------------------------------------------------------------------

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "correct horse battery staple 2"


def _seed_verified_admin(email: str, password: str) -> None:
    """`seed_admin` (`core/security/auth/stores.py`) is the real
    admin-provisioning path this test exercises directly -- then marks the
    row `email_verified=True` via a direct, plain sync ORM `.update()`
    call (bypassing the email flow entirely, same as `tests/test_auth.py`'s
    own `_soft_delete_user_by_email` helper does for its own direct-DB-
    write need -- plain sync ORM is fine here since it runs directly in
    the test's own sync context, not inside an `AuthService` call bridged
    via `async_to_sync`) so `AuthService.login`'s `require_verification`
    gate (on by default -- `config/settings.py`'s
    `AUTH_REQUIRE_EMAIL_VERIFICATION`) doesn't block the login this test
    needs next."""
    async_to_sync(seed_admin)(email, password)
    User.objects.filter(email=email).update(email_verified=True)


def test_admin_ping_returns_200_for_a_seeded_admin(api_client: APIClient) -> None:
    _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    login = api_client.post(
        "/auth/login",
        {"email": _ADMIN_EMAIL, "password": _ADMIN_PASSWORD},
        format="json",
        HTTP_X_AUTH_MODE="bearer",
    )
    assert login.status_code == 200, login.content
    token = login.json()["access_token"]

    response = api_client.get("/admin/ping", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_admin_ping_returns_403_for_an_authenticated_non_admin(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    login = api_client.post(
        "/auth/login", {"email": _EMAIL, "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert login.status_code == 200, login.content
    token = login.json()["access_token"]

    response = api_client.get("/admin/ping", HTTP_AUTHORIZATION=f"Bearer {token}")
    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "permission_denied"


def test_admin_ping_returns_401_for_an_unauthenticated_caller(api_client: APIClient) -> None:
    response = api_client.get("/admin/ping")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_seed_admin_id_is_a_real_uuid(api_client: APIClient) -> None:
    """Sanity check `seed_admin`'s own return value -- round-trips through
    `uuid.UUID` without raising, matching `tests/test_auth.py`'s own
    `test_register_id_is_a_real_uuid`'s identical proof for the ordinary
    registration path."""
    user = async_to_sync(seed_admin)("uuid-check@example.com", "correct horse battery staple 3")
    assert uuid.UUID(user.id)
    assert user.roles == ("admin",)
