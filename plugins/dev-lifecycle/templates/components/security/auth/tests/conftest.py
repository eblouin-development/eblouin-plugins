"""Loads this component's `_core.py`, `_sessions.py`, `_cookies.py`,
`_oauth.py`, `fastapi.py`, and `django.py` under private module names
without ever putting this directory on `sys.path` -- see
security-headers/tests/conftest.py's docstring for the full rationale
(this component mirrors the same load-by-file-path pattern
rate-limiting/db-session/etc. use throughout this catalog). Load order
matters: `_core` first (no sibling deps), then `_sessions`/`_cookies`
(each imports `_core` via a bare `import _core`, resolved against
`sys.modules["_core"]` since that's the exact name it was registered
under below), then `fastapi`/`django` (each imports `_core`,
`_cookies`, AND `_sessions` the same bare-sibling-import way -- so both
must already be registered before either adapter loads). `fastapi.py`
needs the real `fastapi` package installed to import (`fastapi.Depends`,
`fastapi.Request`, `fastapi.security.HTTPBearer`) -- see this component's
README's "Testing" section for the `uv run --with fastapi ...` invocation
this requires; `django.py` needs no `django` package import at all (see
that file's own module docstring), so loading it costs nothing extra.

Also provides the shared test fixtures every test module in this
directory needs: in-memory fakes implementing `UserStore`,
`RefreshTokenStore`, and `SessionStore` (plain dicts, no real database),
an injectable/advanceable `now`, a `TokenService` built against a fixed
test signing key, a fully wired `AuthService`, and a fully wired
`SessionService`."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

import pytest

COMPONENT_DIR = Path(__file__).resolve().parent.parent


def _load(module_name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, COMPONENT_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


core = _load("_core", "_core.py")
sessions = _load("_sessions", "_sessions.py")
cookies = _load("_cookies", "_cookies.py")
oauth = _load("_oauth", "_oauth.py")
fastapi_adapter = _load("auth_fastapi_adapter", "fastapi.py")
django_adapter = _load("auth_django_adapter", "django.py")


@pytest.fixture(scope="session")
def core_mod() -> ModuleType:
    return core


@pytest.fixture(scope="session")
def sessions_mod() -> ModuleType:
    return sessions


@pytest.fixture(scope="session")
def cookies_mod() -> ModuleType:
    return cookies


@pytest.fixture(scope="session")
def oauth_mod() -> ModuleType:
    return oauth


@pytest.fixture(scope="session")
def fastapi_mod() -> ModuleType:
    return fastapi_adapter


@pytest.fixture(scope="session")
def django_mod() -> ModuleType:
    return django_adapter


# ---------------------------------------------------------------------------
# Injectable clock
# ---------------------------------------------------------------------------


class Clock:
    """A mutable, injectable clock -- `now()` returns whatever `current`
    is currently set to, and `advance()` moves it forward. Passed as the
    `now` callable to `TokenService`/`AuthService`, so a test can advance
    time deterministically (past a TTL, across a rotation) with no real
    sleeping and no monkeypatching of a module-global clock."""

    def __init__(self, start: datetime) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@pytest.fixture
def clock() -> Clock:
    return Clock(datetime(2026, 1, 1, tzinfo=timezone.utc))


# ---------------------------------------------------------------------------
# In-memory fakes
# ---------------------------------------------------------------------------


class FakeUserStore:
    """In-memory `UserStore` -- a dict keyed by normalized email, plus a
    parallel dict keyed by id for `get_by_id`."""

    def __init__(self) -> None:
        self._by_email: dict[str, "core.UserRecord"] = {}
        self._by_id: dict[str, "core.UserRecord"] = {}
        self._next_id = 1

    async def get_by_email(self, email):
        return self._by_email.get(email)

    async def get_by_id(self, id):
        return self._by_id.get(id)

    async def create(self, email, password_hash, roles):
        record = core.UserRecord(
            id=str(self._next_id),
            email=email,
            password_hash=password_hash,
            roles=tuple(roles),
        )
        self._next_id += 1
        self._by_email[email] = record
        self._by_id[record.id] = record
        return record

    async def mark_email_verified(self, user_id, at):
        existing = self._by_id[user_id]
        updated = core.UserRecord(
            id=existing.id,
            email=existing.email,
            password_hash=existing.password_hash,
            roles=existing.roles,
            email_verified=True,
        )
        self._by_id[user_id] = updated
        self._by_email[existing.email] = updated

    async def set_password_hash(self, user_id, new_hash):
        existing = self._by_id[user_id]
        updated = core.UserRecord(
            id=existing.id,
            email=existing.email,
            password_hash=new_hash,
            roles=existing.roles,
            email_verified=existing.email_verified,
        )
        self._by_id[user_id] = updated
        self._by_email[existing.email] = updated


class FakeRefreshTokenStore:
    """In-memory `RefreshTokenStore` -- a dict keyed by `token_hash`.
    Tracks `revoke_family` calls (`revoke_family_calls`) so tests can
    assert it was -- or deliberately was NOT -- invoked."""

    def __init__(self) -> None:
        self._by_hash: dict[str, "core.RefreshRecord"] = {}
        self.revoke_family_calls: list[str] = []

    async def add(self, record):
        self._by_hash[record.token_hash] = record

    async def get_by_hash(self, token_hash):
        return self._by_hash.get(token_hash)

    async def mark_used(self, token_hash, used_at):
        existing = self._by_hash[token_hash]
        self._by_hash[token_hash] = core.RefreshRecord(
            token_hash=existing.token_hash,
            jti=existing.jti,
            family_id=existing.family_id,
            user_id=existing.user_id,
            issued_at=existing.issued_at,
            expires_at=existing.expires_at,
            used_at=used_at,
            revoked=existing.revoked,
        )

    async def revoke_family(self, family_id):
        self.revoke_family_calls.append(family_id)
        for token_hash, record in list(self._by_hash.items()):
            if record.family_id == family_id:
                self._by_hash[token_hash] = core.RefreshRecord(
                    token_hash=record.token_hash,
                    jti=record.jti,
                    family_id=record.family_id,
                    user_id=record.user_id,
                    issued_at=record.issued_at,
                    expires_at=record.expires_at,
                    used_at=record.used_at,
                    revoked=True,
                )

    def all_records(self):
        return list(self._by_hash.values())

    async def revoke_all_for_user(self, user_id):
        for token_hash, record in list(self._by_hash.items()):
            if record.user_id == user_id:
                self._by_hash[token_hash] = core.RefreshRecord(
                    token_hash=record.token_hash,
                    jti=record.jti,
                    family_id=record.family_id,
                    user_id=record.user_id,
                    issued_at=record.issued_at,
                    expires_at=record.expires_at,
                    used_at=record.used_at,
                    revoked=True,
                )


class FakeSessionStore:
    """In-memory `SessionStore` -- a dict keyed by `session_hash`,
    mirroring `FakeRefreshTokenStore`'s own shape above. Counts `touch`
    calls (`touch_calls`) so tests can assert `SessionService.resolve`'s
    `touch_interval` write-rate bound actually suppresses writes rather
    than merely producing the right `last_seen_at` by accident."""

    def __init__(self) -> None:
        self._by_hash: dict[str, "sessions.SessionRecord"] = {}
        self.touch_calls: list[tuple[str, "datetime"]] = []

    def _replace(self, record, **changes):
        fields = {
            "session_hash": record.session_hash,
            "user_id": record.user_id,
            "created_at": record.created_at,
            "last_seen_at": record.last_seen_at,
            "absolute_expires_at": record.absolute_expires_at,
            "revoked": record.revoked,
        }
        fields.update(changes)
        return sessions.SessionRecord(**fields)

    async def add(self, record):
        self._by_hash[record.session_hash] = record

    async def get_by_hash(self, session_hash):
        return self._by_hash.get(session_hash)

    async def touch(self, session_hash, last_seen_at):
        self.touch_calls.append((session_hash, last_seen_at))
        existing = self._by_hash[session_hash]
        self._by_hash[session_hash] = self._replace(existing, last_seen_at=last_seen_at)

    async def revoke(self, session_hash):
        # Silently tolerates an unknown hash -- `SessionStore.revoke`'s own
        # documented idempotence contract, which `SessionService.revoke`
        # (logout) relies on to never raise on a stale/forged cookie.
        existing = self._by_hash.get(session_hash)
        if existing is None:
            return
        self._by_hash[session_hash] = self._replace(existing, revoked=True)

    async def revoke_all_for_user(self, user_id):
        for session_hash, record in list(self._by_hash.items()):
            if record.user_id == user_id:
                self._by_hash[session_hash] = self._replace(record, revoked=True)

    def all_records(self):
        return list(self._by_hash.values())


@pytest.fixture
def user_store() -> FakeUserStore:
    return FakeUserStore()


@pytest.fixture
def session_store() -> FakeSessionStore:
    return FakeSessionStore()


@pytest.fixture
def session_service(session_store: FakeSessionStore, user_store: FakeUserStore, clock: Clock):
    """A `SessionService` with deliberately SHORT, round TTLs -- 1 hour
    idle, 24 hours absolute, 1 minute touch interval -- so expiry tests
    advance the injected `clock` by obvious amounts rather than by the
    production defaults (12 hours / 7 days), which would make every
    boundary assertion read as an arbitrary large number."""
    return sessions.SessionService(
        session_store,
        user_store,
        clock,
        idle_ttl=timedelta(hours=1),
        absolute_ttl=timedelta(hours=24),
        touch_interval=timedelta(minutes=1),
    )


@pytest.fixture
def refresh_store() -> FakeRefreshTokenStore:
    return FakeRefreshTokenStore()


TEST_SIGNING_KEY = "test-signing-key-not-for-real-use-at-least-32-bytes-long"


@pytest.fixture
def token_service(clock: Clock):
    return core.TokenService(
        TEST_SIGNING_KEY,
        issuer="test-issuer",
        access_ttl=timedelta(minutes=5),
        refresh_ttl=timedelta(days=7),
        now=clock,
    )


@pytest.fixture
def password_service():
    return core.PasswordService()


@pytest.fixture
def auth_service(user_store, refresh_store, password_service, token_service, clock):
    return core.AuthService(user_store, refresh_store, password_service, token_service, clock)


# ---------------------------------------------------------------------------
# In-memory fakes: single-use tokens, lockout, email, audit events
# ---------------------------------------------------------------------------


class FakeSingleUseTokenStore:
    """In-memory `SingleUseTokenStore` -- a dict keyed by `token_hash`,
    mirroring `FakeRefreshTokenStore`'s own shape above."""

    def __init__(self) -> None:
        self._by_hash: dict[str, "core.SingleUseTokenRecord"] = {}

    async def add(self, record):
        self._by_hash[record.token_hash] = record

    async def get_by_hash(self, token_hash):
        return self._by_hash.get(token_hash)

    async def mark_used(self, token_hash, used_at):
        existing = self._by_hash[token_hash]
        self._by_hash[token_hash] = core.SingleUseTokenRecord(
            token_hash=existing.token_hash,
            user_id=existing.user_id,
            purpose=existing.purpose,
            expires_at=existing.expires_at,
            used_at=used_at,
            created_at=existing.created_at,
        )


class FakeLockoutStore:
    """In-memory `LockoutStore` -- a dict keyed by `account_key`. All the
    counting/threshold/window logic under test lives in `LockoutPolicy`
    itself; this fake is deliberately dumb persistence, matching that
    Protocol's own contract."""

    def __init__(self) -> None:
        self._by_key: dict[str, "core.AttemptRecord"] = {}

    async def get(self, account_key):
        return self._by_key.get(account_key)

    async def upsert(self, record):
        self._by_key[record.account_key] = record

    async def clear(self, account_key):
        self._by_key.pop(account_key, None)


class FakeEmailSender:
    """Recording `EmailSender` -- appends every `EmailMessage` it's asked
    to send to `self.sent`, so a test can assert what (if anything) was
    sent, to whom, and whether a raw token ended up in the body -- without
    any real email infrastructure."""

    def __init__(self) -> None:
        self.sent: list["core.EmailMessage"] = []

    async def send(self, message):
        self.sent.append(message)


class RaisingEmailSender:
    """`EmailSender` whose `send` always raises -- models a failed/
    misbehaving delivery (SMTP outage, bounced relay, timeout) for the
    M1 test: `AccountService.request_password_reset`'s known-email branch
    must swallow this and still return `None`, never let it propagate.
    Deliberately NOT a subclass/variant of `FakeEmailSender` above (kept
    as a fully separate, obviously-named fake) so a test using this one
    can never be mistaken for exercising the happy-path recording
    behavior."""

    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message):
        self.attempts += 1
        raise RuntimeError("simulated delivery failure -- SMTP relay unreachable")


@pytest.fixture
def raising_email_sender() -> RaisingEmailSender:
    return RaisingEmailSender()


class FakeAuthEventSink:
    """Recording `AuthEventSink` -- appends every emitted event as an
    `(action, {"actor": ..., "outcome": ..., **extra})` tuple to
    `self.events`, so a test can assert exactly which auth events fired
    and in what order, without any real audit-logging component wired
    up."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, action, *, actor, outcome, **extra):
        self.events.append((action, {"actor": actor, "outcome": outcome, **extra}))


@pytest.fixture
def single_use_token_store() -> FakeSingleUseTokenStore:
    return FakeSingleUseTokenStore()


@pytest.fixture
def single_use_token_service(single_use_token_store: FakeSingleUseTokenStore, clock: Clock):
    return core.SingleUseTokenService(single_use_token_store, clock)


@pytest.fixture
def lockout_store() -> FakeLockoutStore:
    return FakeLockoutStore()


@pytest.fixture
def email_sender() -> FakeEmailSender:
    return FakeEmailSender()


@pytest.fixture
def event_sink() -> FakeAuthEventSink:
    return FakeAuthEventSink()


class FakeOAuthAccountStore:
    """In-memory `OAuthAccountStore` -- a dict keyed by `(provider,
    subject)`. `link` is an UPSERT (a later call for the same key
    overwrites the row), matching `OAuthAccountStore.link`'s own
    documented contract -- NOT a rejecting-insert simulation; a real
    implementation's `INSERT ... ON CONFLICT (provider, subject) DO
    UPDATE` behaves identically, which is what lets `resolve_or_link`'s
    stale-link fallback re-link the same `(provider, subject)` pair to a
    different user without a constraint violation."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], "oauth.OAuthLinkedAccountRecord"] = {}

    async def get_by_provider_subject(self, provider, subject):
        return self._by_key.get((provider, subject))

    async def link(self, record):
        self._by_key[(record.provider, record.subject)] = record


@pytest.fixture
def oauth_account_store() -> FakeOAuthAccountStore:
    return FakeOAuthAccountStore()


@pytest.fixture
def oauth_service(user_store, oauth_account_store, password_service, auth_service, clock):
    return oauth.OAuthAccountService(user_store, oauth_account_store, password_service, auth_service, clock)


@pytest.fixture
def account_service(
    user_store: FakeUserStore,
    single_use_token_service,
    email_sender: FakeEmailSender,
    password_service,
    refresh_store: FakeRefreshTokenStore,
    clock: Clock,
    event_sink: FakeAuthEventSink,
):
    return core.AccountService(
        user_store,
        single_use_token_service,
        email_sender,
        password_service,
        refresh_store,
        clock,
        events=event_sink,
        frontend_base_url="https://app.example.com",
    )
