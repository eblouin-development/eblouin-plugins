"""Request/response schemas for the auth surface (`app/api/routers/auth.py`).
Stage 3 Step 2 locked the OpenAPI contract and bearer security scheme in
before any handler had a real body; Stage 5a (#41) is the implementation
that actually reads/writes these shapes end to end (register/login/refresh/
logout/me), against the vendored auth component's `AuthService`.

STRICT (`extra="forbid"`) like every other wire schema in this catalog.
`RegisterRequest` is new in Stage 5a — `POST /auth/register` didn't exist
before. `PrincipalOut` stays `{id, email}` only — no `roles` on the wire in
this stage; RBAC's wire surface is Stage 5d, deliberately out of scope
here even though `_core.AccessClaims`/`UserRecord` already carry roles
internally.

Stage 5c (#45) adds `VerifyEmailRequest`/`RequestPasswordResetRequest`/
`ResetPasswordRequest` for the three new account-lifecycle endpoints —
`POST /auth/verify-email`, `POST /auth/request-password-reset`, `POST
/auth/reset-password` (`app/api/routers/auth.py`), against the vendored
`AccountService`."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

# `email: str`, not `EmailStr` — EmailStr requires the `email-validator`
# extra (`pydantic[email]`), which is not in this block's pinned dependency
# set (see pyproject.toml). Real email FORMAT validation isn't added in
# Stage 5a either — `_core.AuthService`'s own normalization (lowercase +
# strip, see its `_normalize_email`) is the only shaping applied; a
# malformed-but-non-empty string is accepted as an email today. A future
# stage can add the `pydantic[email]` extra + switch to `EmailStr` if real
# format validation is wanted.


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    """`POST /auth/refresh`'s and `POST /auth/logout`'s request body.

    **`refresh_token` is optional, defaulting to `""`.** It was required
    (`min_length=1`) when every client of these routes held a refresh
    token; a session client holds none, and would otherwise have to
    fabricate a value to satisfy a field that means nothing on its
    transport — or be 422'd for honestly omitting it. Both handlers read
    the session cookie before they look at this field, so a session-mode
    request never reaches it at all.

    Dropping `min_length=1` costs nothing on the JWT path: an empty
    `refresh_token` presented to `/auth/refresh` raises `InvalidToken` →
    401, which is the correct answer for an invalid token and is
    deliberately indistinguishable from every other invalid-token case
    (see `_core.InvalidToken`'s docstring). Previously it produced a 422
    instead — a *different* response shape for one specific kind of bad
    token, which was itself a small information leak. `/auth/logout` is
    idempotent and 204s either way."""

    model_config = ConfigDict(extra="forbid")

    refresh_token: str = ""


class TokenResponse(BaseModel):
    """`POST /auth/login`'s and `POST /auth/refresh`'s response body,
    shared by BOTH authentication transports so the wire schema stays one
    shape.

    **In session mode (the default), all three fields are effectively
    empty**: `access_token` and `refresh_token` are `""` and `token_type`
    is `"session"`. That is not a placeholder — it is the honest
    representation of what session mode does, which is hand the client no
    token at all. The credential is the `HttpOnly` `session_id` cookie the
    browser stores and JavaScript cannot read; having nothing in the
    response body for the client to keep is precisely the property that
    leaves an XSS payload nothing to exfiltrate.

    **In bearer mode**, `access_token`/`refresh_token` carry the real JWTs
    and `token_type` is `"bearer"`, exactly as before.

    A client reads `token_type` to know which it got, rather than
    inspecting the token fields for emptiness."""

    model_config = ConfigDict(extra="forbid")

    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class PrincipalOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    email: str


# --- Account lifecycle (Stage 5c, #45): verify-email / request-password-reset
# / reset-password wire shapes. Same STRICT (`extra="forbid"`) posture as
# every schema above -- the raw single-use token these carry is opaque to
# this layer (`_core.SingleUseTokenService.consume` does the only real
# validation of it; a `min_length=1` here just rejects an empty string
# before it ever reaches that layer). -----------------------------------


class VerifyEmailRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)


class RequestPasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1)
    # Matches RegisterRequest.password's own policy above (min_length=1 --
    # no further complexity policy enforced at this layer; see that
    # field's own comment on why: real strength requirements are a
    # separate, not-yet-built concern, not something this stage invents).
    new_password: str = Field(min_length=1)
