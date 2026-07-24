/**
 * Custom fetch mutator for orval's React Query + fetch client mode.
 *
 * Orval's generated hooks call this with (url, options) and expect the
 * returned value to already be shaped `{ data, status, headers }` — the
 * generated response types (e.g. `createItemItemsPostResponse`) are unions
 * keyed on `status`, so callers pattern-match on `.status` instead of a
 * thrown error for documented non-2xx responses (e.g. a 422 validation
 * error). A rejected promise here is reserved for things the OpenAPI
 * contract can't describe: a network failure or an unparseable response.
 * Generated files import this module's `customFetch` by name — that import
 * contract is fixed by orval's mutator override and must not change shape.
 *
 * Base URL: injected via `configureApiClient({ baseUrl })`, called once at
 * app startup — deliberately NOT read from `process.env` at module load.
 * That would break every documented consumer: Vite ships no `process`
 * global in the browser bundle (a bare `process.env.X` throws
 * `ReferenceError: process is not defined` at import time), and Next/Expo
 * only statically inline framework-prefixed env vars
 * (`NEXT_PUBLIC_*`/`EXPO_PUBLIC_*`) — a bare `API_BASE_URL` read there
 * silently becomes `""` even when the var is set in the environment. See
 * the README's "Configuration" section for each consumer's exact wiring.
 * Unconfigured (or configured with `baseUrl: ""`) resolves to same-origin
 * relative URLs, a sane default behind a reverse proxy that forwards API
 * paths to the backend.
 *
 * ## Auth modes
 *
 * `configureApiClient({ baseUrl, mode })` selects how this client
 * authenticates. There are three, and which one is right is decided by the
 * RUNTIME, not by preference:
 *
 * ### `"session"` — every browser consumer should use this
 * The backend issues an opaque, `HttpOnly` `session_id` cookie (scoped
 * `Path=/`) and this client:
 *   1. sends `credentials: "include"` on every request, so the browser
 *      attaches the session cookie and the readable `csrf_token` cookie;
 *   2. echoes `csrf_token` back as `X-CSRF-Token` on **every unsafe-method
 *      request** (`POST`/`PUT`/`PATCH`/`DELETE`), not just the auth ones —
 *      because a `Path=/` session cookie makes every state-changing
 *      endpoint a CSRF target, unlike the `Path=/auth` refresh cookie;
 *   3. sends **no `Authorization` header and holds no token at all** —
 *      there is nothing for a `getAccessToken` getter to return, and
 *      nothing in the JS heap for an XSS payload to exfiltrate.
 * Session mode is preferred over the two JWT modes for browsers because a
 * session is revocable on the next request, reflects role changes
 * immediately, and enforces a real idle timeout — none of which a bearer
 * token can do. See `references/wiring/auth-end-to-end.md`.
 *
 * ### `"bearer"` — native/mobile (Expo), and the library default
 * Access token in memory (injected via `getAccessToken`), refresh token in
 * Expo SecureStore, `Authorization: Bearer`, no cookies, no CSRF. Correct
 * for a runtime with a real OS-backed secret store and no ambient-cookie
 * problem — CSRF simply does not exist as a class there, and native HTTP
 * clients handle cookies poorly.
 *
 * **This is the library default deliberately, even though session mode is
 * preferred for browsers**: this module is shared by web and native
 * consumers and cannot detect which it is running in, so the default is the
 * mode that is *safe to get wrong*. A browser accidentally left on bearer
 * mode simply fails to authenticate against a session backend; a native app
 * accidentally switched to session mode would try to rely on cookie
 * semantics its runtime does not properly provide. Every web template in
 * this catalog passes `mode: "session"` explicitly.
 *
 * ### `"cookie"` — superseded, kept for migration
 * The JWT refresh token in an `HttpOnly; Path=/auth` cookie, access token
 * still in memory and still in the `Authorization` header. This was the
 * right answer for browsers before the backend had server-side sessions and
 * is strictly worse now: it still leaves a bearer token in the JS heap,
 * still cannot be revoked before its TTL, and still needs an explicit
 * refresh round-trip. Sends `X-Auth-Mode: cookie` at login and echoes CSRF
 * on `/auth/refresh` + `/auth/logout` only.
 *
 * Reading `csrf_token` requires `document.cookie`, so the CSRF echo is a
 * no-op under SSR or any runtime without a `document` (React Native, Node)
 * — safe, because those runtimes are bearer-mode targets that never receive
 * a CSRF cookie in the first place.
 */

export type ApiClientResponse<T = unknown> = {
  data: T;
  status: number;
  headers: Headers;
};

/**
 * How this client authenticates. `"session"` for browsers (preferred),
 * `"bearer"` for native/mobile and service callers, `"cookie"` for the
 * superseded JWT-refresh-in-a-cookie path. See this module's header.
 */
export type ApiClientAuthMode = "session" | "bearer" | "cookie";

type ApiClientConfig = {
  /** Backend origin prepended to every generated request path. Trailing
   * slash(es) are trimmed. Empty string (the default) resolves to
   * same-origin relative URLs. */
  baseUrl: string;
  /** Auth mode (default `"bearer"`). Browser consumers should pass
   * `"session"`; see this module's header for why the default is `"bearer"`
   * even though session mode is preferred for browsers. */
  mode?: ApiClientAuthMode;
  /** @deprecated Use `mode: "cookie"`. Kept so existing
   * `configureApiClient({ baseUrl, cookieMode: true })` call sites keep
   * working unchanged during a migration. Ignored when `mode` is given
   * explicitly — `mode` always wins, so there is no ambiguity if both are
   * passed. */
  cookieMode?: boolean;
  /** Optional access-token getter (default-off). When supplied AND a request
   * does not already carry its own `Authorization` header, the mutator
   * injects `Authorization: Bearer ${getAccessToken()}` — but only when the
   * getter returns a non-empty string (a `null`/`""` return injects
   * nothing). This is the seam a consumer that keeps the short-lived access
   * token in memory uses so the token rides every generated call, without
   * any generated hook or call site having to thread the header through by
   * hand. It never clobbers a caller-supplied `Authorization` header, so an
   * explicit per-call override still wins.
   *
   * **Ignored entirely in `"session"` mode**, where there is no token: the
   * credential is the `HttpOnly` cookie, and sending a stray `Authorization`
   * header alongside it would be meaningless at best and, against a backend
   * that resolves either credential, actively confusing about which one
   * authenticated the request. */
  getAccessToken?: () => string | null;
};

const NO_TOKEN = (): string | null => null;

let config: Required<Omit<ApiClientConfig, "cookieMode">> = {
  baseUrl: "",
  mode: "bearer",
  getAccessToken: NO_TOKEN,
};

/**
 * Resolve the effective mode from the (possibly legacy) config. `mode` wins
 * outright when given; otherwise `cookieMode: true` maps to `"cookie"` and
 * anything else to the `"bearer"` default. Kept as a named function rather
 * than inlined so the precedence rule is stated in exactly one place.
 */
const resolveMode = (next: ApiClientConfig): ApiClientAuthMode => {
  if (next.mode != null) return next.mode;
  return next.cookieMode === true ? "cookie" : "bearer";
};

/**
 * Configure the shared api-client. Call once at app startup, before any
 * generated hook fires a request — see the README's "Configuration"
 * section for per-consumer wiring. Replaces the config wholesale, so it
 * also doubles as a reset (e.g. between test cases).
 */
export const configureApiClient = (next: ApiClientConfig): void => {
  config = {
    baseUrl: next.baseUrl.replace(/\/+$/, ""),
    mode: resolveMode(next),
    getAccessToken: next.getAccessToken ?? NO_TOKEN,
  };
};

// Cookie-mode auth endpoints. Login is where the JWT-cookie mode is selected
// (`X-Auth-Mode: cookie`); refresh/logout are the state-changing calls the
// backend guards with double-submit CSRF when the refresh cookie is present.
// Session mode needs neither list: it is the backend's DEFAULT (so login
// declares nothing) and it guards every unsafe method (so there is no
// endpoint allowlist to keep in sync).
const AUTH_LOGIN_PATH = "/auth/login";
const AUTH_CSRF_PATHS = new Set(["/auth/refresh", "/auth/logout"]);

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS", "TRACE"]);

/**
 * Read the non-HttpOnly `csrf_token` cookie the backend set alongside the
 * `HttpOnly` session (or refresh) cookie. Returns `null` (a safe no-op for
 * the caller) when there is no `document` — SSR, React Native, or any
 * non-browser runtime — or when the cookie is simply absent.
 */
const readCsrfCookie = (): string | null => {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
  return match?.[1] != null ? decodeURIComponent(match[1]) : null;
};

export const customFetch = async <T>(url: string, options: RequestInit = {}): Promise<T> => {
  const headers = new Headers(options.headers);
  if (options.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }

  // Match on the request PATH only (strip any query/hash) — `url` is the
  // generated path, never carrying the configured baseUrl.
  const path = url.split(/[?#]/)[0] ?? url;
  const method = (options.method ?? "GET").toUpperCase();
  const isUnsafeMethod = !SAFE_METHODS.has(method);

  // Access-token injection (default-off): only when a getter was configured,
  // the caller didn't already set Authorization, and the getter returns a
  // non-empty token. Skipped entirely in session mode, which holds no token
  // — see `getAccessToken`'s own doc comment.
  if (config.mode !== "session" && !headers.has("Authorization")) {
    const token = config.getAccessToken();
    if (token) headers.set("Authorization", `Bearer ${token}`);
  }

  const init: RequestInit = { ...options, headers };

  if (config.mode === "session") {
    // Attach the browser's cookies (session_id / csrf_token) to every
    // request. Unlike cookie mode, the session cookie is scoped `Path=/`
    // and is genuinely needed on every path, not just `/auth/*`.
    init.credentials = "include";

    // Double-submit echo on EVERY unsafe method, matching the backend's own
    // method-filtering CSRF middleware. Login is exempt there (it is
    // authenticated by its body and replaces whatever session the browser
    // was holding), so echoing a stale token at it would be pointless —
    // though harmless, since the backend skips the check for that path.
    if (isUnsafeMethod && path !== AUTH_LOGIN_PATH && !headers.has("X-CSRF-Token")) {
      const csrf = readCsrfCookie();
      if (csrf != null) headers.set("X-CSRF-Token", csrf);
    }
  } else if (config.mode === "cookie") {
    init.credentials = "include";

    if (path === AUTH_LOGIN_PATH) {
      // Select the JWT-refresh-cookie mode at login. The backend's default
      // is session mode, so this header is what opts out of it.
      headers.set("X-Auth-Mode", "cookie");
    } else if (AUTH_CSRF_PATHS.has(path) && isUnsafeMethod && !headers.has("X-CSRF-Token")) {
      // Double-submit echo, on the two guarded paths only — the refresh
      // cookie is `Path=/auth` and reaches nothing else.
      const csrf = readCsrfCookie();
      if (csrf != null) headers.set("X-CSRF-Token", csrf);
    }
  } else if (path === AUTH_LOGIN_PATH) {
    // Bearer mode must opt OUT of the backend's session default explicitly,
    // or a native client would be handed a cookie it cannot properly store
    // and no token at all.
    headers.set("X-Auth-Mode", "bearer");
  }

  const response = await fetch(`${config.baseUrl}${url}`, init);

  const contentType = response.headers.get("content-type") ?? "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : ((await response.text()) as unknown);

  return {
    data,
    status: response.status,
    headers: response.headers,
  } as T;
};
