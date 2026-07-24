"""This block's Django settings module. Not a vendored file — new glue,
env-driven per this block's composition contract (see README.md "Composition
contract"). Every value that varies per-environment is read from process env
via `os.environ`, matching `secrets-loading/secret_store.py`'s "process env
first" posture (`core/contract/secret_store.py`, vendored below) rather than
Django's own `django-environ`/python-decouple conventions — this block has no
dependency on either.

`DEFAULT_RENDERER_CLASSES` is pinned to JSON-only (references/backend/
drf.md's "Browsable API": drop `BrowsableAPIRenderer` in prod). `REST_FRAMEWORK`
also wires the custom `EXCEPTION_HANDLER` (`core/exceptions.py`) that maps
DRF's own exceptions onto `core.contract.errors.ErrorEnvelope`, and the
`DEFAULT_PAGINATION_CLASS` (`core/pagination.py`) that emits `core.contract.
pagination.Page`'s `{items, total, page, size, pages}` shape — Stage 4 Step 2
(#27) — see this block's README, "Conformance", for the full wire-identity
target these two seams complete.

Stage 4 Step 3 (#27): this module now also wires the security-composition
MIDDLEWARE stack — see "Security composition" below for the full,
load-bearing MIDDLEWARE order and its rationale."""

from __future__ import annotations

import os
from pathlib import Path

import dj_database_url

from core.security.cors_lockdown import CORS_MIDDLEWARE_CLASSPATH, CORSPolicy, cors_settings

BASE_DIR = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Core / security — every value env-driven, no hardcoded secret or default
# that would be unsafe left in place in production.
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


try:
    SECRET_KEY = os.environ["SECRET_KEY"]
except KeyError as exc:
    raise RuntimeError(
        "SECRET_KEY is required and must be set via the process environment "
        "(never hardcoded here) — see this block's README, 'Composition "
        "contract'. Generate one with: "
        "python -c \"from django.core.management.utils import "
        "get_random_secret_key; print(get_random_secret_key())\""
    ) from exc

# Default False: a missing/unset DEBUG env var must never silently enable
# debug mode in an environment that forgot to set it (the unsafe default
# would be `True`; this block deliberately inverts that).
DEBUG = _env_bool("DEBUG", False)

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()]


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    # No admin/sessions/messages/staticfiles: this is an API-only block (JSON
    # over DRF) with no server-rendered templates or admin site as of this
    # step — add them back if a materialized project needs the admin site.
    "rest_framework",
    # drf-spectacular (pyproject.toml, matrix-pinned) — the OpenAPI 3 schema
    # generator, wired via REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"] and
    # SPECTACULAR_SETTINGS below (Stage 4 Step 4, #27).
    "drf_spectacular",
    # cors-lockdown's REQUIRED_INSTALLED_APP (core/security/cors_lockdown/
    # django.py) — django-cors-headers' own app, needed for its CorsMiddleware
    # (MIDDLEWARE, below) to function. Matrix-pinned: references/
    # compatibility-matrix.md's "Backend — Django track", django-cors-headers row.
    "corsheaders",
    "core",
]

# ---------------------------------------------------------------------------
# Security composition (Stage 4 Step 3, #27). Four of the six vendored
# core/security/ components are wired here as MIDDLEWARE; the other two
# (secret_store, input_validation) are library code composed at the point of
# use, not middleware — see "Secrets" below for secret_store's composition
# (core/contract/secret_store.py, already vendored in Step 1 — reused here,
# not re-vendored, see "secret_store: one copy, not two" in README.md) and
# core/security/input_validation/__init__.py's docstring for why
# input_validation isn't wired as the DRF request boundary. `webhook_signature`
# and `idempotency` (also in the component catalog) are NOT vendored here at
# all -- payments-shaped concerns with no consumer yet; the Stage 11 payments
# recipe vendors and wires them when there's an actual webhook endpoint to
# protect (mirrors backend/fastapi's own "not vendored yet" posture for the
# same two components).
#
# **MIDDLEWARE order is OUTERMOST -> INNERMOST, TOP-TO-BOTTOM** for Django
# new-style middleware: `MIDDLEWARE[0]` wraps `MIDDLEWARE[1]` wraps ... wraps
# the view, so the request phase runs top-to-bottom (MIDDLEWARE[0] sees the
# request FIRST) and the response phase runs bottom-to-top (MIDDLEWARE[0]
# sees the response LAST, giving it the final word). This is the OPPOSITE
# mechanics of backend/fastapi's Starlette stack (that block's app.py docstring:
# `add_middleware()` prepends then the runtime stack is built by iterating in
# REVERSE, so the LAST `add_middleware()` call ends up outermost) — but the
# same top-of-list/last-call = OUTERMOST semantic falls out both ways, so the
# four components land in the SAME relative outermost-to-innermost order on
# both tracks, with one Django-specific exception (CORS) explained below.
#
# 1. **corsheaders.middleware.CorsMiddleware (OUTERMOST here — a genuine
#    divergence from backend/fastapi, where CORS is INNERMOST of the four).**
#    django-cors-headers' own docs require CorsMiddleware to run "as early as
#    possible" and specifically BEFORE `CommonMiddleware`
#    (core/security/cors_lockdown/django.py's module docstring): Django's
#    `CommonMiddleware` can issue a redirect (e.g. APPEND_SLASH) that returns
#    a response WITHOUT ever calling further into the wrapped middleware
#    chain — if CorsMiddleware were listed below (inside) CommonMiddleware,
#    that redirect response would never reach CorsMiddleware's own
#    response-phase header injection, silently breaking CORS on exactly the
#    requests that get redirected. Starlette has no equivalent "a middleware
#    can synchronously short-circuit before calling downstream" redirect
#    concern baked into CommonMiddleware's own contract, which is why
#    backend/fastapi's CORS sits innermost instead — a genuine Django-vs-
#    Starlette difference, not an inconsistency between the two tracks.
# 2. **security-headers.** Placed so it precedes (is outside) Django's own
#    `SecurityMiddleware` — see core/security/security_headers/django.py's
#    module docstring: Django's `SecurityMiddleware` already sets some of
#    this same header set (nosniff/HSTS/Referrer-Policy) under its own
#    SECURE_* settings; listing ours earlier in MIDDLEWARE means ours runs
#    LAST in the response phase and gets the final, authoritative word —
#    "component wins," backed up by this app's own Django SECURE_* settings
#    staying off (see "Transport security headers" below) so the two never
#    actually race in the first place.
# 3. **request-id / audit-bind (core/security/audit_logging/middleware.py,
#    NEW glue — not vendored, mirrors backend/fastapi's own audit-bind
#    middleware for this track).** Binds a per-request id (inbound
#    `X-Request-ID` if shape-valid, else a fresh `uuid4`) into audit.py's
#    contextvar BEFORE rate-limiting runs, so a rate-limit denial's own audit
#    trail (today: none — `rate_limiting.django.RateLimitMiddleware` doesn't
#    call `audit_event()` itself; a future stage that adds one gets the id
#    automatically) and every other downstream `audit_event()` call in this
#    request already carries it, without threading it through every call
#    site by hand.
# 4. **rate-limiting (INNERMOST of the four).** Pre-auth (this app has no
#    real authentication yet — Stage 5, #28 — so "pre-auth" and "for every
#    request" are the same thing today), general per-client-IP ceiling. Runs
#    inside request-id binding (so a 429 still carries the request id).
#
# Django's own `SecurityMiddleware`/`CommonMiddleware` are placed innermost
# of all six, below (inside) this stack — CORS still precedes CommonMiddleware
# per its own hard requirement (#1 above); security-headers still precedes
# SecurityMiddleware per its own hard requirement (#2 above); neither Django
# middleware has a documented ordering requirement relative to request-id/
# rate-limiting, so they sit closest to the view.
# ---------------------------------------------------------------------------

MIDDLEWARE = [
    CORS_MIDDLEWARE_CLASSPATH,  # "corsheaders.middleware.CorsMiddleware" — see note 1 above
    "core.security.security_headers.django.SecurityHeadersMiddleware",  # note 2
    "core.security.audit_logging.middleware.RequestIDMiddleware",  # note 3
    "core.security.rate_limiting.django.RateLimitMiddleware",  # note 4
    # Session-mode CSRF. Placed INSIDE rate limiting (so a forged
    # cross-site request cannot burn a victim's token bucket on its way to
    # being rejected 403) and INSIDE request-id binding (so a CSRF
    # rejection still carries a traceable x-request-id and lands in the
    # audit log correlatable). This is the enforcement point for the
    # obligation session mode's Path=/ cookie creates: EVERY unsafe-method
    # view is a CSRF target, not just /auth/*. See that module's own
    # docstring for why it is middleware rather than a per-view call, and
    # why it does not replace Django's own CsrfViewMiddleware.
    "core.security.auth.csrf_middleware.SessionCsrfMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"


# ---------------------------------------------------------------------------
# Database — Postgres via psycopg (Django 5.2 prefers psycopg 3; the
# `django.db.backends.postgresql` engine auto-detects whichever of
# psycopg2/psycopg is installed — see references/compatibility-matrix.md's
# "Backend — Django track" psycopg row). DATABASE_URL is required, no
# default: a missing value fails `Settings`/app boot immediately (mirrors
# backend/fastapi's `AppSettings.database_url` posture), not on the first
# request that touches the database.
# ---------------------------------------------------------------------------

if not os.environ.get("DATABASE_URL"):
    raise RuntimeError(
        "DATABASE_URL is required (a postgres:// URL in dev/prod) — see this "
        "block's README, 'Composition contract'. Hermetic checks/tests that "
        "need no real server use config.settings_test instead "
        "(DJANGO_SETTINGS_MODULE=config.settings_test)."
    )

DATABASES = {
    "default": dj_database_url.config(
        env="DATABASE_URL",
        conn_max_age=600,
        # #48, L2 -- cheap mitigation for intermittent "connection already
        # closed" errors under load: with CONN_MAX_AGE=600 (persistent
        # connections) AND async-ORM writes running in a thread-sensitive
        # executor thread (see core/security/auth/stores.py's own "Async
        # ORM only" module docstring), a pooled connection can go stale
        # (closed server-side, e.g. by a proxy/idle timeout) between
        # requests without Django noticing until the next query fails on
        # it. CONN_HEALTH_CHECKS=True makes Django run a cheap liveness
        # check (`SELECT 1`-equivalent) on a REUSED persistent connection
        # before handing it back out, transparently reconnecting if it's
        # gone stale, instead of surfacing that staleness as a query
        # failure. Auth CORRECTNESS was never at risk here (writes
        # autocommit — see DjangoRefreshTokenStore's own docstring) — this
        # is purely an availability/reliability hardening, the minimal safe
        # default for this block's persistent-connection posture. See
        # README's "Database & migrations" section for the heavier
        # CONN_MAX_AGE=0-behind-PgBouncer alternative for multi-worker
        # deploys under sustained heavy load.
        conn_health_checks=True,
        # Not forced True: a local/dev Postgres often has no TLS listener at
        # all. A production DATABASE_URL should append `?sslmode=require`
        # itself rather than have this block force it kit-wide.
        ssl_require=False,
    ),
}


# ---------------------------------------------------------------------------
# Password validation — Django defaults; this block has no custom user model
# or auth flow yet (out of scope for this step).
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internationalization
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ---------------------------------------------------------------------------
# Default primary key field type
# ---------------------------------------------------------------------------

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Transport security headers — NOW WIRED (Stage 4 Step 3, #27; previously
# deferred in Step 2). HSTS/nosniff/frame-options/referrer-policy/CSP/
# Permissions-Policy are set on every response by
# `core.security.security_headers.django.SecurityHeadersMiddleware`
# (MIDDLEWARE, above) — the same component backend/fastapi vendors and wires
# as ASGI middleware (that block's app/main.py "Security composition"
# docstring). Django's OWN settings-driven equivalents
# (SECURE_HSTS_SECONDS, SECURE_CONTENT_TYPE_NOSNIFF, SECURE_SSL_REDIRECT,
# SECURE_REFERRER_POLICY) are DELIBERATELY left at their off/unset defaults
# below — turning them on now would double-stamp the same headers from two
# uncoordinated sources (Django's own `SecurityMiddleware` AND this
# component's middleware), racing to set the same header with whichever
# runs last in the response phase silently winning. This component's
# middleware is placed BEFORE `django.middleware.security.SecurityMiddleware`
# in MIDDLEWARE specifically so it is authoritative even if a future edit
# reorders things — see core/security/security_headers/django.py's own
# module docstring and this file's "Security composition" note above.
SECURE_CONTENT_TYPE_NOSNIFF = False  # core.security.security_headers sets it instead
SECURE_HSTS_SECONDS = 0  # core.security.security_headers sets it instead (when request.is_secure())
SECURE_REFERRER_POLICY = None  # core.security.security_headers sets it instead
# SECURE_SSL_REDIRECT is left at Django's own default (False) — an HTTP->HTTPS
# redirect is a routing/proxy-layer concern (the TLS-terminating proxy/load
# balancer a real deployment sits behind), not something security-headers
# (which only sets response HEADERS, never redirects) claims either. See
# security_headers/django.py's "Deployment note: HSTS behind a TLS-
# terminating proxy" for the related SECURE_PROXY_SSL_HEADER prerequisite a
# real deployment behind such a proxy needs for is_https detection to work
# at all — not set here, since this block has no fixed proxy topology to
# assume.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# CORS — Stage 4 Step 3 (#27); widened for web cookie mode, Stage 5d (#46).
# Deny-by-default: the comma-separated `CORS_ALLOWED_ORIGINS` env var is
# empty unless a project sets it, and an empty tuple means NO cross-origin
# request is ever allowed — see `_cors_allowed_origins` below and
# core.security.cors_lockdown.CORSPolicy's own construction-time guard
# (raises InsecureCORSPolicyError on an empty or wildcard allowlist; a
# project that wants a public, unauthenticated, any-origin API doesn't
# construct a CORSPolicy at all, matching backend/fastapi's own "no
# CORSMiddleware at all" equivalent posture — see that block's app/main.py
# for the parallel comment). NEVER allow-all (`"*"`) combined with
# credentials — CORSPolicy's constructor makes that configuration
# impossible to construct in the first place, not just discouraged.
# ---------------------------------------------------------------------------


def _cors_allowed_origins() -> tuple[str, ...]:
    raw = os.environ.get("CORS_ALLOWED_ORIGINS", "")
    return tuple(origin.strip() for origin in raw.split(",") if origin.strip())


# Stage 5d (#46): gates whether the CORS policy below allows credentials
# (cookies) and the two extra request headers cookie mode's SPA sends
# cross-origin (`X-CSRF-Token`, `X-Auth-Mode`) — see the `if
# AUTH_COOKIE_MODE_ENABLED:` branch below for exactly what this widens, and
# `app/main.py`'s identically-gated CORS construction (`resolved_settings.
# auth_cookie_mode_enabled`) on the FastAPI track for the byte-for-byte same
# rationale this mirrors. SECURE DEFAULT: `False` — a bearer-only
# deployment stays credential-free at the CORS layer even though `core/
# views.py`'s `LoginView`/`RefreshView`/`LogoutView` already support an
# `X-Auth-Mode: cookie` caller unconditionally (see those views' own
# docstrings) — widening CORS is a SEPARATE, explicit opt-in a deployment
# makes only once it actually serves a browser SPA using cookie mode
# cross-origin; it is meaningless, and safe to leave off, for a mobile-only
# or same-origin deployment.
AUTH_COOKIE_MODE_ENABLED: bool = _env_bool("AUTH_COOKIE_MODE_ENABLED", False)

_cors_origins = _cors_allowed_origins()
if _cors_origins:
    _cors_policy = CORSPolicy(allow_origins=_cors_origins)
    if AUTH_COOKIE_MODE_ENABLED:
        # Rebuilt (not mutated -- CORSPolicy is frozen) from the SAME
        # validated `allow_origins`, extending `allow_headers` (the policy's
        # own already-resolved default, `("Content-Type", "Authorization")`,
        # read off the instance above rather than duplicated here as a
        # literal that could silently drift from `cors_lockdown/_core.py`'s
        # own default) with the two headers cookie mode's SPA sends
        # cross-origin. `cors_settings`/`to_django_cors_headers_settings`
        # (`core/security/cors_lockdown/_core.py`) lowercases every header
        # name for `CORS_ALLOW_HEADERS` (django-cors-headers' own
        # convention) -- `X-CSRF-Token`/`X-Auth-Mode` land as `x-csrf-token`/
        # `x-auth-mode`, matching what `core/security/auth/django.py`'s
        # `enforce_csrf`/`core/views.py`'s cookie-mode branches actually
        # read off `request.headers` (a case-insensitive mapping either
        # way).
        #
        # **Invariant, stated plainly: credentials require explicit
        # origins.** This flag only ever WIDENS what's already a validated,
        # non-wildcard allowlist — `CORSPolicy.__post_init__`'s
        # `InsecureCORSPolicyError` guard already forbids a wildcard origin
        # outright, credentials or not (see that guard's own docstring),
        # and there is no code path here that skips constructing
        # `CORSPolicy` in the first place — so this can never smuggle a
        # wildcard-plus-credentials configuration past that guard. Byte-
        # for-byte the same gating logic as `app/main.py`'s own
        # `auth_cookie_mode_enabled`-gated CORS construction.
        _cors_policy = CORSPolicy(
            allow_origins=_cors_policy.allow_origins,
            allow_credentials=True,
            allow_headers=(*_cors_policy.allow_headers, "X-CSRF-Token", "X-Auth-Mode"),
        )
    globals().update(cors_settings(_cors_policy))
else:
    # No CORS_ALLOWED_ORIGINS configured: leave django-cors-headers' own
    # settings entirely unset. CorsMiddleware is still in MIDDLEWARE (it has
    # to be, since Django's MIDDLEWARE is a fixed list, not conditionally
    # built per-environment the way backend/fastapi's app.add_middleware()
    # calls are) but with CORS_ALLOWED_ORIGINS unset, django-cors-headers'
    # own default is an empty allowlist — no Access-Control-Allow-Origin
    # header is ever sent, so a browser blocks every cross-origin JS request
    # against this app regardless. Same practical deny-by-default outcome as
    # backend/fastapi's "no CORSMiddleware at all when unconfigured" path,
    # reached via CorsMiddleware's own empty-allowlist default instead of
    # omitting the middleware (which Django's static MIDDLEWARE list can't
    # do per-request/per-environment the way an ASGI factory function can).
    # `AUTH_COOKIE_MODE_ENABLED` is irrelevant here regardless of its value
    # -- there is no CORSPolicy at all to widen when no origin is
    # configured, matching the "credentials require explicit origins"
    # invariant documented above.
    pass


# ---------------------------------------------------------------------------
# Rate limiting — Stage 4 Step 3 (#27). Read from env with the same defaults
# core.security.rate_limiting.django.RateLimitMiddleware itself falls back to
# when a setting is absent (capacity=60, refill_per_second=1.0,
# trusted_hops=0) — set explicitly here so a project sees the actual
# configured values in one place rather than relying on the component's own
# internal fallback silently applying. `RATE_LIMIT_TRUSTED_HOPS` defaults to
# 0 (ignore X-Forwarded-For, trust only the real TCP peer) -- a project
# behind exactly one trusted edge proxy (e.g. a single ALB) sets this to 1,
# per core/security/rate_limiting/_core.py's client_ip_key docstring; never
# guessed, never higher than the real proxy count.
#
# `RATE_LIMIT_MAX_KEYS` (Stage 4 review fix, #27) bounds the in-process
# InMemoryBucketStore's key cardinality (this app's own addition -- see
# core/security/rate_limiting/django.py's module DRIFT note); default
# 50_000 buckets, a generous cap for a per-client-IP key space that still
# guards against unbounded memory growth under a high-cardinality-client
# burst. `/health` and `/readyz` are exempt from rate limiting entirely
# (same module, `_DEFAULT_EXEMPT_PATHS`) so a readiness/liveness probe can
# never be 429'd by ordinary traffic sharing the same bucket space.
# ---------------------------------------------------------------------------

RATE_LIMIT_CAPACITY = int(os.environ.get("RATE_LIMIT_CAPACITY", "60"))
RATE_LIMIT_REFILL_PER_SECOND = float(os.environ.get("RATE_LIMIT_REFILL_PER_SECOND", "1.0"))
RATE_LIMIT_TRUSTED_HOPS = int(os.environ.get("RATE_LIMIT_TRUSTED_HOPS", "0"))
RATE_LIMIT_MAX_KEYS = int(os.environ.get("RATE_LIMIT_MAX_KEYS", "50000"))


# ---------------------------------------------------------------------------
# Django REST Framework — Stage 4 Step 2 (#27). JSONRenderer-only has been
# locked in since Step 1 (references/backend/drf.md's "Browsable API": drop
# BrowsableAPIRenderer in prod). EXCEPTION_HANDLER (core/exceptions.py) and
# DEFAULT_PAGINATION_CLASS (core/pagination.py) are what make this app's
# wire responses match the FastAPI block byte-for-byte — see this block's
# README, "Conformance", and error-envelope/errors.py + pagination/
# schema.py's own module docstrings for the shapes they reproduce.
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
    ],
    # DRF's own default (unset) is [SessionAuthentication, BasicAuthentication]
    # -- an unintended auth surface on every view that doesn't explicitly
    # override `authentication_classes` (ItemViewSet, core/views.py, never
    # did). Closing it kit-wide here means `permission_classes = [AllowAny]`
    # actually means "no auth attempted at all" everywhere in this block
    # until Stage 5 (#28) adds real authentication, rather than silently
    # accepting HTTP Basic credentials against Django's `auth_user` table
    # nothing in this block ever populates. The health/readyz/auth-stub
    # views' own explicit `authentication_classes = []` (core/views.py) is
    # now consistent with -- not a narrower override of -- this default,
    # rather than the one thing actually closing that surface for them.
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    # Maps DRF's own exception types (ValidationError, NotFound,
    # PermissionDenied, NotAuthenticated, Throttled, ...) — and anything
    # else unhandled — onto core.contract.errors.ErrorEnvelope, so this
    # app's error responses are wire-identical to the FastAPI block's — see
    # core/exceptions.py's own module docstring for the full mapping table
    # and core/contract/errors.py's ("ONE error shape, not two").
    "EXCEPTION_HANDLER": "core.exceptions.exception_handler",
    # DRF's built-in PageNumberPagination emits {count, next, previous,
    # results} — NOT this contract's {items, total, page, size, pages}
    # shape (core.contract.pagination.Page). This class emits the latter —
    # see core/pagination.py.
    "DEFAULT_PAGINATION_CLASS": "core.pagination.ContractPageNumberPagination",
    "PAGE_SIZE": 20,
    # Stage 4 Step 4 (#27): drf-spectacular's AutoSchema, replacing DRF's
    # own (undocumented-by-default) schema generation — see
    # SPECTACULAR_SETTINGS below and README.md's "Conformance" for the
    # wire-surface conformance proof this makes possible.
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
}


# ---------------------------------------------------------------------------
# drf-spectacular — Stage 4 Step 4 (#27). Generates this app's OpenAPI 3
# schema from the DRF views/serializers actually wired above (`core/urls.py`,
# `core/views.py`, `core/serializers.py`), each decorated with `@extend_schema`
# to reach BEST-EFFORT parity — operationIds, tags, and component names —
# with the frozen `packages/api-client/openapi.json` FastAPI exported
# (see README.md, "Conformance" — Step 4, for the full wire-surface
# conformance proof and the documented operationId/component-name deltas).
# `SERVE_INCLUDE_SCHEMA=False`: the schema view itself
# (`config/urls.py`'s `/api/schema`) does not recursively document itself.
# `APPEND_COMPONENTS` hand-declares the `HTTPBearer` security scheme
# (`securitySchemes.HTTPBearer` in the frozen contract) — there is no real
# DRF `authentication_classes` entry yet to auto-derive it from (every view
# is `authentication_classes = []` until Stage 5, #28), so `core/views.py`'s
# `MeView` opts into it explicitly via `@extend_schema(security=...)`
# instead.
# ---------------------------------------------------------------------------

SPECTACULAR_SETTINGS = {
    "TITLE": "Django block",
    "DESCRIPTION": (
        "Stage 4 (#27) Django + DRF backend block — best-effort OpenAPI "
        "schema parity with packages/api-client/openapi.json (the frozen "
        "FastAPI contract); see this block's README.md, 'Conformance'."
    ),
    "VERSION": "0.1.0",
    "SERVE_INCLUDE_SCHEMA": False,
    # This app's own DRF serializers already carry no `Serializer` in their
    # documented shape's intent (`ItemOutSerializer` -> component `ItemOut`,
    # matching `openapi.json`'s `ItemOut` exactly) — spectacular's own
    # default `POSTPROCESSING_HOOKS`/naming already strips the `Serializer`
    # suffix, so no `COMPONENT_SPLIT_REQUEST`/custom naming hook is needed
    # for that half of the parity target.
    "APPEND_COMPONENTS": {
        "securitySchemes": {
            "HTTPBearer": {"type": "http", "scheme": "bearer"},
        },
    },
}


# ---------------------------------------------------------------------------
# Secrets — Stage 4 Step 3 (#27). secret_store: ONE copy, not two. This
# block already vendored `secrets-loading/secret_store.py` verbatim in Step 1
# (`core/contract/secret_store.py`, alongside errors.py/pagination.py -- see
# "Vendored contract sources" in README.md) -- this step REUSES that copy
# rather than vendoring a second one under core/security/. Decision: leave
# it in `core/contract/` (not move it under core/security/secrets_loading/)
# because it is already established there as one of Step 1's three vendored
# contract sources, all sharing that directory's "keep in sync via the
# weekly freshness audit" maintenance path; moving it now would split that
# maintenance story across two locations for a file whose vendored content
# is unaffected either way. `core.security` (this step's new subpackage) has
# no `secrets_loading/` subpackage as a result -- see core/security/
# __init__.py's own docstring for the same note.
#
# `jwt_signing_key` is the concrete secret_store composition example, same
# seam backend/fastapi's app/core/config.py demonstrates: nothing in this
# block consumes it yet (Stage 5, #28, wires real authentication) --
# `required=False` and no invented default means this resolves to `None`
# until a project sets `JWT_SIGNING_KEY`, and this line itself never logs or
# raises with the resolved value (secret_store.get_secret's own "never log a
# secret value" posture -- see core/contract/secret_store.py's module
# docstring).
# ---------------------------------------------------------------------------

from core.contract.secret_store import get_secret as _get_secret  # noqa: E402  (after CORS/rate-limit env reads, matching this module's env-driven top-to-bottom layout)

JWT_SIGNING_KEY: str | None = _get_secret("JWT_SIGNING_KEY", required=False)

# ---------------------------------------------------------------------------
# Auth (Stage 5b, #44): the vendored auth component's TokenService/
# AuthService, constructed in core/security/auth/stores.py:get_token_service()/
# build_auth_service() from JWT_SIGNING_KEY above plus the three fields below
# -- the Django-track counterpart to backend/fastapi's app/core/config.py
# `jwt_issuer`/`jwt_access_ttl_seconds`/`jwt_refresh_ttl_seconds` fields (same
# names, same defaults, same rationale -- see that module's own docstrings
# for the full "why 900s / why 1_209_600s" reasoning this mirrors). Plain
# env-driven module-level settings, matching every other value in this file,
# rather than a pydantic Settings subclass -- this block has no such seam
# (see settings/README.md's "a project SUBCLASSES AppSettings" pattern,
# which is FastAPI-track-specific; this block reads os.environ directly
# throughout, as established at the top of this file).
# ---------------------------------------------------------------------------

JWT_ISSUER: str = os.environ.get("JWT_ISSUER", "app")
JWT_ACCESS_TTL_SECONDS: int = int(os.environ.get("JWT_ACCESS_TTL_SECONDS", "900"))
JWT_REFRESH_TTL_SECONDS: int = int(os.environ.get("JWT_REFRESH_TTL_SECONDS", "1209600"))

# ---------------------------------------------------------------------------
# Account lifecycle (Stage 5c, #45): core/security/auth/stores.py's
# build_lockout_policy()/build_account_service() wire against these -- the
# Django-track counterpart to backend/fastapi's app/core/config.py's
# identically-named (lowercase) Stage 5c fields; same names, same defaults,
# same rationale -- see that module's own docstrings for the full "why
# 5 / 900 / 900 / True / 86400 / 3600" reasoning this mirrors. Plain
# env-driven module-level constants, matching JWT_ISSUER/JWT_ACCESS_TTL_
# SECONDS above, not a pydantic Settings subclass (this block has no such
# seam -- see the JWT block's own comment above).
#
# NOT yet wired into build_auth_service()/login itself -- that's the next
# Django-parity stage's (Agent B's) endpoint work; these fields exist so
# this stage's store/factory layer (core/security/auth/stores.py) has real
# config to build against, the identical "inert until the next stage wires
# it" posture app/core/config.py's own Stage 5c fields document.
# ---------------------------------------------------------------------------

AUTH_REQUIRE_EMAIL_VERIFICATION: bool = _env_bool("AUTH_REQUIRE_EMAIL_VERIFICATION", True)
AUTH_LOCKOUT_ENABLED: bool = _env_bool("AUTH_LOCKOUT_ENABLED", True)
AUTH_LOCKOUT_MAX_FAILURES: int = int(os.environ.get("AUTH_LOCKOUT_MAX_FAILURES", "5"))
AUTH_LOCKOUT_DURATION_SECONDS: int = int(os.environ.get("AUTH_LOCKOUT_DURATION_SECONDS", "900"))
AUTH_LOCKOUT_WINDOW_SECONDS: int = int(os.environ.get("AUTH_LOCKOUT_WINDOW_SECONDS", "900"))

# --- Email (Stage 5c, #45): core/security/auth/stores.py's DjangoEmailSender
# delegates its actual transport entirely to Django's own pluggable
# django.core.mail EMAIL_BACKEND -- see that class's own docstring for why
# this Django track needs only ONE sender class where backend/fastapi needs
# two (ConsoleEmailSender/SmtpEmailSender). EMAIL_BACKEND/EMAIL_HOST/
# EMAIL_PORT/EMAIL_HOST_USER/EMAIL_HOST_PASSWORD/EMAIL_USE_TLS are Django's
# OWN standard setting names -- django.core.mail.backends.smtp.EmailBackend
# reads them automatically, so this app never hand-rolls an smtplib client
# the way SmtpEmailSender (backend/fastapi) does. EMAIL_HOST/EMAIL_HOST_USER/
# EMAIL_HOST_PASSWORD route through secret_store (get_secret) -- the SAME
# "don't invent a secret" and AWS-Secrets-Manager-layered posture
# JWT_SIGNING_KEY documents (see that line's own comment, below) --
# EMAIL_PORT/EMAIL_USE_TLS/EMAIL_BACKEND/EMAIL_FROM are plain, non-secret
# config and stay ordinary os.environ.get() reads, matching JWT_ISSUER's own
# treatment above.
#
# DEPLOYMENT REQUIREMENT, mirroring app/core/config.py's own smtp_host
# comment: when AUTH_REQUIRE_EMAIL_VERIFICATION is True (the default), a
# real EMAIL_BACKEND (django.core.mail.backends.smtp.EmailBackend) plus
# EMAIL_HOST/etc. MUST be configured in every production deployment, or
# get_email_sender() silently falls back to Django's own console backend --
# no delivery ever happens, and the console backend logs raw verify/reset
# tokens to this process's own stdout, a real secret leak if that ever runs
# in production. This is a REQUIRED DEPLOY STEP, not a code-level guard --
# unlike JWT_SIGNING_KEY (which fails closed at the point of use via
# AuthNotConfiguredError), EMAIL_BACKEND deliberately has no equivalent
# fail-closed check here, for the identical "a fragile runtime prod-
# detection check is easy to get wrong" reasoning app/core/config.py's own
# smtp_host comment gives.
# ---------------------------------------------------------------------------

EMAIL_BACKEND: str = os.environ.get("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST: str | None = _get_secret("EMAIL_HOST", required=False)
EMAIL_PORT: int = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER: str | None = _get_secret("EMAIL_HOST_USER", required=False)
EMAIL_HOST_PASSWORD: str | None = _get_secret("EMAIL_HOST_PASSWORD", required=False)
EMAIL_USE_TLS: bool = _env_bool("EMAIL_USE_TLS", True)
EMAIL_FROM: str = os.environ.get("EMAIL_FROM", "no-reply@example.com")

# --- Frontend link target (Stage 5c, #45): AccountService builds
# verify-email/reset-password links against this origin -- see
# core.security.auth._core.AccountService's own docstring on the
# '#token=...' fragment placement. ------------------------------------
FRONTEND_BASE_URL: str = os.environ.get("FRONTEND_BASE_URL", "http://localhost:5173")
# --- Server-side sessions: the DEFAULT browser authentication path.
# `core/security/auth/stores.py:build_session_service()` reads all three.
# None of them is a secret -- a session id is opaque and unguessable rather
# than signed, so unlike JWT_SIGNING_KEY there is nothing here to load from
# a secret store and nothing to fail closed on. See
# core/security/auth/_sessions.py's module docstring for why this path is
# preferred over JWT for browser clients, and this block's README
# "Sessions" section for the wiring. Ordinary os.environ.get() reads,
# matching JWT_ISSUER/AUTH_LOCKOUT_* above. ------------------------------
#
# SESSION_IDLE_TTL_SECONDS (12h): the SLIDING deadline, measured against a
# session's last_seen_at. A session unused for this long stops
# authenticating -- what makes an abandoned session on a shared or stolen
# machine expire without the user doing anything. Shorten it for a
# higher-sensitivity app (a banking or admin console might use 15-30
# minutes).
SESSION_IDLE_TTL_SECONDS: int = int(os.environ.get("SESSION_IDLE_TTL_SECONDS", "43200"))
# SESSION_ABSOLUTE_TTL_SECONDS (7d): the HARD ceiling, measured against
# created_at and unaffected by activity. Without it, a session an attacker
# keeps warm with a periodic request would live forever -- sliding expiry
# alone rewards precisely the party actively using a stolen cookie. A
# session must be inside BOTH deadlines to authenticate.
SESSION_ABSOLUTE_TTL_SECONDS: int = int(os.environ.get("SESSION_ABSOLUTE_TTL_SECONDS", "604800"))
# SESSION_TOUCH_INTERVAL_SECONDS (60s): how stale last_seen_at must be
# before a live request writes it back -- the knob that keeps every
# authenticated GET from becoming a database write, the usual way
# server-side sessions turn into a write-throughput problem. It shortens
# the EFFECTIVE idle window by at most this much, so keep it orders of
# magnitude below SESSION_IDLE_TTL_SECONDS; it can only ever expire a
# session slightly early, never keep a stale one alive.
SESSION_TOUCH_INTERVAL_SECONDS: int = int(os.environ.get("SESSION_TOUCH_INTERVAL_SECONDS", "60"))

AUTH_VERIFY_TTL_SECONDS: int = int(os.environ.get("AUTH_VERIFY_TTL_SECONDS", "86400"))
AUTH_RESET_TTL_SECONDS: int = int(os.environ.get("AUTH_RESET_TTL_SECONDS", "3600"))
