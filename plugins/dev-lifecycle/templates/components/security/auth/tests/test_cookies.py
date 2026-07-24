"""Exhaustive tests for auth's _cookies.py -- the framework-neutral
cookie/CSRF double-submit transport -- plus cross-checks against both
framework adapters' `AUTH_ERROR_HTTP` tables (`fastapi.py`/`django.py`)
confirming `CsrfValidationError` is wired into each identically."""

from __future__ import annotations

import hmac

import pytest

# ---------------------------------------------------------------------------
# verify_double_submit
# ---------------------------------------------------------------------------


def test_valid_matching_pair_passes(cookies_mod):
    # Does not raise.
    cookies_mod.verify_double_submit(csrf_cookie="matching-token", csrf_header="matching-token")


def test_missing_header_raises(cookies_mod):
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="a-token", csrf_header=None)


def test_blank_header_raises(cookies_mod):
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="a-token", csrf_header="")


def test_missing_cookie_raises(cookies_mod):
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie=None, csrf_header="a-token")


def test_blank_cookie_raises(cookies_mod):
    # A blank (empty-string) cookie is just as unusable as a missing one --
    # `bool("")` is falsy, so this hits the same `not csrf_cookie` branch.
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="", csrf_header="a-token")


def test_mismatch_raises(cookies_mod):
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="token-a", csrf_header="token-b")


def test_non_ascii_header_fails_closed_to_csrf_error_not_typeerror(cookies_mod):
    # HTTP header values decode as latin-1, so an attacker can send an
    # X-CSRF-Token with non-ASCII bytes. hmac.compare_digest raises
    # TypeError on a non-ASCII str (which would surface as a 500); the
    # UTF-8-encode before comparing must instead make this fail closed to
    # CsrfValidationError (403), like any other mismatch -- never a 500.
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="an-ascii-token", csrf_header="\xff\xfe\x80")


def test_non_ascii_header_matching_a_non_ascii_cookie_still_passes(cookies_mod):
    # Defensive: even if both sides somehow carry the identical non-ASCII
    # value, the UTF-8-encoded compare must not raise -- it compares equal.
    cookies_mod.verify_double_submit(csrf_cookie="tök\xffen", csrf_header="tök\xffen")


def test_both_missing_raises(cookies_mod):
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie=None, csrf_header=None)


def test_every_failure_mode_raises_the_identical_exception_type_and_message(cookies_mod):
    # Missing header, blank header, missing cookie, and mismatch all
    # collapse to the SAME generic exception -- no distinguishing detail
    # leaked about which half of the check failed (see the function's own
    # docstring). Assert the message is identical across every case, not
    # just the type.
    cases = [
        dict(csrf_cookie="a-token", csrf_header=None),
        dict(csrf_cookie="a-token", csrf_header=""),
        dict(csrf_cookie=None, csrf_header="a-token"),
        dict(csrf_cookie="token-a", csrf_header="token-b"),
    ]
    messages = set()
    for kwargs in cases:
        with pytest.raises(cookies_mod.CsrfValidationError) as excinfo:
            cookies_mod.verify_double_submit(**kwargs)
        messages.add(str(excinfo.value))
    assert len(messages) == 1


def test_equal_length_but_different_strings_are_rejected(cookies_mod):
    # Guards against a naive length-only or truncated comparison bug --
    # two strings of the IDENTICAL length that differ only in their last
    # character must still be rejected.
    cookie = "a" * 43  # matches secrets.token_urlsafe(32)'s typical length
    header = cookie[:-1] + "b"
    assert len(cookie) == len(header)
    assert cookie != header
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie=cookie, csrf_header=header)


def test_uses_hmac_compare_digest_not_dunder_eq(cookies_mod, monkeypatch):
    # Directly confirms the constant-time primitive is what's actually
    # invoked -- not just that behavior happens to match `==`. Patches
    # `hmac.compare_digest` (as imported into the `_cookies` module) with a
    # spy that still delegates to the real implementation, and asserts it
    # was called with the header/cookie pair as UTF-8 BYTES (the module
    # encodes both operands before comparing, so a non-ASCII header can't
    # make compare_digest raise TypeError -- see verify_double_submit).
    calls = []
    real_compare_digest = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(cookies_mod.hmac, "compare_digest", spy)
    cookies_mod.verify_double_submit(csrf_cookie="same-token", csrf_header="same-token")
    assert calls == [(b"same-token", b"same-token")]


def test_compare_digest_not_called_when_header_missing(cookies_mod, monkeypatch):
    # The `and`-chain short-circuits before ever calling compare_digest
    # with a None -- which would raise TypeError, not CsrfValidationError.
    calls = []
    monkeypatch.setattr(cookies_mod.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or True)
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie="a-token", csrf_header=None)
    assert calls == []


def test_compare_digest_not_called_when_cookie_missing(cookies_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(cookies_mod.hmac, "compare_digest", lambda a, b: calls.append((a, b)) or True)
    with pytest.raises(cookies_mod.CsrfValidationError):
        cookies_mod.verify_double_submit(csrf_cookie=None, csrf_header="a-token")
    assert calls == []


# ---------------------------------------------------------------------------
# generate_csrf_token
# ---------------------------------------------------------------------------


def test_generate_csrf_token_is_url_safe_and_high_entropy(cookies_mod):
    token = cookies_mod.generate_csrf_token()
    assert isinstance(token, str)
    assert len(token) >= 32  # base64url(32 raw bytes) is 43 chars; a loose floor
    # Two consecutive calls must not collide (CSPRNG, not a fixed value).
    assert token != cookies_mod.generate_csrf_token()


# ---------------------------------------------------------------------------
# Cookie-kwarg builders -- exact flags
# ---------------------------------------------------------------------------


def test_build_refresh_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.build_refresh_cookie_kwargs("raw-refresh-jwt", 604800)
    assert kwargs == {
        "key": "refresh_token",
        "value": "raw-refresh-jwt",
        "max_age": 604800,
        "path": "/auth",
        "httponly": True,
        "secure": True,
        "samesite": "lax",
    }


def test_build_csrf_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.build_csrf_cookie_kwargs("raw-csrf-token", 604800)
    assert kwargs == {
        "key": "csrf_token",
        "value": "raw-csrf-token",
        "max_age": 604800,
        "path": "/auth",
        "httponly": False,  # the one deliberate difference from the refresh cookie
        "secure": True,
        "samesite": "lax",
    }


def test_clear_refresh_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.clear_refresh_cookie_kwargs()
    assert kwargs == {
        "key": "refresh_token",
        "value": "",
        "max_age": 0,
        "path": "/auth",
        "httponly": True,
        "secure": True,
        "samesite": "lax",
    }


def test_clear_csrf_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.clear_csrf_cookie_kwargs()
    assert kwargs == {
        "key": "csrf_token",
        "value": "",
        "max_age": 0,
        "path": "/auth",
        "httponly": False,
        "secure": True,
        "samesite": "lax",
    }


def test_refresh_cookie_max_age_passed_through_unchanged(cookies_mod):
    assert cookies_mod.build_refresh_cookie_kwargs("v", 42)["max_age"] == 42
    assert cookies_mod.build_refresh_cookie_kwargs("v", 0)["max_age"] == 0


def test_csrf_cookie_max_age_passed_through_unchanged(cookies_mod):
    assert cookies_mod.build_csrf_cookie_kwargs("v", 42)["max_age"] == 42


def test_every_refresh_path_cookie_is_scoped_to_the_auth_path(cookies_mod):
    for kwargs in (
        cookies_mod.build_refresh_cookie_kwargs("v", 1),
        cookies_mod.build_csrf_cookie_kwargs("v", 1),
        cookies_mod.clear_refresh_cookie_kwargs(),
        cookies_mod.clear_csrf_cookie_kwargs(),
    ):
        assert kwargs["path"] == "/auth"
        assert kwargs["secure"] is True
        assert kwargs["samesite"] == "lax"


# ---------------------------------------------------------------------------
# Session-mode cookie builders (the default browser path)
# ---------------------------------------------------------------------------


def test_build_session_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.build_session_cookie_kwargs("raw-opaque-session-id", 86400)
    assert kwargs == {
        "key": "session_id",
        "value": "raw-opaque-session-id",
        "max_age": 86400,
        "path": "/",
        "httponly": True,
        "secure": True,
        "samesite": "lax",
    }


def test_build_session_csrf_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.build_session_csrf_cookie_kwargs("raw-csrf-token", 86400)
    assert kwargs == {
        "key": "csrf_token",
        "value": "raw-csrf-token",
        "max_age": 86400,
        "path": "/",
        # The ONE deliberate difference from the session cookie: the SPA
        # must be able to read this to echo it back as X-CSRF-Token.
        "httponly": False,
        "secure": True,
        "samesite": "lax",
    }


def test_clear_session_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.clear_session_cookie_kwargs()
    assert kwargs == {
        "key": "session_id",
        "value": "",
        "max_age": 0,
        "path": "/",
        "httponly": True,
        "secure": True,
        "samesite": "lax",
    }


def test_clear_session_csrf_cookie_kwargs_exact_flags(cookies_mod):
    kwargs = cookies_mod.clear_session_csrf_cookie_kwargs()
    assert kwargs == {
        "key": "csrf_token",
        "value": "",
        "max_age": 0,
        "path": "/",
        "httponly": False,
        "secure": True,
        "samesite": "lax",
    }


def test_the_session_cookie_is_never_readable_from_javascript(cookies_mod):
    # HttpOnly on both the set and the clear instruction -- the property
    # that means an XSS payload has no credential in the JS heap to steal.
    assert cookies_mod.build_session_cookie_kwargs("v", 1)["httponly"] is True
    assert cookies_mod.clear_session_cookie_kwargs()["httponly"] is True


def test_session_cookies_are_scoped_to_the_root_path(cookies_mod):
    # Path=/ is unavoidable for a credential that authenticates every
    # route -- and is exactly why session mode requires CSRF enforcement on
    # every unsafe-method request rather than only on /auth/*.
    for kwargs in (
        cookies_mod.build_session_cookie_kwargs("v", 1),
        cookies_mod.build_session_csrf_cookie_kwargs("v", 1),
        cookies_mod.clear_session_cookie_kwargs(),
        cookies_mod.clear_session_csrf_cookie_kwargs(),
    ):
        assert kwargs["path"] == "/"
        assert kwargs["secure"] is True
        assert kwargs["samesite"] == "lax"


def test_a_clear_instruction_matches_the_path_it_set(cookies_mod):
    # A browser only matches a delete against a cookie with the SAME path,
    # so a mismatch here would silently leave the credential in place.
    assert (
        cookies_mod.clear_session_cookie_kwargs()["path"]
        == cookies_mod.build_session_cookie_kwargs("v", 1)["path"]
    )
    assert (
        cookies_mod.clear_refresh_cookie_kwargs()["path"]
        == cookies_mod.build_refresh_cookie_kwargs("v", 1)["path"]
    )


def test_session_cookie_max_age_passed_through_unchanged(cookies_mod):
    assert cookies_mod.build_session_cookie_kwargs("v", 42)["max_age"] == 42
    assert cookies_mod.build_session_csrf_cookie_kwargs("v", 42)["max_age"] == 42


def test_the_two_csrf_cookies_differ_only_by_path(cookies_mod):
    # Documented in _cookies.py's "A note on running both paths at once":
    # same name, different Path -- which is why a project picks ONE
    # cookie-borne credential per origin.
    session_csrf = cookies_mod.build_session_csrf_cookie_kwargs("v", 1)
    refresh_csrf = cookies_mod.build_csrf_cookie_kwargs("v", 1)
    assert session_csrf["key"] == refresh_csrf["key"] == "csrf_token"
    assert session_csrf["path"] != refresh_csrf["path"]
    differing = {k for k in session_csrf if session_csrf[k] != refresh_csrf[k]}
    assert differing == {"path"}


# ---------------------------------------------------------------------------
# Cookie-name and path constants
# ---------------------------------------------------------------------------


def test_cookie_name_constants(cookies_mod):
    assert cookies_mod.SESSION_COOKIE_NAME == "session_id"
    assert cookies_mod.REFRESH_COOKIE_NAME == "refresh_token"
    assert cookies_mod.CSRF_COOKIE_NAME == "csrf_token"


def test_cookie_path_constants(cookies_mod):
    assert cookies_mod.SESSION_COOKIE_PATH == "/"
    assert cookies_mod.REFRESH_COOKIE_PATH == "/auth"


# ---------------------------------------------------------------------------
# CsrfValidationError: hierarchy + both adapters' AUTH_ERROR_HTTP mapping
# ---------------------------------------------------------------------------


def test_csrf_validation_error_is_an_auth_error_subclass(cookies_mod, core_mod):
    assert issubclass(cookies_mod.CsrfValidationError, core_mod.AuthError)


def test_csrf_validation_error_is_raisable_and_catchable_as_auth_error(cookies_mod, core_mod):
    with pytest.raises(core_mod.AuthError):
        raise cookies_mod.CsrfValidationError("boom")


def test_fastapi_auth_error_http_maps_csrf_validation_error(cookies_mod, fastapi_mod):
    assert fastapi_mod.AUTH_ERROR_HTTP[cookies_mod.CsrfValidationError] == (403, "permission_denied")


def test_django_auth_error_http_maps_csrf_validation_error(cookies_mod, django_mod):
    assert django_mod.AUTH_ERROR_HTTP[cookies_mod.CsrfValidationError] == (403, "permission_denied")


def test_both_adapters_map_csrf_validation_error_identically(fastapi_mod, django_mod, cookies_mod):
    assert (
        fastapi_mod.AUTH_ERROR_HTTP[cookies_mod.CsrfValidationError]
        == django_mod.AUTH_ERROR_HTTP[cookies_mod.CsrfValidationError]
    )


# ---------------------------------------------------------------------------
# Adapter glue: no rest_framework import in django.py, no app.* import in
# either adapter (guardrail-level regression checks, cheap to assert here).
# ---------------------------------------------------------------------------


def test_django_adapter_source_has_no_rest_framework_import():
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent / "django.py").read_text()
    # The docstrings legitimately MENTION `rest_framework` (explaining why
    # this file works for DRF and non-DRF projects alike) -- what must
    # never appear is an actual `import`/`from ... import` statement
    # naming it.
    import_lines = [
        line
        for line in source.splitlines()
        if re.match(r"^(import|from)\s+\S*rest_framework", line)
    ]
    assert import_lines == []


def test_cookies_module_is_stdlib_only_source():
    import pathlib
    import re

    source = (pathlib.Path(__file__).resolve().parent.parent / "_cookies.py").read_text()
    top_level_imports = {
        line.strip()
        for line in source.splitlines()
        if re.match(r"^(import|from)\s+\S+", line) and "__future__" not in line
    }
    # Only stdlib (`hmac`, `secrets`) plus the sibling `_core` import --
    # no FastAPI/Django/SQLAlchemy/any-third-party import anywhere.
    assert top_level_imports == {"import hmac", "import secrets", "import _core"}
