"""Stage 13c: the moderation admin surface — `/admin/flags*`, gated by the
SAME `require_admin` dependency `app/api/routers/admin.py`'s own admin
user-management surface uses, and audited/rate-limited EXACTLY the same
way — this router deliberately REUSES `admin.py`'s `require_admin_
rate_limit` dependency (imported below), same posture `app/api/routers/
blog.py`'s own module docstring documents for its identical reuse: one
shared 30/min admin-surface bucket per client, not a third, independently-
tracked one for what is, from an attacker's perspective, the same
privileged surface.

**Admin-only queue, no end-user write path.** There is no `POST /flags`
endpoint anywhere in this router or this app — a consuming app writes
`Flag` rows itself (via the ORM); this router only ships the admin list/
get/resolve/dismiss surface over rows that already exist. Every mutation
below calls `audit_event(...)` with `actor=principal.sub`, a `type:id`
`resource` string, `outcome="success"`, and (for `resolve`) an `extra`
payload noting the action taken and the target acted on — ids only, never
PII — same posture `admin.py`/`blog.py`'s own module docstrings document.

**The `resolve` state machine.** Only an `open` flag can be resolved or
dismissed — `resolve_admin_flag`/`dismiss_admin_flag` both raise
`ConflictError` (409) for a flag that's already `resolved`/`dismissed`,
checked BEFORE any content/author side effect runs, so a flag that fails
that gate never mutates anything else either.

**The `resolve` action dispatch, per `Flag.target_type`:**

- `none` — marks the flag resolved, no content/author action at all.
- `hide_content` — `comment` -> `Comment.status="hidden"`; `blog_post` ->
  unpublish (`status="draft"`, `published_at=None`); `user` -> 422
  `validation_failed` (not a valid target for this action).
- `delete_content` — soft-deletes the target `blog_post`/`comment`
  (`AsyncRepository.delete()`, never a hard `DELETE` — same posture every
  other model in this catalog documents); `user` -> 422.
- `ban_author` — bans the AUTHOR: for a `blog_post`/`comment` target,
  resolves the author via that row's `author_id` (a `comment` with a NULL
  `author_id` has no author to ban -> 404); for a `user` target, the
  target IS the author. Calls `app/api/routers/admin.py`'s `ban_user(...)`
  directly — THE SAME ban implementation `POST /admin/users/{user_id}/ban`
  uses, reused (never duplicated) here, plus this router's OWN
  self-protection check (`admin.py`'s `_ensure_not_self`, imported the
  same way): an admin can never `ban_author` themselves, even indirectly
  via a flag whose target/author happens to be their own account (409).

Any `hide_content`/`delete_content`/`ban_author` whose target/author row
doesn't exist at all is a 404 — distinguishing "the flag exists but what
it points at is gone" from the flag-level 404 (`flag_id` itself unknown)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query

from app.api.deps import require_admin
from app.api.routers.admin import _ensure_not_self, ban_user, require_admin_rate_limit
from app.core.db import AsyncRepository, Page, PageParams, get_db
from app.core.errors import ConflictError, ErrorEnvelope, NotFoundError, ValidationFailedError
from app.core.security.audit_logging.audit import audit_event
from app.core.security.auth import SessionPrincipal
from app.core.security.auth.stores import utc_now
from app.models.blog_post import BlogPost
from app.models.comment import Comment
from app.models.flag import Flag
from app.models.user import User
from app.schemas.blog import BlogPostStatus, CommentStatus
from app.schemas.moderation import FlagDismissIn, FlagOut, FlagResolveIn, FlagStatus, FlagTargetType, ResolveAction
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(prefix="/admin/flags", tags=["moderation"])

_AUTH_RESPONSES = {
    401: {"model": ErrorEnvelope, "description": "Missing or invalid bearer token."},
    403: {"model": ErrorEnvelope, "description": "Authenticated, but the caller lacks the 'admin' role."},
}
_NOT_FOUND_RESPONSE = {404: {"model": ErrorEnvelope, "description": "Flag (or its target/author) not found."}}
_CONFLICT_RESPONSE = {
    409: {
        "model": ErrorEnvelope,
        "description": "The flag isn't 'open', or the action is a self-protection guard.",
    }
}
_VALIDATION_RESPONSE = {
    422: {"model": ErrorEnvelope, "description": "The request body failed validation, or the action doesn't apply to this target_type."}
}


def _to_flag_out(flag: Flag) -> FlagOut:
    return FlagOut.model_validate(flag)


async def _resolve_author(db: AsyncSession, flag: Flag) -> User:
    """`ban_author`'s target resolution — see this module's own docstring
    for the per-`target_type` dispatch. Raises `NotFoundError` (404) for a
    missing target row, a target row with no author at all (a `comment`
    whose `author_id` is NULL), or an author whose `User` row is itself
    missing/soft-deleted."""
    if flag.target_type == FlagTargetType.USER.value:
        author = await AsyncRepository(db, User).get(flag.target_id)
        if author is None:
            raise NotFoundError(f"User {flag.target_id} was not found.")
        return author
    if flag.target_type == FlagTargetType.BLOG_POST.value:
        post = await AsyncRepository(db, BlogPost).get(flag.target_id)
        if post is None:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.")
        author = await AsyncRepository(db, User).get(post.author_id)
        if author is None:
            raise NotFoundError(f"User {post.author_id} was not found.")
        return author
    if flag.target_type == FlagTargetType.COMMENT.value:
        comment = await AsyncRepository(db, Comment).get(flag.target_id)
        if comment is None:
            raise NotFoundError(f"Comment {flag.target_id} was not found.")
        if comment.author_id is None:
            raise NotFoundError(f"Comment {flag.target_id} has no author to ban.")
        author = await AsyncRepository(db, User).get(comment.author_id)
        if author is None:
            raise NotFoundError(f"User {comment.author_id} was not found.")
        return author
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


async def _hide_content(db: AsyncSession, flag: Flag) -> None:
    if flag.target_type == FlagTargetType.COMMENT.value:
        comment = await AsyncRepository(db, Comment).get(flag.target_id)
        if comment is None:
            raise NotFoundError(f"Comment {flag.target_id} was not found.")
        await AsyncRepository(db, Comment).update(comment, status=CommentStatus.HIDDEN.value)
        return
    if flag.target_type == FlagTargetType.BLOG_POST.value:
        post = await AsyncRepository(db, BlogPost).get(flag.target_id)
        if post is None:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.")
        await AsyncRepository(db, BlogPost).update(post, status=BlogPostStatus.DRAFT.value, published_at=None)
        return
    if flag.target_type == FlagTargetType.USER.value:
        raise ValidationFailedError("hide_content is not a valid action for a 'user' target.")
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


async def _delete_content(db: AsyncSession, flag: Flag) -> None:
    if flag.target_type == FlagTargetType.COMMENT.value:
        comment = await AsyncRepository(db, Comment).get(flag.target_id)
        if comment is None:
            raise NotFoundError(f"Comment {flag.target_id} was not found.")
        await AsyncRepository(db, Comment).delete(comment)
        return
    if flag.target_type == FlagTargetType.BLOG_POST.value:
        post = await AsyncRepository(db, BlogPost).get(flag.target_id)
        if post is None:
            raise NotFoundError(f"Blog post {flag.target_id} was not found.")
        await AsyncRepository(db, BlogPost).delete(post)
        return
    if flag.target_type == FlagTargetType.USER.value:
        raise ValidationFailedError("delete_content is not a valid action for a 'user' target; use ban_author instead.")
    raise ValidationFailedError(f"Unknown target_type '{flag.target_type}'.")


@router.get(
    "",
    response_model=Page[FlagOut],
    summary="List flags (admin)",
    operation_id="list_admin_flags_admin_flags_get",
    responses=_AUTH_RESPONSES,
)
async def list_admin_flags(
    params: PageParams = Depends(),
    status_filter: FlagStatus | None = Query(default=None, alias="status"),
    target_type_filter: FlagTargetType | None = Query(default=None, alias="target_type"),
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> Page[FlagOut]:
    """`?status=`/`?target_type=` each filter to one exact value, composable
    — the queue's primary "show me open reports" filter shape."""
    repo = AsyncRepository(db, Flag)
    filters = []
    if status_filter is not None:
        filters.append(Flag.status == status_filter.value)
    if target_type_filter is not None:
        filters.append(Flag.target_type == target_type_filter.value)
    result = await repo.list(params=params, filters=filters)
    mapped = [_to_flag_out(flag) for flag in result.items]
    return Page.create(mapped, total=result.total, params=params)


@router.get(
    "/{flag_id}",
    response_model=FlagOut,
    summary="Get flag (admin)",
    operation_id="get_admin_flag_admin_flags__flag_id__get",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE},
)
async def get_admin_flag(
    flag_id: uuid.UUID,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> FlagOut:
    repo = AsyncRepository(db, Flag)
    flag = await repo.get(flag_id)
    if flag is None:
        raise NotFoundError(f"Flag {flag_id} was not found.")
    return _to_flag_out(flag)


@router.post(
    "/{flag_id}/resolve",
    response_model=FlagOut,
    summary="Resolve flag (admin)",
    operation_id="resolve_admin_flag_admin_flags__flag_id__resolve_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE, **_VALIDATION_RESPONSE},
)
async def resolve_admin_flag(
    flag_id: uuid.UUID,
    payload: FlagResolveIn,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> FlagOut:
    """State machine: only an `open` flag can be resolved (409 otherwise).
    Dispatches on `payload.action` — see this module's own docstring for
    the full per-action, per-`target_type` behavior. The content/author
    side effect (if any) runs BEFORE the flag itself is marked resolved,
    so a 404/409/422 raised by that side effect leaves the flag untouched
    (still `open`), never resolved-with-a-failed-action."""
    repo = AsyncRepository(db, Flag)
    flag = await repo.get(flag_id)
    if flag is None:
        raise NotFoundError(f"Flag {flag_id} was not found.")
    if flag.status != FlagStatus.OPEN.value:
        raise ConflictError(f"Cannot resolve a flag with status '{flag.status}'.")

    audit_extra: dict[str, str] = {}
    if payload.action == ResolveAction.NONE:
        pass
    elif payload.action == ResolveAction.HIDE_CONTENT:
        await _hide_content(db, flag)
    elif payload.action == ResolveAction.DELETE_CONTENT:
        await _delete_content(db, flag)
    elif payload.action == ResolveAction.BAN_AUTHOR:
        author = await _resolve_author(db, flag)
        _ensure_not_self(principal, author.id, action="ban")
        await ban_user(db, author)
        audit_extra["banned_user"] = str(author.id)
    else:  # pragma: no cover - unreachable, ResolveAction is a closed StrEnum FastAPI already validates at 422
        raise ValidationFailedError(f"Unknown action '{payload.action}'.")

    flag = await repo.update(
        flag,
        status=FlagStatus.RESOLVED.value,
        resolved_by_id=uuid.UUID(principal.sub),
        resolved_at=utc_now(),
        resolution_note=payload.note,
    )
    audit_event(
        "admin.flag.resolve",
        actor=principal.sub,
        resource=f"flag:{flag.id}",
        outcome="success",
        action_taken=payload.action.value,
        target=f"{flag.target_type}:{flag.target_id}",
        **audit_extra,
    )
    return _to_flag_out(flag)


@router.post(
    "/{flag_id}/dismiss",
    response_model=FlagOut,
    summary="Dismiss flag (admin)",
    operation_id="dismiss_admin_flag_admin_flags__flag_id__dismiss_post",
    responses={**_AUTH_RESPONSES, **_NOT_FOUND_RESPONSE, **_CONFLICT_RESPONSE},
)
async def dismiss_admin_flag(
    flag_id: uuid.UUID,
    payload: FlagDismissIn,
    principal: SessionPrincipal = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
    _rate_limit: None = Depends(require_admin_rate_limit),
) -> FlagOut:
    """State machine: only an `open` flag can be dismissed (409 otherwise).
    Never performs a content/author action — `dismiss` is "this report
    doesn't warrant action," not a moderation outcome."""
    repo = AsyncRepository(db, Flag)
    flag = await repo.get(flag_id)
    if flag is None:
        raise NotFoundError(f"Flag {flag_id} was not found.")
    if flag.status != FlagStatus.OPEN.value:
        raise ConflictError(f"Cannot dismiss a flag with status '{flag.status}'.")
    flag = await repo.update(
        flag,
        status=FlagStatus.DISMISSED.value,
        resolved_by_id=uuid.UUID(principal.sub),
        resolved_at=utc_now(),
        resolution_note=payload.note,
    )
    audit_event(
        "admin.flag.dismiss",
        actor=principal.sub,
        resource=f"flag:{flag.id}",
        outcome="success",
    )
    return _to_flag_out(flag)
