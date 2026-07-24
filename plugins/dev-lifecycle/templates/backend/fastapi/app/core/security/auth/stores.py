"""App-specific SQLAlchemy-backed implementations of the vendored auth
component's `UserStore`/`RefreshTokenStore`/`SingleUseTokenStore`/
`LockoutStore` protocols (`_core.py`), plus the app-level
`PasswordService`/`TokenService` construction this block's
`app/api/deps.py:get_auth_service` binds into a per-request `AuthService`.

Stage 5c (#45) additionally adds this app's `EmailSender` (`get_email_sender`
-- `ConsoleEmailSender` in dev/test, a hand-rolled `SmtpEmailSender` once
SMTP is configured) and `AuthEventSink` (`AuditAuthEventSink`, forwarding to
the vendored audit-logging component) implementations, plus
`build_lockout_policy`/`build_account_service` factories for the new
`AccountService` (email verification + password reset). `app/api/deps.py`'s
`get_auth_service` now also wires `lockout=build_lockout_policy(...)`,
`require_verification=settings.auth_require_email_verification`, and
`events=AuditAuthEventSink()` into the `AuthService` it builds, and a new
`get_account_service` dependency (same module) builds an `AccountService`
via `build_account_service` for the `/auth/verify-email`, `/auth/request-
password-reset`, `/auth/reset-password` routes (`app/api/routers/auth.py`).

**NOT a vendored file** — it lives alongside `_core.py`/`fastapi.py`/
`__init__.py` in this directory because that is where this app's auth
wiring naturally sits, but it imports `app.models` and `app.core.config`,
so it is ordinary app code (see `__init__.py`'s own docstring for the same
distinction the component's README documents: "these import `app.models`,
so they are block app code, never part of the vendored component"). The
weekly freshness audit does not touch this file.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage as MimeEmailMessage
from functools import lru_cache

import anyio.to_thread
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security.audit_logging.audit import audit_event
from app.core.security.auth import (
    AccountService,
    AttemptRecord,
    ConsoleEmailSender,
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
from app.models.login_attempt import LoginAttempt
from app.models.refresh_token import RefreshToken
from app.models.session import Session
from app.models.single_use_token import SingleUseToken
from app.models.user import User


def _user_to_record(user: User) -> UserRecord:
    """`roles` is stored as a JSON list (`app/models/user.py`) but the
    core's `UserRecord.roles` is a `tuple[str, ...]` — converted here at
    the store boundary, not inside the model, so `User.roles` stays a
    plain JSON-native `list` (what the `JSON` column type round-trips)."""
    return UserRecord(
        id=str(user.id),
        email=user.email,
        password_hash=user.password_hash,
        roles=tuple(user.roles),
        email_verified=user.email_verified,
    )


def _as_utc(value: datetime | None) -> datetime | None:
    """Normalizes a datetime read back from the DB to timezone-AWARE UTC.

    sqlite (this app's hermetic test dialect, `aiosqlite`) has no native
    timezone-aware datetime type — SQLAlchemy's `DateTime(timezone=True)`
    columns (`RefreshToken.issued_at`/`expires_at`/`used_at`) round-trip
    correctly on PostgreSQL (a real `timestamptz` column, always tz-aware
    coming back), but on sqlite the value comes back NAIVE regardless of
    what was written. `_core.AuthService.refresh` compares `row.
    expires_at <= self._now()`, and `self._now()` is always tz-aware (per
    `TokenService`/`AuthService`'s own documented `now` contract) —
    comparing an aware and a naive `datetime` raises `TypeError` at that
    comparison, not a silently-wrong result, so this normalization is
    required for correctness under sqlite, not just cosmetic. Every
    datetime this store EVER writes is UTC to begin with (`_core.py`'s
    `TokenService`/`AuthService` only ever construct `now()` as UTC-aware
    — see their own docstrings), so re-attaching `timezone.utc` to a value
    that came back naive is recovering known-lost information, not
    guessing: sqlite dropped the offset, it never had a different one."""
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _refresh_to_record(row: RefreshToken) -> RefreshRecord:
    return RefreshRecord(
        token_hash=row.token_hash,
        jti=row.jti,
        family_id=row.family_id,
        user_id=str(row.user_id),
        issued_at=_as_utc(row.issued_at),
        expires_at=_as_utc(row.expires_at),
        used_at=_as_utc(row.used_at),
        revoked=row.revoked,
    )


def _session_to_record(row: Session) -> SessionRecord:
    """Maps a `Session` ORM row onto the vendored component's frozen
    `SessionRecord`. Every timestamp goes through `_as_utc` for the same
    reason `_refresh_to_record` does: `SessionService.resolve` compares
    them against `utc_now()` (an aware datetime), and comparing an aware
    to a naive datetime raises `TypeError` — which, on this code path,
    would turn every authenticated request into a 500 the moment a driver
    or dialect handed back a naive value."""
    return SessionRecord(
        session_hash=row.session_hash,
        user_id=str(row.user_id),
        created_at=_as_utc(row.created_at),
        last_seen_at=_as_utc(row.last_seen_at),
        absolute_expires_at=_as_utc(row.absolute_expires_at),
        revoked=row.revoked,
    )


def _single_use_token_to_record(row: SingleUseToken) -> SingleUseTokenRecord:
    return SingleUseTokenRecord(
        token_hash=row.token_hash,
        user_id=str(row.user_id),
        purpose=row.purpose,
        expires_at=_as_utc(row.expires_at),
        used_at=_as_utc(row.used_at),
        created_at=_as_utc(row.created_at),
    )


def _attempt_to_record(row: LoginAttempt) -> AttemptRecord:
    return AttemptRecord(
        account_key=row.account_key,
        failure_count=row.failure_count,
        first_failure_at=_as_utc(row.first_failure_at),
        last_failure_at=_as_utc(row.last_failure_at),
        locked_until=_as_utc(row.locked_until),
    )


class SqlAlchemyUserStore:
    """Implements `_core.UserStore` against `app/models/user.py`'s `User`
    and this request's `AsyncSession`. Does NOT commit — matches
    `db-session/`'s `get_db()` session-per-request commit/rollback
    boundary (see that module's docstring); this store only flushes so a
    caller within the same request sees consistent (DB-generated id/
    timestamps) state. A concurrent duplicate-email registration race is
    still caught: `users.email`'s DB-level UNIQUE index (Alembic 0002)
    raises `IntegrityError` on the `INSERT` regardless of what
    `get_by_email`'s prior read saw."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # SECURITY (soft-delete auth bypass fix): both lookups below
    # deliberately honor soft-delete, matching `AsyncRepository._base_select`'s
    # own default (`include_deleted=False`) -- the rest of this app already
    # treats `deleted_at IS NOT NULL` as "gone", and these two auth lookups
    # are the ONLY reads of `User` that bypass `AsyncRepository` entirely
    # (they're constructed directly against `select(User)`, per this
    # store's own class docstring), so they must apply `User.not_deleted()`
    # by hand rather than inherit it for free. Without this, deactivating a
    # user (soft-deleting their row) would NOT revoke their ability to log
    # in (`get_by_email`, via `AuthService.login`) or to refresh
    # (`get_by_id`, via `AuthService.refresh` step 6) -- auth would fail
    # OPEN on deactivation instead of closed.
    #
    # NOTE (do NOT try to "fix"): an already-issued, not-yet-expired
    # *access* token remains valid until its own expiry even after this
    # change, because access tokens are stateless JWTs --
    # `AuthService.resolve_access` only decodes and verifies the token's
    # own signature/claims, it never re-checks the store. This is standard
    # JWT behavior, bounded by the short-lived `jwt_access_ttl_seconds`
    # (900s default) -- refresh denial (this fix) is what actually stops
    # session continuation past that point; there is no way to revoke a
    # single already-minted access token early without turning it into a
    # stateful token (out of scope here).
    #
    # SECURITY (Stage 13b, ban enforcement): both lookups below ALSO filter
    # on `User.status == "active"` -- a suspended or banned user (`app/api/
    # routers/admin.py`'s `suspend`/`ban` actions) is treated as
    # unauthenticated on the LOGIN path (`get_by_email`, via `AuthService.
    # login`) and the REFRESH path (`get_by_id`, via `AuthService.refresh`
    # step 6), composed with (not replacing) the existing `not_deleted()`
    # soft-delete filter above -- both conditions must hold. `get_by_id` is
    # also what `GET /auth/me` (`app/api/routers/auth.py`) resolves the
    # caller's profile through, so this closes that endpoint too for a
    # suspended/banned caller holding a still-unexpired access token -- a
    # bonus, not a scope creep: it's the SAME lookup already filtered here
    # for soft-delete, not a new call site. The admin surface
    # (`app/api/routers/admin.py`) never goes through `SqlAlchemyUserStore`
    # at all -- it queries `User` directly via `AsyncRepository`, entirely
    # unfiltered by `status`, so an admin can still see/act on suspended and
    # banned accounts. The residual window where an already-minted, not-yet-
    # expired ACCESS token still authenticates a just-banned user (bounded
    # by `jwt_access_ttl_seconds`) is the identical, already-accepted race
    # the soft-delete comment above documents -- `ban`/`suspend` additionally
    # call `RefreshTokenStore.revoke_all_for_user` to kill every REFRESH
    # token immediately, which is what actually stops session continuation
    # past that short access-token window; there is no cheap way to revoke a
    # single already-minted access token early without turning it into a
    # stateful token (out of scope here, same as the note above).
    async def get_by_email(self, email: str) -> UserRecord | None:
        result = await self._session.execute(
            select(User).where(User.email == email, User.not_deleted(), User.status == "active")
        )
        user = result.scalar_one_or_none()
        return _user_to_record(user) if user is not None else None

    async def get_by_id(self, id: str) -> UserRecord | None:
        try:
            user_id = uuid.UUID(id)
        except ValueError:
            # A malformed `sub` claim (not a UUID) cannot possibly match a
            # real row -- treated as "not found", not a crash, matching
            # `_core.AuthService.refresh`'s own "user gone -> InvalidToken"
            # handling of a genuinely-missing row.
            return None
        # Was `self._session.get(User, user_id)` -- a bare PK fetch that
        # bypasses soft-delete entirely (`Session.get` has no filtering
        # concept). Replaced with an explicit `select()` + `not_deleted()`
        # so a soft-deleted user's id resolves to "not found" here too,
        # same as `get_by_email` above -- `AuthService.refresh` step 6
        # then raises `InvalidToken`, consistent with its existing "user
        # gone" handling of a genuinely-missing row.
        result = await self._session.execute(
            select(User).where(User.id == user_id, User.not_deleted(), User.status == "active")
        )
        user = result.scalar_one_or_none()
        return _user_to_record(user) if user is not None else None

    async def create(self, email: str, password_hash: str, roles: Sequence[str]) -> UserRecord:
        """Raises `EmailAlreadyExists` (the SAME exception `AuthService.
        register`'s own `get_by_email`-then-`create` sequence raises when
        its prior read finds an active duplicate) if the `INSERT` violates
        `users.email`'s DB-level UNIQUE constraint.

        SECURITY (#48, L1 -- soft-deleted-email re-registration): that
        UNIQUE index is full-table, NOT partial on `deleted_at IS NULL` --
        by DECISION, a soft-deleted account's email stays reserved (letting
        someone claim a deleted user's identity is a security risk, and the
        account may still be restorable), so the index is deliberately
        never narrowed to free it up. But `get_by_email` (this store, above)
        IS soft-delete-scoped (`User.not_deleted()`), so re-registering a
        soft-deleted user's email reads as "free" at that lookup, and
        without this `try/except` the `INSERT` below would raise a raw
        `IntegrityError` straight through `AuthService.register` to the
        FastAPI catch-all `Exception` handler -- a 500, AND a weak
        enumeration oracle (soft-deleted email -> 500, free email -> 201,
        active-duplicate email -> 409 -- three distinct wire signatures for
        three states an attacker should not be able to tell apart). Routing
        the `IntegrityError` here to the SAME `EmailAlreadyExists` the
        active-duplicate path raises makes the response byte-identical
        (409 `conflict`) for "active email", "soft-deleted email", and a
        genuine concurrent duplicate-registration race (this store's own
        class docstring already documents that race is caught at the DB
        level -- this is that catch actually being handled instead of left
        to surface as a 500).

        `rollback()` before raising -- same posture `SqlAlchemyLockoutStore.
        upsert` already uses after its own `IntegrityError` catch: a failed
        flush leaves the session's transaction in SQLAlchemy's "pending
        rollback" state, where any further use of this session (even just
        letting the request finish) would raise `PendingRollbackError`
        instead of the intended `EmailAlreadyExists`/409."""
        user = User(email=email, password_hash=password_hash, roles=list(roles))
        self._session.add(user)
        try:
            await self._session.flush()
        except IntegrityError as exc:
            await self._session.rollback()
            raise EmailAlreadyExists("An account with this email already exists.") from exc
        await self._session.refresh(user)
        return _user_to_record(user)

    async def mark_email_verified(self, user_id: str, at: datetime) -> None:
        """Sets `email_verified=True`/`verified_at=at` for `user_id`. Does
        NOT commit (matches this class's own docstring/contract above) —
        `AccountService.verify_email` calls this right after
        `SingleUseTokenService.consume` (whose `mark_used` write, via
        `SqlAlchemySingleUseTokenStore`, IS committed immediately, matching
        that store's own durable-commit contract), so by the time this
        method's caller returns, the token consumption is already durable
        even though this particular write rides `get_db()`'s normal
        end-of-request commit, same as `create()` above."""
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            # A caller-supplied id that isn't a UUID cannot match a real
            # row -- best-effort no-op, matching UserStore's Protocol,
            # which declares no error path for this method.
            return
        result = await self._session.execute(select(User).where(User.id == uid, User.not_deleted()))
        user = result.scalar_one_or_none()
        if user is None:
            return
        user.email_verified = True
        user.verified_at = at
        await self._session.flush()

    async def set_password_hash(self, user_id: str, new_hash: str) -> None:
        """Overwrites `user_id`'s stored password hash with `new_hash` (an
        already-Argon2id-hashed value -- see `_core.UserStore.
        set_password_hash`'s own docstring). Does NOT commit, same posture
        as `mark_email_verified` above -- `AccountService.reset_password`
        calls `RefreshTokenStore.revoke_all_for_user` right after this,
        which DOES commit (matching `SqlAlchemyRefreshTokenStore`'s
        existing durable-commit contract on the SAME session), making this
        write durable at that point rather than waiting on `get_db()`'s
        end-of-request commit."""
        try:
            uid = uuid.UUID(user_id)
        except ValueError:
            return
        result = await self._session.execute(select(User).where(User.id == uid, User.not_deleted()))
        user = result.scalar_one_or_none()
        if user is None:
            return
        user.password_hash = new_hash
        await self._session.flush()


class SqlAlchemyRefreshTokenStore:
    """Implements `_core.RefreshTokenStore` against
    `app/models/refresh_token.py`'s `RefreshToken` and this request's
    `AsyncSession`.

    **`add`/`mark_used`/`revoke_family` each explicitly `commit()`** —
    NOT just `flush()` — per `_core.RefreshTokenStore`'s own protocol
    docstring: "Implementations MUST make add/mark_used/revoke_family
    durable (committed) before returning... so a concurrent second
    presentation of the just-rotated token sees the updated `used_at` and
    is correctly flagged as reuse rather than racing past this
    implementation's own write." A `flush()` alone only makes a change
    visible to the query planner WITHIN this same DB transaction — under
    the default READ COMMITTED isolation a concurrent transaction (a
    second, racing HTTP request reusing the same refresh token) would
    still see the pre-flush `used_at IS NULL` state until this session
    actually commits, defeating reuse detection under a genuine race. This
    intentionally commits mid-request, ahead of `get_db()`'s own
    end-of-request commit (see that dependency's docstring) — a second
    `commit()` on an already-clean session is a harmless no-op, so this
    does not conflict with that contract, only makes these three writes
    durable strictly earlier than it otherwise would."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: RefreshRecord) -> None:
        row = RefreshToken(
            token_hash=record.token_hash,
            jti=record.jti,
            family_id=record.family_id,
            user_id=uuid.UUID(record.user_id),
            issued_at=record.issued_at,
            expires_at=record.expires_at,
            used_at=record.used_at,
            revoked=record.revoked,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()

    async def get_by_hash(self, token_hash: str) -> RefreshRecord | None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        return _refresh_to_record(row) if row is not None else None

    async def mark_used(self, token_hash: str, used_at: datetime) -> None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        if row is None:
            # _core.AuthService.refresh only calls mark_used() on a row it
            # just looked up successfully in the same call -- a None here
            # would mean it vanished mid-request (not expected in
            # practice). Silently returning (rather than raising) keeps
            # this store's contract "best-effort write", matching
            # RefreshTokenStore's Protocol, which declares no error path.
            return
        row.used_at = used_at
        await self._session.flush()
        await self._session.commit()

    async def revoke_family(self, family_id: str) -> None:
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.family_id == family_id))
        rows = result.scalars().all()
        for row in rows:
            row.revoked = True
        await self._session.flush()
        await self._session.commit()

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY refresh-token row belonging to `user_id`, across
        every family (every device/session) — see `_core.
        RefreshTokenStore.revoke_all_for_user`'s own docstring on why
        `AccountService.reset_password` calls this rather than
        `revoke_family` (which only kills the ONE family behind whichever
        token happened to be presented). Same commit-not-flush durability
        contract as `add`/`mark_used`/`revoke_family` above."""
        result = await self._session.execute(select(RefreshToken).where(RefreshToken.user_id == uuid.UUID(user_id)))
        rows = result.scalars().all()
        for row in rows:
            row.revoked = True
        await self._session.flush()
        await self._session.commit()


class SqlAlchemySessionStore:
    """Implements `_sessions.SessionStore` against `app/models/session.py`'s
    `Session` and this request's `AsyncSession` — the store behind the
    DEFAULT browser authentication path in this app.

    **`add`/`touch`/`revoke`/`revoke_all_for_user` each explicitly
    `commit()`**, not just `flush()`, per `_sessions.SessionStore`'s own
    protocol docstring, and for a sharper reason than the refresh store's:
    `SessionService.revoke` is what a logout endpoint's entire security
    promise rests on, and a revocation still sitting in an uncommitted
    transaction when the response goes out is a logout that did not
    happen. Same mid-request-commit posture `SqlAlchemyRefreshTokenStore`
    documents above — a second `commit()` on an already-clean session at
    `get_db()`'s end-of-request boundary is a harmless no-op.

    `touch` is the one write on the HOT path (every authenticated request
    can potentially trigger it), which is exactly why `SessionService`
    rate-limits it behind `touch_interval` rather than calling it on every
    resolve — see that service's own docstring. This store does no
    rate-limiting of its own; it writes whenever it is asked to."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: SessionRecord) -> None:
        row = Session(
            session_hash=record.session_hash,
            user_id=uuid.UUID(record.user_id),
            # `created_at` is passed EXPLICITLY rather than left to the
            # column's `server_default=func.now()`: the session's own
            # created_at must come from the SAME injected `now` callable
            # (`utc_now`) that `SessionService` measures `absolute_ttl`
            # against, not from the database clock. Letting the two drift
            # would mean the absolute deadline stored on the row is
            # computed from a different "now" than the one the row records
            # as its creation time.
            created_at=record.created_at,
            last_seen_at=record.last_seen_at,
            absolute_expires_at=record.absolute_expires_at,
            revoked=record.revoked,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()

    async def get_by_hash(self, session_hash: str) -> SessionRecord | None:
        result = await self._session.execute(select(Session).where(Session.session_hash == session_hash))
        row = result.scalar_one_or_none()
        return _session_to_record(row) if row is not None else None

    async def touch(self, session_hash: str, last_seen_at: datetime) -> None:
        result = await self._session.execute(select(Session).where(Session.session_hash == session_hash))
        row = result.scalar_one_or_none()
        if row is None:
            # `SessionService.resolve` only calls touch() on a row it just
            # looked up successfully in the same call -- a None here would
            # mean it vanished mid-request. Silently returning (rather than
            # raising) matches `SqlAlchemyRefreshTokenStore.mark_used`'s
            # identical "best-effort write" posture, and is especially
            # right here: failing a live, already-authenticated request
            # because a bookkeeping timestamp could not be written would
            # turn a cosmetic problem into an outage.
            return
        row.last_seen_at = last_seen_at
        await self._session.flush()
        await self._session.commit()

    async def revoke(self, session_hash: str) -> None:
        """Marks ONE session revoked. Silently succeeds for an unknown
        hash — `SessionStore.revoke`'s documented idempotence contract,
        which `SessionService.revoke` (logout) relies on so a stale or
        forged cookie never produces an error, and so this endpoint never
        becomes an oracle distinguishing real session ids from invented
        ones."""
        result = await self._session.execute(select(Session).where(Session.session_hash == session_hash))
        row = result.scalar_one_or_none()
        if row is None:
            return
        row.revoked = True
        await self._session.flush()
        await self._session.commit()

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY session row belonging to `user_id` — every device,
        everywhere, at once. Called on a password reset (alongside
        `SqlAlchemyRefreshTokenStore.revoke_all_for_user`, so BOTH
        transports die on one reset), on a detected compromise, and on an
        account deactivation. Effective on the next request against every
        affected session, which is the guarantee the JWT path can only
        approximate. Indexed on `sessions.user_id` (see migration 0007), so
        this is one indexed UPDATE rather than a scan."""
        result = await self._session.execute(select(Session).where(Session.user_id == uuid.UUID(user_id)))
        rows = result.scalars().all()
        for row in rows:
            row.revoked = True
        await self._session.flush()
        await self._session.commit()


class SqlAlchemySingleUseTokenStore:
    """Implements `_core.SingleUseTokenStore` against
    `app/models/single_use_token.py`'s `SingleUseToken` and this request's
    `AsyncSession`.

    **`add`/`mark_used` each explicitly `commit()`** — the SAME
    durable-commit contract `SqlAlchemyRefreshTokenStore` above documents,
    and for the identical reason: `_core.SingleUseTokenService.consume`
    relies on `mark_used` having taken effect before it returns, so a
    concurrent second presentation of the just-consumed token (someone
    clicking an already-used verify/reset link twice, or an attacker who
    intercepted the link racing the legitimate recipient) sees the updated
    `used_at` and is correctly rejected as reuse rather than racing past
    this implementation's own write."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: SingleUseTokenRecord) -> None:
        row = SingleUseToken(
            token_hash=record.token_hash,
            user_id=uuid.UUID(record.user_id),
            purpose=record.purpose,
            expires_at=record.expires_at,
            used_at=record.used_at,
            created_at=record.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        await self._session.commit()

    async def get_by_hash(self, token_hash: str) -> SingleUseTokenRecord | None:
        result = await self._session.execute(select(SingleUseToken).where(SingleUseToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        return _single_use_token_to_record(row) if row is not None else None

    async def mark_used(self, token_hash: str, used_at: datetime) -> None:
        result = await self._session.execute(select(SingleUseToken).where(SingleUseToken.token_hash == token_hash))
        row = result.scalar_one_or_none()
        if row is None:
            # SingleUseTokenService.consume only calls mark_used() on a row
            # it just looked up successfully -- a None here would mean it
            # vanished mid-request. Best-effort no-op, matching
            # SingleUseTokenStore's Protocol, which declares no error path.
            return
        row.used_at = used_at
        await self._session.flush()
        await self._session.commit()


class SqlAlchemyLockoutStore:
    """Implements `_core.LockoutStore` against
    `app/models/login_attempt.py`'s `LoginAttempt` and this request's
    `AsyncSession` — dumb persistence only; ALL of the counting/threshold/
    rolling-window logic lives in `_core.LockoutPolicy`, not here (see that
    class's own docstring).

    **`upsert`/`clear` each explicitly `commit()`** — lockout state MUST
    survive a process restart (a fresh connection/session, a new request
    hitting a different worker process) to actually do its job: an
    in-memory-only or flush-only lockout would silently reset on restart,
    letting a locked-out account's guessing resume immediately. This is
    THE property the real-PG16 integration script (see README's Alembic
    section) proves directly: write an `AttemptRecord` via this store, open
    a brand-new engine/session, read it back and see the lock still there.

    `upsert` reads then inserts-or-updates the ONE row per `account_key`
    (`login_attempts.account_key` is DB-level UNIQUE, per Alembic 0003) —
    matching `_core.LockoutPolicy`'s own documented, ACCEPTED non-atomic
    read-modify-write relaxation (see that class's docstring: a lockout
    race can only ever delay when a lock becomes visible by a small,
    bounded amount, never let a wrong password succeed). A genuine
    concurrent insert race for the SAME `account_key` (two simultaneous
    wrong-password requests, both seeing no existing row) is still caught
    at the DB level by that UNIQUE index — `upsert` falls back to updating
    whichever row won the race rather than letting the loser's request
    surface a raw `IntegrityError` as a 500."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, account_key: str) -> AttemptRecord | None:
        result = await self._session.execute(select(LoginAttempt).where(LoginAttempt.account_key == account_key))
        row = result.scalar_one_or_none()
        return _attempt_to_record(row) if row is not None else None

    async def upsert(self, record: AttemptRecord) -> None:
        result = await self._session.execute(
            select(LoginAttempt).where(LoginAttempt.account_key == record.account_key)
        )
        row = result.scalar_one_or_none()
        if row is not None:
            row.failure_count = record.failure_count
            row.first_failure_at = record.first_failure_at
            row.last_failure_at = record.last_failure_at
            row.locked_until = record.locked_until
            await self._session.flush()
            await self._session.commit()
            return

        new_row = LoginAttempt(
            account_key=record.account_key,
            failure_count=record.failure_count,
            first_failure_at=record.first_failure_at,
            last_failure_at=record.last_failure_at,
            locked_until=record.locked_until,
        )
        self._session.add(new_row)
        try:
            await self._session.flush()
        except IntegrityError:
            # A concurrent request raced this one and already inserted the
            # row for this account_key -- see this class's own docstring.
            # Discard our own pending insert and fall back to updating
            # whichever row won.
            await self._session.rollback()
            result = await self._session.execute(
                select(LoginAttempt).where(LoginAttempt.account_key == record.account_key)
            )
            existing = result.scalar_one_or_none()
            if existing is not None:
                existing.failure_count = record.failure_count
                existing.first_failure_at = record.first_failure_at
                existing.last_failure_at = record.last_failure_at
                existing.locked_until = record.locked_until
                await self._session.flush()
        await self._session.commit()

    async def clear(self, account_key: str) -> None:
        result = await self._session.execute(select(LoginAttempt).where(LoginAttempt.account_key == account_key))
        row = result.scalar_one_or_none()
        if row is None:
            # Nothing to clear -- matches mark_used()'s/revoke_family()'s
            # own "best-effort, no error path" posture for a missing row.
            return
        await self._session.delete(row)
        await self._session.flush()
        await self._session.commit()


# ---------------------------------------------------------------------------
# Admin seeding (Stage 5d, #46) -- the ONLY sanctioned way an "admin" role
# ever gets attached to a user in this app.
# ---------------------------------------------------------------------------


async def seed_admin(session: AsyncSession, email: str, password: str) -> UserRecord:
    """Creates a user with `roles=["admin"]` -- the real admin-provisioning
    path (run by hand, by a one-off script, or by a test fixture), and
    deliberately the ONLY place in this app that ever constructs a user
    with an elevated role.

    **Why this exists, and why `POST /auth/register` never accepts a
    caller-supplied `roles` field.** `RegisterRequest`
    (`app/schemas/auth.py`) has no `roles` field, and `AuthService.register`
    always calls `UserStore.create(normalized, password_hash, roles=())`
    with an empty tuple (`app/core/security/auth/_core.py`) -- a client
    that could pass its own `roles` on the wire could self-grant `"admin"`
    on registration, a straightforward privilege-escalation bug. This
    function is the ONE place `SqlAlchemyUserStore.create(..., roles=
    ["admin"])` is ever called with a non-empty role list from this app's
    own code -- an operator (or a test's own setup fixture) invokes it
    directly, server-side; it is never reachable from any HTTP request
    body.

    Mirrors `AuthService.register`'s own shape (normalize the email, hash
    the password via the process-wide `PasswordService`, `UserStore.
    create`) rather than delegating to `AuthService.register` itself and
    then separately promoting the row to admin after the fact -- there is
    no `UserStore` method to change roles post-creation (see `UserStore`'s
    own `Protocol` in `_core.py`: `create` is the only place `roles` is
    ever set), so building the row with the right roles from the start,
    exactly once, is the simpler and more obviously-correct construction.

    **Commits immediately** -- unlike `SqlAlchemyUserStore.create` itself
    (which only flushes, deferring to `get_db()`'s end-of-request commit
    boundary -- see that store's own class docstring), because this
    function has no enclosing HTTP request to ride a commit boundary on:
    it is called from a script or a test fixture's own setup, outside any
    request lifecycle, so it must make the seeded admin durable itself
    before returning."""
    normalized = email.strip().lower()
    password_hash = get_password_service().hash(password)
    user = await SqlAlchemyUserStore(session).create(normalized, password_hash, roles=["admin"])
    await session.commit()
    return user


def utc_now() -> datetime:
    """The single `now` callable this app passes to BOTH `TokenService`
    and `AuthService` (see `_core.AuthService.__init__`'s own docstring:
    "a caller normally passes the SAME callable to both") — a plain
    module-level function rather than an inline lambda at each call site,
    so `app/api/deps.py:get_auth_service` and `get_token_service` below
    are provably passing the identical behavior, not two separately
    written (and possibly silently drifting) `lambda: datetime.now(...)`
    expressions."""
    return datetime.now(timezone.utc)


class AuthNotConfiguredError(RuntimeError):
    """Raised when `Settings.jwt_signing_key` is unset at the exact point
    auth is actually used (inside `get_token_service`, called from a
    per-request dependency) rather than at `Settings()` construction time
    (see `app/core/config.py`'s `jwt_signing_key` field docstring on why
    it resolves to `None`, not a hard failure, when unset — most of this
    app's routes/tests never touch auth at all). Deliberately a plain
    `RuntimeError` subclass, not part of `_core.AuthError`'s hierarchy —
    this is a SERVER misconfiguration, not a client-caused auth failure,
    so it must NOT be caught by `AUTH_ERROR_HTTP`/an `AppError` mapping
    and rendered as a 401/409; left unhandled, it reaches app/main.py's
    catch-all `Exception` handler and renders the generic `internal_error`
    envelope at 500 — "fail closed" without ever constructing a
    `TokenService` with an empty/absent signing key (which `TokenService.
    __init__` itself also refuses, per `_core.py`'s own `ValueError`
    guard — this is the layer above that, for the `None`-vs-empty-string
    case `TokenService` cannot see because it's never even called)."""


@lru_cache
def get_password_service() -> PasswordService:
    """One process-wide `PasswordService` — see that class's own docstring
    on why: its `dummy_verify()` timing defense depends on a precomputed
    throwaway hash computed ONCE at construction (a real Argon2id hash,
    not a cheap operation) so every login's "email not found" path costs
    the same wall-clock time as a real `verify()` call; constructing a
    fresh instance per request would pay that Argon2id cost on every
    single request, not just once at process start."""
    return PasswordService()


def get_token_service(settings: Settings) -> TokenService:
    """Builds a `TokenService` from this project's `Settings` — cheap to
    construct (holds config values only, no heavy crypto in `__init__`,
    unlike `PasswordService` above), so a fresh instance per request is
    fine and keeps this function trivially pure over its `settings`
    argument rather than needing its own cache-invalidation story if
    settings ever change between requests (as the test suite's
    `make_client` fixture does, per-test).

    Raises `AuthNotConfiguredError` if `settings.jwt_signing_key` is
    `None` — see that exception's own docstring for the fail-closed
    rationale."""
    if not settings.jwt_signing_key:
        raise AuthNotConfiguredError(
            "JWT_SIGNING_KEY is not configured. Set the JWT_SIGNING_KEY environment "
            "variable (or its AWS Secrets Manager equivalent -- see "
            "app/core/security/secret_store's get_secret()) before any auth endpoint "
            "is used."
        )
    return TokenService(
        settings.jwt_signing_key,
        issuer=settings.jwt_issuer,
        access_ttl=timedelta(seconds=settings.jwt_access_ttl_seconds),
        refresh_ttl=timedelta(seconds=settings.jwt_refresh_ttl_seconds),
        now=utc_now,
    )


# ---------------------------------------------------------------------------
# Email seam: ConsoleEmailSender (dev/test, vendored) vs. a real SmtpEmailSender
# ---------------------------------------------------------------------------


_smtp_logger = logging.getLogger("auth.email.smtp")


class SmtpEmailSender:
    """Production `_core.EmailSender` implementation — hand-rolled, stdlib
    `smtplib` + `email.message.EmailMessage` only (no third-party email
    library/pin), matching this app's "don't add a dependency for what the
    standard library already does" posture elsewhere in this catalog.

    **Fire-and-forget by contract** (adversarial-review fix, FIX 3): `send()`
    SCHEDULES delivery (`asyncio.create_task(self._deliver(message))`) and
    returns immediately — it does NOT await the SMTP round-trip, and it
    NEVER raises. This is what `_core.EmailSender`'s own Protocol docstring
    requires ("implementations MUST NOT let delivery latency or delivery
    failure affect the caller") and what `_core.AccountService.
    request_password_reset`'s anti-enumeration defense and `register`'s
    M2 resilience fix (`app/api/routers/auth.py`) both depend on: neither
    caller can tell, from `send()` returning, whether the message was
    actually delivered — only that delivery was scheduled. `_deliver` runs
    the actual blocking `smtplib` work on a worker thread (`anyio.to_thread.
    run_sync` — see that method's own docstring for why) inside a
    `try/except Exception` that logs a `warning` and swallows the error;
    nothing ever propagates back out of the background task, because
    nothing is awaiting it that could observe a raise or a return.

    **In-flight task lifetime.** A bare `asyncio.create_task(...)` result
    that nothing holds a reference to is eligible for garbage collection
    mid-flight (the task can be silently cancelled before it completes —
    see `asyncio.create_task`'s own docs on this exact footgun). `_tasks`
    (a `set`) holds a strong reference to every task this instance has
    scheduled for as long as it's in flight — added in `send()`, removed
    by a `add_done_callback` once the task finishes (success OR the
    already-caught-internally failure) — so a `send()` call's task is
    guaranteed to actually run to completion rather than vanishing.

    **Best-effort on shutdown.** This class holds no reference to the
    app's own lifespan/shutdown sequence, so a task still in flight when
    the process exits (a slow SMTP handshake racing a deploy) can be cut
    off mid-delivery — that email is then simply not delivered, silently,
    same as any other fire-and-forget background job. This is an accepted
    trade-off for a starter-kit reference sender: a project with a hard
    delivery guarantee should replace this with a real queue/outbox
    (persisted, retried, survives a restart) rather than an in-process
    `asyncio.Task`; `_core.EmailSender`'s Protocol is the seam that swap
    happens behind, unchanged for either `AccountService` caller.

    Always issues `STARTTLS` before authenticating — this app never sends
    a plaintext SMTP AUTH exchange (which would leak `smtp_username`/
    `smtp_password` to a network observer) over an unencrypted connection.
    `username`/`password` are optional (`None`/`None` skips the `AUTH`
    step entirely) for a relay that doesn't require authentication (e.g. an
    internal MTA on a trusted network)."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        from_addr: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._from_addr = from_addr
        # Strong references to in-flight delivery tasks -- see this class's
        # own docstring on why a bare create_task() result can't be left to
        # get garbage-collected mid-flight.
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, message: EmailMessage) -> None:
        """Schedules delivery and returns immediately -- does NOT await the
        SMTP round-trip, does NOT raise. See this class's own docstring."""
        task = asyncio.create_task(self._deliver(message))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _deliver(self, message: EmailMessage) -> None:
        """Runs as a background task (scheduled by `send()` above), never
        awaited by a caller. Bridges the blocking `smtplib` work onto a
        worker thread via `anyio.to_thread.run_sync` — `anyio` is already a
        transitive dependency of FastAPI/Starlette (no new pin in
        `pyproject.toml` is needed), and is the framework-agnostic
        thread-offload primitive Starlette itself uses internally, so
        reaching for it here matches how this app's own ASGI stack already
        handles blocking work. Any exception (a connection failure, an
        auth rejection, a timeout) is caught here and only LOGGED — this is
        the actual enforcement point of this class's fire-and-forget
        contract: nothing above this method ever sees the exception,
        because nothing above it is awaiting this coroutine at all."""
        try:
            await anyio.to_thread.run_sync(self._send_sync, message)
        except Exception:
            _smtp_logger.warning("Failed to deliver email to %s (subject=%r)", message.to, message.subject, exc_info=True)

    def _send_sync(self, message: EmailMessage) -> None:
        """Runs on a worker thread (via `anyio.to_thread.run_sync` in
        `_deliver` above) — every call here is BLOCKING stdlib I/O, never
        `await`ed directly."""
        mime = MimeEmailMessage()
        mime["From"] = self._from_addr
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)
        with smtplib.SMTP(self._host, self._port, timeout=10) as client:
            client.starttls()
            if self._username and self._password:
                client.login(self._username, self._password)
            client.send_message(mime)


def get_email_sender(settings: Settings) -> EmailSender:
    """Returns a `ConsoleEmailSender` (vendored, dev/test-only — logs the
    message, including the raw single-use token, instead of delivering it —
    see that class's own docstring) when `settings.smtp_host` is unset,
    else a real `SmtpEmailSender` built from `settings.smtp_*`/`email_from`.
    Same "don't invent a secret, fail closed to the SAFE dev-only fallback
    rather than a fake/broken production path" posture as
    `get_token_service`'s `AuthNotConfiguredError` guard above, applied to
    email instead of JWT signing: an unset `SMTP_HOST` never raises here —
    it just means this process never intended to send real email (matching
    most of this app's tests/local dev), so `ConsoleEmailSender` is the
    correct, safe default rather than a hard failure."""
    if not settings.smtp_host:
        return ConsoleEmailSender()
    return SmtpEmailSender(
        host=settings.smtp_host,
        port=settings.smtp_port,
        username=settings.smtp_username,
        password=settings.smtp_password,
        from_addr=settings.email_from,
    )


# ---------------------------------------------------------------------------
# Audit seam: AuditAuthEventSink forwards to the vendored audit-logging
# component's audit_event()
# ---------------------------------------------------------------------------


class AuditAuthEventSink:
    """Implements `_core.AuthEventSink` by forwarding every call to the
    vendored audit-logging component's `audit_event(...)`
    (`app/core/security/audit_logging/audit.py`) — this is the "thin
    adapter" `_core.AuthEventSink`'s own docstring describes a project
    wiring, kept as app code (not part of `_core.py`) so that module stays
    at "stdlib + PyJWT + argon2-cffi only, zero framework/app import".

    `action`/`actor`/`outcome` pass straight through — `_core.py` already
    constructs `actor` as a bare opaque id string (a user id, `"anonymous"`,
    or `"unknown"`/`"user:unknown"` for a path with no trustworthy
    principal — see e.g. `AccountService.request_password_reset`'s own
    docstring on why THAT method never uses the submitted email as the
    actor) — this sink never receives, and therefore can never leak, a raw
    token, password, or email address as a field: every `**extra` this
    module's callers pass is limited to what `_core.py`'s own docstrings
    document (which is nothing beyond `action`/`actor`/`outcome` today).
    `audit_event`'s own `redact()` step is a second, independent line of
    defense on top of that — see its own docstring — not the only one.

    `resource` is a fixed `"auth"` string, not per-event — every event this
    sink ever receives IS an auth-subsystem event acting on the actor's own
    account; there is no separate, more specific resource identifier to
    name here that wouldn't just duplicate `actor`."""

    async def emit(self, action: str, *, actor: str, outcome: str, **extra: object) -> None:
        audit_event(action, actor=actor, resource="auth", outcome=outcome, **extra)


# ---------------------------------------------------------------------------
# AccountService factories (Stage 5c, #45) — NEW, alongside app/api/deps.py's
# existing get_auth_service. Not yet called from anywhere in this stage.
# ---------------------------------------------------------------------------


def build_lockout_policy(settings: Settings, session: AsyncSession) -> LockoutPolicy | None:
    """Returns a `LockoutPolicy` backed by `SqlAlchemyLockoutStore(session)`
    when `settings.auth_lockout_enabled` is `True`, `None` when it isn't —
    both `AuthService` and `AccountService` treat a `None` lockout as "not
    wired, skip lockout entirely" (see each class's own `lockout` parameter
    docstring), so this single function is the one place that decision is
    made, callable identically for either service's wiring.

    A project wiring `AuthService.login`'s own `lockout=` parameter (the
    next stage's endpoint work) should call this SAME function, passing the
    SAME `session`, as `AccountService`'s wiring below does — sharing one
    `LockoutPolicy` instance (or at least one built against the same
    underlying `SqlAlchemyLockoutStore`/session/settings) is what lets a
    successful `AccountService.reset_password` lift a lockout `AuthService.
    login` had recorded against the same account (see `_core.
    AccountService.__init__`'s own docstring on its `lockout` parameter)."""
    if not settings.auth_lockout_enabled:
        return None
    return LockoutPolicy(
        SqlAlchemyLockoutStore(session),
        max_failures=settings.auth_lockout_max_failures,
        lockout_duration=timedelta(seconds=settings.auth_lockout_duration_seconds),
        window=timedelta(seconds=settings.auth_lockout_window_seconds),
        now=utc_now,
    )


def build_session_service(settings: Settings, session: AsyncSession) -> SessionService:
    """Builds a per-request `SessionService` — the provider behind this
    app's DEFAULT browser authentication path, the same composition shape
    `build_account_service` below and `app/api/deps.py:get_auth_service`
    use: fresh stores bound to THIS request's `session`, and `utc_now` as
    the single shared clock every other service in this app's auth
    composition also uses (so a session's deadlines and a token's expiry
    are measured against the same "now").

    Takes `SqlAlchemyUserStore` as well as `SqlAlchemySessionStore`
    because `SessionService.resolve` reads the user's CURRENT roles on
    every request rather than trusting a snapshot — that second read is
    what makes a role grant or revocation take effect immediately, and it
    is the deliberate cost of this path over a JWT's baked-in `roles`
    claim (see `_sessions.py`'s module docstring).

    **Needs no signing key**, unlike `get_token_service` above — a session
    id is opaque and unguessable rather than signed, so there is nothing
    to configure wrong and no `AuthNotConfiguredError` equivalent to fail
    closed on. That is one fewer secret to manage in every environment for
    a project that serves only browser clients."""
    return SessionService(
        SqlAlchemySessionStore(session),
        SqlAlchemyUserStore(session),
        utc_now,
        idle_ttl=timedelta(seconds=settings.session_idle_ttl_seconds),
        absolute_ttl=timedelta(seconds=settings.session_absolute_ttl_seconds),
        touch_interval=timedelta(seconds=settings.session_touch_interval_seconds),
        events=AuditAuthEventSink(),
    )


def build_account_service(
    settings: Settings,
    session: AsyncSession,
    *,
    email: EmailSender | None = None,
) -> AccountService:
    """Builds a per-request `AccountService`, the SAME composition shape
    `app/api/deps.py:get_auth_service` uses for `AuthService` — a fresh
    `SqlAlchemyUserStore`/`SqlAlchemyRefreshTokenStore`/
    `SqlAlchemySingleUseTokenStore` bound to THIS request's `session`, the
    process-wide `PasswordService` singleton, `utc_now` as the single
    shared clock, `AuditAuthEventSink()` for `events`, `build_lockout_
    policy(settings, session)` for `lockout` (shared-session, so a
    completed reset can lift a lockout `AuthService.login` recorded — see
    that function's own docstring), and `settings.frontend_base_url`/
    `auth_verify_ttl_seconds`/`auth_reset_ttl_seconds` for the link-building
    and TTL configuration.

    `email` (Stage 5c #45 endpoint work, keyword-only): the `EmailSender`
    to use — `None` (the default) resolves it the same way this function
    always has (`get_email_sender(settings)`). A caller that already has
    one resolved from elsewhere passes it directly instead — this is the
    seam `app/api/deps.py:get_account_service` uses to hand this function
    the SAME `EmailSender` a FastAPI dependency (overridable via
    `app.dependency_overrides` in tests, see `tests/test_auth.py`'s
    `capturing_email_sender` fixture) resolved, rather than this function
    re-resolving its own independent instance that a test override would
    never reach.

    Intentionally analogous to `app/api/deps.py:get_auth_service` — this
    factory is called from `get_account_service`, the FastAPI dependency
    the `/auth/verify-email`, `/auth/request-password-reset`, `/auth/
    reset-password` routes (and `register`'s post-registration
    verification-email side effect) depend on."""
    return AccountService(
        users=SqlAlchemyUserStore(session),
        tokens=SingleUseTokenService(SqlAlchemySingleUseTokenStore(session), now=utc_now),
        email=email if email is not None else get_email_sender(settings),
        passwords=get_password_service(),
        refresh_tokens=SqlAlchemyRefreshTokenStore(session),
        now=utc_now,
        events=AuditAuthEventSink(),
        lockout=build_lockout_policy(settings, session),
        # Both revocation seams are wired, deliberately: `reset_password`
        # already revoked every refresh-token family, and this makes it
        # revoke every server-side session too. Wiring only one would leave
        # a reset account HALF-revoked -- an attacker's session cookie
        # would keep authenticating despite every JWT being killed. Built
        # against the SAME `session` as every other store here, so the
        # revocation commits inside this request like the rest.
        sessions=build_session_service(settings, session),
        frontend_base_url=settings.frontend_base_url,
        verify_ttl=timedelta(seconds=settings.auth_verify_ttl_seconds),
        reset_ttl=timedelta(seconds=settings.auth_reset_ttl_seconds),
    )
