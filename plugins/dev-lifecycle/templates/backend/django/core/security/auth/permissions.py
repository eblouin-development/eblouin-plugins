"""DRF `HasRole` permission (Stage 5d, #46) — this is BLOCK APP CODE, not
part of the vendored, DRF-free `core/security/auth/django.py`.

**WHY THIS LIVES HERE, NOT IN THE VENDORED `django.py`.** That file's own
module docstring is explicit about the boundary: "No DRF `permission_class`
is defined here -- a Django-BLOCK app's own `HasRole`-style permission
class (composing `require_roles` above) is application code, not part of
this vendored, framework-glue-only file." `django.py` is deliberately
`rest_framework`-free so a plain-Django (non-DRF) project can vendor
`_core.py` + `_cookies.py` + `django.py` alone and call `resolve_principal`/
`require_roles` directly from an ordinary view, with zero dependency on
`rest_framework` ever being installed — see that file's own "Django only,
not DRF-specific" paragraph. Importing `rest_framework.permissions.
BasePermission` into `django.py` would break that promise for every
non-DRF consumer of this component, for the sole benefit of DRF consumers.
This file is that DRF-specific glue instead: it lives one directory-mate
over, free to import `rest_framework` because this whole block already
depends on it (`core/views.py`, `core/exceptions.py`, `core/serializers.py`
all do too), and is NOT touched by the weekly freshness audit (only
`_core.py`/`_cookies.py`/`django.py` are vendored — see this package's
`__init__.py` for the identical "vendored vs. app code stay in separate
files" split already drawn for `stores.py`).

**The sync-permission / async-principal bridge.** `BasePermission.
has_permission(self, request, view)` is an ordinary SYNC method — DRF's
own permission-checking machinery (`APIView.check_permissions()`, called
from `dispatch()`) calls it synchronously; there is no `async def`
variant DRF will await for you. `require_roles` (`core/security/auth/
django.py`) is `async def`, because the `AuthService.resolve_access` it
calls (`_core.py`) is itself async — the same "async ORM/service under a
sync DRF view" shape this whole block already bridges everywhere else
(`core/views.py`'s module docstring: "every one of these views is an
ordinary SYNC DRF `APIView` method ... bridged with `asgiref.sync.
async_to_sync(...)`"). `has_permission` below bridges the identical way:
`async_to_sync(require_roles)(request, auth_service, *roles)`, called from
inside the sync method DRF actually invokes — no new bridging idiom, just
this block's existing one applied at a different call site.

**Auth service construction.** `build_session_service()` and
`build_auth_service()` (`core/security/auth/stores.py`) — the SAME
zero-argument factories `core/views.py`'s own views already call — are
built fresh on every `has_permission` invocation, matching those factories'
documented "cheap, stateless, no caching" posture (a fresh service and
fresh, `__init__`-less stores every call is fine). Both are constructed
because this gate serves both transports; only the one matching the
credential the request actually carries does any work.

**Exceptions propagate, never caught here.** `require_roles` raises either
`core.security.auth._core.InvalidToken` (a missing/malformed/expired
bearer token -- maps to 401 `unauthenticated`) or `core.security.auth.
InsufficientRole` (the resolved principal lacks a required role -- maps to
403 `permission_denied`); both are `AuthError` subclasses, and `core/
exceptions.py`'s `exception_handler` (this project's DRF
`EXCEPTION_HANDLER`, `config/settings.py`) already maps the whole `AuthError`
hierarchy onto the correct `ErrorEnvelope` via `AUTH_ERROR_HTTP`
(`core/security/auth/django.py`). Neither exception is caught here — this
mirrors `app/api/deps.py`'s FastAPI `require_admin` dependency, which lets
the identical two exceptions propagate to `app/main.py`'s
`_auth_error_handler` rather than translating them itself.

`has_permission` therefore either returns `True` or lets an exception
fly — it deliberately never returns `False` for an auth failure, since a
bare DRF `False` return would only ever surface DRF's own generic,
un-enveloped 403 (`core.exceptions.exception_handler`'s
`drf_exceptions.PermissionDenied` branch, a DIFFERENT — if
similarly-shaped — envelope than the `AuthError` branch's own, and with
no distinction between "not authenticated at all" and "authenticated but
missing a role"), bypassing the two-status-code (401 vs 403) contract this
block's `AuthError` branch exists to preserve. This is the exact same
reason `core/views.py`'s `MeView` enforces its own auth manually (via
`resolve_principal`) rather than through DRF's built-in
`IsAuthenticated`/`permission_classes` rejection path — see that view's
own docstring."""

from __future__ import annotations

from typing import Any

from asgiref.sync import async_to_sync
from rest_framework.permissions import BasePermission

from core.security.auth import require_roles_either
from core.security.auth.stores import build_auth_service, build_session_service


def has_role(*roles: str) -> type[BasePermission]:
    """Factory returning a fresh `BasePermission` subclass gated on
    `*roles` — used exactly like DRF's own `IsAdminUser`/`IsAuthenticated`:
    `permission_classes = [has_role("admin")]` on a view/viewset.

    A factory (returning a new class per call), not one fixed class,
    because the required role SET is route-specific — `has_role("admin")`
    and a hypothetical future `has_role("billing", "admin")` need distinct
    classes DRF can instantiate per-view; this mirrors why `app/api/
    deps.py`'s FastAPI `require_roles(get_current_principal, ...)` is
    itself called once per distinct role-set (`require_admin =
    require_roles(get_current_principal, "admin")`) rather than being one
    fixed dependency object. Membership is checked with AND semantics
    (every listed role must be present) — see `require_roles`'s own
    docstring (`core/security/auth/django.py`) for the exact rule; a route
    needing OR semantics is not something this factory (or its FastAPI
    counterpart) attempts to guess at."""

    class _HasRole(BasePermission):
        def has_permission(self, request: Any, view: Any) -> bool:
            # `require_roles_either`, not `require_roles`: this gate must
            # accept BOTH transports, resolving whichever credential the
            # request actually carries -- the `session_id` cookie first
            # (this block's default for browsers), falling back to a bearer
            # token (native/mobile, service-to-service). The session-first
            # ordering is load-bearing, not cosmetic: see that helper's own
            # docstring on why checking bearer first would let an
            # attacker-supplied `Authorization` header win over a victim's
            # ambient session cookie. Role membership itself is identical
            # either way, since `SessionPrincipal` and `AccessClaims` agree
            # on `roles`.
            async_to_sync(require_roles_either)(
                request, build_session_service(), build_auth_service(), *roles
            )
            return True

    return _HasRole
