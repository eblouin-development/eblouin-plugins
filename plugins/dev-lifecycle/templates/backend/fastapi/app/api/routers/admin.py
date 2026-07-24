"""RBAC admin example (Stage 5d, #46) -- `GET /admin/ping`, gated by
`require_admin` (`app/api/deps.py`: `require_roles(get_current_principal,
"admin")`, from the vendored auth component's `fastapi.py`). Demonstrates
the role-gating machinery end to end, with no new auth logic of its own:

- **200** for an authenticated principal whose `SessionPrincipal.roles`
  includes `"admin"`.
- **403** `permission_denied` for an authenticated principal WITHOUT the
  `"admin"` role -- `require_roles`'s dependency raises `InsufficientRole`
  (`app/core/security/auth/fastapi.py`), mapped by `app/main.py`'s
  `_auth_error_handler` via the vendored component's `AUTH_ERROR_HTTP`
  table.
- **401** `unauthenticated` for a missing/malformed/expired bearer token --
  `get_current_principal` (which `require_admin` itself depends on) raises
  `InvalidToken` before this handler's body, or even `require_admin`'s own
  role check, ever runs.

Every one of those three outcomes is entirely the existing `get_current_
principal`/`require_roles`/`AuthError`-handler machinery already proven by
`GET /auth/me` and `tests/test_auth.py` -- this router adds no new
authentication or authorization code, only a route to exercise it as an
admin-only example.

Reuses `HealthStatus` (`app/schemas/health.py`, `{"status": str}`) as the
response shape rather than inventing a near-identical model -- this
endpoint's own success body is exactly `{"status": "ok"}`, the shape
`HealthStatus` already is (see that schema's own docstring).

--- Stage 13b: admin user management ---------------------------------

Every route below is gated the SAME way `admin_ping` is (`Depends(
require_admin)`, bound as a parameter here rather than in `dependencies=`
so the resolved `SessionPrincipal` is available in the handler body too --
`principal.sub` is the acting admin's own id, used both as the audit actor and
for the self-protection guard, below), PLUS a second, TIGHTER per-route
rate limit (`require_admin_rate_limit`, this module's own `InMemoryBucketStore`
-- see that dependency's own comment) layered on top of the general per-IP
`RateLimitMiddleware` (`app/main.py`) every request already goes through --
the admin surface is the highest-value target in this app, so it gets its
own, stricter ceiling.

Every mutation (`suspend`/`ban`/`reinstate`/`roles`/`force-verify`/`delete`)
is audited via the vendored audit-logging component's `audit_event(...)`
(`app/core/security/audit_logging/audit.py`, called directly here -- it's a
plain synchronous function, no `AuthEventSink`/`await` indirection needed
the way `app/core/security/auth/stores.py`'s `AuditAuthEventSink` wraps it
for the auth core) -- `actor=principal.sub`, `resource=f"user:{user.id}"`
(a `type:id` identifier, never the user's email), `outcome="success"`, and
`changed_fields=[...]` naming which column(s) changed -- NEVER the raw
before/after values themselves (a status/role-list change is not sensitive
the way a password/token is, but this still follows `audit_event`'s own
"identifiers, not payloads" posture).

**Self-protection.** The acting admin can never ban/suspend/delete their
OWN account, nor drop their OWN `"admin"` role via `PUT .../roles` --
`_ensure_not_self`/`set_admin_user_roles`'s own inline check raise
`ConflictError` (409 `conflict`) for exactly that, preventing an admin from
locking themselves out. `force-verify`/`reinstate` have no such guard --
neither is capable of a self-lockout (verifying or reinstating one's own
account is harmless), matching the endpoint contract table this stage's
plan specifies.

**State machine, not blind idempotency.** `suspend`/`ban`/`reinstate` each
validate the CURRENT `status` before transitioning -- see each handler's
own docstring for the exact from-state set -- raising `ConflictError` (409)
for a transition that doesn't apply from the user's current state (e.g.
`suspend` on an already-`banned` user, `reinstate` on an already-`active`
one). This is what gives `409` real, checkable meaning beyond
self-protection for these three routes, matching this stage's own endpoint
contract table (`suspend`/`ban`/`reinstate` all document a `409`).

**Admin queries are UNFILTERED by `status`.** Every handler below queries
`User` directly via `AsyncRepository` (soft-delete-scoped by that
repository's own default, same as every other model), never through
`SqlAlchemyUserStore` (`app/core/security/auth/stores.py`) -- that store's
`get_by_email`/`get_by_id` are now ALSO filtered to `status == "active"`
(Stage 13b's ban-enforcement fix, that store's own docstring) specifically
so the LOGIN/REFRESH/`/auth/me` path can't authenticate a suspended/banned
user; the admin surface must see and act on exactly those accounts, so it
never goes through that filtered seam."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import require_admin
from app.core.config import get_settings
from app.core.db import AsyncRepository, Page, PageParams, get_db
from app.core.errors import ConflictError, ErrorDetail, ErrorEnvelope, NotFoundError, ValidationFailedError
from app.core.security.audit_logging.audit import audit_event
from app.core.security.auth import SessionPrincipal
from app.core.security.auth.stores import SqlAlchemyRefreshTokenStore, utc_now
from app.core.security.rate_limiting import InMemoryBucketStore, make_rate_limit_dependency
from app.models.user import User
from app.schemas.admin import AdminRolesIn, AdminUserOut, UserStatus
from app.schemas.health import HealthStatus
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/admin", tags=["admin"])

# Mirrors app/api/routers/auth.py's own `_UNAUTHENTICATED_RESPONSE`/
# `_CONFLICT_RESPONSE` pattern (documenting the ErrorEnvelope-shaped error
# responses this route actually sends at runtime, via app/main.py's
# `_auth_error_handler`) -- both entries this route can actually produce,
# gathered in one place rather than inlined in the decorator below.
_AUTH_RESPONSES = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid bearer token."},
    403: {"model": ErrorEnvelope, "description": "Authenticated, but the caller lacks the 'admin' role."},
}

# --- Stage 13b: admin user management -----------------------------------

_NOT_FOUND_RESPONSE = {404: {"model": ErrorEnvelope, "description": "User not found."}}
_CONFLICT_RESPONSE = {
    409: {
        "model": ErrorEnvelope,
        "description": "The action conflicts with the user's current state, or is a self-protection guard.",
    }
}
_ROLES_VALIDATION_RESPONSE = {422: {"model": ErrorEnvelope, "description": "One or more requested roles is unknown."}}

# The allowed-role set `PUT /admin/users/{user_id}/roles` validates against
# -- `{"admin"}` today, the only role this app's own auth surface ever
# grants (`app/core/security/auth/stores.py`'s `seed_admin`) or checks
# (`app/api/deps.py`'s `require_admin`). A kit-defined role a later recipe
# adds (e.g. "billing") extends this set, never an arbitrary caller-supplied
# string -- this is what turns an unknown role into a clean 422
# `validation_failed` instead of a silent, unvalidated column write (no
# mass-assignment).
_ALLOWED_ROLES: frozenset[str] = frozenset({"admin"})

# Tighter, PER-ROUTE rate limit layered on top of the general per-IP
# `RateLimitMiddleware` (app/main.py) every request already goes through --
# the admin surface is the highest-value target in this app, so every
# handler below additionally depends on `require_admin_rate_limit`. A
# dedicated `InMemoryBucketStore` (not the whole-app one `app/main.py`
# constructs per `create_app()` call), module-level like every other
# process-wide singleton in this catalog (`app/core/security/rate_limiting/
# django.py`'s own `_default_store` is the identical pattern on the Django
# track) -- this module is imported once per process, so this store is
# shared by every admin request that process ever handles, same "one bucket
# per client IP, not one per request" property the general middleware's own
# store already has. 30 requests/minute is a starting-point default (not
# load-tested), deliberately tighter than `Settings.rate_limit_capacity`'s
# own 60/minute default -- tune per project.
#
# Per-process, like every other `InMemoryBucketStore` in this catalog -- see
# that class's own "Known limitations" docstring (rate_limiting/_core.py)
# for the full multi-worker caveat: under N gunicorn/uvicorn workers this
# bucket is duplicated per worker, so the effective ceiling is roughly
# N x 30/minute rather than a hard shared one, and a client can land on a
# fresh, unexhausted bucket per worker. A Redis-backed `BucketStore` (Stage
# 11) is the upgrade for a true shared ceiling under a multi-worker deploy.
_ADMIN_RATE_LIMIT_STORE = InMemoryBucketStore(max_keys=10_000)
_ADMIN_RATE_LIMIT_CAPACITY = 30
_ADMIN_RATE_LIMIT_REFILL_PER_SECOND = 30 / 60

# `trusted_hops` mirrors the general `RateLimitMiddleware` wiring in
# app/main.py's `create_app()` (`trusted_hops=resolved_settings.
# rate_limit_trusted_hops`) -- reading the SAME `Settings.
# rate_limit_trusted_hops` field (see that field's own docstring,
# app/core/config.py) so this tighter admin bucket derives the client key
# the identical way the rest of this app does, and matches the Django
# admin limiter's own posture (`core/security/admin_rate_limit.py`'s
# `enforce_admin_rate_limit`, which reads `settings.RATE_LIMIT_TRUSTED_
# HOPS`). Without this, this dependency defaulted to `trusted_hops=0` and
# always keyed on `request.client.host` regardless of environment -- behind
# a trusted reverse proxy that's the proxy's own IP, not the caller's, so
# every admin shared one bucket.
require_admin_rate_limit = make_rate_limit_dependency(
    _ADMIN_RATE_LIMIT_STORE,
    capacity=_ADMIN_RATE_LIMIT_CAPACITY,
    refill_per_second=_ADMIN_RATE_LIMIT_REFILL_PER_SECOND,
    trusted_hops=get_settings().rate_limit_trusted_hops,
)


def reset_admin_rate_limit_store_for_tests() -> None:
    """Test-only hook: clears `_ADMIN_RATE_LIMIT_STORE`'s bucket state in
    place. NOT a public API of this router -- `require_admin_rate_limit`
    (above) closed over `_ADMIN_RATE_LIMIT_STORE` by reference at IMPORT
    time (`make_rate_limit_dependency`'s own `store` argument), so
    reassigning the module-level `_ADMIN_RATE_LIMIT_STORE` name here would
    NOT be seen by that already-built dependency closure -- mutating the
    existing store's internal state in place is what actually resets it.
    Mirrors `core/security/rate_limiting/django.py`'s own module-level
    `_default_store` reset, the Django-side test-isolation fixture
    (`tests/conftest.py`'s `_reset_rate_limit_store`) already relies on."""
    _ADMIN_RATE_LIMIT_STORE._buckets.clear()  # noqa: SLF001


def _to_admin_user_out(user: User) -> AdminUserOut:
    return AdminUserOut.model_validate(user)


def _ensure_not_self(principal: SessionPrincipal, user_id: uuid.UUID, *, action: str) -> None:
    """Self-protection guard (Stage 13b's plan): the acting admin can never
    `action` (ban/suspend/delete) their OWN account -- comparing
    `principal.sub` (the session principal's `sub`, a bare user-id string)
    against `str(user_id)` directly, no extra lookup needed."""
    if str(user_id) == principal.sub:
        raise ConflictError(f"An admin cannot {action} their own account.")


async def ban_user(db: AsyncSession, user: User) -> User:
    """THE ban action -- extracted so `ban_admin_user` (below) and Stage
    13c's moderation `resolve` action (`app/api/routers/moderation.py`'s
    `ban_author`, which imports and calls this function directly) share
    ONE implementation rather than each hand-rolling the state-machine
    check + status write + session revocation. Valid from `status in
    {"active", "suspended"}` -- an already-`banned` user raises
    `ConflictError` (409, idempotent re-ban is rejected rather than
    silently no-op'd). Same refresh-token revocation `suspend_admin_user`
    documents for its own action.

    Deliberately does NOT perform the self-protection check
    (`_ensure_not_self`) or emit the `admin.user.ban` audit event itself --
    both callers have their own `principal`/audit-action-name context this
    function has no business assuming (moderation's own resolve emits
    `admin.flag.resolve`, not `admin.user.ban`, as its audit action -- see
    that router's own docstring), so those two steps stay the CALLER's
    responsibility, exactly as they already were before this extraction."""
    repo = AsyncRepository(db, User)
    if user.status not in (UserStatus.ACTIVE.value, UserStatus.SUSPENDED.value):
        raise ConflictError(f"Cannot ban a user with status '{user.status}'.")
    user = await repo.update(user, status=UserStatus.BANNED.value)
    await SqlAlchemyRefreshTokenStore(db).revoke_all_for_user(str(user.id))
    return user


@router.get(
    "/ping",
    response_model=HealthStatus,
    summary="Admin ping",
    # Explicit, stable operationId (Stage 5d task contract) rather than
    # FastAPI's own auto-derived one -- pinning it here means a later
    # rename of this function/module never silently renames the generated
    # client's method for it.
    operation_id="admin_ping_admin_ping_get",
    dependencies=[Depends(require_admin)],
    responses=_AUTH_RESPONSES,
)
async def admin_ping() -> HealthStatus:
    """`require_admin` (declared in `dependencies=` above, not as a bound
    parameter -- this handler needs nothing from the resolved principal,
    only the gate itself) already verified the bearer token AND the
    `"admin"` role before this body ever runs. `HTTPBearer` (via
    `require_admin` -> `get_current_principal` -> the vendored component's
    `bearer_scheme`) is picked up in this operation's OpenAPI `security`
    the same way it already is for `GET /auth/me` -- FastAPI resolves
    security schemes from the full dependency tree, not just parameters
    bound directly on the route function, so declaring the gate via
    `dependencies=[Depends(require_admin)]` documents it in the schema
    exactly as if it were a direct parameter."""
    return HealthStatus(status="ok")


# ---------------------------------------------------------------------------
# Stage 13b: admin user management
# ---------------------------------------------------------------------------


@router.get(
    "/users",
    response_model=Page[AdminUserOut],
    summary="List users (admin)",
    operation_id="list_admin_users_admin_users_get",
    responses=_AUTH_RESPONSES,
)
async def list_admin_users(
    params: PageParams = Depends(),
    q: str | None = Query(default=None, min_length=1, description="Case-insensitive substring match against email."),
    status_filter: UserStatus | None = Query(default=None, alias="status"),
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> Page[AdminUserOut]:
    """Paginated user listing -- reuses the `pagination/` component's
    `PageParams`/`Page[T]` (the SAME shape `GET /items` uses,
    `app/api/routers/items.py`'s own `list_items`), `?q=` filters `email`
    case-insensitively via `.ilike()` (portable across sqlite/Postgres --
    see `sqlalchemy.md`/this app's own hermetic-test posture), `?status=`
    filters to one exact `UserStatus`. Unfiltered by soft-delete beyond
    `AsyncRepository`'s own default (excludes soft-deleted rows) -- see this
    module's docstring, "Admin queries are UNFILTERED by status": every
    status value is visible here, unlike the login/refresh path."""
    repo = AsyncRepository(db, User)
    filters = []
    if q:
        filters.append(User.email.ilike(f"%{q}%"))
    if status_filter is not None:
        filters.append(User.status == status_filter.value)
    result = await repo.list(params=params, filters=filters)
    mapped = [_to_admin_user_out(u) for u in result.items]
    return Page.create(mapped, total=result.total, params=params)


@router.get(
    "/users/{user_id}",
    response_model=AdminUserOut,
    summary="Get user (admin)",
    operation_id="get_admin_user_admin_users__user_id__get",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE},
)
async def get_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    return _to_admin_user_out(user)


@router.post(
    "/users/{user_id}/suspend",
    response_model=AdminUserOut,
    summary="Suspend user (admin)",
    operation_id="suspend_admin_user_admin_users__user_id__suspend_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def suspend_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    """Valid only from `status == "active"` -- an already-`suspended` or
    already-`banned` user raises `ConflictError` (409); a banned user must
    be `reinstate`d before it can be suspended (suspend is not a "downgrade
    from banned" operation). Self-protection: the acting admin cannot
    suspend themselves (`_ensure_not_self`, 409). On success, also revokes
    every refresh token this user holds (`RefreshTokenStore.
    revoke_all_for_user` -- see `app/core/security/auth/stores.py`'s
    `SqlAlchemyUserStore` docstring for the accepted, bounded
    access-token-TTL race this does NOT close) so existing sessions die
    immediately rather than merely being unable to refresh once their
    access token expires."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    _ensure_not_self(principal, user.id, action="suspend")
    if user.status != UserStatus.ACTIVE.value:
        raise ConflictError(f"Cannot suspend a user with status '{user.status}'.")
    user = await repo.update(user, status=UserStatus.SUSPENDED.value)
    await SqlAlchemyRefreshTokenStore(db).revoke_all_for_user(str(user.id))
    audit_event(
        "admin.user.suspend",
        actor=principal.sub,
        resource=f"user:{user.id}",
        outcome="success",
        changed_fields=["status"],
    )
    return _to_admin_user_out(user)


@router.post(
    "/users/{user_id}/ban",
    response_model=AdminUserOut,
    summary="Ban user (admin)",
    operation_id="ban_admin_user_admin_users__user_id__ban_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def ban_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    """Valid from `status in {"active", "suspended"}` -- an already-`banned`
    user raises `ConflictError` (409, idempotent re-ban is rejected rather
    than silently no-op'd, matching `suspend`'s own strict-transition
    posture). Self-protection: the acting admin cannot ban themselves (409).
    Same refresh-token revocation as `suspend` above -- see that handler's
    own docstring."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    _ensure_not_self(principal, user.id, action="ban")
    user = await ban_user(db, user)
    audit_event(
        "admin.user.ban",
        actor=principal.sub,
        resource=f"user:{user.id}",
        outcome="success",
        changed_fields=["status"],
    )
    return _to_admin_user_out(user)


@router.post(
    "/users/{user_id}/reinstate",
    response_model=AdminUserOut,
    summary="Reinstate user (admin)",
    operation_id="reinstate_admin_user_admin_users__user_id__reinstate_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def reinstate_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    """Valid from `status in {"suspended", "banned"}` -- an already-`active`
    user raises `ConflictError` (409, "nothing to reinstate"). No
    self-protection guard: reinstating one's own account is never harmful
    (it can only WIDEN the caller's own access back to what it already was
    before a suspend/ban, never grant anything new), matching this stage's
    own endpoint contract. No refresh-token action either -- reinstating
    doesn't need to kill sessions, only suspend/ban do."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    if user.status not in (UserStatus.SUSPENDED.value, UserStatus.BANNED.value):
        raise ConflictError(f"Cannot reinstate a user with status '{user.status}'.")
    user = await repo.update(user, status=UserStatus.ACTIVE.value)
    audit_event(
        "admin.user.reinstate",
        actor=principal.sub,
        resource=f"user:{user.id}",
        outcome="success",
        changed_fields=["status"],
    )
    return _to_admin_user_out(user)


@router.put(
    "/users/{user_id}/roles",
    response_model=AdminUserOut,
    summary="Set user roles (admin)",
    operation_id="set_admin_user_roles_admin_users__user_id__roles_put",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE, **_ROLES_VALIDATION_RESPONSE},
)
async def set_admin_user_roles(
    user_id: uuid.UUID,
    payload: AdminRolesIn,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    """Full-replace, not a delta -- `payload.roles` becomes the user's
    ENTIRE role list. Every requested role is validated against
    `_ALLOWED_ROLES` (module-level, above) -- an unknown role raises
    `ValidationFailedError` (422 `validation_failed`, with one `ErrorDetail`
    per unknown role) BEFORE any write, so this is never a mass-assignment
    of an arbitrary caller-supplied column. Self-protection: if `user_id` is
    the ACTING admin's own account and the requested role list would drop
    `"admin"`, raises `ConflictError` (409) instead of writing -- this is
    what stops an admin from locking themselves out via this endpoint."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    unknown = sorted(set(payload.roles) - _ALLOWED_ROLES)
    if unknown:
        raise ValidationFailedError(
            "One or more requested roles is unknown.",
            details=[ErrorDetail(field="roles", message=f"Unknown role: {role!r}") for role in unknown],
        )
    deduped = sorted(set(payload.roles))
    if str(user_id) == principal.sub and "admin" not in deduped:
        raise ConflictError("An admin cannot remove their own admin role.")
    user = await repo.update(user, roles=deduped)
    audit_event(
        "admin.user.roles_set",
        actor=principal.sub,
        resource=f"user:{user.id}",
        outcome="success",
        changed_fields=["roles"],
    )
    return _to_admin_user_out(user)


@router.post(
    "/users/{user_id}/force-verify",
    response_model=AdminUserOut,
    summary="Force-verify user email (admin)",
    operation_id="force_verify_admin_user_admin_users__user_id__force-verify_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE},
)
async def force_verify_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> AdminUserOut:
    """Idempotent: sets `email_verified=True`/`verified_at=<now>` if not
    already verified, otherwise a no-op read-back -- either way returns the
    current `AdminUserOut`. The one sanctioned way to unblock a user whose
    verification email never arrived without making them go through
    `AccountService`'s own token-issuing flow again."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    if not user.email_verified:
        user = await repo.update(user, email_verified=True, verified_at=utc_now())
    audit_event(
        "admin.user.force_verify",
        actor=principal.sub,
        resource=f"user:{user.id}",
        outcome="success",
        changed_fields=["email_verified"],
    )
    return _to_admin_user_out(user)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete user (admin)",
    operation_id="delete_admin_user_admin_users__user_id__delete",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def delete_admin_user(
    user_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> None:
    """Soft-deletes via `AsyncRepository.delete()` (`obj.mark_deleted()` --
    `User` composes `SoftDeleteMixin`, same as every other model in this
    catalog; never a hard `DELETE`, matching this app's "never hard-delete a
    User row" posture documented on `app/models/user.py` and `_core.py`'s
    own soft-delete-vs-hard-delete notes). Self-protection: the acting admin
    cannot delete their own account (409)."""
    repo = AsyncRepository(db, User)
    user = await repo.get(user_id)
    if user is None:
        raise NotFoundError(f"User {user_id} was not found.")
    _ensure_not_self(principal, user.id, action="delete")
    await repo.delete(user)
    audit_event(
        "admin.user.delete",
        actor=principal.sub,
        resource=f"user:{user_id}",
        outcome="success",
    )
