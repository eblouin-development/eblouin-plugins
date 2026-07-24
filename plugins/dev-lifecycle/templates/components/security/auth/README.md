<!--
block: components/security/auth  # catalog component
last-verified: 2026-07-24
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
needs:
  - PyJWT 2.13.x (tested against 2.13.0): the sole non-stdlib dependency for token minting/verification -- needed only by the JWT path (_core.py's TokenService); a session-only project never imports it
  - argon2-cffi 25.1.x (tested against 25.1.0): the sole non-stdlib dependency for password hashing
  - app-level wiring (NOT part of this component): UserStore/RefreshTokenStore/SessionStore implementations against a real ORM/session, AuthService + SessionService construction with real TTLs (and, on the JWT path, a real signing key) at app startup, and an app exception handler using this component's own AUTH_ERROR_HTTP table to map onto ErrorEnvelope/ErrorCode -- see backend/fastapi's app/core/security/auth/stores.py + app/main.py for the reference implementation (Stage 5a, #41), and backend/django's core/security/auth/stores.py for the Django equivalent (Stage 5b, #44)
exposes:
  - SessionService (create, resolve, rotate, revoke, revoke_all_for_user), SessionStore (Protocol), SessionRecord / SessionPrincipal / IssuedSession, generate_session_id, InvalidSession -- the opaque server-side SESSION core, the DEFAULT browser auth path, in _sessions.py
  - PasswordService (hash, verify, needs_rehash, dummy_verify), TokenService (mint_access, mint_refresh, decode_access, decode_refresh), AuthService (register, login, refresh, logout, resolve_access) -- in _core.py
  - UserStore / RefreshTokenStore (Protocols), UserRecord / RefreshRecord (frozen dataclasses), hash_token(raw) -- the storage seam a framework adapter implements
  - TokenPair, AccessClaims, RefreshClaims -- the claim/result shapes
  - AuthError hierarchy: InvalidCredentials, InvalidToken, TokenReused, EmailAlreadyExists, InvalidSingleUseToken -- each documents the ErrorCode it maps to
  - bearer_scheme, build_get_current_principal, require_roles, AUTH_ERROR_HTTP -- the FastAPI wiring, in fastapi.py (Stage 5a, #41)
  - resolve_principal, require_roles, InsufficientRole, AUTH_ERROR_HTTP -- the Django wiring, in django.py (Stage 5b, #44)
  - build_get_current_session_principal -- the FastAPI session dependency factory, in fastapi.py; resolve_session_principal / require_session_roles -- the Django equivalents, in django.py
  - CsrfValidationError, SESSION_COOKIE_NAME, REFRESH_COOKIE_NAME, CSRF_COOKIE_NAME, SESSION_COOKIE_PATH, REFRESH_COOKIE_PATH, generate_csrf_token, verify_double_submit, build_session_cookie_kwargs, build_session_csrf_cookie_kwargs, clear_session_cookie_kwargs, clear_session_csrf_cookie_kwargs, build_refresh_cookie_kwargs, build_csrf_cookie_kwargs, clear_refresh_cookie_kwargs, clear_csrf_cookie_kwargs -- the framework-neutral double-submit-cookie CSRF transport, in _cookies.py (Stage 5d, #46)
  - set_session_cookies, clear_session_cookies, read_session_cookie -- session-cookie glue over _cookies.py, in BOTH fastapi.py and django.py
  - set_auth_cookies, clear_auth_cookies, read_refresh_cookie, enforce_csrf -- thin cookie/CSRF glue over _cookies.py, in BOTH fastapi.py and django.py (Stage 5d, #46); django.py's stays rest_framework-free like the rest of that file
  - AccountService (request_email_verification, verify_email, request_password_reset, reset_password) -- email verification + password reset, composed ALONGSIDE AuthService, not a subclass -- in _core.py (Stage 5c, #45)
  - AuthService.issue_session(user) -- mints a session for an ALREADY-authenticated principal (no password check), the seam OAuthAccountService.complete_login mints through -- added to _core.py (social-login recipe, #96)
  - PKCEPair / generate_pkce_pair, generate_state / generate_nonce, verify_state / verify_nonce, OAuthProviderConfig, build_authorization_url, OAuthAuthorizationRequest / start_authorization -- OAuth 2.0/OIDC authorization-code + PKCE core, framework-neutral, stdlib-only -- in _oauth.py (social-login recipe, #96)
  - OAuthIdentity, OAuthLinkedAccountRecord, OAuthAccountStore (Protocol), OAuthAccountService (resolve_or_link, complete_login) -- the social-login account-linking orchestrator, including the unverified-email-attack defense -- in _oauth.py (social-login recipe, #96)
  - OAuthStateMismatch, OAuthNonceMismatch, UnverifiedEmailAccountConflict -- the AuthError subclasses _oauth.py raises, each documenting its ErrorCode mapping -- in _oauth.py (social-login recipe, #96)
  - SingleUseTokenService (issue, consume) / SingleUseTokenStore (Protocol) / SingleUseTokenRecord -- the hashed, single-use verify/reset token seam AccountService runs against
  - LockoutPolicy (is_locked, record_failure, clear) / LockoutStore (Protocol) / AttemptRecord -- per-account failed-login lockout, optionally shared between AuthService.login and AccountService.reset_password
  - EmailSender (Protocol) / EmailMessage / ConsoleEmailSender -- the email-delivery seam AccountService sends verify/reset links through; ConsoleEmailSender is DEV-ONLY (logs the raw token instead of delivering it)
  - AuthEventSink (Protocol) -- the optional audit-event seam AuthService.login and every AccountService method emit through
  - its co-located doc fragment: docs/fragment.md
-->

# auth

Full composition-contract detail (exact NEEDS/EXPOSES prose) lives in the
"Composition contract" section below — this header is kept short so the
plugin's freshness-header lint (which only scans a file's first 1000 bytes)
reliably finds `last-verified` on every README, regardless of header length.

A framework-neutral auth core shipping **two authentication paths**, with
a clear default:

- **Server-side sessions (`_sessions.py`) — THE DEFAULT for browser
  clients.** Opaque, high-entropy session ids whose entire authority lives
  in a `SessionStore` row: revocable on the next request, reflecting role
  changes immediately, enforcing a real idle timeout, and putting nothing
  readable in the browser. See "Server-side sessions" below.
- **JWT access/refresh (`_core.py`) — for native/mobile clients and
  service-to-service callers.** PyJWT HS256 tokens with single-use
  refresh-token ROTATION and REUSE DETECTION, the security-critical piece
  of that path. Correct where the client has an OS-backed secret store
  (Expo SecureStore, iOS Keychain, Android Keystore) and no ambient-cookie
  exposure, or where there is no user session at all.

Both share one `UserStore`, one `PasswordService` (Argon2id), one role
model, and one exception→`ErrorCode` table, so a project can serve
session-authenticated web and bearer-authenticated mobile from a single
set of route handlers.

**Why sessions are the default.** A JWT is valid because it says it is:
between mint and expiry the server has no say. That statelessness is the
whole point for service-to-service auth, and it is the wrong trade for a
browser session, where logout, bans, and privilege revocation all need to
take effect *now* rather than after a TTL elapses. `_core.py`'s own
`RefreshTokenStore` already concedes this for the refresh half — trust
lives in the store, not the claims; `_sessions.py` applies the same
reasoning to the half that actually authorizes requests. The cost is one
store read per request, which a browser-facing app already pays several
times over. Full argument in `_sessions.py`'s module docstring; the
tradeoff is restated in "Judgment calls" below.

Embodies `references/security/secure-baseline.md`'s "Authentication &
authorization" section (password hashing with a strong adaptive algorithm;
credentials validated fully; sensible expiry and secure rotation/logout).
Lives at `templates/components/security/auth/` in this repo; a Stage 5a
(#41) backend block copies `_core.py` + `fastapi.py` into
`app/core/security/auth/`.

This is a **catalog component** (`template-author`'s partial-contract
kind), not an app-layer template block.

**This component ships `_sessions.py` + `_core.py` + `_cookies.py` +
`_oauth.py` + `fastapi.py` + `django.py`.** `_sessions.py` is a
framework-neutral, **stdlib-plus-`_core`-only** file (no PyJWT import
either — a session-only project never pulls in a JWT library) holding the
opaque server-side session core described below; both adapters carry thin
glue over it (`set_session_cookies`, `clear_session_cookies`,
`read_session_cookie`, plus `build_get_current_session_principal` on the
FastAPI side and `resolve_session_principal`/`require_session_roles` on
the Django side). `fastapi.py` (Stage 5a, #41) is pure framework glue over
`_core.py` — the `HTTPBearer` scheme, a `build_get_current_principal`
dependency FACTORY (takes the app's own `get_auth_service` provider, since
this component has no DB session/settings of its own to build one from),
`require_roles`, and the `AUTH_ERROR_HTTP` exception -> `(status,
ErrorCode string)` table — with **zero `app.*` import**, matching
`_core.py`'s own "zero FastAPI/Django/SQLAlchemy import" posture in
reverse (see `fastapi.py`'s own module docstring). `django.py` (Stage 5b,
#44) is the same idea for Django/DRF — `resolve_principal(request,
auth_service)` (an awaited helper, not a `Depends()`-style factory, since
Django/DRF has no equivalent auto-invoked injection point),
`require_roles(request, auth_service, *roles)`, `InsufficientRole`, and
the identically-shaped `AUTH_ERROR_HTTP` table — with the same **zero
project import** posture (no `core.*`/`app.*`, and deliberately no
`rest_framework` import either, so a plain-Django project without DRF can
use it too; see `django.py`'s own module docstring). `_cookies.py` (Stage
5d, #46) is a SECOND framework-neutral file alongside `_core.py` — the
double-submit-cookie CSRF transport (`CsrfValidationError`,
`generate_csrf_token`, `verify_double_submit`, and the pure cookie-kwarg
builders) neither `fastapi.py` nor `django.py` had before this stage; both
adapters now carry thin glue over it (`set_auth_cookies`,
`clear_auth_cookies`, `read_refresh_cookie`, `enforce_csrf`) — see
"Cookie/CSRF transport" below. Vendoring these files is still NOT the
whole wiring job: `UserStore`/`RefreshTokenStore` implementations against
a real ORM, `AuthService` construction with real secrets/TTLs at app
startup, real route handlers, and an app-level exception handler for the
`AuthError` base class are all APP code (they import the app's own
models/settings), never part of this vendored component — see
`backend/fastapi`'s `app/core/security/auth/stores.py` + `app/main.py`'s
`_auth_error_handler` for the FastAPI reference implementation, and
`backend/django`'s `core/security/auth/stores.py` for the Django
equivalent. Zero FastAPI, Django, or SQLAlchemy import exists anywhere in
`_core.py` or `_cookies.py` — verified by this component's own `tests/`,
which import and exercise both completely standalone.

## Contents
- Composition contract
- Server-side sessions: the default browser path
- Password hashing: Argon2id + the timing-defense `dummy_verify()`
- Tokens: PyJWT HS256, injected clock, and why expiry isn't PyJWT's own check
- Refresh-token storage: SHA-256 hash, never the raw token
- The refresh-rotation state machine (the security-critical core)
- Account lifecycle: email verification, password reset, lockout (Stage 5c, #45)
- Cookie/CSRF transport: double-submit cookies (Stage 5d, #46)
- OAuth 2.0/OIDC social login: PKCE, account linking, the unverified-email defense (`#96`)
- Exception hierarchy → ErrorCode mapping (for the framework adapter)
- Testing
- Judgment calls

## Composition contract

**NEEDS**
- **PyJWT 2.13.x** (tested against the exact pin **2.13.0**) — the only
  dependency `TokenService` needs, and needed ONLY by the JWT path: a
  project that vendors `_sessions.py` and skips `_core.py`'s
  `TokenService` never imports PyJWT at all. Not yet added to
  `references/compatibility-matrix.md`'s Backend — Python row; the agent
  that wires this component into the FastAPI or Django backend block
  owns adding that pin (and `argon2-cffi`'s, below) to the matrix and to
  that block's `pyproject.toml`, landing the dependency pin next to the
  code that first actually consumes it in a running backend.
- **argon2-cffi 25.1.x** (tested against the exact pin **25.1.0**) — the
  only dependency `PasswordService` needs. Same matrix caveat as above.
- **App-level wiring** (not part of this component, even with `fastapi.py`
  vendored) — implements `UserStore` and `RefreshTokenStore` against a
  real ORM/session, constructs `TokenService`/`AuthService` with a real
  signing key (from secrets-loading, never hardcoded) and real TTLs at
  app startup, wires real route handlers, and registers an app exception
  handler for the `AuthError` base class that renders `fastapi.py`'s own
  `AUTH_ERROR_HTTP` table as `error-envelope/`'s `ErrorEnvelope`/
  `ErrorCode`. See "Exception hierarchy → ErrorCode mapping" below for
  the exact mapping, and `backend/fastapi`'s `app/core/security/auth/
  stores.py` for a concrete implementation.

**EXPOSES** (`_core.py` unless noted)
- **`_sessions.py`** (the default browser path): `SessionService(sessions,
  users, now, *, idle_ttl=12h, absolute_ttl=7d, touch_interval=1m,
  events=None)` — `create(user) -> IssuedSession`, `resolve(raw | None) ->
  SessionPrincipal` (THE state machine), `rotate(raw) -> IssuedSession`,
  `revoke(raw | None) -> None` (idempotent, never raises),
  `revoke_all_for_user(user_id) -> None`; `SessionStore` (Protocol) —
  `add`/`get_by_hash`/`touch`/`revoke`/`revoke_all_for_user`;
  `SessionRecord` (the persisted row), `SessionPrincipal` (the resolved
  principal — duck-type-compatible with `AccessClaims` on `sub`/`roles`),
  `IssuedSession` (raw id + record, with `max_age_seconds(now)`);
  `generate_session_id()`; and `InvalidSession`. See "Server-side
  sessions" below.
- `PasswordService` — `hash(password) -> str`, `verify(stored_hash,
  password) -> bool`, `needs_rehash(stored_hash) -> bool`,
  `dummy_verify() -> None` (user-enumeration timing defense — see below).
- `TokenService(signing_key, *, issuer, access_ttl, refresh_ttl, now)` —
  `mint_access(sub, roles) -> str`, `mint_refresh(sub, family_id) ->
  tuple[str, RefreshClaims]`, `decode_access(token) -> AccessClaims`,
  `decode_refresh(token) -> RefreshClaims`.
- `AuthService(users, refresh_tokens, passwords, tokens, now)` —
  `register(email, password, roles=()) -> UserRecord`, `login(email,
  password) -> TokenPair`, `refresh(raw_refresh_token) -> TokenPair` (THE
  rotation state machine), `logout(raw_refresh_token) -> None`,
  `resolve_access(raw_access_token) -> AccessClaims`.
- `UserStore` / `RefreshTokenStore` — `Protocol`s a framework adapter
  implements; `UserRecord` / `RefreshRecord` — the frozen dataclasses
  they operate on; `hash_token(raw) -> str` — the SHA-256 helper the
  refresh store's rows are keyed by.
- `TokenPair`, `AccessClaims`, `RefreshClaims` — the result/claim shapes.
- `AuthError` and its subclasses `InvalidCredentials`, `InvalidToken`,
  `TokenReused`, `EmailAlreadyExists`, `InvalidSingleUseToken` — see the
  mapping section below.
- **Stage 5c (#45)**: `AccountService(users, tokens, email, passwords,
  refresh_tokens, now, *, events=None, lockout=None, frontend_base_url,
  verify_ttl=24h, reset_ttl=1h)` — `request_email_verification(user) ->
  None`, `verify_email(raw_token) -> None`, `request_password_reset(
  email) -> None` (never raises), `reset_password(raw_token,
  new_password) -> None`; `SingleUseTokenService(store, now)` —
  `issue(user_id, purpose, ttl) -> str` (raw token), `consume(raw,
  purpose) -> str` (user id); `SingleUseTokenStore` (Protocol) /
  `SingleUseTokenRecord`; `LockoutPolicy(store, *, max_failures,
  lockout_duration, window, now)` — `is_locked`, `record_failure`,
  `clear`; `LockoutStore` (Protocol) / `AttemptRecord`; `EmailSender`
  (Protocol) / `EmailMessage` / `ConsoleEmailSender` (DEV-ONLY); and
  `AuthEventSink` (Protocol) — see "Account lifecycle" below.
- **`fastapi.py`**: `bearer_scheme` (an `HTTPBearer(auto_error=False)`
  instance), `build_get_current_principal(get_auth_service) ->
  <dependency>` (a dependency FACTORY — takes the app's own per-request
  `AuthService` provider, returns a dependency resolving a bearer token to
  `AccessClaims`), `require_roles(get_current_principal, *roles) ->
  <dependency>` (role-gated dependency factory; RBAC's wire surface is
  Stage 5d — this just enforces `AccessClaims.roles` membership),
  `InsufficientRole` (a component-level exception mapping to the existing
  `permission_denied`/403 — no new `ErrorCode` invented), `AUTH_ERROR_HTTP`
  (the exception-type -> `(status, ErrorCode string)` table an app's own
  exception handler consults), and (Stage 5d, #46) thin cookie/CSRF glue
  over `_cookies.py` — see below.
- **`django.py`**: `resolve_principal(request, auth_service) ->
  AccessClaims` (an awaited helper, not a dependency factory — see
  `django.py`'s own module docstring on why Django/DRF has no `Depends()`
  equivalent to compose against), `require_roles(request, auth_service,
  *roles) -> AccessClaims` (resolves the principal AND enforces role
  membership in one awaited call), `InsufficientRole` (the same
  `permission_denied`/403 mapping as `fastapi.py`'s own, kept as a
  separate class per adapter so each file still reads standalone when
  vendored alone), `AUTH_ERROR_HTTP` (identically shaped to `fastapi.py`'s
  own table), and (Stage 5d, #46) the SAME DRF-free cookie/CSRF glue
  surface as `fastapi.py`'s own — see below.
- **Stage 5d (#46), `_cookies.py`** (framework-neutral, stdlib-only —
  `hmac`/`secrets` only): `CsrfValidationError` (maps to the EXISTING
  `permission_denied`/403 — no new `ErrorCode`), `REFRESH_COOKIE_NAME`
  (`"refresh_token"`) / `CSRF_COOKIE_NAME` (`"csrf_token"`),
  `generate_csrf_token() -> str`, `verify_double_submit(*, csrf_cookie,
  csrf_header) -> None` (the double-submit check, constant-time via
  `hmac.compare_digest`), and the pure cookie-kwarg builders
  `build_refresh_cookie_kwargs(value, max_age) -> dict` /
  `build_csrf_cookie_kwargs(value, max_age) -> dict` /
  `clear_refresh_cookie_kwargs() -> dict` / `clear_csrf_cookie_kwargs() ->
  dict`. Both `fastapi.py` and `django.py` add thin glue over it:
  `set_auth_cookies(response, *, refresh_value, csrf_value, max_age) ->
  None`, `clear_auth_cookies(response) -> None`, `read_refresh_cookie(
  request) -> str | None`, `enforce_csrf(request) -> None` — identical
  signatures across both adapters; see "Cookie/CSRF transport" below.
- Its co-located doc fragment: `docs/fragment.md`.

## Server-side sessions: the default browser path

`_sessions.py` is what a browser client authenticates with in this
catalog. It is composed **alongside** `AuthService`, never as a subclass
and never replacing it: a login route still verifies the password through
`AuthService`'s existing machinery — Argon2id verification, the
`dummy_verify()` timing defense, lockout, the `require_verification`
gate — and then calls `SessionService.create(user)` instead of minting a
token pair. None of that credential logic is duplicated here.

### What a session is

An opaque `secrets.token_urlsafe(32)` string (~256 bits of CSPRNG
entropy) with **no structure at all** — not a JWT, not signed, no claims.
It is a lookup key and nothing else, so there is no payload for a client
to read, no signature for a server to misvalidate, and no `alg` for an
attacker to confuse. Every fact about the session lives in the
`SessionRecord` it looks up, which is exactly what makes every one of
those facts changeable by the server at any moment.

Only the **SHA-256 hash** of the id is ever persisted (`_core.hash_token`
— the same function, and the same fast-hash-not-a-slow-KDF reasoning, that
`RefreshRecord` already uses: a high-entropy, module-generated value, not
a low-entropy human-chosen secret). A read-only compromise of the sessions
table therefore hands out no usable cookies.

The record deliberately stores **no roles snapshot** — see "Roles are read
live" below.

### The `resolve` state machine

`SessionService.resolve(raw_session_id)` runs on every authenticated
request, in this exact order — see `_sessions.py`'s own docstring for the
full detail:

1. **Missing or blank id** → `InvalidSession`. `None` and `""` are handled
   identically, so an adapter passes `request.cookies.get(...)` straight
   through.
2. **Hash and look up.** The raw id is never compared against anything.
   **No row** → `InvalidSession`.
3. **`row.revoked`** → `InvalidSession`. *This single check is the
   immediate-revocation property* — a logged-out or administratively
   killed session fails on the very next request.
4. **`now >= row.absolute_expires_at`** → `InvalidSession`. The hard
   ceiling, unaffected by how recently the session was used.
5. **`now - row.last_seen_at >= idle_ttl`** → `InvalidSession`. The
   sliding deadline.
6. **User no longer exists** → `InvalidSession`. A deleted account's
   sessions die immediately, with no cleanup job.
7. **Otherwise live:** advance `last_seen_at` (rate-limited, below) and
   return a `SessionPrincipal` carrying the user's CURRENT roles.

The persisted `SessionRecord`, never anything the client presents, is the
sole source of truth — the same posture `AuthService.refresh` takes toward
`RefreshRecord`.

### Two deadlines, both enforced

| Deadline | Default | What it defends against |
| --- | --- | --- |
| `idle_ttl` (sliding, vs `last_seen_at`) | 12h | An abandoned session on a shared or stolen machine. A JWT structurally cannot do this — it has one fixed `exp` and no concept of "still being used". |
| `absolute_ttl` (hard, vs `created_at`) | 7d | A session kept alive forever by periodic use. Sliding expiry alone rewards precisely the attacker who is actively using the stolen cookie. |

Neither can rescue the other: a session must be inside **both** windows.
Nonsensical values (zero/negative) are rejected at construction, so a
misconfiguration fails loudly at wiring time rather than silently
expiring — or never expiring — every session at runtime.

### Roles are read live

`resolve` reads roles from `UserStore` on every request rather than
snapshotting them onto the session row. Granting or revoking a role
therefore takes effect on the **next request**, where a `roles` claim
baked into a JWT stays true until that token expires. This costs one extra
store read per request; a project that measures it as a real bottleneck
should cache the *user lookup* behind its own short-TTL cache — a
deliberate decision with a staleness bound it chose — rather than
denormalizing roles onto the session row, which would recreate a JWT's
staleness window over a session's much longer lifetime.

### The `touch_interval` write-rate bound

`last_seen_at` is written back only when it is already at least
`touch_interval` (default 1 minute) stale. Without this, every
authenticated request — including every cache-friendly `GET` — becomes a
database write, which is the most common way a server-side-session design
turns into a write-throughput problem. The cost is that `last_seen_at` may
lag real activity by up to `touch_interval`, shortening the effective idle
window by at most that much; with the defaults (1 minute against 12 hours)
the error is negligible, and it can only ever expire a session slightly
**early**, never keep a stale one alive.

### Session fixation, and rotation at privilege boundaries

Every `create()` mints a fresh id, which *is* the fixation defense: an
attacker who plants a session id in a victim's browser before login gains
nothing, because the id the victim ends up holding afterward is one
`create()` just generated. There is deliberately no "adopt the incoming
cookie value" path.

`rotate(raw)` is for privilege boundaries — immediately after a password
change, a step-up re-authentication, or a role grant. It revokes the old
id and issues a new one, and **the replacement inherits the original's
absolute deadline** (`created_at`/`absolute_expires_at` carried over
verbatim, never recomputed from `now`). Rotation is a security operation,
not a renewal: recomputing the ceiling would give anyone who can induce a
rotation a way to extend a session indefinitely, defeating `absolute_ttl`.

### Wiring checklist

1. Implement `SessionStore` against the project's ORM (a table keyed by
   `session_hash`, with `add`/`get_by_hash`/`touch`/`revoke`/
   `revoke_all_for_user`, each committed before returning).
2. Construct `SessionService` at startup with real TTLs and the SAME `now`
   callable every other service in this component uses.
3. Login route: verify credentials via `AuthService`, then
   `SessionService.create(user)` → `set_session_cookies(response,
   session_value=issued.session_id, csrf_value=generate_csrf_token(),
   max_age=issued.max_age_seconds(now))`.
4. Logout route: `SessionService.revoke(read_session_cookie(request))`
   **and** `clear_session_cookies(response)`. The revoke is the half that
   matters; clearing the cookie is cosmetic.
5. Protected routes: depend on `build_get_current_session_principal(...)`
   (FastAPI) or `await resolve_session_principal(request, session_service)`
   (Django). Role gates use the existing `require_roles` /
   `require_session_roles`.
6. **Enforce CSRF on every unsafe-method request** (`POST`/`PUT`/`PATCH`/
   `DELETE`), not just `/auth/*` — the session cookie is `Path=/`, so
   every state-changing route carries an ambient credential. One
   method-filtering middleware is safer than remembering it per route.
7. Password reset and account deactivation call
   `SessionService.revoke_all_for_user(user_id)` alongside the JWT path's
   `RefreshTokenStore.revoke_all_for_user`.

## Password hashing: Argon2id + the timing-defense `dummy_verify()`

`PasswordService` wraps `argon2.PasswordHasher`, left at Argon2id
(argon2-cffi's own default `Type.ID`) and its library-default cost
parameters — already OWASP's recommended default for new applications,
resistant to both GPU-parallel cracking and timing/cache side-channels
in ways the pure Argon2i/Argon2d variants aren't. A tuned
`argon2.PasswordHasher` instance can be passed into the constructor for a
project that wants to raise/lower the cost parameters for its own
hardware budget.

`verify()` collapses both a wrong password (`VerifyMismatchError`) and a
corrupt/foreign-format stored hash (`InvalidHashError`) to the same
`False` — the caller must never be able to tell those two apart through
the return value. Any OTHER exception is NOT caught and propagates,
deliberately — silently turning an unexpected bug into "verification
failed" would hide a real misconfiguration behind an ordinary-looking
failed login.

**`dummy_verify()`** exists purely as a user-enumeration timing defense.
`AuthService.login` calls it on the "no such email" path before raising
`InvalidCredentials`, so that path costs the same wall-clock time (one
Argon2id verify) as the "email found, password checked" path. Without
this, Argon2id's own deliberate slowness becomes the leak: an attacker
timing the login endpoint could tell a registered email from an
unregistered one purely by which response came back faster.

## Tokens: PyJWT HS256, injected clock, and why expiry isn't PyJWT's own check

`TokenService` mints and verifies HS256 JWTs against one shared
`signing_key`. Every claim listed in the component header's EXPOSES
section is present on every token (`sub`, `type`, `iat`, `exp`, `iss`,
`jti`, plus `roles` on access tokens / `fid` on refresh tokens);
`algorithms=["HS256"]` is passed explicitly on every decode call (never
inferred from the token's own header — trusting a token to name its own
verification algorithm is a known JWT vulnerability class), and the
`type` claim is asserted to match what the caller asked for — an access
token presented as a refresh token, or vice versa, is rejected at that
check.

**Expiry is verified manually against the injected `now()`, not PyJWT's
own built-in exp check** (`verify_exp`/`verify_iat` are explicitly turned
off in the `jwt.decode` call). PyJWT's own expiry validation always
compares against the real system clock with no parameter to substitute a
different "now" — which would make this component's own tests (advance
an injected clock past a TTL, assert rejection) either race the real
clock or be entirely disconnected from the `now` a given `TokenService`
was actually constructed with. Checking expiry by hand, against the
exact same `now` callable every other part of `TokenService`/
`AuthService` uses, is what makes `tests/test_core.py`'s expiry
assertions fully deterministic — no `time.sleep`, no wall-clock races.
A framework adapter should pass a real callable (e.g. `lambda:
datetime.now(timezone.utc)`) in production; tests pass an injectable,
advanceable fake (see `tests/conftest.py`'s `Clock`).

## Refresh-token storage: SHA-256 hash, never the raw token

`hash_token(raw) -> str` is `hashlib.sha256(raw.encode()).hexdigest()` —
the ONLY form of a refresh token this module ever persists. A fast
cryptographic hash (not a slow password KDF like Argon2/bcrypt) is the
correct choice HERE, deliberately different from `PasswordService`
above: a password is a low-entropy human-chosen secret vulnerable to
offline brute force against a stolen hash, which a slow KDF specifically
defends against. A refresh token is a high-entropy value this module
itself generated (a signed JWT — effectively random to an attacker) —
brute-forcing a SHA-256 preimage of 256 bits of entropy is infeasible
regardless of hash speed, so a slow KDF here would only add CPU cost to
every refresh/logout call for zero additional security. Hashing at all
still matters: a read-only compromise of the store's rows (a leaked
backup, a compromised read replica) does not hand out live, directly
usable refresh tokens.

## The refresh-rotation state machine (the security-critical core)

`AuthService.refresh(raw_refresh_token)` implements, in this exact
order — see `_core.py`'s own docstring on this method for the full
detail:

1. `TokenService.decode_refresh` — structural validation only
   (signature, expiry, issuer, `type == "refresh"`). Invalid → `InvalidToken`.
2. Hash the token, look up the row (`RefreshTokenStore.get_by_hash`). **No
   row** → `InvalidToken` — deliberately does NOT trust the token's own
   claims to revoke anything, since there's nothing on file to revoke.
3. **`row.revoked`** → `InvalidToken`.
4. **`row.used_at is not None`** → **REUSE DETECTED.** Calls
   `revoke_family(row.family_id)` — killing EVERY token in the family,
   including whichever one is currently the live tip of the rotation
   chain — then raises `TokenReused`.
5. **`row.expires_at <= now()`** → `InvalidToken`.
6. **Otherwise valid:** `mark_used(row.token_hash, now())`, mint a NEW
   access + refresh pair in the SAME family, persist the new refresh
   record, return the new pair. The just-used row is RETAINED with
   `used_at` set — not deleted — because that retention is exactly what
   makes step 4 able to detect a second presentation as reuse rather
   than "not found".

The persisted `RefreshRecord`, never the JWT's own claims, is the sole
source of truth for whether a refresh token is still usable — a
validly-signed, unexpired JWT whose row says otherwise still loses.

## Account lifecycle: email verification, password reset, lockout (Stage 5c, #45)

`AccountService` is composed ALONGSIDE `AuthService` — constructed and
used independently, not a subclass, not required to touch `AuthService`
at all — against the same underlying `UserStore`/`RefreshTokenStore` (and,
optionally, `LockoutStore`) a project wires both services from. Three new
seams support it, each with exactly one shipped implementation
(`ConsoleEmailSender`) or none (`SingleUseTokenStore`/`LockoutStore`/
`AuthEventSink` are pure `Protocol`s a framework adapter implements):

- **Single-use tokens** (`SingleUseTokenService`). `issue(user_id,
  purpose, ttl)` mints a `secrets.token_urlsafe(32)` raw token (~256 bits
  of CSPRNG entropy), persists only its SHA-256 hash (`hash_token` — the
  SAME fast-hash-not-a-slow-KDF reasoning `RefreshRecord` already
  documents: a high-entropy, module-generated value, not a low-entropy
  human-chosen secret), and returns the raw token. `consume(raw,
  purpose)` looks it up by hash and raises `InvalidSingleUseToken` for
  ANY of: unknown hash, already-used (`used_at` set — the row is RETAINED
  on consumption, exactly `RefreshRecord`'s "retain, don't delete"
  posture, so a second presentation is recognized as reuse), expired, or
  a `purpose` mismatch (a `"verify"` token presented to a reset flow, or
  vice versa) — all four collapse to the SAME exception and message,
  mirroring `InvalidCredentials`'/`TokenReused`'s own "don't leak which
  specific reason" posture.
- **Lockout** (`LockoutPolicy`). Pure counting/threshold logic over
  `LockoutStore`'s dumb persistence: `max_failures` consecutive failures
  for one `account_key` within a rolling `window` locks it for
  `lockout_duration` (re-armed on every subsequent failure while still
  locked). `AuthService.login`'s OPTIONAL `lockout=` parameter (`None` by
  default — every prior behavior is unchanged unless a project passes
  one) consults it BEFORE spending a real Argon2id verify on a locked
  account, and `AccountService.reset_password` — if given the SAME
  `LockoutPolicy` (or at least one built against the same underlying
  store) — clears it on a successful reset, so a user who tripped
  lockout guessing, then reset their password, isn't left blocked for the
  remaining cooldown despite now holding the correct password. A
  deliberately-accepted non-atomic read-modify-write relaxation (see
  `LockoutPolicy`'s own docstring) — at absolute worst it delays exactly
  when a lock becomes visible by a small, bounded amount; it can NEVER
  let a wrong password succeed.
- **Email** (`EmailSender` / `EmailMessage`). `AccountService` builds a
  plain-text `EmailMessage` (never HTML — no templating/injection
  surface) with a link containing the raw token in the URL **fragment**
  (`{frontend_base_url}/verify-email#token=<raw>` /
  `.../reset-password#token=<raw>`) — deliberately never a query string,
  since a fragment is never sent to the server and is typically excluded
  from `Referer` headers and access/proxy logs, keeping a single-use,
  bearer-credential-equivalent token out of exactly the places a query
  string routinely ends up. `ConsoleEmailSender` (the one shipped
  implementation) logs the message, INCLUDING the raw token — **DEV/TEST
  ONLY**; a project's own environment branch (never anything in this
  component) is what must ensure it's never constructed in production. A
  real implementation (SMTP, SES, Postmark, ...) is application/
  infrastructure code, not part of this framework-neutral core — see
  `backend/fastapi`'s `app/core/security/auth/stores.py:
  get_email_sender()` for a reference `SmtpEmailSender`.
- **`request_password_reset(email)` never raises and never reveals
  account existence** — the caller (an HTTP route) always returns the
  SAME response either way (a project's own 202-always convention — see
  `backend/fastapi`'s `POST /auth/request-password-reset`), extending
  `InvalidCredentials`'s user-enumeration defense to the "forgot
  password" flow, historically an even more common enumeration vector
  than login itself.
- **`reset_password` revokes EVERY refresh-token family the user has**
  (`RefreshTokenStore.revoke_all_for_user`, added alongside
  `revoke_family` specifically for this) — every device/session logged
  out, not just the one that requested the reset, since whatever was true
  about the account's security under the OLD password can no longer be
  assumed once it's been reset.
- **`AuthEventSink`** (optional on both services, `None` by default) lets
  a project emit `auth.login`, `auth.lockout.triggered`,
  `auth.email.verify_requested`/`verified`/`verify_failed`,
  `auth.password.reset_requested`/`completed`/`failed` without this
  module importing an audit-logging component directly — a thin adapter
  forwards `emit(action, *, actor, outcome, **extra)` to whatever a
  project's own audit sink expects (see `backend/fastapi`'s
  `AuditAuthEventSink`).

## Cookie/CSRF transport: double-submit cookies (Stage 5d, #46)

`_core.py`'s `AuthService`/`TokenService` mint JWTs but have no opinion on
HOW they travel between client and server — bearer-token auth (an
`Authorization` header a client must deliberately attach on every
request) is the path `fastapi.py`/`django.py` already wired in Stage
5a/5b. `_cookies.py` adds a SECOND, opt-in transport for a project that
instead wants the refresh token (and CSRF token) to travel as cookies —
purely additive, and it does NOT touch `_core.py`, the bearer-token path,
or either adapter's existing `AUTH_ERROR_HTTP` entries.

**Why cookies need a CSRF defense that bearer tokens don't.** A cookie is
attached by the browser AUTOMATICALLY to every matching-origin request —
including one a malicious cross-site page triggers without the victim's
knowledge (classic CSRF). A bearer token in an `Authorization` header has
no such automatic attachment; JavaScript on a different origin cannot read
this app's `Authorization` header value to forge one. That is why CSRF
defense belongs ONLY on the cookie path.

**The double-submit-cookie pattern.** On login/refresh, the server sets
TWO cookies: the refresh token (`HttpOnly`, unreadable to JS) and a CSRF
token (`_cookies.generate_csrf_token()` — a `secrets.token_urlsafe(32)`
value, independent of the JWTs, never persisted server-side) that is
deliberately NOT `HttpOnly`, so the SPA can read it via `document.cookie`
and echo it back as an `X-CSRF-Token` request header on every
state-changing request. `_cookies.verify_double_submit(*, csrf_cookie,
csrf_header)` is the server-side check: it raises `CsrfValidationError`
unless the header is present and non-empty, the cookie is present, AND
`hmac.compare_digest(csrf_header, csrf_cookie)` is `True` — a
CONSTANT-TIME comparison, never `==` (see that function's own docstring
for the timing-side-channel reasoning). All four failure modes (missing
header, blank header, missing cookie, mismatch) collapse to the SAME
generic exception and message — mirroring `InvalidCredentials`/
`InvalidToken`/`InvalidSingleUseToken`'s own "don't leak which specific
reason" posture elsewhere in this component.

A forged cross-site request can make the browser ATTACH the CSRF cookie
(cookies go out regardless of origin) but the attacker's page cannot READ
its value (same-origin `document.cookie` restriction) to also forge the
matching header — so a forged request always arrives with the cookie
present and the header missing or wrong, which `verify_double_submit`
rejects.

**Cookie flags, and why each one.** The four pure builders
(`build_refresh_cookie_kwargs`, `build_csrf_cookie_kwargs`,
`clear_refresh_cookie_kwargs`, `clear_csrf_cookie_kwargs`) all return the
SAME framework-neutral flag set except `httponly` and `value`/`max_age`:

| Flag | Value | Why |
| --- | --- | --- |
| `path` | `/auth` | The cookie is attached ONLY to `/auth/*` requests (login/refresh/logout) — never item/health/admin routes, shrinking both the leak surface and which routes even need the CSRF check. |
| `secure` | `True` | Never transmitted over plain HTTP — a refresh/CSRF token sent in plaintext is as good as published. |
| `samesite` | `"lax"` | Withheld on cross-site sub-resource/POST requests (the CSRF vector) while still attached on a top-level cross-site navigation (an emailed link), so `AccountService`'s verify/reset-link flows keep working. `Strict` would break those links; `None` would re-open the exact cross-site-send exposure `Lax` exists to close. Composes with `verify_double_submit` as DEFENSE IN DEPTH, not a substitute — an older browser or edge case that lets a `SameSite`-blocked cookie through anyway still fails the double-submit check, since the attacker's page still can't forge the matching header. |
| `httponly` | `True` (refresh) / `False` (CSRF) | The refresh cookie is invisible to JS (including XSS-injected JS) — the single most sensitive credential this component mints. The CSRF cookie MUST be readable — the SPA has to echo it back as a header; that's the entire double-submit mechanism. |

`max_age` is passed through on the two `build_*` functions (typically the
refresh token's own TTL in seconds); the two `clear_*` functions hardcode
`max_age=0`, which expires the cookie immediately (`Max-Age=0` is the
standard RFC 6265 "delete this cookie now" mechanism) — used by
`clear_auth_cookies` on logout.

**Adapter glue (`set_auth_cookies`/`clear_auth_cookies`/
`read_refresh_cookie`/`enforce_csrf`)** is IDENTICAL in shape across
`fastapi.py` and `django.py` — each maps `_cookies.py`'s framework-neutral
dicts/reads onto its own `Response.set_cookie(...)`/`Request.cookies` (or
Django's `request.COOKIES`) — never called by anything in this component
itself; a later stage's route/view handlers call them. `enforce_csrf`
must be called ONLY from the cookie-authenticated path, never from the
bearer-token path (`build_get_current_principal`/`resolve_principal`),
which has no CSRF exposure to begin with.

## OAuth 2.0/OIDC social login: PKCE, account linking, the unverified-email defense (`#96`)

`_oauth.py` (a THIRD framework-neutral file alongside `_core.py`/
`_cookies.py`, same "additive, standalone-importable, zero framework
import" posture) adds the authorization-code + PKCE flow for federated
login (Google, GitHub, Apple), wired by `references/recipes/
social-login.md`. It does NOT invent a fourth session/token shape: the
sole way it produces a session is `_core.py`'s new `AuthService.
issue_session(user)` (added alongside this file, minimal and additive —
`login`/`refresh`/`logout`/`resolve_access`/`_mint_and_persist` are
untouched), so a federated login issues the byte-for-byte identical
access/refresh JWT shape, cookie or bearer transport, and refresh-rotation
state machine a password login does.

**PKCE is mandatory for every provider, never optional** — `S256` only
(no `plain` method), per current OAuth 2.0 Security BCP (RFC 9700)
guidance to require it for every client type. **State and nonce are
always generated and checked** — `state` defends the redirect against
login CSRF, `nonce` (for Google/Apple's `id_token`) binds the token to
this exact authorization request. None of `state`, `nonce`, or the PKCE
`code_verifier` is ever placed in a URL query string that survives past
the authorization redirect, and neither the resulting access/refresh
token pair nor any OAuth credential is ever written to `localStorage` —
they inherit the SAME in-memory-access-token / `HttpOnly`-cookie-or-
SecureStore-refresh-token posture `end-to-end-auth` already establishes.

**The account-linking rules — and the unverified-email attack they
close.** `OAuthAccountService.resolve_or_link` resolves a verified
`OAuthIdentity` to a local `UserRecord` in this order: (1) an existing
`(provider, subject)` link, if one exists, wins outright — no email
re-check; (2) otherwise, an email match against an existing `UserStore`
row is auto-linked **only if both** the incoming identity's email AND the
existing account's own email are independently verified — if either is
not, `UnverifiedEmailAccountConflict` is raised instead of guessing; (3)
otherwise, a brand-new account is created. See `UnverifiedEmailAccountConflict`'s
own docstring in `_oauth.py` for the two symmetric attack shapes this
refusal closes (an attacker's unverified-email OAuth identity claiming a
victim's real account; an attacker's unverified local pre-registration of
a victim's email later hit by the victim's own genuinely-verified OAuth
login) — this is `references/security/secure-baseline.md`'s
"Authentication & authorization" section applied to the specific hazard
federated identity introduces that a password-only flow never has to.

**What is deliberately NOT in this file** (app-level, same split
`EmailSender`/`ConsoleEmailSender` already establishes for outbound
email): the actual HTTP calls — exchanging an authorization `code` for
tokens at a provider's token endpoint, fetching/verifying an OIDC
`id_token`'s signature against the provider's JWKS (Google/Apple, via
PyJWT's `PyJWKClient` — already a dependency via `TokenService`, no new
library) — and GitHub's plain-OAuth2 `GET /user` + `GET /user/emails`
calls to derive `email`/`email_verified` (taking ONLY the `primary &&
verified` entry). `references/recipes/social-login.md` shows the concrete
calls; `_oauth.py` starts one step later, at an already-verified
`OAuthIdentity`.

**Apple's two documented quirks**, both surfaced on `OAuthProviderConfig`/
`OAuthIdentity` rather than hidden: `response_mode="form_post"` — Apple
POSTs its callback body instead of a query-string redirect, so a route
wiring Apple's callback must accept a form body, not `request.query_params`;
and `OAuthIdentity.name`, which Apple returns **only on a user's very
first-ever grant to this app** — never again on subsequent logins, even if
the user later changes their Apple ID name. `resolve_or_link` never
overwrites an already-known name with a later `None`; the app-level
`UserStore.create`/`update` call the recipe describes is what actually
persists it on that first grant.

## Exception hierarchy → ErrorCode mapping (for the framework adapter)

This module raises its OWN exceptions rather than importing
`error-envelope/`'s `AppError`/`ErrorCode` directly — keeping `_core.py`
importable with zero framework/app-layer dependencies. A framework
adapter's exception handler maps each one onto that LOCKED, closed enum
(which this component does NOT extend):

| Exception | Maps to `ErrorCode` | HTTP status |
| --- | --- | --- |
| `InvalidCredentials` | `unauthenticated` | 401 |
| `InvalidToken` | `unauthenticated` | 401 |
| `TokenReused` | `unauthenticated` | 401 (same as `InvalidToken` — see below) |
| `EmailAlreadyExists` | `conflict` | 409 |
| `InvalidSingleUseToken` | `unauthenticated` | 401 (same generic shape — see "Account lifecycle" above) |
| `InvalidSession` (`_sessions.py`) | `unauthenticated` | 401 (ONE exception for missing/blank/unknown/revoked/idle-expired/absolute-expired/deleted-user — see "Server-side sessions" above; the specific reason goes to `AuthEventSink` as `auth.session.rejected`, never to the wire) |
| `CsrfValidationError` (`_cookies.py`, Stage 5d) | `permission_denied` | 403 (see "Cookie/CSRF transport" above — a valid cookie but a failed double-submit check is an authorization, not authentication, failure) |
| `OAuthStateMismatch` (`_oauth.py`, `#96`) | `unauthenticated` | 401 (the OAuth-flow CSRF/replay defense — a callback whose `state` doesn't match is treated the same as any other failed authentication attempt) |
| `OAuthNonceMismatch` (`_oauth.py`, `#96`) | `unauthenticated` | 401 (same reasoning as `OAuthStateMismatch`, applied to the OIDC `id_token`'s `nonce` claim) |
| `UnverifiedEmailAccountConflict` (`_oauth.py`, `#96`) | `conflict` | 409 (an account with this email already exists; see `_oauth.py`'s own docstring for why this refuses to guess rather than picking a side) |

`TokenReused` and `InvalidToken` deliberately map to the SAME code and
the same generic message on the wire — a client (attacker or otherwise)
holding a stolen-but-already-rotated refresh token must not be able to
distinguish "reuse was detected and your whole session was just killed"
from "this token was simply invalid," since that distinction would
confirm reuse detection exists and just fired. A framework adapter that
wants reuse events flagged for a human should log `TokenReused`
specifically (an audit-logging component, e.g. `security/audit-logging/`
in this catalog, is the right place for that signal) — never surface it
differently on the response body/status than any other auth failure.

## Testing

`tests/test_sessions.py` (48 tests) covers `_sessions.py`:
`generate_session_id` (URL-safe, high-entropy, collision-free across 500
draws, and asserted to contain no `.` — i.e. structurally not a JWT);
`create` (persists ONLY the hash, sets both deadlines off the injected
clock, stores no roles field, mints a fresh id every time — the fixation
defense — and emits `auth.session.created`); `IssuedSession.max_age_seconds`
(measured to the ABSOLUTE deadline, not the idle one, and clamped at 0);
and — the crown jewel, mirroring `test_core.py`'s emphasis on reuse
detection — `resolve`'s full state machine: the happy path, **the
principal asserted duck-type-compatible with `AccessClaims` on
`sub`/`roles`** (the property that lets one `require_roles` gate and one
set of route handlers serve both transports), and every rejection path
individually — missing cookie, blank cookie, unknown id, **a revoked
session failing on the very next request** (the load-bearing test for
preferring sessions over JWTs), the idle deadline at and one second before
its boundary, activity sliding that deadline forward across three hours of
45-minute gaps, **the absolute ceiling killing a continuously-used session
at exactly 24 hours** despite never being idle, and a session whose user
was deleted — plus an assertion that all six raise the IDENTICAL message,
not merely the same type. Live-role reads are covered in both directions
(a revoked role stops authorizing on the next request; a granted one
starts), as is the `touch_interval` bound (50 seconds of traffic produces
zero writes; a 2-minute gap produces exactly one; `last_seen_at` is never
ahead of the clock, so the bound can only expire early, never extend).
`rotate` covers new-id/old-id-dead, **the replacement inheriting the
original absolute deadline** (the regression test against
rotation-as-renewal), and refusing to resurrect a revoked or expired
session. `revoke` covers idempotence, never raising on missing/blank/
unknown/garbage ids (so logout is neither fragile nor an existence
oracle), and retaining the row rather than deleting it; `revoke_all_for_user`
kills every device while leaving another user's sessions untouched.
Rounding it out: the audit sink records the specific rejection reason the
wire response withholds, constructor validation rejects zero/negative
TTLs, both adapters map `InvalidSession` to `(401, "unauthenticated")`
identically, and a static source check asserts `_sessions.py` imports no
framework (and no `jwt`).

`tests/test_core.py` (54 tests) covers: `PasswordService` (hash≠
plaintext, verify true/false, malformed-hash handling, `needs_rehash`
false-on-fresh/true-after-a-parameter-change, `dummy_verify` never
raising); `TokenService` (access/refresh round-trip, unique `jti` per
mint, tampered signature rejected, expired access AND refresh tokens
rejected via the injected clock, valid-right-up-to-the-ttl-boundary,
wrong secret rejected, issuer mismatch rejected, access-as-refresh and
refresh-as-access both rejected, malformed token strings rejected,
empty-signing-key construction rejected, `hash_token`'s determinism/
uniqueness/hex-format); `AuthService.register` (creates a user, duplicate
email raises, email normalization on both write and lookup, roles
stored); `AuthService.login` (success returns a usable pair, unknown
email raises `InvalidCredentials` while ACTUALLY exercising
`dummy_verify()` — asserted via a spy, wrong password raises the SAME
exception type+message as unknown email, a refresh record is persisted
on success); and — the crown jewel — `AuthService.refresh`'s full state
machine: happy-path rotation (new pair differs, old row's `used_at` set,
new row present and unused, same family), **reuse detection revoking the
entire family including the just-minted valid child** (the load-bearing
regression test), refresh against an already-revoked family, an unknown-
but-validly-signed token (asserting `revoke_family` was NOT called),
expired rows, a multi-hop rotation chain staying in one family, and
type-confusion (an access token presented to `refresh()`); plus
`AuthService.logout` (revokes the family, subsequent refresh with any
family token fails, idempotent, a garbage/unknown/access token doesn't
raise); and `AuthService.resolve_access` (valid returns claims with
roles, invalid/wrong-type/expired all raise `InvalidToken`).

`tests/test_cookies.py` (40 tests, Stage 5d, #46) covers `_cookies.py`
exhaustively: `verify_double_submit` (a valid matching pair passes;
missing header, blank header, missing cookie, blank cookie, mismatch, and
both-missing each raise `CsrfValidationError`; every failure mode raises
the IDENTICAL exception message, not just type; two equal-length-but-
different strings are rejected — not just a length check; a spy on
`hmac.compare_digest` confirms it, not `==`, is what's actually called;
and confirms the short-circuit means `compare_digest` is never invoked at
all when the header or cookie is simply missing); `generate_csrf_token`
(URL-safe, high-entropy, no collisions across calls); all EIGHT cookie-
kwarg builders' EXACT flags — the four refresh-path builders (`path=/auth`)
and the four session-path builders (`path=/`), with `httponly=True` on
each credential cookie and `httponly=False` on each CSRF cookie, and
`secure=True`/`samesite=lax` throughout; the clear variants' `max_age=0`,
`value=""`, and — a real footgun, asserted — each clear instruction
carrying the SAME `path` as the set it must match, since a browser
silently ignores a mismatched delete; the two `csrf_token` builders
asserted to differ in `path` and NOTHING else (the collision documented in
`_cookies.py`'s "running both paths at once"); `max_age` passed through
unchanged; the cookie-name and cookie-path constants;
`CsrfValidationError` IS an
`_core.AuthError` subclass; and — loaded against both real framework
adapters — `fastapi.py`'s and `django.py`'s `AUTH_ERROR_HTTP` tables both
map `CsrfValidationError` to `(403, "permission_denied")`, identically.
Also a static-source regression check that `django.py` contains no
`rest_framework` import statement.

`tests/test_oauth.py` (`#96`) covers `_oauth.py`: `state`/`nonce`
generation (high-entropy, unique) and verification (every missing/blank/
mismatched combination raises, `OAuthStateMismatch`/`OAuthNonceMismatch`
kept as distinct types); `generate_pkce_pair` (RFC 7636 `S256` transform
matched byte-for-byte against a hand-computed digest, uniqueness,
unpadded base64url); `build_authorization_url`/`start_authorization`
(PKCE challenge present, verifier NEVER present in the URL, no
`client_secret` anywhere, Apple's `response_mode=form_post` +
`extra_authorize_params` both honored); and — the crown jewel, mirroring
`test_core.py`'s own emphasis on reuse detection —
`OAuthAccountService.resolve_or_link`'s full account-linking state
machine: a brand-new identity creates an account (verified or
unverified, matching the provider's own claim); a known `(provider,
subject)` link short-circuits straight to its user WITHOUT re-deriving
identity from a since-changed email; a verified identity auto-links onto
a verified existing account; **both unverified-email-attack shapes are
asserted to raise `UnverifiedEmailAccountConflict`** (an unverified
identity against a verified existing account, AND a verified identity
against an unverified existing account — the load-bearing regression
tests for this recipe); a stale link (target user deleted) falls back to
email resolution instead of permanently stranding the identity; a
newly-created account's password hash is a real Argon2id hash of a
random, discarded value that can never verify true against anything;
and `complete_login` is asserted to both return a real, persisted
`TokenPair` and to emit `auth.login` tagged `method="oauth"` through
`AuthService.issue_session`.

All four test modules run together (199 tests). The invocation needs the
real `fastapi` package, since `tests/conftest.py` loads `fastapi.py`/
`django.py` — see that file's own docstring; `django.py` itself needs no
`django` package import, so no `django` pin is required here:
```
uv run --python 3.13 --with pyjwt==2.13.0 --with argon2-cffi==25.1.0 --with fastapi \
  --with pytest --with pytest-asyncio -- \
  pytest templates/components/security/auth/tests/ -q
```
(async tests use explicit `@pytest.mark.asyncio` markers — pytest-asyncio's
default "strict" mode picks them up with no extra `--asyncio-mode` flag or
ini configuration needed, matching this catalog's `db-session` component.)

## Judgment calls

- **Server-side sessions are the DEFAULT for browser clients; JWT is the
  documented exception.** The comparison is in `_sessions.py`'s module
  docstring and summarized at the top of this README. The short version:
  a JWT's defining property — valid because it says it is, with no server
  in the loop — is exactly wrong for a browser session, where logout,
  bans, and privilege revocation must take effect immediately rather than
  after a TTL elapses. The JWT path is not deprecated and is not going
  away: it stays the correct choice for a native/mobile client (real
  OS-backed secret store, no ambient-cookie problem, cookies handled
  poorly by native HTTP clients) and for service-to-service callers (no
  user session exists to look up). What changed is which one a project
  reaches for without having to argue about it.
- **The cost of sessions is a store read per request, accepted
  deliberately.** `resolve` does two reads: the session row and the user
  row. That is the honest price of putting the server back in the loop,
  and a browser-facing app already queries a database several times per
  request. The `touch_interval` bound keeps the WRITE side from scaling
  with traffic, which is the failure mode that actually bites; the read
  side is a primary-key lookup. A project that measures this as a real
  bottleneck caches the user lookup with a staleness bound it chooses —
  it does not denormalize roles onto the session row (see next point).
- **Roles are resolved live on every request, never snapshotted onto the
  session row.** Caching roles would be faster and would reintroduce the
  exact staleness window sessions exist to close — and over a session's
  much longer lifetime (hours to days) than a JWT's own short access TTL
  (minutes), making it strictly worse than the thing being replaced. A
  session that cannot revoke a privilege immediately has given up most of
  its reason to exist.
- **Two deadlines (`idle_ttl` AND `absolute_ttl`), not one.** Sliding
  expiry alone lets an attacker keep a stolen session alive forever with a
  periodic request — it rewards precisely the party who is actively using
  the stolen cookie. A fixed lifetime alone logs out an active user
  mid-work and does nothing about an abandoned session on a shared
  machine. Each closes the other's gap, so both are checked on every
  resolve and neither can rescue the other.
- **`rotate` inherits the original absolute deadline instead of
  recomputing it.** Rotation is a security operation at a privilege
  boundary, not a session renewal. If it reset the ceiling, anything that
  could induce a rotation — including a caller rotating on a schedule
  "for safety" — would become a mechanism for extending a session
  indefinitely, defeating `absolute_ttl` entirely. `last_seen_at` IS
  reset, since the rotating request is itself activity.
- **`SessionService.revoke` never raises, on anything.** A logout that
  errors on a stale cookie is fragile (a double-clicked button, a retried
  request, a client clearing state at startup), and a logout that
  distinguishes "that was a real session" from "that was never a session"
  is an existence ORACLE an attacker can test stolen cookie values
  against. Both problems are closed by the same decision, which mirrors
  `AuthService.logout`'s existing contract.
- **The session cookie is `Path=/`, and that obliges CSRF everywhere.**
  Unlike the `Path=/auth` refresh cookie, a session cookie must reach every
  route to authenticate it — so every state-changing route carries an
  ambient credential and is a CSRF target. This is a real cost of session
  mode, stated as a checklist item ("enforce CSRF on every unsafe-method
  request") rather than left to be inferred. It is worth paying because
  the defense is fully mechanized — double-submit plus `SameSite=Lax`,
  both already implemented here — whereas a JWT's central weakness
  (un-minting a token) has no mechanization available at all.
- **`_sessions.py` is a separate file that imports no PyJWT.** Same
  reasoning `_cookies.py` was split out under: a project on the session
  path can vendor `_sessions.py` + `_cookies.py` + an adapter and never
  take a JWT dependency, while `_core.py`'s reviewed refresh-rotation
  state machine stays zero-diff. The split is visible in the file layout
  rather than only in prose.
- **Shipped `_core.py` alone first, `fastapi.py` in a separate follow-up
  commit, `django.py` deferred a further stage still — not all three
  (`_core.py`+`fastapi.py`+`django.py`) in one commit like every other
  dual-framework component in this catalog (`rate-limiting/`,
  `security-headers/`, ...).** This component's core is unusually
  security-sensitive (Stage 5a's whole point was proving the
  reuse-detection state machine exhaustively in isolation before any
  framework code touched it) — splitting "prove the core is correct" from
  "wire a FastAPI adapter" into two pieces of work was judged the right
  call specifically for this component. `django.py` landed a full stage
  later still (Stage 5b, #44) — until then, a project on the Django track
  could vendor `_core.py` only, implementing its own adapter by hand, same
  as any other catalog component before its second framework lands.
  `django.py`'s own shape deliberately mirrors `fastapi.py`'s (same
  `AUTH_ERROR_HTTP` table, same `InsufficientRole`/role-membership
  semantics) even though its mechanics differ (awaited helper functions,
  not `Depends()`-composed dependencies) — see `django.py`'s own module
  docstring for that mechanical difference and why it doesn't change what
  either adapter actually enforces.
- **Expiry checked by hand against an injected `now`, not PyJWT's
  built-in `verify_exp`.** See "Tokens" above — PyJWT has no parameter to
  substitute a fake "current time" into its own exp/iat validation, so
  relying on it would make this component's expiry tests either flaky
  (racing the real system clock) or untestable without real sleeps. Both
  `TokenService` and `AuthService` take the SAME injected `now` for this
  reason — a framework adapter should pass one shared callable to both.
- **A fast hash (SHA-256), not a KDF, for refresh tokens.** See "Refresh-
  token storage" above — the entropy source differs fundamentally from a
  human-chosen password, so the threat a slow KDF defends against
  (offline brute force of a low-entropy secret) doesn't apply here, and
  paying Argon2id's cost on every refresh call would be pure overhead.
- **`TokenReused` is a distinct exception type from `InvalidToken`, but
  maps to the identical wire response.** Keeping them as separate Python
  exception TYPES (rather than one `InvalidToken` with a `reused: bool`
  flag) lets a framework adapter's exception handler branch internally —
  e.g. to write a distinct audit-log entry for reuse specifically — while
  still rendering the exact same `ErrorEnvelope`/401 on the wire either
  way. Collapsing them into one exception type would make that internal
  branching (log differently, respond identically) awkward without an
  extra flag; keeping the flag out of the type and out of the response
  keeps the wire contract simple while the Python-level distinction stays
  available to whoever wants it server-side.
- **Reuse revokes the WHOLE family, not just the reused token.** An
  attacker holding a stolen refresh token and the legitimate client both
  descend from the same family by the time reuse is detected — revoking
  only the specific token that got reused would still leave whichever
  side currently holds the live, rotated-forward token logged in, which
  could be the attacker. Full-family revocation forces BOTH sides back
  through a real login, the only response that can't leave an attacker
  quietly still authenticated.
- **`register`/`login` normalize email via `.strip().lower()`, applied
  identically at both write and lookup time.** Without this, a
  case/whitespace variant of an existing email (`"Alice@Example.com "`)
  could register as a distinct account even though most mail providers
  deliver it to the same inbox as the canonical form — a real account-
  confusion/duplicate-account footgun, not just a cosmetic one.
- **Cookie/CSRF transport shipped as a SEPARATE file (`_cookies.py`), not
  folded into `_core.py` (Stage 5d, #46).** `_core.py`'s reviewed
  refresh-rotation state machine is this component's security-critical
  core and was deliberately kept ZERO-diff by this stage — cookie/CSRF is
  pure TRANSPORT (how a token travels), completely orthogonal to
  `AuthService`'s token-lifecycle logic (what a token IS and when it's
  valid). A second framework-neutral file, imported by both adapters
  exactly the way each already imports `_core.py`, keeps that separation
  explicit in the file layout itself rather than merely in prose — and
  means a project that never adopts the cookie path can skip vendoring
  `_cookies.py` entirely with zero effect on `_core.py`/the bearer-token
  path.
- **`CsrfValidationError` maps to `permission_denied` (403), not
  `unauthenticated` (401).** A double-submit failure happens on a request
  that already carries a facially valid cookie-borne credential — what's
  missing is proof THIS request was authorized by whoever holds that
  cookie, not proof of identity itself. That is an authorization
  distinction, matching `error-envelope/errors.py`'s own
  `PermissionDeniedError` docstring ("authenticated, but not authorized
  for this action") more precisely than `UnauthenticatedError`'s ("no
  valid credentials presented at all") would.
- **The double-submit check alone raises on ANY of missing header, blank
  header, missing cookie, or mismatch — never distinguishing which.**
  Same "don't leak which specific reason" posture `InvalidCredentials`/
  `InvalidToken`/`InvalidSingleUseToken` already establish elsewhere in
  this component — telling a probing attacker exactly which half of the
  double-submit pair was wrong narrows what they'd try next for no
  defensive benefit.
- **`generate_csrf_token`/`verify_double_submit` never touch `_core.py`'s
  `TokenService`/JWTs at all.** The CSRF token is intentionally NOT a JWT,
  not signed, and not persisted server-side — its entire security
  property rests on "can the requester's page read this cookie back out
  of the browser," which has nothing to do with JWT signature
  verification. Reusing `TokenService` for it would suggest a coupling
  that doesn't exist and isn't needed.
