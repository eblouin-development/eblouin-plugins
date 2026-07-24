"""Auth router (Stage 5a, #41) — real handlers wired against the vendored
`AuthService` (`app/core/security/auth`), replacing the Stage 3 Step 2
stubs (every route used to unconditionally return `HTTPException(501)`;
see `app/api/deps.py`'s pre-Stage-5a history in git log for that stub
era).

Every handler here is thin, matching `app/api/routers/items.py`'s own
"validate, delegate, map, return" shape: no credential/token logic lives
in this file — it's entirely `_core.AuthService`'s job (register/login/
refresh/logout/resolve_access). This router's only real job beyond
delegation is the wire-shape mapping (`_core.UserRecord`/`_core.TokenPair`
-> this app's `PrincipalOut`/`TokenResponse` Pydantic schemas) and — for
`GET /me` only — a second, direct lookup (`SqlAlchemyUserStore.get_by_id`)
to fetch the caller's `email`, since `_core.AccessClaims` (what
`get_current_principal` resolves a bearer token to) intentionally carries
only `sub`/`roles`/`jti`/timestamps, not a full user profile — see that
dataclass's own docstring.

Every `_core.AuthError` subclass (`InvalidCredentials`, `InvalidToken`,
`TokenReused`, `EmailAlreadyExists`, `InvalidSingleUseToken`) raised by any
handler below is left UNCAUGHT here — `app/main.py`'s `create_app()`
registers a handler for the `AuthError` base class that renders the
vendored component's `AUTH_ERROR_HTTP` mapping as this app's own
`ErrorEnvelope`. No handler below ever constructs an `ErrorEnvelope`/
`AppError` itself.

Stage 5c (#45) adds the account-lifecycle surface — `POST /auth/verify-
email`, `POST /auth/request-password-reset`, `POST /auth/reset-password`
— against the vendored `AccountService` (`app/api/deps.py:
get_account_service`), and gives `register` a post-registration side
effect: it now also sends a verification email (`AccountService.
request_email_verification`) and emits an `auth.register` audit event.
`login`'s own behavior (the verification gate, lockout, and its own audit
events) is entirely `AuthService`'s job as of `app/api/deps.py:
get_auth_service`'s Stage 5c wiring — this file's `login` handler itself
is byte-for-byte unchanged from Stage 5a.

**SERVER-SIDE SESSIONS ARE THE DEFAULT.** `POST /auth/login` issues an
opaque session id in an `HttpOnly` `session_id` cookie unless the caller
explicitly sends `X-Auth-Mode: bearer`, and `GET /auth/me` (like every
other protected route in this app) authenticates against that session via
`app/api/deps.py`'s `get_current_principal`. The JWT/bearer path is fully
wired and fully supported for native/mobile clients and service-to-service
callers — it is a documented exception, not a deprecation. See the
vendored `app/core/security/auth/_sessions.py`'s module docstring for why
a browser client should prefer a session (immediate revocation, live role
reads, an enforceable idle timeout, and nothing readable left in the
browser), and this block's README "Sessions" section for the wiring.

Transport is decided per request, never from a client's claim about
itself: `login` reads `X-Auth-Mode` (absent/unrecognized → session; the
literal `"bearer"` → JWT), and `logout` decides by which credential is
ACTUALLY present on the request (`session_id` cookie → session path,
`refresh_token` cookie → refresh-cookie path, neither → bearer body). A
forged or absent cookie therefore cannot claim a path it does not hold.
`X-Auth-Mode` is read directly off `request.headers` rather than as a
declared FastAPI `Header(...)` parameter, which keeps it out of the
exported OpenAPI schema as a documented parameter; `enforce_csrf` reads
`X-CSRF-Token` the identical way, for the identical reason.

CSRF is enforced on every state-changing cookie-authenticated request via
double-submit (see the vendored `_cookies.py`'s own module docstring).
Session mode's cookie is scoped `Path=/`, so that obligation extends to
every unsafe-method route in the app, not just `/auth/*` — this app meets
it with `app/api/middleware/csrf.py`, a single method-filtering
middleware, rather than a per-route call that a future route could
forget."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_account_service,
    get_auth_service,
    get_current_principal,
    get_session_service,
)
from app.core.db import get_db
from app.core.errors import ErrorEnvelope
from app.core.security.auth import (
    AccountService,
    AuthService,
    InvalidToken,
    SessionPrincipal,
    SessionService,
    clear_auth_cookies,
    clear_session_cookies,
    enforce_csrf,
    generate_csrf_token,
    read_refresh_cookie,
    read_session_cookie,
    set_auth_cookies,
    set_session_cookies,
)
from app.core.security.auth.stores import AuditAuthEventSink, SqlAlchemyUserStore, utc_now
from app.schemas.auth import (
    LoginRequest,
    PrincipalOut,
    RefreshRequest,
    RegisterRequest,
    RequestPasswordResetRequest,
    ResetPasswordRequest,
    TokenResponse,
    VerifyEmailRequest,
)

router = APIRouter(prefix="/auth", tags=["auth"])

# FIX C (whole-PR review, Stage 5a, contract completeness): documents the
# ErrorEnvelope-shaped error responses these routes actually send at
# runtime (via app/main.py's `_auth_error_handler`/`_app_error_handler`),
# same pattern as `app/api/routers/items.py`'s own `_NOT_FOUND_RESPONSE` --
# a `responses={...}` dict of `{status: {"model": ErrorEnvelope,
# "description": ...}}` merged into each route decorator below. Before this
# fix, the exported/frozen OpenAPI contract (`packages/api-client/
# openapi.json`) only documented success + 422 for every /auth/* route --
# the runtime 401/409 responses were entirely undeclared, so a generated
# client had no typed knowledge of them. `POST /auth/logout` is
# deliberately NOT given one of these: it's 204 and idempotent by design
# (see that handler's own docstring) and never raises an error a client
# needs to handle.
_UNAUTHENTICATED_RESPONSE = {
    401: {"model": ErrorEnvelope, "description": "Invalid credentials, or an invalid/expired/revoked token."}
}
_CONFLICT_RESPONSE = {409: {"model": ErrorEnvelope, "description": "An account with this email already exists."}}
# Stage 5c (#45): documents the 401 an invalid/expired/reused single-use
# (verify or reset) token produces -- `_core.InvalidSingleUseToken` maps to
# the SAME (401, "unauthenticated") entry in `AUTH_ERROR_HTTP` every other
# auth failure does (see that exception's own docstring on why "bad token",
# "expired token", and "already-used token" all collapse to one generic,
# wire-indistinguishable response), so this reuses the exact envelope shape
# `_UNAUTHENTICATED_RESPONSE` above already documents, just with wording
# specific to a single-use link rather than a login/refresh credential.
_INVALID_SINGLE_USE_TOKEN_RESPONSE = {
    401: {"model": ErrorEnvelope, "description": "The verify/reset link is invalid, expired, or has already been used."}
}
# Stage 5c (#45): every route in this file with a JSON request body already
# gets FastAPI's native 422 automatically, remapped to this app's
# `ErrorEnvelope` shape by `app/main.py`'s `_install_error_envelope_openapi`
# (applied uniformly across every operation, not per-route) -- this
# constant exists purely so the three new account-lifecycle routes'
# `responses=` declarations are self-documenting about that already-real
# behavior, matching this task's own explicit contract, even though the
# schema content itself is fixed up centrally either way.
_VALIDATION_RESPONSE = {422: {"model": ErrorEnvelope, "description": "Request validation failed."}}


@router.post(
    "/register",
    response_model=PrincipalOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register",
    responses=_CONFLICT_RESPONSE,
)
async def register(
    payload: RegisterRequest,
    auth_service: AuthService = Depends(get_auth_service),
    account_service: AccountService = Depends(get_account_service),
) -> PrincipalOut:
    """Delegates straight to `AuthService.register` — raises
    `EmailAlreadyExists` (-> 409 `conflict`) for a duplicate normalized
    email, uncaught here (see module docstring).

    Stage 5c (#45): on success, additionally (a) sends a verification email
    (`AccountService.request_email_verification(user)` — the freshly
    created `UserRecord` `AuthService.register` just returned, so no extra
    lookup is needed) and (b) emits an `auth.register` audit event. Neither
    changes this endpoint's response shape (still 201 `PrincipalOut`) — a
    project whose `Settings.auth_require_email_verification` is `True`
    (the secure default) needs the caller to actually consume the emailed
    link (`POST /auth/verify-email`) before `AuthService.login` will let
    this account in; see that dependency's own docstring.

    Adversarial-review fix (M2): `request_email_verification` is wrapped in
    `try/except Exception` — the user row is already durably committed by
    the time this runs (`AuthService.register` returned successfully), so
    a verification-email failure here (SMTP outage, bounced address) must
    NEVER turn into a 500: the account already exists, a retry would just
    409 on the duplicate email, `require_verification=True` means the
    account can't log in either way, and the wire caller (whoever showed
    the registration form) has no way to "undo" or recover a 500 here —
    it would brick a just-created account with no path forward. Register
    stays 201 regardless of whether the email actually went out; the
    failure is only logged/audited (`auth.register.verification_email_
    failed`, no PII/token in the event), never surfaced to the caller. The
    recovery path for an account whose verification email never arrived is
    `POST /auth/request-password-reset` -> `POST /auth/reset-password` —
    `AccountService.reset_password` now also marks the email verified (see
    `_core.AccountService.reset_password`'s own docstring), so a user who
    never got their verification link can still get into their account."""
    user = await auth_service.register(payload.email, payload.password)
    try:
        await account_service.request_email_verification(user)
    except Exception:
        # M2: never let a verification-email delivery failure 500 an
        # already-committed registration -- see this handler's own
        # docstring above. No PII/token in this event -- just that it
        # happened, for a human to notice and, if needed, resend by hand.
        await AuditAuthEventSink().emit(
            "auth.register.verification_email_failed", actor=user.id, outcome="failure"
        )
    await AuditAuthEventSink().emit("auth.register", actor=user.id, outcome="success")
    return PrincipalOut(id=uuid.UUID(user.id), email=user.email)


@router.post("/login", response_model=TokenResponse, summary="Login", responses=_UNAUTHENTICATED_RESPONSE)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
    session_service: SessionService = Depends(get_session_service),
) -> TokenResponse:
    """Verifies credentials through `AuthService.login` — raises
    `InvalidCredentials` (-> 401 `unauthenticated`) identically for an
    unknown email, a wrong password, a locked account, or an unverified
    one (see that exception's own docstring on the deliberate
    user-enumeration defense), uncaught here.

    **Session mode is the DEFAULT.** `request.headers.get("X-Auth-Mode")`
    selects the transport across three values:

    | `X-Auth-Mode` | Transport |
    | --- | --- |
    | absent, `"session"`, or anything unrecognized | **SESSION** (default) |
    | `"bearer"` | JWT access + refresh in the response body |
    | `"cookie"` | JWT refresh token in an `HttpOnly` `Path=/auth` cookie |

    Read directly off `request.headers`, deliberately NOT a declared
    `Header(...)` parameter (see this module's own docstring: keeps it out
    of the exported OpenAPI schema as a documented parameter). Mode is
    NEVER inferred from `User-Agent` or any other signal — a client asks
    for a non-default path explicitly or gets the default, which is the
    safer direction for an unrecognized value to fall.

    **`"cookie"` mode is superseded, not removed.** It put the JWT refresh
    token in a cookie to keep it out of JS's reach — the right answer
    before this app had server-side sessions, and strictly worse than
    session mode now: it still leaves a bearer access token in the JS heap,
    still cannot be revoked before its TTL, and still needs the refresh
    round-trip session mode does without. It stays wired for a project
    mid-migration; new work should use the default.

    - **Session mode (browsers).** `AuthService.login` verifies the
      password and its token pair is DISCARDED unused — the credential
      that actually authenticates subsequent requests is the opaque
      session id `SessionService.create` mints. `set_session_cookies`
      writes the `HttpOnly` `session_id` cookie plus a fresh,
      independent CSRF cookie (`generate_csrf_token()` — never derived
      from the session id) the SPA echoes back as `X-CSRF-Token` on every
      unsafe-method request. `max_age` comes from
      `IssuedSession.max_age_seconds(utc_now())`, measured to the
      session's ABSOLUTE deadline so the cookie survives an idle period
      and the SERVER, not the browser, is what decides a session went
      stale. The response body is `TokenResponse` with BOTH fields empty
      — the wire schema is unchanged, and empty is honest: in session
      mode there is no token for the client to hold, which is the entire
      point (nothing in the JS heap for an XSS payload to exfiltrate).
    - **Bearer mode (native/mobile, service-to-service).** The exact,
      unchanged prior behavior: the real access and refresh JWTs are
      returned in the body, no cookies are set, and no session row is
      created.

    Both paths run the SAME credential check — `AuthService.authenticate`,
    which owns Argon2id verification, the `dummy_verify` timing defense,
    lockout, and the `require_verification` gate. The session path calls it
    directly rather than calling `login()` and discarding the token pair:
    doing the latter would persist a `RefreshRecord` for a refresh token no
    client will ever hold, starting a token family logout would never
    revoke. See that method's own docstring.

    No CSRF check on login in either mode: login is authenticated by the
    credentials in the body, and there is no cookie yet for a forged
    request to ride."""
    mode = request.headers.get("X-Auth-Mode")
    if mode == "bearer":
        pair = await auth_service.login(payload.email, payload.password)
        return TokenResponse(access_token=pair.access, refresh_token=pair.refresh)
    if mode == "cookie":
        pair = await auth_service.login(payload.email, payload.password)
        set_auth_cookies(
            response,
            refresh_value=pair.refresh,
            csrf_value=generate_csrf_token(),
            max_age=request.app.state.settings.jwt_refresh_ttl_seconds,
        )
        return TokenResponse(access_token=pair.access, refresh_token="", token_type="bearer")
    user = await auth_service.authenticate(payload.email, payload.password)
    issued = await session_service.create(user)
    set_session_cookies(
        response,
        session_value=issued.session_id,
        csrf_value=generate_csrf_token(),
        max_age=issued.max_age_seconds(utc_now()),
    )
    return TokenResponse(access_token="", refresh_token="", token_type="session")


@router.post("/refresh", response_model=TokenResponse, summary="Refresh token", responses=_UNAUTHENTICATED_RESPONSE)
async def refresh(
    payload: RefreshRequest,
    request: Request,
    response: Response,
    auth_service: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    """**The JWT path only — a session client never calls this.** There is
    no refresh step in session mode: `SessionService.resolve` slides the
    session's idle deadline forward on every authenticated request (see
    its `touch_interval` bound), so the credential renews itself as a side
    effect of being used, up to the absolute ceiling. Not having a refresh
    endpoint to call is one of the things session mode buys — the entire
    rotation-and-reuse-detection state machine below exists to compensate
    for a bearer token the server cannot revoke.

    Delegates to `AuthService.refresh` — THE rotation-with-reuse-
    detection state machine (see `_core.py`'s own module docstring and
    `AuthService.refresh`'s docstring for the full 6-step state machine).
    Raises `InvalidToken` or `TokenReused` (both -> 401 `unauthenticated`,
    deliberately indistinguishable at the wire — see `TokenReused`'s own
    docstring), uncaught here. A `TokenReused` raise has, as a side
    effect, ALREADY revoked the token's entire family in the DB by the
    time this handler's caller sees the 401.

    Stage 5d (#46) web cookie mode: DUAL-SOURCE, decided per-request by
    `read_refresh_cookie(request)` (whether the `refresh_token` cookie is
    actually present on THIS request), never by a header the client
    declares — a forged/absent cookie can't claim cookie mode, and a
    genuine cookie-bearing browser request can't accidentally fall onto
    the bearer path either.

    - **Cookie path** (cookie present): `enforce_csrf(request)` runs
      FIRST — raises `CsrfValidationError` (-> 403 `permission_denied`,
      `AUTH_ERROR_HTTP`) before the cookie's refresh token is ever
      presented to `AuthService.refresh` at all, so a request that fails
      the double-submit check never gets to attempt a rotation. The
      request BODY's `payload.refresh_token` is parsed (still required —
      `RefreshRequest`'s schema is unchanged) but its VALUE is
      deliberately ignored; the cookie's own value is what's rotated.
      On success, BOTH cookies are set again — `set_auth_cookies` with
      the NEWLY minted refresh JWT and a FRESH `generate_csrf_token()`
      (never the old CSRF value) — exactly `login`'s own cookie-setting
      shape, so a stolen, already-rotated refresh cookie (reused after
      this response) is rejected the same way `AuthService.refresh`'s
      reuse-detection already rejects any other reused refresh token
      (401, whole family revoked). The response body is `TokenResponse`
      with `refresh_token=""`, matching `login`'s cookie-mode shape.
    - **Bearer path** (no cookie): the exact, unchanged prior behavior —
      `payload.refresh_token` is the real token, no CSRF check, and the
      real new refresh JWT is returned in the body."""
    cookie_refresh_token = read_refresh_cookie(request)
    if cookie_refresh_token is not None:
        enforce_csrf(request)
        pair = await auth_service.refresh(cookie_refresh_token)
        set_auth_cookies(
            response,
            refresh_value=pair.refresh,
            csrf_value=generate_csrf_token(),
            max_age=request.app.state.settings.jwt_refresh_ttl_seconds,
        )
        return TokenResponse(access_token=pair.access, refresh_token="", token_type="bearer")
    pair = await auth_service.refresh(payload.refresh_token)
    return TokenResponse(access_token=pair.access, refresh_token=pair.refresh)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Logout")
async def logout(
    request: Request,
    response: Response,
    payload: RefreshRequest = RefreshRequest(),
    auth_service: AuthService = Depends(get_auth_service),
    session_service: SessionService = Depends(get_session_service),
) -> None:
    """Ends the caller's session, whichever transport they authenticated
    with. **TRIPLE-SOURCE**, decided per request by which credential is
    actually present — never by a header the client declares, so a forged
    or absent cookie cannot claim a path it does not hold.

    - **Session path** (`session_id` cookie present) — the default.
      `enforce_csrf(request)` runs FIRST: logout is state-changing, so a
      request with a missing/blank/mismatched `X-CSRF-Token` is rejected
      403 at that gate and never reaches the revocation. Past the gate,
      `SessionService.revoke` kills the server-side row — the half that
      actually matters, since it stops the id authenticating even for a
      client that ignores the cookie-delete instruction — and
      `clear_session_cookies` clears both cookies. `revoke` is idempotent
      and never raises (see its own docstring), so a stale or unknown
      session id still 204s rather than becoming an oracle that
      distinguishes real ids from invented ones.
    - **Refresh-cookie path** (`refresh_token` cookie present): unchanged
      prior behavior — CSRF enforced first, then `AuthService.logout`
      revokes the whole token family, then `clear_auth_cookies`.
    - **Bearer path** (no cookie at all): unchanged — the body's
      `refresh_token`, no CSRF check, 204 either way.

    Checked in that order because the session cookie is the default
    credential; a request carrying both (only possible mid-migration, and
    discouraged — see the vendored `_cookies.py`'s "running both paths at
    once") has its session ended, which is the credential that would
    otherwise still authenticate every route.

    **`RefreshRequest.refresh_token` is optional** (defaulting to `""`),
    because a session client genuinely has no refresh token to send — see
    that schema's own docstring. The field is read only on the bearer
    path, where it is the credential; a bearer request that omits it still
    204s, since logout is idempotent and there is nothing to revoke."""
    session_id = read_session_cookie(request)
    if session_id is not None:
        enforce_csrf(request)
        await session_service.revoke(session_id)
        clear_session_cookies(response)
        return None
    cookie_refresh_token = read_refresh_cookie(request)
    if cookie_refresh_token is not None:
        enforce_csrf(request)
        await auth_service.logout(cookie_refresh_token)
        clear_auth_cookies(response)
        return None
    await auth_service.logout(payload.refresh_token)
    return None


@router.get("/me", response_model=PrincipalOut, summary="Current principal", responses=_UNAUTHENTICATED_RESPONSE)
async def me(
    principal: SessionPrincipal = Depends(get_current_principal),
    db: AsyncSession = Depends(get_db),
) -> PrincipalOut:
    """`get_current_principal` (the vendored component's
    `build_get_current_session_principal`, bound in `app/api/deps.py`)
    already resolved the caller's `session_id` cookie into a live
    `SessionPrincipal` before this handler body ever runs — a missing,
    revoked, idle-expired, or otherwise dead session never reaches here at
    all (it raises `InvalidSession` -> 401 `unauthenticated` itself).

    `SessionPrincipal` carries `sub` (the user id) and `roles`, but not
    `email` — this handler does one direct `SqlAlchemyUserStore.get_by_id`
    lookup to fill in `PrincipalOut.email`, independent of `AuthService`
    (which has no "fetch a profile" method — see `_core.py`'s `UserStore`
    Protocol; it's a storage seam for the auth flows, not a general
    user-lookup API this router reaches for).

    The `user is None` branch below is now nearly unreachable, and that is
    itself the point: `SessionService.resolve` already loads the user on
    every request and rejects a session whose account has been deleted, so
    the "credential valid but its user is gone" race the bearer path has
    to live with (an access JWT is not individually revocable — see
    `Settings.jwt_access_ttl_seconds`) simply does not exist here. The
    check is kept as defense in depth against a deletion landing between
    those two reads within this one request, and raises `InvalidToken`
    (401) rather than 404 for the same reason it always did: it is the
    credential that is no longer trustworthy, not a resource the caller
    asked for by id."""
    user = await SqlAlchemyUserStore(db).get_by_id(principal.sub)
    if user is None:
        raise InvalidToken("This session no longer maps to an active user.")
    return PrincipalOut(id=uuid.UUID(user.id), email=user.email)


# ---------------------------------------------------------------------------
# Account lifecycle (Stage 5c, #45): verify-email / request-password-reset /
# reset-password, against the vendored AccountService.
# ---------------------------------------------------------------------------


@router.post(
    "/verify-email",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Verify email",
    responses={**_INVALID_SINGLE_USE_TOKEN_RESPONSE, **_VALIDATION_RESPONSE},
)
async def verify_email(
    payload: VerifyEmailRequest,
    account_service: AccountService = Depends(get_account_service),
) -> None:
    """Delegates to `AccountService.verify_email` — raises
    `InvalidSingleUseToken` (-> 401 `unauthenticated`, generic and
    wire-identical to every other single-use-token rejection reason — see
    that exception's own docstring) for an unknown/expired/already-used/
    wrong-purpose token, uncaught here (see module docstring). On success,
    marks the token's owning user's email verified — see `AuthService.
    login`'s `require_verification` gate (`app/api/deps.py:
    get_auth_service`) for why that matters: with `Settings.
    auth_require_email_verification=True` (the default), login for this
    account was refused (generically, as `InvalidCredentials`) until this
    endpoint succeeds."""
    await account_service.verify_email(payload.token)


@router.post(
    "/request-password-reset",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Request password reset",
    responses=_VALIDATION_RESPONSE,
)
async def request_password_reset(
    payload: RequestPasswordResetRequest,
    account_service: AccountService = Depends(get_account_service),
) -> Response:
    """Delegates to `AccountService.request_password_reset` — that method
    NEVER raises and never reveals whether `payload.email` has an account
    (see its own docstring on the anti-user-enumeration defense this
    mirrors from `AuthService.login`'s own `InvalidCredentials`), so this
    handler ALWAYS returns 202 with a genuinely EMPTY body (`Response(...,
    content=b"")`, not FastAPI's default JSON-encoded `null` a bare
    `return None` with no `response_model` would send instead — a
    byte-identical, content-free response is the strongest form of "this
    endpoint reveals nothing" for a known email and an unknown one alike),
    never a 404/409 that would leak account existence. A `422` (declared
    above) is the one response shape this endpoint CAN still send, for a
    request body that fails `RequestPasswordResetRequest`'s own schema
    validation (e.g. an empty `email` string) before this handler body ever
    runs."""
    await account_service.request_password_reset(payload.email)
    return Response(status_code=status.HTTP_202_ACCEPTED, content=b"")


@router.post(
    "/reset-password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Reset password",
    responses={**_INVALID_SINGLE_USE_TOKEN_RESPONSE, **_VALIDATION_RESPONSE},
)
async def reset_password(
    payload: ResetPasswordRequest,
    account_service: AccountService = Depends(get_account_service),
) -> None:
    """Delegates to `AccountService.reset_password` — raises
    `InvalidSingleUseToken` (-> 401 `unauthenticated`, generic — see
    `verify_email`'s docstring above for the identical rationale) for an
    unknown/expired/already-used/wrong-purpose reset token, uncaught here.
    On success, revokes EVERY refresh-token family the user has (every
    device/session is logged out, not just the one that requested the
    reset — see `AccountService.reset_password`'s own docstring) and, if a
    lockout policy is wired, lifts any failed-login lockout on the
    account — the same shared-session `LockoutPolicy` `app/api/deps.py:
    get_auth_service`'s `AuthService.login` recorded against, so the reset
    account can log in with its new password immediately."""
    await account_service.reset_password(payload.token, payload.new_password)
