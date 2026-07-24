"""`create_app()` factory: registers routers, registers the two exception
handlers the `error-envelope` contract requires (RequestValidationError ->
ErrorEnvelope 422; AppError hierarchy -> ErrorEnvelope with each
subclass's own status/code), configures OpenAPI metadata + the bearer
security scheme for the Stage 5 auth seam, and wires the Stage 3 Step 3b
(#26) security-composition middleware stack — see "Security composition"
below for the full, load-bearing order.

--- Security composition (Stage 3 #26, Step 3b) ------------------------
Four of the six vendored `app/core/security/` components are wired here as
middleware, plus this app's own `SessionCsrfMiddleware`; the other two
(secret_store, input_validation) are library code composed at the point of
use, not middleware (see app/core/config.py's
`jwt_signing_key` for secret_store's composition; app/schemas/item.py's
docstring for why input_validation's StrictModel isn't adopted there).
`webhook_signature` and `idempotency` (also in the component catalog) are
NOT vendored here at all -- they're payments-shaped concerns with no
consumer yet; the Stage 11 payments recipe vendors and wires them when
there's an actual webhook endpoint to protect.

**Middleware order is OUTERMOST -> INNERMOST, top-to-bottom in the prose
below -- but Starlette's `app.add_middleware()` PREPENDS to its internal
list and then builds the runtime stack by iterating that list in REVERSE
(confirmed against this project's pinned Starlette:
`Starlette.add_middleware` calls `self.user_middleware.insert(0, ...)`,
and `Starlette.build_middleware_stack` does
`for cls, args, kwargs in reversed(middleware): app = cls(app, ...)`).
The practical consequence: the LAST `add_middleware()`/`add_cors()`/
`add_security_headers()` call in `create_app()` below ends up OUTERMOST at
runtime. The calls in this factory are therefore ordered bottom-to-top
relative to the prose here -- read the code comments at each call site for
the "call N of 5" position.**

1. **security-headers (OUTERMOST of the `add_middleware()` stack).** Runs
   first on the way in and, more importantly, LAST on the way out -- it
   gets to set/overwrite headers on every response any of THIS app's own
   `add_middleware()` layers produce (rate-limiting's 429, CORS's preflight
   reply, a routed handler's normal response). Nothing downstream of it can
   suppress these headers by constructing its own response object, because
   this layer runs after all of them on the response path. One path sits
   OUTSIDE even this: a handler registered for the base `Exception` class
   (this app's catch-all 500) is pulled out by Starlette's own
   `build_middleware_stack()` and given to `ServerErrorMiddleware`, which
   that same method places outside every `add_middleware()` layer,
   including this one -- see `_make_unhandled_exception_handler`'s
   docstring below for the mechanics. That handler therefore stamps the
   same `SecurityHeadersPolicy` output (and the bound request id) onto the
   500 response itself, so "every response" stays true for that path too,
   just via a second, explicit call site rather than this middleware.
2. **request-id / audit binding.** Binds a per-request id (inbound
   `X-Request-ID` if shape-valid, else a fresh `uuid4` --
   audit_logging/middleware.py) into audit.py's contextvar BEFORE
   rate-limiting runs, so a rate-limit denial's own audit trail (today:
   none: `rate_limiting.fastapi.RateLimitMiddleware` doesn't call
   `audit_event()` itself -- a future stage that adds one gets the id
   automatically) and every other downstream `audit_event()` call in this
   request already carries it, without threading it through every call
   site by hand -- exactly the seam audit_logging/README.md's "Request-id
   binding (for Step 3 middleware)" section documents.
3. **session CSRF (`app/api/middleware/csrf.py`).** Enforces the
   double-submit check on every unsafe-method request that actually
   carries a `session_id` cookie -- the obligation session mode's `Path=/`
   cookie creates, since EVERY state-changing route (not just `/auth/*`)
   is then a CSRF target. Runs INSIDE request-id binding, so a 403 still
   carries a traceable request id, and OUTSIDE rate limiting, so a forged
   cross-site request cannot burn a victim's token bucket on its way to
   being rejected. See that module's own docstring for why this is
   middleware rather than a per-route call.
4. **rate-limiting.** General per-client-IP ceiling, applied to every
   request. Runs INSIDE the CSRF check above and OUTSIDE CORS (so an
   attacker can't burn through the rate-limit budget with cross-origin
   preflight `OPTIONS` requests that never even reach CORS's own allow/deny
   decision -- rate limiting sees and counts every request regardless of
   origin).
5. **CORS (INNERMOST of the five).** Closest to routing/exception
   handling. Deny-by-default: wired only when `cors_allowed_origins` is
   non-empty -- see the call site's own comment for why an empty list means
   "skip CORS entirely" rather than constructing a policy that would fail.
------------------------------------------------------------------------
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter

from app.api.middleware import SessionCsrfMiddleware
from app.api.routers import admin, auth, blog, blog_public, health, items, moderation
from app.core.config import Settings, get_settings
from app.core.db import configure_engine
from app.core.errors import AppError, ErrorBody, ErrorCode, ErrorDetail, ErrorEnvelope
from app.core.security.audit_logging import RequestIDMiddleware
from app.core.security.auth import AUTH_ERROR_HTTP, AuthError
from app.core.security.cors_lockdown import CORSPolicy, add_cors
from app.core.security.rate_limiting import InMemoryBucketStore, RateLimitMiddleware
from app.core.security.security_headers import SecurityHeadersPolicy, add_security_headers

APP_TITLE = "FastAPI block"
APP_VERSION = "0.1.0"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Configures the vendored async engine (app/core/db/session.py) from
    this project's settings at startup — the one required call per
    db-session/README.md's "One engine, configured once at startup".
    Deliberately reads `get_settings()` here, not at module import time, so
    a missing/invalid DATABASE_URL fails when the app actually starts
    serving traffic, not at import time in a context (a test collecting
    this module, a one-off script) that never intends to run the app."""
    settings = get_settings()
    configure_engine(settings.database_url)
    yield
    # --- TODO (out of Step 3b's scope) ------------------------------------
    # Graceful engine disposal (`await get_engine().dispose()`) still lands
    # here in a later step. Step 3b's security middleware (security-headers,
    # request-id/audit binding, rate-limiting, CORS — see create_app()) is
    # pure-ASGI/BaseHTTPMiddleware with no persistent resource of its own to
    # tear down (InMemoryBucketStore is a plain in-process dict, not a
    # connection), so there is nothing for it to add here.
    # -----------------------------------------------------------------------


def _validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Remaps FastAPI's native `RequestValidationError` (loc/msg/type list)
    into `ErrorEnvelope` at 422 — the exact remap error-envelope/README.md
    documents as Step 2's job (see "ONE error shape — including the native
    422" in that README)."""
    envelope = ErrorEnvelope(
        error=ErrorBody(
            code=ErrorCode.VALIDATION_FAILED,
            message="Request validation failed.",
            details=[
                ErrorDetail(field=".".join(str(p) for p in err["loc"]), message=err["msg"])
                for err in exc.errors()
            ],
        )
    )
    return JSONResponse(
        # HTTP_422_UNPROCESSABLE_ENTITY is deprecated in this pinned
        # Starlette line in favor of the RFC 9110 name below (same status
        # code, 422) — using the current constant avoids a
        # StarletteDeprecationWarning on every validation failure.
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content=envelope.model_dump(mode="json"),
    )


def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """Catches every `AppError` subclass (NotFoundError, ConflictError, ...)
    and renders `exc.to_envelope()` with `exc.status_code` — the
    per-subclass status/code table in error-envelope/README.md's "The
    exception hierarchy" section."""
    return JSONResponse(status_code=exc.status_code, content=exc.to_envelope().model_dump(mode="json"))


def _auth_error_handler(request: Request, exc: AuthError) -> JSONResponse:
    """Catches the vendored auth component's `AuthError` hierarchy
    (`InvalidCredentials`, `InvalidToken`, `TokenReused`,
    `EmailAlreadyExists` — plus that same component's `InsufficientRole`,
    which also subclasses `AuthError`) and renders THIS app's
    `ErrorEnvelope`, using the component's own `AUTH_ERROR_HTTP` table
    (exception type -> `(status_code, ErrorCode string value)`) to pick
    the status and code — see that table's own docstring in
    `app/core/security/auth/fastapi.py` for why it's keyed by STRING code
    values rather than importing this app's `ErrorCode` enum directly (the
    vendored component has zero `app.*` imports; this handler is the one
    place that bridges the two).

    Registered for `AuthError` itself, not each concrete subclass — FastAPI/
    Starlette's exception-handler lookup walks an exception's MRO, so one
    registration here catches every subclass, present or future, without
    this file needing to know their names. A subclass with no entry in
    `AUTH_ERROR_HTTP` (shouldn't happen — every concrete subclass this app
    can raise has one) still fails SAFELY closed, at 401
    `unauthenticated` — the same posture as an actually-invalid token,
    never a 500 that would leak "this specific auth exception type wasn't
    wired up" as an implementation detail.

    FIX (whole-PR review, Stage 5a): the 401 (`unauthenticated`) bucket
    emits a SINGLE fixed, generic client message, never `str(exc)` — see
    `_core.py`'s `TokenReused` docstring: "A client must not be able to
    distinguish 'reuse was detected and your whole session was killed'
    from 'this token was simply invalid' from the wire response alone."
    `_core.py` raises genuinely distinct messages within that same 401
    bucket (`TokenReused("...reuse detected -- the token family has been
    revoked.")` vs `InvalidToken("Refresh token has expired.")` vs
    `InvalidToken("Refresh token has been revoked.")`, etc.) — echoing
    `str(exc)` straight to the client, as this handler used to
    unconditionally do, would let an attacker replaying a stolen refresh
    token read "reuse detected" in the response body and confirm their
    token was burned, directly violating that contract. Every 401 auth
    failure (bad password, unknown/expired/revoked/malformed token, AND
    reuse) is therefore byte-identical on the wire. `conflict` (409,
    `EmailAlreadyExists`) and `permission_denied` (403, `InsufficientRole`)
    keep echoing `str(exc)` — neither carries a secret the way a
    refresh-token failure's exact cause does. `str(exc)` remains available
    server-side (it's still on `exc`) for logging/audit; this only changes
    what reaches the CLIENT."""
    status_code, code_str = AUTH_ERROR_HTTP.get(type(exc), (401, ErrorCode.UNAUTHENTICATED.value))
    code = ErrorCode(code_str)
    message = "Authentication failed." if code is ErrorCode.UNAUTHENTICATED else (str(exc) or "Authentication failed.")
    envelope = ErrorEnvelope(error=ErrorBody(code=code, message=message))
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


def _make_unhandled_exception_handler(
    security_headers_policy: SecurityHeadersPolicy,
) -> Callable[[Request, Exception], JSONResponse]:
    """Returns the catch-all `Exception` handler, closed over the SAME
    `SecurityHeadersPolicy` instance `create_app()` wires into
    `SecurityHeadersMiddleware` below — so a 500 gets the identical header
    set (including whatever `hsts_preload` this environment is configured
    with) a normal response would, not a second, drifted default.

    FIX (Stage 3 review, MEDIUM): a handler registered for the base
    `Exception` class is pulled out by Starlette's own
    `Starlette.build_middleware_stack()` (see `key in (500, Exception)`) and
    handed to `ServerErrorMiddleware`, which that same method places
    OUTERMOST — *outside every `add_middleware()` call this app makes*,
    including `security_headers`, `RequestIDMiddleware`, and
    `RateLimitMiddleware`. Concretely: `middleware = [ServerErrorMiddleware,
    *user_middleware, ExceptionMiddleware]`, and the runtime stack wraps
    from the router outward through `reversed(middleware)` — so
    `ServerErrorMiddleware` is the LAST thing built, i.e. the outermost ASGI
    app. An exception that reaches this handler has therefore already
    unwound past `SecurityHeadersMiddleware`'s own `send_wrapper` (see
    `security_headers/fastapi.py`) without it ever running — that
    middleware's "sets headers on every response, even a lower layer's own"
    guarantee (module docstring, point 1) does NOT hold for this one path
    unless this handler stamps the same headers itself. Same reasoning for
    `x-request-id`: `RequestIDMiddleware` sits inside `ServerErrorMiddleware`
    too, so its own `send_wrapper` never gets to set the header on this
    response either — but it DID already bind the id into
    `scope["state"]["request_id"]` before the downstream exception was
    raised (see `audit_logging/middleware.py`), so that value is read back
    here instead of re-derived."""

    def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """JUDGMENT CALL (not explicitly required by Step 2's task list,
        added for defense-in-depth): catches anything that reaches here
        having escaped both handlers above — a genuine bug, not a
        deliberate AppError raise. error-envelope/errors.py's own module
        docstring describes this exact case ("an unhandled bug ... the
        framework's generic 500 handler still catches, mapping to this same
        base's to_envelope()"), so this keeps that promise literally true
        instead of leaving FastAPI's raw default 500 (a bare traceback in
        dev, an unenveloped generic message in prod) as the one place the
        app's error contract doesn't hold. Per references/backend/
        fastapi.md's "Validation & error handling" ("Never leak stack
        traces... return a safe message"), the client sees only
        `AppError`'s default internal-error message — never `str(exc)`.
        The envelope body is unchanged by this function's header-stamping
        fix above."""
        response = JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=AppError().to_envelope().model_dump(mode="json"),
        )
        headers = security_headers_policy.build_headers(is_https=request.url.scheme == "https")
        for name, value in headers.items():
            response.headers[name] = value
        request_id = request.scope.get("state", {}).get("request_id")
        if request_id:
            response.headers["x-request-id"] = request_id
        return response

    return _unhandled_exception_handler


def _install_error_envelope_openapi(app: FastAPI) -> None:
    """JUDGMENT CALL (Stage 3 #26, Step 4 — surfaced by actually exporting
    the schema, see app/export_openapi.py): FastAPI's auto-generated
    OpenAPI schema documents every operation's 422 response using its OWN
    native `HTTPValidationError` model (the `{"detail": [{"loc", "msg",
    "type"}]}` shape) — but `_validation_exception_handler` above remaps
    every one of those at runtime into THIS app's `ErrorEnvelope` before it
    ever reaches a client (see that handler's docstring and error-envelope/
    errors.py's own "ONE error shape, not two"). Left alone, the exported
    schema — and therefore any client generated from it (packages/
    api-client's orval regen) — would type every 422 response as a shape
    this app never actually sends, silently reintroducing the exact
    two-shapes problem the error-envelope component exists to prevent.

    This monkey-patches `app.openapi()` (the standard FastAPI customization
    point — https://fastapi.tiangolo.com/how-to/extending-openapi/) to
    swap the 422 response schema from `HTTPValidationError` to
    `ErrorEnvelope` on every operation that has one, adds `ErrorEnvelope`
    (and its nested `ErrorBody`/`ErrorDetail`/`ErrorCode` defs) to
    `components/schemas` via `TypeAdapter(ErrorEnvelope).json_schema(...)`
    (resolves nested refs the same way FastAPI's own schema builder does),
    and drops `HTTPValidationError`/`ValidationError` from
    `components/schemas` once nothing references them anymore — leaving
    them in place would ship two competing shapes for the same status code
    in the schema, confusing rather than merely redundant. Per-route 404s
    (`NotFoundError`) are documented separately, at the call site
    (`responses={404: {"model": ErrorEnvelope, ...}}` in items.py) — this
    function only owns the 422 case because EVERY operation gets FastAPI's
    native 422 by default, making it the one response worth fixing
    centrally instead of per-route.

    Caches onto `app.openapi_schema` exactly like FastAPI's own default
    `.openapi()` does, so this still only computes the schema once."""

    def _custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            summary=app.summary,
            description=app.description,
            routes=app.routes,
        )
        schemas = schema.setdefault("components", {}).setdefault("schemas", {})

        envelope_schema = TypeAdapter(ErrorEnvelope).json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(envelope_schema.pop("$defs", {}))
        schemas["ErrorEnvelope"] = envelope_schema

        error_envelope_ref = {"$ref": "#/components/schemas/ErrorEnvelope"}
        for path_item in schema.get("paths", {}).values():
            for operation in path_item.values():
                responses = operation.get("responses", {})
                validation_response = responses.get("422")
                if validation_response is None:
                    continue
                validation_response["description"] = "Validation Error"
                for media_type in validation_response.get("content", {}).values():
                    media_type["schema"] = error_envelope_ref

        still_referenced = "HTTPValidationError" in json.dumps(schema.get("paths", {}))
        if not still_referenced:
            schemas.pop("HTTPValidationError", None)
            schemas.pop("ValidationError", None)

        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = _custom_openapi


def create_app(*, lifespan_ctx=lifespan, settings: Settings | None = None) -> FastAPI:
    """`lifespan_ctx` is overridable (defaults to the real `lifespan`
    above) purely so the hermetic test suite can substitute a lifespan that
    configures the engine against an already-shared in-memory sqlite
    connection instead of re-deriving DATABASE_URL from `Settings` — see
    tests/conftest.py. Never overridden outside tests.

    `settings` is overridable the same way, and for the same reason: the
    security-composition wiring below (rate limiting, CORS, security
    headers) needs a `Settings` instance at APP-CONSTRUCTION time now, not
    just inside `lifespan` at ASGI-startup time — see the module docstring's
    "Security composition" section. Defaults to `get_settings()` (the real,
    env-derived, `lru_cache`d instance) for every real boot, including this
    module's own `app = create_app()` below; the test suite passes an
    explicit `Settings(...)` instead, so a hermetic test app can configure a
    tiny `rate_limit_capacity` or a specific `cors_allowed_origins` without
    mutating process env vars (which would leak across tests) — see
    tests/conftest.py and tests/test_security_composition.py."""
    resolved_settings = settings if settings is not None else get_settings()

    # Constructed once, up front, so BOTH the SecurityHeadersMiddleware wired
    # near the bottom of this function AND the catch-all Exception handler
    # (registered next) stamp the IDENTICAL header set on every response,
    # including the one path (an unhandled 500) that middleware itself never
    # reaches — see _make_unhandled_exception_handler's docstring.
    security_headers_policy = SecurityHeadersPolicy(hsts_preload=resolved_settings.security_headers_hsts_preload)

    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        lifespan=lifespan_ctx,
    )

    # Stage 5a (#41): `app/api/deps.py:get_auth_service` needs the SAME
    # `resolved_settings` this factory call was actually given (its
    # `jwt_signing_key`/`jwt_issuer`/`jwt_*_ttl_seconds` fields) — NOT the
    # separate, process-wide `get_settings()` singleton, which the hermetic
    # test suite's `make_client` fixture deliberately never touches (see
    # that fixture's own docstring: bespoke `Settings(...)` instances are
    # passed straight to THIS function's `settings=` parameter specifically
    # so a test can configure e.g. `jwt_signing_key` without mutating env
    # vars that would leak across tests). Stashing it on `app.state` is the
    # standard FastAPI way to make a per-app value reachable from a request-
    # scoped dependency without threading it through every route's own
    # `Depends(...)` — see `get_auth_service`'s own docstring for the read
    # side.
    app.state.settings = resolved_settings

    app.include_router(health.router)
    app.include_router(items.router)
    app.include_router(auth.router)
    # Stage 5d (#46): the RBAC admin example -- see app/api/routers/admin.py's
    # own module docstring for what it demonstrates and why it needs no new
    # auth logic of its own.
    app.include_router(admin.router)
    # Stage 13d: the blog/CMS admin surface -- see app/api/routers/blog.py's
    # own module docstring; reuses admin.py's require_admin_rate_limit, so
    # it must be registered after admin.router is constructed (import-time
    # dependency, not a request-time ordering concern).
    app.include_router(blog.router)
    # Stage 13d (issue #54): the PUBLIC, unauthenticated blog read surface --
    # see app/api/routers/blog_public.py's own module docstring for the
    # visibility rule and why it deliberately does NOT reuse admin.py's
    # require_admin_rate_limit. No import-order dependency on admin.router
    # the way blog.router/moderation.router have (this module imports
    # nothing from admin.py) -- registered here, right after blog.router,
    # purely to keep every blog-tagged router adjacent in this list.
    app.include_router(blog_public.router)
    # Stage 13c: the moderation admin surface -- see app/api/routers/
    # moderation.py's own module docstring; reuses admin.py's
    # require_admin_rate_limit/ban_user/_ensure_not_self, so it must be
    # registered after admin.router is constructed, same ordering
    # constraint blog.router documents above.
    app.include_router(moderation.router)

    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(AppError, _app_error_handler)
    # Stage 5a (#41): the vendored auth component raises its OWN exception
    # hierarchy (AuthError, not AppError — see _core.py's module docstring
    # on why) — registered separately so those exceptions render this
    # app's identical ErrorEnvelope shape too. Order relative to the
    # AppError/Exception handlers above/below doesn't matter: Starlette
    # dispatches by walking the RAISED exception's own MRO against the
    # registered handler dict, not by registration order, and AuthError
    # shares no base class with AppError short of Exception itself.
    app.add_exception_handler(AuthError, _auth_error_handler)
    app.add_exception_handler(Exception, _make_unhandled_exception_handler(security_headers_policy))

    # Stage 3 Step 4 (#26): make the exported/served OpenAPI schema
    # describe the 422 responses this app actually sends (ErrorEnvelope),
    # not FastAPI's un-remapped native shape — see
    # _install_error_envelope_openapi's own docstring for why this matters
    # for packages/api-client's generated client.
    _install_error_envelope_openapi(app)

    # --- Security composition (Stage 3 #26, Step 3b) ----------------------
    # See the module docstring's "Security composition" section for the
    # full outermost->innermost rationale and the add_middleware()-prepends-
    # then-reverses derivation. Calls below are ordered call-1-first
    # (innermost of the four) to call-4-last (outermost) — the OPPOSITE of
    # the module docstring's outermost-to-innermost prose order, because
    # that's what actually produces that runtime order.

    # Call 1 of 5 (innermost): CORS. Deny-by-default: `CORSPolicy.__init__`
    # itself refuses to construct with an empty `allow_origins` (see
    # cors_lockdown/_core.py's InsecureCORSPolicyError) — there is no
    # "allow nothing" policy object to build. Treating "no origins
    # configured" as "add no CORSMiddleware at all" is the deny-by-default
    # equivalent without hitting that guard on every dev/test boot where
    # cors_allowed_origins is unset: with no CORSMiddleware in the stack, no
    # Access-Control-Allow-Origin header is ever sent, so a browser blocks
    # every cross-origin JS request against this app regardless — the same
    # practical outcome as an explicit empty-allowlist policy would give.
    #
    # Stage 5d (#46): web cookie mode needs the BROWSER to actually attach
    # credentials (the refresh + CSRF cookies) and the two extra headers it
    # sends (`X-CSRF-Token`, `X-Auth-Mode`) on a CROSS-ORIGIN request — none
    # of that works without `allow_credentials=True` and those headers in
    # `allow_headers`. Gated behind `resolved_settings.
    # auth_cookie_mode_enabled` (secure default `False` — see that field's
    # own docstring) AND `resolved_settings.cors_allowed_origins` being a
    # non-empty EXPLICIT allowlist: this is the SAME `if` this call already
    # guards on, so `allow_credentials=True` can never be constructed
    # without a real, explicit origin list behind it — never `*`, which
    # `CORSPolicy.__post_init__`'s `InsecureCORSPolicyError` guard already
    # forbids outright, credentials or not (see that guard's own docstring).
    # **Invariant, stated plainly: credentials require explicit origins.**
    # This flag only ever WIDENS what's already a validated, non-wildcard
    # allowlist — it can't be used to smuggle a wildcard-plus-credentials
    # configuration past that guard, because there is no path here that
    # skips constructing `CORSPolicy` in the first place.
    if resolved_settings.cors_allowed_origins:
        policy = CORSPolicy(allow_origins=tuple(resolved_settings.cors_allowed_origins))
        if resolved_settings.auth_cookie_mode_enabled:
            # Rebuilt (not mutated -- CORSPolicy is frozen) from the SAME
            # validated `allow_origins`, extending `policy.allow_headers`
            # (its own already-resolved default, `("Content-Type",
            # "Authorization")`, read off the instance above rather than
            # duplicated here as a literal that could silently drift from
            # `cors_lockdown/_core.py`'s own default) with the two headers
            # cookie mode's SPA sends cross-origin.
            policy = CORSPolicy(
                allow_origins=policy.allow_origins,
                allow_credentials=True,
                allow_headers=(*policy.allow_headers, "X-CSRF-Token", "X-Auth-Mode"),
            )
        add_cors(app, policy)

    # Call 2 of 5: rate limiting. One InMemoryBucketStore per app instance
    # (per-process, per rate_limiting/_core.py's own documented limitation —
    # see that file's "Judgment calls" for the multi-worker/multi-replica
    # caveat; Stage 11 swaps in a Redis-backed BucketStore for a true shared
    # ceiling). `trusted_hops` defaults to 0 (distrust X-Forwarded-For) —
    # see Settings.rate_limit_trusted_hops's own docstring for the exact,
    # per-environment opt-in this must never be guessed at. `max_keys=50_000`
    # is explicit, bounded defense-in-depth on top of the store's own
    # idle-eviction (`ttl_seconds`, default 900s): even under a sustained
    # flood of distinct keys (e.g. a high-cardinality spoofed-IP attack)
    # within one TTL window, the in-memory dict is capped rather than
    # growing unbounded — see InMemoryBucketStore's own docstring for the
    # oldest-by-last-seen eviction this triggers once the cap is hit.
    #
    # Issue #42: `exempt_paths` is left unpassed, so `RateLimitMiddleware`
    # resolves it to its own `_DEFAULT_EXEMPT_PATHS` (`{"/health",
    # "/readyz"}`) -- exactly this app's health.py routes (app/api/routers/
    # health.py: `GET /health`, `GET /readyz`). Without this, a
    # TLS-terminating proxy in front of this app at the safe default
    # `trusted_hops=0` above puts every request (including the proxy's own
    # health-check traffic) on ONE shared bucket keyed on the proxy's peer
    # address -- an LB polling /health under burst could 429 its own health
    # check and pull this instance from rotation, an outage the limiter
    # itself would have caused. See RateLimitMiddleware.exempt_paths'
    # docstring (rate_limiting/fastapi.py) for the full rationale and how to
    # opt back into rate-limiting these routes (`exempt_paths=frozenset()`).
    app.add_middleware(
        RateLimitMiddleware,
        store=InMemoryBucketStore(max_keys=50_000),
        capacity=resolved_settings.rate_limit_capacity,
        refill_per_second=resolved_settings.rate_limit_refill_per_second,
        trusted_hops=resolved_settings.rate_limit_trusted_hops,
    )

    # Call 3 of 5: session-mode CSRF. Added just OUTSIDE rate limiting and
    # just INSIDE request-id binding, deliberately:
    #
    # - Outside rate limiting means a request that fails the double-submit
    #   check is rejected 403 without consuming a token bucket. A forged
    #   cross-site request should not be able to exhaust a victim's rate
    #   limit as a side effect of being rejected.
    # - Inside request-id binding means a CSRF rejection still carries an
    #   `x-request-id` and still lands in the audit log with a correlatable
    #   id — a 403 nobody can trace back to a request is a 403 nobody can
    #   investigate.
    #
    # This is the enforcement point for the obligation session mode's
    # `Path=/` cookie creates: EVERY unsafe-method route is a CSRF target,
    # not just `/auth/*`. See app/api/middleware/csrf.py's own module
    # docstring for why that is a middleware rather than a per-route call,
    # and app/core/security/auth/_cookies.py's for the mechanism itself.
    app.add_middleware(SessionCsrfMiddleware)

    # Call 4 of 5: request-id / audit binding. See audit_logging/
    # middleware.py's own docstring for the binding mechanics and the
    # client-supplied-X-Request-ID trust posture.
    app.add_middleware(RequestIDMiddleware)

    # Call 5 of 5 (outermost): security headers. Added LAST so it wraps
    # every other middleware and the router — see the module docstring's
    # point 1 for why that ordering specifically matters (it must see, and
    # be able to overwrite headers on, every response any lower layer
    # produces, including a 429 or a CORS preflight reply).
    add_security_headers(app, policy=security_headers_policy)
    # ------------------------------------------------------------------------

    return app


app = create_app()
