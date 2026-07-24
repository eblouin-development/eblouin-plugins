"""DRF views — Stage 4 Step 2 (#27), the contract-emission layer, with the
`/auth/*` views given real behavior in Stage 5b (#44). Mirrors
`backend/fastapi`'s `app/api/routers/{items,health,auth}.py` handler-for-
handler; see each view's own docstring for the specific behavior it
reproduces and this block's README, "Conformance", for the wire-identity
target these routes work toward.

`items`/`health`/`readyz` wiring (Step 2 scope) stops at status codes +
JSON bodies; their `permission_classes` stay `AllowAny` deliberately (Stage
5, #28, only ever scoped to `/auth/*` -- see `README.md`'s own Stage 5
scope note), matching `backend/fastapi`'s items/health routers having no
`Depends(get_current_principal)` either, never a bare omission
(references/backend/drf.md's "Permissions & queryset scoping": "Never
leave an endpoint AllowAny by omission").

The five `/auth/*` views below ARE the real behavior: each validates its
request body via the matching serializer (DRF's own `is_valid(raise_
exception=True)` -- a malformed body still 422s, mapped by `core.
exceptions.exception_handler`'s `ValidationError` branch), builds a fresh
`AuthService` via `core.security.auth.stores.build_auth_service()`, and
bridges into that service's async methods with `asgiref.sync.
async_to_sync(...)` -- every one of these views is an ordinary SYNC DRF
`APIView` method (`def`, not `async def`), the same "sync view, async
service, bridged at the call site" posture `stores.py`'s own module
docstring documents. Every raised `_core.AuthError` (`InvalidCredentials`,
`InvalidToken`, `TokenReused`, `EmailAlreadyExists`) is left UNCAUGHT here
-- `core/exceptions.py`'s `exception_handler` maps the whole hierarchy onto
`ErrorEnvelope`, mirroring `app/main.py`'s `_auth_error_handler` on the
FastAPI track. No view below ever constructs an `ErrorEnvelope`/`AppError`
itself, same posture `app/api/routers/auth.py`'s own module docstring
documents for its FastAPI counterparts."""

from __future__ import annotations

import re
import uuid

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import connection, transaction
from django.db.utils import Error as DjangoDBError
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema, extend_schema_view
from rest_framework import generics, status, viewsets
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from core.contract.errors import ConflictError, ErrorDetail, NotFoundError, ValidationFailedError
from core.models import BlogPost, Comment, Flag, Item, User
from core.pagination import ContractPageNumberPagination
from core.security.admin_rate_limit import enforce_admin_rate_limit
from core.security.auth import (
    AuthService,
    InvalidToken,
    clear_auth_cookies,
    enforce_csrf,
    generate_csrf_token,
    clear_session_cookies,
    read_refresh_cookie,
    read_session_cookie,
    require_roles,
    require_roles_either,
    resolve_principal,
    resolve_principal_either,
    set_auth_cookies,
    set_session_cookies,
)
from core.security.auth.permissions import has_role
from core.security.auth.stores import (
    AuditAuthEventSink,
    DjangoRefreshTokenStore,
    DjangoUserStore,
    build_account_service,
    build_auth_service,
    build_lockout_policy,
    build_session_service,
    get_password_service,
    get_token_service,
    utc_now,
)
from core.security.audit_logging.audit import audit_event
from core.serializers import (
    AdminRolesInSerializer,
    AdminUserOutSerializer,
    BlogPostCreateSerializer,
    BlogPostOutSerializer,
    BlogPostSummaryOutSerializer,
    BlogPostUpdateSerializer,
    CommentOutSerializer,
    ErrorEnvelopeSerializer,
    FlagDismissInSerializer,
    FlagOutSerializer,
    FlagResolveInSerializer,
    HealthStatusSerializer,
    ItemCreateSerializer,
    ItemOutSerializer,
    ItemUpdateSerializer,
    LoginRequestSerializer,
    PrincipalOutSerializer,
    PublicBlogPostOutSerializer,
    PublicBlogPostSummaryOutSerializer,
    ReadinessStatusSerializer,
    RefreshRequestSerializer,
    RegisterRequestSerializer,
    RequestPasswordResetRequestSerializer,
    ResetPasswordRequestSerializer,
    TokenResponseSerializer,
    VerifyEmailRequestSerializer,
    derive_excerpt,
)
from core.services.sanitize import sanitize_blog_html


def _build_login_auth_service() -> AuthService:
    """Stage 5c (#45): the LOGIN-GATED `AuthService` — same store/service
    composition `core.security.auth.stores.build_auth_service()` uses
    (fresh `DjangoUserStore`/`DjangoRefreshTokenStore`, the process-wide
    `get_password_service()`, a fresh `get_token_service()`, `utc_now` as
    the shared clock), PLUS `lockout=build_lockout_policy()`,
    `require_verification=settings.AUTH_REQUIRE_EMAIL_VERIFICATION`, and
    `events=AuditAuthEventSink()` — mirroring `backend/fastapi`'s
    `app/api/deps.py:get_auth_service`'s own Stage 5c wiring exactly.

    Built here at the view call site, not inside `build_auth_service()`
    itself (Agent A's factory — deliberately left unchanged this stage;
    see that function's own docstring: "wiring `AuthService`'s own new
    keyword parameters into `LoginView`/`RegisterView` ... is Agent B's
    job"). `build_lockout_policy()` is called fresh here — same
    `DjangoLockoutStore`-backed table `core.security.auth.stores.
    build_account_service()`'s own `lockout=build_lockout_policy()` call
    reads/writes (Django's async ORM has no per-request session object to
    share the way `backend/fastapi`'s `AsyncSession` does — see
    `build_lockout_policy`'s own docstring — so "the same policy" here
    means "the same underlying table", which is what actually matters:
    a lockout `LoginView` records is visible to, and can be lifted by, a
    later `AccountService.reset_password` call built from a completely
    separate `build_lockout_policy()` invocation).

    `RegisterView` deliberately does NOT use this — `AuthService.register`
    never consults `lockout`/`require_verification`/`events` at all (see
    `_core.AuthService.register`'s own docstring), so the plain, unwired
    `build_auth_service()` remains the right (and simpler) choice there."""
    return AuthService(
        users=DjangoUserStore(),
        refresh_tokens=DjangoRefreshTokenStore(),
        passwords=get_password_service(),
        tokens=get_token_service(),
        now=utc_now,
        lockout=build_lockout_policy(),
        require_verification=settings.AUTH_REQUIRE_EMAIL_VERIFICATION,
        events=AuditAuthEventSink(),
    )

# Stage 4 Step 4 (#27): every `operation_id`/`tags` value below is set to
# the EXACT string `packages/api-client/openapi.json` (the frozen FastAPI
# contract) already uses for the same operation — FastAPI auto-derives its
# operationIds from `{handler_name}_{path}_{method}` (see that file's own
# `operationId` values); drf-spectacular has no equivalent auto-derivation
# from a DRF view/action name that would land on the same string, so this
# block sets them explicitly rather than accepting spectacular's own
# default naming. This is what gets this block's exported schema to FULL
# operationId parity (not just best-effort) with the frozen contract — see
# README.md, "Conformance", and tests/test_schema_conformance.py for the
# proof.

@extend_schema_view(
    list=extend_schema(
        operation_id="list_items_items_get",
        tags=["items"],
        responses={200: ItemOutSerializer, 422: ErrorEnvelopeSerializer},
    ),
    create=extend_schema(
        operation_id="create_item_items_post",
        tags=["items"],
        request=ItemCreateSerializer,
        responses={201: ItemOutSerializer, 422: ErrorEnvelopeSerializer},
    ),
    retrieve=extend_schema(
        operation_id="get_item_items__item_id__get",
        tags=["items"],
        responses={200: ItemOutSerializer, 404: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    ),
    partial_update=extend_schema(
        operation_id="update_item_items__item_id__patch",
        tags=["items"],
        request=ItemUpdateSerializer,
        responses={200: ItemOutSerializer, 404: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    ),
    destroy=extend_schema(
        operation_id="delete_item_items__item_id__delete",
        tags=["items"],
        responses={204: None, 404: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    ),
)
class ItemViewSet(viewsets.ModelViewSet):
    """Full CRUD for `Item` — the DRF counterpart to backend/fastapi's
    `app/api/routers/items.py`. Every handler below is thin: validate (via
    the serializer DRF already ran), delegate to the ORM, map to
    `ItemOutSerializer`, return — same shape as the FastAPI router's own
    "validate, delegate, map, return" pattern.

    `queryset = Item.objects.all()` already excludes soft-deleted rows —
    `Item.objects` is `ItemManager`, scoped through `ItemQuerySet.
    not_deleted()` by default (core/models.py) — so `list`/`retrieve`
    (and this class's own `get_object` override below) never need to
    repeat that filter by hand.

    `http_method_names` below deliberately excludes `"put"`: `ModelViewSet`
    + a router would otherwise also expose `PUT /items/{item_id}` (full
    replace) via `update()`, which `packages/api-client/openapi.json` does
    not define (FastAPI's `items` router only has PATCH). Excluding it
    means Django's own `View.dispatch()` routes a PUT request to
    `http_method_not_allowed()` (405) before `update()` (below) is ever
    reached — a stray PUT gets the correct "this verb doesn't exist"
    answer instead of being silently accepted, matching the frozen
    contract exactly rather than merely being a harmless no-op for it.
    `update()` still forces partial semantics (`partial=True`
    unconditionally) purely because `partial_update()` (PATCH) delegates
    into it — see that method's own docstring."""

    # Router-registered PUT is excluded so a stray full-replace request
    # 405s instead of silently landing on `update()` (see class docstring
    # above) — `packages/api-client/openapi.json` has no PUT operation for
    # this resource. `"head"`/`"options"` stay: DRF/Django handle both
    # generically and every other view in this block leaves them enabled
    # too.
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    # `.order_by(...)`: `Item` has no default `Meta.ordering` — an
    # unordered queryset paginates non-deterministically page-to-page
    # (DRF's own `UnorderedObjectListWarning`), which would make `GET
    # /items?page=2` an unreliable continuation of page 1. `created_at`
    # (insertion order) then `id` (a stable tiebreaker for same-instant
    # rows) gives every page a deterministic, repeatable order without
    # requiring a model-level change to core/models.py.
    queryset = Item.objects.all().order_by("created_at", "id")
    permission_classes = [AllowAny]

    def get_serializer_class(self):
        if self.action == "create":
            return ItemCreateSerializer
        if self.action in {"update", "partial_update"}:
            return ItemUpdateSerializer
        return ItemOutSerializer

    def get_object(self):
        """Overrides DRF's default (`get_object()` normally raises a bare
        `Http404`) so a missing item renders the SAME message text
        backend/fastapi's `NotFoundError(f"Item {item_id} was not
        found.")` uses (app/api/routers/items.py's `get_item`/
        `update_item`/`delete_item`) — this is what lets the
        conformance-proof tests build the expected envelope straight from
        `core.contract.errors.NotFoundError` and assert byte-equality
        against what this view actually sends, not just "some 404"."""
        pk = self.kwargs[self.lookup_url_kwarg or self.lookup_field]
        try:
            return self.get_queryset().get(pk=pk)
        except (Item.DoesNotExist, ValueError, TypeError, DjangoValidationError):
            # ValueError/TypeError/DjangoValidationError: a malformed
            # (non-UUID) `item_id`. Which of these three Django's UUIDField
            # lookup actually raises for a value that isn't a well-formed
            # UUID depends on the value's shape — `django.core.exceptions.
            # ValidationError` (via `UUIDField.to_python()`, in the normal
            # `.get(pk=pk)` filtering path) is the common case, but
            # ValueError/TypeError are kept too since they're the cheaper,
            # narrower failure a raw `uuid.UUID(pk)` coercion elsewhere
            # could still raise. All three mean the same thing here: "not a
            # value that could ever address a real row." FastAPI's
            # path-typed `item_id: uuid.UUID` would instead reject that at
            # 422 (a routing-level type mismatch) before the handler ever
            # runs — a second, smaller accepted per-framework divergence
            # alongside the one this block's README already documents for
            # PageParams `extra="forbid"`. Treating it as "not found" here
            # (404) rather than "unvalidatable" (422) is the more
            # conservative of the two readings for an ID a caller already
            # has no way to have gotten right, and keeps this override's
            # error path single (one exception type raised, not two).
            raise NotFoundError(f"Item {pk} was not found.") from None

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        return Response(ItemOutSerializer(serializer.instance).data, status=status.HTTP_201_CREATED)

    def update(self, request, *args, **kwargs):
        instance = self.get_object()
        serializer = self.get_serializer(instance, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        self.perform_update(serializer)
        return Response(ItemOutSerializer(serializer.instance).data)

    def perform_destroy(self, instance):
        """Soft-delete via `Item.mark_deleted()`, mirroring backend/
        fastapi's `AsyncRepository.delete()` semantics for this model (see
        that repository's own delete implementation and core/models.py's
        module docstring) — a row disappears from every default-manager
        lookup, this is NOT a hard SQL `DELETE`."""
        instance.mark_deleted()
        instance.save(update_fields=["deleted_at"])


class HealthCheckView(APIView):
    """`GET /health` — liveness only, touches nothing but the process
    itself (no DB), matching backend/fastapi's `app/api/routers/
    health.py`'s `health_check` and its own docstring on why liveness
    must not depend on the database."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="health_check_health_get",
        tags=["health"],
        responses={200: HealthStatusSerializer},
    )
    def get(self, request):
        return Response(HealthStatusSerializer({"status": "ok"}).data)


class ReadinessCheckView(APIView):
    """`GET /readyz` — readiness: runs a real `SELECT 1` against the
    configured database, mirroring backend/fastapi's `readiness_check`.
    `django.db.utils.Error` is the base of Django's whole DB-exception
    hierarchy (`OperationalError`, `InterfaceError`, ...) — caught
    specifically, the same "a real DB-connectivity failure, not a bare
    `except Exception`" posture the FastAPI sibling documents, so a
    genuine application bug inside this view still surfaces as an
    unhandled 500 via `core.exceptions.exception_handler`'s catch-all
    instead of being misreported as "DB down".

    A DB-down response is a plain `ReadinessStatus(status="unavailable")`
    body at 503, NOT `ErrorEnvelope` — same judgment call backend/
    fastapi's `health.py` documents: `ErrorCode` has no
    `service_unavailable` member, and an orchestrator polls this by status
    code, not by envelope shape."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="readiness_check_readyz_get",
        tags=["health"],
        responses={200: ReadinessStatusSerializer, 503: ReadinessStatusSerializer},
    )
    def get(self, request):
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except DjangoDBError:
            return Response(
                ReadinessStatusSerializer({"status": "unavailable"}).data,
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response(ReadinessStatusSerializer({"status": "ready"}).data)


class RegisterView(APIView):
    """`POST /auth/register` (Stage 5b, #44; account-lifecycle side effect
    Stage 5c, #45) — real handler, the DRF counterpart to `app/api/
    routers/auth.py`'s `register`. Delegates straight to `AuthService.
    register` — raises `EmailAlreadyExists` (-> 409 `conflict`, via
    `core.exceptions.exception_handler`'s `AuthError` branch) for a
    duplicate normalized email, uncaught here (see this module's own
    docstring).

    Stage 5c: on success, additionally (a) sends a verification email
    (`AccountService.request_email_verification(user)` — the freshly
    created `UserRecord` `AuthService.register` just returned, no extra
    lookup needed) and (b) emits an `auth.register` audit event. Neither
    changes this endpoint's response shape (still 201 `PrincipalOut`) —
    a project whose `settings.AUTH_REQUIRE_EMAIL_VERIFICATION` is `True`
    (the secure default) needs the caller to actually consume the emailed
    link (`POST /auth/verify-email`) before `LoginView`'s gated
    `AuthService.login` will let this account in.

    `request_email_verification` is wrapped in `try/except Exception` —
    the user row is already durably committed by the time this runs
    (`AuthService.register` returned successfully), so a verification-
    email failure here (SMTP outage, bounced address) must NEVER turn
    into a 500: the account already exists, a retry would just 409 on
    the duplicate email, `require_verification=True` means the account
    can't log in either way, and the wire caller (whoever showed the
    registration form) has no way to "undo" or recover a 500 here — it
    would brick a just-created account with no path forward. Register
    stays 201 regardless of whether the email actually went out; the
    failure is only logged/audited (`auth.register.
    verification_email_failed`, no PII/token in the event), never
    surfaced to the caller. The recovery path for an account whose
    verification email never arrived is `POST /auth/request-password-
    reset` -> `POST /auth/reset-password` — `AccountService.
    reset_password` also marks the email verified (see that method's own
    docstring, `_core.py`), so a user who never got their verification
    link can still get into their account. Byte-for-byte the same
    rationale as `app/api/routers/auth.py`'s own `register` handler
    (adversarial-review fix M2 there)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="register_auth_register_post",
        tags=["auth"],
        request=RegisterRequestSerializer,
        responses={201: PrincipalOutSerializer, 409: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = RegisterRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth_service = build_auth_service()
        user = async_to_sync(auth_service.register)(
            serializer.validated_data["email"], serializer.validated_data["password"]
        )
        account_service = build_account_service()
        try:
            async_to_sync(account_service.request_email_verification)(user)
        except Exception:
            # M2 (ported from app/api/routers/auth.py's register handler):
            # never let a verification-email delivery failure 500 an
            # already-committed registration -- see this view's own
            # docstring above. No PII/token in this event -- just that it
            # happened, for a human to notice and, if needed, resend by
            # hand.
            async_to_sync(AuditAuthEventSink().emit)(
                "auth.register.verification_email_failed", actor=user.id, outcome="failure"
            )
        async_to_sync(AuditAuthEventSink().emit)("auth.register", actor=user.id, outcome="success")
        principal = PrincipalOutSerializer({"id": uuid.UUID(user.id), "email": user.email})
        return Response(principal.data, status=status.HTTP_201_CREATED)


class VerifyEmailView(APIView):
    """`POST /auth/verify-email` (Stage 5c, #45) — real handler, the DRF
    counterpart to `app/api/routers/auth.py`'s `verify_email`. Delegates
    to `AccountService.verify_email` — raises `InvalidSingleUseToken`
    (-> 401 `unauthenticated`, generic and wire-identical to every other
    single-use-token rejection reason — see that exception's own
    docstring) for an unknown/expired/already-used/wrong-purpose token,
    uncaught here (see this module's own docstring). On success, marks
    the token's owning user's email verified — see `LoginView`'s
    `require_verification` gate (`_build_login_auth_service`, above) for
    why that matters: with `settings.AUTH_REQUIRE_EMAIL_VERIFICATION=True`
    (the default), login for this account was refused (generically, as
    `InvalidCredentials`) until this endpoint succeeds."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="verify_email_auth_verify_email_post",
        tags=["auth"],
        request=VerifyEmailRequestSerializer,
        responses={204: None, 401: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = VerifyEmailRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_service = build_account_service()
        async_to_sync(account_service.verify_email)(serializer.validated_data["token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class RequestPasswordResetView(APIView):
    """`POST /auth/request-password-reset` (Stage 5c, #45) — real handler,
    the DRF counterpart to `app/api/routers/auth.py`'s
    `request_password_reset`. Delegates to `AccountService.
    request_password_reset` — that method NEVER raises and never reveals
    whether `email` has an account (see its own docstring on the anti-
    user-enumeration defense this mirrors from `AuthService.login`'s own
    `InvalidCredentials`), so this view ALWAYS returns 202 with a
    genuinely EMPTY body (`Response(status=202)` with no data — DRF
    sends no content body at all when a view returns no `data`, matching
    `app/api/routers/auth.py`'s own explicit `Response(..., content=b"")`
    — a byte-identical, content-free response is the strongest form of
    "this endpoint reveals nothing" for a known email and an unknown one
    alike), never a 404/409 that would leak account existence. A `422`
    (declared below) is the one response shape this endpoint CAN still
    send, for a request body that fails `RequestPasswordResetRequestSerializer`'s
    own schema validation (e.g. an empty `email` string) before this
    view's body ever runs."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="request_password_reset_auth_request_password_reset_post",
        tags=["auth"],
        request=RequestPasswordResetRequestSerializer,
        # `202: OpenApiTypes.ANY` (`build_basic_type(OpenApiTypes.ANY) ==
        # {}`), not `None` -- matches `packages/api-client/openapi.json`'s
        # own documented 202 exactly: FastAPI's `Response(status_code=202,
        # content=b"")` (no `response_model`) still documents a
        # `application/json` media type with an empty (`{}`, "unspecified
        # shape") schema, NOT an absent `content` key the way a genuinely
        # bodiless `None` response (`LogoutView`'s 204, below) does. A raw
        # `{}` dict here is NOT equivalent -- `drf_spectacular.openapi.
        # AutoSchema._get_response_for_code`'s `if not serializer:` falsy
        # check fires for an empty dict BEFORE its `isinstance(serializer,
        # dict)` raw-schema branch is ever reached, collapsing it to "No
        # response body" (no `content` key at all) instead -- `OpenApiTypes.
        # ANY` is `is_basic_type`, so it reaches `build_basic_type` instead,
        # which is what actually produces the `{}` schema this needs. See
        # tests/test_schema_conformance.py's strict wire-surface proof,
        # which this documents.
        responses={202: OpenApiTypes.ANY, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = RequestPasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_service = build_account_service()
        async_to_sync(account_service.request_password_reset)(serializer.validated_data["email"])
        return Response(status=status.HTTP_202_ACCEPTED)


class ResetPasswordView(APIView):
    """`POST /auth/reset-password` (Stage 5c, #45) — real handler, the DRF
    counterpart to `app/api/routers/auth.py`'s `reset_password`.
    Delegates to `AccountService.reset_password` — raises
    `InvalidSingleUseToken` (-> 401 `unauthenticated`, generic — see
    `VerifyEmailView`'s docstring above for the identical rationale) for
    an unknown/expired/already-used/wrong-purpose reset token, uncaught
    here. On success, revokes EVERY refresh-token family the user has
    (every device/session is logged out, not just the one that requested
    the reset — see `AccountService.reset_password`'s own docstring,
    `_core.py`) and, if a lockout policy is wired, lifts any failed-login
    lockout on the account — the same underlying `DjangoLockoutStore`
    table `LoginView`'s own `_build_login_auth_service()` records
    against, so the reset account can log in with its new password
    immediately."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="reset_password_auth_reset_password_post",
        tags=["auth"],
        request=ResetPasswordRequestSerializer,
        responses={204: None, 401: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = ResetPasswordRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        account_service = build_account_service()
        async_to_sync(account_service.reset_password)(
            serializer.validated_data["token"], serializer.validated_data["new_password"]
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class LoginView(APIView):
    """`POST /auth/login` (Stage 5b, #44; verification + lockout + audit
    gate Stage 5c, #45; web cookie mode Stage 5d, #46) — real handler.
    Delegates to `AuthService.login` — raises `InvalidCredentials` (-> 401
    `unauthenticated`) identically for an unknown email, a wrong password,
    an account locked out from too many recent failures, AND (as of Stage
    5c) an account whose email isn't verified yet — all four are
    wire-BYTE-IDENTICAL, uncaught here (see that exception's own docstring
    on the deliberate user-enumeration defense, and `_core.AuthService.
    login`'s own docstring for the full 6-step state machine this now runs
    through `_build_login_auth_service()`'s wiring).

    Stage 5c: `auth_service` is now built via `_build_login_auth_service()`
    (this module, above) instead of the plain `build_auth_service()` every
    other `/auth/*` view still uses — the SAME `lockout`/
    `require_verification`/`events` wiring `backend/fastapi`'s
    `app/api/deps.py:get_auth_service` applies to its own `AuthService`.

    Stage 5d web cookie mode: `request.headers.get("X-Auth-Mode") ==
    "cookie"` switches this call into cookie mode — read directly off
    `request.headers` (present on a DRF `Request` the identical way it is
    on a plain `HttpRequest` — see `core/security/auth/django.py`'s own
    module docstring), deliberately NOT a declared serializer field: this
    keeps it out of `LoginRequestSerializer`'s documented shape, so this
    stage's wire-contract diff is exactly the new `/admin/ping` operation,
    not a field addition on an existing one — the byte-for-byte same
    reasoning `app/api/routers/auth.py`'s own `login` handler documents
    for reading it off `request.headers` rather than a declared FastAPI
    `Header(...)` parameter. Anything else (absent header, any other
    value) is BEARER mode — the exact, unchanged prior behavior; mode is
    NEVER inferred from any other signal, matching the locked design. No
    CSRF check on login either way: login is credential-authenticated
    (email+password), and there is no cookie yet for a CSRF check to
    protect.

    Cookie mode still returns the SAME `TokenResponseSerializer` shape —
    the wire contract is byte-unchanged — but with `refresh_token=""` in
    the body (an empty string still satisfies the schema's required `str`
    field); the real refresh JWT travels ONLY in the HttpOnly
    `refresh_token` cookie `set_auth_cookies` sets below, alongside a
    fresh, independent CSRF cookie (`generate_csrf_token()` — never
    derived from either token) a SPA echoes back as `X-CSRF-Token` on
    every cookie-authenticated `/auth/refresh`/`/auth/logout` call.
    `max_age` is `settings.JWT_REFRESH_TTL_SECONDS` — the SAME TTL
    `get_token_service()` mints the refresh JWT itself against (`core/
    security/auth/stores.py`), so neither cookie outlives the refresh
    token it's paired with. Byte-for-byte the same shape as `app/api/
    routers/auth.py`'s own `login` handler's cookie-mode branch."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="login_auth_login_post",
        tags=["auth"],
        request=LoginRequestSerializer,
        responses={200: TokenResponseSerializer, 401: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = LoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth_service = _build_login_auth_service()
        email = serializer.validated_data["email"]
        password = serializer.validated_data["password"]
        mode = request.headers.get("X-Auth-Mode")
        if mode == "bearer":
            pair = async_to_sync(auth_service.login)(email, password)
            tokens = TokenResponseSerializer(
                {"access_token": pair.access, "refresh_token": pair.refresh}
            )
            return Response(tokens.data)
        if mode == "cookie":
            pair = async_to_sync(auth_service.login)(email, password)
            tokens = TokenResponseSerializer({"access_token": pair.access, "refresh_token": ""})
            response = Response(tokens.data)
            set_auth_cookies(
                response,
                refresh_value=pair.refresh,
                csrf_value=generate_csrf_token(),
                max_age=settings.JWT_REFRESH_TTL_SECONDS,
            )
            return response
        # DEFAULT: server-side session. Calls `authenticate` rather than
        # `login` deliberately -- `login` would additionally mint and
        # persist a RefreshRecord for a refresh token no client will ever
        # hold, starting a token family logout would never revoke. See that
        # method's own docstring in `_core.py`.
        user = async_to_sync(auth_service.authenticate)(email, password)
        session_service = build_session_service()
        issued = async_to_sync(session_service.create)(user)
        tokens = TokenResponseSerializer(
            {"access_token": "", "refresh_token": "", "token_type": "session"}
        )
        response = Response(tokens.data)
        set_session_cookies(
            response,
            session_value=issued.session_id,
            csrf_value=generate_csrf_token(),
            max_age=issued.max_age_seconds(utc_now()),
        )
        return response


class RefreshView(APIView):
    """`POST /auth/refresh` (Stage 5b, #44; web cookie mode Stage 5d, #46)
    — real handler. Delegates to `AuthService.refresh` — THE
    rotation-with-reuse-detection state machine (see `_core.py`'s own
    module docstring and `AuthService.refresh`'s docstring for the full
    state machine). Raises `InvalidToken` or `TokenReused` (both -> 401
    `unauthenticated`, deliberately indistinguishable at the wire — see
    `TokenReused`'s own docstring and `core/exceptions.py`'s FIX-B
    section), uncaught here. A `TokenReused` raise has, as a side effect,
    ALREADY revoked the token's entire family in the DB by the time this
    handler's caller sees the 401.

    Stage 5d web cookie mode: DUAL-SOURCE, decided per-request by
    `read_refresh_cookie(request)` (whether the `refresh_token` cookie is
    actually present on THIS request), never by a header the client
    declares — a forged/absent cookie can't claim cookie mode, and a
    genuine cookie-bearing browser request can't accidentally fall onto
    the bearer path either. Byte-for-byte the same dual-source shape as
    `app/api/routers/auth.py`'s own `refresh` handler.

    - **Cookie path** (cookie present): `enforce_csrf(request)` runs
      FIRST — raises `CsrfValidationError` (-> 403 `permission_denied`)
      before the cookie's refresh token is ever presented to
      `AuthService.refresh` at all, so a request that fails the
      double-submit check never gets to attempt a rotation. The request
      BODY's `refresh_token` is still validated (`RefreshRequestSerializer`'s
      shape is unchanged, still required) but its VALUE is deliberately
      ignored — the cookie's own value is what's rotated. On success,
      BOTH cookies are set again (a fresh refresh JWT + a FRESH
      `generate_csrf_token()`, never the old CSRF value) — exactly
      `LoginView`'s own cookie-setting shape — so a stolen,
      already-rotated refresh cookie replayed after this response is
      rejected the same way `AuthService.refresh`'s reuse-detection
      already rejects any other reused refresh token (401, whole family
      revoked). The response body is `TokenResponseSerializer` with
      `refresh_token=""`, matching `LoginView`'s cookie-mode shape.
    - **Bearer path** (no cookie): the exact, unchanged prior behavior —
      the body's `refresh_token` is the real token, no CSRF check, and
      the real new refresh JWT is returned in the body."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="refresh_auth_refresh_post",
        tags=["auth"],
        request=RefreshRequestSerializer,
        responses={200: TokenResponseSerializer, 401: ErrorEnvelopeSerializer, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        serializer = RefreshRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        auth_service = build_auth_service()
        cookie_refresh_token = read_refresh_cookie(request)
        if cookie_refresh_token is not None:
            enforce_csrf(request)
            pair = async_to_sync(auth_service.refresh)(cookie_refresh_token)
            tokens = TokenResponseSerializer({"access_token": pair.access, "refresh_token": ""})
            response = Response(tokens.data)
            set_auth_cookies(
                response,
                refresh_value=pair.refresh,
                csrf_value=generate_csrf_token(),
                max_age=settings.JWT_REFRESH_TTL_SECONDS,
            )
            return response
        pair = async_to_sync(auth_service.refresh)(serializer.validated_data["refresh_token"])
        tokens = TokenResponseSerializer({"access_token": pair.access, "refresh_token": pair.refresh})
        return Response(tokens.data)


class LogoutView(APIView):
    """`POST /auth/logout` (Stage 5b, #44; web cookie mode Stage 5d, #46)
    — real handler. Delegates to `AuthService.logout` — best-effort and
    idempotent by design (see that method's own docstring): an
    already-invalid, unknown, or already-revoked refresh token still
    returns 204, never an error. Revokes the entire token family, not just
    the presented token. Deliberately given no error `responses=` entry
    beyond 422 (see `app/api/routers/auth.py`'s identical, documented
    choice for its own `logout` route) — it's 204 and idempotent by
    design, never raises an error a client needs to handle.

    Stage 5d web cookie mode: same dual-source shape as `RefreshView`
    above, decided by `read_refresh_cookie(request)`.

    - **Cookie path** (cookie present): JUDGMENT CALL — logout is
      STATE-CHANGING (it revokes the presented token's entire family via
      `AuthService.logout`), so this view enforces the double-submit CSRF
      check on the cookie path too, `enforce_csrf(request)` called BEFORE
      the best-effort logout runs — a cookie-present request with a
      missing/blank/mismatched `X-CSRF-Token` is rejected 403 at that
      gate and `AuthService.logout` is never even called; it does NOT
      reach 204. This does not weaken `AuthService.logout`'s own
      idempotency for the TOKEN itself — a bad/expired/already-revoked
      cookie value, once past the CSRF gate, still 204s exactly as the
      bearer path already does. On success, clears both cookies
      (`clear_auth_cookies`). Byte-for-byte the same judgment call
      `app/api/routers/auth.py`'s own `logout` handler documents.
    - **Bearer path** (no cookie): the exact, unchanged prior behavior —
      the body's `refresh_token`, no CSRF check, 204 either way."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="logout_auth_logout_post",
        tags=["auth"],
        request=RefreshRequestSerializer,
        responses={204: None, 422: ErrorEnvelopeSerializer},
    )
    def post(self, request):
        # SESSION path first -- the default credential. Checked before the
        # body is even validated, because a session client legitimately has
        # no `refresh_token` to send and must not be 422'd for omitting a
        # field that means nothing on its transport.
        session_id = read_session_cookie(request)
        if session_id is not None:
            enforce_csrf(request)
            async_to_sync(build_session_service().revoke)(session_id)
            response = Response(status=status.HTTP_204_NO_CONTENT)
            clear_session_cookies(response)
            return response
        auth_service = build_auth_service()
        cookie_refresh_token = read_refresh_cookie(request)
        if cookie_refresh_token is not None:
            enforce_csrf(request)
            async_to_sync(auth_service.logout)(cookie_refresh_token)
            response = Response(status=status.HTTP_204_NO_CONTENT)
            clear_auth_cookies(response)
            return response
        serializer = RefreshRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        async_to_sync(auth_service.logout)(serializer.validated_data["refresh_token"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class MeView(APIView):
    """`GET /auth/me` (Stage 5b, #44) — real handler, the bearer-scheme
    seam. `permission_classes = [AllowAny]`/`authentication_classes = []`
    stay unchanged from the Stage 4 stub — this view enforces
    authentication itself, via `resolve_principal` below, rather than
    through DRF's own `IsAuthenticated`/`authentication_classes` machinery
    (which would reject a missing/malformed bearer with DRF's own 403/401
    shape, bypassing this block's `ErrorEnvelope` — see
    `core/security/auth/django.py`'s `resolve_principal` docstring: a
    missing or malformed `Authorization` header raises `_core.InvalidToken`
    directly, the identical exception a present-but-invalid token raises,
    so both land on the SAME 401 `unauthenticated` envelope via
    `core/exceptions.py`'s `AuthError` branch).

    `resolve_principal` (async, bridged via `async_to_sync`) already
    verified the bearer access token and resolved it to `AccessClaims`
    before this method's body does anything else — a missing/malformed/
    expired token never reaches the `DjangoUserStore` lookup below at all.

    `AccessClaims` carries `sub` (the user id) and `roles`, but not
    `email` — this handler does one direct `DjangoUserStore.get_by_id`
    lookup to fill in `PrincipalOut.email`, independent of `AuthService`
    (which has no "fetch a profile" method — see `_core.py`'s `UserStore`
    Protocol; it's a storage seam for `AuthService`'s own register/login/
    refresh flows, not a general user-lookup API this view reaches for) —
    identical to `app/api/routers/auth.py`'s own `me` handler.

    The user having been deleted BETWEEN minting the access token and this
    request (a real, if narrow, race — access tokens are not individually
    revocable) is treated as `InvalidToken` (401), matching
    `AuthService.refresh`'s identical "row valid but the user it points to
    is gone" handling — NOT a 404, since the token itself is what's no
    longer trustworthy, not a missing resource the caller asked for by
    id.

    Stage 4 Step 4 (#27): the `HTTPBearer` security scheme is registered
    (`config/settings.py`'s `SPECTACULAR_SETTINGS["APPEND_COMPONENTS"]`)
    and this view opts into it via `@extend_schema(auth=[{"HTTPBearer":
    []}])` below (drf-spectacular's `auth` kwarg overrides `AutoSchema.
    get_auth()`, which is what actually populates the operation's
    `security` key) — an exact match with `openapi.json`'s own `security:
    [{"HTTPBearer": []}]` on this operation."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="me_auth_me_get",
        tags=["auth"],
        responses={200: PrincipalOutSerializer, 401: ErrorEnvelopeSerializer},
        auth=[{"HTTPBearer": []}],
    )
    def get(self, request):
        auth_service = build_auth_service()
        claims = async_to_sync(resolve_principal_either)(request, build_session_service(), auth_service)
        user = async_to_sync(DjangoUserStore().get_by_id)(claims.sub)
        if user is None:
            raise InvalidToken("This token no longer maps to an active user.")
        principal = PrincipalOutSerializer(
            {"id": uuid.UUID(user.id), "email": user.email, "roles": list(user.roles)}
        )
        return Response(principal.data)


class AdminPingView(APIView):
    """`GET /admin/ping` (Stage 5d, #46) — the RBAC admin example, the DRF
    counterpart to `app/api/routers/admin.py`'s `admin_ping`. Demonstrates
    `has_role` (`core/security/auth/permissions.py`) end to end, with no
    new auth logic of its own:

    - **200** for an authenticated principal whose `AccessClaims.roles`
      includes `"admin"`.
    - **403** `permission_denied` for an authenticated principal WITHOUT
      the `"admin"` role — `has_role("admin")`'s permission class raises
      `InsufficientRole` (`core/security/auth/django.py`), mapped by
      `core/exceptions.py`'s `AuthError` branch via `AUTH_ERROR_HTTP`.
    - **401** `unauthenticated` for a missing/malformed/expired bearer
      token — `require_roles`'s own `resolve_principal` call (inside
      `has_role`'s `has_permission`) raises `_core.InvalidToken` before
      DRF's dispatch ever reaches this view's `get` body.

    `permission_classes = [has_role("admin")]` does the entire job here —
    unlike `MeView` above, this view does NOT re-implement auth by hand in
    its body; `has_role`'s own docstring explains why that's still safe
    for this block's `ErrorEnvelope` contract (both exceptions it can raise
    are `AuthError` subclasses the existing handler already maps, so DRF's
    own un-enveloped `False`-return 403 path is never reached).
    `authentication_classes = []` mirrors every other view in this
    module — this block enforces authentication itself via the vendored
    auth component, never through DRF's separate `authentication_classes`
    machinery.

    Reuses `HealthStatusSerializer` (`{"status": str}`) as the response
    shape rather than inventing a near-identical serializer — this
    endpoint's own success body is exactly `{"status": "ok"}`, matching
    `app/api/routers/admin.py`'s own identical reuse of `HealthStatus` and
    this block's frozen-contract target, `packages/api-client/openapi.json`'s
    `admin_ping` operation, which this view's `operation_id`/`tags`/`auth`/
    `responses` below are set to match EXACTLY (see `tests/
    test_schema_conformance.py`, whose `_PENDING_PARITY_OPS` no longer
    lists `("/admin/ping", "get")` as of this stage)."""

    permission_classes = [has_role("admin")]
    authentication_classes: list = []

    @extend_schema(
        operation_id="admin_ping_admin_ping_get",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={200: HealthStatusSerializer, 401: ErrorEnvelopeSerializer, 403: ErrorEnvelopeSerializer},
    )
    def get(self, request):
        return Response(HealthStatusSerializer({"status": "ok"}).data)


# ---------------------------------------------------------------------------
# Stage 13b: admin user management — the DRF counterpart to `app/api/
# routers/admin.py`'s own admin user-management surface. See that module's
# docstring for the full design this mirrors handler-for-handler
# (self-protection, the suspend/ban/reinstate state machine, audit events,
# the tighter per-route rate limit, `status`-unfiltered admin queries).
#
# **The auth gate.** Every view below calls `require_roles(request,
# auth_service, "admin")` directly (`core.security.auth`, the vendored
# Django adapter — NOT `has_role("admin")`, `AdminPingView`'s own
# `permission_classes` gate) so the SAME call that enforces the role ALSO
# hands back the resolved `AccessClaims` — `claims.sub` is the acting
# admin's own id, needed here (unlike `AdminPingView`) as the audit actor
# and for the self-protection guard. `has_role`'s `BasePermission` has no
# way to hand its resolved claims back to the view body, so it would force
# a SECOND, redundant token verification (`resolve_principal`, decoding the
# JWT twice) to get the same information this single call already has —
# see `app/api/deps.py`'s `require_admin` (FastAPI) for the identical
# "gate + actor in one call" rationale. `permission_classes = [AllowAny]`/
# `authentication_classes = []` below matches `MeView`'s own posture for
# the same reason: this file enforces authentication/authorization itself.
# ---------------------------------------------------------------------------

# The allowed-role set `PUT /admin/users/{user_id}/roles` validates
# against — matches `app/api/routers/admin.py`'s own `_ALLOWED_ROLES`
# exactly (see that module's own comment for the "no mass-assignment"
# rationale).
_ALLOWED_ROLES: frozenset[str] = frozenset({"admin"})

_ALLOWED_STATUS_VALUES = frozenset({"active", "suspended", "banned"})

_ADMIN_NOT_FOUND_RESPONSE = {404: ErrorEnvelopeSerializer}
_ADMIN_CONFLICT_RESPONSE = {409: ErrorEnvelopeSerializer}
_ADMIN_VALIDATION_RESPONSE = {422: ErrorEnvelopeSerializer}
_ADMIN_AUTH_RESPONSES = {401: ErrorEnvelopeSerializer, 403: ErrorEnvelopeSerializer}


def _get_admin_user(user_id: str) -> User:
    """Looks up `User.objects` (soft-delete-scoped by `UserManager`'s own
    default, `core/models.py` — same scoping `AsyncRepository` applies on
    the FastAPI track) by a caller-supplied, NOT-YET-VALIDATED path
    segment. `<str:user_id>` (see `core/urls.py`), not Django's own
    `<uuid:...>` converter, is what routes a malformed UUID to THIS
    function rather than to Django's own unenveloped routing-level 404 —
    mirrors `ItemViewSet.get_object`'s identical "accept a plain string
    segment, validate by hand, raise the app's own NotFoundError" posture
    and its own docstring's rationale for why (keeps a malformed-id request
    inside DRF's exception-handler cycle, so it still renders THIS block's
    `ErrorEnvelope`, not Django's raw 404 page)."""
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        raise NotFoundError(f"User {user_id} was not found.") from None
    try:
        return User.objects.get(id=uid)
    except User.DoesNotExist:
        raise NotFoundError(f"User {user_id} was not found.") from None


def _ensure_not_self(claims, user: User, *, action: str) -> None:
    """Self-protection guard — identical rule to `app/api/routers/admin.py`'s
    `_ensure_not_self`: the acting admin can never `action` (ban/suspend/
    delete) their OWN account."""
    if str(user.id) == claims.sub:
        raise ConflictError(f"An admin cannot {action} their own account.")


def _ban_user(user: User) -> None:
    """THE ban action — extracted so `AdminUserBanView.post` (below) and
    Stage 13c's moderation `resolve` action (`AdminFlagResolveView`, this
    module's own moderation section) share ONE implementation rather than
    each hand-rolling the state-machine check + status write + session
    revocation. Byte-identical posture to `app/api/routers/admin.py`'s own
    `ban_user()` extraction on the FastAPI track — see that function's own
    docstring for why this deliberately does NOT perform the
    self-protection check or emit an audit event itself; both stay the
    CALLER's responsibility."""
    if user.status not in ("active", "suspended"):
        raise ConflictError(f"Cannot ban a user with status '{user.status}'.")
    user.status = "banned"
    user.save(update_fields=["status"])
    async_to_sync(DjangoRefreshTokenStore().revoke_all_for_user)(str(user.id))


class AdminUserListView(generics.ListAPIView):
    """`GET /admin/users` — paginated listing via the SAME `core.pagination.
    ContractPageNumberPagination` `GET /items` already uses (`config/
    settings.py`'s `DEFAULT_PAGINATION_CLASS`), auto-wrapped into the
    `{items, total, page, size, pages}` envelope by that class's own
    `get_paginated_response` — this view declares no pagination wiring of
    its own beyond `get_queryset`. `?q=` filters `email` case-insensitively
    (`icontains` — Postgres/sqlite both apply case-INsensitive `LIKE`
    semantics for ASCII by default; `?status=` filters to one exact status,
    rejecting an unrecognized value with `ValidationFailedError` (422) —
    matching `app/api/routers/admin.py`'s own `UserStatus` enum-typed query
    param, which pydantic/FastAPI reject the same way automatically."""

    serializer_class = AdminUserOutSerializer
    permission_classes = [AllowAny]
    authentication_classes: list = []
    # `.order_by(...)`: same "an unordered queryset paginates
    # non-deterministically" rationale `ItemViewSet.queryset`'s own comment
    # documents.
    queryset = User.objects.all().order_by("created_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        q = self.request.query_params.get("q")
        if q:
            queryset = queryset.filter(email__icontains=q)
        status_filter = self.request.query_params.get("status")
        if status_filter:
            if status_filter not in _ALLOWED_STATUS_VALUES:
                raise ValidationFailedError(
                    "Invalid status filter.",
                    details=[ErrorDetail(field="status", message=f"Unknown status: {status_filter!r}")],
                )
            queryset = queryset.filter(status=status_filter)
        return queryset

    @extend_schema(
        operation_id="list_admin_users_admin_users_get",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={200: AdminUserOutSerializer, **_ADMIN_AUTH_RESPONSES, **_ADMIN_VALIDATION_RESPONSE},
    )
    def get(self, request, *args, **kwargs):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        return super().get(request, *args, **kwargs)


@extend_schema_view(
    get=extend_schema(
        operation_id="get_admin_user_admin_users__user_id__get",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    ),
    delete=extend_schema(
        operation_id="delete_admin_user_admin_users__user_id__delete",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            204: None,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_CONFLICT_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    ),
)
class AdminUserDetailView(APIView):
    """`GET`/`DELETE /admin/users/{user_id}` — get and (soft-)delete one
    user, the DRF counterpart to `app/api/routers/admin.py`'s
    `get_admin_user`/`delete_admin_user`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request, user_id):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        return Response(AdminUserOutSerializer(user).data)

    def delete(self, request, user_id):
        """Soft-deletes via `User.mark_deleted()` (`core/models.py` — never
        a hard `DELETE`, matching `ItemViewSet.perform_destroy`'s identical
        posture for `Item`). Self-protection: the acting admin cannot
        delete their own account (409)."""
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        _ensure_not_self(claims, user, action="delete")
        user.mark_deleted()
        user.save(update_fields=["deleted_at"])
        audit_event(
            "admin.user.delete",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminUserSuspendView(APIView):
    """`POST /admin/users/{user_id}/suspend` — the DRF counterpart to
    `app/api/routers/admin.py`'s `suspend_admin_user`: valid only from
    `status == "active"` (409 otherwise), self-protection (409), and
    revokes every refresh token this user holds on success — see that
    handler's own docstring for the full rationale, identical here."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="suspend_admin_user_admin_users__user_id__suspend_post",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_CONFLICT_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, user_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        _ensure_not_self(claims, user, action="suspend")
        if user.status != "active":
            raise ConflictError(f"Cannot suspend a user with status '{user.status}'.")
        user.status = "suspended"
        user.save(update_fields=["status"])
        async_to_sync(DjangoRefreshTokenStore().revoke_all_for_user)(str(user.id))
        audit_event(
            "admin.user.suspend",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
            changed_fields=["status"],
        )
        return Response(AdminUserOutSerializer(user).data)


class AdminUserBanView(APIView):
    """`POST /admin/users/{user_id}/ban` — the DRF counterpart to `app/api/
    routers/admin.py`'s `ban_admin_user`: valid from `status in {"active",
    "suspended"}` (409 otherwise), self-protection (409), and revokes every
    refresh token this user holds on success."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="ban_admin_user_admin_users__user_id__ban_post",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_CONFLICT_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, user_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        _ensure_not_self(claims, user, action="ban")
        _ban_user(user)
        audit_event(
            "admin.user.ban",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
            changed_fields=["status"],
        )
        return Response(AdminUserOutSerializer(user).data)


class AdminUserReinstateView(APIView):
    """`POST /admin/users/{user_id}/reinstate` — the DRF counterpart to
    `app/api/routers/admin.py`'s `reinstate_admin_user`: valid from `status
    in {"suspended", "banned"}` (409 otherwise). No self-protection guard
    and no refresh-token action — see that handler's own docstring for why
    neither applies here."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="reinstate_admin_user_admin_users__user_id__reinstate_post",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_CONFLICT_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, user_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        if user.status not in ("suspended", "banned"):
            raise ConflictError(f"Cannot reinstate a user with status '{user.status}'.")
        user.status = "active"
        user.save(update_fields=["status"])
        audit_event(
            "admin.user.reinstate",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
            changed_fields=["status"],
        )
        return Response(AdminUserOutSerializer(user).data)


class AdminUserRolesView(APIView):
    """`PUT /admin/users/{user_id}/roles` — the DRF counterpart to `app/
    api/routers/admin.py`'s `set_admin_user_roles`: full-replace, every
    requested role validated against `_ALLOWED_ROLES` (422 for an unknown
    one — no mass-assignment), self-protection against the acting admin
    dropping their OWN `"admin"` role (409)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="set_admin_user_roles_admin_users__user_id__roles_put",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        request=AdminRolesInSerializer,
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_CONFLICT_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    )
    def put(self, request, user_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        serializer = AdminRolesInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = _get_admin_user(user_id)
        requested_roles = serializer.validated_data["roles"]
        unknown = sorted(set(requested_roles) - _ALLOWED_ROLES)
        if unknown:
            raise ValidationFailedError(
                "One or more requested roles is unknown.",
                details=[ErrorDetail(field="roles", message=f"Unknown role: {role!r}") for role in unknown],
            )
        deduped = sorted(set(requested_roles))
        if str(user.id) == claims.sub and "admin" not in deduped:
            raise ConflictError("An admin cannot remove their own admin role.")
        user.roles = deduped
        user.save(update_fields=["roles"])
        audit_event(
            "admin.user.roles_set",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
            changed_fields=["roles"],
        )
        return Response(AdminUserOutSerializer(user).data)


class AdminUserForceVerifyView(APIView):
    """`POST /admin/users/{user_id}/force-verify` — the DRF counterpart to
    `app/api/routers/admin.py`'s `force_verify_admin_user`: idempotent,
    sets `email_verified=True`/`verified_at=<now>` if not already
    verified."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="force_verify_admin_user_admin_users__user_id__force-verify_post",
        tags=["admin"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: AdminUserOutSerializer,
            **_ADMIN_AUTH_RESPONSES,
            **_ADMIN_NOT_FOUND_RESPONSE,
            **_ADMIN_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, user_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        user = _get_admin_user(user_id)
        if not user.email_verified:
            user.email_verified = True
            user.verified_at = utc_now()
            user.save(update_fields=["email_verified", "verified_at"])
        audit_event(
            "admin.user.force_verify",
            actor=claims.sub,
            resource=f"user:{user.id}",
            outcome="success",
            changed_fields=["email_verified"],
        )
        return Response(AdminUserOutSerializer(user).data)


# ---------------------------------------------------------------------------
# Stage 13d: blog/CMS admin surface -- the DRF counterpart to `app/api/
# routers/blog.py`'s own admin blog surface. See that module's docstring
# for the full design this mirrors handler-for-handler (the stored-XSS
# write-path boundary, slug resolution, publish/unpublish state machine,
# audit events, the shared per-route rate limit). Same auth-gate posture
# as the Stage 13b admin user-management views immediately above: every
# view calls `require_roles(request, auth_service, "admin")` directly
# (never `has_role("admin")`) so the SAME call yields the resolved
# `AccessClaims` this router needs as the audit actor.
# ---------------------------------------------------------------------------

_BLOG_NOT_FOUND_RESPONSE = {404: ErrorEnvelopeSerializer}
_BLOG_CONFLICT_RESPONSE = {409: ErrorEnvelopeSerializer}
_BLOG_VALIDATION_RESPONSE = {422: ErrorEnvelopeSerializer}
_BLOG_AUTH_RESPONSES = {401: ErrorEnvelopeSerializer, 403: ErrorEnvelopeSerializer}

_ALLOWED_BLOG_POST_STATUS_VALUES = frozenset({"draft", "published"})
_ALLOWED_COMMENT_STATUS_VALUES = frozenset({"visible", "hidden", "pending"})


def _slugify(title: str) -> str:
    """Byte-identical algorithm to `app/schemas/blog.py`'s `slugify` --
    see that function's own docstring."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
    return slug or "post"


def _slug_taken(slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
    """`True` iff a NOT-soft-deleted `BlogPost` other than `exclude_id`
    already owns `slug` -- same read-then-write uniqueness check
    `app/api/routers/blog.py`'s `_slug_taken` documents (including the
    accepted, DB-UNIQUE-index-backstopped race)."""
    queryset = BlogPost.objects.filter(slug=slug)
    if exclude_id is not None:
        queryset = queryset.exclude(id=exclude_id)
    return queryset.exists()


def _unique_slug(base_slug: str) -> str:
    """Disambiguates a DERIVED slug -- byte-identical algorithm to
    `app/api/routers/blog.py`'s `_unique_slug`."""
    candidate = base_slug
    suffix = 2
    while _slug_taken(candidate):
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


def _get_admin_blog_post(post_id: str) -> BlogPost:
    """`<str:post_id>`, NOT Django's `<uuid:...>` converter -- same
    "accept a plain string segment, validate by hand, raise the app's own
    NotFoundError" posture `_get_admin_user`'s own docstring documents,
    applied here to `BlogPost`."""
    try:
        uid = uuid.UUID(post_id)
    except (ValueError, TypeError):
        raise NotFoundError(f"Blog post {post_id} was not found.") from None
    try:
        return BlogPost.objects.get(id=uid)
    except BlogPost.DoesNotExist:
        raise NotFoundError(f"Blog post {post_id} was not found.") from None


def _get_admin_comment(comment_id: str) -> Comment:
    try:
        uid = uuid.UUID(comment_id)
    except (ValueError, TypeError):
        raise NotFoundError(f"Comment {comment_id} was not found.") from None
    try:
        return Comment.objects.get(id=uid)
    except Comment.DoesNotExist:
        raise NotFoundError(f"Comment {comment_id} was not found.") from None


class AdminBlogPostListCreateView(generics.ListAPIView):
    """`GET`/`POST /admin/blog/posts` -- list (paginated via the SAME
    `ContractPageNumberPagination` `GET /items`/`GET /admin/users` already
    use -- inherited from `DEFAULT_PAGINATION_CLASS`, `config/settings.py`)
    and create, the DRF counterpart to `app/api/routers/blog.py`'s
    `list_admin_blog_posts`/`create_admin_blog_post`. `generics.
    ListAPIView` (not a plain `APIView`, unlike this router's other
    single-object views) is what makes drf-spectacular auto-wrap `GET`'s
    documented 200 response in the `{items, total, page, size, pages}`
    pagination envelope -- the SAME reason `AdminUserListView` (Stage 13b)
    is built the identical way; a plain `APIView` manually calling
    `ContractPageNumberPagination` (as this view's own first draft did)
    produces a WORKING response at runtime but an UN-wrapped, wrong
    documented schema, since drf-spectacular's pagination introspection
    keys off the view class, not the runtime call. `post()` (create) is
    a plain additional method on this same class -- `ListAPIView` defines
    no `post` of its own, so no override conflict."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = BlogPostSummaryOutSerializer
    # `.order_by(...)`: same "an unordered queryset paginates
    # non-deterministically" rationale `ItemViewSet.queryset`'s/
    # `AdminUserListView.queryset`'s own comment documents.
    queryset = BlogPost.objects.all().order_by("created_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter:
            if status_filter not in _ALLOWED_BLOG_POST_STATUS_VALUES:
                raise ValidationFailedError(
                    "Invalid status filter.",
                    details=[ErrorDetail(field="status", message=f"Unknown status: {status_filter!r}")],
                )
            queryset = queryset.filter(status=status_filter)
        return queryset

    @extend_schema(
        operation_id="list_admin_blog_posts_admin_blog_posts_get",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={200: BlogPostSummaryOutSerializer, **_BLOG_AUTH_RESPONSES, **_BLOG_VALIDATION_RESPONSE},
    )
    def get(self, request, *args, **kwargs):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        return super().get(request, *args, **kwargs)

    @extend_schema(
        operation_id="create_admin_blog_post_admin_blog_posts_post",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        request=BlogPostCreateSerializer,
        responses={
            201: BlogPostOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_CONFLICT_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, *args, **kwargs):
        """**THE stored-XSS write-path boundary, half 1 of 2** -- see
        `app/api/routers/blog.py`'s `create_admin_blog_post` docstring for
        the full rationale this mirrors line-for-line."""
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)

        serializer = BlogPostCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        if data.get("slug"):
            if _slug_taken(data["slug"]):
                raise ConflictError(f"Slug '{data['slug']}' is already in use.")
            slug = data["slug"]
        else:
            slug = _unique_slug(_slugify(data["title"]))

        sanitized_html = sanitize_blog_html(data["body_html"])

        post = BlogPost.objects.create(
            slug=slug,
            title=data["title"],
            body_json=data["body_json"],
            body_html=sanitized_html,
            author_id=uuid.UUID(claims.sub),
        )
        audit_event(
            "admin.blog.create",
            actor=claims.sub,
            resource=f"blog_post:{post.id}",
            outcome="success",
        )
        return Response(BlogPostOutSerializer(post).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    get=extend_schema(
        operation_id="get_admin_blog_post_admin_blog_posts__post_id__get",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: BlogPostOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    ),
    patch=extend_schema(
        operation_id="update_admin_blog_post_admin_blog_posts__post_id__patch",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        request=BlogPostUpdateSerializer,
        responses={
            200: BlogPostOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_CONFLICT_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    ),
    delete=extend_schema(
        operation_id="delete_admin_blog_post_admin_blog_posts__post_id__delete",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            204: None,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    ),
)
class AdminBlogPostDetailView(APIView):
    """`GET`/`PATCH`/`DELETE /admin/blog/posts/{post_id}` -- get, update
    (re-sanitizing any supplied `body_html`), and (soft-)delete one post."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    def get(self, request, post_id):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        post = _get_admin_blog_post(post_id)
        return Response(BlogPostOutSerializer(post).data)

    def patch(self, request, post_id):
        """**THE stored-XSS write-path boundary, half 2 of 2.** Only
        explicitly-set fields are applied (`partial=True`) -- an explicit
        `null` for any of these four NOT-NULL columns is rejected at 422
        by `BlogPostUpdateSerializer.validate()`, so `serializer.
        validated_data` only ever contains fields the caller genuinely
        wants changed, to a genuinely non-null value -- see that
        serializer's own docstring."""
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        post = _get_admin_blog_post(post_id)

        serializer = BlogPostUpdateSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        updates = serializer.validated_data

        if "slug" in updates and _slug_taken(updates["slug"], exclude_id=post.id):
            raise ConflictError(f"Slug '{updates['slug']}' is already in use.")
        if "body_html" in updates:
            updates["body_html"] = sanitize_blog_html(updates["body_html"])

        for field, value in updates.items():
            setattr(post, field, value)
        if updates:
            post.save(update_fields=list(updates.keys()))
        audit_event(
            "admin.blog.update",
            actor=claims.sub,
            resource=f"blog_post:{post.id}",
            outcome="success",
            changed_fields=sorted(updates.keys()),
        )
        return Response(BlogPostOutSerializer(post).data)

    def delete(self, request, post_id):
        """Soft-deletes via `BlogPost.mark_deleted()` -- never a hard
        `DELETE`, same posture `ItemViewSet.perform_destroy`/
        `AdminUserDetailView.delete` already document."""
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        post = _get_admin_blog_post(post_id)
        post.mark_deleted()
        post.save(update_fields=["deleted_at"])
        audit_event(
            "admin.blog.delete",
            actor=claims.sub,
            resource=f"blog_post:{post_id}",
            outcome="success",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


class AdminBlogPostPublishView(APIView):
    """`POST /admin/blog/posts/{post_id}/publish` -- valid only from
    `status == "draft"` (409 otherwise), matching `app/api/routers/
    blog.py`'s `publish_admin_blog_post`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="publish_admin_blog_post_admin_blog_posts__post_id__publish_post",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: BlogPostOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_CONFLICT_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, post_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        post = _get_admin_blog_post(post_id)
        if post.status != "draft":
            raise ConflictError(f"Cannot publish a post with status '{post.status}'.")
        post.status = "published"
        post.published_at = utc_now()
        post.save(update_fields=["status", "published_at"])
        audit_event(
            "admin.blog.publish",
            actor=claims.sub,
            resource=f"blog_post:{post.id}",
            outcome="success",
            changed_fields=["status", "published_at"],
        )
        return Response(BlogPostOutSerializer(post).data)


class AdminBlogPostUnpublishView(APIView):
    """`POST /admin/blog/posts/{post_id}/unpublish` -- valid only from
    `status == "published"` (409 otherwise); reverts fully to draft
    (`status="draft"`, `published_at=None`), matching `app/api/routers/
    blog.py`'s `unpublish_admin_blog_post`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="unpublish_admin_blog_post_admin_blog_posts__post_id__unpublish_post",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: BlogPostOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_CONFLICT_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, post_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        post = _get_admin_blog_post(post_id)
        if post.status != "published":
            raise ConflictError(f"Cannot unpublish a post with status '{post.status}'.")
        post.status = "draft"
        post.published_at = None
        post.save(update_fields=["status", "published_at"])
        audit_event(
            "admin.blog.unpublish",
            actor=claims.sub,
            resource=f"blog_post:{post.id}",
            outcome="success",
            changed_fields=["status", "published_at"],
        )
        return Response(BlogPostOutSerializer(post).data)


class AdminBlogCommentListView(generics.ListAPIView):
    """`GET /admin/blog/comments` -- `?status=`/`?post_id=` filters, both
    optional and composable, matching `app/api/routers/blog.py`'s
    `list_admin_blog_comments`. No public create endpoint in this stage --
    see `core/models.py`'s `Comment` docstring. `generics.ListAPIView`,
    same pagination-schema rationale `AdminBlogPostListCreateView`'s own
    docstring documents."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = CommentOutSerializer
    queryset = Comment.objects.all().order_by("created_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter:
            if status_filter not in _ALLOWED_COMMENT_STATUS_VALUES:
                raise ValidationFailedError(
                    "Invalid status filter.",
                    details=[ErrorDetail(field="status", message=f"Unknown status: {status_filter!r}")],
                )
            queryset = queryset.filter(status=status_filter)
        post_id_filter = self.request.query_params.get("post_id")
        if post_id_filter:
            try:
                post_uuid = uuid.UUID(post_id_filter)
            except (ValueError, TypeError):
                raise ValidationFailedError(
                    "Invalid post_id filter.",
                    details=[ErrorDetail(field="post_id", message="Must be a valid UUID.")],
                ) from None
            queryset = queryset.filter(post_id=post_uuid)
        return queryset

    @extend_schema(
        operation_id="list_admin_blog_comments_admin_blog_comments_get",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={200: CommentOutSerializer, **_BLOG_AUTH_RESPONSES, **_BLOG_VALIDATION_RESPONSE},
    )
    def get(self, request, *args, **kwargs):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        return super().get(request, *args, **kwargs)


class AdminBlogCommentHideView(APIView):
    """`POST /admin/blog/comments/{comment_id}/hide` -- valid from
    `status in {"visible", "pending"}` (409 for an already-hidden
    comment), matching `app/api/routers/blog.py`'s
    `hide_admin_blog_comment`. Lightweight moderation-ADJACENT action, NOT
    the Stage 13c Flag/Report surface (out of scope here)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="hide_admin_blog_comment_admin_blog_comments__comment_id__hide_post",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: CommentOutSerializer,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_CONFLICT_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, comment_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        comment = _get_admin_comment(comment_id)
        if comment.status == "hidden":
            raise ConflictError(f"Cannot hide a comment with status '{comment.status}'.")
        comment.status = "hidden"
        comment.save(update_fields=["status"])
        audit_event(
            "admin.comment.hide",
            actor=claims.sub,
            resource=f"blog_comment:{comment.id}",
            outcome="success",
            changed_fields=["status"],
        )
        return Response(CommentOutSerializer(comment).data)


class AdminBlogCommentDeleteView(APIView):
    """`DELETE /admin/blog/comments/{comment_id}` -- soft-deletes via
    `Comment.mark_deleted()`, matching `app/api/routers/blog.py`'s
    `delete_admin_blog_comment`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="delete_admin_blog_comment_admin_blog_comments__comment_id__delete",
        tags=["blog"],
        auth=[{"HTTPBearer": []}],
        responses={
            204: None,
            **_BLOG_AUTH_RESPONSES,
            **_BLOG_NOT_FOUND_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def delete(self, request, comment_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        comment = _get_admin_comment(comment_id)
        comment.mark_deleted()
        comment.save(update_fields=["deleted_at"])
        audit_event(
            "admin.comment.delete",
            actor=claims.sub,
            resource=f"blog_comment:{comment_id}",
            outcome="success",
        )
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Stage 13d public read (issue #54, the deferred acceptance item) -- the
# PUBLIC, unauthenticated blog read surface. The DRF counterpart to
# `app/api/routers/blog_public.py` -- see that module's own docstring for
# the full design this mirrors handler-for-handler: published + not
# soft-deleted + `published_at <= now()` only, NEVER `body_json`, a draft/
# soft-deleted post's slug 404s identically to an unknown one, the general
# per-IP rate-limiting middleware only (no `enforce_admin_rate_limit`
# call -- this is deliberately NOT the privileged admin bucket).
# ---------------------------------------------------------------------------

_PUBLIC_BLOG_POST_NOT_FOUND_RESPONSE = {404: ErrorEnvelopeSerializer}


def _public_blog_post_queryset():
    """The ONE visibility predicate both public blog views share --
    byte-identical filters to `app/api/routers/blog_public.py`'s own
    `_visible_filters()` (published, not soft-deleted -- via `BlogPost.
    objects`'s own soft-delete-scoped default manager, `core/models.py` --
    and `published_at` set and not in the future). A fresh queryset per
    call (not a class-level `queryset = ...` constant) since `published_at
    __lte=timezone.now()` has to be evaluated at REQUEST time, not once at
    class-definition/import time."""
    return BlogPost.objects.filter(
        status="published",
        published_at__isnull=False,
        published_at__lte=timezone.now(),
    ).order_by("-published_at", "-id")


def _to_public_summary_dict(post: BlogPost) -> dict:
    """Explicit field-by-field mapping, NOT
    `PublicBlogPostSummaryOutSerializer(post)` -- `excerpt` (`core/
    serializers.py`'s `derive_excerpt`) has no matching `BlogPost`
    attribute for DRF to read off the ORM row directly, same reason
    `app/api/routers/blog_public.py`'s own `_to_public_summary_out`
    constructs its schema by hand rather than via `from_attributes`."""
    return {
        "id": post.id,
        "title": post.title,
        "slug": post.slug,
        "excerpt": derive_excerpt(post.body_html),
        "published_at": post.published_at,
        "author_id": post.author_id,
        "created_at": post.created_at,
    }


class PublicBlogPostListView(generics.ListAPIView):
    """`GET /blog/posts` -- the DRF counterpart to `app/api/routers/
    blog_public.py`'s `list_public_blog_posts`. Newest-first by
    `published_at` (`_public_blog_post_queryset`'s own `.order_by(
    "-published_at", "-id")` -- `-id` as a stable tiebreaker for two posts
    published at the identical instant, same "deterministic pagination"
    rationale `ItemViewSet.queryset`'s own comment documents).

    `list()` is fully overridden (not left to `ListModelMixin`'s default),
    the SAME reason `_to_public_summary_dict`'s own docstring gives: this
    serializer's `excerpt` field has no ORM attribute of its own to read.
    Paginated via the SAME `ContractPageNumberPagination`
    (`DEFAULT_PAGINATION_CLASS`, `config/settings.py`) every other list
    endpoint in this block uses -- no separate, looser `?size=` cap for
    this public surface."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = PublicBlogPostSummaryOutSerializer

    def get_queryset(self):
        return _public_blog_post_queryset()

    def list(self, request, *args, **kwargs):
        queryset = self.filter_queryset(self.get_queryset())
        page = self.paginate_queryset(queryset)
        rows = page if page is not None else queryset
        serializer = self.get_serializer([_to_public_summary_dict(post) for post in rows], many=True)
        if page is not None:
            return self.get_paginated_response(serializer.data)
        return Response(serializer.data)

    @extend_schema(
        operation_id="list_public_blog_posts_blog_posts_get",
        tags=["blog"],
        responses={200: PublicBlogPostSummaryOutSerializer, **_BLOG_VALIDATION_RESPONSE},
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)


class PublicBlogPostDetailView(APIView):
    """`GET /blog/posts/{slug}` -- the DRF counterpart to `app/api/routers/
    blog_public.py`'s `get_public_blog_post`. A draft or soft-deleted
    post's slug 404s IDENTICALLY to an unknown slug (`_public_blog_post_
    queryset()` already excludes both from the lookup -- there is no
    separate "found but hidden" branch to accidentally leak a different
    message from) -- see that FastAPI view's own docstring, "no
    draft-existence oracle"."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="get_public_blog_post_blog_posts__slug__get",
        tags=["blog"],
        responses={
            200: PublicBlogPostOutSerializer,
            **_PUBLIC_BLOG_POST_NOT_FOUND_RESPONSE,
            **_BLOG_VALIDATION_RESPONSE,
        },
    )
    def get(self, request, slug):
        try:
            post = _public_blog_post_queryset().get(slug=slug)
        except BlogPost.DoesNotExist:
            raise NotFoundError(f"Blog post '{slug}' was not found.") from None
        data = {**_to_public_summary_dict(post), "body_html": post.body_html}
        return Response(PublicBlogPostOutSerializer(data).data)


# ---------------------------------------------------------------------------
# Stage 13c: moderation admin surface — the DRF counterpart to `app/api/
# routers/moderation.py`'s own moderation admin surface. See that module's
# docstring for the full design this mirrors handler-for-handler (the
# resolve state machine, the per-`target_type` action dispatch, audit
# events, the shared per-route rate limit). Same auth-gate posture as the
# Stage 13b/13d admin views above: every view calls `require_roles(request,
# auth_service, "admin")` directly so the SAME call yields the resolved
# `AccessClaims` this router needs as the audit actor and for the
# `ban_author` self-protection guard (`_ensure_not_self`, above).
#
# **Admin-only queue, no end-user write path.** There is no `POST /flags`
# view anywhere in this module — a consuming app writes `Flag` rows itself
# (via the ORM); this section only ships the admin list/get/resolve/
# dismiss surface over rows that already exist, byte-identical scope to
# `app/api/routers/moderation.py`'s own module docstring.
# ---------------------------------------------------------------------------

_FLAG_NOT_FOUND_RESPONSE = {404: ErrorEnvelopeSerializer}
_FLAG_CONFLICT_RESPONSE = {409: ErrorEnvelopeSerializer}
_FLAG_VALIDATION_RESPONSE = {422: ErrorEnvelopeSerializer}
_FLAG_AUTH_RESPONSES = {401: ErrorEnvelopeSerializer, 403: ErrorEnvelopeSerializer}

_ALLOWED_FLAG_STATUS_VALUES = frozenset({"open", "resolved", "dismissed"})
_ALLOWED_FLAG_TARGET_TYPE_VALUES = frozenset({"blog_post", "comment", "user"})


def _get_admin_flag(flag_id: str) -> Flag:
    """`<str:flag_id>`, NOT Django's `<uuid:...>` converter — same "accept
    a plain string segment, validate by hand, raise the app's own
    NotFoundError" posture `_get_admin_user`/`_get_admin_blog_post`'s own
    docstrings document, applied here to `Flag`."""
    try:
        uid = uuid.UUID(flag_id)
    except (ValueError, TypeError):
        raise NotFoundError(f"Flag {flag_id} was not found.") from None
    try:
        return Flag.objects.get(id=uid)
    except Flag.DoesNotExist:
        raise NotFoundError(f"Flag {flag_id} was not found.") from None


def _resolve_flag_author(flag: Flag) -> User:
    """`ban_author`'s target resolution — byte-identical dispatch to
    `app/api/routers/moderation.py`'s `_resolve_author`: for a `blog_post`/
    `comment` target, resolves the author via that row's `author_id` (a
    `comment` with a NULL `author_id` has no author to ban — 404); for a
    `user` target, the target IS the author. Raises `NotFoundError` (404)
    for a missing target row, a target row with no author at all, or an
    author whose `User` row is itself missing/soft-deleted."""
    if flag.target_type == "user":
        try:
            return User.objects.get(id=flag.target_id)
        except User.DoesNotExist:
            raise NotFoundError(f"User {flag.target_id} was not found.") from None
    if flag.target_type == "blog_post":
        try:
            post = BlogPost.objects.get(id=flag.target_id)
        except BlogPost.DoesNotExist:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.") from None
        try:
            return User.objects.get(id=post.author_id)
        except User.DoesNotExist:
            raise NotFoundError(f"User {post.author_id} was not found.") from None
    if flag.target_type == "comment":
        try:
            comment = Comment.objects.get(id=flag.target_id)
        except Comment.DoesNotExist:
            raise NotFoundError(f"Comment {flag.target_id} was not found.") from None
        if comment.author_id is None:
            raise NotFoundError(f"Comment {flag.target_id} has no author to ban.")
        try:
            return User.objects.get(id=comment.author_id)
        except User.DoesNotExist:
            raise NotFoundError(f"User {comment.author_id} was not found.") from None
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


def _hide_flag_content(flag: Flag) -> None:
    if flag.target_type == "comment":
        try:
            comment = Comment.objects.get(id=flag.target_id)
        except Comment.DoesNotExist:
            raise NotFoundError(f"Comment {flag.target_id} was not found.") from None
        comment.status = "hidden"
        comment.save(update_fields=["status"])
        return
    if flag.target_type == "blog_post":
        try:
            post = BlogPost.objects.get(id=flag.target_id)
        except BlogPost.DoesNotExist:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.") from None
        post.status = "draft"
        post.published_at = None
        post.save(update_fields=["status", "published_at"])
        return
    if flag.target_type == "user":
        raise ValidationFailedError("hide_content is not a valid action for a 'user' target.")
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


def _delete_flag_content(flag: Flag) -> None:
    if flag.target_type == "comment":
        try:
            comment = Comment.objects.get(id=flag.target_id)
        except Comment.DoesNotExist:
            raise NotFoundError(f"Comment {flag.target_id} was not found.") from None
        comment.mark_deleted()
        comment.save(update_fields=["deleted_at"])
        return
    if flag.target_type == "blog_post":
        try:
            post = BlogPost.objects.get(id=flag.target_id)
        except BlogPost.DoesNotExist:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.") from None
        post.mark_deleted()
        post.save(update_fields=["deleted_at"])
        return
    if flag.target_type == "user":
        raise ValidationFailedError(
            "delete_content is not a valid action for a 'user' target; use ban_author instead."
        )
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


class AdminFlagListView(generics.ListAPIView):
    """`GET /admin/flags` — paginated listing via the SAME
    `ContractPageNumberPagination` every other admin list view uses.
    `?status=`/`?target_type=` each filter to one exact value, composable
    — matching `app/api/routers/moderation.py`'s `list_admin_flags`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []
    serializer_class = FlagOutSerializer
    queryset = Flag.objects.all().order_by("created_at", "id")

    def get_queryset(self):
        queryset = super().get_queryset()
        status_filter = self.request.query_params.get("status")
        if status_filter:
            if status_filter not in _ALLOWED_FLAG_STATUS_VALUES:
                raise ValidationFailedError(
                    "Invalid status filter.",
                    details=[ErrorDetail(field="status", message=f"Unknown status: {status_filter!r}")],
                )
            queryset = queryset.filter(status=status_filter)
        target_type_filter = self.request.query_params.get("target_type")
        if target_type_filter:
            if target_type_filter not in _ALLOWED_FLAG_TARGET_TYPE_VALUES:
                raise ValidationFailedError(
                    "Invalid target_type filter.",
                    details=[
                        ErrorDetail(field="target_type", message=f"Unknown target_type: {target_type_filter!r}")
                    ],
                )
            queryset = queryset.filter(target_type=target_type_filter)
        return queryset

    @extend_schema(
        operation_id="list_admin_flags_admin_flags_get",
        tags=["moderation"],
        auth=[{"HTTPBearer": []}],
        responses={200: FlagOutSerializer, **_FLAG_AUTH_RESPONSES, **_FLAG_VALIDATION_RESPONSE},
    )
    def get(self, request, *args, **kwargs):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        return super().get(request, *args, **kwargs)


class AdminFlagDetailView(APIView):
    """`GET /admin/flags/{flag_id}` — get one flag, the DRF counterpart to
    `app/api/routers/moderation.py`'s `get_admin_flag`."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="get_admin_flag_admin_flags__flag_id__get",
        tags=["moderation"],
        auth=[{"HTTPBearer": []}],
        responses={
            200: FlagOutSerializer,
            **_FLAG_AUTH_RESPONSES,
            **_FLAG_NOT_FOUND_RESPONSE,
            **_FLAG_VALIDATION_RESPONSE,
        },
    )
    def get(self, request, flag_id):
        auth_service = build_auth_service()
        async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        flag = _get_admin_flag(flag_id)
        return Response(FlagOutSerializer(flag).data)


class AdminFlagResolveView(APIView):
    """`POST /admin/flags/{flag_id}/resolve` — the DRF counterpart to
    `app/api/routers/moderation.py`'s `resolve_admin_flag`: only an `open`
    flag can be resolved (409 otherwise, checked BEFORE any content/author
    side effect runs, so a 404/409/422 raised by that side effect leaves
    the flag untouched). Dispatches on `action`, per `flag.target_type` —
    see this module's own moderation-section docstring for the full
    per-action behavior. `ban_author` calls this module's own `_ban_user`
    (the SAME implementation `AdminUserBanView.post` uses, above) plus
    `_ensure_not_self` — an admin can never `ban_author` themselves, even
    indirectly via a flag whose target/author happens to be their own
    account (409)."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="resolve_admin_flag_admin_flags__flag_id__resolve_post",
        tags=["moderation"],
        auth=[{"HTTPBearer": []}],
        request=FlagResolveInSerializer,
        responses={
            200: FlagOutSerializer,
            **_FLAG_AUTH_RESPONSES,
            **_FLAG_NOT_FOUND_RESPONSE,
            **_FLAG_CONFLICT_RESPONSE,
            **_FLAG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, flag_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        flag = _get_admin_flag(flag_id)
        if flag.status != "open":
            raise ConflictError(f"Cannot resolve a flag with status '{flag.status}'.")

        serializer = FlagResolveInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        action = serializer.validated_data["action"]
        note = serializer.validated_data.get("note")

        audit_extra: dict = {}
        # `transaction.atomic()` around the mutation sequence (content/
        # author action + the final `flag.save()`) so a failure partway
        # through can't leave a half-applied state -- e.g. a banned author
        # whose flag is still `open`, or (were `flag.save()` to fail) a
        # user left banned with un-revoked sessions while the flag reports
        # otherwise. Unlike `stores.py`'s `DjangoRefreshTokenStore` (whose
        # own docstring locks OUT `transaction.atomic()` around its calls
        # so each `.aupdate()` stays independently durable), THIS wrap is
        # safe: `_ban_user`'s `async_to_sync(...revoke_all_for_user)` call
        # still runs Django's async ORM `.aupdate()` via `sync_to_async`
        # with its default `thread_sensitive=True`, which routes the write
        # back onto THIS thread rather than a separate one -- so it shares
        # this thread's connection/transaction state and participates in
        # the same atomic block instead of racing it on another
        # connection. Verified empirically (Stage 13c review fix): forcing
        # the trailing `flag.save()` to raise rolls the preceding
        # `_ban_user` write back too (see `tests/test_moderation.py`'s
        # `test_resolve_ban_author_rolls_back_the_ban_if_flag_save_fails`).
        with transaction.atomic():
            if action == "none":
                pass
            elif action == "hide_content":
                _hide_flag_content(flag)
            elif action == "delete_content":
                _delete_flag_content(flag)
            elif action == "ban_author":
                author = _resolve_flag_author(flag)
                _ensure_not_self(claims, author, action="ban")
                _ban_user(author)
                audit_extra["banned_user"] = str(author.id)
            else:  # pragma: no cover - unreachable, ChoiceField already 422s an unknown action
                raise ValidationFailedError(f"Unknown action '{action}'.")

            flag.status = "resolved"
            flag.resolved_by_id = uuid.UUID(claims.sub)
            flag.resolved_at = utc_now()
            flag.resolution_note = note
            flag.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note"])
        audit_event(
            "admin.flag.resolve",
            actor=claims.sub,
            resource=f"flag:{flag.id}",
            outcome="success",
            action_taken=action,
            target=f"{flag.target_type}:{flag.target_id}",
            **audit_extra,
        )
        return Response(FlagOutSerializer(flag).data)


class AdminFlagDismissView(APIView):
    """`POST /admin/flags/{flag_id}/dismiss` — the DRF counterpart to
    `app/api/routers/moderation.py`'s `dismiss_admin_flag`: only an `open`
    flag can be dismissed (409 otherwise). Never performs a content/author
    action — dismiss is "this report doesn't warrant action," not a
    moderation outcome."""

    permission_classes = [AllowAny]
    authentication_classes: list = []

    @extend_schema(
        operation_id="dismiss_admin_flag_admin_flags__flag_id__dismiss_post",
        tags=["moderation"],
        auth=[{"HTTPBearer": []}],
        request=FlagDismissInSerializer,
        responses={
            200: FlagOutSerializer,
            **_FLAG_AUTH_RESPONSES,
            **_FLAG_NOT_FOUND_RESPONSE,
            **_FLAG_CONFLICT_RESPONSE,
            **_FLAG_VALIDATION_RESPONSE,
        },
    )
    def post(self, request, flag_id):
        auth_service = build_auth_service()
        claims = async_to_sync(require_roles_either)(request, build_session_service(), auth_service, "admin")
        enforce_admin_rate_limit(request)
        flag = _get_admin_flag(flag_id)
        if flag.status != "open":
            raise ConflictError(f"Cannot dismiss a flag with status '{flag.status}'.")

        serializer = FlagDismissInSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        note = serializer.validated_data.get("note")

        flag.status = "dismissed"
        flag.resolved_by_id = uuid.UUID(claims.sub)
        flag.resolved_at = utc_now()
        flag.resolution_note = note
        flag.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note"])
        audit_event(
            "admin.flag.dismiss",
            actor=claims.sub,
            resource=f"flag:{flag.id}",
            outcome="success",
        )
        return Response(FlagOutSerializer(flag).data)
