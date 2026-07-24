# Vendored from templates/components/security/auth (_cookies.py); keep in sync via the weekly freshness audit.
# Do not hand-edit below this line except for this header — see that component's README
# for the composition contract this file is part of.
"""Framework-neutral cookie/CSRF transport for the auth component --
the double-submit-cookie CSRF defense, cookie-name constants, and pure
cookie-kwarg builders a framework adapter (`fastapi.py`/`django.py`) maps
onto its own `Response.set_cookie`/`delete_cookie`. Stdlib only (`hmac`,
`secrets`) -- **no FastAPI, Django, or SQLAlchemy import anywhere in this
file**, matching `_core.py`'s own "framework-neutral core" posture; this
module is the transport-layer sibling of that file, not a replacement for
any of it. Canon: references/security/secure-baseline.md's CSRF guidance
(cross-site request forgery defense for cookie-authenticated requests).

Drop-in: copy this file into app/core/security/auth/_cookies.py, alongside
`_core.py`, `_sessions.py`, and whichever framework adapter(s) a project
vendors -- see this component's README's "Cookie/CSRF transport" section
for the full composition contract.

**Two cookie-borne credentials, two path scopes.** This file serves both
authentication paths this component ships, and the difference between them
is entirely a matter of which routes need the cookie:

- **`_sessions.py`'s opaque session id (the DEFAULT for browser clients)**
  -- `session_id`, scoped `Path=/`, because it authenticates EVERY request,
  not just the auth routes. Built by `build_session_cookie_kwargs` below.
- **`_core.py`'s JWT refresh token (the mobile/native and legacy path)** --
  `refresh_token`, scoped `Path=/auth`, because only login/refresh/logout
  ever need it; the access JWT it mints travels in an `Authorization`
  header instead. Built by `build_refresh_cookie_kwargs` below.

The CSRF token is paired with whichever of the two a project uses, at that
credential's own path (`build_session_csrf_cookie_kwargs` vs
`build_csrf_cookie_kwargs`) -- see "A note on running both paths at once"
at the bottom of this docstring.

**Why cookies need a CSRF defense that bearer tokens don't.** A cookie is
sent AUTOMATICALLY by the browser on every request to a matching
origin+path -- including cross-site requests a malicious page triggers
without the victim's knowledge or consent (classic CSRF). Bearer-token
auth (an `Authorization` header a client must deliberately attach) has no
equivalent exposure, which is exactly why the bearer-token path
`fastapi.py`/`django.py` wire for native clients needs no CSRF defense at
all -- CSRF and the double-submit check below apply ONLY to the cookie
paths, never to the bearer path.

**Session mode raises the stakes on CSRF, deliberately accepted.** When the
credential in the cookie authenticates every route (session mode) rather
than only `/auth/*` (refresh mode), a successful CSRF forgery can drive any
state-changing endpoint, not just a token refresh. That is the real cost of
`Path=/`, and it is why a session-mode project must enforce
`verify_double_submit` on every unsafe-method request (`POST`/`PUT`/
`PATCH`/`DELETE`), not merely on the auth routes -- see `fastapi.py`'s
`enforce_csrf`/`django.py`'s equivalent, and this component's README's
"Server-side sessions" section, which states that requirement as a
checklist item rather than leaving it to be inferred. The tradeoff is
worth taking because the alternative -- a JWT the server cannot revoke --
trades a defense that is fully mechanized (double-submit plus
`SameSite=Lax`, both enforced here) for one that is not available at all
(un-minting a token).

**The double-submit-cookie pattern, in one paragraph.** On login/refresh,
the server sets TWO cookies: the (HttpOnly) refresh token, and a
(non-HttpOnly) CSRF token the SPA can read via `document.cookie` and echo
back as a request header on every state-changing request. A forged
cross-site request can make the browser ATTACH the CSRF cookie
automatically (cookies are sent regardless of origin), but the attacker's
page cannot READ that cookie's value (browsers enforce same-origin on
`document.cookie`) to also set the matching header -- so a forged request
always arrives with the cookie present but the header missing or wrong.
`verify_double_submit` below is the server-side check that enforces
exactly that: header and cookie must BOTH be present and BYTE-IDENTICAL.
This composes with, but does not replace, the `SameSite=Lax` cookie
attribute set by the builders below -- see their own docstrings for why
both layers matter (defense in depth: `SameSite=Lax` blocks most
cross-site sends outright, catching browsers or edge cases the double-
submit check alone might miss; double-submit catches everything else,
including any origin/environment where `SameSite` support or enforcement
is imperfect).

**A note on running both paths at once.** The session and refresh cookies
have distinct names and can coexist, but the CSRF cookie deliberately does
NOT: both `build_csrf_cookie_kwargs` and `build_session_csrf_cookie_kwargs`
write `csrf_token`, differing only in `Path`. A browser holding both would
send two same-named cookies on an `/auth/*` request, and neither
`document.cookie` (client side) nor `request.cookies`/`request.COOKIES`
(server side) can reliably say which one it handed back -- the double-
submit check would then fail or pass unpredictably. This is not a bug to
work around with a second cookie name: a project picks ONE cookie-borne
credential per origin (session mode for browsers, per this catalog's
default), and pairs a native/mobile client with the bearer path, which
sets no cookies at all and therefore cannot collide. A project that truly
must serve both cookie paths from one origin should give them separate
origins (e.g. a distinct API hostname), not separate cookie names."""

from __future__ import annotations

import hmac
import secrets

from . import _core

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class CsrfValidationError(_core.AuthError):
    """Raised when a cookie-authenticated request fails the double-submit
    CSRF check (`verify_double_submit` below) -- the CSRF header is
    missing/blank, the CSRF cookie is missing, or the two do not match.
    Maps to the EXISTING `ErrorCode.PERMISSION_DENIED` (403) -- this
    module does NOT invent a new `ErrorCode` (`error-envelope/errors.py`'s
    enum is LOCKED, matching every other exception in this component's
    `AUTH_ERROR_HTTP` tables). `PERMISSION_DENIED` is the right existing
    member, deliberately not `UNAUTHENTICATED`: the caller already has a
    valid, cookie-borne credential (the refresh token itself may be
    perfectly valid) -- what failed is proof that THIS request was
    actually authorized by the party who holds that cookie, which is an
    authorization/permission failure, not an authentication one. See this
    module's own docstring for the double-submit pattern this exception
    signals a failure of."""


# ---------------------------------------------------------------------------
# Cookie-name constants
# ---------------------------------------------------------------------------

SESSION_COOKIE_NAME = "session_id"
"""The cookie name `_sessions.py`'s opaque server-side session id travels
under -- the DEFAULT browser credential in this catalog. HttpOnly (see
`build_session_cookie_kwargs`) -- never readable from JS."""

REFRESH_COOKIE_NAME = "refresh_token"
"""The cookie name the JWT refresh token travels under, on the
mobile/native-oriented token path. HttpOnly (see
`build_refresh_cookie_kwargs`) -- never readable from JS."""

CSRF_COOKIE_NAME = "csrf_token"
"""The cookie name the CSRF token travels under, on BOTH cookie paths --
they differ by `Path`, not by name (see this module's docstring, "A note
on running both paths at once", for why that means a project must pick one
path per origin). Deliberately NOT HttpOnly (see
`build_csrf_cookie_kwargs`) -- the SPA must be able to read it via
`document.cookie` to echo it back as a request header; that is the entire
double-submit mechanism."""

SESSION_COOKIE_PATH = "/"
"""The `Path` scope for session-mode cookies. **`/`, not `/auth`** -- a
session id authenticates every route in the application, so a narrower
path would simply mean no credential arrives anywhere except the auth
endpoints. This is the one place session mode is strictly broader than the
refresh-cookie path below, and the reason session mode requires CSRF
enforcement on every unsafe-method route rather than only on `/auth/*`."""

REFRESH_COOKIE_PATH = "/auth"
"""The `Path` scope for refresh-mode cookies. **`/auth`** -- only
login/refresh/logout ever need the refresh token, so scoping it here keeps
it off every item/health/admin request, shrinking both the leak surface
and the set of routes that need a CSRF check at all. The access JWT this
path mints travels in an `Authorization` header, which is why the cookie
itself never needs to reach an ordinary API route."""


# ---------------------------------------------------------------------------
# CSRF token generation
# ---------------------------------------------------------------------------


def generate_csrf_token() -> str:
    """Mints a fresh CSRF token: `secrets.token_urlsafe(32)` -- 32 bytes
    (~256 bits) of CSPRNG entropy, base64url-encoded, the SAME construction
    `_core.SingleUseTokenService.issue` uses for its own raw tokens.
    Deliberately INDEPENDENT of the JWTs `_core.TokenService` mints (not
    derived from the access/refresh token, not embedded as a JWT claim) --
    the double-submit pattern's security property rests entirely on this
    value being something an attacker's cross-site page cannot read back
    out of the browser, which has nothing to do with JWT structure or
    signing. NOT persisted server-side anywhere -- pure double-submit:
    the server does not remember what CSRF token it issued, it only
    compares whatever the client hands back (the cookie) against whatever
    the client separately attached (the header) at verification time
    (`verify_double_submit` below). A fresh one is minted on every
    login/refresh, alongside the new refresh token."""
    return secrets.token_urlsafe(32)


# ---------------------------------------------------------------------------
# The core CSRF check -- double submit, constant-time
# ---------------------------------------------------------------------------


def verify_double_submit(*, csrf_cookie: str | None, csrf_header: str | None) -> None:
    """THE double-submit CSRF check -- called ONLY on the cookie-
    authenticated request path (never on the bearer-token path, which
    has no CSRF exposure to begin with -- see this module's own
    docstring). Raises `CsrfValidationError` unless ALL of:

    1. `csrf_header` is present AND non-empty (a missing `X-CSRF-Token`
       header, or one sent as an empty string, both fail here);
    2. `csrf_cookie` is present (a missing `csrf_token` cookie fails
       here, regardless of what the header says); and
    3. `hmac.compare_digest(csrf_header, csrf_cookie)` is `True` -- the
       two values are byte-identical.

    **Constant-time comparison, deliberately never `==`.** Python's `==`
    on strings short-circuits on the first mismatched byte, so its
    running time leaks information about how many leading bytes of a
    guess were correct -- a timing side-channel an attacker could exploit
    to recover the CSRF token byte-by-byte across many requests.
    `hmac.compare_digest` is specifically designed to take the same time
    regardless of where (or whether) the inputs first differ, closing
    that channel. This is the SAME reasoning `_core.py`'s own security-
    critical comparisons follow throughout (e.g. why `PasswordService`
    never compares hashes with `==` either) -- applied here to the CSRF
    token instead of a password hash or a token digest.

    **All three failure modes collapse to the SAME generic
    `CsrfValidationError`, with no distinction in the exception raised**
    (missing header vs. blank header vs. missing cookie vs. mismatch are
    NOT separately signaled) -- mirroring `_core.py`'s own repeated
    "don't leak which specific reason" posture (`InvalidCredentials`,
    `InvalidToken`/`TokenReused`, `InvalidSingleUseToken`): telling an
    attacker probing this endpoint WHICH half of the double-submit check
    failed would only help them figure out what to try next.

    The `and` chain below short-circuits deliberately -- `csrf_header`
    and `csrf_cookie` are only passed to `hmac.compare_digest` once both
    are already known to be truthy `str` values, so a `None` (missing
    header or cookie) never reaches `compare_digest` (which requires two
    `str`, or two `bytes`, arguments -- a `None` would raise `TypeError`,
    not the intended `CsrfValidationError`)."""
    # `.encode("utf-8")` both operands before the constant-time compare: an
    # attacker-controlled `X-CSRF-Token` header can carry non-ASCII bytes
    # (HTTP header values decode as latin-1), and `hmac.compare_digest`
    # raises `TypeError` on a non-ASCII `str` -- which would surface as a
    # 500 rather than the intended 403. Comparing the UTF-8 byte encodings
    # never raises on content, so any malformed/non-ASCII header simply
    # fails the match and falls through to `CsrfValidationError` (403), the
    # same fail-closed outcome as any other mismatch. `secrets.token_urlsafe`
    # (the cookie side) is always ASCII, so this only ever matters for the
    # attacker-supplied header.
    ok = (
        bool(csrf_header)
        and bool(csrf_cookie)
        and hmac.compare_digest(csrf_header.encode("utf-8"), csrf_cookie.encode("utf-8"))
    )
    if not ok:
        raise CsrfValidationError(
            "CSRF validation failed: the X-CSRF-Token header is missing, blank, "
            "or does not match the csrf_token cookie."
        )


# ---------------------------------------------------------------------------
# Pure cookie-kwarg builders
# ---------------------------------------------------------------------------


def _cookie_kwargs(*, key: str, value: str, max_age: int, httponly: bool, path: str) -> dict:
    """Shared shape every builder below returns -- a framework-neutral
    dict a framework adapter maps onto its own `Response.set_cookie(...)`
    (FastAPI/Starlette and Django both accept a `set_cookie(key=, value=,
    max_age=, path=, httponly=, secure=, samesite=)` call with these exact
    keyword names, so no adapter-side renaming is needed). Every flag
    below except `path` and `httponly` is fixed for every cookie this
    component sets -- there is no caller-configurable escape hatch,
    deliberately: a project that needs different cookie semantics is not
    using the double-submit pattern this file implements. `path` and
    `httponly` are passed in by the specific builders rather than
    hardcoded here, and NEITHER is reachable from outside this module --
    every public builder below pins both to a module constant
    (`SESSION_COOKIE_PATH`/`REFRESH_COOKIE_PATH`) or a literal, so a
    caller still has no way to widen a cookie's scope by hand.

    - **`path`** -- which requests the browser attaches this cookie to.
      `SESSION_COOKIE_PATH` (`/`) for the session credential, which must
      reach every route to authenticate it; `REFRESH_COOKIE_PATH`
      (`/auth`) for the refresh credential, which only login/refresh/
      logout ever need. See both constants' own docstrings for the full
      reasoning, and this module's docstring for why the narrower scope
      is not available to session mode.
    - **`secure=True`** -- the browser will never transmit this cookie
      over plain HTTP, only HTTPS. A refresh token (or a CSRF token that
      guards it) sent in plaintext over the network is as good as
      published; this flag is non-negotiable on every environment that
      terminates TLS, which is every environment this component targets
      (see `references/security/secure-baseline.md`).
    - **`samesite="lax"`** -- the browser withholds this cookie on
      cross-site sub-resource requests and cross-site `POST`/`PUT`/etc.
      (exactly the CSRF vector: a malicious page's forged `<form>` submit
      or `fetch()` to this app's origin), while still attaching it on a
      top-level cross-site navigation (a user clicking a link, or
      following an emailed link, INTO this app) so an ordinary
      "click a link and land logged in" flow keeps working. `Strict`
      would additionally withhold the cookie on that top-level-navigation
      case too -- breaking the common "click the link in your email"
      pattern this component's own `AccountService` verify/reset flows
      rely on. `None` would re-open exactly the cross-site-POST exposure
      `Lax` exists to close, making the cookie flow that in every browser
      request regardless of origin -- the entire reason `SameSite` exists
      at all. `Lax` composes with `verify_double_submit`'s own check as
      DEFENSE IN DEPTH, not a substitute for it: an older browser, a
      misconfigured proxy, or a `SameSite`-exempt request type (some
      browsers still allow certain top-level GETs through even under
      `Strict`-adjacent policies) could theoretically let a cookie
      through that `SameSite` was meant to block -- the double-submit
      check catches that request anyway, because the attacker's page
      still cannot read the CSRF cookie's value to also forge the header.

    `httponly` is the one flag that DIFFERS between a credential cookie
    and its paired CSRF cookie -- see `build_refresh_cookie_kwargs`/
    `build_csrf_cookie_kwargs` for why, passed in here rather than
    hardcoded."""
    return {
        "key": key,
        "value": value,
        "max_age": max_age,
        "path": path,
        "httponly": httponly,
        "secure": True,
        "samesite": "lax",
    }


def build_session_cookie_kwargs(value: str, max_age: int) -> dict:
    """Kwargs for the SESSION cookie -- the default browser credential in
    this catalog: `{"key": "session_id", "value": value, "max_age":
    max_age, "path": "/", "httponly": True, "secure": True, "samesite":
    "lax"}`.

    **`httponly=True`** -- JavaScript running in the page, including
    injected-via-XSS JavaScript, cannot read this cookie at all. This is
    the property that makes session mode's XSS posture strictly better
    than any design that keeps a token in a JS-reachable variable or
    `localStorage`: there is no credential in the JS heap to steal. An XSS
    payload can still RIDE the session by issuing same-origin requests
    while it runs (no cookie flag can prevent that), but it cannot
    exfiltrate anything that keeps working after the user closes the tab.

    **`path="/"`** (`SESSION_COOKIE_PATH`) -- unavoidable, since this
    cookie authenticates every route. See that constant's docstring and
    this module's own for the CSRF obligation that scope creates.

    `max_age` is the caller's -- pass `IssuedSession.max_age_seconds(now)`
    (`_sessions.py`), which measures to the session's ABSOLUTE deadline so
    the cookie outlives an idle period and lets the SERVER, not the
    browser, be the thing that decides a session went stale."""
    return _cookie_kwargs(
        key=SESSION_COOKIE_NAME,
        value=value,
        max_age=max_age,
        httponly=True,
        path=SESSION_COOKIE_PATH,
    )


def build_session_csrf_cookie_kwargs(value: str, max_age: int) -> dict:
    """Kwargs for the CSRF cookie PAIRED WITH the session cookie --
    identical to `build_session_cookie_kwargs` except `"key":
    "csrf_token"` and **`"httponly": False`**, for exactly the reason
    `build_csrf_cookie_kwargs` gives: the SPA must be able to read this
    value via `document.cookie` to echo it back as `X-CSRF-Token`, which
    is the entire double-submit mechanism.

    Scoped to `SESSION_COOKIE_PATH` (`/`) so it accompanies the session
    cookie everywhere the session cookie goes -- a CSRF cookie scoped
    more narrowly than the credential it protects would leave every route
    outside that scope with an ambient credential and no way to run the
    double-submit check at all."""
    return _cookie_kwargs(
        key=CSRF_COOKIE_NAME,
        value=value,
        max_age=max_age,
        httponly=False,
        path=SESSION_COOKIE_PATH,
    )


def clear_session_cookie_kwargs() -> dict:
    """Kwargs to CLEAR the session cookie on logout: the same shape
    `build_session_cookie_kwargs` returns, with `value=""` and
    `max_age=0` (RFC 6265's "expire this cookie now"). `path` is repeated
    identically to the setting call -- a browser only matches a delete
    instruction against a cookie with the SAME `path`, so clearing a
    `Path=/` cookie with a `Path=/auth` instruction silently leaves it in
    place.

    Clearing the cookie is the COSMETIC half of logout; the half that
    matters is `_sessions.SessionService.revoke`, which kills the
    server-side record so the id stops authenticating even if the cookie
    survives in some client that ignored this instruction."""
    return _cookie_kwargs(
        key=SESSION_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        path=SESSION_COOKIE_PATH,
    )


def clear_session_csrf_cookie_kwargs() -> dict:
    """Kwargs to CLEAR the session-paired CSRF cookie on logout --
    identical to `clear_session_cookie_kwargs` except `"key":
    "csrf_token"` and `"httponly": False`, matching
    `build_session_csrf_cookie_kwargs`'s own one difference. Cleared
    alongside the session cookie so this component leaves none of its own
    cookies behind in the browser."""
    return _cookie_kwargs(
        key=CSRF_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=False,
        path=SESSION_COOKIE_PATH,
    )


def build_refresh_cookie_kwargs(value: str, max_age: int) -> dict:
    """Kwargs for the refresh-token cookie: `{"key": "refresh_token",
    "value": value, "max_age": max_age, "path": "/auth", "httponly": True,
    "secure": True, "samesite": "lax"}`.

    **`httponly=True`** -- JavaScript running in the page (including
    injected-via-XSS JavaScript) cannot read this cookie's value via
    `document.cookie` at all. The refresh token is the single most
    sensitive credential this component mints (see `_core.py`'s own
    `RefreshTokenStore`/rotation docstrings on why) -- keeping it
    completely inaccessible to JS is what makes an XSS bug on this
    origin unable to exfiltrate it, even though the SAME XSS bug could
    still exfiltrate the (necessarily readable) CSRF cookie or ride along
    on an authenticated request while it's active. `max_age` is passed
    through unchanged from the caller (typically the refresh token's own
    TTL, so the cookie expires no later than the token it carries would
    have anyway)."""
    return _cookie_kwargs(
        key=REFRESH_COOKIE_NAME,
        value=value,
        max_age=max_age,
        httponly=True,
        path=REFRESH_COOKIE_PATH,
    )


def build_csrf_cookie_kwargs(value: str, max_age: int) -> dict:
    """Kwargs for the CSRF-token cookie: identical shape to
    `build_refresh_cookie_kwargs` except `"key": "csrf_token"` and
    **`"httponly": False`** -- the one deliberate difference. The SPA
    MUST be able to read this cookie's value (via `document.cookie`) to
    echo it back as the `X-CSRF-Token` request header on every state-
    changing request; that echo-back is the entire double-submit
    mechanism `verify_double_submit` checks server-side. Making this
    cookie readable does NOT reopen the exposure `HttpOnly` closes for
    the refresh cookie: the CSRF token's job is to prove "this request
    was made by code that can read this origin's cookies" (which is
    exactly what a cross-site attacker's page cannot do), not to stay
    secret from same-origin JS the way the refresh token must."""
    return _cookie_kwargs(
        key=CSRF_COOKIE_NAME,
        value=value,
        max_age=max_age,
        httponly=False,
        path=REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookie_kwargs() -> dict:
    """Kwargs to CLEAR the refresh-token cookie on logout:
    `{"key": "refresh_token", "value": "", "max_age": 0, "path": "/auth",
    "httponly": True, "secure": True, "samesite": "lax"}` -- `max_age=0`
    instructs the browser to delete the cookie immediately (a `Max-Age`
    of zero or negative is the standard "expire this cookie now"
    mechanism per RFC 6265). `path`/`httponly`/`secure`/`samesite` are
    repeated identically to the SETTING call -- a browser only matches a
    clear/delete instruction against a cookie with the SAME `path` (and,
    for some browsers/older semantics, other attributes); mismatching any
    of them here would silently fail to clear the cookie the login/
    refresh flow actually set."""
    return _cookie_kwargs(
        key=REFRESH_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=True,
        path=REFRESH_COOKIE_PATH,
    )


def clear_csrf_cookie_kwargs() -> dict:
    """Kwargs to CLEAR the CSRF-token cookie on logout -- identical to
    `clear_refresh_cookie_kwargs` except `"key": "csrf_token"` and
    `"httponly": False`, matching `build_csrf_cookie_kwargs`'s own one
    difference from the refresh builder. Cleared alongside the refresh
    cookie on every logout -- a stale CSRF cookie left behind after
    logout has no refresh cookie left to protect, but clearing both
    together keeps the pair's lifecycle simple and leaves nothing of
    this component's own cookies behind in the browser."""
    return _cookie_kwargs(
        key=CSRF_COOKIE_NAME,
        value="",
        max_age=0,
        httponly=False,
        path=REFRESH_COOKIE_PATH,
    )
