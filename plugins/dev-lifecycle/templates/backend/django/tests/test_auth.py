"""HTTP-level auth parity suite (Stage 5b, #44; account-lifecycle + lockout
Stage 5c, #45) — the DRF counterpart to `backend/fastapi`'s
`tests/test_auth.py`, porting every one of that module's scenarios over
`rest_framework.test.APIClient` against the real `core/views.py` `/auth/*`
views and `core/exceptions.py`'s `AuthError` -> `ErrorEnvelope` mapping.
See each test's own docstring for what it proves; most carry over their
FastAPI counterpart's reasoning near-verbatim, translated to this block's
Django-specific mechanics.

**`@pytest.mark.django_db(transaction=True)` is REQUIRED on every test
here** — same two reasons `tests/test_auth_stores.py`'s own module
docstring documents in full: (1) `DjangoRefreshTokenStore`'s durability
argument (autocommit, no explicit `commit()`) is only genuinely exercised
under real autocommit semantics, not pytest-django's default per-test
`atomic()` wrapper — and reuse detection specifically depends on a
concurrent-looking second presentation of a rotated token seeing the
first rotation's `used_at` write as already durable; (2) Django's async
ORM (`.acreate()`/`.afirst()`/`.aupdate()`, used throughout `core/
security/auth/stores.py`) is a known source of `SynchronousOnlyOperation`/
connection flakiness under a rolled-back `atomic()` block.

`JWT_SIGNING_KEY` needs no per-test override here (unlike backend/
fastapi's `auth_client` fixture, which must bypass its process-wide,
`lru_cache`d `get_settings()`): `config/settings_test.py` already sets a
real placeholder signing key via `os.environ.setdefault` before `config.
settings` is even imported, and `core/security/auth/stores.py:
get_token_service()` reads `django.conf.settings.JWT_SIGNING_KEY` fresh
on every call — no cache to bypass.

Stage 5c (#45): `settings.AUTH_REQUIRE_EMAIL_VERIFICATION` defaults to
`True` (`config/settings.py`), so `LoginView`'s gated `AuthService.login`
(`core/views.py`'s `_build_login_auth_service`) now generically refuses an
unverified account — every register-then-login flow below is now
register-then-VERIFY-then-login. The verification (and password-reset)
token is emailed, never returned on the wire — the real `DjangoEmailSender`
(this app's `get_email_sender()` default) is fire-and-forget and, under
the hermetic `EMAIL_BACKEND` (Django's own console backend), only LOGS the
raw token, which is not a seam a test can read deterministically. Every
test below that needs a token instead uses the `email_sender` fixture,
which monkeypatches `core.security.auth.stores.get_email_sender` —
`build_account_service()`'s own module-level resolution point for the
`EmailSender` it hands to `AccountService` — with `_CapturingEmailSender`,
a tiny SYNCHRONOUS-bodied in-memory `EmailSender` that appends every
`EmailMessage` it's given to a list a test can inspect directly. Because
its `send()` is directly `await`ed by `AccountService.
request_email_verification`/`request_password_reset` (not scheduled as a
background task the way the real `DjangoEmailSender` is), the message is
guaranteed to be captured by the time the `async_to_sync(...)`-bridged
view call returns — no dependence on the real sender's fire-and-forget
timing, and no log-string parsing."""

from __future__ import annotations

import re
import uuid

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

import core.security.auth.stores as stores
from core.models import User
from core.security.auth import EmailMessage

pytestmark = pytest.mark.django_db(transaction=True)


# ---------------------------------------------------------------------------
# Token-capture seam: monkeypatch core.security.auth.stores.get_email_sender
# ---------------------------------------------------------------------------


class _CapturingEmailSender:
    """Test-only `EmailSender` (see `core.security.auth.EmailSender`'s
    `Protocol`) that appends every message to `self.messages` instead of
    delivering or scheduling it — deterministic, unlike the real
    `DjangoEmailSender` (fire-and-forget via `asyncio.create_task`, see
    that class's own docstring). `messages[-1].body` is where a test reads
    the most recently issued raw verify/reset token from, via
    `_token_from` below."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


class _RaisingEmailSender:
    """Adversarial-review-fix (M1/M2, ported from `backend/fastapi`'s
    identical test double) test double: an `EmailSender` whose `send`
    always raises — models a misbehaving/failed delivery (SMTP outage,
    bounced relay, timeout) at the exact seam `core.security.auth.stores.
    get_email_sender` is monkeypatched through. Proves, at the ENDPOINT
    contract, that neither `POST /auth/register` nor
    `POST /auth/request-password-reset` ever surfaces this as a 500."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        raise RuntimeError("simulated delivery failure -- SMTP relay unreachable")


_TOKEN_LINE = re.compile(r"code if your client stripped the link: (\S+)")


def _token_from(message: EmailMessage) -> str:
    match = _TOKEN_LINE.search(message.body)
    assert match, f"no token line found in email body: {message.body!r}"
    return match.group(1)


@pytest.fixture()
def email_sender(monkeypatch: pytest.MonkeyPatch) -> _CapturingEmailSender:
    """Monkeypatches `core.security.auth.stores.get_email_sender` (the
    exact module-level name `build_account_service(email=None)` calls when
    a caller doesn't pass its own `email=` — see that function's own
    docstring) with a lambda returning ONE shared `_CapturingEmailSender`
    instance for the whole test — every `RegisterView`/
    `RequestPasswordResetView` call within the test appends to the SAME
    `.messages` list, matching `backend/fastapi`'s own `email_sender`
    fixture's per-test-shared-sender shape."""
    sender = _CapturingEmailSender()
    monkeypatch.setattr(stores, "get_email_sender", lambda: sender)
    return sender


def _register(client: APIClient, email: str = "alice@example.com", password: str = "correct horse battery staple") -> dict:
    response = client.post("/auth/register", {"email": email, "password": password}, format="json")
    assert response.status_code == 201, response.content
    return response.json()


def _login(client: APIClient, email: str = "alice@example.com", password: str = "correct horse battery staple") -> dict:
    response = client.post("/auth/login", {"email": email, "password": password}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert response.status_code == 200, response.content
    return response.json()


def _verify(client: APIClient, email_sender: _CapturingEmailSender, *, message_index: int = -1) -> None:
    """Reads the raw verify/reset token out of
    `email_sender.messages[message_index]` (default: the most recently
    captured message — the verification email `POST /auth/register`'s
    side effect just sent, in every call site that doesn't pass an
    explicit index) and consumes it against `POST /auth/verify-email`,
    asserting 204."""
    token = _token_from(email_sender.messages[message_index])
    response = client.post("/auth/verify-email", {"token": token}, format="json")
    assert response.status_code == 204, response.content
    assert response.content == b""


def _register_and_verify(
    client: APIClient,
    email_sender: _CapturingEmailSender,
    email: str = "alice@example.com",
    password: str = "correct horse battery staple",
) -> dict:
    """`_register` + `_verify` in one call — the new baseline happy path
    every pre-existing register-then-login test below now needs, since
    `settings.AUTH_REQUIRE_EMAIL_VERIFICATION` defaults to `True` (see
    this module's own docstring)."""
    registered = _register(client, email=email, password=password)
    _verify(client, email_sender)
    return registered


# ---------------------------------------------------------------------------
# register -> verify -> login -> me happy path
# ---------------------------------------------------------------------------


def test_register_then_verify_then_login_then_me_happy_path(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """THE Stage 5c happy path: `register` sends a verification email as a
    side effect (captured by `email_sender`, never parsed from a log
    line — see this module's own docstring), `POST /auth/verify-email`
    consumes it, and only THEN does `login` succeed — proving the
    `require_verification` gate `core/views.py`'s
    `_build_login_auth_service` wires into `AuthService.login` actually
    gates, end to end over real HTTP."""
    registered = _register(api_client)
    assert registered["email"] == "alice@example.com"
    assert registered["id"]

    # A verification email was actually "sent" as `register`'s side
    # effect -- addressed to the registered account, subject matching
    # `AccountService.request_email_verification`'s own.
    assert len(email_sender.messages) == 1
    assert email_sender.messages[0].to == "alice@example.com"
    assert email_sender.messages[0].subject == "Verify your email address"

    _verify(api_client, email_sender)

    tokens = _login(api_client)
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    me_response = api_client.get("/auth/me", HTTP_AUTHORIZATION=f"Bearer {tokens['access_token']}")
    assert me_response.status_code == 200
    me_body = me_response.json()
    assert me_body["id"] == registered["id"]
    assert me_body["email"] == "alice@example.com"


def test_register_normalizes_and_persists_email_case(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """Sanity check the wire-level effect of `_core.AuthService.
    _normalize_email` (lowercase + strip) — registering with mixed case/
    whitespace round-trips to the normalized form."""
    registered = _register(api_client, email="  Alice@Example.COM ", password="correct horse battery staple")
    assert registered["email"] == "alice@example.com"
    _verify(api_client, email_sender)

    # Login with a DIFFERENT case/whitespace variant of the same email
    # still succeeds -- both normalize to the same account.
    tokens = _login(api_client, email="ALICE@example.com", password="correct horse battery staple")
    assert tokens["access_token"]


# ---------------------------------------------------------------------------
# duplicate register -> 409 envelope
# ---------------------------------------------------------------------------


def test_duplicate_register_returns_409_conflict_envelope(api_client: APIClient) -> None:
    _register(api_client)
    response = api_client.post(
        "/auth/register", {"email": "alice@example.com", "password": "a different password"}, format="json"
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["message"]


def test_reregistering_a_soft_deleted_email_returns_409_not_500(api_client: APIClient) -> None:
    """#48, L1 -- regression test for the security fix (see `core/security/
    auth/stores.py:DjangoUserStore.create`'s own docstring): `get_by_email`
    queries through `User.objects` (soft-delete-scoped), so a soft-deleted
    account's email reads as "free" at that lookup, but `core.models.User.
    email`'s DB-level `unique=True` constraint is full-table (by DECISION
    -- the email stays reserved, not freed for re-registration). Before the
    fix, re-registering that email hit the constraint's `IntegrityError`
    uncaught, surfacing as a raw 500 AND a weak enumeration oracle
    (soft-deleted -> 500 vs. active -> 409 vs. free -> 201 were three
    distinguishable wire signatures). After the fix, it must return the
    SAME 409 `conflict` envelope the active-duplicate path returns --
    byte-identical, no enumeration signal, no 500."""
    _register(api_client)
    _soft_delete_user_by_email("alice@example.com")

    soft_deleted_response = api_client.post(
        "/auth/register", {"email": "alice@example.com", "password": "a different password"}, format="json"
    )
    assert soft_deleted_response.status_code == 409
    soft_deleted_body = soft_deleted_response.json()
    assert soft_deleted_body["error"]["code"] == "conflict"

    # Byte-identical to the active-duplicate-email 409 (same email, same
    # request shape -- only the account's soft-delete state differs) -- no
    # enumeration distinction between "active" and "soft-deleted" is
    # observable on the wire.
    active_duplicate_response = api_client.post(
        "/auth/register", {"email": "bob@example.com", "password": "correct horse battery staple"}, format="json"
    )
    assert active_duplicate_response.status_code == 201
    duplicate_of_active_response = api_client.post(
        "/auth/register", {"email": "bob@example.com", "password": "a different password"}, format="json"
    )
    assert duplicate_of_active_response.status_code == 409
    assert duplicate_of_active_response.json() == soft_deleted_body


# ---------------------------------------------------------------------------
# bad login -> 401 envelope
# ---------------------------------------------------------------------------


def test_login_with_unknown_email_returns_401_unauthenticated_envelope(api_client: APIClient) -> None:
    response = api_client.post("/auth/login", {"email": "nobody@example.com", "password": "whatever"}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"


def test_login_with_wrong_password_returns_401_unauthenticated_envelope(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "wrong password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# refresh rotates
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_returns_a_new_pair(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)

    response = api_client.post("/auth/refresh", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"]
    # A genuinely NEW pair, not an echo of the presented tokens.
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]

    # The rotated access token is itself immediately usable.
    me_response = api_client.get("/auth/me", HTTP_AUTHORIZATION=f"Bearer {rotated['access_token']}")
    assert me_response.status_code == 200


# ---------------------------------------------------------------------------
# refresh reuse -> 401 AND the whole family is dead (the crown jewel)
# ---------------------------------------------------------------------------


def test_refresh_token_reuse_is_detected_and_kills_the_whole_family(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """THE reuse-detection proof, at the HTTP level (see
    `_core.AuthService.refresh`'s own docstring for the state machine this
    exercises end to end through real routes/DB rows, not the vendored
    component's own unit tests): replaying an already-rotated refresh
    token returns 401, and — more than just that one token being rejected
    — the ROTATED token that replaced it (the family's current, otherwise
    still-valid tip) is ALSO rejected afterward, proving the entire
    family was revoked, not just the specific reused row."""
    _register_and_verify(api_client, email_sender)
    original = _login(api_client)

    # Rotate once: original.refresh_token -> rotated (still a live,
    # unused, valid token at this point).
    first_refresh = api_client.post("/auth/refresh", {"refresh_token": original["refresh_token"]}, format="json")
    assert first_refresh.status_code == 200
    rotated = first_refresh.json()

    # REPLAY the already-used original refresh token -- reuse detected.
    replay = api_client.post("/auth/refresh", {"refresh_token": original["refresh_token"]}, format="json")
    assert replay.status_code == 401
    replay_body = replay.json()
    assert replay_body["error"]["code"] == "unauthenticated"

    # The family is now dead -- even the CURRENTLY-VALID rotated token
    # (never itself reused) no longer works, proving whole-family
    # revocation, not merely "that one presented token was rejected".
    second_refresh = api_client.post("/auth/refresh", {"refresh_token": rotated["refresh_token"]}, format="json")
    assert second_refresh.status_code == 401
    assert second_refresh.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# soft-deleted (deactivated) user -> auth fails CLOSED, not open
# ---------------------------------------------------------------------------


def _soft_delete_user_by_email(email: str) -> None:
    """Soft-deletes the `User` row matching `email` directly through the
    ORM (`User.all_objects` -- the unscoped manager, since `User.objects`
    already excludes soft-deleted rows by default) — exercises `User.
    mark_deleted()`, without needing a real "deactivate user" API endpoint
    (this app doesn't have one yet). Plain sync ORM is fine here (unlike
    `core/security/auth/stores.py`'s async-only rule) — this helper runs
    directly in the test's own sync context, not inside an `AuthService`
    call bridged via `async_to_sync`."""
    user = User.all_objects.get(email=email)
    user.mark_deleted()
    user.save(update_fields=["deleted_at"])


def test_soft_deleted_user_cannot_login_or_refresh(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """A soft-deleted (deactivated) user must NOT be able to log in, and a
    refresh token issued BEFORE deactivation must NOT be usable afterward
    -- both `DjangoUserStore.get_by_email`/`get_by_id` apply `User.
    objects`'s default `not_deleted()` scoping (see that store's own
    docstring, "SECURITY (soft-delete auth-bypass fix..."). Does NOT
    assert `/auth/me` with the pre-deletion ACCESS token is rejected --
    that token remains valid until its own expiry, by design (stateless
    JWTs)."""
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)

    _soft_delete_user_by_email("alice@example.com")

    login_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert login_response.status_code == 401
    assert login_response.json()["error"]["code"] == "unauthenticated"

    refresh_response = api_client.post("/auth/refresh", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# reuse vs. an ordinary invalid refresh token must be WIRE-INDISTINGUISHABLE
# ---------------------------------------------------------------------------


def test_reuse_and_invalid_refresh_responses_are_wire_indistinguishable(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """Drives a genuine reuse (rotate once, then replay the used token) and
    an ordinary invalid-token refresh (a well-formed-looking but unknown
    token), and asserts both land on the exact SAME response body -- not
    merely the same status/code, but byte-identical, including `message`.
    Also asserts the reuse body contains none of "reuse"/"revoked"/
    "family" -- the substrings `_core.TokenReused`'s own message carries,
    which must never reach the client (see `core/exceptions.py`'s FIX-B
    section)."""
    _register_and_verify(api_client, email_sender)
    original = _login(api_client)

    first_refresh = api_client.post("/auth/refresh", {"refresh_token": original["refresh_token"]}, format="json")
    assert first_refresh.status_code == 200

    # REUSE: replay the already-used original refresh token.
    reuse_response = api_client.post("/auth/refresh", {"refresh_token": original["refresh_token"]}, format="json")
    assert reuse_response.status_code == 401
    reuse_body = reuse_response.json()

    # ORDINARY invalid token: a well-formed-looking but entirely unknown
    # token (never issued by this app, so `TokenService.decode_refresh`
    # rejects it as a bad signature -- a different `InvalidToken` failure
    # mode than reuse, but must render identically).
    invalid_response = api_client.post("/auth/refresh", {"refresh_token": "not-even-a-jwt"}, format="json")
    assert invalid_response.status_code == 401
    invalid_body = invalid_response.json()

    assert reuse_body == invalid_body

    reuse_message = reuse_body["error"]["message"].lower()
    assert "reuse" not in reuse_message
    assert "revoked" not in reuse_message
    assert "family" not in reuse_message


# ---------------------------------------------------------------------------
# logout -> 204, then refresh -> 401
# ---------------------------------------------------------------------------


def test_logout_then_refresh_returns_401(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)

    logout_response = api_client.post("/auth/logout", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert logout_response.status_code == 204
    assert logout_response.content == b""

    refresh_response = api_client.post("/auth/refresh", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "unauthenticated"


def test_logout_is_idempotent(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """A second logout call with an already-revoked token still returns
    204, never an error -- `AuthService.logout`'s own documented,
    deliberate best-effort/idempotent contract. A garbage (never-issued)
    token doesn't 500 either."""
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)

    first = api_client.post("/auth/logout", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert first.status_code == 204

    second = api_client.post("/auth/logout", {"refresh_token": tokens["refresh_token"]}, format="json")
    assert second.status_code == 204

    garbage = api_client.post("/auth/logout", {"refresh_token": "not-even-a-jwt"}, format="json")
    assert garbage.status_code == 204


# ---------------------------------------------------------------------------
# /me without/with-bad token -> 401 envelope
# ---------------------------------------------------------------------------


def test_me_without_credentials_returns_401_envelope(api_client: APIClient) -> None:
    response = api_client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_with_garbage_bearer_token_returns_401_envelope(api_client: APIClient) -> None:
    response = api_client.get("/auth/me", HTTP_AUTHORIZATION="Bearer not-even-a-jwt")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_with_a_refresh_token_instead_of_an_access_token_returns_401(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """A refresh token presented where an access token is expected is
    REJECTED at `TokenService`'s own `type` claim check -- the two token
    kinds are not interchangeable."""
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)
    response = api_client.get("/auth/me", HTTP_AUTHORIZATION=f"Bearer {tokens['refresh_token']}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_accepts_a_lowercase_bearer_scheme(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """The `Authorization` scheme token is case-insensitive per RFC 7235,
    and Starlette's `HTTPBearer` (the FastAPI side) accepts `bearer <token>`
    — so this backend must too, or the same client breaks against one
    backend but not the other. The token VALUE stays case-sensitive; only
    the scheme keyword is compared case-insensitively."""
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client)
    response = api_client.get("/auth/me", HTTP_AUTHORIZATION=f"bearer {tokens['access_token']}")
    assert response.status_code == 200, response.content
    assert response.json()["email"] == "alice@example.com"


# ---------------------------------------------------------------------------
# Stage 5c (#45): login BEFORE verification -> 401, indistinguishable from
# a wrong password.
# ---------------------------------------------------------------------------


def test_login_before_verification_returns_401_indistinguishable_from_bad_password(api_client: APIClient) -> None:
    """A registered-but-not-yet-verified account cannot log in --
    `AuthService.login`'s `require_verification` gate (`_core.py`'s
    `AuthService.login`, step 5) rejects it with the SAME generic
    `InvalidCredentials` (401 `unauthenticated`) every other login failure
    uses, so an unverified account is wire-BYTE-IDENTICAL to a wrong
    password, not merely the same status/code."""
    _register(api_client)  # deliberately NOT verified

    unverified_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert unverified_response.status_code == 401
    unverified_body = unverified_response.json()
    assert unverified_body["error"]["code"] == "unauthenticated"

    wrong_password_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "not the right password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert wrong_password_response.status_code == 401
    wrong_password_body = wrong_password_response.json()

    assert unverified_body == wrong_password_body


# ---------------------------------------------------------------------------
# Stage 5c (#45): request-password-reset -- byte-identical 202 for a known
# and an unknown email (anti-enumeration), token only actually issued for
# the known one.
# ---------------------------------------------------------------------------


def test_request_password_reset_is_byte_identical_for_known_and_unknown_email(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    messages_before = len(email_sender.messages)

    known_response = api_client.post("/auth/request-password-reset", {"email": "alice@example.com"}, format="json")
    unknown_response = api_client.post(
        "/auth/request-password-reset", {"email": "nobody@example.com"}, format="json"
    )

    assert known_response.status_code == 202
    assert unknown_response.status_code == 202
    assert known_response.content == b""
    assert unknown_response.content == b""
    # BYTE-IDENTICAL, not merely "both empty" -- the actual anti-
    # enumeration proof this endpoint exists for.
    assert known_response.content == unknown_response.content

    # A reset email/token was issued ONLY for the known account -- exactly
    # one new captured message, addressed to it -- never two, and never
    # one for the unknown address.
    assert len(email_sender.messages) == messages_before + 1
    reset_message = email_sender.messages[-1]
    assert reset_message.to == "alice@example.com"
    assert reset_message.subject == "Reset your password"


def test_request_password_reset_rejects_empty_email(api_client: APIClient) -> None:
    response = api_client.post("/auth/request-password-reset", {"email": ""}, format="json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Stage 5c (#45): reset-password happy path -- old password dead, new
# password works, verification survives, EVERY pre-reset refresh token
# revoked (not just one family).
# ---------------------------------------------------------------------------


def test_reset_password_happy_path_revokes_old_sessions_and_logs_in_with_new_password(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    pre_reset_tokens = _login(api_client)

    reset_request_response = api_client.post(
        "/auth/request-password-reset", {"email": "alice@example.com"}, format="json"
    )
    assert reset_request_response.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = api_client.post(
        "/auth/reset-password", {"token": reset_token, "new_password": "a brand new password"}, format="json"
    )
    assert reset_response.status_code == 204
    assert reset_response.content == b""

    # Old password no longer works.
    old_password_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert old_password_response.status_code == 401
    assert old_password_response.json()["error"]["code"] == "unauthenticated"

    # New password works IMMEDIATELY -- the account was already verified
    # pre-reset here (AccountService.reset_password also marks the email
    # verified on every reset, whether or not it already was -- see
    # test_reset_password_recovers_a_never_verified_account below for the
    # case where it wasn't).
    new_password_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "a brand new password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert new_password_response.status_code == 200

    # EVERY pre-reset refresh token is revoked -- `AccountService.
    # reset_password`'s `revoke_all_for_user`, not merely the one family
    # behind whichever token happened to request the reset.
    pre_reset_refresh_response = api_client.post(
        "/auth/refresh", {"refresh_token": pre_reset_tokens["refresh_token"]}, format="json"
    )
    assert pre_reset_refresh_response.status_code == 401
    assert pre_reset_refresh_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Adversarial-review fix (M1/M2, ported): a raising EmailSender must never
# change either endpoint's response -- request-password-reset stays 202
# (byte-identical to unknown-email), register stays 201 (account not
# bricked).
# ---------------------------------------------------------------------------


def test_request_password_reset_known_email_still_returns_202_when_email_send_fails(
    api_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1, at the endpoint contract: monkeypatching `core.security.auth.
    stores.get_email_sender` with a sender whose `send` raises models an
    SMTP failure. The KNOWN-email branch must still return 202 with an
    EMPTY body, byte-identical to the unknown-email response — a 500 here
    (or any response shape difference) would be exactly the account-
    enumeration oracle this fix closes."""
    raising_sender = _RaisingEmailSender()
    monkeypatch.setattr(stores, "get_email_sender", lambda: raising_sender)

    # Register with the same (raising) sender -- the user just needs to
    # exist; verification status is irrelevant to this endpoint (it never
    # reveals account state either way).
    register_response = api_client.post(
        "/auth/register", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json"
    )
    assert register_response.status_code == 201
    assert raising_sender.attempts == 1  # the verification-email send was attempted (and failed)

    known_response = api_client.post("/auth/request-password-reset", {"email": "alice@example.com"}, format="json")
    unknown_response = api_client.post(
        "/auth/request-password-reset", {"email": "nobody@example.com"}, format="json"
    )

    assert known_response.status_code == 202
    assert unknown_response.status_code == 202
    assert known_response.content == b""
    assert unknown_response.content == b""
    assert known_response.content == unknown_response.content
    # The known-email branch really did attempt (and fail) a send.
    assert raising_sender.attempts == 2


def test_register_returns_201_when_verification_email_send_fails(
    api_client: APIClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M2, at the endpoint contract: `register` must still return 201 (the
    account is created and durably committed) even though its
    post-registration verification-email side effect fails. Before this
    fix, a raising sender here would 500 an otherwise-successful
    registration."""
    raising_sender = _RaisingEmailSender()
    monkeypatch.setattr(stores, "get_email_sender", lambda: raising_sender)

    response = api_client.post(
        "/auth/register", {"email": "bob@example.com", "password": "correct horse battery staple"}, format="json"
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "bob@example.com"
    assert raising_sender.attempts == 1

    # The account really was created -- a second register attempt with the
    # same email now 409s, not another 201, proving this wasn't silently
    # rolled back.
    duplicate_response = api_client.post(
        "/auth/register", {"email": "bob@example.com", "password": "a different password"}, format="json"
    )
    assert duplicate_response.status_code == 409


# ---------------------------------------------------------------------------
# Adversarial-review fix (M2) recovery path: a registration whose
# verification email failed to send can still recover via password reset --
# reset_password now also marks the email verified.
# ---------------------------------------------------------------------------


def test_reset_password_recovers_a_never_verified_account(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """The end-to-end M2 recovery story: register a user, deliberately
    never call verify-email (modeling a verification email that never
    arrived), confirm login is blocked (401, generic -- see `_core.
    AuthService.login`'s `require_verification` gate), then request-
    password-reset -> reset-password -- login with the NEW password now
    succeeds, proving `AccountService.reset_password` marked the email
    verified even though `verify_email` was never called."""
    register_response = api_client.post(
        "/auth/register", {"email": "carol@example.com", "password": "an original password"}, format="json"
    )
    assert register_response.status_code == 201
    # Deliberately no _verify(api_client, email_sender) call here.

    blocked_response = api_client.post(
        "/auth/login", {"email": "carol@example.com", "password": "an original password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert blocked_response.status_code == 401
    assert blocked_response.json()["error"]["code"] == "unauthenticated"

    reset_request_response = api_client.post(
        "/auth/request-password-reset", {"email": "carol@example.com"}, format="json"
    )
    assert reset_request_response.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = api_client.post(
        "/auth/reset-password",
        {"token": reset_token, "new_password": "a freshly reset password"},
        format="json",
    )
    assert reset_response.status_code == 204

    recovered_response = api_client.post(
        "/auth/login", {"email": "carol@example.com", "password": "a freshly reset password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert recovered_response.status_code == 200
    assert "access_token" in recovered_response.json()


# ---------------------------------------------------------------------------
# Stage 5c (#45): bad / expired / reused single-use tokens -> 401 generic,
# for both verify-email and reset-password.
# ---------------------------------------------------------------------------


def test_verify_email_with_garbage_token_returns_401_generic(api_client: APIClient) -> None:
    response = api_client.post("/auth/verify-email", {"token": "not-a-real-token"}, format="json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_reset_password_with_garbage_token_returns_401_generic(api_client: APIClient) -> None:
    response = api_client.post(
        "/auth/reset-password", {"token": "not-a-real-token", "new_password": "whatever new password"}, format="json"
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_verify_email_token_is_single_use_reuse_returns_401(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register(api_client)
    token = _token_from(email_sender.messages[-1])

    first = api_client.post("/auth/verify-email", {"token": token}, format="json")
    assert first.status_code == 204

    # REUSE (double-consume) of the exact same token.
    second = api_client.post("/auth/verify-email", {"token": token}, format="json")
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "unauthenticated"


def test_reset_password_token_is_single_use_reuse_returns_401(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender)
    reset_request = api_client.post(
        "/auth/request-password-reset", {"email": "alice@example.com"}, format="json"
    )
    assert reset_request.status_code == 202
    token = _token_from(email_sender.messages[-1])

    first = api_client.post(
        "/auth/reset-password", {"token": token, "new_password": "first new password"}, format="json"
    )
    assert first.status_code == 204

    # REUSE (double-consume) of the exact same reset token.
    second = api_client.post(
        "/auth/reset-password", {"token": token, "new_password": "second new password"}, format="json"
    )
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "unauthenticated"


@override_settings(AUTH_VERIFY_TTL_SECONDS=-1)
def test_verify_email_expired_token_returns_401(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """`AUTH_VERIFY_TTL_SECONDS=-1` makes `AccountService.
    request_email_verification`'s issued token expire in the past the
    instant it's minted -- `SingleUseTokenService.consume`'s `expires_at
    <= now()` check then rejects it deterministically, without a real
    `time.sleep` in this test."""
    _register(api_client)
    token = _token_from(email_sender.messages[-1])

    response = api_client.post("/auth/verify-email", {"token": token}, format="json")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


@override_settings(AUTH_RESET_TTL_SECONDS=-1)
def test_reset_password_expired_token_returns_401(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    _register_and_verify(api_client, email_sender)
    reset_request = api_client.post(
        "/auth/request-password-reset", {"email": "alice@example.com"}, format="json"
    )
    assert reset_request.status_code == 202
    token = _token_from(email_sender.messages[-1])

    response = api_client.post(
        "/auth/reset-password", {"token": token, "new_password": "a new password"}, format="json"
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Stage 5c (#45): lockout -- N wrong passwords lock the account (even the
# correct password fails while locked); a completed password reset lifts
# the lock, proving AccountService/AuthService share the same underlying
# lockout table.
# ---------------------------------------------------------------------------


def test_lockout_after_max_failures_locks_out_even_the_correct_password(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """5 wrong passwords (`settings.AUTH_LOCKOUT_MAX_FAILURES`'s default)
    lock the account -- the NEXT attempt, even with the CORRECT password,
    still returns 401 while locked (`_core.AuthService.login` step 3: the
    real password is deliberately never checked for a locked account, so
    a correct guess against a locked account is rejected exactly like a
    wrong one)."""
    _register_and_verify(api_client, email_sender)

    for _ in range(5):
        wrong = api_client.post(
            "/auth/login", {"email": "alice@example.com", "password": "definitely wrong"}, format="json", HTTP_X_AUTH_MODE="bearer"
        )
        assert wrong.status_code == 401

    locked_response = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert locked_response.status_code == 401
    assert locked_response.json()["error"]["code"] == "unauthenticated"


def test_reset_password_lifts_lockout_and_new_password_logs_in_immediately(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    """Proves `AccountService` and `AuthService` share the SAME underlying
    `DjangoLockoutStore` table (both built via `build_lockout_policy()` --
    see `core/views.py`'s `_build_login_auth_service`/`core/security/auth/
    stores.py`'s `build_account_service` own docstrings): after tripping
    the lockout via repeated `AuthService.login` failures, a completed
    `AccountService.reset_password` lifts it, and the freshly reset
    account logs in with its NEW password immediately -- no remaining
    cooldown."""
    _register_and_verify(api_client, email_sender)

    for _ in range(5):
        wrong = api_client.post(
            "/auth/login", {"email": "alice@example.com", "password": "definitely wrong"}, format="json", HTTP_X_AUTH_MODE="bearer"
        )
        assert wrong.status_code == 401

    # Confirm the account is actually locked -- the correct OLD password
    # still fails while locked.
    still_locked = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "correct horse battery staple"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert still_locked.status_code == 401

    reset_request = api_client.post(
        "/auth/request-password-reset", {"email": "alice@example.com"}, format="json"
    )
    assert reset_request.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = api_client.post(
        "/auth/reset-password",
        {"token": reset_token, "new_password": "a freshly reset password"},
        format="json",
    )
    assert reset_response.status_code == 204

    # The lock is lifted -- the new password logs in IMMEDIATELY, with no
    # remaining lockout cooldown.
    new_login = api_client.post(
        "/auth/login", {"email": "alice@example.com", "password": "a freshly reset password"}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert new_login.status_code == 200


# ---------------------------------------------------------------------------
# Auth not configured -> fails closed (500), never signs with no key
# ---------------------------------------------------------------------------


@override_settings(JWT_SIGNING_KEY=None)
def test_auth_endpoint_without_a_configured_signing_key_fails_closed(api_client: APIClient) -> None:
    """No `JWT_SIGNING_KEY` configured -- `get_token_service()`'s
    fail-closed guard (`core/security/auth/stores.py`'s
    `AuthNotConfiguredError`) refuses to construct a `TokenService`,
    surfacing as the app's generic 500 `internal_error` envelope (`core/
    exceptions.py`'s catch-all -- `AuthNotConfiguredError` is a plain
    `RuntimeError`, not an `AuthError`, so it is NOT caught by this
    handler's `AuthError` branch) rather than ever signing a token with an
    empty/absent key."""
    response = api_client.post(
        "/auth/register", {"email": "a@example.com", "password": "whatever-password"}, format="json"
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# Bearer scheme still declared in OpenAPI
# ---------------------------------------------------------------------------


def test_bearer_scheme_is_declared_in_openapi() -> None:
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"


# ---------------------------------------------------------------------------
# Malformed/duplicate register bodies still 422 through the real route
# ---------------------------------------------------------------------------


def test_register_with_missing_password_returns_422_validation_failed_envelope(api_client: APIClient) -> None:
    response = api_client.post("/auth/register", {"email": "a@example.com"}, format="json")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_register_id_is_a_real_uuid(api_client: APIClient) -> None:
    registered = _register(api_client)
    # Round-trips through uuid.UUID without raising -- a real UUID string,
    # not e.g. the raw Django pk repr.
    assert uuid.UUID(registered["id"])
