"""App-specific settings: this project's own `Settings`, subclassing the
vendored `AppSettings` (app/core/settings.py, vendored from
templates/components/backend/settings/settings.py — see that file's header
note). NOT itself a vendored file — this is the per-project composition
point settings/README.md documents ("a project SUBCLASSES AppSettings"),
so it lives here rather than inside the vendored settings.py, keeping that
file byte-identical to its source for the freshness audit.

`get_settings()` is `lru_cache`d so `Depends(get_settings)` (or a plain
call from app/main.py's lifespan) doesn't re-read/re-validate the
environment on every call — matching the "fails fast at startup" intent:
the first construction either succeeds once or raises once, and every
subsequent call reuses that same validated instance. Tests that need a
different environment call `get_settings.cache_clear()` after changing env
vars (see tests/conftest.py).

Stage 3 Step 3b (#26): adds the config the security-composition wiring in
app/main.py's create_app() needs (rate limiting, security headers) plus one
concrete secret_store composition example (`jwt_signing_key`) — see each
field's own docstring below for its default and why.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field

from app.core.security.secret_store import get_secret
from app.core.settings import AppSettings


class Settings(AppSettings):
    """This project's concrete settings. `app_name`/`api_version` are
    static app metadata, not environment-sourced config, so they live as
    plain constants in app/main.py instead of here."""

    # --- Rate limiting (rate_limiting/fastapi.py's RateLimitMiddleware,
    # wired in app/main.py's create_app()) -----------------------------
    rate_limit_capacity: int = Field(
        default=60,
        description="Token-bucket capacity for the general per-IP API ceiling "
        "(rate_limiting/README.md's 'the general per-IP API ceiling'). 60 is a "
        "starting-point default, not a load-tested figure -- tune per project.",
    )
    rate_limit_refill_per_second: float = Field(
        default=1.0,
        description="Tokens refilled per second -- 1.0 pairs with capacity=60 for "
        "a nominal ~60/minute steady-state ceiling with a 60-request burst "
        "allowance. See rate_limiting/_core.py's 'refill_per_second as a float' "
        "judgment call for why this isn't 'N requests per window' instead.",
    )
    rate_limit_trusted_hops: int = Field(
        default=0,
        description="SECURE DEFAULT: 0 -- distrust X-Forwarded-For entirely and "
        "key on the real TCP peer address, per rate_limiting/_core.py's "
        "client_ip_key(). Set to the EXACT number of trusted reverse proxies "
        "directly in front of this app (e.g. 1 for a single ALB) once that "
        "topology is confirmed for a given environment -- never guessed, and "
        "never left at a default '>0' value, which would let a client spoof "
        "its own rate-limit key via a forged header.",
    )

    # --- Security headers (security_headers/fastapi.py's
    # add_security_headers, wired in app/main.py's create_app()) --------
    security_headers_hsts_preload: bool = Field(
        default=False,
        description="SECURE DEFAULT: False, matching security_headers/_core.py's "
        "own SecurityHeadersPolicy default. HSTS preload is a one-way door (the "
        "browser preload list) -- opt in deliberately per environment once HTTPS "
        "is confirmed correct everywhere on this domain, never by default.",
    )

    # --- Secrets composition seam (Stage 5, #28) ------------------------
    # settings.py's own module docstring documents this exact composition
    # pattern ("a project SUBCLASSES AppSettings and wires a field's default
    # to secret_store.get_secret(...)"); this is that pattern's one concrete
    # example for this block. `required=False` and no invented fallback
    # value: nothing in this app consumes jwt_signing_key yet (Stage 5 wires
    # real JWT issuance), so this resolves to `None` in dev/test today
    # rather than either (a) making Settings() construction fail wherever
    # JWT_SIGNING_KEY isn't set -- which would break every existing test and
    # the plain `uvicorn app.main:app` dev boot, both of which predate this
    # field and don't set it -- or (b) hard-coding a fake "dev" secret value,
    # which is exactly the "don't invent secrets" this seam is scoped to
    # avoid. `repr=False`/`exclude=True` so a resolved value (once Stage 5
    # sets JWT_SIGNING_KEY for real) never appears in a `Settings` repr, a
    # `model_dump()`, or a traceback that happens to print `self` -- the
    # same "never log a secret value" posture secret_store.py's own
    # docstring establishes, applied here at the settings-object boundary.
    jwt_signing_key: str | None = Field(
        default_factory=lambda: get_secret("JWT_SIGNING_KEY", required=False),
        repr=False,
        exclude=True,
        description="Stage 5a (#41) JWT signing key, resolved via "
        "secret_store.get_secret's layered env-then-AWS-Secrets-Manager "
        "resolution. Still resolves to None when JWT_SIGNING_KEY is unset -- "
        "most of this app's routes/tests never touch auth at all, so "
        "Settings() construction must not hard-fail on a missing secret this "
        "request doesn't need. app/core/security/auth/stores.py's "
        "get_token_service() is where the fail-CLOSED check actually lives: "
        "it refuses to construct a TokenService (and therefore refuses to "
        "sign/verify anything) when this is None, at the point auth is "
        "actually used, not at Settings() construction time.",
    )

    # --- Auth (Stage 5a, #41): the vendored auth component's TokenService,
    # constructed in app/core/security/auth/stores.py:get_token_service()
    # from the three fields below plus jwt_signing_key above -----------
    jwt_issuer: str = Field(
        default="app",
        description="The `iss` claim TokenService stamps into every minted "
        "JWT and requires on every decode (_core.py's TokenService.decode_* "
        "-- see its own docstring on why issuer is checked, not just "
        "signature/expiry). A generic default; a project with more than one "
        "issuer sharing a signing key (rare) would override this per "
        "environment -- most projects never need to.",
    )
    jwt_access_ttl_seconds: int = Field(
        default=900,
        description="Access token TTL, 15 minutes -- short-lived by design "
        "(references/security/secure-baseline.md: 'Prefer short-lived "
        "access tokens with refresh over long-lived static tokens'). A "
        "stolen access token's blast radius is bounded by this window; "
        "there is no server-side revocation for access tokens themselves "
        "(only refresh tokens are revocable, via RefreshTokenStore).",
    )
    jwt_refresh_ttl_seconds: int = Field(
        default=1_209_600,
        description="Refresh token TTL, 14 days (1_209_600 seconds) -- long "
        "enough that a legitimate user isn't forced to re-login constantly, "
        "short enough to bound how long a family that somehow evades reuse "
        "detection could stay alive. Refresh tokens ARE individually "
        "revocable (RefreshTokenStore.revoke_family, on reuse detection or "
        "logout), unlike access tokens above -- this TTL is a ceiling on top "
        "of that, not the only defense.",
    )

    # --- Account lifecycle (Stage 5c, #45): AccountService/build_account_
    # service() in app/core/security/auth/stores.py wires against these.
    # NOT yet wired into get_auth_service()/login itself -- that's the next
    # stage's endpoint work; these fields exist so this stage's service/
    # factory layer has real config to build against. ------------------
    auth_require_email_verification: bool = Field(
        default=True,
        description="SECURE DEFAULT: True -- once an endpoint stage wires "
        "this into AuthService(require_verification=...), an unverified "
        "account cannot log in. This field is inert on its own until that "
        "wiring lands (Stage 5c does not touch login behavior).",
    )
    auth_lockout_enabled: bool = Field(
        default=True,
        description="SECURE DEFAULT: True -- build_lockout_policy() returns "
        "a real LockoutPolicy (backed by SqlAlchemyLockoutStore) when this "
        "is set, None when it isn't. See build_lockout_policy()'s own "
        "docstring.",
    )
    auth_lockout_max_failures: int = Field(
        default=5,
        description="_core.LockoutPolicy's max_failures -- consecutive "
        "failed logins for one account, within auth_lockout_window_seconds, "
        "before the account locks.",
    )
    auth_lockout_duration_seconds: int = Field(
        default=900,
        description="_core.LockoutPolicy's lockout_duration, 15 minutes -- "
        "how long an account stays locked once max_failures is crossed "
        "(re-armed on every subsequent failure while still locked -- see "
        "LockoutPolicy.record_failure's own docstring).",
    )
    auth_lockout_window_seconds: int = Field(
        default=900,
        description="_core.LockoutPolicy's rolling window, 15 minutes -- a "
        "failure older than this resets the streak rather than "
        "accumulating onto it (see LockoutPolicy.record_failure's own "
        "'Rolling window' section).",
    )

    # --- Email (Stage 5c, #45): get_email_sender() below resolves a
    # ConsoleEmailSender (dev/test) when smtp_host is unset, else a real
    # SmtpEmailSender built from these -- same "don't invent a secret"
    # posture as jwt_signing_key above (smtp_* resolve via secret_store's
    # layered env-then-AWS-Secrets-Manager lookup, never a hardcoded
    # fallback). --------------------------------------------------------
    # DEPLOYMENT REQUIREMENT (adversarial-review fix, FIX 4) -- NOT a
    # code-level fail-closed check, deliberately unlike jwt_signing_key
    # above: when auth_require_email_verification is True (the default),
    # a real SMTP_HOST MUST be configured in every production/L3
    # deployment. Contrast with jwt_signing_key, which FAILS CLOSED
    # (get_token_service() refuses to construct a TokenService, surfacing
    # as a 500, when it's unset) -- smtp_host has no equivalent guard,
    # and deliberately isn't given one here: unlike a missing signing key
    # (which makes EVERY auth endpoint unusable, an unmistakable, loud
    # failure), a missing SMTP_HOST fails open and quiet -- the app keeps
    # running, /auth/register still returns 201, /auth/login still works
    # for already-verified accounts -- while get_email_sender() silently
    # falls back to ConsoleEmailSender, which:
    #   (a) LOGS raw verify/reset tokens in plaintext (see that class's own
    #       docstring) -- a dev/test convenience that is a real secret
    #       leak into this process's log aggregator if it ever runs that
    #       way in production, and
    #   (b) means delivery never actually happens for any user -- no one
    #       can complete /auth/verify-email, so no one can ever satisfy
    #       login's require_verification gate (short of the password-reset
    #       recovery path, which also requires an email that was never
    #       sent -- reset shares the same sender).
    # A fragile runtime "are we in prod?" check (an ENVIRONMENT string
    # comparison, a hostname sniff) was deliberately NOT added here to
    # enforce this -- that class of check is easy to get wrong, easy to
    # bypass by accident (a staging environment named oddly, a container
    # running with the wrong env var), and this catalog's existing
    # posture (see jwt_signing_key's own docstring: "don't invent a
    # secret, fail closed at the point of use") already treats "the
    # operator configured the required secret" as the correct point of
    # enforcement, not a guess made from other settings. This is a
    # REQUIRED DEPLOY STEP, not a code change: set SMTP_HOST (and
    # SMTP_PORT/SMTP_USERNAME/SMTP_PASSWORD/EMAIL_FROM as the relay
    # requires) in every real environment before serving real traffic --
    # see backend/fastapi/README.md's "Auth" / email seam section for the
    # same note in operator-facing form.
    smtp_host: str | None = Field(
        default_factory=lambda: get_secret("SMTP_HOST", required=False),
        repr=False,
        exclude=True,
        description="SMTP relay hostname. Unset (None) in dev/test -- "
        "get_email_sender() falls back to ConsoleEmailSender when this is "
        "unset, exactly the 'don't invent a secret, fail closed at the "
        "point of use, not at Settings() construction' posture "
        "jwt_signing_key's own docstring documents. UNLIKE "
        "jwt_signing_key, though, there is no code-level fail-closed "
        "guard on this field -- see the comment immediately above this "
        "field for why, and why a real SMTP_HOST is a REQUIRED "
        "deployment step (not optional) whenever "
        "auth_require_email_verification is True (the default).",
    )
    smtp_port: int = Field(
        default_factory=lambda: int(get_secret("SMTP_PORT", required=False, default="587")),
        description="SMTP relay port. 587 (STARTTLS submission) is the "
        "default if SMTP_PORT is unset -- only consulted once smtp_host is "
        "actually set.",
    )
    smtp_username: str | None = Field(
        default_factory=lambda: get_secret("SMTP_USERNAME", required=False),
        repr=False,
        exclude=True,
        description="SMTP auth username. None skips SMTP AUTH entirely "
        "(some relays, e.g. an internal MTA, don't require it).",
    )
    smtp_password: str | None = Field(
        default_factory=lambda: get_secret("SMTP_PASSWORD", required=False),
        repr=False,
        exclude=True,
        description="SMTP auth password -- never logged, never included in "
        "a Settings repr/model_dump (repr=False, exclude=True), matching "
        "every other secret field in this class.",
    )
    email_from: str = Field(
        default="no-reply@example.com",
        description="The 'From' address AccountService's verify/reset "
        "emails (and any future transactional email) are sent as. Not a "
        "secret -- a plain per-environment config value, overridden per "
        "deployment, never left at this placeholder in production.",
    )

    # --- Frontend link target (Stage 5c, #45): AccountService builds
    # verify-email/reset-password links against this origin -- see
    # _core.AccountService's own docstring on the '#token=' fragment
    # placement. ----------------------------------------------------------
    frontend_base_url: str = Field(
        default="http://localhost:5173",
        description="The SPA/site origin AccountService.request_email_"
        "verification/request_password_reset build links against "
        "('{frontend_base_url}/verify-email#token=...'). Default is a "
        "typical local Vite dev server -- override per environment.",
    )
    auth_verify_ttl_seconds: int = Field(
        default=86_400,
        description="AccountService's verify_ttl, 24 hours -- how long an "
        "email-verification link stays valid before SingleUseTokenService."
        "consume rejects it as expired.",
    )
    auth_reset_ttl_seconds: int = Field(
        default=3_600,
        description="AccountService's reset_ttl, 1 hour -- shorter than "
        "verify_ttl on purpose, see AccountService's own docstring: an "
        "unconsumed password-reset link is a more immediately sensitive "
        "thing to have floating around than an unconsumed verify link.",
    )

    # --- Server-side sessions: the DEFAULT browser authentication path.
    # `app/core/security/auth/stores.py:build_session_service` reads all
    # three. None of them is a secret -- a session id is opaque and
    # unguessable rather than signed, so unlike jwt_signing_key above there
    # is nothing here to load from a secret store and nothing to fail
    # closed on. See app/core/security/auth/_sessions.py's module docstring
    # for why this path is preferred over JWT for browser clients. --------
    session_idle_ttl_seconds: int = Field(
        default=43_200,
        description="SessionService's idle_ttl, 12 hours -- the SLIDING "
        "deadline, measured against a session's last_seen_at. A session "
        "unused for this long stops authenticating, which is what makes an "
        "abandoned session on a shared or stolen machine expire without the "
        "user doing anything. Shorten it for a higher-sensitivity app (a "
        "banking or admin console might use 15-30 minutes); it composes "
        "with, and can never be overridden by, the absolute ceiling below.",
    )
    session_absolute_ttl_seconds: int = Field(
        default=604_800,
        description="SessionService's absolute_ttl, 7 days -- the HARD "
        "ceiling, measured against a session's created_at and unaffected by "
        "activity. Without it, a session an attacker keeps warm with a "
        "periodic request would live forever: sliding expiry alone rewards "
        "precisely the party actively using a stolen cookie. A session must "
        "be inside BOTH this and session_idle_ttl_seconds to authenticate.",
    )
    session_touch_interval_seconds: int = Field(
        default=60,
        description="SessionService's touch_interval, 1 minute -- how stale "
        "last_seen_at must be before a live request writes it back. This is "
        "the knob that keeps every authenticated GET from becoming a "
        "database write, the usual way server-side sessions turn into a "
        "write-throughput problem. It shortens the EFFECTIVE idle window by "
        "at most this much, so keep it orders of magnitude below "
        "session_idle_ttl_seconds; it can only ever expire a session "
        "slightly early, never keep a stale one alive.",
    )

    # --- Web cookie mode (Stage 5d, #46): app/main.py's create_app() reads
    # this to decide whether the CORS policy it constructs allows
    # credentials (cookies) and the two extra request headers cookie mode
    # needs cross-origin -- see that call site's own comment for the
    # "credentials require explicit origins" invariant this flag composes
    # with, never replaces. ------------------------------------------------
    auth_cookie_mode_enabled: bool = Field(
        default=False,
        description="SECURE DEFAULT: False -- a bearer-only deployment "
        "stays credential-free at the CORS layer (allow_credentials=False, "
        "the CORSPolicy default) even if this app's /auth/login-refresh-"
        "logout handlers themselves already support an X-Auth-Mode: cookie "
        "caller (they do, unconditionally -- see app/api/routers/auth.py). "
        "Set True only for a deployment that actually serves a browser SPA "
        "using cookie mode cross-origin -- it widens the CORS policy "
        "(allow_credentials=True, plus X-CSRF-Token/X-Auth-Mode in "
        "allow_headers), which is meaningless -- and safe to leave off -- "
        "for a mobile-only or same-origin deployment. See app/main.py's "
        "create_app() CORS construction for exactly what this flips.",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
