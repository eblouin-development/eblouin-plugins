"""Session-mode CSRF enforcement, as ONE middleware rather than a
per-route call.

**Why this exists.** The server-side session cookie
(`app/core/security/auth/_sessions.py`, this app's default browser
credential) is scoped `Path=/`, because it has to authenticate every
route. That makes every state-changing route in this application a CSRF
target -- the browser attaches the session cookie automatically to a
forged cross-site request exactly as it does to a legitimate one. The
`Path=/auth` refresh cookie the JWT path uses never had this problem: it
reached three endpoints, so three explicit `enforce_csrf(request)` calls
covered it completely.

Covering "every unsafe-method route" the same per-route way would mean a
call at the top of every `POST`/`PUT`/`PATCH`/`DELETE` handler in the app,
and a security control that must be remembered at each of N call sites is
one that will eventually be forgotten at call site N+1. A middleware
filtering on `request.method` is the shape that cannot be forgotten: a
route added tomorrow is covered the moment it is mounted, with nothing for
its author to remember.

**What it checks, and what it deliberately does not.**

- Runs the vendored component's `enforce_csrf` (double-submit: the
  `X-CSRF-Token` header must be present and byte-identical to the
  `csrf_token` cookie, compared in constant time) -- see `_cookies.py`'s
  own module docstring for the mechanism and why it works.
- **Only on unsafe methods.** `GET`/`HEAD`/`OPTIONS` are skipped:
  demanding a CSRF header on every read would break ordinary navigation
  and CORS preflight for zero benefit, since a safe method is not
  supposed to change state. (A `GET` that mutates state is a bug this
  middleware cannot fix.)
- **Only when a session cookie is actually present.** A request with no
  `session_id` cookie has no ambient credential, so it has no CSRF
  exposure: a bearer-token client (mobile, service-to-service) attaches
  its credential explicitly and must not be required to echo a CSRF token
  it was never given. Deciding on the cookie's ACTUAL presence, rather
  than on anything the client declares about itself, is the same
  per-request posture `app/api/routers/auth.py`'s `logout` uses -- a
  client cannot opt out of CSRF enforcement by claiming to be a mobile
  app.
- **`POST /auth/login` is exempt**, and only that route. Login is
  authenticated by the credentials in its body and runs before any
  session exists; a request reaching it while holding a stale session
  cookie (re-login after an idle timeout, a second account in the same
  browser) would otherwise be rejected 403 for failing to echo a CSRF
  token belonging to a session it is in the middle of replacing. Logout
  is NOT exempt -- it is state-changing and must stay protected (see
  that handler's own docstring).

**Why this renders its own response instead of raising.** Every other
`CsrfValidationError` in this app is raised inside a route handler and
caught by `app/main.py`'s `_auth_error_handler`. That handler cannot help
here: Starlette runs registered exception handlers in `ExceptionMiddleware`,
which it places at the INNERMOST end of the stack -- inside every
`add_middleware()` layer, including this one. An exception raised here
therefore escapes past `ExceptionMiddleware` entirely and surfaces as an
unhandled 500 rather than the intended 403. So this middleware catches the
error and builds the response itself, reading the status and code from the
SAME vendored `AUTH_ERROR_HTTP` table `_auth_error_handler` consults, so
the two paths can never drift into producing different shapes for the same
failure. The layers OUTSIDE this one still apply normally -- security
headers and the request-id stamp are both added on the way out, so a
403 from here is as well-formed and as traceable as any other response.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.errors import ErrorBody, ErrorCode, ErrorEnvelope
from app.core.security.auth import (
    AUTH_ERROR_HTTP,
    CsrfValidationError,
    enforce_csrf,
    read_session_cookie,
)

# Methods with no CSRF exposure -- they are not supposed to change state,
# and requiring a token on them would break navigation and preflight.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# The one unsafe-method route exempt from the check. Kept as an explicit,
# tiny set rather than a prefix match so it can never silently widen to
# cover, say, `/auth/logout` -- which must stay protected.
_EXEMPT_PATHS = frozenset({"/auth/login"})


class SessionCsrfMiddleware(BaseHTTPMiddleware):
    """Enforces the double-submit CSRF check on every unsafe-method
    request that actually carries a session cookie. See this module's
    docstring for the full rationale on each of those three conditions.

    Deliberately a `BaseHTTPMiddleware` rather than raw ASGI: it needs the
    parsed `request.cookies`/`request.headers` mapping the `Request` object
    provides, and it does not touch the response body at all (the one case
    where `BaseHTTPMiddleware`'s streaming behavior would matter)."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if (
            request.method.upper() not in _SAFE_METHODS
            and request.url.path not in _EXEMPT_PATHS
            and read_session_cookie(request) is not None
        ):
            try:
                # Checked BEFORE `call_next`, so a forged request never
                # reaches the route handler and never has a chance to
                # change anything.
                enforce_csrf(request)
            except CsrfValidationError:
                return _csrf_denied_response()
        return await call_next(request)


def _csrf_denied_response() -> JSONResponse:
    """The 403 `permission_denied` `ErrorEnvelope` a failed double-submit
    check produces -- identical in status, code, and body shape to what
    `app/main.py`'s `_auth_error_handler` renders for the same exception
    raised anywhere else, because both read the status and code from the
    vendored component's own `AUTH_ERROR_HTTP` table rather than hardcoding
    them.

    The message is fixed and generic, never `str(exc)` -- the vendored
    `verify_double_submit` deliberately raises the SAME message for a
    missing header, a blank header, a missing cookie, and a mismatch (see
    its own docstring), and this response preserves that: telling a probing
    attacker which half of the double-submit pair was wrong would narrow
    what they try next for no defensive benefit."""
    status_code, code = AUTH_ERROR_HTTP[CsrfValidationError]
    envelope = ErrorEnvelope(
        error=ErrorBody(code=ErrorCode(code), message="Permission denied.", details=None)
    )
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))
