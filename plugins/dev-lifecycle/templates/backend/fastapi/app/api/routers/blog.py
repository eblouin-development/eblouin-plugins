"""Stage 13d: the blog/CMS admin surface — `/admin/blog/posts*` and
`/admin/blog/comments*`, gated by the SAME `require_admin` dependency
`app/api/routers/admin.py`'s own admin user-management surface uses, and
audited/rate-limited EXACTLY the same way: every mutation calls
`audit_event(...)` (`app/core/security/audit_logging/audit.py`) with
`actor=principal.sub`, a `type:id` `resource` string, `outcome="success"`,
and `changed_fields=[...]` naming which column(s) changed (never raw
values) — see `admin.py`'s own module docstring for the full rationale,
identical here. This router deliberately REUSES `admin.py`'s
`require_admin_rate_limit` dependency (imported below) rather than
building a second, parallel `InMemoryBucketStore` — one shared 30/min
admin-surface bucket per client, not two independently-tracked ones for
what is, from an attacker's perspective, the same privileged surface.

**THIS IS THE STORED-XSS SECURITY CENTERPIECE of the whole kit.** Every
write path that can set `BlogPost.body_html` — `create_admin_blog_post`
and `update_admin_blog_post` below, nowhere else — calls
`app/services/sanitize.py:sanitize_blog_html()` on the caller-supplied
HTML BEFORE it ever reaches `AsyncRepository.create`/`.update`. A post can
never reach the database with unsanitized HTML through this router.
`body_json` is persisted verbatim (opaque, never sanitized, never
rendered — see `sanitize.py`'s own docstring for the render rule this
whole module respects).

Does NOT build: the TipTap editor UI (a later, Stage 13d UI stage), moderation/
Flag/Report (Stage 13c, separate), or a public comment-creation endpoint
(comments are created by end-users in a CONSUMING app — this stage only
ships the model plus the admin list/hide/delete surface, per the Stage 13
plan)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.routers.admin import require_admin_rate_limit
from app.core.db import AsyncRepository, Page, PageParams, get_db
from app.core.errors import ConflictError, ErrorEnvelope, NotFoundError
from app.core.security.audit_logging.audit import audit_event
from app.core.security.auth import SessionPrincipal
from app.core.security.auth.stores import utc_now
from app.models.blog_post import BlogPost
from app.models.comment import Comment
from app.schemas.blog import (
    BlogPostCreate,
    BlogPostOut,
    BlogPostStatus,
    BlogPostSummaryOut,
    BlogPostUpdate,
    CommentOut,
    CommentStatus,
    slugify,
)
from app.services.sanitize import sanitize_blog_html

router = APIRouter(prefix="/admin/blog", tags=["blog"])

_AUTH_RESPONSES = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid bearer token."},
    403: {"model": ErrorEnvelope, "description": "Authenticated, but the caller lacks the 'admin' role."},
}
_POST_NOT_FOUND_RESPONSE = {404: {"model": ErrorEnvelope, "description": "Blog post not found."}}
_COMMENT_NOT_FOUND_RESPONSE = {404: {"model": ErrorEnvelope, "description": "Comment not found."}}
_CONFLICT_RESPONSE = {
    409: {
        "model": ErrorEnvelope,
        "description": "The action conflicts with the resource's current state, or its slug is already taken.",
    }
}
_VALIDATION_RESPONSE = {422: {"model": ErrorEnvelope, "description": "The request body failed validation."}}


def _to_blog_post_summary_out(post: BlogPost) -> BlogPostSummaryOut:
    return BlogPostSummaryOut.model_validate(post)


def _to_blog_post_out(post: BlogPost) -> BlogPostOut:
    return BlogPostOut.model_validate(post)


def _to_comment_out(comment: Comment) -> CommentOut:
    return CommentOut.model_validate(comment)


async def _slug_taken(db: AsyncSession, slug: str, *, exclude_id: uuid.UUID | None = None) -> bool:
    """`True` iff a NOT-soft-deleted `BlogPost` other than `exclude_id`
    already owns `slug` — the read-then-write uniqueness check backing
    both `create_admin_blog_post`'s explicit-slug 409 and
    `update_admin_blog_post`'s own. Not itself atomic against a
    concurrent duplicate write (the same accepted, bounded race
    `alembic/versions/0002_create_auth_tables.py`'s own docstring
    documents for `users.email`) — `blog_posts.slug`'s UNIQUE index
    (migration 0005) is the DB-level enforcement of last resort behind
    this friendly-error-path check."""
    stmt = select(BlogPost.id).where(BlogPost.not_deleted(), BlogPost.slug == slug)
    if exclude_id is not None:
        stmt = stmt.where(BlogPost.id != exclude_id)
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


async def _unique_slug(db: AsyncSession, base_slug: str) -> str:
    """Disambiguates a DERIVED (never an explicitly-caller-supplied) slug
    by appending `-2`, `-3`, ... until free — the plan's own "append/
    increment... on collision" rule for the omitted-slug case (an
    EXPLICIT collision is a 409 instead, handled at the call site, never
    here)."""
    candidate = base_slug
    suffix = 2
    while await _slug_taken(db, candidate):
        candidate = f"{base_slug}-{suffix}"
        suffix += 1
    return candidate


@router.get(
    "/posts",
    response_model=Page[BlogPostSummaryOut],
    summary="List blog posts (admin)",
    operation_id="list_admin_blog_posts_admin_blog_posts_get",
    responses=_AUTH_RESPONSES,
)
async def list_admin_blog_posts(
    params: PageParams = Depends(),
    status_filter: BlogPostStatus | None = Query(default=None, alias="status"),
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> Page[BlogPostSummaryOut]:
    """`?status=` filters to one exact `BlogPostStatus` (an unrecognized
    value 422s automatically, same as `app/api/routers/admin.py`'s own
    `?status=` on `GET /admin/users`). Deliberately the SUMMARY shape —
    no `body_json`/`body_html` on a list response, see `app/schemas/
    blog.py`'s own module docstring."""
    repo = AsyncRepository(db, BlogPost)
    filters = []
    if status_filter is not None:
        filters.append(BlogPost.status == status_filter.value)
    result = await repo.list(params=params, filters=filters)
    mapped = [_to_blog_post_summary_out(post) for post in result.items]
    return Page.create(mapped, total=result.total, params=params)


@router.post(
    "/posts",
    response_model=BlogPostOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create blog post (admin)",
    operation_id="create_admin_blog_post_admin_blog_posts_post",
    responses={**_AUTH_RESPONSES, **_CONFLICT_RESPONSE, **_VALIDATION_RESPONSE},
)
async def create_admin_blog_post(
    payload: BlogPostCreate,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> BlogPostOut:
    """**THE stored-XSS write-path boundary, half 1 of 2** (the other is
    `update_admin_blog_post`, below). `payload.body_html` is caller-
    supplied, UNTRUSTED HTML — `sanitize_blog_html()` runs on it BEFORE
    `AsyncRepository.create` ever persists a row; the value written to
    `BlogPost.body_html` is always the SANITIZED result, never
    `payload.body_html` itself.

    Slug resolution: an explicit `payload.slug` that's already taken is a
    409 `conflict` (the caller chose it, so a silent auto-rename would
    surprise them); an OMITTED slug is derived from `payload.title`
    (`slugify`) and auto-disambiguated (`_unique_slug`) — never a 409 for
    that case, see this module's own `_unique_slug` docstring.

    The creating admin becomes the post's `author_id` — this stage has no
    separate "assign an author" concept; `principal.sub` (the access token's
    own `sub` claim) is already a user id string, parsed straight to
    `uuid.UUID`."""
    if payload.slug is not None:
        if await _slug_taken(db, payload.slug):
            raise ConflictError(f"Slug '{payload.slug}' is already in use.")
        slug = payload.slug
    else:
        slug = await _unique_slug(db, slugify(payload.title))

    sanitized_html = sanitize_blog_html(payload.body_html)

    repo = AsyncRepository(db, BlogPost)
    post = await repo.create(
        slug=slug,
        title=payload.title,
        body_json=payload.body_json,
        body_html=sanitized_html,
        author_id=uuid.UUID(principal.sub),
    )
    audit_event(
        "admin.blog.create",
        actor=principal.sub,
        resource=f"blog_post:{post.id}",
        outcome="success",
    )
    return _to_blog_post_out(post)


@router.get(
    "/posts/{post_id}",
    response_model=BlogPostOut,
    summary="Get blog post (admin)",
    operation_id="get_admin_blog_post_admin_blog_posts__post_id__get",
    responses={**_AUTH_RESPONSES, **_POST_NOT_FOUND_RESPONSE},
)
async def get_admin_blog_post(
    post_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> BlogPostOut:
    """Includes `body_json` (opaque, for the TipTap editor to reload —
    NEVER rendered) and `body_html` (already sanitized at write time, see
    this module's own docstring) — the single-post shape, admin-gated
    like every route in this router."""
    repo = AsyncRepository(db, BlogPost)
    post = await repo.get(post_id)
    if post is None:
        raise NotFoundError(f"Blog post {post_id} was not found.")
    return _to_blog_post_out(post)


@router.patch(
    "/posts/{post_id}",
    response_model=BlogPostOut,
    summary="Update blog post (admin)",
    operation_id="update_admin_blog_post_admin_blog_posts__post_id__patch",
    responses={**_AUTH_RESPONSES, **_POST_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE, **_VALIDATION_RESPONSE},
)
async def update_admin_blog_post(
    post_id: uuid.UUID,
    payload: BlogPostUpdate,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> BlogPostOut:
    """**THE stored-XSS write-path boundary, half 2 of 2.** Only
    explicitly-set fields are applied (`model_dump(exclude_unset=True)`,
    the `ItemUpdate`/`app/api/routers/items.py` convention) — an omitted
    `body_html` leaves the already-sanitized column untouched; a SUPPLIED
    `body_html` is RE-sanitized here (`sanitize_blog_html`) before the
    update, same as create — this is what "re-sanitizes" means in this
    stage's own test suite (`tests/test_blog.py`).

    An explicitly-set `slug` that collides with a DIFFERENT post
    (`exclude_id=post_id`, so a post PATCHing its own unchanged slug back
    never self-conflicts) is a 409, same posture as create's explicit-slug
    case. `BlogPostUpdate`'s own `_reject_explicit_null` validator
    (`app/schemas/blog.py`) already rejects an explicit `null` for any of
    these four NOT-NULL columns at the schema layer — this handler never
    has to guess whether a value made it into `model_dump(exclude_unset=
    True)` because it was omitted or because it was nulled; only the
    "omitted" case is possible by the time this body runs."""
    repo = AsyncRepository(db, BlogPost)
    post = await repo.get(post_id)
    if post is None:
        raise NotFoundError(f"Blog post {post_id} was not found.")

    updates = payload.model_dump(exclude_unset=True)
    if "slug" in updates and await _slug_taken(db, updates["slug"], exclude_id=post_id):
        raise ConflictError(f"Slug '{updates['slug']}' is already in use.")
    if "body_html" in updates:
        updates["body_html"] = sanitize_blog_html(updates["body_html"])

    post = await repo.update(post, **updates)
    audit_event(
        "admin.blog.update",
        actor=principal.sub,
        resource=f"blog_post:{post.id}",
        outcome="success",
        changed_fields=sorted(updates.keys()),
    )
    return _to_blog_post_out(post)


@router.post(
    "/posts/{post_id}/publish",
    response_model=BlogPostOut,
    summary="Publish blog post (admin)",
    operation_id="publish_admin_blog_post_admin_blog_posts__post_id__publish_post",
    responses={**_AUTH_RESPONSES, **_POST_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def publish_admin_blog_post(
    post_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> BlogPostOut:
    """Valid only from `status == "draft"` — an already-`published` post
    raises `ConflictError` (409, matching `app/api/routers/admin.py`'s own
    strict-transition posture for `suspend`/`ban`/`reinstate`: idempotent
    re-publish is rejected, not silently no-op'd). Sets `status=
    "published"` and stamps `published_at=<now>`."""
    repo = AsyncRepository(db, BlogPost)
    post = await repo.get(post_id)
    if post is None:
        raise NotFoundError(f"Blog post {post_id} was not found.")
    if post.status != BlogPostStatus.DRAFT.value:
        raise ConflictError(f"Cannot publish a post with status '{post.status}'.")
    post = await repo.update(post, status=BlogPostStatus.PUBLISHED.value, published_at=utc_now())
    audit_event(
        "admin.blog.publish",
        actor=principal.sub,
        resource=f"blog_post:{post.id}",
        outcome="success",
        changed_fields=["status", "published_at"],
    )
    return _to_blog_post_out(post)


@router.post(
    "/posts/{post_id}/unpublish",
    response_model=BlogPostOut,
    summary="Unpublish blog post (admin)",
    operation_id="unpublish_admin_blog_post_admin_blog_posts__post_id__unpublish_post",
    responses={**_AUTH_RESPONSES, **_POST_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def unpublish_admin_blog_post(
    post_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> BlogPostOut:
    """Valid only from `status == "published"` — an already-`draft` post
    raises `ConflictError` (409). Reverts fully to draft: `status=
    "draft"` AND `published_at=None` — a re-publish later stamps a FRESH
    `published_at`, rather than this endpoint leaving a stale historical
    timestamp on a post that's no longer live."""
    repo = AsyncRepository(db, BlogPost)
    post = await repo.get(post_id)
    if post is None:
        raise NotFoundError(f"Blog post {post_id} was not found.")
    if post.status != BlogPostStatus.PUBLISHED.value:
        raise ConflictError(f"Cannot unpublish a post with status '{post.status}'.")
    post = await repo.update(post, status=BlogPostStatus.DRAFT.value, published_at=None)
    audit_event(
        "admin.blog.unpublish",
        actor=principal.sub,
        resource=f"blog_post:{post.id}",
        outcome="success",
        changed_fields=["status", "published_at"],
    )
    return _to_blog_post_out(post)


@router.delete(
    "/posts/{post_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete blog post (admin)",
    operation_id="delete_admin_blog_post_admin_blog_posts__post_id__delete",
    responses={**_AUTH_RESPONSES, **_POST_NOT_FOUND_RESPONSE},
)
async def delete_admin_blog_post(
    post_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> None:
    """Soft-deletes via `AsyncRepository.delete()` (`post.mark_deleted()`
    — `BlogPost` composes `SoftDeleteMixin`, same as every other model in
    this catalog), never a hard `DELETE`."""
    repo = AsyncRepository(db, BlogPost)
    post = await repo.get(post_id)
    if post is None:
        raise NotFoundError(f"Blog post {post_id} was not found.")
    await repo.delete(post)
    audit_event(
        "admin.blog.delete",
        actor=principal.sub,
        resource=f"blog_post:{post_id}",
        outcome="success",
    )


# ---------------------------------------------------------------------------
# Comments — admin list/hide/delete only (no public create in this stage)
# ---------------------------------------------------------------------------


@router.get(
    "/comments",
    response_model=Page[CommentOut],
    summary="List blog comments (admin)",
    operation_id="list_admin_blog_comments_admin_blog_comments_get",
    responses=_AUTH_RESPONSES,
)
async def list_admin_blog_comments(
    params: PageParams = Depends(),
    status_filter: CommentStatus | None = Query(default=None, alias="status"),
    post_id: uuid.UUID | None = Query(default=None),
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> Page[CommentOut]:
    """`?status=` filters to one exact `CommentStatus`; `?post_id=`
    filters to one post's comments — both optional, composable."""
    repo = AsyncRepository(db, Comment)
    filters = []
    if status_filter is not None:
        filters.append(Comment.status == status_filter.value)
    if post_id is not None:
        filters.append(Comment.post_id == post_id)
    result = await repo.list(params=params, filters=filters)
    mapped = [_to_comment_out(comment) for comment in result.items]
    return Page.create(mapped, total=result.total, params=params)


@router.post(
    "/comments/{comment_id}/hide",
    response_model=CommentOut,
    summary="Hide blog comment (admin)",
    operation_id="hide_admin_blog_comment_admin_blog_comments__comment_id__hide_post",
    responses={**_AUTH_RESPONSES, **_COMMENT_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def hide_admin_blog_comment(
    comment_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> CommentOut:
    """Valid from `status in {"visible", "pending"}` — an already-`hidden`
    comment raises `ConflictError` (409, idempotent re-hide rejected, same
    strict-transition posture as the post publish/unpublish pair above).
    This is a lightweight moderation-ADJACENT action, NOT the Stage 13c
    Flag/Report surface (out of scope here) — it only flips `status`."""
    repo = AsyncRepository(db, Comment)
    comment = await repo.get(comment_id)
    if comment is None:
        raise NotFoundError(f"Comment {comment_id} was not found.")
    if comment.status == CommentStatus.HIDDEN.value:
        raise ConflictError(f"Cannot hide a comment with status '{comment.status}'.")
    comment = await repo.update(comment, status=CommentStatus.HIDDEN.value)
    audit_event(
        "admin.comment.hide",
        actor=principal.sub,
        resource=f"blog_comment:{comment.id}",
        outcome="success",
        changed_fields=["status"],
    )
    return _to_comment_out(comment)


@router.delete(
    "/comments/{comment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete blog comment (admin)",
    operation_id="delete_admin_blog_comment_admin_blog_comments__comment_id__delete",
    responses={**_AUTH_RESPONSES, **_COMMENT_NOT_FOUND_RESPONSE},
)
async def delete_admin_blog_comment(
    comment_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> None:
    """Soft-deletes via `AsyncRepository.delete()` — `Comment` composes
    `SoftDeleteMixin` too, never a hard `DELETE`."""
    repo = AsyncRepository(db, Comment)
    comment = await repo.get(comment_id)
    if comment is None:
        raise NotFoundError(f"Comment {comment_id} was not found.")
    await repo.delete(comment)
    audit_event(
        "admin.comment.delete",
        actor=principal.sub,
        resource=f"blog_comment:{comment_id}",
        outcome="success",
    )
