"""Request/response serializers for this block's DRF contract-emission
layer (Stage 4 Step 2, #27) — the DRF counterpart to backend/fastapi's
`app/schemas/{item,auth,health}.py`. Field names/types/required-ness are
kept in exact step with `packages/api-client/openapi.json`'s `ItemOut`/
`ItemCreate`/`ItemUpdate`/... schemas (see this block's README,
"Conformance") — `core/views.py` is what actually renders these over HTTP,
this module only owns the shape."""

from __future__ import annotations

import json
import re

from rest_framework import serializers

from core.contract.errors import ErrorCode
from core.models import Item


class ItemOutSerializer(serializers.ModelSerializer):
    """The read shape — matches `openapi.json`'s `ItemOut` exactly: `id`,
    `name`, `description`, `created_at`, `updated_at`. Deliberately NOT
    `deleted_at`, even though the model carries it for soft-delete
    bookkeeping (core/models.py) — every field here is output-only
    (`read_only_fields = fields`); this serializer is never constructed
    with `data=...`, only `ItemOutSerializer(instance)`.

    `name`/`description` explicit `max_length` (Stage 4 Step 4, #27,
    schema-conformance fix round): a PLAIN `ModelSerializer`-derived
    read-only field carries no `MaxLengthValidator` (DRF doesn't attach
    validators to fields it will never validate incoming data against),
    so drf-spectacular's generated schema silently dropped the
    `maxLength` constraint `openapi.json`'s own `ItemOut` documents on
    these same two fields (pydantic's `ItemOut` reuses `ItemBase`'s
    `Field(max_length=...)` regardless of read/write direction) —
    documentation-only, zero effect on runtime behavior (still never
    validated, since both fields stay `read_only=True`); this exists
    purely to close that generated-schema gap,
    `tests/test_schema_conformance.py`'s wire-surface proof."""

    name = serializers.CharField(max_length=200, min_length=1, read_only=True)
    description = serializers.CharField(max_length=2000, allow_null=True, read_only=True)

    class Meta:
        model = Item
        fields = ["id", "name", "description", "created_at", "updated_at"]
        read_only_fields = fields


class ItemCreateSerializer(serializers.ModelSerializer):
    """The create-body shape — matches `openapi.json`'s `ItemCreate`:
    `name` required, explicit `min_length=1`/`allow_blank=False` (matches
    the frozen contract's `minLength: 1` — `allow_blank=False` alone would
    give the same practical rejection for `name=""`, but the explicit
    `min_length=1` documents the wire constraint directly rather than
    leaning on that DRF default). `description` optional AND nullable —
    `default=None` (not just `required=False`) so a client that omits it
    entirely still lands `description=None` in `validated_data`, matching
    `ItemBase.description: str | None = Field(default=None, ...)`'s own
    "omitted means None" default on the FastAPI side (`core/views.py`'s
    `ItemViewSet.create` passes `validated_data` straight to
    `Item.objects.create(**...)`; without this default, Django's own
    `CharField.get_default()` would silently store `""` instead of `NULL`
    for an omitted field)."""

    name = serializers.CharField(min_length=1, max_length=200, allow_blank=False)
    description = serializers.CharField(
        max_length=2000, required=False, allow_null=True, allow_blank=True, default=None
    )

    class Meta:
        model = Item
        fields = ["name", "description"]


class ItemUpdateSerializer(serializers.ModelSerializer):
    """The update-body (PATCH) shape — matches `openapi.json`'s
    `ItemUpdate`: every field optional, so a client can PATCH a subset.
    Deliberately NO `default=` on either field (unlike `ItemCreateSerializer`
    above) — DRF only adds a key to `validated_data` for a field the
    client actually sent (explicit value, including an explicit
    `"description": null`) when `required=False` carries no default; an
    omitted field is left out of `validated_data` entirely. That mirrors
    FastAPI's `payload.model_dump(exclude_unset=True)` (app/api/routers/
    items.py's `update_item`) field-for-field: only explicitly-set fields
    land on the existing row (`core/views.py`'s `ItemViewSet.update`)."""

    name = serializers.CharField(min_length=1, max_length=200, allow_blank=False, required=False)
    description = serializers.CharField(max_length=2000, required=False, allow_null=True, allow_blank=True)

    class Meta:
        model = Item
        fields = ["name", "description"]


class HealthStatusSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `HealthStatus`: `{"status": "..."}`."""

    status = serializers.CharField()


class ReadinessStatusSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `ReadinessStatus`: `{"status": "..."}`."""

    status = serializers.CharField()


class RegisterRequestSerializer(serializers.Serializer):
    """Stage 5b (#44) `POST /auth/register` request shape — matches
    `openapi.json`'s `RegisterRequest` exactly: `email`/`password`, each
    `min_length=1`, both required. Same strictness posture as
    `LoginRequestSerializer` immediately below (a plain `Serializer`, not
    `ModelSerializer` — this never round-trips straight to `core.models.
    User`, `core.security.auth.stores.DjangoUserStore.create` owns that
    mapping, including the Argon2id hash `AuthService.register` computes
    from `password` before any DB write)."""

    email = serializers.CharField(min_length=1)
    password = serializers.CharField(min_length=1)


class LoginRequestSerializer(serializers.Serializer):
    """`POST /auth/login` request shape — matches `openapi.json`'s
    `LoginRequest`. Defined alongside backend/fastapi's `app/schemas/
    auth.py`, so the wire contract (and the `HTTPBearer` security scheme
    a real OpenAPI schema view documents) is locked in identically on
    both tracks."""

    email = serializers.CharField(min_length=1)
    password = serializers.CharField(min_length=1)


class RefreshRequestSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `RefreshRequest`. See `LoginRequestSerializer`.

    **`refresh_token` is optional, defaulting to `""`** -- mirroring
    backend/fastapi's own `RefreshRequest`, whose docstring carries the
    full rationale: a session client (the default transport) holds no
    refresh token and must not be 422'd for honestly omitting a field that
    means nothing on its transport. `required=False` + `allow_blank=True` +
    `default=""` is the DRF spelling of Pydantic's `refresh_token: str =
    ""`, and produces the byte-identical wire schema
    `tests/test_schema_conformance.py` holds the two blocks to."""

    refresh_token = serializers.CharField(required=False, allow_blank=True, default="")


class VerifyEmailRequestSerializer(serializers.Serializer):
    """Stage 5c (#45) `POST /auth/verify-email` request shape — matches
    `openapi.json`'s `VerifyEmailRequest` exactly: `token`, `min_length=1`,
    required. Same strictness posture as `RegisterRequestSerializer`/
    `LoginRequestSerializer` above — a plain `Serializer`, not
    `ModelSerializer`; the raw single-use token itself is opaque to this
    layer (`core.security.auth._core.SingleUseTokenService.consume` does
    the only real validation of it — see `core/views.py`'s
    `VerifyEmailView`)."""

    token = serializers.CharField(min_length=1)


class RequestPasswordResetRequestSerializer(serializers.Serializer):
    """Stage 5c (#45) `POST /auth/request-password-reset` request shape —
    matches `openapi.json`'s `RequestPasswordResetRequest`: `email`,
    `min_length=1`, required. See `VerifyEmailRequestSerializer`."""

    email = serializers.CharField(min_length=1)


class ResetPasswordRequestSerializer(serializers.Serializer):
    """Stage 5c (#45) `POST /auth/reset-password` request shape — matches
    `openapi.json`'s `ResetPasswordRequest`: `token`/`new_password`, each
    `min_length=1`, both required. `new_password` matches
    `RegisterRequestSerializer.password`'s own policy — no further
    complexity policy enforced at this layer (see that field's own
    comment)."""

    token = serializers.CharField(min_length=1)
    new_password = serializers.CharField(min_length=1)


class TokenResponseSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `TokenResponse`. Documentation-only as of
    this step — every `/auth/*` handler returns the plain `{"detail": ...}`
    501-stub body instead (`core/views.py`'s module docstring), never this
    shape; Stage 5 is what actually returns it."""

    access_token = serializers.CharField()
    refresh_token = serializers.CharField()
    token_type = serializers.CharField(default="bearer")


class PrincipalOutSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `PrincipalOut`. See `TokenResponseSerializer`.

    **`roles` is on the wire now** -- see backend/fastapi's own
    `PrincipalOut` docstring for the full rationale: session mode removes
    the access JWT a browser client could previously decode locally for a
    client-side role check, so `/auth/me` has to carry the answer itself.
    UI-only and non-authoritative; every real admin action is still
    enforced server-side by `has_role`/`require_roles_either`, which read
    the current principal's roles fresh on every request."""

    id = serializers.UUIDField()
    email = serializers.CharField()
    roles = serializers.ListField(child=serializers.CharField(), default=list)


# ---------------------------------------------------------------------------
# Documentation-only: the ErrorEnvelope shape, for drf-spectacular
# (Stage 4 Step 4, #27). `core.exceptions.exception_handler` builds every
# actual error response straight from `core.contract.errors.ErrorEnvelope`
# (the vendored Pydantic model) — it never constructs or validates against
# these DRF serializers, which exist ONLY so `core/views.py`'s
# `@extend_schema(responses={...: ErrorEnvelopeSerializer})` declarations
# have a DRF serializer to point the schema generator at (drf-spectacular
# documents DRF serializers, not arbitrary Pydantic models). Field-for-field
# copies of `core.contract.errors.{ErrorDetail,ErrorBody,ErrorEnvelope}` —
# kept in sync by hand since there are only three, small, rarely-changing
# fields; a drift here would only ever affect the DOCUMENTED schema, never
# the actual wire response (which is exactly what the wire-surface
# conformance proof, tests/test_schema_conformance.py, is there to catch).
# Class names deliberately end in `Serializer` so drf-spectacular's default
# naming (strips that suffix) produces component names `ErrorDetail`/
# `ErrorBody`/`ErrorEnvelope` — an exact match with `openapi.json`'s own
# component names, not a coincidence.
# ---------------------------------------------------------------------------


class ErrorDetailSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `ErrorDetail`: one item in an error's
    optional `details` list."""

    field = serializers.CharField(required=False, allow_null=True, default=None)
    message = serializers.CharField()


class ErrorBodySerializer(serializers.Serializer):
    """Matches `openapi.json`'s `ErrorBody`. `code` is a `ChoiceField` over
    `core.contract.errors.ErrorCode`'s own members — the same closed,
    versioned set — so the generated schema documents a proper enum, not an
    unconstrained string, matching `openapi.json`'s own `ErrorCode` enum
    component."""

    code = serializers.ChoiceField(choices=[member.value for member in ErrorCode])
    message = serializers.CharField()
    details = ErrorDetailSerializer(many=True, required=False, allow_null=True, default=None)


class ErrorEnvelopeSerializer(serializers.Serializer):
    """Matches `openapi.json`'s `ErrorEnvelope` — THE error shape every
    non-2xx response in this block uses except the one documented exception
    (429 rate-limit denial's plain `{"detail": ...}` — see README.md,
    "Conformance")."""

    error = ErrorBodySerializer()


# ---------------------------------------------------------------------------
# Stage 13b: admin user management -- matches
# `app/schemas/admin.py`'s `AdminUserOut`/`AdminRolesIn`/`UserStatus`
# field-for-field.
# ---------------------------------------------------------------------------

_USER_STATUS_CHOICES = ["active", "suspended", "banned"]


class AdminUserOutSerializer(serializers.Serializer):
    """The read shape every admin user-management endpoint returns.
    Deliberately NO `password_hash`, NO token fields -- see `app/schemas/
    admin.py`'s `AdminUserOut` docstring for the identical rationale. A
    plain `Serializer` (not `ModelSerializer`) constructed directly from a
    `core.models.User` instance (`AdminUserOutSerializer(user).data`) --
    DRF's plain `Serializer` reads each field via `getattr` on whatever
    instance it's given, the same as `ModelSerializer` would, without
    tying this shape to the model's own field set (e.g. `roles`/`status`
    stay plain declared fields here, not auto-derived)."""

    id = serializers.UUIDField()
    email = serializers.CharField()
    roles = serializers.ListField(child=serializers.CharField())
    status = serializers.ChoiceField(choices=_USER_STATUS_CHOICES)
    email_verified = serializers.BooleanField()
    created_at = serializers.DateTimeField()


class AdminRolesInSerializer(serializers.Serializer):
    """`PUT /admin/users/{user_id}/roles`'s request body -- matches `app/
    schemas/admin.py`'s `AdminRolesIn`: a full-replace role list, validated
    at the VIEW layer (`core/views.py`'s `AdminUserRolesView`) against the
    app's own closed allowed-role set -- this serializer only enforces "a
    list of strings", same posture that schema's own docstring documents.

    `validate()` below rejects any key in the request body that isn't a
    declared field -- parity with `AdminRolesIn`'s own `ConfigDict(extra=
    "forbid")` (that schema's docstring, `app/schemas/admin.py`), which 422s
    on an unknown top-level key. A plain DRF `Serializer` otherwise silently
    DROPS undeclared input keys instead of rejecting them (`is_valid()`
    only ever populates `validated_data` from declared `fields`, so an
    unknown key just never makes it in) -- comparing `self.initial_data`
    (the raw, as-received body DRF stashes before validation) against
    `self.fields` catches exactly that gap. Raising `serializers.
    ValidationError` here (not a bespoke exception) keeps this on the SAME
    path `AdminUserRolesView.put`'s `serializer.is_valid(raise_exception=
    True)` already uses for every other validation failure -- `core.
    exceptions.exception_handler`'s `rest_framework.exceptions.
    ValidationError` branch envelopes it as 422 `validation_failed`, same
    as an unknown ROLE (`AdminUserRolesView`'s own `_ALLOWED_ROLES` check)
    already is, just one layer earlier."""

    roles = serializers.ListField(child=serializers.CharField(), default=list)

    def validate(self, attrs: dict) -> dict:
        unexpected = set(self.initial_data) - set(self.fields)
        if unexpected:
            raise serializers.ValidationError(
                {field: "This field is not recognized." for field in sorted(unexpected)}
            )
        return attrs


# ---------------------------------------------------------------------------
# Stage 13d: blog/CMS -- matches `app/schemas/blog.py`'s `BlogPostSummaryOut`/
# `BlogPostOut`/`BlogPostCreate`/`BlogPostUpdate`/`CommentOut` field-for-field.
# ---------------------------------------------------------------------------

_BLOG_POST_STATUS_CHOICES = ["draft", "published"]
_COMMENT_STATUS_CHOICES = ["visible", "hidden", "pending"]

# Matches app/schemas/blog.py's SLUG_PATTERN exactly -- the ONE regex both
# backends validate a caller-supplied slug against.
SLUG_PATTERN = r"^[a-z0-9-]+$"

# Defense-in-depth size caps -- byte-identical intent and VALUE to
# `app/schemas/blog.py`'s `_BODY_HTML_MAX_CHARS`/
# `_BODY_JSON_MAX_SERIALIZED_CHARS` (see that module's own comment for the
# full rationale: this write-path is already admin-gated and rate-limited,
# so this is a generous backstop, not a tight editorial limit). Kept at the
# SAME 1,000,000-character value on both tracks deliberately -- `body_html`'s
# cap is `CharField(max_length=...)`, which drf-spectacular renders as a
# `maxLength` JSON Schema keyword -- exactly the keyword pydantic's own
# `Field(max_length=...)` renders on the FastAPI side -- so the two MUST
# match numerically or `tests/test_schema_conformance.py`'s strict wire-shape
# comparison fails on `maxLength` divergence.
_BODY_HTML_MAX_CHARS = 1_000_000
_BODY_JSON_MAX_SERIALIZED_CHARS = 1_000_000


def _validate_body_json_size(value: dict) -> dict:
    """Shared body for `BlogPostCreateSerializer`/`BlogPostUpdateSerializer`'s
    `validate_body_json` -- byte-identical check to `app/schemas/blog.py`'s
    `_check_body_json_size`: caps the SERIALIZED (`json.dumps`) size, not
    Python object identity/depth, the same quantity the FastAPI-side
    validator bounds. `DictField` has no `max_length` of its own the way
    `CharField` does, so this is a `validate_<field>` method, not a
    constructor kwarg -- deliberately NOT schema-visible (FastAPI's own
    `field_validator` is likewise a runtime-only check with no
    `Field(max_length=...)` counterpart on `body_json`, so neither side's
    OpenAPI schema documents a `body_json` size constraint; only
    `body_html`'s does, via `maxLength`) -- this keeps both tracks'
    wire-schemas for `body_json` identical (no divergence to reconcile)
    while still enforcing the SAME runtime cap on both."""
    serialized_len = len(json.dumps(value))
    if serialized_len > _BODY_JSON_MAX_SERIALIZED_CHARS:
        raise serializers.ValidationError(
            f"body_json is too large: serialized size {serialized_len} exceeds the "
            f"{_BODY_JSON_MAX_SERIALIZED_CHARS}-character cap."
        )
    return value


def _reject_unrecognized_fields(serializer: serializers.Serializer) -> None:
    """Shared body for the `validate()` override every blog write
    serializer below needs -- parity with `AdminRolesInSerializer.
    validate()`'s own identical check (see that method's own docstring):
    a plain DRF `Serializer`/`ModelSerializer` otherwise silently DROPS an
    undeclared input key instead of rejecting it, unlike `app/schemas/
    blog.py`'s `ConfigDict(extra="forbid")` Pydantic schemas."""
    unexpected = set(serializer.initial_data) - set(serializer.fields)
    if unexpected:
        raise serializers.ValidationError(
            {field: "This field is not recognized." for field in sorted(unexpected)}
        )


class BlogPostSummaryOutSerializer(serializers.Serializer):
    """The LIST shape (`GET /admin/blog/posts`) -- matches `app/schemas/
    blog.py`'s `BlogPostSummaryOut` exactly: deliberately NO body fields.
    A plain `Serializer` constructed directly from a `core.models.
    BlogPost` instance -- `author_id`/`post_id`-style fields below read
    straight off Django's own auto-generated `<fk_name>_id` attribute
    (`instance.author_id`), no explicit `source=` needed since the field
    name already matches."""

    id = serializers.UUIDField()
    title = serializers.CharField()
    slug = serializers.CharField()
    status = serializers.ChoiceField(choices=_BLOG_POST_STATUS_CHOICES)
    published_at = serializers.DateTimeField(allow_null=True)
    author_id = serializers.UUIDField()
    created_at = serializers.DateTimeField()


class BlogPostOutSerializer(BlogPostSummaryOutSerializer):
    """The single-post shape -- `BlogPostSummaryOutSerializer` plus both
    body columns. `body_html` is always the sanitized value already
    persisted (`core/services/sanitize.py`) -- this serializer never
    re-runs or bypasses sanitization, only reads back what the write-path
    already cleaned.

    `body_json` uses `DictField()`, NOT the more generic `JSONField()` --
    `app/schemas/blog.py`'s `dict[str, Any]` Pydantic type generates a
    `{"type": "object"}` JSON Schema; `JSONField` (any JSON-serializable
    value, including a bare string/number/array) has no fixed `type` to
    declare and drf-spectacular emits an unconstrained `{}` for it, a wire
    divergence from the frozen contract. `DictField` (an object whose keys
    are strings — exactly what a ProseMirror doc's top level always is)
    is both the semantically-correct match AND the schema-matching one."""

    body_json = serializers.DictField()
    body_html = serializers.CharField(allow_blank=True)


class BlogPostCreateSerializer(serializers.Serializer):
    """`POST /admin/blog/posts`'s request body -- matches `app/schemas/
    blog.py`'s `BlogPostCreate`: `slug` OPTIONAL (server derives one from
    `title` when omitted -- see `core/views.py`'s create view), `body_json`/
    `body_html` both REQUIRED. `body_html` is RAW/UNTRUSTED input at this
    layer -- the view sanitizes it before persisting, never this
    serializer (same "the write-path sanitizes, not the schema" split
    `BlogPostCreate`'s own docstring documents)."""

    title = serializers.CharField(min_length=1, max_length=200)
    slug = serializers.RegexField(
        regex=SLUG_PATTERN, min_length=1, max_length=220, required=False, allow_null=True, default=None
    )
    body_json = serializers.DictField()
    body_html = serializers.CharField(allow_blank=True, max_length=_BODY_HTML_MAX_CHARS)

    def validate_body_json(self, value: dict) -> dict:
        return _validate_body_json_size(value)

    def validate(self, attrs: dict) -> dict:
        _reject_unrecognized_fields(self)
        return attrs


class BlogPostUpdateSerializer(serializers.Serializer):
    """`PATCH /admin/blog/posts/{post_id}`'s request body -- matches `app/
    schemas/blog.py`'s `BlogPostUpdate`: every field optional (so a client
    can PATCH a subset). `allow_null=True` on all four (matching the
    frozen contract's own schema -- `BlogPostUpdate`'s Pydantic type is
    genuinely `str | None`/`dict[str, Any] | None`, so its GENERATED
    SCHEMA documents `nullable: true` even though its own
    `_reject_explicit_null` model validator rejects an ACTUAL null at
    runtime; see that validator's own docstring) -- `validate()` below
    reproduces the identical runtime behavior: an explicit `null` for any
    of these four NOT-NULL columns is rejected as a 422
    `validation_failed`, the SAME wire outcome `app/schemas/blog.py`'s
    validator produces, achieved here by checking `attrs` after DRF's own
    per-field validation already let a `None` value through
    (`allow_null=True` short-circuits `CharField`'s own `min_length`/etc.
    checks for a `None` input, so this is the one place left to catch
    it)."""

    title = serializers.CharField(min_length=1, max_length=200, required=False, allow_null=True)
    slug = serializers.RegexField(
        regex=SLUG_PATTERN, min_length=1, max_length=220, required=False, allow_null=True
    )
    body_json = serializers.DictField(required=False, allow_null=True)
    body_html = serializers.CharField(
        allow_blank=True, required=False, allow_null=True, max_length=_BODY_HTML_MAX_CHARS
    )

    def validate_body_json(self, value: dict | None) -> dict | None:
        # `None` here means "explicitly nulled" -- `validate()` below is
        # what turns that into a 422 (see this class's own docstring), so
        # there's nothing to size-check for a `None` value; pass it
        # through unchanged. Byte-identical short-circuit to `app/schemas/
        # blog.py`'s `BlogPostUpdate._check_body_json_size`.
        if value is None:
            return value
        return _validate_body_json_size(value)

    def validate(self, attrs: dict) -> dict:
        _reject_unrecognized_fields(self)
        offending = sorted(
            field for field in ("title", "slug", "body_json", "body_html") if field in attrs and attrs[field] is None
        )
        if offending:
            raise serializers.ValidationError(
                {field: "This field may not be null." for field in offending}
            )
        return attrs


class CommentOutSerializer(serializers.Serializer):
    """Matches `app/schemas/blog.py`'s `CommentOut` exactly: `id`,
    `post_id`, `author_id`, `body`, `status`, `created_at`."""

    id = serializers.UUIDField()
    post_id = serializers.UUIDField()
    author_id = serializers.UUIDField(allow_null=True)
    body = serializers.CharField(allow_blank=True)
    status = serializers.ChoiceField(choices=_COMMENT_STATUS_CHOICES)
    created_at = serializers.DateTimeField()


# ---------------------------------------------------------------------------
# Stage 13d public read (issue #54, the deferred acceptance item) -- the
# PUBLIC, unauthenticated blog read surface. Matches `app/schemas/blog.py`'s
# `PublicBlogPostSummaryOut`/`PublicBlogPostOut`/`derive_excerpt` field-for-
# field / byte-for-byte -- see that module's own docstrings for the full
# "no status, no body_json, ever" rationale this mirrors.
# ---------------------------------------------------------------------------

_EXCERPT_MAX_CHARS = 200
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")
_SPACE_BEFORE_PUNCTUATION_RE = re.compile(r"\s+([,.;:!?])")


def derive_excerpt(body_html: str) -> str | None:
    """Byte-identical algorithm to `app/schemas/blog.py`'s `derive_excerpt`
    -- see that function's own docstring for the full rationale (strip
    tags, collapse whitespace, tidy space-before-punctuation, truncate at
    a word boundary with a trailing `"…"`, `None` -- never `""` -- for an
    all-markup body)."""
    text = _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", body_html)).strip()
    text = _SPACE_BEFORE_PUNCTUATION_RE.sub(r"\1", text)
    if not text:
        return None
    if len(text) <= _EXCERPT_MAX_CHARS:
        return text
    truncated = text[:_EXCERPT_MAX_CHARS].rsplit(" ", 1)[0].rstrip()
    if not truncated:
        truncated = text[:_EXCERPT_MAX_CHARS].rstrip()
    return truncated + "…"


class PublicBlogPostSummaryOutSerializer(serializers.Serializer):
    """The PUBLIC list shape (`GET /blog/posts`) -- matches `app/schemas/
    blog.py`'s `PublicBlogPostSummaryOut` exactly: NO `status`, NO body
    fields, `excerpt` computed by `derive_excerpt` above (never a model
    column -- see `core/views.py`'s public blog views for where this
    serializer is actually constructed, always from a plain `dict`, never
    `PublicBlogPostSummaryOutSerializer(post)` straight off the ORM row,
    since `excerpt` has no matching attribute for DRF to read)."""

    id = serializers.UUIDField()
    title = serializers.CharField()
    slug = serializers.CharField()
    excerpt = serializers.CharField(allow_null=True)
    published_at = serializers.DateTimeField()
    author_id = serializers.UUIDField()
    created_at = serializers.DateTimeField()


class PublicBlogPostOutSerializer(PublicBlogPostSummaryOutSerializer):
    """The PUBLIC single-post shape (`GET /blog/posts/{slug}`) --
    `PublicBlogPostSummaryOutSerializer` plus `body_html` ONLY -- matches
    `app/schemas/blog.py`'s `PublicBlogPostOut` exactly. **`body_json` is
    NEVER a field here, full stop** -- see that schema's own docstring for
    why rendering it publicly would reopen the stored-XSS hole `core/
    services/sanitize.py:sanitize_blog_html()` exists to close."""

    body_html = serializers.CharField(allow_blank=True)


# ---------------------------------------------------------------------------
# Stage 13c: moderation -- matches `app/schemas/moderation.py`'s `FlagOut`/
# `FlagResolveIn`/`FlagDismissIn`/`FlagTargetType`/`FlagStatus`/
# `ResolveAction` field-for-field.
# ---------------------------------------------------------------------------

_FLAG_TARGET_TYPE_CHOICES = ["blog_post", "comment", "user"]
_FLAG_STATUS_CHOICES = ["open", "resolved", "dismissed"]
_RESOLVE_ACTION_CHOICES = ["none", "hide_content", "delete_content", "ban_author"]


class FlagOutSerializer(serializers.Serializer):
    """The read shape every moderation admin endpoint returns -- matches
    `app/schemas/moderation.py`'s `FlagOut` exactly. A plain `Serializer`
    constructed directly from a `core.models.Flag` instance (`reporter_id`/
    `resolved_by_id` read straight off Django's own auto-generated
    `<fk_name>_id` attribute, no explicit `source=` needed), same
    convention `BlogPostSummaryOutSerializer`'s own docstring documents."""

    id = serializers.UUIDField()
    target_type = serializers.ChoiceField(choices=_FLAG_TARGET_TYPE_CHOICES)
    target_id = serializers.UUIDField()
    reporter_id = serializers.UUIDField(allow_null=True)
    reason = serializers.CharField()
    status = serializers.ChoiceField(choices=_FLAG_STATUS_CHOICES)
    resolved_by_id = serializers.UUIDField(allow_null=True)
    resolved_at = serializers.DateTimeField(allow_null=True)
    resolution_note = serializers.CharField(allow_null=True, allow_blank=True)
    created_at = serializers.DateTimeField()


class FlagResolveInSerializer(serializers.Serializer):
    """`POST /admin/flags/{flag_id}/resolve`'s request body -- matches
    `app/schemas/moderation.py`'s `FlagResolveIn`: `action` picks the
    content/author side effect (validated at the VIEW layer, per
    `target_type` -- see `core/views.py`'s `AdminFlagResolveView`), `note`
    is an optional free-text resolution note. `validate()` below rejects
    any key in the request body that isn't a declared field -- same
    parity-with-`extra="forbid"` posture `AdminRolesInSerializer.
    validate()`'s own docstring documents."""

    action = serializers.ChoiceField(choices=_RESOLVE_ACTION_CHOICES)
    note = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, attrs: dict) -> dict:
        unexpected = set(self.initial_data) - set(self.fields)
        if unexpected:
            raise serializers.ValidationError(
                {field: "This field is not recognized." for field in sorted(unexpected)}
            )
        return attrs


class FlagDismissInSerializer(serializers.Serializer):
    """`POST /admin/flags/{flag_id}/dismiss`'s request body -- matches
    `app/schemas/moderation.py`'s `FlagDismissIn`: no content action,
    ever; `note` is the same optional free-text resolution note
    `FlagResolveInSerializer.note` documents."""

    note = serializers.CharField(required=False, allow_null=True, allow_blank=True, default=None)

    def validate(self, attrs: dict) -> dict:
        unexpected = set(self.initial_data) - set(self.fields)
        if unexpected:
            raise serializers.ValidationError(
                {field: "This field is not recognized." for field in sorted(unexpected)}
            )
        return attrs
