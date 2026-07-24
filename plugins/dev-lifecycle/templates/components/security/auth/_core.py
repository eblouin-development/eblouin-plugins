"""Framework-neutral auth core: Argon2id password hashing (`PasswordService`),
PyJWT-based HS256 access/refresh tokens (`TokenService`), and the
`AuthService` orchestrator implementing register/login/refresh/logout --
including the refresh-token ROTATION-WITH-REUSE-DETECTION state machine,
the security-critical part of this module. Canon:
references/security/secure-baseline.md ("Hash passwords with a strong
adaptive algorithm... Tokens (JWT/session) validated fully -- signature,
expiry, audience/issuer -- with sensible expiry and secure rotation/
logout. Prefer short-lived access tokens with refresh over long-lived
static tokens").

Drop-in: copy this file into app/core/security/auth/_core.py (keep it
alongside fastapi.py/django.py from the same directory, once a later
stage adds them -- this component ships the framework-neutral core only;
wiring it into a FastAPI or Django backend block is separate work). Stdlib
+ PyJWT + argon2-cffi only -- **no FastAPI, Django, or SQLAlchemy import
anywhere in this file.** `UserStore` and `RefreshTokenStore` below are
`Protocol`s a framework adapter implements against its own ORM/session;
this module never touches a database, a request object, or a settings
object directly -- every secret/TTL/store it needs is passed in by the
caller.

**This file raises its OWN exception hierarchy** (`AuthError` and its
subclasses below) rather than importing
`templates/components/backend/error-envelope/errors.py`'s `AppError`/
`ErrorCode` -- that keeps this module importable with zero framework/
app-layer dependencies. Each exception's docstring names the `ErrorCode`
member (from that LOCKED, closed enum -- this module does NOT extend it)
a framework adapter's exception handler is expected to map it onto.

**The refresh-rotation state machine (`AuthService.refresh`) is the
security-critical core of this module** -- read its docstring and
`tests/test_core.py`'s reuse-detection tests before touching it. Summary:
every refresh token is single-use; presenting an already-used one means
the client's token was stolen and used by someone else first (or the
legitimate client raced itself) -- either way, the entire token FAMILY
(every token descended from one login) is revoked immediately, forcing a
fresh login. The persisted `RefreshRecord` (looked up by SHA-256 hash of
the raw token), never the JWT's own claims, is the sole source of truth
for whether a refresh token is still valid -- a validly-signed,
unexpired JWT whose row has been marked used/revoked/deleted is still
rejected, because trust lives in the store, not in what the client is
merely able to present.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

# ---------------------------------------------------------------------------
# Core exception hierarchy
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Base of every exception this module raises deliberately (as
    opposed to letting an unexpected bug propagate unchanged -- see
    `PasswordService.verify`'s docstring on not catching unexpected
    errors). A framework adapter's exception handler catches this
    hierarchy and maps each concrete subclass onto an existing
    `ErrorCode` member from `templates/components/backend/error-envelope/
    errors.py` -- that enum is a LOCKED, closed set this module does NOT
    extend; every subclass below documents which member it maps to."""


class InvalidCredentials(AuthError):
    """Login failed -- either the email is not registered, or the
    password did not match. Maps to `ErrorCode.UNAUTHENTICATED` (401).

    Deliberately the SAME exception (and the SAME generic message) for
    both causes: `AuthService.login` raises this identically whether the
    email was never registered or the password was simply wrong, so a
    client (or an attacker probing for valid emails) cannot distinguish
    "no such account" from "wrong password" -- the classic user-
    enumeration-via-login-error class of bug. See `AuthService.login`'s
    own docstring for the timing-defense half of this (calling
    `PasswordService.dummy_verify()` on the not-found path)."""


class InvalidToken(AuthError):
    """A presented access or refresh token failed to validate. Covers
    every failure mode: bad signature, expired, wrong issuer, malformed/
    missing required claims, wrong `type` claim (an access token
    presented where a refresh token is expected, or vice versa), unknown
    to the refresh-token store (a validly-signed JWT with no matching
    persisted row), or belonging to a refresh-token family that has been
    revoked. Maps to `ErrorCode.UNAUTHENTICATED` (401).

    Deliberately indistinguishable, at this exception-TYPE level, from
    `TokenReused` below -- see that class's docstring for why a client
    must never be able to tell the two apart from the wire response."""


class TokenReused(AuthError):
    """A refresh token that was already rotated (its persisted record's
    `used_at` is already set) was presented again. This is the reuse-
    detection signal: single-use refresh tokens mean a second
    presentation of an already-used token means either (a) the token was
    stolen and an attacker used it after -- or before -- the legitimate
    client, or (b) the legitimate client raced itself (e.g. a retried
    request after a dropped response). Either way, this cannot be
    resolved safely, so raising this exception has ALREADY, as a side
    effect, revoked the token's entire FAMILY (every refresh token
    descended from the same login, via `RefreshTokenStore.revoke_family`)
    by the time the caller sees it -- see `AuthService.refresh`'s
    docstring for the full state machine.

    Maps to `ErrorCode.UNAUTHENTICATED` (401) -- the SAME code as
    `InvalidToken`, deliberately. A client must not be able to
    distinguish "reuse was detected and your whole session was killed"
    from "this token was simply invalid" from the wire response alone:
    revealing that distinction to whoever is holding the stolen-but-
    already-rotated token confirms reuse detection exists and just
    fired, which is exactly the information an attacker probing the
    endpoint would find useful. A server-side audit log is where reuse
    gets flagged for a human to investigate -- not the response the
    client (attacker or legitimate) receives."""


class EmailAlreadyExists(AuthError):
    """`AuthService.register` was called with an email that already has
    an account. Maps to `ErrorCode.CONFLICT` (409)."""


class InvalidSingleUseToken(AuthError):
    """A single-use token (email-verification or password-reset) presented
    to `SingleUseTokenService.consume` could not be accepted. Maps to
    `ErrorCode.UNAUTHENTICATED` (401).

    Deliberately ONE exception for every rejection reason -- unknown hash
    (never issued, or issued by a different environment), already
    `used_at` (single-use token presented a second time), expired
    (`expires_at` has passed), and purpose mismatch (a `"verify"` token
    presented to a reset flow, or vice versa) all collapse to this SAME
    type with the SAME generic message. This mirrors `InvalidCredentials`
    and the `InvalidToken`/`TokenReused` pairing above: the wire response
    for "this link is bad" must not let a caller distinguish "already
    used" (which would confirm the token was once valid and consumed --
    useful to an attacker who intercepted an old email) from "expired"
    from "never existed". A server-side audit event (via `AuthEventSink`,
    emitted by `AccountService`) is where the real reason is recorded for
    a human to investigate, exactly as `TokenReused`'s own docstring
    describes for refresh-token reuse."""


# ---------------------------------------------------------------------------
# Password hashing (Argon2id)
# ---------------------------------------------------------------------------


class PasswordService:
    """Wraps `argon2.PasswordHasher` -- Argon2id (argon2-cffi's default
    `Type.ID`) is left at its default, which this class does not
    override; Argon2id is the OWASP-recommended default for new
    applications (resistant to both GPU-cracking and side-channel
    attacks, unlike the pure-Argon2i/Argon2d variants). A custom
    `argon2.PasswordHasher` instance can be passed in (e.g. a project
    tuning `memory_cost`/`time_cost` for its own hardware budget) --
    `None` (the default) uses argon2-cffi's own library defaults, which
    are already reasonable for a starter kit."""

    def __init__(self, hasher: PasswordHasher | None = None) -> None:
        self._hasher = hasher or PasswordHasher()
        # A precomputed, throwaway Argon2id hash of a fixed, never-used
        # password -- see dummy_verify() below. Computed once at
        # construction, not per call, so dummy_verify()'s cost is exactly
        # one Argon2id verify, matching the cost of a real verify() call.
        self._dummy_hash = self._hasher.hash("dummy-password-never-used-for-a-real-account")

    def hash(self, password: str) -> str:
        """Hashes `password` with Argon2id, returning an encoded hash
        string that embeds the algorithm, its parameters, the salt, and
        the digest -- everything `verify()`/`needs_rehash()` need, so
        nothing else about the hashing parameters needs to be stored
        alongside it."""
        return self._hasher.hash(password)

    def verify(self, stored_hash: str, password: str) -> bool:
        """Returns `True` if `password` matches `stored_hash`, `False`
        otherwise. Both a wrong password (`VerifyMismatchError`) and a
        corrupt/foreign-format stored hash (`InvalidHashError`) collapse
        to the same `False` return -- deliberately: the caller must never
        be able to tell "wrong password" apart from "the stored hash
        itself is malformed" through this method's return value, which
        would leak information about the account's stored data rather
        than just whether the guess was right. Any OTHER exception
        (a bug, an unexpected argon2-cffi internal error) is NOT caught
        here and propagates -- silently converting an unexpected error
        into "verification failed" would hide real bugs (e.g. a
        misconfigured hasher) behind an indistinguishable-from-normal
        failed-login response."""
        try:
            self._hasher.verify(stored_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False
        return True

    def needs_rehash(self, stored_hash: str) -> bool:
        """Delegates to `PasswordHasher.check_needs_rehash` -- `True` when
        `stored_hash` was produced with different (typically weaker)
        parameters than this instance's current `time_cost`/
        `memory_cost`/`parallelism`/`type`. Lets a login flow
        transparently upgrade an old hash (recompute and store a fresh
        one right after a successful `verify()`) as this service's own
        parameters are tightened over time, without a bulk migration."""
        return self._hasher.check_needs_rehash(stored_hash)

    def dummy_verify(self) -> None:
        """User-enumeration timing defense: runs one Argon2id verify
        against a precomputed throwaway hash (see `__init__`), spending
        the same CPU time a real `verify()` call would, then discards the
        (always-`False`) result. `AuthService.login` calls this on the
        "email not found" path specifically so that path costs the same
        wall-clock time as the "email found, password checked" path --
        without it, an attacker could distinguish a registered email from
        an unregistered one purely by how fast the login endpoint
        responds (Argon2id's whole point is to be slow; skipping it
        entirely for unknown emails would make that slowness itself the
        leak). Never raises -- reuses `verify()`, which only ever returns
        `bool`."""
        self.verify(self._dummy_hash, "irrelevant-guess-always-mismatches")


# ---------------------------------------------------------------------------
# Tokens (PyJWT, HS256, shared secret)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccessClaims:
    """The decoded, verified claims of an access token, as returned by
    `TokenService.decode_access` -- what `AuthService.resolve_access`
    hands back to a framework's `get_current_principal` dependency."""

    sub: str
    roles: list[str]
    jti: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class RefreshClaims:
    """The decoded, verified claims of a refresh token. Also returned
    directly by `TokenService.mint_refresh` (alongside the raw JWT) so a
    caller can persist a `RefreshRecord` without a redundant decode of the
    token it just minted."""

    sub: str
    jti: str
    family_id: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class TokenPair:
    """An access/refresh token pair, as returned by every `AuthService`
    method that mints new tokens (`login`, `refresh`)."""

    access: str
    refresh: str


class TokenService:
    """Mints and verifies HS256 JWTs against one shared `signing_key`.

    `now` is a required, injected `Callable[[], datetime]` (no default --
    the framework layer supplies `lambda: datetime.now(timezone.utc)` or
    equivalent) so every test in `tests/` can advance time deterministically
    (expiry, rotation) without sleeping in a test or monkeypatching a
    module-global clock. `now()` MUST return a timezone-aware `datetime` in
    UTC -- a naive datetime would make the `exp`/`iat` timestamp math
    ambiguous about which timezone it's relative to.

    Access token claims: `sub` (principal id, as a string), `type`
    (literal `"access"`), `iat`, `exp`, `iss`, `jti` (a fresh `uuid4` hex
    per token), `roles` (list of role strings).

    Refresh token claims: `sub`, `type` (literal `"refresh"`), `iat`,
    `exp`, `iss`, `jti`, `fid` (the token's FAMILY id -- shared by every
    refresh token descended from one login, via rotation; see
    `AuthService.refresh`).

    Decoding verifies the signature (HS256 only -- `algorithms=["HS256"]`
    is passed explicitly on every decode call, never inferred from the
    token's own header, which would let a malicious token dictate its own
    verification algorithm), issuer, and the presence of every claim
    above via `options={"require": [...]}}`. **Expiry is checked
    manually against the injected `now()`, not PyJWT's own built-in
    wall-clock check** (`verify_exp`/`verify_iat` are explicitly turned
    OFF in the `options` passed to `jwt.decode`) -- PyJWT's own expiry
    validation always compares against the real system clock
    (`datetime.now(tz=timezone.utc)`) with no way to inject a substitute,
    which would make `tests/test_core.py`'s "advance the injected `now`
    past the TTL, assert `InvalidToken`" tests either flaky (racing the
    real clock) or entirely disconnected from the `now` this class was
    actually constructed with. Checking it manually, against the exact
    same `now` every other part of this class uses, is what makes expiry
    fully deterministic under test. Then additionally asserts the decoded
    `type` claim matches what the caller asked for
    (`decode_access` requires `type == "access"`, `decode_refresh`
    requires `type == "refresh"`). An access token presented where a
    refresh token is expected (or vice versa) is REJECTED at this `type`
    check, not silently accepted just because it happens to be a
    validly-signed JWT from the same issuer -- the two token kinds are
    security-distinct (a refresh token grants a NEW token pair; an access
    token does not) and must never be interchangeable.

    Every failure mode -- bad signature, expired, wrong issuer, malformed/
    missing claims, wrong `type` -- raises `InvalidToken`, never a raw
    `jwt.PyJWTError` subclass, so callers never need to import PyJWT's own
    exception hierarchy to handle a decode failure."""

    def __init__(
        self,
        signing_key: str,
        *,
        issuer: str,
        access_ttl: timedelta,
        refresh_ttl: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        if not signing_key:
            raise ValueError("signing_key must not be empty -- an empty HS256 key is not a secret.")
        self._signing_key = signing_key
        self._issuer = issuer
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._now = now

    def mint_access(self, sub: str, roles: Sequence[str]) -> str:
        """Mints a fresh access token for principal `sub` with the given
        `roles`. `exp` is `now() + access_ttl`."""
        current = self._now()
        claims = {
            "sub": sub,
            "type": "access",
            "iat": _to_timestamp(current),
            "exp": _to_timestamp(current + self._access_ttl),
            "iss": self._issuer,
            "jti": uuid.uuid4().hex,
            "roles": list(roles),
        }
        return jwt.encode(claims, self._signing_key, algorithm="HS256")

    def mint_refresh(self, sub: str, family_id: str) -> tuple[str, RefreshClaims]:
        """Mints a fresh refresh token for principal `sub` in token family
        `family_id`. `exp` is `now() + refresh_ttl`. Returns BOTH the raw
        JWT (to hand to the client) AND its decoded `RefreshClaims` (so
        the caller -- `AuthService`'s rotation logic -- can build and
        persist a `RefreshRecord` without redundantly decoding the token
        it just minted)."""
        current = self._now()
        expires_at = current + self._refresh_ttl
        jti = uuid.uuid4().hex
        claims = {
            "sub": sub,
            "type": "refresh",
            "iat": _to_timestamp(current),
            "exp": _to_timestamp(expires_at),
            "iss": self._issuer,
            "jti": jti,
            "fid": family_id,
        }
        token = jwt.encode(claims, self._signing_key, algorithm="HS256")
        decoded = RefreshClaims(
            sub=sub,
            jti=jti,
            family_id=family_id,
            issued_at=current,
            expires_at=expires_at,
        )
        return token, decoded

    def decode_access(self, token: str) -> AccessClaims:
        """Verifies and decodes an access token. Raises `InvalidToken` on
        any failure, including a refresh token presented here."""
        payload = self._decode(token, expected_type="access")
        try:
            return AccessClaims(
                sub=str(payload["sub"]),
                roles=list(payload.get("roles", [])),
                jti=str(payload["jti"]),
                issued_at=_from_timestamp(payload["iat"]),
                expires_at=_from_timestamp(payload["exp"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidToken("The access token is malformed.") from exc

    def decode_refresh(self, token: str) -> RefreshClaims:
        """Verifies and decodes a refresh token. Raises `InvalidToken` on
        any failure, including an access token presented here."""
        payload = self._decode(token, expected_type="refresh")
        try:
            return RefreshClaims(
                sub=str(payload["sub"]),
                jti=str(payload["jti"]),
                family_id=str(payload["fid"]),
                issued_at=_from_timestamp(payload["iat"]),
                expires_at=_from_timestamp(payload["exp"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidToken("The refresh token is malformed.") from exc

    def _decode(self, token: str, *, expected_type: str) -> dict[str, object]:
        try:
            payload = jwt.decode(
                token,
                self._signing_key,
                algorithms=["HS256"],
                issuer=self._issuer,
                options={
                    "require": ["exp", "iat", "iss", "sub", "jti", "type"],
                    # Expiry is verified manually below, against this
                    # instance's injected `now()` -- see this class's
                    # docstring for why PyJWT's own built-in exp check
                    # (always against the real system clock) is turned off
                    # here rather than relied on.
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError as exc:
            raise InvalidToken("The token is invalid, expired, or malformed.") from exc
        if payload.get("type") != expected_type:
            raise InvalidToken(f"Expected a {expected_type!r} token, got {payload.get('type')!r}.")
        if self._now() >= _from_timestamp(payload["exp"]):
            raise InvalidToken("The token has expired.")
        return payload


def _to_timestamp(value: datetime) -> int:
    """Converts a timezone-aware `datetime` to a POSIX timestamp (whole
    seconds) for embedding as a JWT numeric-date claim (`iat`/`exp`)."""
    return int(value.timestamp())


def _from_timestamp(value: object) -> datetime:
    """Converts a JWT numeric-date claim back to a timezone-aware UTC
    `datetime`. Raises `TypeError`/`ValueError` on a non-numeric value --
    left uncaught here so `decode_access`/`decode_refresh`'s callers wrap
    it into `InvalidToken` (a malformed claim is a malformed token, not a
    crash)."""
    return datetime.fromtimestamp(float(value), tz=timezone.utc)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Refresh-token store: Protocol + record (server keeps SHA-256, never raw)
# ---------------------------------------------------------------------------


def hash_token(raw: str) -> str:
    """`hashlib.sha256(raw.encode()).hexdigest()` -- the ONLY form of a
    refresh token this module ever persists; the raw JWT itself is never
    written to storage.

    A fast cryptographic hash (SHA-256), not a slow password KDF (Argon2/
    bcrypt/scrypt), is the CORRECT choice here, deliberately different
    from `PasswordService` above: a password is a low-entropy secret a
    human chose, vulnerable to offline brute-force/dictionary attack
    against a stolen hash, which is exactly what a slow KDF defends
    against. A refresh token is a high-entropy value THIS module
    generated (a signed JWT, effectively random from an attacker's
    perspective) -- brute-forcing a SHA-256 preimage of a 256-bit-entropy
    input is computationally infeasible regardless of hash speed, so a
    slow KDF here would only add CPU cost on every single refresh-token
    lookup (every token refresh, every logout) for zero additional
    security. Hashing at all (rather than storing the raw token) still
    matters: it means a read-only compromise of the store's rows (a
    leaked DB backup, a compromised replica) does not hand out live,
    directly-usable refresh tokens."""
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class RefreshRecord:
    """One persisted refresh-token row. `token_hash` (from `hash_token`)
    is the lookup key -- never the raw token. `used_at` is `None` until
    the token is rotated (see `AuthService.refresh`), at which point it's
    set and the row is RETAINED, not deleted -- that retention is what
    lets a second presentation of the same (now-used) token be recognized
    as reuse rather than simply "not found". `revoked` is set on every row
    in a family at once by `RefreshTokenStore.revoke_family` (on reuse
    detection, or on logout)."""

    token_hash: str
    jti: str
    family_id: str
    user_id: str
    issued_at: datetime
    expires_at: datetime
    used_at: datetime | None
    revoked: bool


class RefreshTokenStore(Protocol):
    """The storage seam `AuthService`'s refresh-rotation state machine
    runs against -- a framework adapter implements this against its own
    ORM/session (e.g. a SQLAlchemy or Django model table keyed by
    `token_hash`). All methods are `async` since a real implementation
    talks to a database.

    Implementations MUST make `add`/`mark_used`/`revoke_family` durable
    (committed) before returning -- `AuthService.refresh` relies on
    `mark_used` having taken effect before it returns the new token pair,
    so a concurrent second presentation of the just-rotated token sees the
    updated `used_at` and is correctly flagged as reuse rather than racing
    past this implementation's own write."""

    async def add(self, record: RefreshRecord) -> None: ...

    async def get_by_hash(self, token_hash: str) -> RefreshRecord | None: ...

    async def mark_used(self, token_hash: str, used_at: datetime) -> None: ...

    async def revoke_family(self, family_id: str) -> None: ...

    async def revoke_all_for_user(self, user_id: str) -> None:
        """Revokes EVERY refresh-token family belonging to `user_id` --
        not just one family (`revoke_family` above), all of them, across
        every device/session the user is currently logged in on. Added
        for `AccountService.reset_password`: changing a password after a
        reset must kill every existing session, since the old password
        (and whatever refresh tokens were minted while it was live) can no
        longer be trusted to have been the only thing protecting the
        account -- this is the same "kill everything, force a fresh
        login" posture `AuthService.refresh`'s reuse-detection step 4
        takes for a single family, generalized to all of them. Same
        durable-commit contract as `add`/`mark_used`/`revoke_family`
        above."""
        ...


# ---------------------------------------------------------------------------
# User store: Protocol + record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    """The minimal user shape `AuthService` needs -- deliberately NOT a
    full user profile (no name, no created_at, no arbitrary metadata);
    a framework adapter's own ORM model can carry more fields, this
    dataclass only needs to carry what `register`/`login`/`refresh` read
    or write."""

    id: str
    email: str
    password_hash: str
    roles: tuple[str, ...]
    email_verified: bool = False
    """Whether `AccountService.verify_email` has ever successfully consumed
    a `"verify"` single-use token for this user. Defaults to `False` so
    every existing call site that constructs a `UserRecord` positionally
    or with just the original four fields keeps working unchanged --
    added for `AuthService.login`'s optional `require_verification` gate
    (see that method's docstring) and set by `UserStore.
    mark_email_verified` below, never assigned to directly by
    `AuthService`/`AccountService`."""


class UserStore(Protocol):
    """The storage seam `AuthService` runs registration/login against --
    a framework adapter implements this against its own user model. All
    methods `async`, matching `RefreshTokenStore`."""

    async def get_by_email(self, email: str) -> UserRecord | None: ...

    async def get_by_id(self, id: str) -> UserRecord | None: ...

    async def create(self, email: str, password_hash: str, roles: Sequence[str]) -> UserRecord: ...

    async def mark_email_verified(self, user_id: str, at: datetime) -> None:
        """Sets `UserRecord.email_verified` to `True` for `user_id` (and,
        typically, records `at` as a `verified_at` timestamp in the
        adapter's own schema, even though that timestamp isn't part of
        `UserRecord` itself). Called by `AccountService.verify_email`
        after a `"verify"` single-use token consumes successfully -- never
        called directly by `AuthService`."""
        ...

    async def set_password_hash(self, user_id: str, new_hash: str) -> None:
        """Overwrites `user_id`'s stored password hash with `new_hash` (an
        Argon2id hash already produced by `PasswordService.hash` -- this
        method never hashes anything itself, matching `UserStore.create`'s
        own contract of receiving an already-hashed value). Called by
        `AccountService.reset_password`; the plaintext new password is
        never passed to this method or persisted."""
        ...


# ---------------------------------------------------------------------------
# Email seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmailMessage:
    """A plain-text (never HTML -- no templating engine or injection
    surface to worry about here) email, as `EmailSender.send` receives
    it. `AccountService` is the only thing in this module that builds
    one -- it is the sole caller of `EmailSender.send`."""

    to: str
    subject: str
    body: str


class EmailSender(Protocol):
    """The email-delivery seam `AccountService` sends verification/reset
    messages through -- a framework adapter (or a project's own thin
    wrapper around SES/Postmark/SMTP/whatever) implements this. `async`
    for the same reason every storage `Protocol` in this module is:
    `send` almost always crosses a network boundary in a real
    implementation. This module ships exactly one implementation
    (`ConsoleEmailSender` below) -- a real one is application/
    infrastructure code, not part of this framework-neutral core.

    **Implementations MUST NOT let delivery latency or delivery failure
    affect the caller.** `send()` is expected to return promptly and to
    NEVER raise -- deliver OUT-OF-BAND (e.g. hand the message to a
    background task/queue and return immediately) and swallow+log any
    delivery error internally rather than propagating it. Two callers in
    this module depend on that contract to stay correct, not just fast:
    `AccountService.request_password_reset`'s known-email branch awaits
    `send()` and then returns `None` exactly like the unknown-email
    branch -- if `send()` could raise or could block for a real SMTP
    round-trip, a delivery failure or a slow relay would turn into either
    a different HTTP outcome or a different response latency for a known
    vs. an unknown email, which is precisely the account-enumeration
    oracle this method's docstring says it must never become. Likewise a
    failed verification-email `send()` during registration (`AccountService.
    request_email_verification`, called from a framework adapter's
    register handler) must never be able to brick a just-created account.
    `ConsoleEmailSender` (fast, synchronous, and it only ever logs -- it
    cannot itself fail in a way worth propagating) satisfies this
    trivially; a real SMTP-backed sender must actively deliver on a
    background task/thread and catch its own errors -- see this
    component's FastAPI backend's `SmtpEmailSender` for a reference
    implementation."""

    async def send(self, message: EmailMessage) -> None: ...


class ConsoleEmailSender:
    """**DEV-ONLY.** Logs `message` -- INCLUDING its body, which for
    `AccountService`'s verify/reset emails contains the raw single-use
    token -- to a passed-in or module `logging.Logger`, instead of
    actually delivering it anywhere. Its entire purpose is to surface the
    token somewhere a developer running the app locally can see it and
    complete the verify/reset flow by hand, with zero email
    infrastructure (SMTP credentials, a transactional-email provider
    account) required to exercise auth locally or in CI.

    This deliberately logs a secret. That does NOT contradict this
    module's "tokens never appear in logs" posture elsewhere (the
    `AUDIT` log `AuthEventSink` feeds, and this component's guidance for
    PRODUCTION logging generally) -- that rule governs logs a real
    deployment ships to a log aggregator that other people (support,
    on-call, an attacker who compromises log storage) can read after the
    fact. This class is not that: it is a **dev-environment stand-in for
    an email provider**, not a log statement embedded in the normal
    request-handling path, and it must NEVER be constructed in a
    production wiring -- a project's own settings/environment branch is
    what enforces that (see this component's README's "app wiring"
    note), not anything in this class itself. `AccountService`'s own
    audit events (via `AuthEventSink`), by contrast, never carry a raw
    token -- see `AccountService.verify_email`/`request_password_reset`/
    `reset_password`'s docstrings."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("auth.email.console")

    async def send(self, message: EmailMessage) -> None:
        self._logger.info(
            "DEV EMAIL to=%s subject=%s\n%s",
            message.to,
            message.subject,
            message.body,
        )


# ---------------------------------------------------------------------------
# Single-use tokens (email verification, password reset)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SingleUseTokenRecord:
    """One persisted single-use-token row -- the same hash-at-rest shape
    as `RefreshRecord` above, and for the identical reason (see
    `hash_token`'s docstring): `token_hash` is the lookup key, the raw
    token is never stored. `purpose` (`"verify"` or `"reset"`, though
    this module does not enumerate the allowed values as a closed set --
    a project could add its own) scopes a token to exactly the flow it
    was issued for; `SingleUseTokenService.consume` rejects a token
    presented for any purpose other than the one it was issued with (see
    `InvalidSingleUseToken`'s docstring on why that collapses to the same
    generic exception as every other rejection reason). `used_at` is
    `None` until `consume` succeeds, at which point it's set and the row
    is RETAINED -- exactly `RefreshRecord`'s "retain, don't delete"
    posture, so a second presentation of an already-consumed token is
    recognized as reuse (`used_at is not None`) rather than simply
    treated as unknown."""

    token_hash: str
    user_id: str
    purpose: str
    expires_at: datetime
    used_at: datetime | None
    created_at: datetime


class SingleUseTokenStore(Protocol):
    """The storage seam `SingleUseTokenService` runs against -- a
    framework adapter implements this against its own ORM/session,
    typically one table shared by both `"verify"` and `"reset"` purposes
    (distinguished by the `purpose` column), though a project could split
    them into two tables if it prefers.

    Implementations MUST make `add`/`mark_used` durable (committed)
    before returning -- the SAME durable-commit contract
    `RefreshTokenStore`'s own docstring documents, and for the identical
    reason: `SingleUseTokenService.consume` relies on `mark_used` having
    taken effect before it returns, so a concurrent second presentation
    of the just-consumed token sees the updated `used_at` and is
    correctly rejected as reuse rather than racing past this
    implementation's own write."""

    async def add(self, record: SingleUseTokenRecord) -> None: ...

    async def get_by_hash(self, token_hash: str) -> SingleUseTokenRecord | None: ...

    async def mark_used(self, token_hash: str, used_at: datetime) -> None: ...


class SingleUseTokenService:
    """Mints and consumes single-use tokens for `AccountService`'s
    verify-email and reset-password flows. `now` is injected the same
    way `TokenService`'s and `AuthService`'s are (required, no default),
    so expiry is deterministic under test."""

    def __init__(self, store: SingleUseTokenStore, now: Callable[[], datetime]) -> None:
        self._store = store
        self._now = now

    async def issue(self, user_id: str, purpose: str, ttl: timedelta) -> str:
        """Mints a fresh raw token (`secrets.token_urlsafe(32)` -- 32
        bytes, ~256 bits of entropy from a CSPRNG, base64url-encoded),
        hashes it (`hash_token`, the SAME SHA-256-hex scheme
        `RefreshTokenStore` uses, and for the same reason: this is a
        high-entropy, module-generated value, not a low-entropy secret a
        human chose, so a fast cryptographic hash is the right tool, not
        a slow password KDF), persists a `SingleUseTokenRecord` with
        `expires_at = now() + ttl`, and returns the RAW token. The raw
        token is NEVER stored -- only its hash -- so a read-only
        compromise of this store's rows does not hand out live,
        directly-usable tokens, exactly `RefreshRecord`'s own posture."""
        raw = secrets.token_urlsafe(32)
        current = self._now()
        record = SingleUseTokenRecord(
            token_hash=hash_token(raw),
            user_id=user_id,
            purpose=purpose,
            expires_at=current + ttl,
            used_at=None,
            created_at=current,
        )
        await self._store.add(record)
        return raw

    async def consume(self, raw: str, purpose: str) -> str:
        """Hashes `raw` and looks it up. Raises `InvalidSingleUseToken`
        if: no row matches the hash; the row's `used_at` is already set
        (reuse); the row's `purpose` does not match the `purpose`
        argument; or the row is expired (`expires_at <= now()`) -- see
        `InvalidSingleUseToken`'s own docstring for why all four
        collapse to the identical exception type and message. On
        success, marks the row used (`mark_used(token_hash, now())`, so
        it can never validate again) and returns `record.user_id` -- the
        principal the caller (`AccountService`) should act on."""
        token_hash = hash_token(raw)
        record = await self._store.get_by_hash(token_hash)
        if record is None:
            raise InvalidSingleUseToken("This link is invalid or has expired.")
        if record.used_at is not None:
            raise InvalidSingleUseToken("This link is invalid or has expired.")
        if record.purpose != purpose:
            raise InvalidSingleUseToken("This link is invalid or has expired.")
        if record.expires_at <= self._now():
            raise InvalidSingleUseToken("This link is invalid or has expired.")
        await self._store.mark_used(token_hash, self._now())
        return record.user_id


# ---------------------------------------------------------------------------
# Per-account lockout
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptRecord:
    """One account's current failed-login bookkeeping, as `LockoutStore`
    persists it. `account_key` is typically the user's id (what
    `AuthService.login` passes below) -- a framework adapter is free to
    key it some other way (e.g. `f"{user_id}:{client_ip}"`) as long as it
    is consistent between `is_locked`/`record_failure`/`clear` calls for
    the same logical account. `failure_count` and `first_failure_at`
    reset together whenever `LockoutPolicy.record_failure` observes the
    rolling window has elapsed since `last_failure_at` -- see that
    method's own docstring. `locked_until` is `None` until
    `failure_count` first reaches the configured threshold."""

    account_key: str
    failure_count: int
    first_failure_at: datetime
    last_failure_at: datetime
    locked_until: datetime | None


class LockoutStore(Protocol):
    """Dumb persistence for `AttemptRecord` rows -- ALL of the counting,
    threshold, and rolling-window logic lives in `LockoutPolicy` below,
    not here; this `Protocol` only reads and writes whatever
    `AttemptRecord` `LockoutPolicy` hands it. A framework adapter
    implements this against its own ORM/session, or even a fast
    key-value store (Redis) given lockout state has no need for
    relational structure.

    Unlike `RefreshTokenStore`/`SingleUseTokenStore`, this `Protocol`
    does NOT document a strict single-transaction durability
    requirement -- see `LockoutPolicy`'s own docstring for the
    (deliberate, accepted) relaxation and why it can never compromise an
    account."""

    async def get(self, account_key: str) -> AttemptRecord | None: ...

    async def upsert(self, record: AttemptRecord) -> None: ...

    async def clear(self, account_key: str) -> None: ...


class LockoutPolicy:
    """Pure counting/threshold logic over `LockoutStore`'s dumb
    persistence: after `max_failures` consecutive failed logins for one
    account within a rolling `window`, the account is locked for
    `lockout_duration`. `now` is injected the same way every other clock
    in this module is (required, no default) so lockout timing is
    deterministic under test.

    **Accepted non-atomic relaxation, by design -- contrast with
    `AuthService.refresh`'s reuse detection.** `record_failure` below
    does a read (`store.get`), computes the next state in Python, then
    writes it back (`store.upsert`) -- a read-modify-write with no
    transactional guarantee that a concurrent call against the SAME
    `account_key` can't interleave between the read and the write. That
    is DELIBERATELY not hardened into a single atomic store operation:
    the refresh-token reuse-detection state machine cannot tolerate this
    relaxation because a race there can let a stolen token slip past
    detection entirely (a correctness AND security failure). A lockout
    race, by contrast, at absolute worst lets an attacker's concurrent
    guesses land a request or two beyond the configured threshold before
    the lock takes visible effect -- it can NEVER let a WRONG password
    succeed (that still requires `PasswordService.verify` to return
    `True`, which lockout racing has no influence over at all) and can
    NEVER compromise the account by itself; it only ever delays exactly
    when the lock becomes effective by a small, bounded amount. Given
    that ceiling, spending the operational complexity of a single-
    transaction compare-and-swap store implementation for every project
    that vendors this component is not worth it for what it would buy."""

    def __init__(
        self,
        store: LockoutStore,
        *,
        max_failures: int,
        lockout_duration: timedelta,
        window: timedelta,
        now: Callable[[], datetime],
    ) -> None:
        self._store = store
        self._max_failures = max_failures
        self._lockout_duration = lockout_duration
        self._window = window
        self._now = now

    async def is_locked(self, account_key: str) -> bool:
        """`True` iff `account_key` currently has a `locked_until` in the
        future. `False` for an account with no record at all, a record
        that never crossed the threshold, or one whose `locked_until` has
        already passed -- this method does NOT itself clear an expired
        lock's bookkeeping (that happens naturally the next time
        `record_failure` observes the rolling window has elapsed, or via
        an explicit `clear`); it only reports current lock status."""
        record = await self._store.get(account_key)
        if record is None or record.locked_until is None:
            return False
        return self._now() < record.locked_until

    async def record_failure(self, account_key: str) -> bool:
        """Records one more failed login for `account_key` and returns
        `True` iff THIS failure is the one that just crossed
        `max_failures` (i.e. the count was below the threshold before
        this call and is at the threshold now) -- `False` on every other
        call, including calls before the threshold and calls after an
        account is already locked. This lets a caller (`AuthService.
        login`) emit a distinct `auth.lockout.triggered` audit event
        exactly once per lock, rather than once per failed attempt.

        **Rolling window:** if no record exists yet, or the existing
        record's `last_failure_at` is older than `window`, the count
        starts fresh at 1 (as if this were the account's first-ever
        failure) rather than accumulating onto a stale streak -- a
        failure from three days ago should not still count towards
        today's threshold. Otherwise the count increments from the
        existing record.

        Once `failure_count` reaches `max_failures`, `locked_until` is
        (re)set to `now() + lockout_duration` on this and every
        subsequent call that still lands within the same (non-reset)
        streak -- an account that keeps failing while already locked
        stays locked at least `lockout_duration` from its MOST RECENT
        failure, not just its first."""
        current = self._now()
        existing = await self._store.get(account_key)
        reset = existing is None or (current - existing.last_failure_at) > self._window
        if reset:
            new_count = 1
            first_failure_at = current
        else:
            new_count = existing.failure_count + 1
            first_failure_at = existing.first_failure_at
        just_crossed = new_count == self._max_failures
        locked_until = current + self._lockout_duration if new_count >= self._max_failures else None
        await self._store.upsert(
            AttemptRecord(
                account_key=account_key,
                failure_count=new_count,
                first_failure_at=first_failure_at,
                last_failure_at=current,
                locked_until=locked_until,
            )
        )
        return just_crossed

    async def clear(self, account_key: str) -> None:
        """Clears `account_key`'s failure bookkeeping entirely (via
        `LockoutStore.clear`) -- called by `AuthService.login` on a
        successful login, so a legitimate login after some earlier failed
        attempts (but before ever crossing the threshold) resets the
        streak, matching the everyday expectation that "you eventually
        got it right" shouldn't count against a future unrelated string
        of typos."""
        await self._store.clear(account_key)


# ---------------------------------------------------------------------------
# Audit seam
# ---------------------------------------------------------------------------


class SessionRevoker(Protocol):
    """The ONE method `AccountService` needs from the server-side-session
    path: "kill every session this user has." `_sessions.SessionService`
    satisfies this structurally -- no registration, no subclassing, and
    crucially **no import**: declaring the shape here rather than importing
    `_sessions.SessionService` is what keeps this file standalone-importable
    (`_core.py` depends on no sibling in this component, which is what lets
    a project vendor it alone).

    Deliberately narrow. `AccountService` has no business creating,
    resolving, or rotating sessions -- it only ever needs to revoke them
    wholesale, on a password reset. A wider Protocol here would invite
    coupling that isn't needed and would make this seam harder to satisfy
    with something other than `SessionService` (an app that keeps sessions
    somewhere unusual can implement this single method and be done)."""

    async def revoke_all_for_user(self, user_id: str) -> None: ...


class AuthEventSink(Protocol):
    """Lets `AuthService`/`AccountService` emit auth events (login
    success/failure/denial, lockout triggering, email verification,
    password reset) WITHOUT this module importing the audit-logging
    component (`templates/components/security/audit-logging/`) --
    keeping `_core.py`'s "stdlib + PyJWT + argon2-cffi only, zero
    framework/app import" posture intact. A project wires a thin adapter
    around this `Protocol` that calls that component's own
    `audit_event(action, actor=actor, resource=..., outcome=outcome,
    **extra)` (or an equivalent audit sink), rather than this module
    depending on it directly.

    `action` is a short verb phrase (`"auth.login"`,
    `"auth.lockout.triggered"`, `"auth.password.reset_completed"`),
    `actor` an identifier string (a user id, `"anonymous"` for an
    unauthenticated caller, or `"user:unknown"` for a password-reset
    request against an email with no account -- see `AccountService.
    request_password_reset`'s own docstring on why that path never uses
    the submitted email as the actor), `outcome` one of `"success"`,
    `"failure"`, `"denied"` (matching the audit-logging component's own
    closed set), and `**extra` anything else worth recording. Every
    caller in this module treats `events` as OPTIONAL (`None` is a
    valid, no-op default) -- a project that hasn't wired an audit sink
    yet still gets a fully working `AuthService`/`AccountService`, just
    without the audit trail."""

    async def emit(self, action: str, *, actor: str, outcome: str, **extra: object) -> None: ...


# ---------------------------------------------------------------------------
# AuthService: the orchestrator + the reuse-detection state machine
# ---------------------------------------------------------------------------


class AuthService:
    """Orchestrates `UserStore`, `RefreshTokenStore`, `PasswordService`,
    and `TokenService` into `register`/`login`/`refresh`/`logout`/
    `resolve_access`. `now` is injected the same way `TokenService`'s is
    (required, no default) -- `AuthService` uses it for the refresh-
    rotation state machine's expiry comparison (`refresh`, step 5 below),
    independently of whatever `now` the `TokenService` instance it was
    constructed with also uses; a caller normally passes the SAME
    callable to both.

    Three OPTIONAL, keyword-only constructor parameters extend `login`
    (see that method's own docstring for the exact flow) without
    changing its signature or touching `register`/`refresh`/`logout`/
    `resolve_access`/`_mint_and_persist` at all -- every one of those
    stays byte-for-byte what it was before this component grew an email
    seam, single-use tokens, and lockout:

    - `lockout`: an optional `LockoutPolicy` -- when provided, `login`
      consults it to reject a locked account (generically, before
      spending a real Argon2 verify) and to record failures/successes.
      `None` (the default) reproduces the exact prior behavior: no
      lockout is ever consulted or recorded.
    - `require_verification`: when `True`, `login` additionally rejects
      an otherwise-correct login for a user whose `email_verified` is
      still `False` -- generically, the same `InvalidCredentials` as
      every other login failure. `False` (the default) reproduces the
      exact prior behavior: `email_verified` is never consulted.
    - `events`: an optional `AuthEventSink` -- when provided, `login`
      emits `auth.login`/`auth.lockout.triggered` events. `None` (the
      default) reproduces the exact prior behavior: nothing is emitted."""

    def __init__(
        self,
        users: UserStore,
        refresh_tokens: RefreshTokenStore,
        passwords: PasswordService,
        tokens: TokenService,
        now: Callable[[], datetime],
        *,
        lockout: LockoutPolicy | None = None,
        require_verification: bool = False,
        events: AuthEventSink | None = None,
    ) -> None:
        self._users = users
        self._refresh_tokens = refresh_tokens
        self._passwords = passwords
        self._tokens = tokens
        self._now = now
        self._lockout = lockout
        self._require_verification = require_verification
        self._events = events

    @staticmethod
    def _normalize_email(email: str) -> str:
        """Lowercases and strips whitespace. Applied identically at
        registration and login lookup time so `"Alice@Example.com "` and
        `"alice@example.com"` are always treated as the same account --
        without this, a user could accidentally (or an attacker
        deliberately) register a case/whitespace variant of an existing
        email as a DISTINCT account, which most mail providers treat as
        delivering to the same inbox regardless of case."""
        return email.strip().lower()

    async def register(self, email: str, password: str, roles: Sequence[str] = ()) -> UserRecord:
        """Registers a new user. Raises `EmailAlreadyExists` if
        `_normalize_email(email)` is already on file. The password is
        hashed (`PasswordService.hash`) before it ever reaches
        `UserStore.create` -- the plaintext password is never persisted
        or passed to the store."""
        normalized = self._normalize_email(email)
        existing = await self._users.get_by_email(normalized)
        if existing is not None:
            raise EmailAlreadyExists("An account with this email already exists.")
        password_hash = self._passwords.hash(password)
        return await self._users.create(normalized, password_hash, tuple(roles))

    async def authenticate(self, email: str, password: str) -> UserRecord:
        """Verifies credentials and returns the `UserRecord`, minting
        NOTHING. This is the credential-verification half of `login`
        below, split out so the SESSION path can reuse it verbatim.

        **Why this exists.** A session login needs exactly what this
        returns -- an authenticated `UserRecord` to hand to
        `_sessions.SessionService.create` -- and nothing else. Before this
        split, a session login had to call `login()` and discard the
        `TokenPair` it produced, which was not merely wasteful: it
        persisted a `RefreshRecord` for a refresh token no client would
        ever hold, leaving a row in the refresh-token table that could only
        ever expire unused, and starting a token family that logout would
        never revoke. Calling this method instead means the session path
        writes exactly one row (its own session) and touches the JWT
        machinery not at all.

        Every check, every failure mode, every audit event, and the
        Argon2id timing guarantee are IDENTICAL to `login`'s -- because
        `login` is now literally this method plus a mint step. See
        `login`'s own docstring below for the full ordered flow; there is
        no second copy of that logic to drift.

        Callers: `login` below (the JWT path), and a project's own session
        login route (the default browser path) -- e.g. this component's
        FastAPI backend block, whose `POST /auth/login` calls
        `authenticate` then `SessionService.create`."""
        normalized = self._normalize_email(email)
        user = await self._users.get_by_email(normalized)
        if user is None:
            self._passwords.dummy_verify()
            if self._events is not None:
                await self._events.emit("auth.login", actor="anonymous", outcome="failure")
            raise InvalidCredentials("Invalid email or password.")
        if self._lockout is not None and await self._lockout.is_locked(user.id):
            self._passwords.dummy_verify()
            if self._events is not None:
                await self._events.emit("auth.login", actor=user.id, outcome="denied")
            raise InvalidCredentials("Invalid email or password.")
        if not self._passwords.verify(user.password_hash, password):
            if self._lockout is not None:
                just_locked = await self._lockout.record_failure(user.id)
                if just_locked and self._events is not None:
                    await self._events.emit("auth.lockout.triggered", actor=user.id, outcome="denied")
            if self._events is not None:
                await self._events.emit("auth.login", actor=user.id, outcome="failure")
            raise InvalidCredentials("Invalid email or password.")
        if self._require_verification and not user.email_verified:
            if self._events is not None:
                await self._events.emit("auth.login", actor=user.id, outcome="denied")
            raise InvalidCredentials("Invalid email or password.")
        if self._lockout is not None:
            await self._lockout.clear(user.id)
        if self._events is not None:
            await self._events.emit("auth.login", actor=user.id, outcome="success")
        return user

    async def login(self, email: str, password: str) -> TokenPair:
        """Verifies credentials (via `authenticate` above) and, on
        success, starts a brand-new refresh-token FAMILY (a fresh
        `family_id`), mints an access + refresh token pair in it, persists
        the refresh token's `RefreshRecord`, and returns the pair.

        **This is the JWT/bearer path.** A browser client should
        authenticate with a server-side session instead -- see
        `_sessions.py`'s module docstring for why, and `authenticate`
        above for the primitive that path calls in place of this method.
        Behavior here is byte-for-byte what it has always been; only the
        credential-checking half now lives in `authenticate`.

        Every failure path -- unknown email, wrong password, a locked
        account, or (with `require_verification=True`) an unverified
        email -- raises the SAME `InvalidCredentials`, with the SAME
        generic message, so a client (or an attacker probing the
        endpoint) can never distinguish any of these from any other (see
        `InvalidCredentials`'s own docstring on why that matters for
        "unknown email" vs "wrong password"; the same logic extends to
        "locked" and "unverified" here -- revealing that an account
        exists-but-is-locked, or exists-but-is-unverified, is exactly the
        kind of account-existence signal this exception has always
        existed to deny). Every path also spends exactly ONE Argon2id
        operation (a real `verify()` or a `dummy_verify()`), so response
        latency carries no timing signal either. In order:

        1. Normalize the email and look up the user.
        2. **Unknown email** -> `dummy_verify()` (the timing defense --
           unchanged from before this method grew lockout/verification);
           emit `auth.login` `outcome="failure"` (`actor="anonymous"`,
           since there is no user id to attach the event to); raise.
        3. **User found AND `lockout` is set AND the account is
           currently locked** -> `dummy_verify()` (uniform timing -- the
           real password is deliberately never checked for a locked
           account, so a correct guess against a locked account costs
           the identical time and yields the identical outcome as a
           wrong one); emit `auth.login` `outcome="denied"`; raise.
        4. **Wrong password** -> if `lockout` is set, record the failure
           (`LockoutPolicy.record_failure`); if that failure JUST crossed
           the lockout threshold, additionally emit
           `auth.lockout.triggered`; emit `auth.login`
           `outcome="failure"`; raise.
        5. **`require_verification=True` AND the user's `email_verified`
           is `False`** -> emit `auth.login` `outcome="denied"`; raise.
           (The one real Argon2 verify already happened in step 4's
           successful `verify()` call above, so this step spends no
           additional Argon2 time and stays timing-uniform with every
           other path.)
        6. **Success** -> if `lockout` is set, clear its bookkeeping for
           this account (`LockoutPolicy.clear`); emit `auth.login`
           `outcome="success"`; mint and persist a new token pair in a
           brand-new family, exactly as before.

        Steps 1-6 are `authenticate`'s body, not a copy of it -- this
        method is that call plus the mint below."""
        user = await self.authenticate(email, password)
        family_id = uuid.uuid4().hex
        return await self._mint_and_persist(user, family_id)

    async def refresh(self, raw_refresh_token: str) -> TokenPair:
        """THE refresh-token rotation-with-reuse-detection state machine.
        Implements, in this exact order:

        1. `TokenService.decode_refresh` -- structural validation only
           (signature, expiry, issuer, `type == "refresh"`). Invalid on
           any of those grounds -> `InvalidToken`. The decoded claims are
           deliberately NOT otherwise trusted below -- the persisted
           `RefreshRecord` (looked up next, by hash) is the sole source of
           truth for whether this token is still usable; a validly-signed
           JWT whose row says otherwise still loses.
        2. Hash the raw token (`hash_token`) and look it up
           (`RefreshTokenStore.get_by_hash`). **No row** means a validly-
           signed token that this store has never heard of (or no longer
           does) -- forged, or a genuine token from a family/row that's
           been deleted. This does NOT trust the token's own claims to
           revoke anything (there is nothing on file to revoke) --
           just `InvalidToken`.
        3. **`row.revoked`** -> `InvalidToken`. The token's family was
           already killed (by a prior reuse-detection event or a
           `logout()`).
        4. **`row.used_at is not None`** -> **REUSE DETECTED.** This
           exact token was already rotated once before; a second
           presentation of it now can only mean the token was
           compromised (or, less commonly, a client-side race). Calls
           `RefreshTokenStore.revoke_family(row.family_id)` -- revoking
           EVERY row in the family, including whichever token is
           currently the "live" tip of the rotation chain -- then raises
           `TokenReused`. This is deliberately more aggressive than just
           rejecting the reused token: an attacker holding a stolen
           refresh token and a legitimate client both descend from the
           same family, so killing only the reused token would leave
           whichever side currently holds the live rotated token still
           logged in -- which could be the attacker.
        5. **`row.expires_at <= now()`** -> `InvalidToken`. An unused,
           non-revoked row past its own TTL.
        6. **Otherwise, valid:** `RefreshTokenStore.mark_used(row.
           token_hash, now())` (so this exact row can never validate
           again -- see step 4 for why that matters), then mint a NEW
           access token and a NEW refresh token in the SAME family
           (`family_id=row.family_id` -- rotation extends a family, it
           does not start a new one), persist the new refresh token's
           `RefreshRecord`, and return the new `TokenPair`. The just-used
           row is retained with `used_at` set, NOT deleted.

        `TokenReused` and `InvalidToken` both map to `ErrorCode.
        UNAUTHENTICATED` (401) -- see `TokenReused`'s own docstring for
        why a reuse event is deliberately indistinguishable to the client
        from any other refresh failure."""
        # Step 1.
        self._tokens.decode_refresh(raw_refresh_token)

        # Step 2.
        token_hash = hash_token(raw_refresh_token)
        row = await self._refresh_tokens.get_by_hash(token_hash)
        if row is None:
            raise InvalidToken("Refresh token is unknown.")

        # Step 3.
        if row.revoked:
            raise InvalidToken("Refresh token has been revoked.")

        # Step 4.
        if row.used_at is not None:
            await self._refresh_tokens.revoke_family(row.family_id)
            raise TokenReused("Refresh token reuse detected -- the token family has been revoked.")

        # Step 5.
        if row.expires_at <= self._now():
            raise InvalidToken("Refresh token has expired.")

        # Step 6.
        await self._refresh_tokens.mark_used(row.token_hash, self._now())
        user = await self._users.get_by_id(row.user_id)
        if user is None:
            # The row is valid but the user it belongs to is gone (e.g.
            # deleted between mint and refresh) -- there is no principal
            # left to mint a new pair for, so this is an auth failure,
            # not a 500.
            raise InvalidToken("Refresh token no longer maps to an active user.")
        return await self._mint_and_persist(user, row.family_id)

    async def logout(self, raw_refresh_token: str) -> None:
        """Revokes the entire refresh-token family behind
        `raw_refresh_token` -- every token descended from the same login
        stops working, not just this one. Best-effort and idempotent: an
        already-invalid token (malformed, expired, unknown, already
        revoked) does NOT raise -- logging out is not an auth check, and a
        client calling logout with a token that's invalid for any reason
        should still land on "you are logged out", not an error."""
        try:
            self._tokens.decode_refresh(raw_refresh_token)
        except InvalidToken:
            return
        token_hash = hash_token(raw_refresh_token)
        row = await self._refresh_tokens.get_by_hash(token_hash)
        if row is None:
            return
        await self._refresh_tokens.revoke_family(row.family_id)

    async def resolve_access(self, raw_access_token: str) -> AccessClaims:
        """Verifies and decodes an access token, raising `InvalidToken` on
        failure. This is what a framework adapter's
        `get_current_principal` dependency calls on every authenticated
        request's `Authorization: Bearer <token>` header. `async` (even
        though `TokenService.decode_access` itself is synchronous CPU
        work) so this method's signature matches every other
        `AuthService` method and a future revision can add an async check
        (e.g. against a token-blacklist store) without changing the
        interface a framework adapter already depends on."""
        return self._tokens.decode_access(raw_access_token)

    async def issue_session(self, user: UserRecord) -> TokenPair:
        """Mints a brand-new refresh-token FAMILY and an access+refresh pair
        for a principal whose identity was ALREADY established by some means
        other than this class's own password check -- the sole caller in
        this catalog is `_oauth.py`'s `OAuthAccountService.complete_login`,
        invoked once federated (Google/GitHub/Apple) identity has been
        resolved and linked to a `UserRecord`. No credential is verified
        here; that is entirely `OAuthAccountService`'s job before this is
        ever called. Emits `auth.login` `outcome="success"`, `method="oauth"`
        (when `events` is wired) so an audit trail can distinguish a
        federated login from a password one via the SAME event name a
        password login emits. Mirrors `login`'s own tail exactly -- a fresh
        `family_id`, `_mint_and_persist` -- issuing the identical session/
        token shape either path produces, which is the whole point: a
        federated login is indistinguishable, downstream, from a password
        one."""
        if self._events is not None:
            await self._events.emit("auth.login", actor=user.id, outcome="success", method="oauth")
        family_id = uuid.uuid4().hex
        return await self._mint_and_persist(user, family_id)

    async def _mint_and_persist(self, user: UserRecord, family_id: str) -> TokenPair:
        """Shared by `login` (new family) and `refresh` (existing family,
        rotation) -- mints a fresh access + refresh token pair, persists
        the refresh token's `RefreshRecord`, and returns the pair."""
        access = self._tokens.mint_access(user.id, user.roles)
        refresh_token, refresh_claims = self._tokens.mint_refresh(user.id, family_id)
        record = RefreshRecord(
            token_hash=hash_token(refresh_token),
            jti=refresh_claims.jti,
            family_id=refresh_claims.family_id,
            user_id=user.id,
            issued_at=refresh_claims.issued_at,
            expires_at=refresh_claims.expires_at,
            used_at=None,
            revoked=False,
        )
        await self._refresh_tokens.add(record)
        return TokenPair(access=access, refresh=refresh_token)


# ---------------------------------------------------------------------------
# AccountService: email verification + password reset, composed alongside
# AuthService (does NOT subclass it)
# ---------------------------------------------------------------------------


def _normalize_email_for_account(email: str) -> str:
    """The identical normalization `AuthService._normalize_email` applies
    (strip + lowercase), duplicated here as a free function rather than
    calling that staticmethod, so `AccountService` has zero coupling to
    `AuthService`'s internals -- the two are explicitly composed
    ALONGSIDE each other, not one built on the other (see
    `AccountService`'s own docstring), and this module's stated
    contract for `AuthService` is that only `__init__`/`login` change for
    this stage; this helper keeps that true by never touching
    `AuthService` at all for `AccountService`'s own normalization need."""
    return email.strip().lower()


class AccountService:
    """Orchestrates email verification and password reset:
    `UserStore`, `SingleUseTokenService`, `EmailSender`, `PasswordService`,
    and `RefreshTokenStore` composed into `request_email_verification`/
    `verify_email`/`request_password_reset`/`reset_password`. Composed
    ALONGSIDE `AuthService` -- constructed and used independently, NOT a
    subclass and NOT required to use `AuthService` at all -- a project
    wires both against the same underlying stores.

    `now` is injected the same way every other clock in this module is
    (required, no default). `events` is an optional `AuthEventSink`,
    exactly like `AuthService`'s own -- `None` is a valid, no-op default.

    `frontend_base_url` (e.g. `"https://app.example.com"`, no trailing
    slash) is the SPA/site origin `request_email_verification`/
    `request_password_reset` build a link against: `{frontend_base_url}/
    verify-email#token=<raw>` and `{frontend_base_url}/reset-password#
    token=<raw>` respectively. The raw token is placed in the URL
    FRAGMENT (`#token=...`), never a query string, DELIBERATELY: a
    fragment is never sent to the server by the browser (it's a
    client-side-only part of the URL) and is typically excluded from
    `Referer` headers and most access/proxy logs, whereas a query string
    routinely ends up in exactly those places -- so a fragment keeps a
    single-use, highly sensitive token (equivalent to a bearer credential
    until consumed) out of server logs, proxy logs, and any third-party
    `Referer` a page on that route might send a request to, purely via
    where in the URL it's placed. The SPA's own client-side routing reads
    `window.location.hash` and POSTs the token to the backend directly --
    this module has no opinion on how; that is app-layer wiring.

    `verify_ttl`/`reset_ttl` default to 24 hours / 1 hour respectively --
    a verification link tolerates sitting unread in an inbox far longer
    than a password-reset link should remain valid, since an unconsumed
    reset link is a more immediately sensitive thing to have floating
    around (whoever holds it can take over the account, whereas an
    unconsumed verify link only grants "mark this email verified")."""

    def __init__(
        self,
        users: UserStore,
        tokens: SingleUseTokenService,
        email: EmailSender,
        passwords: PasswordService,
        refresh_tokens: RefreshTokenStore,
        now: Callable[[], datetime],
        *,
        events: AuthEventSink | None = None,
        lockout: LockoutPolicy | None = None,
        sessions: "SessionRevoker | None" = None,
        frontend_base_url: str,
        verify_ttl: timedelta = timedelta(hours=24),
        reset_ttl: timedelta = timedelta(hours=1),
    ) -> None:
        self._users = users
        self._tokens = tokens
        self._email = email
        self._passwords = passwords
        self._refresh_tokens = refresh_tokens
        self._now = now
        self._events = events
        # Optional -- shared with the `AuthService` a project wires against
        # the SAME `LockoutStore`, so a successful `reset_password` below can
        # lift a lockout the user accrued from failed guesses BEFORE
        # resetting. Without this, a legitimate user who forgot their
        # password, tripped the lockout guessing, then reset it, would still
        # be blocked at `AuthService.login`'s is-locked check (step 3) for
        # the remaining cooldown despite now holding the correct password --
        # a password reset is meant to RESTORE access, so it clears the lock.
        # `None` (the default) simply skips that clear -- a project not using
        # lockout, or wiring `AccountService` in isolation, is unaffected.
        self._lockout = lockout
        # Optional -- the SERVER-SIDE SESSION half of "a password reset logs
        # you out everywhere". `refresh_tokens.revoke_all_for_user` above
        # kills every JWT refresh family; this kills every session on the
        # (default, browser) session path. A project serving both transports
        # MUST wire both, or a reset leaves the account half-revoked: the
        # attacker's session cookie would keep authenticating even though
        # every refresh token was killed. `None` (the default) skips it
        # entirely, so a project on the JWT path only is unaffected and every
        # prior call site keeps working unchanged. Typed as `SessionRevoker`
        # -- a one-method Protocol declared below -- rather than importing
        # `_sessions.SessionService` directly, which would make this file
        # depend on its own sibling and break `_core.py`'s standalone-import
        # guarantee; `SessionService` satisfies it structurally.
        self._sessions = sessions
        self._frontend_base_url = frontend_base_url.rstrip("/")
        self._verify_ttl = verify_ttl
        self._reset_ttl = reset_ttl

    async def request_email_verification(self, user: UserRecord) -> None:
        """Issues a `"verify"` single-use token (ttl `verify_ttl`, ~24h
        by default) for `user` and emails a verification link containing
        it (fragment-encoded -- see this class's own docstring). Emits
        `auth.email.verify_requested`. Takes a full `UserRecord` (not
        just an email) since the caller -- typically right after
        `AuthService.register` succeeds -- already has one; this method
        never looks a user up itself."""
        raw = await self._tokens.issue(user.id, "verify", self._verify_ttl)
        link = f"{self._frontend_base_url}/verify-email#token={raw}"
        body = (
            "Verify your email address by opening this link:\n\n"
            f"{link}\n\n"
            f"Or enter this code if your client stripped the link: {raw}\n"
        )
        await self._email.send(EmailMessage(to=user.email, subject="Verify your email address", body=body))
        if self._events is not None:
            await self._events.emit("auth.email.verify_requested", actor=user.id, outcome="success")

    async def verify_email(self, raw_token: str) -> None:
        """Consumes `raw_token` as a `"verify"` token (`SingleUseTokenService.
        consume` -- raises `InvalidSingleUseToken` on any of the reasons
        that method's own docstring lists) and, on success, marks the
        token's owning user's email verified (`UserStore.
        mark_email_verified`, timestamped `now()`). Emits
        `auth.email.verified` on success, `auth.email.verify_failed`
        (actor `"unknown"` -- an invalid/expired/reused token carries no
        trustworthy user id) on failure, either way re-raising
        `InvalidSingleUseToken` unchanged so the caller's wire mapping
        (401, generic) is untouched by this method existing."""
        try:
            user_id = await self._tokens.consume(raw_token, "verify")
        except InvalidSingleUseToken:
            if self._events is not None:
                await self._events.emit("auth.email.verify_failed", actor="unknown", outcome="failure")
            raise
        await self._users.mark_email_verified(user_id, self._now())
        if self._events is not None:
            await self._events.emit("auth.email.verified", actor=user_id, outcome="success")

    async def request_password_reset(self, email: str) -> None:
        """Never raises and never reveals whether `email` has an
        account -- the caller (an HTTP route) always returns 202 either
        way, exactly the same user-enumeration defense `AuthService.
        login`'s `InvalidCredentials` applies to login, extended here to
        the "forgot password" flow (which has historically been an even
        more common enumeration vector than login itself, since a
        "no account with that email" message is such a natural thing to
        want to show).

        **Found:** issues a `"reset"` single-use token (ttl `reset_ttl`,
        ~1h by default) and emails a reset link containing it (fragment-
        encoded). The `EmailSender.send()` call is wrapped in a bare
        `try/except Exception`: a delivery failure (the sender raising)
        is caught, an `auth.password.reset_email_failed` audit event is
        emitted (when `events` is wired) instead of `reset_requested`,
        and this method still returns `None` -- it NEVER re-raises. This
        is the core of the anti-enumeration defense, not an incidental
        robustness nicety: if a known email's `send()` failure were
        allowed to propagate (and a framework adapter's route left it
        uncaught, as this component expects -- see `EmailSender`'s own
        docstring), a known email would 500 while an unknown one still
        202s, which IS an account-enumeration oracle (an SMTP outage, a
        bounced/invalid mailbox, a rate-limited relay -- any of these
        would out a registered address). The token is still issued and
        persisted before `send()` is attempted, so a failed delivery
        never silently discards work the caller might expect to retry.

        **Not found:** computes a throwaway token
        (`secrets.token_urlsafe(32)`) and hashes it (`hash_token`) --
        discarding both immediately, never persisting or sending
        anything -- so this path spends comparable CPU/allocation cost
        to the found path's `SingleUseTokenService.issue` (itself a
        `secrets.token_urlsafe` + `hash_token`), keeping the two paths
        close in timing even though this method's overall cost is
        dominated by whatever the caller's own response latency looks
        like (unlike `AuthService.login`, there's no slow Argon2 op
        gating either path here, so an exact single-Argon2-op timing
        match isn't the goal -- the CPU-comparable throwaway op is a
        best-effort match, not a formal guarantee). The found path's own
        `send()` no longer costs a real SMTP round-trip either, by
        contract (see `EmailSender`'s docstring: a compliant sender
        delivers out-of-band and returns promptly) -- so this was never
        a race against real network latency to begin with, on either
        path.

        Either way, emits `auth.password.reset_requested` on a
        successful (or not-attempted, for the not-found path) send, with
        `actor=user.id` (found) or `actor="user:unknown"` (not found) --
        **never the submitted email itself**, in either branch, so the
        audit trail cannot be grepped for "which email addresses did
        someone try resetting" even by someone with access to it."""
        normalized = _normalize_email_for_account(email)
        user = await self._users.get_by_email(normalized)
        if user is None:
            throwaway_raw = secrets.token_urlsafe(32)
            hash_token(throwaway_raw)  # discarded -- comparable-cost, not persisted/sent
            if self._events is not None:
                await self._events.emit("auth.password.reset_requested", actor="user:unknown", outcome="success")
            return
        raw = await self._tokens.issue(user.id, "reset", self._reset_ttl)
        link = f"{self._frontend_base_url}/reset-password#token={raw}"
        body = (
            "Reset your password by opening this link:\n\n"
            f"{link}\n\n"
            f"Or enter this code if your client stripped the link: {raw}\n"
        )
        # M1 fix: a delivery failure here must NEVER change this method's
        # outcome (it always returns None either way) or propagate to the
        # caller -- see this method's own docstring and `EmailSender`'s for
        # why: letting `send()` raise past this point is the enumeration
        # oracle (known email -> 500, unknown email -> 202) this whole
        # method exists to prevent.
        try:
            await self._email.send(EmailMessage(to=user.email, subject="Reset your password", body=body))
        except Exception:
            if self._events is not None:
                await self._events.emit("auth.password.reset_email_failed", actor=user.id, outcome="failure")
        else:
            if self._events is not None:
                await self._events.emit("auth.password.reset_requested", actor=user.id, outcome="success")

    async def reset_password(self, raw_token: str, new_password: str) -> None:
        """Consumes `raw_token` as a `"reset"` token, hashes
        `new_password` (`PasswordService.hash` -- the plaintext is never
        persisted or passed to `UserStore`, matching `AuthService.
        register`'s own posture), overwrites the user's stored hash
        (`UserStore.set_password_hash`), then revokes EVERY refresh-token
        family the user has (`RefreshTokenStore.revoke_all_for_user`) --
        killing every existing logged-in session everywhere, since
        whatever was true about the account's security under the OLD
        password can no longer be assumed once it's been reset. **If a
        `sessions` revoker was wired, ALSO revokes every server-side
        session** (`SessionRevoker.revoke_all_for_user`) -- the two
        transports are revoked together, since killing only one leaves the
        account half-revoked (see `__init__`'s own `sessions` note). If a
        `lockout` policy was wired, also clears any failed-login lockout on
        the account (see `__init__`'s `lockout` note) so the reset restores
        access immediately.

        **Also marks the account's email verified** (`UserStore.
        mark_email_verified(user_id, self._now())`), regardless of whether
        it already was. Rationale: successfully consuming a `"reset"`
        single-use token proves control of the account's email -- the
        token was only ever delivered to that inbox (see `request_
        password_reset`'s own docstring) -- which is exactly the same
        proof-of-inbox-control `verify_email` establishes via a `"verify"`
        token; completing a reset therefore satisfies email verification
        too. This is deliberately the RECOVERY PATH for an account whose
        original verification email never arrived: `register`'s
        post-registration `request_email_verification` call can fail to
        deliver (an SMTP outage, a bounced address at signup time) without
        the caller being told -- see this component's FastAPI backend's
        `register` handler, which never lets that failure turn a 201 into
        a 500 -- so without this, such an account would be permanently
        stuck behind `AuthService.login`'s `require_verification` gate
        with no way back in. Ordered AFTER session revocation and the
        lockout clear above, so a reset invalidates old credentials/
        sessions, unblocks the new login, AND unblocks it from the
        verification gate too, all in one call.

        Emits `auth.password.reset_completed` on success (emitted LAST,
        after every other side effect above has already happened),
        `auth.password.reset_failed` (actor `"unknown"`) on a bad/expired/
        reused token, re-raising `InvalidSingleUseToken` unchanged either
        way."""
        try:
            user_id = await self._tokens.consume(raw_token, "reset")
        except InvalidSingleUseToken:
            if self._events is not None:
                await self._events.emit("auth.password.reset_failed", actor="unknown", outcome="failure")
            raise
        new_hash = self._passwords.hash(new_password)
        await self._users.set_password_hash(user_id, new_hash)
        await self._refresh_tokens.revoke_all_for_user(user_id)
        # The server-side-session half of the same "log out everywhere"
        # guarantee the refresh revocation above provides for the JWT path.
        # A project serving both transports must wire `sessions=` or a reset
        # leaves the account half-revoked -- see `__init__`'s own note. No-op
        # when it isn't wired.
        if self._sessions is not None:
            await self._sessions.revoke_all_for_user(user_id)
        # Lift any failed-login lockout the user accrued before resetting --
        # a completed reset (proving control of the account's email) should
        # restore access immediately, not leave the user blocked at login's
        # is-locked check for the remaining cooldown. No-op when lockout
        # isn't wired. Ordered AFTER the session revocation above so a reset
        # both invalidates old credentials/sessions AND unblocks the new one.
        if self._lockout is not None:
            await self._lockout.clear(user_id)
        # A completed reset proves control of the account's email -- see
        # this method's own docstring on why that also satisfies email
        # verification, and why that matters as the recovery path for a
        # registration whose verification email failed to send (FIX 2/3).
        await self._users.mark_email_verified(user_id, self._now())
        if self._events is not None:
            await self._events.emit("auth.password.reset_completed", actor=user_id, outcome="success")
