"""App-specific Django-ORM-backed implementations of the vendored auth
component's `UserStore`/`RefreshTokenStore`/`SingleUseTokenStore`/
`LockoutStore` protocols (`_core.py`), plus the app-level
`PasswordService`/`TokenService`/`AuthService` construction Agent B's views
call via `build_auth_service()`.

Stage 5c (#45) additionally adds this app's `DjangoEmailSender`
(`get_email_sender` — ONE class covering both dev/test and prod, unlike
`backend/fastapi`'s two-class `ConsoleEmailSender`/`SmtpEmailSender` split;
see `DjangoEmailSender`'s own docstring for why) and `AuthEventSink`
(`AuditAuthEventSink`, forwarding to the vendored audit-logging component)
implementations, plus `build_lockout_policy`/`build_account_service`
factories for the new `AccountService` (email verification + password
reset). These are PLUMBING ONLY for this stage — `build_auth_service()`
below is UNCHANGED (still wires no `lockout`/`require_verification`/
`events` into `AuthService`); wiring `AuthService`'s own new keyword
parameters into `LoginView`/`RegisterView`, and calling
`build_account_service()` from the 3 new `/auth/verify-email`,
`/auth/request-password-reset`, `/auth/reset-password` views, is Agent B's
job (see this module's own factories' docstrings for the exact call shape
each expects).

**NOT a vendored file** — it lives alongside `_core.py`/`django.py`/
`__init__.py` in this directory because that is where this app's auth
wiring naturally sits, but it imports `core.models` and
`django.conf.settings`, so it is ordinary app code (see `__init__.py`'s own
docstring for the same distinction the component's README documents:
"these import `core.models`, so they are block app code, never part of the
vendored component"). The weekly freshness audit does not touch this file.

**Async ORM only, never bare sync ORM.** Every store method below uses
Django's async QuerySet API (`.afirst()`, `.acreate()`, `.filter(...).
aupdate(...)`, `.filter(...).adelete()`) — this block's locked "stores use
Django's ASYNC ORM" decision. A bare sync ORM call (`.first()`, `.create()`,
plain `.filter(...).update(...)`) executed from inside the async context
these methods run in would raise `SynchronousOnlyOperation` — Django's async
ORM support does not silently fall back to a thread-pool bridge the way,
say, `sync_to_async`-wrapped code does; it refuses outright. Agent B's views
bridge the other direction (an ordinary DRF sync view calling into this
async `AuthService`) via `asgiref.sync.async_to_sync`, not by making these
stores sync.

Reference implementation this file's shape mirrors: `backend/fastapi`'s
`app/core/security/auth/stores.py` (SQLAlchemy `AsyncSession`-backed) — see
each class/function below for exactly what carries over unchanged and what
differs because Django's async ORM has no session object to hold.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from django.conf import settings
from django.core import mail as django_mail
from django.db import IntegrityError

from core.models import LoginAttempt, RefreshToken, Session, SingleUseToken, User
from core.security.auth import (
    AccountService,
    AttemptRecord,
    AuthService,
    EmailAlreadyExists,
    EmailMessage,
    EmailSender,
    LockoutPolicy,
    PasswordService,
    RefreshRecord,
    SessionRecord,
    SessionService,
    SingleUseTokenRecord,
    SingleUseTokenService,
    TokenService,
    UserRecord,
)
from core.security.audit_logging.audit import audit_event


def _user_to_record(user: User) -> UserRecord:
    """`roles` is stored as a JSON list (`core/models.py`'s `User.roles`)
    but the core's `UserRecord.roles` is a `tuple[str, ...]` — converted
    here at the store boundary, not inside the model, so `User.roles` stays
    a plain JSON-native `list` (what `models.JSONField` round-trips).
    Identical conversion to `app/core/security/auth/stores.py`'s own
    `_user_to_record`. `email_verified` (Stage 5c, #45) passes straight
    through — `core/models.py`'s `User.email_verified` and `_core.
    UserRecord.email_verified` are both plain booleans, no conversion
    needed."""
    return UserRecord(
        id=str(user.id),
        email=user.email,
        password_hash=user.password_hash,
        roles=tuple(user.roles),
        email_verified=user.email_verified,
    )


def _refresh_to_record(row: RefreshToken) -> RefreshRecord:
    """No `_as_utc`-style normalization needed here — see `utc_now`'s own
    docstring below for why Django's `USE_TZ=True` makes that FastAPI-side
    naive-sqlite workaround unnecessary on this track."""
    return RefreshRecord(
        token_hash=row.token_hash,
        jti=row.jti,
        family_id=row.family_id,
        user_id=str(row.user_id),
        issued_at=row.issued_at,
        expires_at=row.expires_at,
        used_at=row.used_at,
        revoked=row.revoked,
    )


def _session_to_record(row: Session) -> SessionRecord:
    """Maps a `Session` ORM row onto the vendored component's frozen
    `SessionRecord`. Django returns timezone-aware datetimes whenever
    `USE_TZ = True` (this block's setting), so no `_as_utc`-style
    normalization is needed here -- unlike backend/fastapi's SQLAlchemy
    equivalent, which must coerce because a driver can hand back a naive
    value that would then raise `TypeError` when compared against
    `utc_now()` on this exact hot path."""
    return SessionRecord(
        session_hash=row.session_hash,
        user_id=str(row.user_id),
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        absolute_expires_at=row.absolute_expires_at,
        revoked=row.revoked,
    )


def _single_use_token_to_record(row: SingleUseToken) -> SingleUseTokenRecord:
    """Stage 5c (#45) — same "no `_as_utc` needed" posture `_refresh_to_record`
    above documents; `USE_TZ=True` round-trips a tz-aware UTC `datetime` on
    every backend this block runs against (sqlite hermetic tests AND
    Postgres), so `row.expires_at`/`used_at`/`created_at` are read back
    directly with no normalization step."""
    return SingleUseTokenRecord(
        token_hash=row.token_hash,
        user_id=str(row.user_id),
        purpose=row.purpose,
        expires_at=row.expires_at,
        used_at=row.used_at,
        created_at=row.created_at,
    )


def _attempt_to_record(row: LoginAttempt) -> AttemptRecord:
    """Stage 5c (#45) — same "no `_as_utc` needed" posture as above."""
    return AttemptRecord(
        account_key=row.account_key,
        failure_count=row.failure_count,
        first_failure_at=row.first_failure_at,
        last_failure_at=row.last_failure_at,
        locked_until=row.locked_until,
    )


class DjangoUserStore:
    """Implements `_core.UserStore` against `core.models.User` via Django's
    async ORM. No session/connection object to hold, unlike
    `SqlAlchemyUserStore`'s `AsyncSession` constructor argument — Django's
    ORM manages the per-task async database connection itself, so this
    class needs no `__init__` at all; every method below is a plain
    manager/queryset call.

    A concurrent duplicate-email registration race is still caught:
    `core.models.User.email`'s DB-level `unique=True` constraint (see
    `core/migrations/0002_user_refreshtoken.py`) raises
    `django.db.utils.IntegrityError` on the `INSERT` regardless of what
    `get_by_email`'s prior read saw — `AuthService.register`'s own
    `get_by_email`-then-`create` sequence is the friendly-error path, this
    constraint is the enforcement of last resort, the identical two-layer
    posture `SqlAlchemyUserStore`'s own docstring documents."""

    # SECURITY (soft-delete auth-bypass fix, carried over from Stage 5a):
    # both lookups below query through `User.objects` -- `UserManager`
    # (core/models.py), NOT `User.all_objects` -- and `User.objects`'s own
    # `get_queryset()` override already applies `UserQuerySet.not_deleted()`
    # to EVERY lookup by default, so `.filter(email=...)`/`.filter(id=...)`
    # below inherit that scoping automatically, with no explicit
    # `deleted_at__isnull=True` needed at each call site (unlike
    # `SqlAlchemyUserStore`, which must add `User.not_deleted()` to each
    # `select()` by hand, since `AsyncRepository`'s own default scoping is
    # bypassed entirely by that store's direct `select(User)` construction —
    # see that class's own docstring). The SECURITY property is identical
    # either way: deactivating a user (soft-deleting their row) revokes
    # their ability to log in (`get_by_email`, via `AuthService.login`) and
    # to refresh (`get_by_id`, via `AuthService.refresh` step 6) — auth
    # fails CLOSED on deactivation, not open.
    #
    # NOTE (do NOT try to "fix"): an already-issued, not-yet-expired
    # *access* token remains valid until its own expiry even after
    # deactivation, because access tokens are stateless JWTs --
    # `AuthService.resolve_access` only decodes and verifies the token's
    # own signature/claims, it never re-checks the store. This is standard
    # JWT behavior, bounded by the short-lived `JWT_ACCESS_TTL_SECONDS`
    # (900s default) -- refresh denial (this fix) is what actually stops
    # session continuation past that point; there is no way to revoke a
    # single already-minted access token early without turning it into a
    # stateful token (out of scope here). Identical caveat to
    # `SqlAlchemyUserStore`'s own.
    #
    # SECURITY (Stage 13b, ban enforcement): both lookups below ALSO filter
    # on `status="active"` -- a suspended or banned user (`core/views.py`'s
    # `AdminUserSuspendView`/`AdminUserBanView`) is treated as
    # unauthenticated on the LOGIN path (`get_by_email`, via `AuthService.
    # login`) and the REFRESH path (`get_by_id`, via `AuthService.refresh`
    # step 6), composed with (not replacing) `UserManager`'s own default
    # soft-delete scoping. `get_by_id` is also what `GET /auth/me`
    # (`MeView`) resolves the caller's profile through, so this closes that
    # endpoint too for a suspended/banned caller holding a still-unexpired
    # access token -- the identical bonus `app/core/security/auth/
    # stores.py`'s own equivalent fix documents. The admin surface
    # (`core/views.py`'s `AdminUser*View` classes) never goes through
    # `DjangoUserStore` at all -- it queries `User.objects` directly,
    # entirely unfiltered by `status`, so an admin can still see/act on
    # suspended and banned accounts. Same accepted, bounded
    # access-token-TTL race the comment above documents -- `suspend`/`ban`
    # additionally call `RefreshTokenStore.revoke_all_for_user` to kill
    # every REFRESH token immediately.
    async def get_by_email(self, email: str) -> UserRecord | None:
        user = await User.objects.filter(email=email, status="active").afirst()
        return _user_to_record(user) if user is not None else None

    async def get_by_id(self, id: str) -> UserRecord | None:
        try:
            user_id = uuid.UUID(id)
        except ValueError:
            # A malformed `sub` claim (not a UUID) cannot possibly match a
            # real row -- treated as "not found", not a crash, matching
            # `_core.AuthService.refresh`'s own "user gone -> InvalidToken"
            # handling of a genuinely-missing row. Identical to
            # `SqlAlchemyUserStore.get_by_id`'s own guard.
            return None
        user = await User.objects.filter(id=user_id, status="active").afirst()
        return _user_to_record(user) if user is not None else None

    async def create(self, email: str, password_hash: str, roles: Sequence[str]) -> UserRecord:
        """Raises `EmailAlreadyExists` (the SAME exception `AuthService.
        register`'s own `get_by_email`-then-`create` sequence raises when
        its prior read finds an active duplicate) if `.acreate()` violates
        `core.models.User.email`'s DB-level `unique=True` constraint.

        SECURITY (#48, L1 -- soft-deleted-email re-registration): that
        UNIQUE constraint is full-table, NOT partial on `deleted_at
        IS NULL` -- by DECISION, a soft-deleted account's email stays
        reserved (letting someone claim a deleted user's identity is a
        security risk, and the account may still be restorable), so the
        constraint is deliberately never narrowed to free it up. But
        `get_by_email` (this store, above) queries through `User.objects`,
        which IS soft-delete-scoped (`UserManager.get_queryset`), so
        re-registering a soft-deleted user's email reads as "free" at that
        lookup, and without this `try/except` `.acreate()` below would
        raise a raw `django.db.IntegrityError` straight through
        `AuthService.register`, through the `async_to_sync` bridge
        `RegisterView.post` calls it under, to `core.exceptions.
        exception_handler`'s catch-all `Exception` branch -- a 500, AND a
        weak enumeration oracle (soft-deleted email -> 500, free email ->
        201, active-duplicate email -> 409 -- three distinct wire
        signatures for three states an attacker should not be able to tell
        apart). Routing the `IntegrityError` here to the SAME
        `EmailAlreadyExists` the active-duplicate path raises makes the
        response byte-identical (409 `conflict`) for "active email",
        "soft-deleted email", and a genuine concurrent duplicate-
        registration race (this class's own docstring already documents
        that race is caught at the DB level -- this is that catch actually
        being handled instead of left to surface as a 500).

        Caught HERE, inside this `async def`, before the exception ever
        crosses the `async_to_sync` boundary -- `EmailAlreadyExists` is
        what propagates out to `RegisterView.post` instead, and
        `async_to_sync` re-raises whatever the wrapped coroutine raised
        unchanged in the calling (sync view) thread, so `core.exceptions.
        exception_handler`'s existing `AuthError` branch maps it to 409
        `conflict` exactly as it already does for the active-duplicate
        path -- no separate handling needed on the sync side of that
        bridge. No `rollback()` call needed here (unlike the SQLAlchemy
        reference's own equivalent fix) -- this block's locked posture is
        no session/transaction object for this store to hold at all (see
        this module's own docstring), so there is no failed-transaction
        state left behind for a subsequent query to trip over the way
        `SqlAlchemyUserStore.create`'s `AsyncSession` would without its own
        explicit `rollback()`."""
        try:
            user = await User.objects.acreate(email=email, password_hash=password_hash, roles=list(roles))
        except IntegrityError as exc:
            raise EmailAlreadyExists("An account with this email already exists.") from exc
        return _user_to_record(user)

    async def mark_email_verified(self, user_id: str, at: datetime) -> None:
        """Sets `email_verified=True`/`verified_at=at` for `user_id` (Stage
        5c, #45) — a single queryset-level `.aupdate()`, not a fetch-then-
        `.save()` round trip, matching `DjangoRefreshTokenStore.mark_used`'s
        own idiom below rather than `SqlAlchemyUserStore.
        mark_email_verified`'s fetch-mutate-`flush()` shape: Django's async
        ORM has no session object to `flush()` a pending mutation through
        (see this module's own docstring), so an `.aupdate()` against
        `User.objects` (soft-delete-scoped by `UserManager.get_queryset` —
        see the SECURITY note on `get_by_email`/`get_by_id` above) is both
        simpler and already durable the instant it returns, per this file's
        established autocommit posture. `.aupdate()` on a queryset matching
        zero rows (an unknown or soft-deleted `user_id`) silently updates
        zero rows rather than raising — the SAME best-effort, no-error-path
        contract `UserStore.mark_email_verified`'s own Protocol docstring
        declares."""
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            # A caller-supplied id that isn't a UUID cannot match a real
            # row -- best-effort no-op, matching UserStore's Protocol,
            # which declares no error path for this method. Identical guard
            # to `get_by_id`'s own above.
            return
        await User.objects.filter(id=uid).aupdate(email_verified=True, verified_at=at)

    async def set_password_hash(self, user_id: str, new_hash: str) -> None:
        """Overwrites `user_id`'s stored password hash with `new_hash` (an
        already-Argon2id-hashed value — this method never hashes anything
        itself, matching `create()`'s own contract of receiving an
        already-hashed value). Same queryset-level `.aupdate()` shape as
        `mark_email_verified` above, for the identical reason."""
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return
        await User.objects.filter(id=uid).aupdate(password_hash=new_hash)


class DjangoRefreshTokenStore:
    """Implements `_core.RefreshTokenStore` against
    `core.models.RefreshToken` via Django's async ORM.

    **Durability = Django's own AUTOCOMMIT, not an explicit `commit()`
    call.** `SqlAlchemyRefreshTokenStore` (backend/fastapi) must call
    `session.commit()` explicitly after each write because SQLAlchemy's
    `AsyncSession` holds a session-scoped transaction that only `flush()`es
    (makes a change visible to later queries WITHIN that same transaction)
    by default — a `flush()` alone would not be durable against a
    concurrent, separately-connected transaction until an explicit
    `commit()` follows. Django has no equivalent session-held transaction
    to manage here: this block's locked posture is `ATOMIC_REQUESTS` left
    unset (see `config/settings.py`) and NO `transaction.atomic()` block
    anywhere around these calls (this block's own locked decision) — under
    that posture, every one of Django's async ORM write calls
    (`.acreate()`, `.filter(...).aupdate(...)`) is its OWN single
    autocommitted SQL statement, durable the instant the call returns, with
    nothing further to flush or commit. That is EXACTLY the durability
    `_core.RefreshTokenStore`'s own Protocol docstring requires:
    "Implementations MUST make `add`/`mark_used`/`revoke_family` durable
    (committed) before returning... so a concurrent second presentation of
    the just-rotated token sees the updated `used_at` and is correctly
    flagged as reuse rather than racing past this implementation's own
    write" — it falls out of Django's default autocommit mode for free,
    PROVIDED nothing wraps these calls in `transaction.atomic()` (which
    would defer the commit to the end of that block) or sets
    `ATOMIC_REQUESTS = True` (which would defer it to end-of-request,
    reintroducing exactly the race this Protocol forbids). `tests/
    test_auth_stores.py` uses `@pytest.mark.django_db(transaction=True)`
    specifically so this durability is genuinely exercised against real
    autocommit semantics, not pytest-django's own default (a wrapping
    `atomic()` block per test, rolled back at the end) — see that test
    module's own docstring.

    `add`/`mark_used`/`revoke_family` below deliberately do nothing beyond
    the single ORM call each needs — there is no `commit()` to call, unlike
    the SQLAlchemy reference."""

    async def add(self, record: RefreshRecord) -> None:
        await RefreshToken.objects.acreate(
            token_hash=record.token_hash,
            jti=record.jti,
            family_id=record.family_id,
            user_id=uuid.UUID(record.user_id),
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            used_at=record.used_at,
            revoked=record.revoked,
        )

    async def get_by_hash(self, token_hash: str) -> RefreshRecord | None:
        row = await RefreshToken.objects.filter(token_hash=token_hash).afirst()
        return _refresh_to_record(row) if row is not None else None

    async def mark_used(self, token_hash: str, used_at: datetime) -> None:
        # `_core.AuthService.refresh` only calls `mark_used()` on a row it
        # just looked up successfully in the same call -- an empty
        # queryset here would mean it vanished mid-request (not expected in
        # practice). `.aupdate()` on a queryset matching zero rows silently
        # updates zero rows rather than raising, keeping this store's
        # contract "best-effort write", matching `RefreshTokenStore`'s
        # Protocol, which declares no error path -- identical posture to
        # `SqlAlchemyRefreshTokenStore.mark_used`'s own "row is None ->
        # silently return" handling, expressed here as a no-op update
        # instead of an explicit early return.
        await RefreshToken.objects.filter(token_hash=token_hash).aupdate(used_at=used_at)

    async def revoke_family(self, family_id: str) -> None:
        await RefreshToken.objects.filter(family_id=family_id).aupdate(revoked=True)

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY refresh-token row belonging to `user_id`, across
        every family (every device/session) — see `_core.
        RefreshTokenStore.revoke_all_for_user`'s own docstring on why
        `AccountService.reset_password` calls this rather than
        `revoke_family` (which only kills the ONE family behind whichever
        token happened to be presented). Stage 5c (#45). Same single-
        `.aupdate()`-call, no-`commit()`-needed durability posture as
        `mark_used`/`revoke_family` above."""
        await RefreshToken.objects.filter(user_id=uuid.UUID(user_id)).aupdate(revoked=True)


class DjangoSessionStore:
    """Implements `_sessions.SessionStore` against `core.models.Session`
    via Django's async ORM -- the store behind this block's DEFAULT
    browser authentication path.

    Same "durability = Django's own AUTOCOMMIT, not an explicit commit()"
    posture `DjangoRefreshTokenStore` above documents at length, and it
    matters at least as much here: `SessionService.revoke` is what a
    logout endpoint's entire security promise rests on, and a revocation
    that is not durable when the response goes out is a logout that did
    not happen. Every write below is a single autocommitted statement
    (`.acreate()`, `.filter(...).aupdate()`), durable the instant it
    returns, PROVIDED nothing wraps it in `transaction.atomic()` and
    `ATOMIC_REQUESTS` stays unset -- this block's locked posture.

    `touch` is the one write on the HOT path (potentially every
    authenticated request), which is exactly why `SessionService`
    rate-limits it behind `touch_interval` rather than calling it on every
    resolve. This store does no rate-limiting of its own; it writes
    whenever it is asked to."""

    async def add(self, record: SessionRecord) -> None:
        await Session.objects.acreate(
            session_hash=record.session_hash,
            user_id=uuid.UUID(record.user_id),
            # `created_at` is written EXPLICITLY from the record rather
            # than via `auto_now_add`: it must come from the SAME injected
            # clock `SessionService` measured `absolute_expires_at`
            # against, or the row's creation time and its deadline would be
            # computed from two different "now"s.
            created_at=record.created_at,
            last_seen_at=record.last_seen_at,
            absolute_expires_at=record.absolute_expires_at,
            revoked=record.revoked,
        )

    async def get_by_hash(self, session_hash: str) -> SessionRecord | None:
        row = await Session.objects.filter(session_hash=session_hash).afirst()
        return _session_to_record(row) if row is not None else None

    async def touch(self, session_hash: str, last_seen_at: datetime) -> None:
        # A queryset matching zero rows updates zero rows rather than
        # raising -- the same "best-effort write" posture
        # `DjangoRefreshTokenStore.mark_used` documents, and especially
        # right here: failing a live, already-authenticated request because
        # a bookkeeping timestamp could not be written would turn a
        # cosmetic problem into an outage.
        await Session.objects.filter(session_hash=session_hash).aupdate(last_seen_at=last_seen_at)

    async def revoke(self, session_hash: str) -> None:
        """Marks ONE session revoked. An unknown hash updates zero rows and
        returns normally -- `SessionStore.revoke`'s documented idempotence
        contract, which `SessionService.revoke` (logout) relies on so a
        stale or forged cookie never errors, and so this endpoint never
        becomes an oracle distinguishing real session ids from invented
        ones."""
        await Session.objects.filter(session_hash=session_hash).aupdate(revoked=True)

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY session belonging to `user_id` -- every device,
        everywhere, at once. Called on a password reset (alongside
        `DjangoRefreshTokenStore.revoke_all_for_user`, so BOTH transports
        die on one reset), on a detected compromise, and on an account
        deactivation. Effective on the next request against every affected
        session -- the guarantee the JWT path can only approximate. One
        indexed `.aupdate()` (see migration 0007's index on
        `sessions.user_id`), not a scan."""
        await Session.objects.filter(user_id=uuid.UUID(user_id)).aupdate(revoked=True)


class DjangoSingleUseTokenStore:
    """Implements `_core.SingleUseTokenStore` against
    `core.models.SingleUseToken` via Django's async ORM (Stage 5c, #45).
    Same "no session/connection object to hold, no `__init__`" shape as
    `DjangoUserStore`/`DjangoRefreshTokenStore` above.

    Same durable-autocommit contract as `DjangoRefreshTokenStore`'s own
    `add`/`mark_used`/`revoke_family` (see that class's docstring): every
    ORM write call below is its own single autocommitted SQL statement,
    durable the instant it returns, with nothing to `flush()`/`commit()` —
    `_core.SingleUseTokenService.consume` relies on `mark_used` having taken
    effect before it returns, so a concurrent second presentation of the
    just-consumed token (someone clicking an already-used verify/reset link
    twice) sees the updated `used_at` and is correctly rejected as reuse."""

    async def add(self, record: SingleUseTokenRecord) -> None:
        await SingleUseToken.objects.acreate(
            token_hash=record.token_hash,
            user_id=uuid.UUID(record.user_id),
            purpose=record.purpose,
            expires_at=record.expires_at,
            used_at=record.used_at,
            created_at=record.created_at,
        )

    async def get_by_hash(self, token_hash: str) -> SingleUseTokenRecord | None:
        row = await SingleUseToken.objects.filter(token_hash=token_hash).afirst()
        return _single_use_token_to_record(row) if row is not None else None

    async def mark_used(self, token_hash: str, used_at: datetime) -> None:
        # Same "best-effort write, no error path" posture as
        # `DjangoRefreshTokenStore.mark_used` above -- `SingleUseTokenService.
        # consume` only calls this on a row it just looked up successfully.
        await SingleUseToken.objects.filter(token_hash=token_hash).aupdate(used_at=used_at)


class DjangoLockoutStore:
    """Implements `_core.LockoutStore` against `core.models.LoginAttempt`
    via Django's async ORM (Stage 5c, #45) — dumb persistence only; ALL of
    the counting/threshold/rolling-window logic lives in `_core.
    LockoutPolicy`, not here (see that class's own docstring).

    Lockout state MUST survive a process restart (a fresh connection, a new
    request hitting a different worker process) to actually do its job —
    the SAME "durability is the exact thing under test" property
    `SqlAlchemyLockoutStore`'s own docstring (backend/fastapi) documents;
    `tests/test_auth_stores.py`'s `@pytest.mark.django_db(transaction=True)`
    posture (module docstring) is what proves it genuinely, against real
    autocommit semantics rather than a per-test rolled-back transaction.

    `upsert` maintains exactly one row per `account_key`
    (`core.models.LoginAttempt.account_key` is DB-level UNIQUE, per
    migration 0003) — mirroring `SqlAlchemyLockoutStore.upsert`'s own
    accepted non-atomic read-modify-write relaxation (`_core.LockoutPolicy`'s
    own docstring: a lockout race can only ever delay when a lock becomes
    visible by a small, bounded amount, never let a wrong password succeed).
    Expressed here as `.aupdate()`-first (an `UPDATE ... WHERE account_key =
    ...` that reports how many rows it touched) rather than the SQLAlchemy
    reference's explicit `SELECT`-then-branch, since Django's async
    queryset API makes "try the update, see if anything matched" a single
    round trip instead of two — functionally identical outcome: if the
    `.aupdate()` touches zero rows (no existing row for this `account_key`
    yet), fall back to `.acreate()`; a genuine concurrent insert race for
    the SAME `account_key` (two simultaneous wrong-password requests, both
    seeing zero rows updated) is still caught at the DB level by that
    UNIQUE index (`django.db.IntegrityError` on the losing `.acreate()`),
    which falls back to one more `.aupdate()` rather than letting the
    loser's request surface a raw `IntegrityError`."""

    async def get(self, account_key: str) -> AttemptRecord | None:
        row = await LoginAttempt.objects.filter(account_key=account_key).afirst()
        return _attempt_to_record(row) if row is not None else None

    async def upsert(self, record: AttemptRecord) -> None:
        updated = await LoginAttempt.objects.filter(account_key=record.account_key).aupdate(
            failure_count=record.failure_count,
            first_failure_at=record.first_failure_at,
            last_failure_at=record.last_failure_at,
            locked_until=record.locked_until,
        )
        if updated:
            return
        try:
            await LoginAttempt.objects.acreate(
                account_key=record.account_key,
                failure_count=record.failure_count,
                first_failure_at=record.first_failure_at,
                last_failure_at=record.last_failure_at,
                locked_until=record.locked_until,
            )
        except IntegrityError:
            # A concurrent request raced this one and already inserted the
            # row for this account_key -- see this class's own docstring.
            # Fall back to updating whichever row won the race.
            await LoginAttempt.objects.filter(account_key=record.account_key).aupdate(
                failure_count=record.failure_count,
                first_failure_at=record.first_failure_at,
                last_failure_at=record.last_failure_at,
                locked_until=record.locked_until,
            )

    async def clear(self, account_key: str) -> None:
        # `.adelete()` on a queryset matching zero rows (nothing to clear)
        # is a no-op, not an error -- matching mark_used()'s/revoke_family()'s
        # own "best-effort, no error path" posture elsewhere in this module.
        await LoginAttempt.objects.filter(account_key=account_key).adelete()


def utc_now() -> datetime:
    """The single `now` callable this app passes to BOTH `TokenService` and
    `AuthService` (see `_core.AuthService.__init__`'s own docstring: "a
    caller normally passes the SAME callable to both") — a plain
    module-level function, not an inline lambda at each call site, so
    `get_token_service` below and `build_auth_service` are provably passing
    the identical behavior. Identical shape to `app/core/security/auth/
    stores.py`'s own `utc_now`.

    **Deliberate simplification vs. the FastAPI reference: no `_as_utc`
    naive-sqlite patch-up needed here.** `SqlAlchemyRefreshTokenStore`'s
    reference implementation must re-attach `timezone.utc` to every
    datetime it reads back on sqlite (its hermetic test dialect), because
    raw aiosqlite has no native timezone-aware datetime type and always
    hands SQLAlchemy back a NAIVE value regardless of what was written.
    Django's ORM sits at a different layer: with `USE_TZ = True`
    (`config/settings.py`, this block's locked setting), Django's own
    `DateTimeField` performs the naive<->aware conversion ITSELF, uniformly
    across every backend it supports — sqlite (this block's hermetic test
    dialect, via ISO-format string storage) and Postgres (prod, via a real
    `timestamptz` column) both hand back tz-aware UTC `datetime` values from
    the ORM, with no per-backend special-casing needed at the call site.
    `_refresh_to_record` (above) therefore reads `row.issued_at`/
    `expires_at`/`used_at` directly with no normalization step — this is a
    genuine simplification this Django-track module gets for free, not an
    oversight or a gap relative to the FastAPI reference."""
    return datetime.now(timezone.utc)


class AuthNotConfiguredError(RuntimeError):
    """Raised when `settings.JWT_SIGNING_KEY` is unset at the exact point
    auth is actually used (inside `get_token_service`, called from
    `build_auth_service`, called from a view) rather than at Django
    settings-module import time (see `config/settings.py`'s
    `JWT_SIGNING_KEY` line — resolved via `secret_store.get_secret(...,
    required=False)`, so it's `None`, not a hard failure, when unset — most
    of this app's routes/tests never touch auth at all). Deliberately a
    plain `RuntimeError` subclass, NOT part of `_core.AuthError`'s
    hierarchy — this is a SERVER misconfiguration, not a client-caused auth
    failure, so it must NOT be caught by `AUTH_ERROR_HTTP`/an `AppError`-
    style mapping and rendered as a 401/409; left unhandled, it reaches
    `core.exceptions.exception_handler`'s catch-all `Exception` branch and
    renders the generic `internal_error` envelope at 500 — "fail closed"
    without ever constructing a `TokenService` with an empty/absent signing
    key (which `TokenService.__init__` itself also refuses, per `_core.py`'s
    own `ValueError` guard — this is the layer above that, for the
    `None`-vs-empty-string case `TokenService` cannot see because it's
    never even called). Identical shape and rationale to `app/core/
    security/auth/stores.py`'s own `AuthNotConfiguredError`."""


@lru_cache
def get_password_service() -> PasswordService:
    """One process-wide `PasswordService` — see that class's own docstring
    on why: its `dummy_verify()` timing defense depends on a precomputed
    throwaway hash computed ONCE at construction (a real Argon2id hash, not
    a cheap operation) so every login's "email not found" path costs the
    same wall-clock time as a real `verify()` call; constructing a fresh
    instance per request would pay that Argon2id cost on every single
    request, not just once at process start. Identical to `app/core/
    security/auth/stores.py`'s own `get_password_service`."""
    return PasswordService()


def get_token_service() -> TokenService:
    """Builds a `TokenService` from `django.conf.settings` directly —
    unlike the FastAPI reference's `get_token_service(settings: Settings)`,
    this takes NO argument: this block has no per-request `Settings`
    object to thread through (see `config/settings.py`'s own module
    docstring — this block reads `django.conf.settings`, Django's own
    global settings object, everywhere, rather than a pydantic `Settings`
    instance constructed per-app). Cheap to construct (holds config values
    only, no heavy crypto in `__init__`, unlike `PasswordService` above),
    so a fresh instance per call is fine.

    Raises `AuthNotConfiguredError` if `settings.JWT_SIGNING_KEY` is falsy
    (`None` or an empty string) — see that exception's own docstring for
    the fail-closed rationale."""
    if not settings.JWT_SIGNING_KEY:
        raise AuthNotConfiguredError(
            "JWT_SIGNING_KEY is not configured. Set the JWT_SIGNING_KEY environment "
            "variable (or its AWS Secrets Manager equivalent -- see "
            "core.contract.secret_store.get_secret()) before any auth endpoint is used."
        )
    return TokenService(
        settings.JWT_SIGNING_KEY,
        issuer=settings.JWT_ISSUER,
        access_ttl=timedelta(seconds=settings.JWT_ACCESS_TTL_SECONDS),
        refresh_ttl=timedelta(seconds=settings.JWT_REFRESH_TTL_SECONDS),
        now=utc_now,
    )


def build_auth_service() -> AuthService:
    """Constructs a fresh `AuthService`, wired to this module's
    Django-async-ORM-backed `DjangoUserStore`/`DjangoRefreshTokenStore`,
    the process-wide `get_password_service()`, and a `get_token_service()`
    built from the current `django.conf.settings`. This is what Agent B's
    views call — a DRF view (ordinarily synchronous) bridges into the
    returned `AuthService`'s async methods via `asgiref.sync.
    async_to_sync(...)`, the same bridge `tests/test_auth_stores.py` uses
    to exercise the stores below directly.

    No arguments, unlike `app/core/security/auth/stores.py`'s
    FastAPI-dependency-shaped `get_auth_service` (which takes an
    `AsyncSession` from `Depends(get_db)`) — Django's async ORM has no
    per-request session object for a caller to hand in; each store method
    manages its own database access per call. A fresh `AuthService` (and
    fresh stores) is constructed on every call rather than cached, since
    `DjangoUserStore`/`DjangoRefreshTokenStore` are stateless (no
    `__init__` at all) and `get_token_service()` is itself cheap — see that
    function's own docstring. Raises `AuthNotConfiguredError` (via
    `get_token_service()`) if `settings.JWT_SIGNING_KEY` is unset."""
    return AuthService(
        users=DjangoUserStore(),
        refresh_tokens=DjangoRefreshTokenStore(),
        passwords=get_password_service(),
        tokens=get_token_service(),
        now=utc_now,
    )


async def seed_admin(email: str, password: str) -> UserRecord:
    """Creates a user with `roles=["admin"]` (Stage 5d, #46) -- the real
    admin-provisioning path (run by hand via `manage.py seed_admin`, see
    `core/management/commands/seed_admin.py`, or by a test fixture), and
    deliberately the ONLY place in this app that ever constructs a user
    with an elevated role. Mirrors `app/core/security/auth/stores.py`'s
    identically-named FastAPI counterpart function-for-function.

    **Why this exists, and why `POST /auth/register` never accepts a
    caller-supplied `roles` field.** `RegisterRequestSerializer`
    (`core/serializers.py`) has no `roles` field, and `RegisterView`
    (`core/views.py`) always calls `AuthService.register` with its default
    `roles=()` -- a client that could pass its own `roles` on the wire
    could self-grant `"admin"` on registration, a straightforward
    privilege-escalation bug. This function is the ONE place `DjangoUserStore.
    create(..., roles=["admin"])` is ever called with a non-empty role list
    from this app's own code -- an operator (or a test's own setup fixture)
    invokes it directly, server-side; it is never reachable from any HTTP
    request body.

    Bypasses `AuthService.register` itself (rather than calling it with
    `roles=["admin"]`, even though that parameter exists) for the SAME
    reason `app/core/security/auth/stores.py`'s own `seed_admin` bypasses
    it: mirroring `AuthService.register`'s own shape (hash the password via
    the process-wide `PasswordService`, `UserStore.create`) by hand keeps
    this the one obvious place a non-empty role list is ever constructed,
    rather than threading an elevated-role argument through the same
    general-purpose method `RegisterView` calls with a caller-supplied
    email/password. No explicit commit needed here, unlike the FastAPI
    counterpart's own `await session.commit()` -- `DjangoUserStore.create`
    (`.acreate()`) is already durable the instant it returns, per this
    module's own "async ORM ... already durable" posture documented on
    `mark_email_verified` above; there is no session/flush boundary on this
    track for a script-context caller (no enclosing HTTP request) to need
    to close out."""
    normalized = email.strip().lower()
    password_hash = get_password_service().hash(password)
    return await DjangoUserStore().create(normalized, password_hash, roles=["admin"])


# ---------------------------------------------------------------------------
# Email seam (Stage 5c, #45): DjangoEmailSender -- ONE class, unlike
# backend/fastapi's ConsoleEmailSender/SmtpEmailSender split
# ---------------------------------------------------------------------------


_email_logger = logging.getLogger("auth.email.django")


# In-process bounded thread pool this app's `DjangoEmailSender` delivers
# through -- see that class's own docstring for the full WHY. Module-level
# (not per-instance) so every `DjangoEmailSender()` this process ever
# constructs (`build_account_service()` builds a fresh one per call, see
# `get_email_sender`'s own docstring) shares ONE bounded pool rather than
# each spinning up its own unbounded set of OS threads -- `max_workers=4`
# caps how much concurrent blocking mail I/O (SMTP handshakes, etc.) this
# process will run at once, matching the "bounded" half of this fix's own
# name. `thread_name_prefix="auth-email"` makes these threads identifiable
# in a stack dump / `py-spy` trace / thread-count metric, distinct from
# gunicorn's own sync worker threads.
_email_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="auth-email")

# Strong references to in-flight delivery futures, module-level (shared by
# every `DjangoEmailSender` instance, mirroring `_email_executor` above) --
# a `Future` returned by `ThreadPoolExecutor.submit()` is NOT at risk of
# garbage collection the way a bare `asyncio.create_task()` result is (the
# executor itself keeps the underlying work alive until it completes), but
# this set is still what `flush_pending_email_deliveries` below waits on,
# and it doubles as the single place every delivery's terminal
# exception is logged (via the `done_callback` registered in `send()`
# below), regardless of which `DjangoEmailSender` instance submitted it.
_pending_deliveries: set[Future[None]] = set()


def _on_delivery_done(future: Future[None]) -> None:
    """`done_callback` registered on every future `DjangoEmailSender.send`
    submits -- runs in the POOL THREAD that just finished the delivery
    attempt (`ThreadPoolExecutor`'s own documented callback semantics),
    never on the request thread/event loop that called `send()`. Discards
    the future from `_pending_deliveries` (mirroring `SmtpEmailSender`'s
    own `_tasks.discard` `add_done_callback`) and logs any exception that
    escaped `_deliver_sync`'s own `try/except` -- belt-and-braces only;
    `_deliver_sync` already catches and logs everything itself, so
    `future.exception()` is expected to be `None` here in the normal case."""
    _pending_deliveries.discard(future)
    exc = future.exception()
    if exc is not None:
        _email_logger.warning("Unhandled exception delivering auth email", exc_info=exc)


def flush_pending_email_deliveries(timeout: float | None = None) -> None:
    """Blocks until every delivery future that was pending AT THE MOMENT
    THIS IS CALLED has finished (or `timeout` seconds elapse, per future).
    Two intended callers:

    1. **Tests** -- the real `DjangoEmailSender` (below) is fire-and-forget
       by contract (see its own docstring), so a test driving a real HTTP
       request through `POST /auth/register` and then wanting to assert on
       `django.core.mail.outbox` needs a deterministic point at which
       delivery is GUARANTEED to have already happened, without a flaky
       `time.sleep`. This is that point.
    2. **Graceful shutdown** -- a project wiring this app's shutdown
       sequence (e.g. a `SIGTERM` handler, an ASGI lifespan shutdown event)
       can call this to give in-flight deliveries a chance to finish before
       the process actually exits, rather than the pool being torn down
       out from under them. This is still BEST-EFFORT, not a durability
       guarantee -- see `DjangoEmailSender`'s own docstring's
       "best-effort-on-process-shutdown" caveat: a worker that is killed
       (not given a chance to run its shutdown hook at all -- e.g. `SIGKILL`,
       an OOM-kill, a recycled gunicorn worker mid-request) loses whatever
       was still pending regardless of this function.

    Snapshots `_pending_deliveries` before waiting (`list(...)`, a copy) so
    a delivery that gets scheduled by some OTHER concurrent request while
    this call is already waiting does not extend how long this particular
    call blocks -- callers that need to wait for deliveries scheduled
    after this call started should call this again."""
    for future in list(_pending_deliveries):
        try:
            future.result(timeout=timeout)
        except Exception:
            # A delivery failure (or a timeout) here has already been
            # logged by `_on_delivery_done` above -- this call's job is
            # only to WAIT, not to re-raise or re-log.
            pass


class DjangoEmailSender:
    """The single `_core.EmailSender` implementation this Django track
    needs, for both dev/test AND prod — built on Django's OWN pluggable
    `django.core.mail` backend system (`settings.EMAIL_BACKEND` —
    `config/settings.py`) rather than the two-class split (`_core.
    ConsoleEmailSender` vendored for dev/test vs. a hand-rolled
    `SmtpEmailSender` for prod) `app/core/security/auth/stores.py` needs on
    the FastAPI track. Django already ships a console-vs-SMTP-vs-anything
    `EmailBackend` abstraction (`django.core.mail.backends.{console,smtp,
    ...}.EmailBackend`) resolved from `settings.EMAIL_BACKEND` at SEND time
    by `django.core.mail.EmailMessage.send()` itself — this class never
    branches on which backend is configured, and never imports `smtplib`
    directly the way `SmtpEmailSender` does. `settings.EMAIL_BACKEND`
    defaults to Django's own `django.core.mail.backends.console.
    EmailBackend` (`config/settings.py`) — the SAME dev-only "print the raw
    token instead of delivering it" convenience `_core.ConsoleEmailSender`'s
    own docstring describes, just reached via Django's own backend
    machinery rather than a second Python class.

    **Delivered via a bounded thread pool, NOT `asyncio.create_task` --
    this is the fix for a HIGH-severity availability bug, read this
    carefully before touching it again.** An earlier version of this class
    scheduled delivery with `asyncio.create_task(self._deliver(message))`,
    the same pattern `SmtpEmailSender` (backend/fastapi) correctly uses.
    That pattern depends on the event loop that created the task still
    being alive, and still pumping, at some point AFTER the task is
    created -- true for `backend/fastapi`, which runs under uvicorn's own
    long-lived, continuously-pumped event loop. It is FALSE here: this
    app's shipped deployment (`backend/django/Dockerfile`, `docker-
    compose.yml`) runs `gunicorn config.wsgi:application` -- plain
    synchronous WSGI workers, never uvicorn/ASGI. Every DRF view that ends
    up calling `AccountService.request_email_verification`/
    `request_password_reset` (which call this class's `send()`) is an
    ordinary SYNC view that bridges into the async `AccountService` via
    `asgiref.sync.async_to_sync(...)` (see `build_account_service`'s own
    docstring). `async_to_sync` creates a FRESH event loop for that one
    call, drives the awaited coroutine to completion, and then TEARS THAT
    LOOP DOWN before returning control to the sync view -- and because
    neither `request_email_verification` nor `request_password_reset`
    awaits anything else after `send()` returns, a task merely SCHEDULED
    (not yet run) by `asyncio.create_task` inside that call never gets a
    chance to actually execute: the loop it was scheduled on is gone the
    instant `async_to_sync` returns. The result, under this app's default
    `AUTH_REQUIRE_EMAIL_VERIFICATION=True`: verification emails (and
    password-reset emails) were SILENTLY NEVER DELIVERED -- every new
    account was permanently unable to verify (and therefore unable to log
    in), and the reset-based recovery path was equally dead. An
    availability/auth denial-of-service, not a data-correctness bug -- the
    anti-enumeration and generic-401 SECURITY properties were unaffected
    either way, only delivery was silently dropped.

    A `concurrent.futures.ThreadPoolExecutor` (module-level `_email_
    executor` above) sidesteps this entirely: `send()` calls `_email_
    executor.submit(...)`, which starts running `_deliver_sync` on a REAL
    OS thread immediately -- that thread is not owned by, and does not
    depend on, whichever asyncio event loop happened to be running when
    `send()` was called (or whether one is running at all by the time the
    work finishes). It survives `async_to_sync`'s loop teardown by
    construction, and the identical `send()`/`_deliver_*` shape keeps
    working unchanged if this app later adds a real ASGI view path
    (`config/asgi.py` already exists for that -- see the `Dockerfile`'s own
    comment) since a thread pool works under both WSGI and ASGI, unlike
    `asyncio.create_task`, which only works when a loop is guaranteed to
    keep pumping.

    **Fire-and-forget by contract, still** — the SAME `_core.EmailSender`
    Protocol requirement as before (see `SmtpEmailSender`'s own docstring,
    backend/fastapi): `send()` SUBMITS delivery and returns immediately —
    it does NOT wait for the result, and it NEVER raises. This is what
    `_core.EmailSender`'s own Protocol docstring requires ("implementations
    MUST NOT let delivery latency or delivery failure affect the caller")
    and what `_core.AccountService.request_password_reset`'s
    anti-enumeration defense and `register`'s post-registration
    verification-email call both depend on. `_deliver_sync` (a plain SYNC
    method, running in the pool thread -- no event loop involved at all,
    so no `sync_to_async`/`anyio.to_thread` bridge is needed the way the
    old `asyncio`-task version needed one) performs the actual, potentially
    network-blocking `django.core.mail` send inside a `try/except Exception`
    that only LOGS (`_email_logger`, `warning` level) -- nothing above that
    method ever sees the exception, because nothing above it is waiting on
    this future's result at all (`send()` itself never calls `.result()`).

    **In-flight future lifetime**: `send()` registers every future it
    submits in the module-level `_pending_deliveries` set (added on
    submit, discarded by `_on_delivery_done`'s `done_callback` once the
    future finishes, success or already-caught-internally failure) --
    this is what `flush_pending_email_deliveries` (module-level, above)
    waits on, both for tests and as an optional graceful-shutdown hook.
    Unlike `asyncio.create_task`'s bare-reference footgun, a
    `ThreadPoolExecutor` future is not at risk of being garbage-collected
    mid-flight even without this set (the executor itself keeps the work
    alive) -- `_pending_deliveries` exists for `flush_pending_email_
    deliveries` to have something to wait on, not to prevent GC.

    **Best-effort on process shutdown, still an accepted caveat.** A
    delivery still running in a pool thread when the process is killed
    (not given a chance to shut down gracefully -- `SIGKILL`, an OOM-kill,
    a gunicorn worker recycled mid-request) is lost, same as any other
    in-process background send -- this is the SAME class of caveat every
    fire-and-forget in-process sender has (identical to the old
    `asyncio.create_task` version's own "best-effort on shutdown" note,
    and to `SmtpEmailSender`'s), just no longer compounded by the
    `async_to_sync`-teardown bug above. A project that needs a hard
    delivery guarantee (survives a crash, retried, durable) should replace
    this with a real queue/outbox -- `_core.EmailSender`'s Protocol is the
    seam that swap happens behind, unchanged for either `AccountService`
    caller; that is a Stage-11-and-later recipe, not something this stage
    adds."""

    async def send(self, message: EmailMessage) -> None:
        """Submits delivery to the module-level bounded thread pool and
        returns immediately -- does NOT await/wait for the result, does
        NOT raise. Declared `async def` (rather than a plain sync method)
        because it implements `_core.EmailSender`'s `Protocol`, which
        declares `send` as `async def` -- see that Protocol's own
        docstring. See this class's own docstring for why a thread pool,
        not `asyncio.create_task`, is what actually runs the delivery."""
        future = _email_executor.submit(self._deliver_sync, message)
        _pending_deliveries.add(future)
        future.add_done_callback(_on_delivery_done)

    def _deliver_sync(self, message: EmailMessage) -> None:
        """Runs on a pool thread (submitted by `send()` above) -- a plain
        SYNC method, never `await`ed, with no event loop involved at all.
        BLOCKING I/O under the SMTP backend is exactly what a real OS
        thread is for. `fail_silently=False` so a delivery failure raises
        here, caught by this method's own `try/except` and only LOGGED
        (`_email_logger`, `warning` level) -- rather than Django's own
        `EmailMessage.send(fail_silently=True)` posture, which would
        swallow the error one layer BELOW where this class's own logging
        happens, silently. Calls Django's sync mail API directly (no
        `sync_to_async` wrapper needed -- this is already a real thread,
        not asyncio-scheduled work)."""
        try:
            django_mail.EmailMessage(
                subject=message.subject,
                body=message.body,
                from_email=settings.EMAIL_FROM,
                to=[message.to],
            ).send(fail_silently=False)
        except Exception:
            _email_logger.warning(
                "Failed to deliver email to %s (subject=%r)", message.to, message.subject, exc_info=True
            )


def get_email_sender() -> EmailSender:
    """Returns a `DjangoEmailSender` — see that class's own docstring for
    why, unlike `app/core/security/auth/stores.py`'s own `get_email_sender
    (settings)`, this Django-track function takes NO argument (matching
    `get_token_service()`'s own "no per-request `Settings` object to thread
    through" posture above) and never branches between two sender classes:
    `DjangoEmailSender` itself defers console-vs-SMTP selection entirely to
    `django.core.mail`'s own `settings.EMAIL_BACKEND` at send time, so this
    function's job is simply to construct one."""
    return DjangoEmailSender()


# ---------------------------------------------------------------------------
# Audit seam (Stage 5c, #45): AuditAuthEventSink forwards to the vendored
# audit-logging component's audit_event()
# ---------------------------------------------------------------------------


class AuditAuthEventSink:
    """Implements `_core.AuthEventSink` by forwarding every call to the
    vendored audit-logging component's `audit_event(...)`
    (`core/security/audit_logging/audit.py`) — this is the "thin adapter"
    `_core.AuthEventSink`'s own docstring describes a project wiring, kept
    as app code (not part of `_core.py`) so that module stays at "stdlib +
    PyJWT + argon2-cffi only, zero framework/app import". Identical shape
    and rationale to `app/core/security/auth/stores.py`'s own
    `AuditAuthEventSink` (backend/fastapi).

    `action`/`actor`/`outcome` pass straight through — `_core.py` already
    constructs `actor` as a bare opaque id string (a user id, `"anonymous"`,
    or `"unknown"`/`"user:unknown"` for a path with no trustworthy
    principal), so this sink never receives, and therefore can never leak,
    a raw token, password, or email address as a field. `audit_event`'s own
    redaction is a second, independent line of defense on top of that, not
    the only one. `resource` is a fixed `"auth"` string, not per-event —
    every event this sink ever receives IS an auth-subsystem event acting
    on the actor's own account.

    `audit_event` (unlike `_core.AuthEventSink.emit`) is a plain SYNCHRONOUS
    function — no network/DB I/O, just a structured `logging` call (see
    that function's own docstring) — so this method calls it directly,
    with no `await`, despite `emit` itself being declared `async def` to
    satisfy the `AuthEventSink` Protocol.

    **What `outcome="success"` means for `auth.email.verify_requested`/
    `auth.password.reset_requested`** (emitted by `_core.py`, forwarded
    here unchanged): it denotes that the request was accepted and delivery
    was DISPATCHED to `DjangoEmailSender` — never that the email was
    actually delivered. `DjangoEmailSender.send` is fire-and-forget by the
    `EmailSender` Protocol's own contract (see that class's docstring), so
    this sink is never in a position to know the real delivery outcome by
    the time it emits. A genuine delivery failure IS still attempted and
    logged — by `DjangoEmailSender._deliver_sync`, on its own pool thread,
    at `_email_logger` `warning` level — just as a separate log line, not
    as a different `outcome` on this audit event."""

    async def emit(self, action: str, *, actor: str, outcome: str, **extra: object) -> None:
        audit_event(action, actor=actor, resource="auth", outcome=outcome, **extra)


# ---------------------------------------------------------------------------
# AccountService factories (Stage 5c, #45) — NEW, alongside build_auth_
# service() above. Not yet called from anywhere in this stage -- Agent B's
# job (see this module's own module docstring).
# ---------------------------------------------------------------------------


def build_lockout_policy() -> LockoutPolicy | None:
    """Returns a `LockoutPolicy` backed by `DjangoLockoutStore()` when
    `settings.AUTH_LOCKOUT_ENABLED` is `True`, `None` when it isn't — both
    `AuthService` and `AccountService` treat a `None` lockout as "not
    wired, skip lockout entirely" (see each class's own `lockout` parameter
    docstring in `_core.py`), so this single function is the one place that
    decision is made, callable identically for either service's wiring.
    Identical decision to `app/core/security/auth/stores.py`'s own
    `build_lockout_policy(settings, session)` (backend/fastapi) — no
    `settings`/`session` arguments here, matching `get_token_service()`'s
    own no-argument, `django.conf.settings`-reading posture above (Django's
    async ORM has no per-request session object for a caller to hand in
    either, unlike a SQLAlchemy `AsyncSession`).

    Agent B's `LoginView` wiring (the next stage's endpoint work) should
    call this SAME function when it builds its own `AuthService(lockout=...)`
    — sharing one `LockoutPolicy` instance (or at least one built against
    the same underlying `DjangoLockoutStore`/table) is what lets a
    successful `AccountService.reset_password` lift a lockout `AuthService.
    login` had recorded against the same account (see `_core.
    AccountService.__init__`'s own docstring on its `lockout` parameter)."""
    if not settings.AUTH_LOCKOUT_ENABLED:
        return None
    return LockoutPolicy(
        DjangoLockoutStore(),
        max_failures=settings.AUTH_LOCKOUT_MAX_FAILURES,
        lockout_duration=timedelta(seconds=settings.AUTH_LOCKOUT_DURATION_SECONDS),
        window=timedelta(seconds=settings.AUTH_LOCKOUT_WINDOW_SECONDS),
        now=utc_now,
    )


def build_session_service() -> SessionService:
    """Builds a fresh `SessionService` — the provider behind this block's
    DEFAULT browser authentication path. Same composition shape (and the
    same no-argument, `django.conf.settings`-reading posture) as
    `build_auth_service()`/`build_account_service()` above: stateless
    stores, and `utc_now` as the single shared clock every other service in
    this block's auth composition also uses, so a session's deadlines and a
    token's expiry are measured against the same "now".

    Takes `DjangoUserStore` as well as `DjangoSessionStore` because
    `SessionService.resolve` reads the user's CURRENT roles on every
    request rather than trusting a snapshot — that second read is what
    makes a role grant or revocation take effect immediately, and it is the
    deliberate cost of this path over a JWT's baked-in `roles` claim (see
    `_sessions.py`'s module docstring).

    **Needs no signing key**, unlike `get_token_service()` above — a
    session id is opaque and unguessable rather than signed, so there is
    nothing to configure wrong and no `AuthNotConfiguredError` equivalent
    to fail closed on. That is one fewer secret to manage in every
    environment for a project serving only browser clients."""
    return SessionService(
        DjangoSessionStore(),
        DjangoUserStore(),
        utc_now,
        idle_ttl=timedelta(seconds=settings.SESSION_IDLE_TTL_SECONDS),
        absolute_ttl=timedelta(seconds=settings.SESSION_ABSOLUTE_TTL_SECONDS),
        touch_interval=timedelta(seconds=settings.SESSION_TOUCH_INTERVAL_SECONDS),
        events=AuditAuthEventSink(),
    )


def build_account_service(*, email: EmailSender | None = None) -> AccountService:
    """Builds a fresh `AccountService`, the SAME composition shape
    `build_auth_service()` above uses for `AuthService` — a fresh
    `DjangoUserStore`/`DjangoRefreshTokenStore`/`DjangoSingleUseTokenStore`
    (each stateless, no `__init__`/session to hold — see this module's own
    docstring), the process-wide `get_password_service()`, `utc_now` as the
    single shared clock, `AuditAuthEventSink()` for `events`, `build_lockout_
    policy()` for `lockout` (built against the SAME underlying
    `DjangoLockoutStore` table `AuthService`'s own wiring should use — see
    that function's own docstring), and `settings.FRONTEND_BASE_URL`/
    `AUTH_VERIFY_TTL_SECONDS`/`AUTH_RESET_TTL_SECONDS` for the link-building
    and TTL configuration. No arguments beyond the keyword-only `email`
    below, matching `build_auth_service()`'s own no-argument,
    `django.conf.settings`-reading posture.

    `email` (Stage 5c #45 endpoint work, keyword-only): the `EmailSender` to
    use — `None` (the default) resolves it via `get_email_sender()`. A
    caller that already has one resolved from elsewhere (e.g. a test's own
    fake `EmailSender`) passes it directly instead. Identical seam to
    `app/core/security/auth/stores.py`'s own `build_account_service(...,
    email=...)` (backend/fastapi).

    This is what Agent B's `/auth/verify-email`, `/auth/
    request-password-reset`, `/auth/reset-password` views call — a DRF view
    bridges into the returned `AccountService`'s async methods via
    `asgiref.sync.async_to_sync(...)`, the same bridge `build_auth_service()`
    above already documents for `AuthService`. Raises `AuthNotConfiguredError`
    if a caller happens to also need `get_token_service()`/`build_auth_
    service()` in the same view and `settings.JWT_SIGNING_KEY` is unset --
    `build_account_service()` itself never touches `JWT_SIGNING_KEY` (it has
    no `TokenService` dependency at all), so it never raises that error on
    its own."""
    return AccountService(
        users=DjangoUserStore(),
        tokens=SingleUseTokenService(DjangoSingleUseTokenStore(), now=utc_now),
        email=email if email is not None else get_email_sender(),
        passwords=get_password_service(),
        refresh_tokens=DjangoRefreshTokenStore(),
        now=utc_now,
        events=AuditAuthEventSink(),
        lockout=build_lockout_policy(),
        # Both revocation seams are wired, deliberately: reset_password
        # already revoked every refresh-token family, and this makes it
        # revoke every server-side session too. Wiring only one would
        # leave a reset account HALF-revoked -- an attacker's session
        # cookie would keep authenticating despite every JWT dying.
        sessions=build_session_service(),
        frontend_base_url=settings.FRONTEND_BASE_URL,
        verify_ttl=timedelta(seconds=settings.AUTH_VERIFY_TTL_SECONDS),
        reset_ttl=timedelta(seconds=settings.AUTH_RESET_TTL_SECONDS),
    )
