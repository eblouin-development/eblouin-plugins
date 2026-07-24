/**
 * Adapts the generated `@repo/api-client` auth operations to the engine's
 * framework-free `AuthApi` interface. This is the ONLY auth file that imports
 * the generated client, so the engine (and its test) never pull it in.
 *
 * BEARER mode: login/refresh/logout carry their token in the request BODY —
 * no cookie, no CSRF. `app/_layout.tsx`'s `configureApiClient({ mode: "bearer"
 * })` is what selects this path — EXPLICITLY, now that the backend's own
 * default is server-side sessions rather than bearer JWTs; that call sends
 * `X-Auth-Mode: bearer` on login so the backend mints the real access/refresh
 * token pair this file's functions expect back, instead of setting a session
 * cookie this app has no use for.
 */
import {
  loginAuthLoginPost,
  refreshAuthRefreshPost,
  logoutAuthLogoutPost,
} from "@repo/api-client";
import type { TokenResponse } from "@repo/api-client";

import type { AuthApi, TokenResult } from "./authEngine";

const toTokenResult = (res: { status: number; data: unknown }): TokenResult => {
  if (res.status === 200) {
    const body = res.data as TokenResponse;
    return {
      status: 200,
      accessToken: body.access_token,
      refreshToken: body.refresh_token,
    };
  }
  return { status: res.status, accessToken: null, refreshToken: null };
};

export const generatedAuthApi: AuthApi = {
  async login(email, password) {
    return toTokenResult(await loginAuthLoginPost({ email, password }));
  },
  async refresh(refreshToken) {
    return toTokenResult(await refreshAuthRefreshPost({ refresh_token: refreshToken }));
  },
  async logout(refreshToken) {
    await logoutAuthLogoutPost({ refresh_token: refreshToken });
  },
};
