"""Framework-neutral SERVER-SIDE SESSION core -- opaque, high-entropy
session identifiers whose entire authority lives in a `SessionStore` row,
not in anything the client can present. This is the **default**
authentication path for browser clients in this catalog; `_core.py`'s
JWT access/refresh pair remains the path for native/mobile clients and
service-to-service callers. Canon: references/security/secure-baseline.md
("Authentication & authorization"), references/wiring/auth-end-to-end.md
("Session mode (web, the default)").

Drop-in: copy this file into app/core/security/auth/_sessions.py, alongside
`_core.py`, `_cookies.py`, and whichever framework adapter(s) a project
vendors -- see this component's README's "Server-side sessions" section for
the full composition contract. Stdlib + `_core` only -- **no FastAPI,
Django, SQLAlchemy, or PyJWT import anywhere in this file**, matching
`_core.py`/`_cookies.py`'s own framework-neutral posture. `SessionStore`
below is a `Protocol` a framework adapter implements against its own
ORM/session; this module never touches a database, a request object, or a
settings object directly.

**Why sessions are preferred over JWT for browser clients.** A JWT is a
BEARER credential the server validates by signature alone: between mint
and expiry it is valid because it says it is. That property is what makes
JWTs attractive for stateless, cross-service authentication -- and it is
exactly what makes them the wrong default for a browser session:

- **Revocation is immediate, not eventual.** Killing a session is one
  `UPDATE` on one row (`SessionStore.revoke`); the very next request fails.
  A JWT cannot be un-minted -- the standard mitigation is to keep the
  access token's TTL short and accept a window (minutes) in which a
  logged-out, banned, or compromised principal still authenticates
  successfully. `_core.py`'s `RefreshTokenStore` already concedes this
  point for the REFRESH half (trust lives in the store, not the claims);
  this module applies the same reasoning to the ACCESS half, which is the
  half that actually authorizes requests.
- **Privilege changes take effect at once.** `resolve()` below reads the
  user's CURRENT roles from `UserStore` on every request (see that
  method's docstring), so revoking an admin role logs that power away on
  the next request. A `roles` claim baked into a JWT stays true until the
  token expires, no matter what the database says.
- **Nothing sensitive is parked in the browser.** The cookie holds a
  random opaque string that means nothing off this server, and the store
  keeps only its SHA-256 hash. A JWT, by contrast, carries the subject and
  roles in a base64 payload anyone holding it can read.
- **Idle timeout is enforceable.** Sliding expiry (`idle_ttl`) is a
  server-side comparison against a stored `last_seen_at`. A JWT has one
  fixed `exp` and no notion of "the user stopped using this."

The cost is one store read per authenticated request (plus one user read),
which is the honest tradeoff: sessions trade statelessness for control.
For a browser-facing app that already queries a database on nearly every
request, that trade is worth making -- see this component's README's
"Judgment calls" for when it is NOT (native/mobile clients, which have a
real OS-backed secret store and no ambient-cookie problem, and
service-to-service callers, which have no user session at all).

**The resolve state machine is the security-critical core of this module**
-- read `SessionService.resolve`'s docstring and `tests/test_sessions.py`
before touching it. Summary: the persisted `SessionRecord`, never anything
the client presents, decides whether a session is live; a session id that
is unknown, revoked, past its absolute deadline, or idle past `idle_ttl`
is rejected identically, and so is one whose user has since been deleted.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import _core

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidSession(_core.AuthError):
    """A presented session id could not be resolved to a live session.
    Maps to `ErrorCode.UNAUTHENTICATED` (401).

    Deliberately ONE exception for every rejection reason -- no cookie at
    all, a blank cookie, an unknown id (never issued, or issued by a
    different environment/signing domain), a REVOKED session (logged out,
    or killed by a password reset), one past its ABSOLUTE deadline, one
    idle past `idle_ttl`, and one whose user has since been deleted all
    collapse to this SAME type with the SAME generic message. This mirrors
    `_core.py`'s own repeated "don't leak which specific reason" posture
    (`InvalidCredentials`, `InvalidToken`/`TokenReused`,
    `InvalidSingleUseToken`): a client holding a session id must not be
    able to distinguish "this was valid until you logged out" from "this
    was never a session" -- the former confirms the id was once real,
    which is exactly what an attacker replaying a stolen or intercepted
    cookie would like to learn. A server-side audit event (via
    `_core.AuthEventSink`, emitted by `SessionService` when one is wired)
    is where the real reason is recorded for a human, exactly as
    `TokenReused`'s own docstring describes for refresh-token reuse."""


# ---------------------------------------------------------------------------
# Session id generation
# ---------------------------------------------------------------------------


def generate_session_id() -> str:
    """Mints a fresh, OPAQUE session id: `secrets.token_urlsafe(32)` -- 32
    bytes (~256 bits) of CSPRNG entropy, base64url-encoded, the SAME
    construction `_core.SingleUseTokenService.issue` and
    `_cookies.generate_csrf_token` already use.

    **Opaque, deliberately -- not a JWT, not signed, not structured.** A
    session id carries NO claims: it is a lookup key and nothing else, so
    there is no payload for a client to read, no signature for a server to
    misvalidate, and no algorithm for an attacker to confuse (the `alg`
    confusion class of JWT vulnerability simply has no surface here). Every
    fact about the session -- who it belongs to, when it was created, when
    it goes stale -- lives in the `SessionRecord` this id looks up, which
    means every one of those facts can be changed or invalidated by the
    server at any moment. That is the entire point of this module.

    256 bits of entropy is far past the point where guessing is a
    consideration: an attacker cannot enumerate the id space, so the only
    routes to a valid session id are theft of the cookie itself (closed by
    `HttpOnly`/`Secure`/`SameSite` -- see `_cookies.build_session_cookie_kwargs`)
    or a store compromise (blunted by hashing at rest -- see
    `SessionRecord.session_hash`)."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# Session store: Protocol + records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionRecord:
    """One persisted session row -- the SOLE source of truth for whether a
    session is live. `session_hash` (from `_core.hash_token`) is the lookup
    key; **the raw session id is never written to storage**, for exactly
    the reasons `_core.hash_token`'s own docstring gives for refresh
    tokens: a fast cryptographic hash (not a slow KDF) is correct because
    a session id is a high-entropy value THIS module generated rather than
    a low-entropy human-chosen secret, and hashing at all means a
    read-only compromise of these rows (a leaked backup, a compromised
    read replica) hands out no live, directly-usable session cookies.

    Deliberately stores NO roles snapshot. A session's authorization is
    resolved live from `UserStore` on every request (see
    `SessionService.resolve`) -- caching roles here would reintroduce
    exactly the staleness window this module exists to close, and over a
    much longer horizon than a JWT's own short access TTL, since a session
    can legitimately live for days.

    `last_seen_at` is what sliding/idle expiry is measured against;
    `absolute_expires_at` is the hard ceiling no amount of activity can
    push back (see `SessionService`'s constructor docstring on why both
    exist). `revoked` is set by `revoke`/`revoke_all_for_user`, and the row
    is RETAINED rather than deleted -- the same "retain, don't delete"
    posture `_core.RefreshRecord` takes, so a replay of a logged-out
    session is recognized as a revoked session (auditable) rather than
    merely vanishing into "not found"."""

    session_hash: str
    user_id: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime
    revoked: bool


@dataclass(frozen=True)
class IssuedSession:
    """What `SessionService.create`/`rotate` hand back: the RAW session id
    (the only time this module ever exposes it -- the caller sets it as a
    cookie and then forgets it; only its hash was persisted) alongside the
    `SessionRecord` that was just written.

    The record is returned too so a caller can derive the session cookie's
    `max_age` from real persisted values rather than recomputing a TTL by
    hand -- see `max_age_seconds` below."""

    session_id: str
    record: SessionRecord

    def max_age_seconds(self, now: datetime) -> int:
        """Seconds from `now` until this session's ABSOLUTE deadline --
        what a caller passes as the session cookie's `max_age` (see
        `_cookies.build_session_cookie_kwargs`), so the browser drops the
        cookie no later than the moment the server would stop honoring it
        anyway.

        Deliberately measured against `absolute_expires_at`, not
        `idle_ttl`: the cookie must SURVIVE an idle period long enough for
        the server to be the one that decides the session went stale. A
        cookie expiring at the idle deadline would delete itself in the
        browser first, turning every idle timeout into "no cookie
        presented" -- indistinguishable, to the server and to an audit
        log, from a user who simply never had a session. Clamped at 0 so
        an already-expired session can never produce a negative `max_age`
        (which some browsers read as a session-length cookie rather than
        an immediate delete)."""
        return max(0, int((self.record.absolute_expires_at - now).total_seconds()))


class SessionStore(Protocol):
    """The storage seam `SessionService` runs against -- a framework
    adapter implements this against its own ORM/session (e.g. a SQLAlchemy
    or Django model table keyed by `session_hash`). All methods are `async`
    since a real implementation talks to a database.

    Implementations MUST make `add`/`touch`/`revoke`/`revoke_all_for_user`
    durable (committed) before returning, matching
    `_core.RefreshTokenStore`'s identical contract: `SessionService.revoke`
    is what a logout endpoint's security promise rests on, and a revocation
    that is still sitting in an uncommitted transaction when the response
    goes out is a logout that did not happen."""

    async def add(self, record: SessionRecord) -> None: ...

    async def get_by_hash(self, session_hash: str) -> SessionRecord | None: ...

    async def touch(self, session_hash: str, last_seen_at: datetime) -> None:
        """Advances one session's `last_seen_at` -- the sliding half of
        expiry. Called by `SessionService.resolve` on a live session, and
        deliberately RATE-LIMITED by that method (see its `touch_interval`
        discussion) so an authenticated request does not always incur a
        write."""
        ...

    async def revoke(self, session_hash: str) -> None:
        """Marks ONE session revoked. Idempotent: revoking an already-
        revoked or entirely unknown `session_hash` must succeed silently
        rather than raise, since `SessionService.revoke` (logout) is
        specified to be idempotent and must not turn a double-click on a
        logout button into a 500."""
        ...

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Marks EVERY session belonging to `user_id` revoked -- every
        device, everywhere, at once. This is the "log out everywhere"
        primitive a password reset, a detected compromise, or an
        administrative ban runs through, and it is the operation a JWT
        access token fundamentally cannot offer. Same durable-commit
        contract as the methods above."""
        ...


# ---------------------------------------------------------------------------
# The resolved principal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SessionPrincipal:
    """What `SessionService.resolve` hands back to a framework adapter's
    `get_current_principal`-equivalent -- "who is calling, and with which
    roles", resolved from a live session.

    **Deliberately duck-type-compatible with `_core.AccessClaims` on `sub`
    and `roles`**, the only two attributes generic authorization code in
    this component reads. That compatibility is what lets `fastapi.py`'s
    `require_roles(...)` factory be reused verbatim against a session
    principal instead of JWT claims, and lets a route body that reads
    `principal.sub` work unchanged whether the request authenticated with
    a session cookie or a bearer token -- so a project can run session mode
    on web and bearer mode on mobile against ONE set of route handlers.

    The session-specific fields below have no `AccessClaims` counterpart:
    `session_hash` identifies THIS session for audit logging and for a
    "log out this one device" UI, and the three timestamps let a route
    surface session age/idle information without a second store read."""

    sub: str
    roles: list[str]
    session_hash: str
    created_at: datetime
    last_seen_at: datetime
    absolute_expires_at: datetime


# ---------------------------------------------------------------------------
# SessionService: create / resolve / rotate / revoke
# ---------------------------------------------------------------------------


class SessionService:
    """Orchestrates `SessionStore` and `_core.UserStore` into
    `create`/`resolve`/`rotate`/`revoke`/`revoke_all_for_user` -- the
    server-side-session counterpart to `_core.AuthService`'s JWT surface,
    and the DEFAULT authentication path for browser clients.

    Composed ALONGSIDE `AuthService`, never as a subclass of it and never
    replacing it: a login route verifies the password through
    `AuthService`'s existing machinery (which owns Argon2id verification,
    the `dummy_verify` timing defense, lockout, and the
    `require_verification` gate -- none of which this module duplicates),
    then calls `create()` here instead of minting a token pair. A project
    running BOTH paths (session on web, bearer on mobile) constructs both
    services against the same `UserStore`.

    `now` is injected exactly as `_core.TokenService`/`AuthService`'s is
    (required, no default) -- a caller normally passes the SAME callable to
    all of them, so expiry comparisons across the whole component agree.

    **Two independent expiry deadlines, both enforced.** This is the part
    worth reading twice:

    - **`idle_ttl`** (sliding) -- a session dies if `now - last_seen_at`
      reaches it. This is what makes an abandoned session on a shared or
      stolen machine stop working without the user doing anything, and it
      is a property a JWT structurally cannot have (one fixed `exp`, no
      concept of "still being used").
    - **`absolute_ttl`** (hard ceiling) -- a session dies once `now`
      reaches `created_at + absolute_ttl`, no matter how continuously it
      has been used. Without this, a session an attacker keeps warm with a
      periodic request lives forever; the sliding deadline alone rewards
      exactly the attacker who is actively using the stolen cookie.

    Both are checked on every `resolve`, and neither can rescue the other:
    a session must be inside BOTH windows to authenticate.

    `touch_interval` bounds how often a live `resolve` writes back to the
    store (see `resolve`'s own docstring) -- it trades a bounded amount of
    `last_seen_at` precision for not turning every authenticated GET into a
    database write.

    `events` is an optional `_core.AuthEventSink`, the same seam
    `AuthService.login`/`AccountService` already emit through -- when
    provided, this service emits `auth.session.created`,
    `auth.session.rotated`, `auth.session.revoked`,
    `auth.session.revoked_all`, and `auth.session.rejected`. `None` (the
    default) emits nothing."""

    def __init__(
        self,
        sessions: SessionStore,
        users: _core.UserStore,
        now: Callable[[], datetime],
        *,
        idle_ttl: timedelta = timedelta(hours=12),
        absolute_ttl: timedelta = timedelta(days=7),
        touch_interval: timedelta = timedelta(minutes=1),
        events: _core.AuthEventSink | None = None,
    ) -> None:
        if idle_ttl <= timedelta(0):
            raise ValueError("idle_ttl must be positive.")
        if absolute_ttl <= timedelta(0):
            raise ValueError("absolute_ttl must be positive.")
        if touch_interval < timedelta(0):
            raise ValueError("touch_interval must not be negative.")
        self._sessions = sessions
        self._users = users
        self._now = now
        self._idle_ttl = idle_ttl
        self._absolute_ttl = absolute_ttl
        self._touch_interval = touch_interval
        self._events = events

    @property
    def idle_ttl(self) -> timedelta:
        """The sliding idle deadline this service was constructed with --
        exposed read-only so a route handler or an adapter can report it
        (e.g. to drive a client-side "you will be signed out soon"
        warning) without reaching into a private attribute."""
        return self._idle_ttl

    @property
    def absolute_ttl(self) -> timedelta:
        """The absolute-lifetime ceiling this service was constructed
        with -- exposed read-only for the same reason as `idle_ttl`."""
        return self._absolute_ttl

    async def create(self, user: _core.UserRecord) -> IssuedSession:
        """Starts a BRAND-NEW session for an ALREADY-AUTHENTICATED user --
        this method performs no credential check of any kind, exactly like
        `_core.AuthService.issue_session`, whose contract it mirrors on the
        session side. The caller (a login route, an OAuth callback, a
        post-registration auto-login) is responsible for having established
        who `user` is first.

        **Every login mints a fresh id, which is the session-fixation
        defense.** An attacker who plants a session id in a victim's
        browser before login (via a crafted link, a subdomain cookie
        write, or an XSS on a sibling origin) gains nothing: the id the
        victim's browser ends up holding after authenticating is one this
        method just generated, and the planted one was never associated
        with the victim's account. This is why there is no "adopt the
        existing session id" path here, and why a caller must never reuse
        an incoming cookie value as the new session id.

        Returns an `IssuedSession` carrying the raw id (set it as a cookie;
        it is never recoverable afterward, since only its hash is stored)
        and the persisted record."""
        now = self._now()
        raw = generate_session_id()
        record = SessionRecord(
            session_hash=_core.hash_token(raw),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            absolute_expires_at=now + self._absolute_ttl,
            revoked=False,
        )
        await self._sessions.add(record)
        if self._events is not None:
            await self._events.emit("auth.session.created", actor=user.id, outcome="success")
        return IssuedSession(session_id=raw, record=record)

    async def resolve(self, raw_session_id: str | None) -> SessionPrincipal:
        """THE session-resolution state machine -- called on every
        authenticated request. Raises `InvalidSession` (one generic
        exception for every rejection reason -- see that class's docstring)
        unless the presented id resolves to a live session. In this exact
        order:

        1. **Missing or blank id** -> `InvalidSession`. A `None` (no cookie
           on the request at all) and an empty-string cookie are treated
           identically, so an adapter can pass `request.cookies.get(...)`
           straight through without pre-checking it.
        2. **Hash the id and look up the row.** The raw id is never
           compared against anything and never leaves this method; only
           `_core.hash_token(raw)` is used as the lookup key. **No row** ->
           `InvalidSession`.
        3. **`row.revoked`** -> `InvalidSession`. A logged-out or
           administratively killed session fails here on the VERY NEXT
           request -- this single check is the concrete form of the
           immediate-revocation property this module exists for.
        4. **`now >= row.absolute_expires_at`** -> `InvalidSession`. The
           hard ceiling, unaffected by how recently the session was used.
        5. **`now - row.last_seen_at >= idle_ttl`** -> `InvalidSession`.
           The sliding deadline. Checked AFTER the absolute one so a
           session that has blown through both is attributed to the
           deadline that is impossible to argue with; both raise
           identically, so the ordering is an internal reasoning aid, not
           an observable difference.
        6. **Load the user (`UserStore.get_by_id`). No user** ->
           `InvalidSession`. A deleted account's sessions stop
           authenticating immediately, with no cleanup job required -- a
           JWT minted before the deletion would keep working until its
           `exp`.
        7. **Otherwise live:** advance `last_seen_at` if it is stale by at
           least `touch_interval` (see below), and return a
           `SessionPrincipal` carrying the user's CURRENT roles.

        **Roles are read live, never cached on the session row.** Step 6
        costs one extra store read per request and buys the property that
        granting or revoking a role takes effect on the next request. The
        alternative -- snapshotting roles at login -- would recreate a JWT's
        own staleness window over a session's much longer lifetime, which
        would give up most of the reason to prefer sessions in the first
        place. A project that measures this read as a real bottleneck
        should cache the USER lookup behind its own short-TTL cache (a
        deliberate, visible decision with a bounded staleness it chooses),
        not silently denormalize roles onto the session row here.

        **The `touch_interval` write-rate bound.** `last_seen_at` is only
        written back when it is already at least `touch_interval` stale.
        Without this, every authenticated request -- including every
        cache-friendly `GET` -- becomes a database write, which is the
        single most common way a server-side-session design turns into a
        write-throughput problem. The cost is that `last_seen_at` may lag
        real activity by up to `touch_interval`, which shortens the
        EFFECTIVE idle window by at most that same amount; keeping
        `touch_interval` orders of magnitude smaller than `idle_ttl`
        (the defaults are 1 minute against 12 hours) makes that error
        negligible. It can only ever expire a session slightly EARLY,
        never keep a stale one alive."""
        if not raw_session_id:
            raise InvalidSession("No valid session was presented.")
        session_hash = _core.hash_token(raw_session_id)
        record = await self._sessions.get_by_hash(session_hash)
        if record is None:
            await self._emit_rejection(actor="anonymous")
            raise InvalidSession("No valid session was presented.")
        if record.revoked:
            await self._emit_rejection(actor=record.user_id, reason="revoked")
            raise InvalidSession("No valid session was presented.")
        now = self._now()
        if now >= record.absolute_expires_at:
            await self._emit_rejection(actor=record.user_id, reason="absolute_expiry")
            raise InvalidSession("No valid session was presented.")
        if now - record.last_seen_at >= self._idle_ttl:
            await self._emit_rejection(actor=record.user_id, reason="idle_expiry")
            raise InvalidSession("No valid session was presented.")
        user = await self._users.get_by_id(record.user_id)
        if user is None:
            await self._emit_rejection(actor=record.user_id, reason="unknown_user")
            raise InvalidSession("No valid session was presented.")
        last_seen_at = record.last_seen_at
        if now - last_seen_at >= self._touch_interval:
            await self._sessions.touch(session_hash, now)
            last_seen_at = now
        return SessionPrincipal(
            sub=user.id,
            roles=list(user.roles),
            session_hash=session_hash,
            created_at=record.created_at,
            last_seen_at=last_seen_at,
            absolute_expires_at=record.absolute_expires_at,
        )

    async def rotate(self, raw_session_id: str) -> IssuedSession:
        """Resolves a live session, REVOKES it, and issues a replacement
        for the same user -- the session-id rotation a caller runs at a
        PRIVILEGE BOUNDARY: immediately after a password change, after a
        step-up/re-authentication, or after a role grant. Raises
        `InvalidSession` (via `resolve`) if the incoming session was not
        live, so rotation can never mint a session out of an expired or
        revoked one.

        **The replacement inherits the original's absolute deadline**
        (`created_at` and `absolute_expires_at` are carried over verbatim,
        NOT recomputed from `now`). Rotation is a security operation, not a
        renewal: if it reset the absolute ceiling, a caller that rotated
        periodically -- or an attacker who could induce a rotation -- would
        have a mechanism for extending a session indefinitely, defeating
        the exact ceiling `absolute_ttl` exists to impose. `last_seen_at`
        IS reset to `now`, since the request performing the rotation is
        itself activity.

        The old session is revoked before the new one is added, so there is
        no instant at which both ids authenticate. A caller MUST set the
        returned cookie on its response -- the old id stops working the
        moment this returns."""
        principal = await self.resolve(raw_session_id)
        now = self._now()
        await self._sessions.revoke(principal.session_hash)
        raw = generate_session_id()
        record = SessionRecord(
            session_hash=_core.hash_token(raw),
            user_id=principal.sub,
            created_at=principal.created_at,
            last_seen_at=now,
            absolute_expires_at=principal.absolute_expires_at,
            revoked=False,
        )
        await self._sessions.add(record)
        if self._events is not None:
            await self._events.emit("auth.session.rotated", actor=principal.sub, outcome="success")
        return IssuedSession(session_id=raw, record=record)

    async def revoke(self, raw_session_id: str | None) -> None:
        """Revokes ONE session -- what a logout route calls. **Idempotent
        and never raises**, deliberately: a missing cookie, a blank cookie,
        an unknown id, and an already-revoked session all return `None`
        exactly as a successful revocation does.

        This mirrors `_core.AuthService.logout`'s own "a garbage/unknown
        token doesn't raise" contract, for the same two reasons. First,
        logout must be safe to retry -- a double-clicked button, a retried
        request after a dropped response, or a client clearing state on
        startup must not produce an error. Second, raising here would turn
        the logout endpoint into an ORACLE that distinguishes real session
        ids from fabricated ones, handing an attacker a way to test stolen
        cookie values without ever needing them to authenticate.

        The caller is still responsible for clearing the cookie on its
        response (`_cookies.clear_session_cookie_kwargs`) -- this method
        only kills the server-side record, which is the half that
        actually matters."""
        if not raw_session_id:
            return
        session_hash = _core.hash_token(raw_session_id)
        record = await self._sessions.get_by_hash(session_hash)
        await self._sessions.revoke(session_hash)
        if self._events is not None and record is not None:
            await self._events.emit("auth.session.revoked", actor=record.user_id, outcome="success")

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY session belonging to `user_id` -- "sign out
        everywhere". Called on a password reset (alongside
        `_core.RefreshTokenStore.revoke_all_for_user`, which does the same
        for the JWT path, so a project running both transports kills both
        with one reset), on a detected account compromise, and on an
        administrative ban or deactivation.

        Effective on the NEXT request against every affected session --
        there is no window to wait out, which is precisely the guarantee
        `AccountService.reset_password` can only approximate on the JWT
        side, where an already-minted access token stays valid until its
        own `exp` regardless of how many refresh families are revoked.
        Idempotent and non-raising for a user with no sessions, matching
        `revoke` above."""
        await self._sessions.revoke_all_for_user(user_id)
        if self._events is not None:
            await self._events.emit("auth.session.revoked_all", actor=user_id, outcome="success")

    async def _emit_rejection(self, *, actor: str, reason: str = "unknown_session") -> None:
        """Emits the `auth.session.rejected` audit event every `resolve`
        rejection path funnels through, carrying the SPECIFIC `reason` the
        wire response deliberately withholds (see `InvalidSession`'s
        docstring). This is the "a server-side audit log is where the real
        reason is recorded for a human" half of that posture -- the
        distinction exists, it simply never leaves the server. A no-op when
        no `AuthEventSink` was wired."""
        if self._events is not None:
            await self._events.emit(
                "auth.session.rejected", actor=actor, outcome="failure", reason=reason
            )
