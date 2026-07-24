// The narrow, module-scoped channel between the data layer (createQueryClient,
// which reacts to a 401 from the cache, not a component) and the auth layer
// (AuthProvider, which owns the `/auth/me` query and the app's `onAuthExpired`
// callback). Module-scoped mutable state is safe here because the only thing
// it carries is a set of listener functions, never data — see
// `notifySessionInvalidated`'s own doc comment.
//
// Session mode holds NO in-memory token (see `AuthProvider`'s own docstring
// on why: the credential is an `HttpOnly` cookie JS cannot read), so there is
// nothing here for the api-client mutator's `getAccessToken` to read and no
// refresh step to trigger — both of which an earlier, bearer/cookie-JWT-mode
// version of this file carried. What remains is strictly smaller: one event,
// "the session this tab thought it had just turned out to be dead."

const invalidatedListeners = new Set<() => void>();

/**
 * @internal The QueryClient's `onError` (see `createQueryClient.ts`) calls
 * this on a 401 `ApiError` from ANY query or mutation — not just `/auth/me`'s
 * own. There is no refresh step to attempt on the session path (unlike the
 * JWT paths this replaced): a 401 here always means the session is already
 * gone — logged out in another tab, idle-expired, or server-revoked — so the
 * only correct reaction is to notify, never to retry.
 */
export const notifySessionInvalidated = (): void => {
  for (const fn of invalidatedListeners) fn();
};

/**
 * @internal Register a session-invalidated listener; returns an unsubscribe
 * fn. Used by BOTH `AuthProvider` (to clear its `/auth/me` query and fire the
 * app's own `onAuthExpired` prop) and `createQueryClient` (its own
 * `onAuthExpired` option), so every registered hook fires — they don't
 * compete, they all run.
 */
export const addExpiredListener = (fn: () => void): (() => void) => {
  invalidatedListeners.add(fn);
  return () => {
    invalidatedListeners.delete(fn);
  };
};

/** @internal Test-only: reset all module state so cases don't leak into one another. */
export const __resetAuthBridge = (): void => {
  invalidatedListeners.clear();
};
