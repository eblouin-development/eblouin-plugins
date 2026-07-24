import { MutationCache, QueryCache, QueryClient } from "@tanstack/react-query";
import { isApiError } from "../errors/ApiError";
import { addExpiredListener, notifySessionInvalidated } from "../auth/authBridge";

export interface CreateQueryClientOptions {
  /**
   * Called when a 401 `ApiError` from any query or mutation notifies that
   * the session is invalid. Registered as a session-invalidated listener
   * alongside `AuthProvider`'s own `onAuthExpired` prop — both fire, they
   * don't compete. Use it for a cache-layer reaction (e.g.
   * `queryClient.clear()`), distinct from the app's redirect.
   */
  onAuthExpired?: () => void;
}

/**
 * A `QueryClient` with the kit's sane defaults and the auth-aware error wiring.
 *
 * - **No retry on 401/403.** On the session path there is no refresh step to
 *   retry against (see `authBridge.ts`'s own docstring) — a 401 always means
 *   the session is already gone, so retrying would just repeat the same
 *   failure against the same dead cookie. A 403 is a real permission answer,
 *   not a transient fault either way. Other errors retry twice.
 * - **QueryCache + MutationCache `onError`.** On a 401 `ApiError` (which only
 *   reaches here because a `queryFn`/`mutationFn` used `unwrap` to throw it),
 *   this notifies through the auth bridge (`notifySessionInvalidated`) —
 *   `AuthProvider` reacts by clearing its cached principal, and this
 *   options object's own `onAuthExpired` (if given) fires too.
 */
export const createQueryClient = (options: CreateQueryClientOptions = {}): QueryClient => {
  const handleAuthError = (error: unknown): void => {
    if (!isApiError(error)) return;
    if (error.status === 401) {
      notifySessionInvalidated();
    }
  };

  // Registered on the SAME shared listener set `AuthProvider` registers its
  // own `onAuthExpired` prop on (see `authBridge.ts`) -- one event
  // (`notifySessionInvalidated`, fired by `handleAuthError` above), every
  // registered listener runs. Registering here rather than calling directly
  // inside `onError` is what keeps this consistent with AuthProvider's own
  // registration -- both react to the identical signal, so a consumer that
  // passes the SAME callback to both options never double-fires it (the
  // underlying `Set` dedupes by reference).
  if (options.onAuthExpired) {
    addExpiredListener(options.onAuthExpired);
  }

  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (failureCount, error) => {
          if (isApiError(error) && (error.status === 401 || error.status === 403)) return false;
          return failureCount < 2;
        },
        refetchOnWindowFocus: false,
        staleTime: 30_000,
      },
      mutations: {
        retry: false,
      },
    },
    queryCache: new QueryCache({ onError: handleAuthError }),
    mutationCache: new MutationCache({ onError: handleAuthError }),
  });
};
