import { createContext } from "react";
import type { PrincipalOut } from "@repo/api-client";

export interface AuthState {
  /** True once `GET /auth/me` has resolved a live principal — i.e. this
   *  tab holds a valid session cookie. With no in-memory token, this query
   *  IS the signal; see `AuthProvider`'s own docstring. */
  isAuthenticated: boolean;
  /** The principal resolved from `GET /auth/me` (id, email, and — since
   *  session mode carries no JWT for the client to decode locally —
   *  `roles`, UI-only and non-authoritative; see `PrincipalOut`'s own
   *  docstring), or `null` when not authenticated. */
  principal: PrincipalOut | null;
  /** True while a login, logout, or the initial/a refetching `/auth/me`
   *  call is in flight. */
  isPending: boolean;
}

export interface AuthContextValue extends AuthState {
  /** Log in: the backend sets the `HttpOnly` session cookie (see
   *  `references/wiring/auth-end-to-end.md`), then this refetches
   *  `/auth/me` to load the principal and invalidates every other cached
   *  query so nothing from a PRIOR identity in this tab leaks into the new
   *  one. Throws an `ApiError` on bad credentials (401) or a validation
   *  failure (422) for the caller's form to surface. */
  login: (email: string, password: string) => Promise<void>;
  /** Log out: best-effort server call (revokes the session server-side —
   *  the half that actually matters), then clears the query cache. */
  logout: () => Promise<void>;
  /** UI-only role check against `principal.roles`. Never the real
   *  authorization gate — the server's 403 on the underlying call is. */
  hasRole: (role: string) => boolean;
}

/** Null outside a provider — `useAuth` throws a clear error in that case. */
export const AuthContext = createContext<AuthContextValue | null>(null);
