"""Shared FastAPI dependencies for both authentication paths.

**`get_current_principal` resolves a SERVER-SIDE SESSION** — the
`session_id` cookie — and is the DEFAULT every browser-facing protected
route depends on. It is built from `get_session_service` (a per-request
`SessionService` over `SqlAlchemySessionStore` + `SqlAlchemyUserStore`).
`get_bearer_principal` is the JWT/bearer equivalent, kept fully wired for
native/mobile clients and service-to-service callers; `get_auth_service`
is still what BOTH paths verify credentials through at login, since
`AuthService` owns Argon2id verification, the timing defense, lockout, and
the email-verification gate regardless of what the successful login then
issues.

`get_auth_service` is the per-request `AuthService` provider — binds this
request's DB session (`get_db`) into fresh `SqlAlchemyUserStore`/
`SqlAlchemyRefreshTokenStore` instances, plus the process-wide
`PasswordService` singleton and a `Settings`-derived `TokenService`, into
one `AuthService`.

Stage 5c (#45) adds `get_account_service` — the per-request `AccountService`
provider `app/api/routers/auth.py`'s new verify-email/request-password-reset/
reset-password routes (and `register`'s post-registration verification-email
side effect) depend on — and wires `get_auth_service` up to the SAME
lockout/verification/audit seams `AccountService` already uses (see
`build_account_service`'s own docstring in `stores.py`): `login` now
consults a real `LockoutPolicy`, gates on `email_verified` when
`Settings.auth_require_email_verification` is `True` (the default), and
emits `auth.*` audit events."""

from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security.auth import (
    AccountService,
    AuthService,
    EmailSender,
    SessionService,
    build_get_current_principal,
    build_get_current_principal_either,
    build_get_current_session_principal,
    require_roles,
)
from app.core.security.auth.stores import (
    AuditAuthEventSink,
    SqlAlchemyRefreshTokenStore,
    SqlAlchemyUserStore,
    build_account_service,
    build_lockout_policy,
    build_session_service,
    get_password_service,
    get_token_service,
    utc_now,
)
from app.core.security.auth.stores import get_email_sender as _resolve_email_sender


async def get_auth_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> AuthService:
    """Per-request `AuthService`, bound to THIS request's `AsyncSession` —
    a fresh pair of store instances every call (they're thin wrappers
    holding only a session reference, so this is cheap), the process-wide
    `PasswordService` singleton (`get_password_service()` — see its own
    docstring on why that one IS cached), and a `TokenService` built fresh
    from `request.app.state.settings` (`get_token_service()` — raises
    `AuthNotConfiguredError`, fail-closed, if `jwt_signing_key` is unset;
    see that function's own docstring). `now=utc_now` is the SAME callable
    `get_token_service()` passes to the `TokenService` it builds — see
    that function's own module, `utc_now`'s docstring.

    Reads `request.app.state.settings` — the EXACT `Settings` instance
    `app/main.py`'s `create_app()` was actually constructed with (see that
    function's own comment on `app.state.settings`) — deliberately NOT
    `Depends(get_settings)`, the separate process-wide `lru_cache`d
    singleton every OTHER piece of this app's security composition
    (rate limiting, CORS, security headers) reads directly at
    APP-CONSTRUCTION time, not per-request. A route-level dependency has
    no other way to see a bespoke `Settings(...)` a caller passed to
    `create_app(settings=...)` instead of the cached singleton — see
    `tests/conftest.py`'s `make_client` fixture, which relies on exactly
    that seam to configure e.g. `jwt_signing_key` per test without
    mutating process env vars (which would leak across tests).

    Stage 5c (#45): additionally passes `lockout=build_lockout_policy(
    settings, db)` — the SAME `session` (`db`) `get_account_service` below
    builds its own `AccountService`'s `lockout=` from, when both are used
    within the same request/test — so a successful `AccountService.
    reset_password` can lift a lockout this `AuthService.login` recorded
    against the same account (see `build_lockout_policy`'s own docstring);
    `require_verification=settings.auth_require_email_verification`
    (secure default `True` — an unverified account cannot log in); and
    `events=AuditAuthEventSink()` so `login` emits its `auth.login`/
    `auth.lockout.triggered` audit events."""
    settings = request.app.state.settings
    return AuthService(
        users=SqlAlchemyUserStore(db),
        refresh_tokens=SqlAlchemyRefreshTokenStore(db),
        passwords=get_password_service(),
        tokens=get_token_service(settings),
        now=utc_now,
        lockout=build_lockout_policy(settings, db),
        require_verification=settings.auth_require_email_verification,
        events=AuditAuthEventSink(),
    )


def get_email_sender(request: Request) -> EmailSender:
    """FastAPI-dependency-shaped wrapper around `stores.get_email_sender(
    settings)` (imported here as `_resolve_email_sender` to avoid shadowing
    this function's own name) — a deliberately THIN seam whose only job is
    to be a distinct, overridable dependency callable: `get_account_service`
    below depends on THIS function via `Depends(get_email_sender)` rather
    than calling `stores.get_email_sender` directly, so a test can do
    `app.dependency_overrides[get_email_sender] = lambda: capturing_sender`
    and have `AccountService.request_email_verification`/
    `request_password_reset` hand their `EmailMessage` (raw verify/reset
    token included — see `_core.ConsoleEmailSender`'s own docstring on why
    that's the ONE place a raw token is deliberately surfaced) to that
    capturing sender instead of the real `ConsoleEmailSender`/
    `SmtpEmailSender` — a clean, deterministic way to read an issued token
    in a test without parsing a log string. Reads `request.app.state.
    settings`, matching `get_auth_service`/`get_account_service`'s own
    rationale for doing so rather than `Depends(get_settings)`."""
    settings = request.app.state.settings
    return _resolve_email_sender(settings)


async def get_account_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
    email_sender: EmailSender = Depends(get_email_sender),
) -> AccountService:
    """Per-request `AccountService` provider — the Stage 5c (#45) analogue
    of `get_auth_service` above, same session-per-request shape: delegates
    the rest of the composition to `stores.py:build_account_service(
    settings, db, email=email_sender)` (the SAME `db` session this request's
    `get_auth_service` — if also depended on within the same request —
    builds its own stores against, so a shared-session `LockoutPolicy` is
    possible; see that function's own docstring), but takes `email` as an
    explicit `Depends(get_email_sender)` argument rather than letting
    `build_account_service` re-resolve its own — see `get_email_sender`'s
    own docstring for why that's the seam a test overrides. Reads
    `request.app.state.settings`, matching `get_auth_service`'s own
    rationale for doing so rather than `Depends(get_settings)`."""
    settings = request.app.state.settings
    return build_account_service(settings, db, email=email_sender)


async def get_session_service(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SessionService:
    """Per-request `SessionService`, bound to THIS request's
    `AsyncSession` — the provider behind this app's DEFAULT authentication
    path. Same session-per-request composition shape as
    `get_auth_service` above; the composition itself lives in
    `stores.py:build_session_service` (see its own docstring), and
    `request.app.state.settings` is read for the same reason
    `get_auth_service` documents rather than `Depends(get_settings)`.

    Unlike `get_auth_service`, this can never raise
    `AuthNotConfiguredError`: a session id is opaque and unguessable
    rather than signed, so there is no signing key to be missing and
    nothing to fail closed on."""
    settings = request.app.state.settings
    return build_session_service(settings, db)


# THE DEFAULT PRINCIPAL, and what every protected route in this app
# depends on. Authenticates by whichever credential the request ACTUALLY
# carries -- the `session_id` cookie first (the default for browsers),
# falling back to an `Authorization: Bearer` token (native/mobile,
# service-to-service). One dependency serves both transports because
# `SessionPrincipal` and `AccessClaims` are duck-type-compatible on
# `sub`/`roles`, so this app needs exactly one set of route handlers and
# one authorization rule rather than a parallel API per client type.
#
# The session-first ordering is load-bearing, not cosmetic -- see the
# vendored `build_get_current_principal_either`'s own docstring: were
# bearer checked first, a request carrying both a live session cookie and
# an attacker-supplied `Authorization` header would authenticate as the
# attacker's token. The decision is made from what is present on the
# request, never from anything the client declares about itself, matching
# `references/wiring/auth-end-to-end.md`'s per-request transport rule.
get_current_principal = build_get_current_principal_either(get_session_service, get_auth_service)

# The SESSION-ONLY principal. Same resolution as the default above minus
# the bearer fallback -- for a route that must refuse a bearer token
# outright (an admin console served exclusively to browsers, say, where
# accepting a long-lived token would undercut the immediate-revocation
# property the session path exists for). Not used by any route in this
# block today; provided so choosing it is a one-line change rather than a
# rewrite.
get_session_principal = build_get_current_session_principal(get_session_service)

# The BEARER-ONLY principal, for native and mobile clients (which have a
# real OS-backed secret store and no ambient-cookie exposure) and for
# service-to-service callers (which have no user session to look up at
# all). Kept fully wired and fully supported -- preferring sessions is a
# default, not a deprecation. `app/api/routers/auth.py`'s
# `POST /auth/login` serves this path on an explicit `X-Auth-Mode: bearer`
# request.
get_bearer_principal = build_get_current_principal(get_auth_service)

# The RBAC admin example's gate -- `require_roles(...)` is the vendored
# component's generic role-AND-set dependency factory
# (`app/core/security/auth/fastapi.py`), bound here once against the
# DEFAULT (session) principal. It works identically against either
# principal because `SessionPrincipal` is duck-type-compatible with
# `AccessClaims` on `sub`/`roles` (see that dataclass's own docstring), so
# one role gate covers both transports and there is no second, parallel
# authorization rule to keep in sync. `app/api/routers/admin.py`'s
# `GET /admin/ping` is the one route that depends on this today; any future
# admin-only route reuses this SAME dependency rather than calling
# `require_roles(...)` again at each call site.
require_admin = require_roles(get_current_principal, "admin")

# The bearer-path equivalent of `require_admin`, for a role-gated route
# served to native/mobile clients. Same factory, same AND-semantics, same
# `InsufficientRole` -> 403 -- only the principal it resolves differs.
require_admin_bearer = require_roles(get_bearer_principal, "admin")
