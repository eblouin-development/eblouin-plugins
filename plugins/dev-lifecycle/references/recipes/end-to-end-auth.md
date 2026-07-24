<!--
recipe: end-to-end-auth
applies-to:
  - backend block: fastapi OR django (one auth contract, either adapter)
  - frontend block: any React web block (session mode) — pairs with @repo/api-client
  - mobile block: any Expo block (bearer mode) — pairs with @repo/api-client
last-verified: 2026-07-24
provenance: manual
sources:
  - https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
  - https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html
  - https://docs.expo.dev/versions/latest/sdk/securestore/
  - https://pyjwt.readthedocs.io/en/stable/
  - references/wiring/auth-end-to-end.md
  - references/security/secure-baseline.md
  - references/compatibility-matrix.md
-->

# End-to-end auth

Wire register/login/logout with role-based access control across a scaffolded monorepo: one backend auth contract serving a React web SPA (server-side session, the default) and an Expo mobile app (bearer JWT) through the shared typed client. Everything here is **subordinate to the project's existing conventions** — when they conflict, the project wins.

## Contents
- What this wires
- Prerequisites
- Wire-up steps
- Security checklist
- Doc fragment

## What this wires
Applying this recipe gives a project working authentication end to end: users can register, log in, and log out; a `roles` claim/field gates protected routes with a `403` on the wrong role; and both a web SPA and a mobile app authenticate against the same backend, each with the credential and CSRF posture correct for its runtime — a revocable server-side session for the browser, a rotating JWT pair for the native app.

It **composes existing pieces** — it invents no new infrastructure:
- **`templates/components/security/auth/`** — the framework-neutral auth core: `_sessions.py` (opaque server-side sessions — `SessionService`: create/resolve/rotate/revoke, sliding idle + absolute expiry, live role resolution — THE default browser credential), `_core.py` (Argon2id hashing, PyJWT HS256 access/refresh tokens, rotation + reuse detection — the native/mobile and service-to-service path), its `_cookies.py` double-submit CSRF transport (covering both credential shapes), and the `require_roles`/`require_roles_either` RBAC gates. Vendored into the backend block.
- **A backend block** — `templates/backend/fastapi` or `templates/backend/django` — which hosts the vendored component, exposes `/auth/*` and the `/admin/ping` RBAC example, and owns the `UserStore`/`SessionStore`/`RefreshTokenStore` against the real DB.
- **`templates/components/security/cors-lockdown/`** — emits the credentialed CORS session mode requires.
- **`templates/packages/api-client/`** — the shared `@repo/api-client`; its `src/mutator.ts` implements all three transports behind `configureApiClient({ mode })`. `mode: "session"` (the web default) sends `credentials: "include"` and the CSRF echo on every unsafe method; `mode: "bearer"` (the mobile default, passed explicitly) is the JWT half.
- **A frontend block** (React web) and/or **a mobile block** (Expo) — the consumers that call `configureApiClient(...)`. The web block additionally pulls in `@repo/web-shared`'s session-mode `AuthProvider` (`templates/components/frontend/`).

The full conceptual model — the credential shapes and *why* session is preferred for browsers — is `references/wiring/auth-end-to-end.md`; this recipe is the ordered how-to.

## Prerequisites
- A scaffolded monorepo (Stage 1) with a **backend block** (`templates/backend/fastapi` or `templates/backend/django`) and the **`@repo/api-client`** package present, plus at least one consumer block (a React web app, an Expo app, or both).
- The **auth component vendored** into the backend block (its files copied under `app/core/security/auth/` for FastAPI or `core/security/auth/` for Django — `_core.py`, `_sessions.py`, `_cookies.py`, plus the framework adapter — with the app-level `UserStore`/`SessionStore`/`RefreshTokenStore` implemented against the project's ORM/session — the backend block's own reference wiring in `app/core/security/auth/stores.py`/`core/security/auth/stores.py`).
- A **PostgreSQL** database (matrix: **18.x**) with the users/sessions/refresh-tokens tables migrated.
- Runtime dependencies per `references/compatibility-matrix.md`: **argon2-cffi 25.1.x** (backend, always); **PyJWT 2.13.x** (backend, only if the JWT/bearer path is served — a session-only backend never imports it); **orval 8.22.x** + **@tanstack/react-query 5.101.x** + **React 19.x** (web/client); **Expo SDK 57** / **expo-secure-store** (mobile); **Django 5.2 LTS** + **DRF 3.17.x** on the Django track; **django-cors-headers 4.9.x** for that track's CORS.

## Wire-up steps
1. **Compose the auth component into the backend block.** Confirm `SessionService` is constructed once at startup with real idle/absolute TTLs (no signing key needed — a session id is opaque, not signed), and — if this deployment also serves a bearer/native client — that `AuthService`/`TokenService` are likewise constructed with a real signing key. Confirm `/auth/register|login|logout|me` plus the `/admin/ping` RBAC example route are mounted, and that the session-mode CSRF middleware (`SessionCsrfMiddleware` in both reference blocks) is registered — it must run on every unsafe-method request, not just `/auth/*`. Don't re-author the component — the backend block's own README documents its store/exception wiring; follow it.

2. **Set the backend auth config (secrets never inlined).** Provide these as environment/secret values (names from the FastAPI block's `app/core/config.py`; the Django track mirrors them):
   - `SESSION_IDLE_TTL_SECONDS` / `SESSION_ABSOLUTE_TTL_SECONDS` / `SESSION_TOUCH_INTERVAL_SECONDS` — the sliding idle deadline (default 12h), the hard ceiling (default 7d), and the write-rate bound on `last_seen_at` (default 60s). None of these is a secret.
   - `JWT_SIGNING_KEY` — the HS256 secret, needed only if the bearer/native path is served. Generate a high-entropy random value (e.g. `openssl rand -hex 32`); load it from the environment/secret store, **never** commit it. The app resolves it via the secrets-loading component, and it never appears in a `Settings` repr.
   - `JWT_ACCESS_TTL_SECONDS` / `JWT_REFRESH_TTL_SECONDS` — short access TTL (minutes), longer refresh TTL (days), for the bearer path.
   - `FRONTEND_BASE_URL` — the SPA origin that email-verification / password-reset links are built against (default `http://localhost:5173`; override per environment).
   - `SMTP_HOST` (+ `SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`EMAIL_FROM`) — required in any real environment for verification/reset email delivery; unset falls open to a dev-only console sender that logs the raw token. **A real `SMTP_HOST` is a required deploy step, not a code change.**

3. **Wire the client — no mode choice needed for web, an explicit one for mobile.**
   - **Web (session mode — the default):** call `configureApiClient({ baseUrl, mode: "session" })` once at app startup (before any generated hook fires). Mount `@repo/web-shared`'s `AuthProvider` inside a `QueryClientProvider`. There is no token to keep anywhere — the credential is the `HttpOnly` `session_id` cookie the browser holds and this code never reads.
   - **Mobile (bearer mode — pass it EXPLICITLY):** call `configureApiClient({ baseUrl, mode: "bearer" })`. Omitting `mode` is no longer safe: the *backend's* default is now session mode, so an unconfigured mobile client would be handed a session cookie it cannot properly store instead of the token pair its `AuthApi` expects. Store the refresh token in **Expo SecureStore** (`expo-secure-store`), keep the access token in memory, send it as `Authorization: Bearer`.

4. **Enable credentialed CORS.** Set `CORS_ALLOWED_ORIGINS` to the **explicit** web origin(s) — never a `*` wildcard, which is incompatible with `credentials: "include"`. This is required for session mode (the web default), not an opt-in the way it was for the superseded cookie-JWT path — `cors-lockdown` should already be flipped to `allow_credentials=True` for the web origin(s) with `X-CSRF-Token` in `allow_headers`. A mobile-only deployment leaves credentials off for the mobile origin (bearer needs none). Distinct allowlists per environment (dev/staging/prod).

5. **Seed an admin.** Roles are **never** settable over the wire — `POST /auth/register` has no `roles` field and always creates users with empty roles. Create the first admin server-side with the component's sanctioned `seed_admin(session, email, password)` path (a one-off script or a fixture; it commits immediately and is the only place `roles=["admin"]` is ever constructed). Verify with `GET /admin/ping`: an admin's credential → `200`, a non-admin's → `403`, no/invalid credential → `401` — in whichever mode(s) this deployment serves.

6. **Verify the security-critical behaviors.** Confirm session revocation is immediate: log out (or revoke via a password reset), then confirm the *very next* request 401s — no TTL window to wait out. Confirm CSRF is enforced on a non-auth, unsafe-method route (not just `/auth/*`) when a session cookie is present, and that a bearer-only request is never asked for a CSRF header. If the bearer/JWT path is also served, additionally confirm refresh **rotation + reuse detection** (a replayed refresh token returns `401` and revokes the whole family).

## Security checklist
- [ ] Session TTLs (`SESSION_IDLE_TTL_SECONDS`/`SESSION_ABSOLUTE_TTL_SECONDS`) are set to values appropriate for this app's sensitivity — shorter for a higher-stakes admin console.
- [ ] `JWT_SIGNING_KEY` (only if the bearer path is served) is high-entropy, loaded from the environment/secret store, and never committed or logged.
- [ ] Web holds no token anywhere — the session cookie is `HttpOnly; Secure; SameSite=Lax; Path=/`. Mobile keeps its access token in memory only and its refresh token in SecureStore — never in `localStorage`/`sessionStorage` on either.
- [ ] Session-mode CSRF double-submit is enforced on EVERY unsafe-method request app-wide (via a middleware, not a per-route call) — verified against a non-auth route, not just `/auth/refresh`/`/auth/logout`.
- [ ] CORS names explicit origins with `allow_credentials=True` — no `*` wildcard; distinct allowlists per environment.
- [ ] Logout (and a password reset) revoke the session on the very next request — no residual access window. If the bearer path is served, refresh rotation + reuse detection is also verified (replayed token → 401, family revoked).
- [ ] Admin role granted only via `seed_admin` server-side; `/auth/register` cannot self-grant roles; `/admin/ping` returns 403 on the wrong role in every mode this deployment serves.
- [ ] A real `SMTP_HOST` is configured in every non-dev environment (the console sender is dev-only and logs raw tokens).
- [ ] Auth endpoints (login, password reset) are rate-limited / lockout-guarded per `references/security/secure-baseline.md`.
- [ ] Mobile's `configureApiClient` call passes `mode: "bearer"` EXPLICITLY — never omitted, since the backend's own default is now session mode.

## Doc fragment
The portable fragment this recipe contributes to the project's root README when applied:

```markdown
### Authentication (end to end)
- **Setup:** Users register/login/logout against the backend auth component; role-based access control gates protected routes (`GET /admin/ping` is the reference). Web uses **server-side sessions** (the default) — an opaque, `HttpOnly; Secure; SameSite=Lax; Path=/` `session_id` cookie, CSRF double-submit enforced on every unsafe-method request app-wide — enabled via `configureApiClient({ baseUrl, mode: "session" })`. Mobile uses **bearer JWTs**, passed explicitly (`configureApiClient({ baseUrl, mode: "bearer" })`, since bearer is no longer the backend's own default) — access token in memory, refresh token in Expo SecureStore, `Authorization: Bearer`, refresh-token rotation with reuse detection, no CSRF. See `references/wiring/auth-end-to-end.md`.
- **Secrets:** `JWT_SIGNING_KEY` — HS256 signing secret for the mobile/bearer path, generate with `openssl rand -hex 32`, load from the environment/secret store (never commit); not needed if this deployment serves web only. `SMTP_HOST` (+ `SMTP_PORT`/`SMTP_USERNAME`/`SMTP_PASSWORD`/`EMAIL_FROM`) — email relay for verification/reset links; required in every non-dev environment.
- **Config:** `SESSION_IDLE_TTL_SECONDS`/`SESSION_ABSOLUTE_TTL_SECONDS`/`SESSION_TOUCH_INTERVAL_SECONDS` (session sliding/hard/write-rate deadlines), `JWT_ACCESS_TTL_SECONDS`/`JWT_REFRESH_TTL_SECONDS` (bearer path, short access/longer refresh), `FRONTEND_BASE_URL` (SPA origin for email links). `CORS_ALLOWED_ORIGINS` must name explicit web origin(s) with credentials enabled — never `*`.
- **Maintenance:** The first admin is created server-side with the auth component's `seed_admin(session, email, password)` (roles are never settable over the wire). Keep argon2-cffi (and PyJWT, if the bearer path is served) and the client (orval / @tanstack/react-query) on the versions pinned in `references/compatibility-matrix.md`; regenerate `@repo/api-client` after any auth-route change.
```

---
<!--
Recipe authored via the `recipe-author` skill (Stage 5d, #46). Updated for the
server-side-sessions-as-default migration. Composes existing catalog
components/blocks only — no new infrastructure. Every version-sensitive step
cites references/compatibility-matrix.md; every step defaults to the secure
option per references/security/secure-baseline.md.
-->
