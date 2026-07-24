"""Session-mode CSRF enforcement, as ONE Django middleware rather than a
per-view call. This block's own app code, NOT a vendored file -- it
composes the vendored `django.py` adapter's `enforce_csrf`/
`read_session_cookie` with the method/cookie/path filtering this block's
session routing needs.

**Why this exists.** The server-side session cookie
(`core/security/auth/_sessions.py`, this block's default browser
credential) is scoped `Path=/`, because it has to authenticate every
route. That makes every state-changing view in this application a CSRF
target -- the browser attaches the session cookie automatically to a forged
cross-site request exactly as it does to a legitimate one. The `Path=/auth`
refresh cookie the JWT path uses never had this problem: it reached three
endpoints, so three explicit `enforce_csrf(request)` calls covered it
completely.

Covering "every unsafe-method view" the same per-view way would mean a call
at the top of every `POST`/`PUT`/`PATCH`/`DELETE` handler, and a security
control that must be remembered at each of N call sites is one that will
eventually be forgotten at call site N+1. A middleware filtering on
`request.method` is the shape that cannot be forgotten: a view added
tomorrow is covered the moment it is routed, with nothing for its author to
remember.

**This is NOT Django's own `CsrfViewMiddleware`, and does not replace it.**
Django's built-in CSRF protection is tied to `django.contrib.sessions`'
own cookie and its `csrftoken` cookie/`X-CSRFToken` header pair. The
credential being protected here is this component's `session_id` cookie,
with its own `csrf_token`/`X-CSRF-Token` double-submit pair, verified with
the vendored `verify_double_submit`'s constant-time comparison. The two are
independent and can coexist -- a project also serving Django-session-backed
admin views should leave `CsrfViewMiddleware` in place for those.

**What it checks, and what it deliberately does not.**

- **Only unsafe methods.** `GET`/`HEAD`/`OPTIONS`/`TRACE` are skipped:
  demanding a CSRF header on every read would break ordinary navigation and
  CORS preflight for zero benefit, since a safe method is not supposed to
  change state.
- **Only when a session cookie is actually present.** A request with no
  `session_id` cookie has no ambient credential and so no CSRF exposure: a
  bearer-token client (mobile, service-to-service) attaches its credential
  explicitly and must not be required to echo a CSRF token it was never
  given. Deciding on the cookie's ACTUAL presence, rather than on anything
  the client declares about itself, is the same per-request posture
  `LogoutView` uses -- a client cannot opt out of CSRF enforcement by
  claiming to be a mobile app.
- **`POST /auth/login` is exempt**, and only that path. Login is
  authenticated by the credentials in its body and runs before any session
  exists; a request reaching it while holding a stale session cookie
  (re-login after an idle timeout, a second account in the same browser)
  would otherwise be rejected 403 for failing to echo a CSRF token
  belonging to the session it is in the middle of replacing. Logout is NOT
  exempt -- it is state-changing and must stay protected.

**Why it renders its own response instead of raising.** Every other
`CsrfValidationError` in this block is raised inside a view and mapped by
`core/exceptions.py`'s DRF `exception_handler`. That handler cannot help
here: DRF's exception handling is per-view, invoked inside the view
dispatch that this middleware runs *before*. An exception raised here would
surface as Django's generic 500 rather than the intended 403. So this
middleware builds the response itself, reading the status and code from the
SAME vendored `AUTH_ERROR_HTTP` table `core/exceptions.py` consults, so the
two paths cannot drift into producing different shapes for the same
failure.
"""

from __future__ import annotations

from collections.abc import Callable

from django.http import HttpRequest, HttpResponse, JsonResponse

from core.security.auth import (
    AUTH_ERROR_HTTP,
    CsrfValidationError,
    enforce_csrf,
    read_session_cookie,
)

# Methods with no CSRF exposure -- they are not supposed to change state,
# and requiring a token on them would break navigation and preflight.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

# The one unsafe-method path exempt from the check. An explicit, tiny set
# rather than a prefix match, so it can never silently widen to cover
# `/auth/logout` -- which must stay protected.
_EXEMPT_PATHS = frozenset({"/auth/login", "/auth/login/"})


class SessionCsrfMiddleware:
    """Enforces the double-submit CSRF check on every unsafe-method request
    that actually carries a session cookie. See this module's docstring for
    the rationale behind each of those three conditions.

    Plain new-style Django middleware (a callable returning a callable) --
    no `MiddlewareMixin`, matching this block's other middleware."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if (
            request.method.upper() not in _SAFE_METHODS
            and request.path not in _EXEMPT_PATHS
            and read_session_cookie(request) is not None
        ):
            try:
                # Checked BEFORE the view runs, so a forged request never
                # reaches it and never has a chance to change anything.
                enforce_csrf(request)
            except CsrfValidationError:
                return _csrf_denied_response()
        return self._get_response(request)


def _csrf_denied_response() -> JsonResponse:
    """The 403 `permission_denied` `ErrorEnvelope` a failed double-submit
    check produces -- identical in status, code, and body shape to what
    `core/exceptions.py` renders for the same exception raised inside a
    view, because both read the status and code from the vendored
    component's own `AUTH_ERROR_HTTP` table rather than hardcoding them.

    The message is fixed and generic, never `str(exc)` -- the vendored
    `verify_double_submit` deliberately raises the SAME message for a
    missing header, a blank header, a missing cookie, and a mismatch (see
    its own docstring), and this response preserves that: telling a probing
    attacker which half of the double-submit pair was wrong would narrow
    what they try next for no defensive benefit.

    The envelope is built as a literal dict rather than through
    `ErrorEnvelopeSerializer` to keep this middleware importable from
    `config/settings.py`'s `MIDDLEWARE` list without pulling DRF's
    serializer machinery (and, transitively, the app registry) into
    settings-module import order. The shape is verified against the
    serializer's own output by `tests/test_session_auth.py`."""
    status_code, code = AUTH_ERROR_HTTP[CsrfValidationError]
    return JsonResponse(
        {"error": {"code": code, "message": "Permission denied.", "details": None}},
        status=status_code,
    )
