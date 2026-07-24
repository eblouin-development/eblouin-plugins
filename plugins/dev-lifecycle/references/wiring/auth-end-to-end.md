<!--
wiring: auth-end-to-end
covers: auth component (backend) <-> React web (session mode) <-> Expo mobile (bearer mode)
last-verified: 2026-07-24
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
  - https://cheatsheetseries.owasp.org/cheatsheets/JSON_Web_Token_for_Java_Cheat_Sheet.html
  - https://developer.mozilla.org/en-US/docs/Web/HTTP/Headers/Set-Cookie
  - https://docs.expo.dev/versions/latest/sdk/securestore/
  - references/security/secure-baseline.md
  - references/compatibility-matrix.md
-->

# Auth, end to end

**How one auth backend serves two very different clients** — a React web SPA and an Expo mobile app — over the *same* HTTP contract, with each client using the transport that's correct for *its* runtime. This is a wiring reference: it stitches together pieces that each have their own canon doc, and is **subordinate to the project's existing conventions** — when they conflict, the project wins.

The three pieces:
- **Backend** — the `templates/components/security/auth/` component, vendored into either the FastAPI block (`templates/backend/fastapi`) or the Django block (`templates/backend/django`). One contract, two credential shapes it can issue (`_sessions.py`'s opaque session, `_core.py`'s JWT access/refresh pair), served through two framework adapters (`fastapi.py`, `django.py`) that behave identically on the wire.
- **Web** — a React SPA importing `@repo/api-client` (`templates/packages/api-client`) in **session mode**.
- **Mobile** — an Expo app importing the same `@repo/api-client` in **bearer mode** (the library default, but Expo passes it explicitly — see "The two client modes" below).

## Contents
- The one thing to understand first
- The two client modes (and why)
- Login → logout, in each mode
- Where CSRF applies (and where it can't help)
- RBAC: roles → gate → 403
- CORS is part of session mode
- Wiring checklist
- Related canon

## The one thing to understand first
There is **one** backend auth contract, and it defaults to the credential that's right for a browser. The *client* can opt into a different transport; the backend serves whichever the request asks for, per request:

- **Login selects the mode.** `POST /auth/login` reads the `X-Auth-Mode` request header. Absent, `"session"`, or any unrecognized value → **session mode** (the default); the literal `"bearer"` → bearer JWTs; the literal `"cookie"` → the superseded JWT-refresh-in-a-cookie path (kept for a project mid-migration, see "The two client modes" below). A client asks for a non-default transport explicitly or gets the safer default.
- **Logout is multi-source.** `POST /auth/logout` decides per request by which credential is ACTUALLY present — the `session_id` cookie, then the `refresh_token` cookie, then the request body — never by a header the client declares. A forged or absent cookie cannot claim a path it does not hold; a genuine cookie-bearing browser request cannot accidentally fall onto the bearer path.

Everything below is a consequence of those two rules.

## The two client modes (and why)

### Web → session mode (the default, and the preferred posture)
| Concern | Web (session mode) |
| --- | --- |
| Credential | An opaque, `HttpOnly; Secure; SameSite=Lax; Path=/` `session_id` cookie the backend sets. JS **cannot read it**, and there is no token of any kind for this client to hold. |
| Login | `configureApiClient({ baseUrl, mode: "session" })` → client sends no `X-Auth-Mode` header (session is already the default). Response body is `{ access_token: "", refresh_token: "", token_type: "session" }` — genuinely empty, not a placeholder. |
| Requests | `credentials: "include"` so the browser attaches the cookies. |
| CSRF | Double-submit, on **every unsafe-method request** app-wide: the client echoes the non-`HttpOnly` `csrf_token` cookie as `X-CSRF-Token`. |

**Why:** the backend's session store is the sole source of truth for whether the credential is still valid, which is exactly what lets logout, an administrative ban, or a role change take effect on the client's **very next request** — a property no bearer token can offer, since the server has no say once one is minted. The dangerous class for a browser is still **XSS**, and session mode closes it more completely than a token-in-memory design ever could: there is nothing token-shaped in the JS heap for an injected script to read or exfiltrate at all, because the only credential is a cookie JS cannot access. See the auth component's `_sessions.py` module docstring for the full argument (sliding idle timeout, live role resolution, the store-read cost this trades for those properties).

### Mobile → bearer mode (the library default — but Expo sets it explicitly)
| Concern | Mobile (bearer mode) |
| --- | --- |
| Access token | In memory. |
| Refresh token | In **Expo SecureStore** (iOS Keychain / Android Keystore) — a real OS-backed secret store. |
| Login | `configureApiClient({ baseUrl, mode: "bearer" })` → client sends `X-Auth-Mode: bearer`, opting OUT of the backend's session default. Body returns the real access + refresh JWTs; app stores the refresh token in SecureStore. |
| Requests | `Authorization: Bearer <access>`; no cookies, no `credentials`. |
| CSRF | **None** — not needed (see below). |

**Why:** a native app has a real OS-backed secret store (Keychain/Keystore) and, crucially, **no ambient-cookie problem** — there is no browser to auto-attach credentials to a forged cross-site request, so CSRF simply doesn't exist as a class here. A server-side session's revocation advantage matters less here too: Expo has no cookie jar suited to `HttpOnly`/`SameSite` semantics, so bearer + SecureStore is both simpler and correct for this runtime, with the JWT refresh-rotation-with-reuse-detection state machine (`_core.py`) providing its own, transport-appropriate revocation story.

**`mode: "bearer"` must be passed explicitly, not omitted, even though it's the api-client library's own default.** That default exists because the shared mutator module can't detect which runtime it's running in and has to pick the choice that's *safe to get wrong* (see `@repo/api-client`'s own README) — it is not a statement that bearer is the backend's default, which it no longer is. Omitting `mode` on native would silently authenticate the app against a session cookie it cannot properly store, with `login()` returning empty token fields instead of the real pair `authApi.ts` expects.

### The superseded third mode: `"cookie"`
An earlier design put the JWT **refresh** token in an `HttpOnly; Path=/auth` cookie (access token still in memory, still returned in the body) — the right answer for browsers before this backend had server-side sessions, and strictly worse now: it still leaves a bearer access token in the JS heap, still can't be revoked before its TTL, and still needs an explicit refresh round-trip session mode doesn't. `mode: "cookie"` (or the legacy `cookieMode: true` client flag) still works, for a project mid-migration off it — new work should use session mode.

**The tradeoff in one line:** session mode trades "nothing token-shaped for JS to touch, and the server can kill it on the next request" for "every unsafe-method call needs the CSRF echo, app-wide"; bearer mode has neither the revocation property nor the CSRF exposure, which is exactly right for a runtime with no ambient-cookie problem to begin with.

## Login → logout, in each mode
Refresh-token rotation with reuse detection (`_core.AuthService.refresh`) still exists as a mode-independent, security-critical state machine — but only the JWT-issuing modes (`bearer`, `cookie`) ever call it. Session mode has no refresh endpoint at all: a session's sliding idle deadline advances as a side effect of the backend resolving the cookie on any authenticated request (see `_sessions.SessionService.resolve`'s `touch_interval`), so there is nothing for the client to call to keep the credential alive short of using the app normally.

### Session mode (web)
1. **Login** — `POST /auth/login`, no `X-Auth-Mode` header. Backend sets two cookies: `session_id` (`HttpOnly`, `Path=/`) and `csrf_token` (non-`HttpOnly`, so JS can read it to echo it). Response body carries no token at all.
2. **Every subsequent request** — `credentials: "include"` (browser sends the session cookie) plus, on any unsafe method, `X-CSRF-Token` = the `csrf_token` cookie's value. The backend enforces CSRF **first**, then resolves the session; a dead session (revoked, idle-expired, absolute-expired) is `401`, indistinguishable at the wire from "never had one" (see `_sessions.InvalidSession`'s own docstring).
3. **Logout** — `POST /auth/logout`, `credentials: "include"` + `X-CSRF-Token`, no body needed (`RefreshRequest.refresh_token` is optional for exactly this reason). CSRF is enforced first, then the session is revoked server-side (the half that actually matters) and both cookies are cleared. Idempotent past the CSRF gate: a stale/unknown session id still `204`s.

### Bearer mode (mobile)
1. **Login** — `POST /auth/login`, `X-Auth-Mode: bearer`. Body returns access + real `refresh_token`; app writes the refresh token to SecureStore, keeps access in memory.
2. **Refresh** — `POST /auth/refresh` with the refresh token in the request **body**. No cookie, no CSRF. Body returns the new access + new refresh token (rotation — the OLD refresh token is now dead and a second presentation of it revokes the whole family); app overwrites SecureStore with the rotated refresh token.
3. **Logout** — `POST /auth/logout` with the refresh token in the body. No CSRF. App clears SecureStore.

## Where CSRF applies (and where it can't help)
CSRF protection is enforced only where a cookie carries an ambient credential, and its *scope* differs by mode:

- **Login needs no CSRF** in any mode — it's authenticated by the credentials in the body (email + password), and there is no cookie yet for a forged request to ride.
- **Bearer mode needs no CSRF at all.** CSRF exploits the browser *automatically attaching ambient credentials* (cookies) to a cross-site request. A bearer token is attached *explicitly by the app's own code* in the `Authorization` header — an attacker's forged page cannot read it (it's not a cookie, and it's not exposed cross-origin) and the browser will not add it for them. No ambient credential ⇒ no CSRF.
- **Session mode needs CSRF on EVERY unsafe-method request, app-wide.** The session cookie is `Path=/`, so unlike the `Path=/auth`-scoped refresh cookie the cookie-JWT mode uses, every state-changing route — not just `/auth/*` — carries the ambient credential and is therefore a CSRF target. The reference backends enforce this with a single method-filtering middleware (`SessionCsrfMiddleware` in both the FastAPI and Django blocks) rather than a per-route call, specifically because a per-route call is the kind of control a future route can forget to add; a middleware cannot be.
- **The double-submit check itself works identically in both cookie-bearing modes.** A forged cross-site request *can't read* the `csrf_token` cookie to copy it into the `X-CSRF-Token` header (same-origin policy blocks reading another origin's cookie), so it can't forge a matching pair. `SameSite=Lax` is a second, independent layer that blocks the cross-site request from carrying the cookie in the first place.

The double-submit transport itself lives in the auth component's framework-neutral `_cookies.py` (`generate_csrf_token`, `verify_double_submit`, and the session- vs. refresh-cookie builders); the web client's echo half lives in `packages/api-client`'s `src/mutator.ts`.

## RBAC: roles → gate → 403
Authorization is orthogonal to the credential transport — it works identically in every mode, because both a session principal and a decoded access token expose the same `roles`.

- **Session mode reads roles LIVE from the user store on every request** (`_sessions.SessionService.resolve`) — a role grant or revocation lands on the very next request. **Bearer mode's access JWT carries a `roles` claim baked in at mint time** — it stays true until that token expires, no matter what the database says a moment later. This is the single sharpest practical difference between the two credential shapes, and it's why `GET /auth/me`'s `PrincipalOut` now carries `roles` on the wire too: a session-mode browser client has no JWT to decode a claim out of locally, so the server has to hand back the answer for any client-side "should I render this admin link" decision (still UI-only — the server's own gate is the real one either way).
- A protected route declares a role gate:
  - **FastAPI** — `dependencies=[Depends(require_admin)]`, built on the component's `require_roles` (or `require_roles_either`/`build_get_current_principal_either` for a route that must accept either credential shape — see that factory's own docstring on why the session check must run FIRST).
  - **Django/DRF** — the component's `require_roles(request, auth_service, *roles)` / a `HasRole` permission (or the `_either` variants for the same reason).
- A caller with a valid credential but the wrong role gets **`403`** (`permission_denied` → the `ErrorEnvelope` shape); a missing/invalid/dead credential gets **`401`**.
- **Worked example:** `GET /admin/ping` is gated on the `"admin"` role. It appears in the exported OpenAPI schema with documented `401`/`403` responses, so the generated `@repo/api-client` hook (`useAdminPingAdminPingGet`) exposes the typed error branches. This is the RBAC reference endpoint — copy its shape for any role-gated route.

## CORS is part of session mode
Session mode **requires** the backend's CORS to be configured for credentialed cross-origin requests, and this is a hard security constraint, not a convenience toggle — identical to what cookie-JWT mode already required, just now the default rather than an opt-in:

- **Explicit origins only — never a `*` wildcard.** A wildcard `Access-Control-Allow-Origin` is *incompatible* with `credentials: "include"`: the browser refuses to send cookies to a wildcard origin. You must name the exact web origin(s), per environment (dev/staging/prod get distinct allowlists).
- **`Access-Control-Allow-Credentials: true`** must be set so the browser attaches and accepts cookies.
- Allow the `X-CSRF-Token` request header (and `X-Auth-Mode` if any client on this deployment still uses the superseded cookie-JWT path).

Wire this through the `cors-lockdown` component (`templates/components/security/cors-lockdown/`), not by hand — it emits FastAPI `CORSMiddleware` / `django-cors-headers` settings from an explicit `CORSPolicy`. Bearer/mobile is same-origin-agnostic and doesn't need credentialed CORS, but a shared backend serving both should still scope CORS to the web origin.

## Wiring checklist
1. **Backend** — vendor the auth component into the FastAPI or Django block; construct `SessionService` (idle/absolute TTLs — no secret needed, a session id is opaque rather than signed) and, if any client on this deployment needs the JWT path, `AuthService` with a real `JWT_SIGNING_KEY` and access/refresh TTLs, at startup. Expose `/auth/*` and at least one role-gated route (`/admin/ping`). Register the session-mode CSRF middleware. Seed an admin user.
2. **Web** — `configureApiClient({ baseUrl, mode: "session" })` once at startup; set CORS to the web origin with credentials enabled; there is no token to keep in memory.
3. **Mobile** — `configureApiClient({ baseUrl, mode: "bearer" })` (EXPLICIT — see "The two client modes" above for why omitting it is now wrong); store the refresh token in Expo SecureStore; never enable session or cookie mode on native.
4. Confirm session revocation works (logout, then the next request 401s) and CSRF is rejected without a valid `X-CSRF-Token` on a non-auth route, not just `/auth/*`. If the JWT path is also served, confirm refresh **rotation + reuse detection** works (a replayed refresh token 401s and revokes the family) too. Confirm a wrong-role credential 403s in whichever mode(s) are wired.

For the step-by-step application of this in a scaffolded project, see the **`end-to-end-auth` recipe** (`references/recipes/end-to-end-auth.md`).

## Related canon
- `templates/components/security/auth/README.md` — the backend auth component (the `SessionService` contract, the `AuthService`/JWT contract, `_cookies.py`'s CSRF transport, `require_roles`/`require_roles_either`).
- `templates/packages/api-client/README.md` — the client's "Auth modes" section (the mutator seam that implements all three transports).
- `references/security/secure-baseline.md` — the firm security bar (Authentication & authorization, CORS lockdown, CSRF/cookie posture).
- `references/recipes/end-to-end-auth.md` — the recipe that applies this wiring.
- `references/compatibility-matrix.md` — the pinned versions (Expo SDK 57 / SecureStore, orval 8.22.x, React 19.x, PyJWT 2.13.x, argon2-cffi 25.1.x, Django 5.2 / DRF 3.17.x).
