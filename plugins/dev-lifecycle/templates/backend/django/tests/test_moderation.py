"""Stage 13c — moderation admin queue, over real HTTP against `APIClient`.
The DRF counterpart to `backend/fastapi`'s `tests/test_moderation.py` --
same test names/scenarios, ported to this block's `email_sender`/
`_register_and_verify`/`_login` helpers (`.test_auth`) and `seed_admin`
(`core.security.auth.stores`, `tests/test_admin_users.py`'s own
`_seed_verified_admin` precedent).

**Admin-only queue, no create endpoint.** There is no `POST /flags`
anywhere in this app -- every `Flag` row this suite exercises is inserted
directly via the ORM (`_seed_flag`, below), same posture `tests/
test_blog.py`'s own `_seed_comment` documents for `Comment`.

`@pytest.mark.django_db(transaction=True)` module-wide -- same rationale
`tests/test_admin_users.py`'s own module docstring documents in full."""

from __future__ import annotations

import logging
import uuid

import pytest
from asgiref.sync import async_to_sync
from rest_framework.test import APIClient

from core.models import Comment, Flag, User
from core.security.auth.stores import seed_admin

from .test_auth import (  # noqa: F401
    _CapturingEmailSender,
    _login,
    _register_and_verify,
    email_sender,
)

pytestmark = pytest.mark.django_db(transaction=True)

_ADMIN_EMAIL = "admin@example.com"
_ADMIN_PASSWORD = "correct horse battery staple 2"
_PASSWORD = "correct horse battery staple"

_SOME_ID = "00000000-0000-0000-0000-000000000000"

_SIMPLE_DOC = {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}


def _seed_verified_admin(email: str, password: str) -> str:
    user = async_to_sync(seed_admin)(email, password)
    User.objects.filter(email=email).update(email_verified=True)
    return user.id


def _admin_headers(client: APIClient, email: str = _ADMIN_EMAIL, password: str = _ADMIN_PASSWORD) -> dict:
    _seed_verified_admin(email, password)
    tokens = _login(client, email, password)
    return {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}


def _create_post(client: APIClient, headers: dict, **overrides: object) -> dict:
    payload = {
        "title": "Hello World",
        "body_json": _SIMPLE_DOC,
        "body_html": "<p>Hello <strong>world</strong></p>",
        **overrides,
    }
    response = client.post("/admin/blog/posts", payload, format="json", **headers)
    assert response.status_code == 201, response.content
    return response.json()


def _seed_comment(post_id: str, *, author_id: str | None = None, body: str = "Nice post!") -> str:
    comment = Comment.objects.create(
        post_id=uuid.UUID(post_id),
        author_id=uuid.UUID(author_id) if author_id else None,
        body=body,
        status="visible",
    )
    return str(comment.id)


def _seed_flag(
    target_type: str,
    target_id: str,
    *,
    reporter_id: str | None = None,
    reason: str = "This is spam.",
    status: str = "open",
) -> str:
    flag = Flag.objects.create(
        target_type=target_type,
        target_id=uuid.UUID(target_id),
        reporter_id=uuid.UUID(reporter_id) if reporter_id else None,
        reason=reason,
        status=status,
    )
    return str(flag.id)


def _get_user_status(user_id: str) -> str:
    return User.objects.get(id=uuid.UUID(user_id)).status


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
    api_client: APIClient, method: str, path: str, body: dict | None
) -> None:
    response = getattr(api_client, method)(path, body, format="json")
    assert response.status_code == 401, response.content
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_flag_endpoint_returns_403_for_a_non_admin(
    api_client: APIClient, email_sender: _CapturingEmailSender, method: str, path: str, body: dict | None
) -> None:
    _register_and_verify(api_client, email_sender, email="nonadmin@example.com", password=_PASSWORD)
    tokens = _login(api_client, "nonadmin@example.com", _PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
    response = getattr(api_client, method)(path, body, format="json", **headers)
    assert response.status_code == 403, response.content
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# Queue: list / get
# ---------------------------------------------------------------------------


def test_list_flags_happy_path_and_pagination_shape(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    _seed_flag("blog_post", post["id"], reason="Reported for spam")

    response = api_client.get("/admin/flags", **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert {"items", "total", "page", "size", "pages"} <= body.keys()
    assert body["total"] >= 1


def test_list_flags_filters_by_status(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    open_id = _seed_flag("blog_post", post["id"], reason="open one")
    dismissed_id = _seed_flag("blog_post", post["id"], reason="dismissed one", status="dismissed")

    response = api_client.get("/admin/flags", {"status": "open"}, **headers)
    assert response.status_code == 200, response.content
    ids = {item["id"] for item in response.json()["items"]}
    assert open_id in ids
    assert dismissed_id not in ids


def test_list_flags_filters_by_target_type(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    comment_id = _seed_comment(post["id"])
    post_flag_id = _seed_flag("blog_post", post["id"])
    comment_flag_id = _seed_flag("comment", comment_id)

    response = api_client.get("/admin/flags", {"target_type": "comment"}, **headers)
    assert response.status_code == 200, response.content
    ids = {item["id"] for item in response.json()["items"]}
    assert comment_flag_id in ids
    assert post_flag_id not in ids


def test_get_flag_happy_path(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], reason="Reported for spam")

    response = api_client.get(f"/admin/flags/{flag_id}", **headers)
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["reason"] == "Reported for spam"
    assert body["status"] == "open"
    assert body["target_type"] == "blog_post"
    assert body["target_id"] == post["id"]


def test_get_flag_returns_404_for_an_unknown_id(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    response = api_client.get(f"/admin/flags/{_SOME_ID}", **headers)
    assert response.status_code == 404, response.content
    assert response.json()["error"]["code"] == "not_found"


def test_get_flag_returns_404_for_a_malformed_id(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    response = api_client.get("/admin/flags/not-a-uuid", **headers)
    assert response.status_code == 404, response.content


# ---------------------------------------------------------------------------
# resolve: action=none
# ---------------------------------------------------------------------------


def test_resolve_none_marks_flag_resolved_and_audits(
    api_client: APIClient, caplog: pytest.LogCaptureFixture
) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(
            f"/admin/flags/{flag_id}/resolve", {"action": "none", "note": "looked fine"}, format="json", **headers
        )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "resolved"
    assert body["resolution_note"] == "looked fine"
    assert body["resolved_at"] is not None
    assert any('"action": "admin.flag.resolve"' in r.message for r in caplog.records)
    assert any(f'"resource": "flag:{flag_id}"' in r.message for r in caplog.records)

    follow_up = api_client.get(f"/admin/blog/posts/{post['id']}", **headers)
    assert follow_up.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# resolve: action=hide_content
# ---------------------------------------------------------------------------


def test_resolve_hide_content_hides_a_comment(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    comment_id = _seed_comment(post["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "hide_content"}, format="json", **headers
    )
    assert response.status_code == 200, response.content

    comments = api_client.get("/admin/blog/comments", {"post_id": post["id"]}, **headers)
    hidden = next(item for item in comments.json()["items"] if item["id"] == comment_id)
    assert hidden["status"] == "hidden"


def test_resolve_hide_content_unpublishes_a_blog_post(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    publish = api_client.post(f"/admin/blog/posts/{post['id']}/publish", **headers)
    assert publish.status_code == 200, publish.content
    flag_id = _seed_flag("blog_post", post["id"])

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "hide_content"}, format="json", **headers
    )
    assert response.status_code == 200, response.content

    follow_up = api_client.get(f"/admin/blog/posts/{post['id']}", **headers)
    body = follow_up.json()
    assert body["status"] == "draft"
    assert body["published_at"] is None


def test_resolve_hide_content_on_a_user_target_is_422(api_client: APIClient, email_sender) -> None:
    headers = _admin_headers(api_client)
    target = _register_and_verify(api_client, email_sender, email="target@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "hide_content"}, format="json", **headers
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "validation_failed"


def test_resolve_hide_content_returns_404_for_missing_target(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    flag_id = _seed_flag("comment", _SOME_ID)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "hide_content"}, format="json", **headers
    )
    assert response.status_code == 404, response.content


# ---------------------------------------------------------------------------
# resolve: action=delete_content
# ---------------------------------------------------------------------------


def test_resolve_delete_content_soft_deletes_a_comment(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    comment_id = _seed_comment(post["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "delete_content"}, format="json", **headers
    )
    assert response.status_code == 200, response.content

    comments = api_client.get("/admin/blog/comments", {"post_id": post["id"]}, **headers)
    ids = {item["id"] for item in comments.json()["items"]}
    assert comment_id not in ids


def test_resolve_delete_content_soft_deletes_a_blog_post(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "delete_content"}, format="json", **headers
    )
    assert response.status_code == 200, response.content

    follow_up = api_client.get(f"/admin/blog/posts/{post['id']}", **headers)
    assert follow_up.status_code == 404, follow_up.content


def test_resolve_delete_content_on_a_user_target_is_422(api_client: APIClient, email_sender) -> None:
    headers = _admin_headers(api_client)
    target = _register_and_verify(api_client, email_sender, email="target2@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "delete_content"}, format="json", **headers
    )
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# resolve: action=ban_author
# ---------------------------------------------------------------------------


def test_resolve_ban_author_bans_a_blog_post_author(
    api_client: APIClient, email_sender: _CapturingEmailSender, caplog: pytest.LogCaptureFixture
) -> None:
    # Second admin authors the post so it isn't the resolving admin's own.
    author_id = _seed_verified_admin("author@example.com", _PASSWORD)
    author_tokens = _login(api_client, "author@example.com", _PASSWORD)
    author_headers = {"HTTP_AUTHORIZATION": f"Bearer {author_tokens['access_token']}"}
    post = _create_post(api_client, author_headers)

    headers = _admin_headers(api_client)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(
            f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
        )
    assert response.status_code == 200, response.content
    assert response.json()["status"] == "resolved"
    assert _get_user_status(author_id) == "banned"
    assert any('"action": "admin.flag.resolve"' in r.message for r in caplog.records)


def test_resolve_ban_author_bans_a_comment_author_and_revokes_sessions(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    author = _register_and_verify(api_client, email_sender, email="commenter@example.com", password=_PASSWORD)
    login = api_client.post(
        "/auth/login", {"email": "commenter@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert login.status_code == 200, login.content
    refresh_token = login.json()["refresh_token"]

    comment_id = _seed_comment(post["id"], author_id=author["id"])
    flag_id = _seed_flag("comment", comment_id)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
    )
    assert response.status_code == 200, response.content
    assert _get_user_status(author["id"]) == "banned"

    refresh = api_client.post("/auth/refresh", {"refresh_token": refresh_token}, format="json")
    assert refresh.status_code == 401, refresh.content


def test_resolve_ban_author_on_a_user_target_bans_that_user(
    api_client: APIClient, email_sender: _CapturingEmailSender
) -> None:
    headers = _admin_headers(api_client)
    target = _register_and_verify(api_client, email_sender, email="banme@example.com", password=_PASSWORD)
    flag_id = _seed_flag("user", target["id"])

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
    )
    assert response.status_code == 200, response.content
    assert _get_user_status(target["id"]) == "banned"


def test_resolve_ban_author_returns_404_for_a_comment_with_no_author(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    comment_id = _seed_comment(post["id"], author_id=None)
    flag_id = _seed_flag("comment", comment_id)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
    )
    assert response.status_code == 404, response.content


def test_resolve_ban_author_rolls_back_the_ban_if_flag_save_fails(
    api_client: APIClient, email_sender: _CapturingEmailSender, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves `AdminFlagResolveView.post`'s `transaction.atomic()` wrap
    (review fix, Stage 13c) genuinely covers `_ban_user`'s writes -- ban +
    session revocation -- not just the trailing `flag.save()`: forcing
    `flag.save()` to raise must roll the whole sequence back together, so a
    mid-operation failure can never leave a banned user (with revoked
    sessions) behind a flag that still reports `open`."""
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    author = _register_and_verify(api_client, email_sender, email="rollback@example.com", password=_PASSWORD)
    login = api_client.post(
        "/auth/login", {"email": "rollback@example.com", "password": _PASSWORD}, format="json", HTTP_X_AUTH_MODE="bearer"
    )
    assert login.status_code == 200, login.content
    refresh_token = login.json()["refresh_token"]

    comment_id = _seed_comment(post["id"], author_id=author["id"])
    flag_id = _seed_flag("comment", comment_id)

    def _boom(self, *args, **kwargs) -> None:
        raise RuntimeError("simulated flag.save failure")

    # Only the FLAG's own `.save()` is forced to fail -- `_ban_user`'s
    # `user.save(...)` and the ORM writes underneath `revoke_all_for_user`
    # are untouched, so this isolates "did the atomic block roll THOSE
    # back too" from "did flag.save() itself get called".
    monkeypatch.setattr(Flag, "save", _boom)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
    )
    assert response.status_code == 500, response.content

    monkeypatch.undo()
    assert _get_user_status(author["id"]) == "active"
    assert Flag.objects.get(id=uuid.UUID(flag_id)).status == "open"

    # The ban's session revocation must have rolled back too -- the
    # commenter's refresh token (issued before the failed resolve) is still
    # valid, proving `revoke_all_for_user`'s write didn't escape the atomic
    # block onto a separate, un-rolled-back connection.
    refresh = api_client.post("/auth/refresh", {"refresh_token": refresh_token}, format="json")
    assert refresh.status_code == 200, refresh.content


def test_resolve_ban_author_self_protection_is_a_conflict(api_client: APIClient) -> None:
    admin_id = _seed_verified_admin(_ADMIN_EMAIL, _ADMIN_PASSWORD)
    tokens = _login(api_client, _ADMIN_EMAIL, _ADMIN_PASSWORD)
    headers = {"HTTP_AUTHORIZATION": f"Bearer {tokens['access_token']}"}
    flag_id = _seed_flag("user", admin_id)

    response = api_client.post(
        f"/admin/flags/{flag_id}/resolve", {"action": "ban_author"}, format="json", **headers
    )
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"
    assert _get_user_status(admin_id) == "active"


# ---------------------------------------------------------------------------
# resolve/dismiss state machine
# ---------------------------------------------------------------------------


def test_resolve_an_already_resolved_flag_is_a_conflict(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="resolved")

    response = api_client.post(f"/admin/flags/{flag_id}/resolve", {"action": "none"}, format="json", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


def test_resolve_returns_404_for_an_unknown_flag(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    response = api_client.post(f"/admin/flags/{_SOME_ID}/resolve", {"action": "none"}, format="json", **headers)
    assert response.status_code == 404, response.content


def test_resolve_unknown_action_is_422(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    response = api_client.post(f"/admin/flags/{flag_id}/resolve", {"action": "nuke"}, format="json", **headers)
    assert response.status_code == 422, response.content


# ---------------------------------------------------------------------------
# dismiss
# ---------------------------------------------------------------------------


def test_dismiss_happy_path_and_audit(api_client: APIClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"])

    with caplog.at_level(logging.INFO, logger="audit"):
        response = api_client.post(
            f"/admin/flags/{flag_id}/dismiss", {"note": "not a real issue"}, format="json", **headers
        )
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["status"] == "dismissed"
    assert body["resolution_note"] == "not a real issue"
    assert any('"action": "admin.flag.dismiss"' in r.message for r in caplog.records)

    follow_up = api_client.get(f"/admin/blog/posts/{post['id']}", **headers)
    assert follow_up.status_code == 200, follow_up.content


def test_dismiss_an_already_dismissed_flag_is_a_conflict(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="dismissed")

    response = api_client.post(f"/admin/flags/{flag_id}/dismiss", {}, format="json", **headers)
    assert response.status_code == 409, response.content
    assert response.json()["error"]["code"] == "conflict"


def test_dismiss_a_resolved_flag_is_a_conflict(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    post = _create_post(api_client, headers)
    flag_id = _seed_flag("blog_post", post["id"], status="resolved")

    response = api_client.post(f"/admin/flags/{flag_id}/dismiss", {}, format="json", **headers)
    assert response.status_code == 409, response.content


def test_dismiss_returns_404_for_an_unknown_flag(api_client: APIClient) -> None:
    headers = _admin_headers(api_client)
    response = api_client.post(f"/admin/flags/{_SOME_ID}/dismiss", {}, format="json", **headers)
    assert response.status_code == 404, response.content
