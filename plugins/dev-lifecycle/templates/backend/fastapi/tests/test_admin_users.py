"""Stage 13b — admin user management, over real HTTP against the hermetic
client. Reuses `tests/test_auth.py`'s own fixtures/helpers (`auth_client`,
`email_sender`, `_register_and_verify`, `_CapturingEmailSender`,
`_make_auth_client`) — see that module's own docstring for why `auth_client`
(not the plain `client` fixture) is required for anything that calls a real
auth endpoint. `_seed_verified_admin` mirrors `tests/test_cookie_auth.py`'s
identically-named helper for the RBAC admin example this stage extends."""

from __future__ import annotations

import asyncio
import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.api.routers.admin import reset_admin_rate_limit_store_for_tests
from app.core.db import get_sessionmaker
from app.core.security.auth.stores import seed_admin
from app.models.user import User

from .test_auth import _CapturingEmailSender, _make_auth_client, _register_and_verify

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "correct horse battery staple 2"
_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"


@pytest.fixture()
def email_sender() -> _CapturingEmailSender:
    """Own instance, not a cross-module fixture reuse -- see `tests/
    test_cookie_auth.py`'s identically-named fixture's own docstring for
    why a pytest fixture defined in another test module isn't directly
    reusable as a test parameter here."""
    return _CapturingEmailSender()


@pytest.fixture()
def auth_client(make_client, email_sender: _CapturingEmailSender):
    return _make_auth_client(make_client, email_sender)


@pytest.fixture(autouse=True)
def _reset_admin_rate_limit() -> None:
    """Same test-isolation rationale as `core/security/rate_limiting/
    django.py`'s own `_default_store` / `tests/conftest.py`'s
    `_reset_rate_limit_store` fixture (Django track) — this suite's admin
    tests share ONE module-level `InMemoryBucketStore` across every test in
    this process (`app/api/routers/admin.py`'s own `require_admin_rate_limit`
    docstring), so without a reset a bucket a later test's own burst could
    trip a 429 that belongs to an EARLIER test's traffic."""
    reset_admin_rate_limit_store_for_tests()


async def _seed_verified_admin(email: str, password: str) -> str:
    """`seed_admin` (`app/core/security/auth/stores.py`) is the real
    admin-provisioning path — then marks the row `email_verified=True` by
    hand (bypassing the email flow entirely, same as `tests/
    test_cookie_auth.py`'s own identically-named helper) so `AuthService.
    login`'s `require_verification` gate doesn't block the login this
    suite's tests need next. Returns the seeded user's id (a string)."""
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        user = await seed_admin(session, email, password)
        result = await session.execute(select(User).where(User.id == uuid.UUID(user.id)))
        row = result.scalar_one()
        row.email_verified = True
        await session.commit()
        return user.id


def _login(client: TestClient, email: str, password: str) -> dict:
    response = client.post("/auth/login", json={"email": email, "password": password}, headers={"X-Auth-Mode": "bearer"})
    assert response.status_code == 200, response.text
    return response.json()


def _admin_headers(client: TestClient, email: str = _ADMIN_EMAIL, password: str = _ADMIN_PASSWORD) -> dict:
    asyncio.run(_seed_verified_admin(email, password))
    tokens = _login(client, email, password)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


# ---------------------------------------------------------------------------
# 401 / 403 across every /admin/users* endpoint
# ---------------------------------------------------------------------------

_SOME_ID = "00000000-0000-0000-0000-000000000000"

_ENDPOINTS: list[tuple[str, str, dict | None]] = [
    ("get", "/admin/users", None),
    ("get", f"/admin/users/{_SOME_ID}", None),
    ("post", f"/admin/users/{_SOME_ID}/suspend", None),
    ("post", f"/admin/users/{_SOME_ID}/ban", None),
    ("post", f"/admin/users/{_SOME_ID}/reinstate", None),
    ("put", f"/admin/users/{_SOME_ID}/roles", {"roles": []}),
    ("post", f"/admin/users/{_SOME_ID}/force-verify", None),
    ("delete", f"/admin/users/{_SOME_ID}", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_admin_user_endpoint_returns_401_for_anonymous(
    auth_client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = auth_client.request(method.upper(), path, json=body)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_admin_user_endpoint_returns_403_for_a_non_admin(
    auth_client: TestClient, email_sender: _CapturingEmailSender, method: str, path: str, body: dict | None
) -> None:
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client, _EMAIL, _PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = auth_client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def test_list_users_happy_path_and_pagination_shape(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender, email="bob@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.get("/admin/users", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"items", "total", "page", "size", "pages"} <= body.keys()
    assert body["total"] >= 2  # the admin + bob
    emails = {item["email"] for item in body["items"]}
    assert "bob@example.com" in emails
    assert _ADMIN_EMAIL in emails
    for item in body["items"]:
        assert "password_hash" not in item
        assert "roles" in item and "status" in item


def test_list_users_filters_by_q_case_insensitively(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(auth_client, email_sender, email="carol@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.get("/admin/users", params={"q": "CAROL"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["email"] for item in body["items"]] == ["carol@example.com"]


def test_list_users_filters_by_status(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="dave@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)
    auth_client.post(f"/admin/users/{registered['id']}/suspend", headers=headers)

    response = auth_client.get("/admin/users", params={"status": "suspended"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert [item["email"] for item in body["items"]] == ["dave@example.com"]
    assert body["items"][0]["status"] == "suspended"


def test_get_user_happy_path(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="erin@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.get(f"/admin/users/{registered['id']}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == "erin@example.com"
    assert body["status"] == "active"
    assert "password_hash" not in body


def test_get_user_returns_404_for_an_unknown_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.get(f"/admin/users/{_SOME_ID}", headers=headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# suspend / ban / reinstate — happy path, state-machine conflicts, audit
# ---------------------------------------------------------------------------


def test_suspend_then_reinstate_happy_path_and_audit(
    auth_client: TestClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="frank@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/users/{registered['id']}/suspend", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "suspended"
    assert any('"action": "admin.user.suspend"' in r.message for r in caplog.records)
    assert any(f'"resource": "user:{registered["id"]}"' in r.message for r in caplog.records)
    assert any('"outcome": "success"' in r.message for r in caplog.records)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/users/{registered['id']}/reinstate", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "active"
    assert any('"action": "admin.user.reinstate"' in r.message for r in caplog.records)


def test_suspend_a_banned_user_is_a_conflict(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="gina@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)
    ban = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert ban.status_code == 200, ban.text

    response = auth_client.post(f"/admin/users/{registered['id']}/suspend", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_reinstate_an_active_user_is_a_conflict(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="henry@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.post(f"/admin/users/{registered['id']}/reinstate", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_ban_happy_path_and_audit(
    auth_client: TestClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="ivan@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "banned"
    assert any('"action": "admin.user.ban"' in r.message for r in caplog.records)


def test_ban_an_already_banned_user_is_a_conflict(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="jack@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)
    first = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert first.status_code == 200, first.text

    response = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert response.status_code == 409, response.text


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def test_set_roles_happy_path_and_audit(
    auth_client: TestClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="karen@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.put(
            f"/admin/users/{registered['id']}/roles", json={"roles": ["admin"]}, headers=headers
        )
    assert response.status_code == 200, response.text
    assert response.json()["roles"] == ["admin"]
    assert any('"action": "admin.user.roles_set"' in r.message for r in caplog.records)


def test_set_roles_rejects_an_unknown_role(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="leo@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.put(
        f"/admin/users/{registered['id']}/roles", json={"roles": ["superuser"]}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# force-verify
# ---------------------------------------------------------------------------


def test_force_verify_happy_path(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    # Register but do NOT verify -- an unverified user.
    register_response = auth_client.post(
        "/auth/register", json={"email": "mia@example.com", "password": _PASSWORD}
    )
    assert register_response.status_code == 201, register_response.text
    user_id = register_response.json()["id"]
    headers = _admin_headers(auth_client)

    response = auth_client.post(f"/admin/users/{user_id}/force-verify", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["email_verified"] is True

    # Now the user can log in without going through /auth/verify-email.
    login = auth_client.post("/auth/login", json={"email": "mia@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 200, login.text


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_user_soft_deletes(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="nina@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)

    response = auth_client.delete(f"/admin/users/{registered['id']}", headers=headers)
    assert response.status_code == 204, response.text
    assert response.content == b""

    follow_up = auth_client.get(f"/admin/users/{registered['id']}", headers=headers)
    assert follow_up.status_code == 404, follow_up.text


# ---------------------------------------------------------------------------
# Self-protection
# ---------------------------------------------------------------------------


def test_admin_cannot_suspend_self(auth_client: TestClient) -> None:
    admin_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    tokens = _login(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = auth_client.post(f"/admin/users/{admin_id}/suspend", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_admin_cannot_ban_self(auth_client: TestClient) -> None:
    admin_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    tokens = _login(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = auth_client.post(f"/admin/users/{admin_id}/ban", headers=headers)
    assert response.status_code == 409, response.text


def test_admin_cannot_delete_self(auth_client: TestClient) -> None:
    admin_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    tokens = _login(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = auth_client.delete(f"/admin/users/{admin_id}", headers=headers)
    assert response.status_code == 409, response.text


def test_admin_cannot_remove_their_own_admin_role(auth_client: TestClient) -> None:
    admin_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    tokens = _login(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    response = auth_client.put(f"/admin/users/{admin_id}/roles", json={"roles": []}, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# Ban enforcement: login/refresh, and session revocation on ban
# ---------------------------------------------------------------------------


def test_suspended_user_cannot_login(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="oscar@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)
    suspend = auth_client.post(f"/admin/users/{registered['id']}/suspend", headers=headers)
    assert suspend.status_code == 200, suspend.text

    login = auth_client.post("/auth/login", json={"email": "oscar@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 401, login.text
    assert login.json()["error"]["code"] == "unauthenticated"


def test_banned_user_cannot_login(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="paula@example.com", password=_PASSWORD)
    headers = _admin_headers(auth_client)
    ban = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert ban.status_code == 200, ban.text

    login = auth_client.post("/auth/login", json={"email": "paula@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 401, login.text


def test_active_user_can_still_login(auth_client: TestClient, email_sender: _CapturingEmailSender) -> None:
    """Control case for the two tests above -- proves the `status=="active"`
    filter isn't rejecting everyone."""
    _register_and_verify(auth_client, email_sender, email="quinn@example.com", password=_PASSWORD)

    login = auth_client.post("/auth/login", json={"email": "quinn@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 200, login.text


def test_ban_revokes_existing_refresh_tokens(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    """Logs in as a real user (minting a real refresh token), then has an
    admin ban that user, then proves the ALREADY-ISSUED refresh token no
    longer works -- `RefreshTokenStore.revoke_all_for_user`
    (`app/api/routers/admin.py`'s `ban_admin_user`) is what actually kills
    the session; the `status=="active"` filter alone would only stop a
    FUTURE login/refresh lookup, not a session already in flight."""
    registered = _register_and_verify(auth_client, email_sender, email="rosa@example.com", password=_PASSWORD)
    login = auth_client.post("/auth/login", json={"email": "rosa@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    headers = _admin_headers(auth_client)
    ban = auth_client.post(f"/admin/users/{registered['id']}/ban", headers=headers)
    assert ban.status_code == 200, ban.text

    refresh = auth_client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401, refresh.text


def test_suspend_revokes_existing_refresh_tokens(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(auth_client, email_sender, email="sam@example.com", password=_PASSWORD)
    login = auth_client.post("/auth/login", json={"email": "sam@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    headers = _admin_headers(auth_client)
    suspend = auth_client.post(f"/admin/users/{registered['id']}/suspend", headers=headers)
    assert suspend.status_code == 200, suspend.text

    refresh = auth_client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401, refresh.text
