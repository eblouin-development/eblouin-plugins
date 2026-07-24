"""Stage 13c — moderation admin queue, over real HTTP against the
hermetic client. Reuses `tests/test_auth.py`'s own fixtures/helpers
(`auth_client`, `email_sender`, `_register_and_verify`,
`_CapturingEmailSender`, `_make_auth_client`) and `tests/test_admin_users.
py`'s `_seed_verified_admin`/`_admin_headers` shape — same posture, see
those modules' own docstrings.

**Admin-only queue, no create endpoint.** There is no `POST /flags`
anywhere in this app — every `Flag` row this suite exercises is inserted
directly via the ORM (`_seed_flag`, below), the same "a consuming app
writes rows itself" posture `app/models/flag.py`'s own docstring
documents. Blog posts/comments used as flag targets are seeded the same
way `tests/test_blog.py` already does (`_create_post` over the real admin
API, `_seed_comment` direct-to-DB)."""

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
_PASSWORD = "correct horse battery staple"

_SOME_ID = "00000000-0000-0000-0000-000000000000"

_SIMPLE_DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}


@pytest.fixture()
def email_sender() -> _CapturingEmailSender:
    return _CapturingEmailSender()


@pytest.fixture()
def auth_client(make_client, email_sender: _CapturingEmailSender):
    return _make_auth_client(make_client, email_sender)


@pytest.fixture(autouse=True)
def _reset_admin_rate_limit() -> None:
    """Same test-isolation rationale as `tests/test_admin_users.py`'s
    identically-named fixture — this router reuses `admin.py`'s own
    `require_admin_rate_limit` dependency."""
    reset_admin_rate_limit_store_for_tests()


async def _seed_verified_admin(email: str, password: str) -> str:
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


def _create_post(client: TestClient, headers: dict, **overrides: object) -> dict:
    payload = {
        "title": "Hello World",
        "body_json": _SIMPLE_DOC,
        "body_html": "<p>Hello <strong>world</strong></p>",
        **overrides,
    }
    response = client.post("/admin/blog/posts", json=payload, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_comment_async(post_id: str, *, author_id: str | None = None, body: str = "Nice post!") -> str:
    from app.models.comment import Comment

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comment = Comment(
            post_id=uuid.UUID(post_id),
            author_id=uuid.UUID(author_id) if author_id else None,
            body=body,
            status="visible",
        )
        session.add(comment)
        await session.commit()
        await session.refresh(comment)
        return str(comment.id)


def _seed_comment(post_id: str, **kwargs: object) -> str:
    return asyncio.run(_seed_comment_async(post_id, **kwargs))


async def _seed_flag_async(
    target_type: str,
    target_id: str,
    *,
    reporter_id: str | None = None,
    reason: str = "This is spam.",
    status: str = "open",
) -> str:
    from app.models.flag import Flag

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        flag = Flag(
            target_type=target_type,
            target_id=uuid.UUID(target_id),
            reporter_id=uuid.UUID(reporter_id) if reporter_id else None,
            reason=reason,
            status=status,
        )
        session.add(flag)
        await session.commit()
        await session.refresh(flag)
        return str(flag.id)


def _seed_flag(target_type: str, target_id: str, **kwargs: object) -> str:
    return asyncio.run(_seed_flag_async(target_type, target_id, **kwargs))


async def _get_user_status_async(user_id: str) -> str:
    session_factory = get_sessionmaker()
    async with session_factory() as session:
        result = await session.execute(select(User).where(User.id == uuid.UUID(user_id)))
        return result.scalar_one().status


def _get_user_status(user_id: str) -> str:
    return asyncio.run(_get_user_status_async(user_id))


# ---------------------------------------------------------------------------
# 401 / 403 across every /admin/flags* endpoint
# ---------------------------------------------------------------------------

_ENDPOINTS: list[tuple[str, str, dict | None]] = [
    ("get", "/admin/flags", None),
    ("get", f"/admin/flags/{_SOME_ID}", None),
    ("post", f"/admin/flags/{_SOME_ID}/resolve", {"action": "none"}),
    ("post", f"/admin/flags/{_SOME_ID}/dismiss", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_flag_endpoint_returns_401_for_anonymous(
    auth_client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = auth_client.request(method.upper(), path, json=body)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_flag_endpoint_returns_403_for_a_non_admin(
    auth_client: TestClient, email_sender: _CapturingEmailSender, method: str, path: str, body: dict | None
) -> None:
    _register_and_verify(auth_client, email_sender, email="nonadmin@example.com", password=_PASSWORD)
    tokens = _login(auth_client, "nonadmin@example.com", _PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = auth_client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# Queue: list / get
# ---------------------------------------------------------------------------


def test_list_flags_happy_path_and_pagination_shape(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    _seed_flag("blog_post", post["id"], reason="Reported for spam")

    response = auth_client.get("/admin/flags", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"items", "total", "page", "size", "pages"} <= body.keys()
    assert body["total"] >= 1


def test_list_flags_filters_by_status(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    open_id = _seed_flag("blog_post", post["id"], reason="open one")
    dismissed_id = _seed_flag("blog_post", post["id"], reason="dismissed one", status="dismissed")

    response = auth_client.get("/admin/flags", params={"status": "open"}, headers=headers)
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert open_id in ids
    assert dismissed_id not in ids


def test_list_flags_filters_by_target_type(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = _seed_comment(post["id"])
    post_flag_id = _seed_flag("blog_post", post["id"])
    comment_flag_id = _seed_flag("comment", comment_id)

    response = auth_client.get("/admin/flags", params={"target_type": "comment"}, headers=headers)
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert comment_flag_id in ids
    assert post_flag_id not in ids


def test_get_flag_happy_path(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], reason="Reported for spam")

    response = auth_client.get(f"/admin/flags/{flag_id}", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reason"] == "Reported for spam"
    assert body["status"] == "open"
    assert body["target_type"] == "blog_post"
    assert body["target_id"] == post["id"]


def test_get_flag_returns_404_for_an_unknown_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.get(f"/admin/flags/{_SOME_ID}", headers=headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_get_flag_returns_404_for_a_malformed_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.get("/admin/flags/not-a-uuid", headers=headers)
    assert response.status_code in (404, 422), response.text


# ---------------------------------------------------------------------------
# resolve: action=none
# ---------------------------------------------------------------------------


def test_resolve_none_marks_flag_resolved_and_audits(
    auth_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(
            f"/admin/flags/{flag_id}/resolve", json={"action": "none", "note": "looked fine"}, headers=headers
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution_note"] == "looked fine"
    assert body["resolved_at"] is not None
    assert any('"action": "admin.flag.resolve"' in r.message for r in caplog.records)
    assert any(f'"resource": "flag:{flag_id}"' in r.message for r in caplog.records)

    # Post untouched.
    follow_up = auth_client.get(f"/admin/blog/posts/{post['id']}", headers=headers)
    assert follow_up.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# resolve: action=hide_content
# ---------------------------------------------------------------------------


def test_resolve_hide_content_hides_a_comment(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = _seed_comment(post["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "hide_content"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"

    comments = auth_client.get("/admin/blog/comments", params={"post_id": post["id"]}, headers=headers)
    hidden = next(item for item in comments.json()["items"] if item["id"] == comment_id)
    assert hidden["status"] == "hidden"


def test_resolve_hide_content_unpublishes_a_blog_post(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    publish = auth_client.post(f"/admin/blog/posts/{post['id']}/publish", headers=headers)
    assert publish.status_code == 200, publish.text
    flag_id = _seed_flag("blog_post", post["id"])

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "hide_content"}, headers=headers)
    assert response.status_code == 200, response.text

    follow_up = auth_client.get(f"/admin/blog/posts/{post['id']}", headers=headers)
    body = follow_up.json()
    assert body["status"] == "draft"
    assert body["published_at"] is None


def test_resolve_hide_content_on_a_user_target_is_422(auth_client: TestClient, email_sender) -> None:
    headers = _admin_headers(auth_client)
    target = _register_and_verify(auth_client, email_sender, email="target@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "hide_content"}, headers=headers)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_resolve_hide_content_returns_404_for_missing_target(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    flag_id = _seed_flag("comment", _SOME_ID)

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "hide_content"}, headers=headers)
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# resolve: action=delete_content
# ---------------------------------------------------------------------------


def test_resolve_delete_content_soft_deletes_a_comment(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = _seed_comment(post["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = auth_client.post(
        f"/admin/flags/{flag_id}/resolve", json={"action": "delete_content"}, headers=headers
    )
    assert response.status_code == 200, response.text

    comments = auth_client.get("/admin/blog/comments", params={"post_id": post["id"]}, headers=headers)
    ids = {item["id"] for item in comments.json()["items"]}
    assert comment_id not in ids


def test_resolve_delete_content_soft_deletes_a_blog_post(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    response = auth_client.post(
        f"/admin/flags/{flag_id}/resolve", json={"action": "delete_content"}, headers=headers
    )
    assert response.status_code == 200, response.text

    follow_up = auth_client.get(f"/admin/blog/posts/{post['id']}", headers=headers)
    assert follow_up.status_code == 404, follow_up.text


def test_resolve_delete_content_on_a_user_target_is_422(auth_client: TestClient, email_sender) -> None:
    headers = _admin_headers(auth_client)
    target = _register_and_verify(auth_client, email_sender, email="target2@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = auth_client.post(
        f"/admin/flags/{flag_id}/resolve", json={"action": "delete_content"}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# resolve: action=ban_author
# ---------------------------------------------------------------------------


def test_resolve_ban_author_bans_a_blog_post_author(
    auth_client: TestClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    # Second admin authors the post so it isn't the resolving admin's own.
    author_id = asyncio.run(_seed_verified_admin("author@example.com", _PASSWORD))
    author_tokens = _login(auth_client, "author@example.com", _PASSWORD)
    author_headers = {"Authorization": f"Bearer {author_tokens['access_token']}"}
    post = _create_post(auth_client, author_headers)

    headers = _admin_headers(auth_client)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "ban_author"}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "resolved"
    assert _get_user_status(author_id) == "banned"
    assert any('"action": "admin.flag.resolve"' in r.message for r in caplog.records)


def test_resolve_ban_author_bans_a_comment_author_and_revokes_sessions(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    author = _register_and_verify(auth_client, email_sender, email="commenter@example.com", password=_PASSWORD)
    login = auth_client.post("/auth/login", json={"email": "commenter@example.com", "password": _PASSWORD}, headers={"X-Auth-Mode": "bearer"})
    assert login.status_code == 200, login.text
    refresh_token = login.json()["refresh_token"]

    comment_id = _seed_comment(post["id"], author_id=author["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "ban_author"}, headers=headers)
    assert response.status_code == 200, response.text
    assert _get_user_status(author["id"]) == "banned"

    refresh = auth_client.post("/auth/refresh", json={"refresh_token": refresh_token})
    assert refresh.status_code == 401, refresh.text


def test_resolve_ban_author_on_a_user_target_bans_that_user(
    auth_client: TestClient, email_sender: _CapturingEmailSender
) -> None:
    headers = _admin_headers(auth_client)
    target = _register_and_verify(auth_client, email_sender, email="banme@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "ban_author"}, headers=headers)
    assert response.status_code == 200, response.text
    assert _get_user_status(target["id"]) == "banned"


def test_resolve_ban_author_returns_404_for_a_comment_with_no_author(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = _seed_comment(post["id"], author_id=None)
    flag_id = _seed_flag("comment", comment_id)

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "ban_author"}, headers=headers)
    assert response.status_code == 404, response.text


def test_resolve_ban_author_self_protection_is_a_conflict(auth_client: TestClient) -> None:
    admin_id = asyncio.run(_seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD))
    tokens = _login(auth_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    flag_id = _seed_flag("user", admin_id)

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "ban_author"}, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"
    assert _get_user_status(admin_id) == "active"


# ---------------------------------------------------------------------------
# resolve/dismiss state machine
# ---------------------------------------------------------------------------


def test_resolve_an_already_resolved_flag_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="resolved")

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "none"}, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_resolve_returns_404_for_an_unknown_flag(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.post(f"/admin/flags/{_SOME_ID}/resolve", json={"action": "none"}, headers=headers)
    assert response.status_code == 404, response.text


def test_resolve_unknown_action_is_422(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    response = auth_client.post(f"/admin/flags/{flag_id}/resolve", json={"action": "nuke"}, headers=headers)
    assert response.status_code == 422, response.text


# ---------------------------------------------------------------------------
# dismiss
# ---------------------------------------------------------------------------


def test_dismiss_happy_path_and_audit(auth_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(
            f"/admin/flags/{flag_id}/dismiss", json={"note": "not a real issue"}, headers=headers
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "dismissed"
    assert body["resolution_note"] == "not a real issue"
    assert any('"action": "admin.flag.dismiss"' in r.message for r in caplog.records)

    # Content untouched.
    follow_up = auth_client.get(f"/admin/blog/posts/{post['id']}", headers=headers)
    assert follow_up.status_code == 200, follow_up.text


def test_dismiss_an_already_dismissed_flag_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="dismissed")

    response = auth_client.post(f"/admin/flags/{flag_id}/dismiss", json={}, headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_dismiss_a_resolved_flag_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="resolved")

    response = auth_client.post(f"/admin/flags/{flag_id}/dismiss", json={}, headers=headers)
    assert response.status_code == 409, response.text


def test_dismiss_returns_404_for_an_unknown_flag(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.post(f"/admin/flags/{_SOME_ID}/dismiss", json={}, headers=headers)
    assert response.status_code == 404, response.text
