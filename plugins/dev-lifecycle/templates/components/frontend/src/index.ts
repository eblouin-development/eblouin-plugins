// Public surface of @repo/web-shared. Every export is framework-portable:
// no react-router, no `import.meta`, and no `document`/`window` access at
// module top level — so this package imports cleanly into a Vite SPA and a
// Next.js client component alike (the guards are render-gate primitives the
// app supplies router redirects to; see auth/guards).

// --- errors ---------------------------------------------------------------
export { ApiError, isApiError } from "./errors/ApiError";
export { unwrap } from "./errors/unwrap";
export type { ApiResult } from "./errors/unwrap";
export { errorCodeToMessage, getErrorCode, isErrorEnvelope } from "./errors/errorEnvelope";
export { ApiErrorBoundary } from "./errors/ApiErrorBoundary";

// --- query ----------------------------------------------------------------
export { createQueryClient } from "./query/createQueryClient";
export type { CreateQueryClientOptions } from "./query/createQueryClient";

// --- jwt ------------------------------------------------------------------
export { decodeAccessTokenClaims } from "./jwt/decodeAccessTokenClaims";
export type { AccessTokenClaims } from "./jwt/decodeAccessTokenClaims";

// --- auth -----------------------------------------------------------------
// No token getter to export: session mode (this provider's only mode —
// see AuthProvider's own docstring) holds no token, so there is nothing for
// `configureApiClient`'s `getAccessToken` to read. Pair this provider with
// `configureApiClient({ mode: "session" })`.
export { AuthProvider } from "./auth/AuthProvider";
export type { AuthProviderProps } from "./auth/AuthProvider";
export { useAuth } from "./auth/useAuth";
export { AuthContext } from "./auth/AuthContext";
export type { AuthContextValue, AuthState } from "./auth/AuthContext";
export { RequireAuth, RequireRole } from "./auth/guards";

// --- forms ----------------------------------------------------------------
export { useZodForm } from "./forms/useZodForm";
export { FieldError } from "./forms/FieldError";
export { applyEnvelopeToForm } from "./forms/applyEnvelopeToForm";
