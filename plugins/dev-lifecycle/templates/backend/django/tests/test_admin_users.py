"""Stage 13b — admin user management, over real HTTP against `APIClient`.
The DRF counterpart to `backend/fastapi`'s `tests/test_admin_users.py` --
same test names/scenarios, ported to this block's `email_sender`/
`_register_and_verify`/`_login` helpers (`.test_auth`) and `seed_admin`
(`core.security.auth.stores`, Stage 5d's own `tests/test_cookie_auth.py`
precedent for `_seed_verified_admin`).

`@pytest.mark.django_db(transaction=True)` module-wide -- same rationale
`tests/test_auth.py`'s own module docstring documents in full: this
suite's admin actions go through `DjangoRefreshTokenStore.
revoke_all_for_user` (an `.aupdate()` durability argument that only holds
under real autocommit semantics) and Django's async ORM throughout."""

from __future__ import annotations

import logging

import pytest
from asgiref.sync import async_to_sync
from rest_framework.test import APIClient

from core.models import User
from core.security.auth.stores import seed_admin

from .test_auth import _CapturingEmailSender, _login, _register_and_verify, email_sender  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "correct horse battery staple 2"
_EMAIL = "alice@example.com"
_PASSWORD = "correct horse battery staple"


def _seed_verified_admin(email: str, password: str) -> str:
    """`seed_admin` (`core/security/auth/stores.py`) is the real
    admin-provisioning path -- then marks the row `email_verified=True` via
    a direct, plain sync ORM `.update()` call, matching `tests/
    test_cookie_auth.py`'s identically-named helper exactly. Returns the
    seeded user's id (a string)."""
    user = async_to_sync(seed_admin)(email, password)
    User.objects.filter(email=email).update(email_verified=True)
    return user.id


def _admin_headers(client: APIClient, email: str = _ADMIN_EMAIL, password: str = _ADMIN_PASSWORD) -> dict:
    _seed_verified_admin(email, password)
    tokens = _login(client, email, password)
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}


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
    api_client: APIClient, method: str, path: str, body: dict | None
) -> None:
    response = getattr(api_client, method)(path, body, format="json")
    assert response.status_code == 401, response.content
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_admin_user_endpoint_returns_403_for_a_non_admin(
    api_client: APIClient, email_sender: _CapturingEmailSender, method: str, path: str, body: dict | None
) -> None:
    _register_and_verify(api_client, email_sender)
    tokens = _login(api_client, _EMAIL, _PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
    response = getattr(api_client, method)(path, body, format="json", **headers)
    assert response.status_code == 403, response.content
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def test_list_users_happy_path_and_pagination_shape(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender, email="bob@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.get("/admin/users", **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert {"items", "total", "page", "size", "pages"} <= body.keys()
    assert body["total"] >= 2
    emails = {item["email"] for item in body["items"]}
    assert "bob@example.com" in emails
    assert _ADMIN_EMAIL in emails
    for item in body["items"]:
        assert "password_hash" not in item
        assert "roles" in item and "status" in item


def test_list_users_filters_by_q_case_insensitively(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    _register_and_verify(api_client, email_sender, email="carol@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.get("/admin/users", {"q": "CAROL"}, **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert [item["email"] for item in body["items"]] == ["carol@example.com"]


def test_list_users_filters_by_status(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="dave@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)
    api_client.post(f"/admin/users/{registered['id']}/suspend", **headers)

    response = api_client.get("/admin/users", {"status": "suspended"}, **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert [item["email"] for item in body["items"]] == ["dave@example.com"]
    assert body["items"][0]["status"] == "suspended"


def test_get_user_happy_path(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="erin@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.get(f"/admin/users/{registered['id']}", **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["email"] == "erin@example.com"
    assert body["status"] == "active"
    assert "password_hash" not in body


def test_get_user_returns_404_for_an_unknown_id(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    response = api_client.get(f"/admin/users/{_SOME_ID}", **headers)
    assert response.status_code == 404, response.content
    assert response.json()["error"]["code"] == "not_found"


def test_get_user_returns_404_for_a_malformed_id(api_client: APIClient) -> None:
    """A non-UUID path segment -- `<str:user_id>` (`core/urls.py`) routes
    it to `_get_admin_user` (`core/views.py`), which catches the
    `uuid.UUID(...)` failure and raises `NotFoundError` -- the SAME
    ErrorEnvelope-shaped 404 an unknown-but-valid UUID gets, never Django's
    raw, unenveloped routing-level 404 (see `_get_admin_user`'s own
    docstring)."""
    headers = _admin_headers(api_client)
    response = api_client.get("/admin/users/not-a-uuid", **headers)
    assert response.status_code == 404, response.content
    assert response.json()["error"]["code"] == "not_found"


# ---------------------------------------------------------------------------
# suspend / ban / reinstate — happy path, state-machine conflicts, audit
# ---------------------------------------------------------------------------


def test_suspend_then_reinstate_happy_path_and_audit(
    api_client: APIClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:

    registered = _register_and_verify(api_client, email_sender, email="frank@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(f"/admin/users/{registered['id']}/suspend", **headers)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "suspended"
    assert any('"action": "admin.user.suspend"' in r.message for r in caplog.records)
    assert any(f'"resource": "user:{registered["id"]}"' in r.message for r in caplog.records)
    assert any('"outcome": "success"' in r.message for r in caplog.records)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(f"/admin/users/{registered['id']}/reinstate", **headers)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "active"
    assert any('"action": "admin.user.reinstate"' in r.message for r in caplog.records)


def test_suspend_a_banned_user_is_a_conflict(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="gina@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)
    ban = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert ban.status_code == 200, ban.content

    response = api_client.post(f"/admin/users/{registered['id']}/suspend", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


def test_reinstate_an_active_user_is_a_conflict(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(api_client, email_sender, email="henry@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.post(f"/admin/users/{registered['id']}/reinstate", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


def test_ban_happy_path_and_audit(
    api_client: APIClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:

    registered = _register_and_verify(api_client, email_sender, email="ivan@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "banned"
    assert any('"action": "admin.user.ban"' in r.message for r in caplog.records)


def test_ban_an_already_banned_user_is_a_conflict(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(api_client, email_sender, email="jack@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)
    first = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert first.status_code == 200, first.content

    response = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert response.status_code == 409, response.content


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def test_set_roles_happy_path_and_audit(
    api_client: APIClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:

    registered = _register_and_verify(api_client, email_sender, email="karen@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.put(
            f"/admin/users/{registered['id']}/roles", {"roles": ["admin"]}, format="json", **headers
        )
    assert response.status_code == 200, response.content
    assert response.json()["roles"] == ["admin"]
    assert any('"action": "admin.user.roles_set"' in r.message for r in caplog.records)


def test_set_roles_rejects_an_unknown_role(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="leo@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.put(
        f"/admin/users/{registered['id']}/roles", {"roles": ["superuser"]}, format="json", **headers
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "validation_failed"


def test_set_roles_rejects_an_unknown_field(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """Parity with the FastAPI track's `AdminRolesIn` (`ConfigDict(extra=
    "forbid")`) -- an unknown top-level key in the request body 422s the
    same way on both backends, per `AdminRolesInSerializer.validate()`'s
    own docstring."""
    registered = _register_and_verify(api_client, email_sender, email="nora@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.put(
        f"/admin/users/{registered['id']}/roles",
        {"roles": ["admin"], "is_superuser": True},
        format="json",
        **headers,
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# force-verify
# ---------------------------------------------------------------------------


def test_force_verify_happy_path(api_client: APIClient) -> None:
    register_response = api_client.post(
        "/auth/register", {"email": "mia@example.com", "password": _PASSWORD}, format="json"
    )
    assert register_response.status_code == 201, register_response.content
    user_id = register_response.json()["id"]
    headers = _admin_headers(api_client)

    response = api_client.post(f"/admin/users/{user_id}/force-verify", **headers)
    assert response.status_code == 200, response.content
    assert response.json()["email_verified"] is True

    login = api_client.post("/auth/login", {"email": "mia@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 200, login.content


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_delete_user_soft_deletes(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="nina@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)

    response = api_client.delete(f"/admin/users/{registered['id']}", **headers)
    assert response.status_code == 204, response.content
    assert response.content == b""

    follow_up = api_client.get(f"/admin/users/{registered['id']}", **headers)
    assert follow_up.status_code == 404, follow_up.content


# ---------------------------------------------------------------------------
# Self-protection
# ---------------------------------------------------------------------------


def test_admin_cannot_suspend_self(api_client: APIClient) -> None:
    admin_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    tokens = _login(api_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}

    response = api_client.post(f"/admin/users/{admin_id}/suspend", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


def test_admin_cannot_ban_self(api_client: APIClient) -> None:
    admin_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    tokens = _login(api_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}

    response = api_client.post(f"/admin/users/{admin_id}/ban", **headers)
    assert response.status_code == 409, response.content


def test_admin_cannot_delete_self(api_client: APIClient) -> None:
    admin_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    tokens = _login(api_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}

    response = api_client.delete(f"/admin/users/{admin_id}", **headers)
    assert response.status_code == 409, response.content


def test_admin_cannot_remove_their_own_admin_role(api_client: APIClient) -> None:
    admin_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    tokens = _login(api_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}

    response = api_client.put(f"/admin/users/{admin_id}/roles", {"roles": []}, format="json", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# Ban enforcement: login/refresh, and session revocation on ban
# ---------------------------------------------------------------------------


def test_suspended_user_cannot_login(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="oscar@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)
    suspend = api_client.post(f"/admin/users/{registered['id']}/suspend", **headers)
    assert suspend.status_code == 200, suspend.content

    login = api_client.post("/auth/login", {"email": "oscar@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 401, login.content
    assert login.json()["error"]["code"] == "unauthenticated"


def test_banned_user_cannot_login(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    registered = _register_and_verify(api_client, email_sender, email="paula@example.com", password=_PASSWORD)
    headers = _admin_headers(api_client)
    ban = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert ban.status_code == 200, ban.content

    login = api_client.post("/auth/login", {"email": "paula@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 401, login.content


def test_active_user_can_still_login(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """Control case for the two tests above."""
    _register_and_verify(api_client, email_sender, email="quinn@example.com", password=_PASSWORD)

    login = api_client.post("/auth/login", {"email": "quinn@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 200, login.content


def test_ban_revokes_existing_refresh_tokens(api_client: APIClient, email_sender: _CapturingEmailSender) -> None:
    """Same proof as `backend/fastapi`'s identically-named test -- see that
    module's own docstring."""
    registered = _register_and_verify(api_client, email_sender, email="rosa@example.com", password=_PASSWORD)
    login = api_client.post("/auth/login", {"email": "rosa@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 200, login.content
    refresh_token = login.json()["refresh_token"]

    headers = _admin_headers(api_client)
    ban = api_client.post(f"/admin/users/{registered['id']}/ban", **headers)
    assert ban.status_code == 200, ban.content

    refresh = api_client.post("/auth/refresh", {"refresh_token": refresh_token}, format="json")
    assert refresh.status_code == 401, refresh.content


def test_suspend_revokes_existing_refresh_tokens(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    registered = _register_and_verify(api_client, email_sender, email="sam@example.com", password=_PASSWORD)
    login = api_client.post("/auth/login", {"email": "sam@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer")
    assert login.status_code == 200, login.content
    refresh_token = login.json()["refresh_token"]

    headers = _admin_headers(api_client)
    suspend = api_client.post(f"/admin/users/{registered['id']}/suspend", **headers)
    assert suspend.status_code == 200, suspend.content

    refresh = api_client.post("/auth/refresh", {"refresh_token": refresh_token}, format="json")
    assert refresh.status_code == 401, refresh.content
