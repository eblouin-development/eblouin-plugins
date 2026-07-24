<!-- fragment: block:components/security/auth -->

## Setup
Copy the `auth/` directory into `app/core/security/auth/` (or, on the
Django track, `core/security/auth/`). Ships **two authentication paths**:
server-side sessions (the default for browser clients) and JWT
access/refresh (for native/mobile clients and service-to-service callers).

`_sessions.py` — a framework-neutral, stdlib-plus-`_core`-only
`SessionService` over opaque, high-entropy session ids whose authority
lives entirely in a `SessionStore` row: revocable on the next request,
resolving roles live from `UserStore` so a privilege change takes effect
immediately, with both a sliding `idle_ttl` and a hard `absolute_ttl`, a
`touch_interval` write-rate bound, and `rotate()` for privilege
boundaries. **This is what a browser client should authenticate with** —
see this component's README's "Server-side sessions" section for the
resolve state machine and the wiring checklist. It imports no PyJWT, so a
session-only project takes no JWT dependency at all.

`_core.py` — a framework-neutral `PasswordService` (Argon2id) +
`TokenService` (PyJWT HS256 access/refresh) + `AuthService` orchestrator,
including refresh rotation with reuse detection. Still required on the
session path for `PasswordService`/`UserStore`/`AuthService.register`/
`login`'s credential verification — a session login verifies the password
through `AuthService` and then calls `SessionService.create(user)` instead
of minting tokens. `_cookies.py`
(Stage 5d, #46) — a SECOND framework-neutral file, stdlib-only, holding
the double-submit-cookie CSRF transport (`CsrfValidationError`,
`generate_csrf_token`, `verify_double_submit`, and the pure cookie-kwarg
builders for both the `Path=/` session cookie and the `Path=/auth` refresh
cookie) — `fastapi.py` — `build_get_current_session_principal` (the
session dependency factory), the `HTTPBearer` scheme,
`build_get_current_principal` (a dependency factory resolving a bearer
token to `AccessClaims`), `require_roles` (one role-gated dependency
factory that works against EITHER principal), `AUTH_ERROR_HTTP` (exception
type -> `(status, ErrorCode string)` table), and thin cookie/CSRF glue over
`_cookies.py` (`set_session_cookies`, `clear_session_cookies`,
`read_session_cookie`, `set_auth_cookies`, `clear_auth_cookies`,
`read_refresh_cookie`, `enforce_csrf`) — and `django.py` — the Django/DRF
equivalent: `resolve_session_principal(request, session_service)` /
`require_session_roles(...)` for the session path and
`resolve_principal(request, auth_service)` / `require_roles(request,
auth_service, *roles)` for the bearer path (all plain awaited helpers,
since Django/DRF has no `Depends()`-style auto-invoked injection point to
compose against), `InsufficientRole`, the identically-shaped
`AUTH_ERROR_HTTP`, and the SAME DRF-free cookie/CSRF glue surface as
`fastapi.py`'s own. Copy `_core.py` always, `_sessions.py` for the
(default) session path, `_cookies.py` whenever either cookie transport is
used, and only the adapter file(s) your track actually uses (a FastAPI
project never vendors `django.py`, and vice versa). Add an `__init__.py`
re-exporting the vendored files' public surface — see backend/fastapi's
`app/core/security/auth/__init__.py` (FastAPI track) or backend/django's
`core/security/auth/__init__.py` (Django track) for the exact shape.

Vendoring `_core.py`+`_sessions.py`+the framework adapter is NOT the whole
wiring job — a project still needs, as its OWN (non-vendored) app code:
`UserStore`/`SessionStore`/`RefreshTokenStore` implementations against a
real ORM/DB (these import the app's models, so they can never be part of
this vendored, framework-neutral component); `SessionService` construction
with real `idle_ttl`/`absolute_ttl` values (and, if the JWT path is also
served, `AuthService` construction with a real signing key via
`secrets-loading/`, never hardcoded — rotate per environment) at app
startup; real route handlers calling `AuthService.login` +
`SessionService.create` on the session path, or `AuthService`'s
register/login/refresh/logout/resolve_access on the JWT path; **CSRF
enforcement on every unsafe-method route** wherever session mode is used
(the session cookie is `Path=/`, unlike the `Path=/auth` refresh cookie);
and an app-level exception
handler registered for the `AuthError` base class that renders
`AUTH_ERROR_HTTP`'s mapping as the app's own `ErrorEnvelope` (catches
every subclass via one registration — Starlette-family frameworks walk an
exception's MRO against registered handlers; a DRF `EXCEPTION_HANDLER`
does the equivalent `isinstance` walk by hand). `pyjwt==2.13.*` and
`argon2-cffi==25.1.*` are already in `references/compatibility-matrix.md`'s
Backend — Python row; add the matching pins to the consuming backend's own
`pyproject.toml`/`requirements`.

**Reference implementations:** `backend/fastapi` (Stage 5a, #41) was the
first block to complete this wiring end to end — see that block's
README.md "Auth" section and `app/core/security/auth/stores.py` for a
concrete `UserStore`/`RefreshTokenStore` implementation, and `app/main.py`'s
`_auth_error_handler` for the exception-handler side. `backend/django`
(Stage 5b, #44) is the Django-track reference — see that block's
`core/security/auth/stores.py` for its Django-async-ORM `UserStore`/
`RefreshTokenStore` implementation and `core/exceptions.py` for the
DRF-side exception mapping.

**Account lifecycle (Stage 5c, #45):** `AccountService`/`LockoutPolicy`
(email verification, password reset, per-account lockout — see this
component's README's "Account lifecycle" section for the full seam list)
are composed ALONGSIDE `AuthService`, never touching `fastapi.py`/
`django.py` themselves — a project wires its own `SingleUseTokenStore`/
`LockoutStore` implementations, an `EmailSender` (a real one; never
`ConsoleEmailSender` outside dev/test), and an `AccountService` FastAPI/
Django dependency, alongside `AuthService`'s own. `backend/fastapi` is
again the reference implementation — see that block's README's "Account
lifecycle" subsection, `app/core/security/auth/stores.py`'s
`build_account_service`/`build_lockout_policy`/`get_email_sender`/
`AuditAuthEventSink`, and `app/api/deps.py`'s `get_account_service`/
`get_email_sender` (the latter a thin FastAPI-dependency wrapper around
the former, purely so a test can override it deterministically). Django
parity for this surface is pending — `backend/django/tests/
test_schema_conformance.py`'s `_PENDING_PARITY_OPS` tracks the three
still-unimplemented ops.

**OAuth 2.0/OIDC social login (`#96`):** `_oauth.py` — a THIRD
framework-neutral, stdlib-only file — adds PKCE (`S256`-only), state/
nonce generation and verification, provider-agnostic authorization-URL
building, and `OAuthAccountService` (the account-linking orchestrator,
including the unverified-email-account-takeover defense — see this
component's README's own "OAuth 2.0/OIDC social login" section). `_core.py`
gained one small, additive public method, `AuthService.issue_session(user)`,
so a federated login mints the EXACT SAME session/token shape a password
login does; `login`/`refresh`/`logout`/`resolve_access` are unchanged.
`references/recipes/social-login.md` is the wire-up: it composes this
file plus the existing `fastapi.py`/`django.py` bearer/cookie transport,
the app-level HTTP calls to each provider's token/userinfo/JWKS
endpoints, and the frontend/mobile provider-button + Expo AuthSession
pieces — no new catalog component was needed; `_oauth.py` vendors
alongside `_core.py` exactly like `_cookies.py` already does.

## Maintenance
`SessionService.resolve`'s state machine and `AuthService.refresh`'s
reuse-detection state machine are the two security-critical cores of this
component — re-run `tests/test_sessions.py` after any change to
`_sessions.py` (especially the revoked-session, both-deadline, live-role,
and rotation-inherits-the-absolute-deadline tests) and `tests/test_core.py`
after any change to `_core.py` (especially "reuse revokes the whole
family"), before shipping. A password reset, an account deactivation, and
a detected compromise must all call `SessionService.revoke_all_for_user`
alongside `RefreshTokenStore.revoke_all_for_user`; a project that adds a
new "kill this account's access" path and wires only one of the two has a
half-revoked account. `PasswordService.needs_rehash()` exists so a
project can tighten Argon2id's cost parameters over time and transparently
upgrade old hashes on next successful login, rather than a bulk
migration — wire that check into the framework adapter's login flow once
it exists. Re-verify the PyJWT/argon2-cffi pins against
`references/compatibility-matrix.md` on the same cadence as the rest of
the Backend — Python row. `OAuthAccountService.resolve_or_link`'s
unverified-email-attack defense (`tests/test_oauth.py`) is equally
security-critical to `_oauth.py` — re-run it after any change to the
account-linking logic, especially the "both sides must be verified before
auto-linking" tests.
