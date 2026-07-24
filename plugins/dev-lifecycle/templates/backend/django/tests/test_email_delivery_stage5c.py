"""Regression test for the adversarial-review-flagged HIGH-severity Stage 5c
defect: `DjangoEmailSender` (`core/security/auth/stores.py`) used to
schedule delivery with `asyncio.create_task(...)` and return -- correct
under `backend/fastapi`'s uvicorn/ASGI deployment, but silently broken
under THIS block's shipped `gunicorn config.wsgi:application` deployment
(`backend/django/Dockerfile`, `docker-compose.yml`). Every DRF view that
reaches `AccountService.request_email_verification`/`request_password_
reset` is an ordinary sync view bridged into the async `AccountService` via
`asgiref.sync.async_to_sync(...)` -- and `async_to_sync` creates a FRESH
event loop per call, drives the coroutine to completion, and TEARS THAT
LOOP DOWN before returning. Since neither caller awaits anything else after
`send()` returns, a task merely SCHEDULED (never run) on that now-dead loop
never actually delivered anything. Under this app's default
`AUTH_REQUIRE_EMAIL_VERIFICATION=True`, that meant EVERY verification email
(and every password-reset email) was silently dropped -- new accounts could
never verify, and the reset-based recovery path was equally dead. An
availability/auth denial-of-service; the anti-enumeration and generic-401
SECURITY properties were unaffected either way.

`tests/test_auth.py`'s own suite never caught this because its `email_
sender` fixture monkeypatches `core.security.auth.stores.get_email_sender`
with a synchronous, directly-`await`ed `_CapturingEmailSender` (see that
module's own docstring) -- deliberately so tests are deterministic, but as
a side effect the REAL `DjangoEmailSender`'s `send()` implementation, and
therefore the real `async_to_sync`-teardown hazard, is never exercised
anywhere in that suite.

This module closes that blind spot: it does NOT monkeypatch the sender --
it drives `POST /auth/register` through the real `APIClient` (real sync
DRF view -> real `async_to_sync` -> the real `DjangoEmailSender`), flushes
pending deliveries via `flush_pending_email_deliveries` (the deterministic,
non-`sleep`-based synchronization point that function exists for -- see its
own docstring), and asserts the verification email actually landed in
`django.core.mail.outbox` under `EMAIL_BACKEND=locmem`. Against the old
`asyncio.create_task` implementation this test fails (`outbox` stays
empty, and `flush_pending_email_deliveries` -- which also didn't exist
against that implementation -- would have had nothing to wait on with an
`asyncio.Task`-based design in the first place). Against the thread-pool
fix, delivery happens on a real OS thread that is not tied to the
`async_to_sync` loop's lifetime, so it genuinely completes.

`@pytest.mark.django_db(transaction=True)` for the same two reasons `tests/
test_auth.py`'s own module docstring documents (real autocommit semantics;
Django's async ORM under a rolled-back `atomic()` block is a known flake
source) -- this module drives the same `AuthService`/`AccountService`
machinery through the same real HTTP routes."""

from __future__ import annotations

import re

import pytest
from django.core import mail
from django.test import override_settings
from rest_framework.test import APIClient

from core.security.auth.stores import flush_pending_email_deliveries

pytestmark = pytest.mark.django_db(transaction=True)

_TOKEN_LINE = re.compile(r"code if your client stripped the link: (\S+)")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_register_delivers_real_verification_email_through_real_django_email_sender(
    api_client: APIClient,
) -> None:
    """THE regression proof: register through the real view stack (no
    monkeypatched sender), flush pending deliveries, and assert the
    verification email actually reached `mail.outbox` -- proving delivery
    genuinely happens under this app's real `async_to_sync`-bridged
    execution model, not merely that it was scheduled."""
    response = api_client.post(
        "/auth/register",
        {"email": "dana@example.com", "password": "correct horse battery staple"},
        format="json",
    )
    assert response.status_code == 201, response.content

    # Deterministic sync point -- waits for the delivery this request's
    # `AccountService.request_email_verification` call just submitted to
    # the module-level thread pool to actually finish, no `time.sleep`.
    flush_pending_email_deliveries(timeout=5)

    assert len(mail.outbox) == 1
    delivered = mail.outbox[0]
    assert delivered.to == ["dana@example.com"]
    assert delivered.subject == "Verify your email address"

    # The token really is in the delivered body -- and it really is a
    # working, consumable verify token against the real endpoint, proving
    # this isn't just an empty/placeholder message.
    match = _TOKEN_LINE.search(delivered.body)
    assert match, f"no token line found in delivered body: {delivered.body!r}"
    token = match.group(1)

    verify_response = api_client.post("/auth/verify-email", {"token": token}, format="json")
    assert verify_response.status_code == 204

    login_response = api_client.post(
        "/auth/login",
        {"email": "dana@example.com", "password": "correct horse battery staple"},
        format="json",
        HTTP_X_AUTH_MODE="bearer",
    )
    assert login_response.status_code == 200
    assert "access_token" in login_response.json()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
def test_request_password_reset_delivers_real_email_through_real_django_email_sender(
    api_client: APIClient,
) -> None:
    """Same regression proof, for the OTHER caller of `DjangoEmailSender.
    send` that the review flagged as equally dead under the old
    implementation: `AccountService.request_password_reset`'s
    known-email branch."""
    register_response = api_client.post(
        "/auth/register",
        {"email": "erin@example.com", "password": "an original password"},
        format="json",
    )
    assert register_response.status_code == 201
    flush_pending_email_deliveries(timeout=5)
    mail.outbox.clear()

    reset_response = api_client.post(
        "/auth/request-password-reset", {"email": "erin@example.com"}, format="json"
    )
    assert reset_response.status_code == 202

    flush_pending_email_deliveries(timeout=5)

    assert len(mail.outbox) == 1
    delivered = mail.outbox[0]
    assert delivered.to == ["erin@example.com"]
    assert delivered.subject == "Reset your password"

    match = _TOKEN_LINE.search(delivered.body)
    assert match, f"no token line found in delivered body: {delivered.body!r}"
    token = match.group(1)

    reset_password_response = api_client.post(
        "/auth/reset-password",
        {"token": token, "new_password": "a brand new password"},
        format="json",
    )
    assert reset_password_response.status_code == 204

    login_response = api_client.post(
        "/auth/login",
        {"email": "erin@example.com", "password": "a brand new password"},
        format="json",
        HTTP_X_AUTH_MODE="bearer",
    )
    assert login_response.status_code == 200
