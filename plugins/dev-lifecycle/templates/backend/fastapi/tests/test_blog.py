"""Stage 13d — the blog/CMS admin surface, over real HTTP against the
hermetic client. Reuses `tests/test_auth.py`'s own fixtures/helpers
(`auth_client`, `email_sender`, `_register_and_verify`,
`_CapturingEmailSender`, `_make_auth_client`) and `tests/test_admin_users.
py`'s `_seed_verified_admin`/`_admin_headers` shape — same posture, see
that module's own docstring.

**The stored-XSS proof (`test_create_stores_only_sanitized_html_stripping_every_xss_payload`,
`test_update_re_sanitizes_body_html`) is THE load-bearing test in this
module** — it is the acceptance proof for the whole stage's #1 risk. A
divergence between this file's assertions and `tests/test_blog.py` on the
Django track (`backend/django`) is a parity BUG, not a style choice."""

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
    `require_admin_rate_limit` dependency (see `app/api/routers/blog.py`'s
    own module docstring), so it shares that SAME module-level bucket."""
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


# ---------------------------------------------------------------------------
# 401 / 403 across every /admin/blog/* endpoint
# ---------------------------------------------------------------------------

_ENDPOINTS: list[tuple[str, str, dict | None]] = [
    ("get", "/admin/blog/posts", None),
    ("post", "/admin/blog/posts", {"title": "x", "body_json": _SIMPLE_DOC, "body_html": "<p>x</p>"}),
    ("get", f"/admin/blog/posts/{_SOME_ID}", None),
    ("patch", f"/admin/blog/posts/{_SOME_ID}", {"title": "y"}),
    ("post", f"/admin/blog/posts/{_SOME_ID}/publish", None),
    ("post", f"/admin/blog/posts/{_SOME_ID}/unpublish", None),
    ("delete", f"/admin/blog/posts/{_SOME_ID}", None),
    ("get", "/admin/blog/comments", None),
    ("post", f"/admin/blog/comments/{_SOME_ID}/hide", None),
    ("delete", f"/admin/blog/comments/{_SOME_ID}", None),
]


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_blog_endpoint_returns_401_for_anonymous(
    auth_client: TestClient, method: str, path: str, body: dict | None
) -> None:
    response = auth_client.request(method.upper(), path, json=body)
    assert response.status_code == 401, response.text
    assert response.json()["error"]["code"] == "unauthenticated"


@pytest.mark.parametrize("method,path,body", _ENDPOINTS)
def test_every_blog_endpoint_returns_403_for_a_non_admin(
    auth_client: TestClient, email_sender: _CapturingEmailSender, method: str, path: str, body: dict | None
) -> None:
    _register_and_verify(auth_client, email_sender)
    tokens = _login(auth_client, _EMAIL, _PASSWORD)
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    response = auth_client.request(method.upper(), path, json=body, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "permission_denied"


# ---------------------------------------------------------------------------
# THE stored-XSS proof
# ---------------------------------------------------------------------------


def test_create_stores_only_sanitized_html_stripping_every_xss_payload(auth_client: TestClient) -> None:
    """Creates a post whose `body_html` contains every payload the Stage
    13d plan enumerates, then asserts the STORED `body_html` (read back via
    `GET /admin/blog/posts/{id}`) contains NONE of them, while the allowed
    markup (`<p>`, `<strong>`, and an `<a href="https://...">` with the
    FORCED `rel`) survives intact."""
    headers = _admin_headers(auth_client)
    raw_html = (
        '<p>Intro paragraph with <strong>bold</strong> text.</p>'
        '<a href="javascript:alert(1)">click me</a>'
        '<a href="data:text/html,alert(1)">data link</a>'
        '<img src=x onerror=alert(1)>'
        "<script>alert(1)</script>"
        '<iframe src="javascript:alert(1)"></iframe>'
        '<p style="expression(alert(1))">styled</p>'
        "<svg onload=alert(1)></svg>"
        '<p onclick="alert(1)">click handler</p>'
        '<object data="evil.swf"></object>'
        '<a href="https://example.com" title="Example">safe link</a>'
    )
    created = _create_post(auth_client, headers, title="XSS Test Post", body_html=raw_html)

    response = auth_client.get(f"/admin/blog/posts/{created['id']}", headers=headers)
    assert response.status_code == 200, response.text
    stored_html = response.json()["body_html"]

    # Every dangerous construct must be GONE.
    assert "javascript:" not in stored_html
    assert "data:text/html" not in stored_html
    assert "onerror" not in stored_html
    assert "onclick" not in stored_html
    assert "onload" not in stored_html
    assert "<script" not in stored_html
    assert "</script>" not in stored_html
    assert "alert(1)" not in stored_html.replace("XSS Test Post", "")  # no leaked payload text either
    assert "<iframe" not in stored_html
    assert "<svg" not in stored_html
    assert "<img" not in stored_html
    assert "style=" not in stored_html
    assert "<object" not in stored_html

    # Allowed markup survives, with the forced rel.
    assert "<p>Intro paragraph with <strong>bold</strong> text.</p>" in stored_html
    assert '<a href="https://example.com" title="Example" rel="noopener noreferrer nofollow">safe link</a>' in (
        stored_html
    )


def test_update_re_sanitizes_body_html(auth_client: TestClient) -> None:
    """`PATCH` re-runs the sanitizer on a supplied `body_html` — a post
    created clean can still be attacked via a later update; this proves
    the update path is not a bypass."""
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}",
        json={"body_html": '<p>updated</p><script>alert(1)</script><a href="javascript:alert(2)">x</a>'},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    stored_html = response.json()["body_html"]
    assert "<script" not in stored_html
    assert "javascript:" not in stored_html
    assert "<p>updated</p>" in stored_html


# ---------------------------------------------------------------------------
# Create — happy paths, slug handling, validation
# ---------------------------------------------------------------------------


def test_create_without_slug_derives_one_from_title(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers, title="Hello, World! Édition")
    assert created["slug"] == "hello-world-dition" or created["slug"].startswith("hello-world")
    assert created["status"] == "draft"
    assert created["published_at"] is None
    assert created["body_json"] == _SIMPLE_DOC


def test_create_with_explicit_slug(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers, slug="my-custom-slug")
    assert created["slug"] == "my-custom-slug"


def test_create_derived_slug_auto_disambiguates_on_collision(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    first = _create_post(auth_client, headers, title="Same Title")
    second = _create_post(auth_client, headers, title="Same Title")
    assert first["slug"] != second["slug"]
    assert second["slug"].startswith(first["slug"])


def test_create_with_explicit_duplicate_slug_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    _create_post(auth_client, headers, slug="taken-slug")
    response = auth_client.post(
        "/admin/blog/posts",
        json={"title": "Another", "slug": "taken-slug", "body_json": _SIMPLE_DOC, "body_html": "<p>x</p>"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_create_with_two_active_duplicate_slugs_is_still_a_conflict(auth_client: TestClient) -> None:
    """Companion to `test_create_with_explicit_duplicate_slug_is_a_conflict`
    above, spelled out explicitly alongside the soft-deleted-slug-reuse test
    below so the two "same slug, different post state" outcomes sit next to
    each other: TWO ACTIVE posts can never share a slug (409), but ONE
    active post reusing a SOFT-DELETED post's slug succeeds cleanly (201,
    see `test_create_reusing_a_soft_deleted_posts_slug_succeeds` below) --
    the partial-unique-index fix's whole point."""
    headers = _admin_headers(auth_client)
    _create_post(auth_client, headers, slug="dup-slug")
    response = auth_client.post(
        "/admin/blog/posts",
        json={"title": "Second", "slug": "dup-slug", "body_json": _SIMPLE_DOC, "body_html": "<p>x</p>"},
        headers=headers,
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_create_reusing_a_soft_deleted_posts_slug_succeeds(auth_client: TestClient) -> None:
    """THE partial-unique-index proof: create `foo`, soft-delete it, then
    create ANOTHER `foo` -- `_slug_taken`'s `not_deleted()`-scoped friendly
    check already considered the slug free (that's the intended soft-delete
    semantics); before the DB-level `uq_blog_posts_slug_active` partial
    unique index (`app/models/blog_post.py`), the OLD full-table unique
    index disagreed and this INSERT raised an unenveloped 500
    (`IntegrityError`). Now the DB constraint matches `not_deleted()`
    exactly, so this is a clean 201, not a 500 -- and NOT a 409 either
    (the caller's chosen slug genuinely is available again)."""
    headers = _admin_headers(auth_client)
    first = _create_post(auth_client, headers, slug="reusable-slug")

    delete_response = auth_client.delete(f"/admin/blog/posts/{first['id']}", headers=headers)
    assert delete_response.status_code == 204, delete_response.text

    second = _create_post(auth_client, headers, slug="reusable-slug")
    assert second["slug"] == "reusable-slug"
    assert second["id"] != first["id"]


def test_create_with_invalid_slug_shape_is_a_422(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.post(
        "/admin/blog/posts",
        json={
            "title": "Bad Slug",
            "slug": "Not A Valid Slug!",
            "body_json": _SIMPLE_DOC,
            "body_html": "<p>x</p>",
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_create_with_body_html_over_the_size_cap_is_a_422(auth_client: TestClient) -> None:
    """`app/schemas/blog.py`'s `_BODY_HTML_MAX_CHARS` (1,000,000) defense-
    in-depth cap -- a `body_html` one character over it is rejected at the
    schema layer (422 `validation_failed`), never reaches the sanitizer or
    the database."""
    headers = _admin_headers(auth_client)
    oversized_html = "<p>" + ("x" * 1_000_000) + "</p>"
    response = auth_client.post(
        "/admin/blog/posts",
        json={"title": "Too Big", "body_json": _SIMPLE_DOC, "body_html": oversized_html},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_create_with_body_json_over_the_size_cap_is_a_422(auth_client: TestClient) -> None:
    """`app/schemas/blog.py`'s `_BODY_JSON_MAX_SERIALIZED_CHARS` (1,000,000)
    defense-in-depth cap, enforced on the SERIALIZED (`json.dumps`) size of
    `body_json` via `_check_body_json_size`."""
    headers = _admin_headers(auth_client)
    oversized_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x" * 1_100_000}]}],
    }
    response = auth_client.post(
        "/admin/blog/posts",
        json={"title": "Too Big", "body_json": oversized_doc, "body_html": "<p>x</p>"},
        headers=headers,
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_create_with_body_html_at_normal_size_still_works(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    normal_html = "<p>" + ("hello world " * 100) + "</p>"
    created = _create_post(auth_client, headers, title="Normal Size", body_html=normal_html)
    assert created["body_html"].startswith("<p>hello world")


def test_create_and_audit(auth_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(auth_client)
    with caplog.at_level(logging.INFO, logger="audit"):
        created = _create_post(auth_client, headers)
    assert any('"action": "admin.blog.create"' in r.message for r in caplog.records)
    assert any(f'"resource": "blog_post:{created["id"]}"' in r.message for r in caplog.records)
    assert any('"outcome": "success"' in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Get / list
# ---------------------------------------------------------------------------


def test_get_post_returns_404_for_an_unknown_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.get(f"/admin/blog/posts/{_SOME_ID}", headers=headers)
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_list_posts_summary_shape_omits_bodies_and_paginates(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    _create_post(auth_client, headers, title="Post One")
    _create_post(auth_client, headers, title="Post Two")

    response = auth_client.get("/admin/blog/posts", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"items", "total", "page", "size", "pages"} <= body.keys()
    assert body["total"] >= 2
    for item in body["items"]:
        assert "body_html" not in item
        assert "body_json" not in item


def test_list_posts_filters_by_status(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers, title="Published Post")
    auth_client.post(f"/admin/blog/posts/{created['id']}/publish", headers=headers)
    _create_post(auth_client, headers, title="Still Draft")

    response = auth_client.get("/admin/blog/posts", params={"status": "published"}, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    titles = {item["title"] for item in body["items"]}
    assert "Published Post" in titles
    assert "Still Draft" not in titles


# ---------------------------------------------------------------------------
# Update
# ---------------------------------------------------------------------------


def test_update_happy_path_partial(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}", json={"title": "New Title"}, headers=headers
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["title"] == "New Title"
    assert body["slug"] == created["slug"]  # untouched


def test_update_to_a_taken_slug_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    _create_post(auth_client, headers, slug="slug-a")
    other = _create_post(auth_client, headers, slug="slug-b")

    response = auth_client.patch(
        f"/admin/blog/posts/{other['id']}", json={"slug": "slug-a"}, headers=headers
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_update_to_the_same_slug_is_not_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers, slug="unchanged-slug")

    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}", json={"slug": "unchanged-slug"}, headers=headers
    )
    assert response.status_code == 200, response.text


def test_update_with_explicit_null_title_is_a_422(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}", json={"title": None}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_update_returns_404_for_an_unknown_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.patch(f"/admin/blog/posts/{_SOME_ID}", json={"title": "x"}, headers=headers)
    assert response.status_code == 404, response.text


def test_update_with_body_html_over_the_size_cap_is_a_422(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    oversized_html = "<p>" + ("x" * 1_000_000) + "</p>"
    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}", json={"body_html": oversized_html}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


def test_update_with_body_json_over_the_size_cap_is_a_422(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    oversized_doc = {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "x" * 1_100_000}]}],
    }
    response = auth_client.patch(
        f"/admin/blog/posts/{created['id']}", json={"body_json": oversized_doc}, headers=headers
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_failed"


# ---------------------------------------------------------------------------
# Publish / unpublish
# ---------------------------------------------------------------------------


def test_publish_then_unpublish_happy_path_and_audit(
    auth_client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/blog/posts/{created['id']}/publish", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "published"
    assert body["published_at"] is not None
    assert any('"action": "admin.blog.publish"' in r.message for r in caplog.records)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/blog/posts/{created['id']}/unpublish", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "draft"
    assert body["published_at"] is None
    assert any('"action": "admin.blog.unpublish"' in r.message for r in caplog.records)


def test_publish_an_already_published_post_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)
    first = auth_client.post(f"/admin/blog/posts/{created['id']}/publish", headers=headers)
    assert first.status_code == 200, first.text

    response = auth_client.post(f"/admin/blog/posts/{created['id']}/publish", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_unpublish_a_draft_post_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    response = auth_client.post(f"/admin/blog/posts/{created['id']}/unpublish", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


def test_delete_post_soft_deletes_and_audits(auth_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(auth_client)
    created = _create_post(auth_client, headers)

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.delete(f"/admin/blog/posts/{created['id']}", headers=headers)
    assert response.status_code == 204, response.text
    assert response.content == b""
    assert any('"action": "admin.blog.delete"' in r.message for r in caplog.records)

    follow_up = auth_client.get(f"/admin/blog/posts/{created['id']}", headers=headers)
    assert follow_up.status_code == 404, follow_up.text


# ---------------------------------------------------------------------------
# Comments — list / hide / delete
# ---------------------------------------------------------------------------


async def _seed_comment(post_id: str, *, status: str = "visible", body: str = "Nice post!") -> str:
    from app.models.comment import Comment

    session_factory = get_sessionmaker()
    async with session_factory() as session:
        comment = Comment(post_id=uuid.UUID(post_id), author_id=None, body=body, status=status)
        session.add(comment)
        await session.commit()
        await session.refresh(comment)
        return str(comment.id)


def test_list_comments_filters_by_post_id_and_status(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    other_post = _create_post(auth_client, headers, title="Other")
    comment_id = asyncio.run(_seed_comment(post["id"], body="On post one"))
    asyncio.run(_seed_comment(other_post["id"], body="On post two"))
    hidden_id = asyncio.run(_seed_comment(post["id"], status="hidden", body="hidden comment"))

    response = auth_client.get("/admin/blog/comments", params={"post_id": post["id"]}, headers=headers)
    assert response.status_code == 200, response.text
    bodies = {item["body"] for item in response.json()["items"]}
    assert "On post one" in bodies
    assert "On post two" not in bodies
    assert "hidden comment" in bodies  # unfiltered by status

    response = auth_client.get(
        "/admin/blog/comments", params={"post_id": post["id"], "status": "hidden"}, headers=headers
    )
    assert response.status_code == 200, response.text
    ids = {item["id"] for item in response.json()["items"]}
    assert ids == {hidden_id}
    assert comment_id not in ids


def test_hide_comment_happy_path_and_audit(auth_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = asyncio.run(_seed_comment(post["id"]))

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.post(f"/admin/blog/comments/{comment_id}/hide", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "hidden"
    assert any('"action": "admin.comment.hide"' in r.message for r in caplog.records)


def test_hide_an_already_hidden_comment_is_a_conflict(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = asyncio.run(_seed_comment(post["id"], status="hidden"))

    response = auth_client.post(f"/admin/blog/comments/{comment_id}/hide", headers=headers)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_hide_comment_returns_404_for_an_unknown_id(auth_client: TestClient) -> None:
    headers = _admin_headers(auth_client)
    response = auth_client.post(f"/admin/blog/comments/{_SOME_ID}/hide", headers=headers)
    assert response.status_code == 404, response.text


def test_delete_comment_soft_deletes_and_audits(auth_client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    headers = _admin_headers(auth_client)
    post = _create_post(auth_client, headers)
    comment_id = asyncio.run(_seed_comment(post["id"]))

    with caplog.at_level(logging.INFO, logger="audit"):
        response = auth_client.delete(f"/admin/blog/comments/{comment_id}", headers=headers)
    assert response.status_code == 204, response.text
    assert any('"action": "admin.comment.delete"' in r.message for r in caplog.records)

    follow_up = auth_client.get("/admin/blog/comments", params={"post_id": post["id"]}, headers=headers)
    ids = {item["id"] for item in follow_up.json()["items"]}
    assert comment_id not in ids
