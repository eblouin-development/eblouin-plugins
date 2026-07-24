import { useCallback, useEffect, useMemo } from "react";
import type { ReactNode } from "react";
import {
  getMeAuthMeGetQueryKey,
  useLoginAuthLoginPost,
  useLogoutAuthLogoutPost,
  useMeAuthMeGet,
} from "@repo/api-client";
import type { PrincipalOut } from "@repo/api-client";
import { useQueryClient } from "@tanstack/react-query";
import { ApiError } from "../errors/ApiError";
import { isErrorEnvelope } from "../errors/errorEnvelope";
import { addExpiredListener } from "./authBridge";
import { AuthContext } from "./AuthContext";
import type { AuthContextValue } from "./AuthContext";

export interface AuthProviderProps {
  children: ReactNode;
  /**
   * Fired when the session turns out to be invalid — either a login/refetch
   * of `/auth/me` came back 401, or a 401 from ANY other request notified
   * this provider via the auth bridge (see `authBridge.ts`). The app
   * typically redirects to its login route here. Runs AFTER the `/auth/me`
   * query cache is cleared. (`createQueryClient`'s `onAuthExpired` option, if
   * set, also fires — both are registered listeners, they don't compete.)
   */
  onAuthExpired?: () => void;
}

/**
 * The session-mode auth lifecycle from `references/wiring/auth-end-to-end.md`,
 * as a portable React provider. MUST be mounted inside a `QueryClientProvider`
 * (it uses the generated React Query hooks) and paired with
 * `configureApiClient({ mode: "session" })` (`@repo/api-client`'s own
 * README, "Auth modes").
 *
 * **There is no token anywhere in this component, and that is the point.**
 * The backend's default credential is an opaque, `HttpOnly` `session_id`
 * cookie the browser attaches automatically and this code never reads. That
 * removes the entire "hold an access token in memory, decode its claims,
 * single-flight a refresh, rotate on 401" machinery an earlier, bearer/
 * cookie-JWT-mode version of this component carried — there is no access
 * token to hold, no claims to decode, and no refresh ENDPOINT to call (a
 * session's idle deadline slides forward as a side effect of the backend
 * resolving the cookie on every authenticated request; see the auth
 * component's `_sessions.py` module docstring). What replaces all of it:
 * `GET /auth/me` IS the "am I logged in, and as whom" signal, refetched
 * after login/on a session-invalidated notification, nothing more.
 */
export const AuthProvider = ({ children, onAuthExpired }: AuthProviderProps): ReactNode => {
  const queryClient = useQueryClient();
  const { mutateAsync: loginAsync, isPending: loginPending } = useLoginAuthLoginPost();
  const { mutateAsync: logoutAsync, isPending: logoutPending } = useLogoutAuthLogoutPost();

  // Always enabled, unlike the old token-gated version: with a cookie-borne
  // credential there is no in-memory signal for "might be logged in" the way
  // a held access token used to be -- this query itself is that signal.
  // `retry: false` so an honest "not logged in" 401 isn't retried against a
  // session that is deliberately absent; `staleTime` matches
  // `createQueryClient`'s own query default so this doesn't refetch on every
  // remount for no reason.
  const meQuery = useMeAuthMeGet({ query: { retry: false, staleTime: 30_000 } });
  const principal: PrincipalOut | null =
    meQuery.data && meQuery.data.status === 200 ? meQuery.data.data : null;

  const login = useCallback(
    async (email: string, password: string): Promise<void> => {
      const res = await loginAsync({ data: { email, password } });
      if (res.status !== 200) {
        throw new ApiError(res.status, isErrorEnvelope(res.data) ? res.data : undefined);
      }
      // The response body carries no token to apply (see `TokenResponse`'s
      // own docstring: in session mode both fields are `""`) -- the backend
      // set the session cookie already. Refetch /auth/me to load the
      // principal, and invalidate every other cached query so nothing from
      // a PRIOR identity in this tab (a different account that was logged
      // out, or nothing at all) leaks into the freshly-authenticated one.
      await queryClient.invalidateQueries();
    },
    [loginAsync, queryClient],
  );

  const logout = useCallback(async (): Promise<void> => {
    try {
      // No BODY -- a session client has no refresh token to send, and the
      // generated hook's `data` field is optional for exactly this reason
      // (see `RefreshRequest`'s own docstring). The mutation call itself
      // still takes an (empty) variables object -- `mutateAsync` always
      // expects one, whether or not any of its fields are required.
      await logoutAsync({});
    } finally {
      queryClient.clear();
    }
  }, [logoutAsync, queryClient]);

  const hasRole = useCallback(
    // `roles` is an optional field on the wire (see `PrincipalOut`'s own
    // docstring: it defaults to `[]` server-side, which OpenAPI represents
    // as an optional property rather than a required-but-possibly-empty
    // array) -- the `?? []` here is that default, applied client-side.
    (role: string): boolean => (principal?.roles ?? []).includes(role),
    [principal],
  );

  // A 401 from ANY request -- not just /auth/me's own refetch -- means the
  // session died server-side: logged out in another tab, idle-expired, or
  // revoked (a password reset, an admin ban). There is no refresh to
  // attempt on this path (see `authBridge.ts`'s own docstring on why session
  // mode drops that machinery entirely) -- the only correct reaction is to
  // clear the cached principal and let the app react.
  useEffect(() => {
    return addExpiredListener(() => {
      queryClient.setQueryData(getMeAuthMeGetQueryKey(), undefined);
      void queryClient.invalidateQueries({ queryKey: getMeAuthMeGetQueryKey() });
      onAuthExpired?.();
    });
  }, [queryClient, onAuthExpired]);

  const value = useMemo<AuthContextValue>(
    () => ({
      isAuthenticated: principal !== null,
      principal,
      isPending: loginPending || logoutPending || meQuery.isLoading,
      login,
      logout,
      hasRole,
    }),
    [principal, loginPending, logoutPending, meQuery.isLoading, login, logout, hasRole],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
