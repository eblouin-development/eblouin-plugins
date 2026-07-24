"""Stage 5a (#41) — real auth behavior against the hermetic client, over
HTTP, exercising `AuthService` through the real `app/api/routers/auth.py`
handlers and the `AuthError` -> `ErrorEnvelope` exception handler
(`app/main.py`'s `_auth_error_handler`). Replaces the Stage 3 Step 2 stub
tests (every route used to unconditionally 501).

Uses `make_client` (not the plain `client` fixture) for every test that
actually calls an auth endpoint: the plain `client` fixture builds its app
from the process-wide, `lru_cache`d `get_settings()`, which never has
`JWT_SIGNING_KEY` set in this test process's environment (see
`app/core/security/auth/stores.py`'s `AuthNotConfiguredError` — the
fail-closed guard this app deliberately keeps in place) — `make_client`
instead builds a bespoke `Settings(...)` with an explicit
`jwt_signing_key=`, exactly like `tests/test_security_composition.py`
already does for its own bespoke config, bypassing that cache entirely.

Stage 5c (#45): `Settings.auth_require_email_verification` defaults to
`True`, so `AuthService.login` now generically refuses an unverified
account (see `app/api/deps.py:get_auth_service`'s docstring) — every
register-then-login flow below is now register-then-VERIFY-then-login.
The verification (and password-reset) token is emailed, never returned on
the wire — `_core.ConsoleEmailSender` (the dev/test default) only LOGS it,
which is not a seam a test can read deterministically. `auth_client`
below instead overrides the `get_email_sender` FastAPI dependency
(`app/api/deps.py`) with `_CapturingEmailSender`, a tiny in-memory
`EmailSender` that appends every `EmailMessage` it's given to a list a
test can inspect directly — `app.dependency_overrides[get_email_sender] =
lambda: sender` is exactly the override seam FastAPI recommends for this
(https://fastapi.tiangolo.com/advanced/testing-dependencies/), and reading
`sender.messages[-1].body` for the raw token is the deterministic
alternative to parsing `ConsoleEmailSender`'s log output the module
docstring for `app/api/deps.py:get_email_sender` calls for."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.pool import StaticPool

from app.api.deps import get_email_sender
from app.core.db import configure_engine, get_sessionmaker
from app.core.db.session import _reset_engine_for_tests
from app.core.security.auth import EmailMessage
from app.main import create_app

from .conftest import _test_lifespan

# Import side effect: registers every model on Base.metadata -- see
# tests/test_error_envelope.py's identical import for why a fixture that
# builds its own app/engine (rather than reusing the `client` fixture)
# needs this too.
import app.models  # noqa: F401,E402
from app.models.user import User  # noqa: E402

_TEST_SIGNING_KEY = "hermetic-test-signing-key-do-not-use-in-prod"

# Matches the "Or enter this code if your client stripped the link: <raw>"
# line both `AccountService.request_email_verification` and
# `AccountService.request_password_reset` put in their `EmailMessage.body`
# (`_core.py`) -- deliberately reading the CODE line, not parsing the
# `#token=` URL fragment out of the link line above it, since that's the
# more robust anchor (a bare trailing token, not a URL to further parse).
_TOKEN_LINE = re.compile(r"code if your client stripped the link: (\S+)")


class _CapturingEmailSender:
    """Test-only `EmailSender` (see `_core.EmailSender`'s `Protocol`) that
    appends every message to `self.messages` instead of delivering or
    logging it — the deterministic seam this module's own docstring
    describes. `messages[-1].body` is where a test reads the most
    recently issued raw verify/reset token from, via `_token_from`
    below."""

    def __init__(self) -> None:
        self.messages: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.messages.append(message)


def _token_from(message: EmailMessage) -> str:
    match = _TOKEN_LINE.search(message.body)
    assert match, f"no token line found in email body: {message.body!r}"
    return match.group(1)


class _RaisingEmailSender:
    """Adversarial-review fix (M1/M2) test double: an `EmailSender` (see
    `_core.EmailSender`'s `Protocol`) whose `send` always raises — models a
    misbehaving/failed delivery (SMTP outage, bounced relay, timeout) at
    the exact seam `get_email_sender` is overridden through. Proves, at
    the ENDPOINT contract (not just the component level — see `components/
    security/auth/tests/test_core.py`'s equivalent `RaisingEmailSender`
    fixture for that), that neither `POST /auth/request-password-reset`
    nor `POST /auth/register` ever surfaces this as a 500."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message: EmailMessage) -> None:
        self.attempts += 1
        raise RuntimeError("simulated delivery failure -- SMTP relay unreachable")


@pytest.fixture()
def email_sender() -> _CapturingEmailSender:
    return _CapturingEmailSender()


def _make_auth_client(
    make_client: Callable[..., TestClient],
    email_sender: _CapturingEmailSender,
    **settings_overrides: object,
) -> TestClient:
    """Shared body for the `auth_client` fixture below and every test that
    needs a bespoke `Settings(...)` beyond `auth_client`'s fixed defaults
    (e.g. a negative `auth_verify_ttl_seconds`/`auth_reset_ttl_seconds` to
    force immediate expiry) while STILL getting the `get_email_sender`
    override — duplicating `auth_client`'s two-line body would otherwise
    be needed at every such call site."""
    client = make_client(jwt_signing_key=_TEST_SIGNING_KEY, **settings_overrides)
    client.app.dependency_overrides[get_email_sender] = lambda: email_sender
    return client


@pytest.fixture()
def auth_client(make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender) -> TestClient:
    return _make_auth_client(make_client, email_sender)


def _verify(client: TestClient, email_sender: _CapturingEmailSender, *, message_index: int = -1) -> None:
    """Reads the raw verify/reset token out of `email_sender.messages[
    message_index]` (default: the most recently captured message — the
    verification email `POST /auth/register`'s side effect just sent, in
    every call site that doesn't pass an explicit index) and consumes it
    against `POST /auth/verify-email`, asserting 204."""
    token = _token_from(email_sender.messages[message_index])
    response = client.post("/auth/verify-email", json={"token": token})
    assert response.status_code == 204, response.text
    assert response.content == b""


def _register_and_verify(
    client: TestClient,
    email_sender: _CapturingEmailSender,
    email: str = "alice@example.com",
    password: str = "correct horse battery staple",
) -> dict:
    """`_register` + `_verify` in one call — the new baseline happy path
    every pre-existing register-then-login test below now needs, since
    `auth_require_email_verification` defaults to `True` (see this
    module's own docstring)."""
    registered = _register(client, email=email, password=password)
    _verify(client, email_sender)
    return registered


@pytest.fixture()
def unconfigured_auth_client() -> Iterator[TestClient]:
    """Same shape as `tests/test_error_envelope.py`'s `crashing_client`
    fixture: a fresh app/engine, `raise_server_exceptions=False` so a
    genuine 500 comes back as a real HTTP response instead of re-raising
    into the test process. Deliberately does NOT set `jwt_signing_key` —
    built via the plain `get_settings()` path (no `settings=` override),
    which never has `JWT_SIGNING_KEY` in this test process's environment
    (see this module's own docstring) — used by the one test proving the
    fail-closed guard actually fires."""
    configure_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    app = create_app(lifespan_ctx=_test_lifespan)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client
    _reset_engine_for_tests()


def _register(client: TestClient, email: str = "alice@example.com", password: str = "correct horse battery staple") -> dict:
    response = client.post("/auth/register", json={"email": email, "password": password})
    assert response.status_code == 201, response.text
    return response.json()


def _login(client: TestClient, email: str = "alice@example.com", password: str = "correct horse battery staple") -> dict:
    response = client.post("/auth/login", json={"email": email, "password": password}, headers={"X-Auth-Mode": "bearer"})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# register -> login -> me happy path
# ---------------------------------------------------------------------------


def test_register_then_verify_then_login_then_me_happy_path(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """THE Stage 5c happy path: `register` sends a verification email as a
    side effect (captured by `email_sender`, never parsed from a log
    line — see this module's own docstring), `POST /auth/verify-email`
    consumes it, and only THEN does `login` succeed — proving the
    `require_verification` gate `app/api/deps.py:get_auth_service` wires
    into `AuthService.login` actually gates, end to end over real HTTP."""
    registered = _register(auth_client)
    assert registered["email"] == "alice@example.com"
    assert registered["id"]

    # A verification email was actually "sent" as `register`'s side
    # effect -- addressed to the registered account, subject matching
    # `AccountService.request_email_verification`'s own.
    assert len(email_sender.messages) == 1
    assert email_sender.messages[0].to == "alice@example.com"
    assert email_sender.messages[0].subject == "Verify your email address"

    _verify(auth_client, email_sender)

    tokens = _login(auth_client)
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    me_response = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me_response.status_code == 200
    me_body = me_response.json()
    assert me_body["id"] == registered["id"]
    assert me_body["email"] == "alice@example.com"


def test_register_normalizes_and_persists_email_case(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """Sanity check the wire-level effect of `_core.AuthService.
    _normalize_email` (lowercase + strip) — registering with mixed case/
    whitespace round-trips to the normalized form."""
    registered = _register(auth_client, email="  Alice@Example.COM ", password="correct horse battery staple")
    assert registered["email"] == "alice@example.com"
    _verify(auth_client, email_sender)

    # Login with a DIFFERENT case/whitespace variant of the same email
    # still succeeds -- both normalize to the same account.
    tokens = _login(auth_client, email="ALICE@example.com", password="correct horse battery staple")
    assert tokens["access_token"]


# ---------------------------------------------------------------------------
# duplicate register -> 409 envelope
# ---------------------------------------------------------------------------


def test_duplicate_register_returns_409_conflict_envelope(auth_client: TestClient) -> None:
    _register(auth_client)
    response = auth_client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "a different password"}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "conflict"
    assert body["error"]["message"]


def test_reregistering_a_soft_deleted_email_returns_409_not_500(auth_client: TestClient) -> None:
    """#48, L1 -- regression test for the security fix (see `app/core/
    security/auth/stores.py:SqlAlchemyUserStore.create`'s own docstring):
    `get_by_email` is soft-delete-scoped, so a soft-deleted account's email
    reads as "free" at that lookup, but `users.email`'s DB-level UNIQUE
    constraint is full-table (by DECISION -- the email stays reserved, not
    freed for re-registration). Before the fix, re-registering that email
    hit the constraint's `IntegrityError` uncaught, surfacing as a raw 500
    AND a weak enumeration oracle (soft-deleted -> 500 vs. active -> 409 vs.
    free -> 201 were three distinguishable wire signatures). After the fix,
    it must return the SAME 409 `conflict` envelope the active-duplicate
    path returns -- byte-identical, no enumeration signal, no 500."""
    _register(auth_client)
    asyncio.run(_soft_delete_user_by_email("alice@example.com"))

    soft_deleted_response = auth_client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "a different password"}
    )
    assert soft_deleted_response.status_code == 409
    soft_deleted_body = soft_deleted_response.json()
    assert soft_deleted_body["error"]["code"] == "conflict"

    # Byte-identical to the active-duplicate-email 409 (same email, same
    # request shape -- only the account's soft-delete state differs) -- no
    # enumeration distinction between "active" and "soft-deleted" is
    # observable on the wire.
    active_duplicate_response = auth_client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "correct horse battery staple"}
    )
    assert active_duplicate_response.status_code == 201
    duplicate_of_active_response = auth_client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "a different password"}
    )
    assert duplicate_of_active_response.status_code == 409
    assert duplicate_of_active_response.json() == soft_deleted_body


# ---------------------------------------------------------------------------
# bad login -> 401 envelope
# ---------------------------------------------------------------------------


def test_login_with_unknown_email_returns_401_unauthenticated_envelope(auth_client: TestClient) -> None:
    response = auth_client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever"}, headers={"X-Auth-Mode": "bearer"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"


def test_login_with_wrong_password_returns_401_unauthenticated_envelope(auth_client: TestClient) -> None:
    _register(auth_client)
    response = auth_client.post("/auth/login", json={"email": "alice@example.com", "password": "wrong password"}, headers={"X-Auth-Mode": "bearer"})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# refresh rotates
# ---------------------------------------------------------------------------


def test_refresh_rotates_and_returns_a_new_pair(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client)

    response = auth_client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200
    rotated = response.json()
    assert rotated["access_token"]
    assert rotated["refresh_token"]
    # A genuinely NEW pair, not an echo of the presented tokens.
    assert rotated["access_token"] != tokens["access_token"]
    assert rotated["refresh_token"] != tokens["refresh_token"]

    # The rotated access token is itself immediately usable.
    me_response = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {rotated['access_token']}"})
    assert me_response.status_code == 200


# ---------------------------------------------------------------------------
# refresh reuse -> 401 AND the whole family is dead (the crown jewel)
# ---------------------------------------------------------------------------


def test_refresh_token_reuse_is_detected_and_kills_the_whole_family(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """THE reuse-detection proof, at the HTTP level (see
    `_core.AuthService.refresh`'s own docstring for the state machine this
    exercises end to end through real routes/DB rows, not the core's own
    unit tests): replaying an already-rotated refresh token returns 401,
    and — more than just that one token being rejected — the ROTATED
    token that replaced it (the family's current, otherwise still-valid
    tip) is ALSO rejected afterward, proving the entire family was
    revoked, not just the specific reused row."""
    _register_and_verify(auth_client, email_sender)
    original = _login(auth_client)

    # Rotate once: original.refresh_token -> rotated (still a live,
    # unused, valid token at this point).
    first_refresh = auth_client.post("/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert first_refresh.status_code == 200
    rotated = first_refresh.json()

    # REPLAY the already-used original refresh token -- reuse detected.
    replay = auth_client.post("/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert replay.status_code == 401
    replay_body = replay.json()
    assert replay_body["error"]["code"] == "unauthenticated"

    # The family is now dead -- even the CURRENTLY-VALID rotated token
    # (never itself reused) no longer works, proving whole-family
    # revocation, not merely "that one presented token was rejected".
    second_refresh = auth_client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})
    assert second_refresh.status_code == 401
    assert second_refresh.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# soft-deleted (deactivated) user -> auth fails CLOSED, not open
# ---------------------------------------------------------------------------


async def _soft_delete_user_by_email(email: str) -> None:
    """Soft-deletes the `User` row matching `email` directly through this
    app's own DB session/engine (`app.core.db.get_sessionmaker`, already
    configured by whichever `make_client(...)` call the calling test's
    fixture made) -- exercises `SoftDeleteMixin.mark_deleted()`, the exact
    mutation `AsyncRepository.delete()` itself calls, without needing a
    real "deactivate user" API endpoint (this app doesn't have one yet;
    that's a separate, later feature -- this test only needs the ROW in
    the deactivated state `stores.py`'s auth lookups must now honor)."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.email == email))
        user = result.scalar_one()
        user.mark_deleted()
        await session.commit()


def test_soft_deleted_user_cannot_login_or_refresh(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """FIX (whole-PR review, Stage 5a, security MEDIUM): a soft-deleted
    (deactivated) user must NOT be able to log in, and a refresh token
    issued BEFORE deactivation must NOT be usable afterward -- both
    `SqlAlchemyUserStore.get_by_email`/`get_by_id` now apply
    `User.not_deleted()`, matching `AsyncRepository`'s own default. Does
    NOT assert `/auth/me` with the pre-deletion ACCESS token is rejected
    -- that token remains valid until its own expiry, by design (stateless
    JWTs -- see `stores.py`'s comment on this exact point)."""
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client)

    asyncio.run(_soft_delete_user_by_email("alice@example.com"))

    login_response = auth_client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert login_response.status_code == 401
    assert login_response.json()["error"]["code"] == "unauthenticated"

    refresh_response = auth_client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# FIX (whole-PR review, Stage 5a, security LOW): reuse vs. an ordinary
# invalid refresh token must be WIRE-INDISTINGUISHABLE -- `_auth_error_
# handler` no longer echoes `str(exc)` for the 401 bucket.
# ---------------------------------------------------------------------------


def test_reuse_and_invalid_refresh_responses_are_wire_indistinguishable(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """Drives a genuine reuse (rotate once, then replay the used token) and
    an ordinary invalid-token refresh (a well-formed-looking but unknown
    token), and asserts both land on the exact SAME response body -- not
    merely the same status/code, but byte-identical, including `message`.
    Also asserts the reuse body contains none of "reuse"/"revoked"/
    "family" -- the substrings `_core.TokenReused`'s own message carries,
    which must never reach the client (see `_core.py`'s `TokenReused`
    docstring and `_auth_error_handler`'s updated docstring in
    `app/main.py`)."""
    _register_and_verify(auth_client, email_sender)
    original = _login(auth_client)

    first_refresh = auth_client.post("/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert first_refresh.status_code == 200

    # REUSE: replay the already-used original refresh token.
    reuse_response = auth_client.post("/auth/refresh", json={"refresh_token": original["refresh_token"]})
    assert reuse_response.status_code == 401
    reuse_body = reuse_response.json()

    # ORDINARY invalid token: a well-formed-looking but entirely unknown
    # token (never issued by this app, so `TokenService.decode_refresh`
    # rejects it as a bad signature -- a different `InvalidToken` failure
    # mode than reuse, but must render identically).
    invalid_response = auth_client.post("/auth/refresh", json={"refresh_token": "not-even-a-jwt"})
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


def test_logout_then_refresh_returns_401(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client)

    logout_response = auth_client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert logout_response.status_code == 204
    assert logout_response.content == b""

    refresh_response = auth_client.post("/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh_response.status_code == 401
    assert refresh_response.json()["error"]["code"] == "unauthenticated"


def test_logout_is_idempotent(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    """A second logout call with an already-revoked token still returns
    204, never an error -- `AuthService.logout`'s own documented,
    deliberate best-effort/idempotent contract."""
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client)

    first = auth_client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert first.status_code == 204

    second = auth_client.post("/auth/logout", json={"refresh_token": tokens["refresh_token"]})
    assert second.status_code == 204

    garbage = auth_client.post("/auth/logout", json={"refresh_token": "not-even-a-jwt"})
    assert garbage.status_code == 204


# ---------------------------------------------------------------------------
# /me without/with-bad token -> 401 envelope
# ---------------------------------------------------------------------------


def test_me_without_credentials_returns_401_envelope(auth_client: TestClient) -> None:
    response = auth_client.get("/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_with_garbage_bearer_token_returns_401_envelope(auth_client: TestClient) -> None:
    response = auth_client.get("/auth/me", headers={"Authorization": "Bearer not-even-a-jwt"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_me_with_a_refresh_token_instead_of_an_access_token_returns_401(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """A refresh token presented where an access token is expected is
    REJECTED at `TokenService`'s own `type` claim check (`_core.py`'s
    `TokenService` docstring) -- the two token kinds are not
    interchangeable."""
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client)
    response = auth_client.get("/auth/me", headers={"Authorization": f"Bearer {tokens['refresh_token']}"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Stage 5c (#45): login BEFORE verification -> 401, indistinguishable from
# a wrong password.
# ---------------------------------------------------------------------------


def test_login_before_verification_returns_401_indistinguishable_from_bad_password(
    auth_client: TestClient,
) -> None:
    """A registered-but-not-yet-verified account cannot log in --
    `AuthService.login`'s `require_verification` gate (`_core.py`'s
    `AuthService.login`, step 5) rejects it with the SAME generic
    `InvalidCredentials` (401 `unauthenticated`) every other login failure
    uses, so an unverified account is wire-BYTE-IDENTICAL to a wrong
    password, not merely the same status/code."""
    _register(auth_client)  # deliberately NOT verified

    unverified_response = auth_client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert unverified_response.status_code == 401
    unverified_body = unverified_response.json()
    assert unverified_body["error"]["code"] == "unauthenticated"

    wrong_password_response = auth_client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "not the right password"}
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
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    messages_before = len(email_sender.messages)

    known_response = auth_client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    unknown_response = auth_client.post("/auth/request-password-reset", json={"email": "nobody@example.com"})

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


def test_request_password_reset_rejects_empty_email(auth_client: TestClient) -> None:
    response = auth_client.post("/auth/request-password-reset", json={"email": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Stage 5c (#45): reset-password happy path -- old password dead, new
# password works, verification survives, EVERY pre-reset refresh token
# revoked (not just one family).
# ---------------------------------------------------------------------------


def test_reset_password_happy_path_revokes_old_sessions_and_logs_in_with_new_password(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    pre_reset_tokens = _login(auth_client)

    reset_request_response = auth_client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    assert reset_request_response.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = auth_client.post(
        "/auth/reset-password", json={"token": reset_token, "new_password": "a brand new password"}
    )
    assert reset_response.status_code == 204
    assert reset_response.content == b""

    # Old password no longer works.
    old_password_response = auth_client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert old_password_response.status_code == 401
    assert old_password_response.json()["error"]["code"] == "unauthenticated"

    # New password works IMMEDIATELY -- the account was already verified
    # pre-reset here (AccountService.reset_password also marks the email
    # verified on every reset, whether or not it already was -- see
    # test_reset_password_recovers_a_never_verified_account below for the
    # case where it wasn't).
    new_password_response = auth_client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "a brand new password"}
    )
    assert new_password_response.status_code == 200

    # EVERY pre-reset refresh token is revoked -- `AccountService.
    # reset_password`'s `revoke_all_for_user`, not merely the one family
    # behind whichever token happened to request the reset.
    pre_reset_refresh_response = auth_client.post(
        "/auth/refresh", json={"refresh_token": pre_reset_tokens["refresh_token"]}
    )
    assert pre_reset_refresh_response.status_code == 401
    assert pre_reset_refresh_response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Adversarial-review fix (M1/M2): a raising EmailSender must never change
# either endpoint's response -- request-password-reset stays 202 (byte-
# identical to unknown-email), register stays 201 (account not bricked).
# ---------------------------------------------------------------------------


def test_request_password_reset_known_email_still_returns_202_when_email_send_fails(
    make_client: Callable[..., TestClient],
) -> None:
    """M1, at the endpoint contract: overriding `get_email_sender` with a
    sender whose `send` raises models an SMTP failure. The KNOWN-email
    branch must still return 202 with an EMPTY body, byte-identical to the
    unknown-email response — a 500 here (or any response shape difference)
    would be exactly the account-enumeration oracle this fix closes."""
    raising_sender = _RaisingEmailSender()
    client = make_client(jwt_signing_key=_TEST_SIGNING_KEY)
    client.app.dependency_overrides[get_email_sender] = lambda: raising_sender

    # Register with a working sender first isn't needed here -- the user
    # just needs to exist; verification status is irrelevant to this
    # endpoint (it never reveals account state either way).
    register_response = client.post(
        "/auth/register", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert register_response.status_code == 201
    assert raising_sender.attempts == 1  # the verification-email send was attempted (and failed)

    known_response = client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    unknown_response = client.post("/auth/request-password-reset", json={"email": "nobody@example.com"})

    assert known_response.status_code == 202
    assert unknown_response.status_code == 202
    assert known_response.content == b""
    assert unknown_response.content == b""
    assert known_response.content == unknown_response.content
    # The known-email branch really did attempt (and fail) a send.
    assert raising_sender.attempts == 2


def test_register_returns_201_when_verification_email_send_fails(
    make_client: Callable[..., TestClient],
) -> None:
    """M2, at the endpoint contract: `register` must still return 201 (the
    account is created and durably committed) even though its
    post-registration verification-email side effect fails. Before this
    fix, a raising sender here would 500 an otherwise-successful
    registration."""
    raising_sender = _RaisingEmailSender()
    client = make_client(jwt_signing_key=_TEST_SIGNING_KEY)
    client.app.dependency_overrides[get_email_sender] = lambda: raising_sender

    response = client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "correct horse battery staple"}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == "bob@example.com"
    assert raising_sender.attempts == 1

    # The account really was created -- a second register attempt with the
    # same email now 409s, not another 201, proving this wasn't silently
    # rolled back.
    duplicate_response = client.post(
        "/auth/register", json={"email": "bob@example.com", "password": "a different password"}
    )
    assert duplicate_response.status_code == 409


# ---------------------------------------------------------------------------
# Adversarial-review fix (M2) recovery path: a registration whose
# verification email failed to send can still recover via password reset --
# reset_password now also marks the email verified.
# ---------------------------------------------------------------------------


def test_reset_password_recovers_a_never_verified_account(
    make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender
) -> None:
    """The end-to-end M2 recovery story: register a user, deliberately
    never call verify-email (modeling a verification email that never
    arrived), confirm login is blocked (401, generic -- see `_core.
    AuthService.login`'s `require_verification` gate), then request-
    password-reset -> reset-password -- login with the NEW password now
    succeeds, proving `AccountService.reset_password` marked the email
    verified even though `verify_email` was never called."""
    client = _make_auth_client(make_client, email_sender)

    register_response = client.post(
        "/auth/register", json={"email": "carol@example.com", "password": "an original password"}
    )
    assert register_response.status_code == 201
    # Deliberately no _verify(client, email_sender) call here.

    blocked_response = client.post(
        "/auth/login", json={"email": "carol@example.com", "password": "an original password"}
    )
    assert blocked_response.status_code == 401
    assert blocked_response.json()["error"]["code"] == "unauthenticated"

    reset_request_response = client.post("/auth/request-password-reset", json={"email": "carol@example.com"})
    assert reset_request_response.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = client.post(
        "/auth/reset-password", json={"token": reset_token, "new_password": "a freshly reset password"}
    )
    assert reset_response.status_code == 204

    recovered_response = client.post(
        "/auth/login", json={"email": "carol@example.com", "password": "a freshly reset password"}
    )
    assert recovered_response.status_code == 200
    assert "access_token" in recovered_response.json()


# ---------------------------------------------------------------------------
# Stage 5c (#45): bad / expired / reused single-use tokens -> 401 generic,
# for both verify-email and reset-password.
# ---------------------------------------------------------------------------


def test_verify_email_with_garbage_token_returns_401_generic(auth_client: TestClient) -> None:
    response = auth_client.post("/auth/verify-email", json={"token": "not-a-real-token"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_reset_password_with_garbage_token_returns_401_generic(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/auth/reset-password", json={"token": "not-a-real-token", "new_password": "whatever new password"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_verify_email_token_is_single_use_reuse_returns_401(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register(auth_client)
    token = _token_from(email_sender.messages[-1])

    first = auth_client.post("/auth/verify-email", json={"token": token})
    assert first.status_code == 204

    # REUSE (double-consume) of the exact same token.
    second = auth_client.post("/auth/verify-email", json={"token": token})
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "unauthenticated"


def test_reset_password_token_is_single_use_reuse_returns_401(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender)
    reset_request = auth_client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    assert reset_request.status_code == 202
    token = _token_from(email_sender.messages[-1])

    first = auth_client.post("/auth/reset-password", json={"token": token, "new_password": "first new password"})
    assert first.status_code == 204

    # REUSE (double-consume) of the exact same reset token.
    second = auth_client.post("/auth/reset-password", json={"token": token, "new_password": "second new password"})
    assert second.status_code == 401
    assert second.json()["error"]["code"] == "unauthenticated"


def test_verify_email_expired_token_returns_401(
    make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender
) -> None:
    """`auth_verify_ttl_seconds=-1` makes `AccountService.
    request_email_verification`'s issued token expire in the past the
    instant it's minted -- `SingleUseTokenService.consume`'s `expires_at
    <= now()` check then rejects it deterministically, without a real
    `time.sleep` in this test."""
    client = _make_auth_client(make_client, email_sender, auth_verify_ttl_seconds=-1)
    _register(client)
    token = _token_from(email_sender.messages[-1])

    response = client.post("/auth/verify-email", json={"token": token})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


def test_reset_password_expired_token_returns_401(
    make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender
) -> None:
    client = _make_auth_client(make_client, email_sender, auth_reset_ttl_seconds=-1)
    _register_and_verify(client, email_sender)
    reset_request = client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    assert reset_request.status_code == 202
    token = _token_from(email_sender.messages[-1])

    response = client.post("/auth/reset-password", json={"token": token, "new_password": "a new password"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


# ---------------------------------------------------------------------------
# Stage 5c (#45): lockout -- N wrong passwords lock the account (even the
# correct password fails while locked); a completed password reset lifts
# the lock, proving AccountService/AuthService share the lockout store.
# ---------------------------------------------------------------------------


def test_lockout_after_max_failures_locks_out_even_the_correct_password(
    make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender
) -> None:
    """5 wrong passwords (`Settings.auth_lockout_max_failures`'s default)
    lock the account -- the NEXT attempt, even with the CORRECT password,
    still returns 401 while locked (`_core.AuthService.login` step 3: the
    real password is deliberately never checked for a locked account, so
    a correct guess against a locked account is rejected exactly like a
    wrong one)."""
    client = _make_auth_client(make_client, email_sender)
    _register_and_verify(client, email_sender)

    for _ in range(5):
        wrong = client.post("/auth/login", json={"email": "alice@example.com", "password": "definitely wrong"}, headers={"X-Auth-Mode": "bearer"})
        assert wrong.status_code == 401

    locked_response = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert locked_response.status_code == 401
    assert locked_response.json()["error"]["code"] == "unauthenticated"


def test_reset_password_lifts_lockout_and_new_password_logs_in_immediately(
    make_client: Callable[..., TestClient], email_sender: _CapturingEmailSender
) -> None:
    """Proves `AccountService` and `AuthService` share the SAME
    `LockoutPolicy`/session (both built via `build_lockout_policy(settings,
    <the same request's session>)` -- see `app/api/deps.py:
    get_auth_service`/`get_account_service`'s own docstrings): after
    tripping the lockout via repeated `AuthService.login` failures, a
    completed `AccountService.reset_password` lifts it, and the freshly
    reset account logs in with its NEW password immediately -- no
    remaining cooldown."""
    client = _make_auth_client(make_client, email_sender)
    _register_and_verify(client, email_sender)

    for _ in range(5):
        wrong = client.post("/auth/login", json={"email": "alice@example.com", "password": "definitely wrong"}, headers={"X-Auth-Mode": "bearer"})
        assert wrong.status_code == 401

    # Confirm the account is actually locked -- the correct OLD password
    # still fails while locked.
    still_locked = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "correct horse battery staple"}
    )
    assert still_locked.status_code == 401

    reset_request = client.post("/auth/request-password-reset", json={"email": "alice@example.com"})
    assert reset_request.status_code == 202
    reset_token = _token_from(email_sender.messages[-1])

    reset_response = client.post(
        "/auth/reset-password", json={"token": reset_token, "new_password": "a freshly reset password"}
    )
    assert reset_response.status_code == 204

    # The lock is lifted -- the new password logs in IMMEDIATELY, with no
    # remaining lockout cooldown.
    new_login = client.post(
        "/auth/login", json={"email": "alice@example.com", "password": "a freshly reset password"}
    )
    assert new_login.status_code == 200


# ---------------------------------------------------------------------------
# Auth not configured -> fails closed (500), never signs with no key
# ---------------------------------------------------------------------------


def test_auth_endpoint_without_a_configured_signing_key_fails_closed(
    unconfigured_auth_client: TestClient,
) -> None:
    """No `JWT_SIGNING_KEY` configured -- `get_token_service()`'s
    fail-closed guard (`app/core/security/auth/stores.py`) refuses to
    construct a `TokenService`, surfacing as the app's generic 500
    `internal_error` envelope (the catch-all `Exception` handler — see
    app/main.py) rather than ever signing a token with an empty/absent
    key."""
    response = unconfigured_auth_client.post(
        "/auth/register", json={"email": "a@example.com", "password": "whatever-password"}
    )
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "internal_error"


# ---------------------------------------------------------------------------
# Bearer scheme still declared in OpenAPI
# ---------------------------------------------------------------------------


def test_bearer_scheme_is_declared_in_openapi(client: TestClient) -> None:
    schema = client.get("/openapi.json").json()
    security_schemes = schema.get("components", {}).get("securitySchemes", {})
    assert "HTTPBearer" in security_schemes
    assert security_schemes["HTTPBearer"]["type"] == "http"
    assert security_schemes["HTTPBearer"]["scheme"] == "bearer"


# ---------------------------------------------------------------------------
# extra="forbid" still enforced on the new RegisterRequest schema
# ---------------------------------------------------------------------------


def test_register_rejects_unknown_fields(auth_client: TestClient) -> None:
    response = auth_client.post(
        "/auth/register",
        json={"email": "a@example.com", "password": "a-password", "is_admin": True},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"
