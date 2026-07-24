"""The contract exemplar model: a minimal `Item` matching backend/fastapi's
`app/models/item.py` field-for-field (`id` UUID primary key, `created_at`/
`updated_at`, soft-delete via `deleted_at`, `name`/`description`) so this
block's eventual DRF serializer round-trips the same wire shape the FastAPI
block's Pydantic schemas already produce. Not a vendored file — this is this
step's own app code, the Django-ORM counterpart to
`templates/components/backend/db-mixins/mixins.py` (that component's own
module docstring: "a Django backend (Stage 4) does NOT reuse this file; it
reaches for Django's own `models.UUIDField`, `auto_now_add`/`auto_now`, and
a custom soft-delete manager instead" — this module is that reach)."""

from __future__ import annotations

import uuid

from django.db import models
from django.db.models import Q


class ItemQuerySet(models.QuerySet):
    """The queryset-level soft-delete filter — `Item.objects` (below) is
    built on this via `.as_manager()` so every default lookup
    (`Item.objects.all()`, `.get()`, `.filter()`) already excludes
    soft-deleted rows without a caller having to remember to add
    `deleted_at__isnull=True` themselves. Mirrors
    `db-mixins/mixins.py`'s `SoftDeleteMixin.not_deleted()` — the
    SQLAlchemy repository composes that as a `WHERE` fragment on every
    `select()`; this queryset is the Django-ORM equivalent default-scoping
    mechanism."""

    def not_deleted(self) -> "ItemQuerySet":
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> "ItemQuerySet":
        """Escape hatch for the rare caller (an admin view, a hard-delete
        cleanup job) that genuinely needs soft-deleted rows too — the
        default manager below never returns them."""
        return self


class ItemManager(models.Manager.from_queryset(ItemQuerySet)):
    """`Item.objects`'s default manager: every `Item.objects.<lookup>()`
    call is scoped through `ItemQuerySet.not_deleted()` first (`get_queryset`
    override below), the same "soft-deleted rows are invisible by default"
    posture `db-mixins/mixins.py`'s `SoftDeleteMixin` establishes for the
    SQLAlchemy block. Use `Item.objects.with_deleted()` (via
    `ItemQuerySet.with_deleted`) to opt out for the rare caller that needs
    everything."""

    def get_queryset(self) -> ItemQuerySet:
        return super().get_queryset().not_deleted()


class Item(models.Model):
    """Field-for-field match to backend/fastapi's `Item`
    (`UUIDPrimaryKey` + `TimestampMixin` + `SoftDeleteMixin` composed):
    `id` (UUID, default `uuid4`), `created_at` (set once, on insert),
    `updated_at` (bumped on every Django-ORM `.save()`), `deleted_at`
    (nullable — `NULL` means "not deleted"), `name`/`description`. See
    that model's own module docstring for the SQLAlchemy-side contract this
    mirrors.

    `objects` is `ItemManager` (soft-delete-scoped by default, see above);
    `all_objects` is the unscoped default `models.Manager()`, kept around
    for the same "escape hatch" reason `ItemQuerySet.with_deleted()`
    exists — Django recommends keeping an unfiltered manager available even
    when a custom default manager narrows `objects` (see Django's own
    "Custom managers and model inheritance" docs), since some internal
    machinery (e.g. `Model.objects.get_or_create` from a migration, an
    admin registration) expects an unscoped default manager to exist
    somewhere on the model."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None)

    name = models.CharField(max_length=200)
    description = models.CharField(max_length=2000, null=True, blank=True)

    objects = ItemManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "items"
        indexes = [
            # Partial index on deleted_at WHERE NULL — Postgres-specific
            # (Django compiles a no-op / full index on backends without
            # partial-index support, e.g. sqlite in the hermetic test
            # settings; harmless there, just not partial). Speeds up the
            # default manager's `deleted_at IS NULL` filter on every
            # unscoped lookup without indexing the (much rarer) soft-
            # deleted rows too. Mirrors `db-mixins/mixins.py`'s
            # `SoftDeleteMixin.not_deleted()` WHERE fragment being the
            # thing every default query filters on.
            models.Index(
                fields=["deleted_at"],
                name="items_not_deleted_idx",
                condition=Q(deleted_at__isnull=True),
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.name

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, when=None) -> None:
        """Python-side soft-delete mutation, mirroring
        `SoftDeleteMixin.mark_deleted()`'s API — sets `deleted_at` without
        saving; the caller decides when to `.save(update_fields=[...])`."""
        from django.utils import timezone

        self.deleted_at = when or timezone.now()


# ---------------------------------------------------------------------------
# Stage 5b (#44): auth tables -- the User/RefreshToken the vendored auth
# component's UserStore/RefreshTokenStore protocols
# (core/security/auth/_core.py) are implemented against (see
# core/security/auth/stores.py, Step 6). Column-for-column match to
# backend/fastapi's app/models/{user,refresh_token}.py, cross-checked
# against alembic/versions/0002_create_auth_tables.py -- see each model's
# own docstring for the exact shape mirrored and any intentional nuance.
# Not vendored files -- this block's own app code, same as Item above.
# ---------------------------------------------------------------------------


class UserQuerySet(models.QuerySet):
    """`User`'s soft-delete filter -- identical shape to `ItemQuerySet`
    above; see that class's own docstring. Composed by `UserManager`
    (below) into `User.objects`'s default, soft-delete-scoped queryset."""

    def not_deleted(self) -> "UserQuerySet":
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> "UserQuerySet":
        """Escape hatch — see `ItemQuerySet.with_deleted`'s own docstring.
        This is also the SECURITY-relevant queryset a raw, unscoped lookup
        would need to bypass `User.objects`'s default filtering; the
        vendored auth component's own store (`core/security/auth/
        stores.py`) deliberately never uses it — see that module's own
        "soft-delete auth-bypass fix" docstring on why its lookups always
        go through the soft-delete-scoped default manager instead."""
        return self


class UserManager(models.Manager.from_queryset(UserQuerySet)):
    """`User.objects`'s default manager — identical shape to `ItemManager`
    above; see that class's own docstring. `User.objects.with_deleted()` is
    the same opt-out escape hatch `Item.objects.with_deleted()` provides."""

    def get_queryset(self) -> UserQuerySet:
        return super().get_queryset().not_deleted()


class User(models.Model):
    """The `User` the vendored auth component's `UserStore` protocol
    (`core/security/auth/_core.py`) is implemented against (see
    `core/security/auth/stores.py`, Step 6) — column-for-column match to
    `backend/fastapi`'s `app/models/user.py` `User`
    (`UUIDPrimaryKey` + `TimestampMixin` + `SoftDeleteMixin` composed
    there) and `alembic/versions/0002_create_auth_tables.py`'s `users`
    table: `id` (UUID, default `uuid4`), `email` (unique, 320 chars —
    RFC 5321's own max mailbox-address length), `password_hash` (255
    chars — an Argon2id encoded hash string never approaches this, but
    it's the same generous bound the FastAPI model uses), `roles`
    (JSON, not Postgres `ARRAY` — see below), `created_at`/`updated_at`
    (Django `auto_now_add`/`auto_now`, the ORM-level equivalent of the
    SQLAlchemy model's `server_default=func.now()`/`onupdate=func.now()`),
    `deleted_at` (nullable — `NULL` means "not deleted").

    `email` is stored already-normalized (lowercased/stripped) by
    `_core.AuthService._normalize_email` — this model does not re-normalize
    it, so the unique constraint below is on exactly the value the auth
    core already normalized, matching `app/models/user.py`'s own docstring
    on the same point.

    `roles` uses `models.JSONField` (cross-dialect JSON, not a Postgres-only
    `ArrayField`) — deliberately, even though this table only ever runs on
    Postgres in prod: this app's hermetic test suite
    (`config/settings_test.py`) runs the identical model against sqlite3,
    and `ArrayField` has no sqlite equivalent. This is the SAME JSON-not-
    ARRAY choice `app/models/user.py`'s own docstring documents for the
    SQLAlchemy side — the one INTENTIONAL parity nuance this model shares
    with that reference (Postgres could use a native `ARRAY(varchar)`
    column instead; both tracks deliberately don't, for hermetic-testability
    reasons that apply equally to both ORMs). `default=list`, not `[]` — a
    callable default, not a single shared mutable one; see
    `app/models/user.py`'s own docstring for the identical mutable-default
    footgun this avoids.

    Deliberately a plain `User(models.Model)` — NOT `settings.
    AUTH_USER_MODEL`, NOT `AbstractBaseUser`, and this app registers no
    Django admin for it. This app's entire authentication surface is the
    vendored auth component's own JWT-based `AuthService`
    (`core/security/auth/_core.py`) — `AbstractBaseUser` exists to support
    Django's OWN session-cookie auth, password-reset tokens, and
    permission/group machinery, none of which this app uses; adopting it
    would pull in an entire parallel, unused authentication system purely
    for the field names it happens to also define.

    `objects` is `UserManager` (soft-delete-scoped by default — see
    `UserQuerySet` above); `all_objects` is the unscoped default
    `models.Manager()`, the same escape hatch `Item.all_objects` provides,
    kept for the same "some internal machinery expects an unscoped default
    manager to exist somewhere on the model" reason `Item`'s own docstring
    cites."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.CharField(max_length=320, unique=True)
    password_hash = models.CharField(max_length=255)
    roles = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None)
    # Stage 5c (#45): back `_core.UserRecord.email_verified` and
    # `UserStore.mark_email_verified` (see `core/security/auth/stores.py`'s
    # `DjangoUserStore.mark_email_verified`) -- set once `AccountService.
    # verify_email` successfully consumes a `"verify"` single-use token for
    # this user. `default=False` at the Python/ORM level (every row this app
    # inserts supplies it explicitly, same as every other field on this
    # model) -- migration 0003 additionally gives the DB column a
    # `db_default=False` so the migration itself backfills any pre-existing
    # row to a real, non-NULL `false` rather than leaving it undefined, the
    # same `server_default`-vs-`default` distinction `app/models/user.py`'s
    # own docstring documents for the SQLAlchemy side (see that module's
    # docstring, cross-referenced in migration 0003's own docstring).
    # Column-for-column match to `app/models/user.py`'s `email_verified`/
    # `verified_at` pair.
    email_verified = models.BooleanField(default=False, db_default=False)
    verified_at = models.DateTimeField(null=True, blank=True, default=None)
    # Stage 13b: backs the admin user-management surface (`core/views.py`'s
    # `AdminUser*View` classes) -- an app-level, closed set of
    # `{"active", "suspended", "banned"}`, a plain `CharField`, NOT a DB
    # enum -- column-for-column match to `app/models/user.py`'s `status`
    # (see that column's own docstring for the full "hermetic sqlite
    # testability" rationale this mirrors). `db_default="active"` is the
    # SAME `db_default`-vs-`default` two-layer precedent `email_verified`
    # above already establishes -- migration 0004 backfills every
    # pre-existing row to a real, non-NULL `"active"`.
    status = models.CharField(max_length=16, default="active", db_default="active")

    objects = UserManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "users"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.email

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, when=None) -> None:
        """Python-side soft-delete mutation — see `Item.mark_deleted`'s own
        docstring; the SECURITY-relevant consequence for `User` specifically
        is documented on `core/security/auth/stores.py`'s
        `DjangoUserStore.get_by_email`/`get_by_id` (a deactivated/soft-
        deleted user fails login and refresh, closed)."""
        from django.utils import timezone

        self.deleted_at = when or timezone.now()


class RefreshToken(models.Model):
    """One row per minted refresh token — the `RefreshToken` the vendored
    auth component's `RefreshTokenStore` protocol
    (`core/security/auth/_core.py`) is implemented against (see
    `core/security/auth/stores.py`, Step 6), persisted exactly as
    `_core.RefreshRecord` describes: `token_hash` (never the raw token —
    see `_core.hash_token`'s own docstring) is the lookup key, `used_at`/
    `revoked` implement the rotation-with-reuse-detection state machine
    (`_core.AuthService.refresh`'s docstring is THE reference for what this
    table's rows mean at each state).

    Deliberately composes plain `models.Model`, NOT `User`'s soft-delete
    manager above — a refresh-token row is never "soft deleted"; its
    lifecycle is fully captured by `used_at`/`revoked` already (see
    `_core.RefreshRecord`'s own docstring on why a used row is RETAINED,
    not deleted, soft or otherwise) — matching `app/models/refresh_token.py`
    composing `UUIDPrimaryKey` + `TimestampMixin` only, never
    `SoftDeleteMixin`.

    Column-for-column match to `backend/fastapi`'s
    `app/models/refresh_token.py` `RefreshToken` and
    `alembic/versions/0002_create_auth_tables.py`'s `refresh_tokens` table,
    with TWO INTENTIONAL parity nuances:

    1. **`user` uses `on_delete=models.PROTECT` — an app-level (Django ORM)
       enforcement — vs. Alembic 0002's DB-level `ForeignKeyConstraint`
       left at its default `ondelete` (RESTRICT).** Both refuse to delete a
       `User` row that still has `RefreshToken` rows pointing at it —
       `PROTECT` raises `django.db.models.ProtectedError` INSIDE the ORM,
       before any `DELETE` statement reaches the database, while RESTRICT
       is enforced by Postgres itself at the SQL level (would surface as an
       `IntegrityError` instead). Neither this app nor `backend/fastapi`
       ever hard-deletes a `User` row today (both use `User`'s soft-delete
       instead — see `User.mark_deleted` above), so this divergence isn't
       exercised in practice; `PROTECT` is still the correct Django-ORM
       expression of the same "don't silently lose a security-relevant
       audit trail of past sessions" intent `0002_create_auth_tables.py`'s
       own module docstring gives for choosing RESTRICT over CASCADE.
    2. **No `created_at`/`updated_at` columns**, unlike Alembic 0002's
       `refresh_tokens` table (which carries them via
       `app/models/refresh_token.py`'s `TimestampMixin`). Omitted
       deliberately, not an oversight: this app's stores
       (`core/security/auth/stores.py`, per this block's locked "stores use
       Django's ASYNC ORM" decision) write `mark_used`/`revoke_family` via
       a queryset-level `.filter(...).aupdate(...)` call, and Django's
       `auto_now` only fires on `Model.save()` — a queryset `.update()`/
       `.aupdate()` bypasses model-level field machinery entirely, so an
       `updated_at` column here would silently go stale on every write
       instead of reflecting reality. Leaving the column out is more honest
       than shipping a timestamp that lies. `issued_at` already carries
       this row's creation time for the table's actual purpose (rotation
       auditing), so nothing this table's own consumers need is lost."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # UNIQUE — the lookup key `RefreshTokenStore.get_by_hash` queries by
    # (SHA-256 hex digest of the raw token, per `_core.hash_token`).
    token_hash = models.CharField(max_length=64, unique=True)
    jti = models.CharField(max_length=32)
    # INDEXED (not unique) — `RefreshTokenStore.revoke_family` queries every
    # row sharing one `family_id` at once (reuse detection, logout).
    family_id = models.CharField(max_length=32, db_index=True)
    # PROTECT, not CASCADE — see this class's own docstring, nuance 1.
    user = models.ForeignKey(User, on_delete=models.PROTECT, db_index=True)
    issued_at = models.DateTimeField()
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True, default=None)
    revoked = models.BooleanField(default=False)

    class Meta:
        db_table = "refresh_tokens"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"RefreshToken(jti={self.jti})"


class Session(models.Model):
    """One row per live server-side session -- the `Session` the vendored
    auth component's `SessionStore` protocol
    (`core/security/auth/_sessions.py`) is implemented against (see
    `core/security/auth/stores.py`'s `DjangoSessionStore`), and **this
    block's DEFAULT browser credential**.

    Persisted exactly as `_sessions.SessionRecord` describes:
    `session_hash` (never the raw session id -- see `_core.hash_token`'s
    own docstring on why only a hash is stored, and
    `_sessions.generate_session_id`'s on why the id itself is opaque)
    is the lookup key; `last_seen_at` carries the sliding idle deadline and
    `absolute_expires_at` the hard ceiling, both checked on every
    authenticated request; `revoked` is what makes logout, a password
    reset, and an administrative ban take effect on the NEXT request rather
    than after a token TTL elapses.

    **No roles column, deliberately** -- `SessionService.resolve` reads the
    user's current roles from `UserStore` on every request, so a role
    change lands immediately. Denormalizing roles here would reintroduce
    exactly the staleness window sessions exist to close, over a session's
    much longer lifetime than a JWT's own short access TTL.

    Column-for-column match to `backend/fastapi`'s `app/models/session.py`
    and `alembic/versions/0007_create_sessions_table.py`'s `sessions`
    table, with the SAME two intentional parity nuances `RefreshToken`
    above documents at length:

    1. **`user` uses `on_delete=models.PROTECT`** (an ORM-level refusal)
       vs. Alembic 0007's DB-level FK left at its RESTRICT default -- both
       refuse to delete a `User` that still has `Session` rows, for the
       same "don't silently lose a security-relevant record of past
       sessions" reason.
    2. **No `updated_at` column.** This block's stores write via
       queryset-level `.aupdate()`, which bypasses Django's `auto_now`
       field machinery entirely, so an `updated_at` here would go stale on
       every write rather than reflect reality -- leaving it out is more
       honest than shipping a timestamp that lies. `created_at` IS kept
       (unlike `RefreshToken`, which has neither): `SessionService` needs
       it to measure `absolute_ttl` against, and it is written explicitly
       by `DjangoSessionStore.add` from the service's own injected clock
       rather than by `auto_now_add`, so it never drifts from the deadline
       computed alongside it."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # UNIQUE -- the lookup key `SessionStore.get_by_hash` queries by, on
    # EVERY authenticated request. The hottest index in this schema; a
    # unique-index lookup is what keeps the read cost session auth trades
    # statelessness for cheap.
    session_hash = models.CharField(max_length=64, unique=True)
    # PROTECT, not CASCADE -- see this class's own docstring, nuance 1.
    # Indexed because `revoke_all_for_user` ("sign out everywhere", run on
    # every password reset) filters on it.
    user = models.ForeignKey(User, on_delete=models.PROTECT, db_index=True)
    created_at = models.DateTimeField()
    last_seen_at = models.DateTimeField()
    absolute_expires_at = models.DateTimeField()
    revoked = models.BooleanField(default=False)

    class Meta:
        db_table = "sessions"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Session(user_id={self.user_id}, revoked={self.revoked})"


# ---------------------------------------------------------------------------
# Stage 5c (#45): account-lifecycle tables -- the SingleUseToken/LoginAttempt
# the vendored auth component's SingleUseTokenStore/LockoutStore protocols
# (core/security/auth/_core.py) are implemented against (see
# core/security/auth/stores.py). Column-for-column match to backend/fastapi's
# app/models/{single_use_token,login_attempt}.py, cross-checked against
# alembic/versions/0003_stage5c_account_lifecycle.py -- see each model's own
# docstring for the exact shape mirrored and any intentional nuance. Not
# vendored files -- this block's own app code, same as Item/User/RefreshToken
# above.
# ---------------------------------------------------------------------------


class SingleUseToken(models.Model):
    """One row per issued email-verification or password-reset token -- the
    `SingleUseToken` the vendored auth component's `SingleUseTokenStore`
    protocol (`core/security/auth/_core.py`) is implemented against (see
    `core/security/auth/stores.py`'s `DjangoSingleUseTokenStore`), persisted
    exactly as `_core.SingleUseTokenRecord` describes: `token_hash` (never the
    raw token -- see `_core.hash_token`'s own docstring) is the lookup key,
    `used_at` implements the single-use/reuse-rejection state
    (`_core.SingleUseTokenService.consume`'s docstring is THE reference for
    what this table's rows mean at each state). Column-for-column match to
    `backend/fastapi`'s `app/models/single_use_token.py` `SingleUseToken` and
    `alembic/versions/0003_stage5c_account_lifecycle.py`'s
    `single_use_tokens` table.

    `user` uses `on_delete=models.PROTECT` -- the SAME app-level (Django ORM)
    enforcement `RefreshToken.user` uses above (see that class's own
    docstring, nuance 1), for the identical reason applied here: Alembic
    0003's `single_use_tokens.user_id` FK is left at its DB-level default
    `ondelete` (RESTRICT), and `PROTECT` is the Django-ORM expression of that
    same "don't silently lose a security-relevant token-issuance history"
    intent -- deleting a `User` row while it still has outstanding/consumed
    single-use token rows is refused rather than silently cascading away that
    history. Neither this app nor `backend/fastapi` ever hard-deletes a
    `User` row today (both use `User.mark_deleted` / `SoftDeleteMixin`
    instead), so this divergence from CASCADE isn't exercised in practice.

    `created_at`/`expires_at`/`used_at` are each explicit,
    application-supplied columns (set from a `SingleUseTokenRecord` built by
    `SingleUseTokenService.issue`/`consume` against its OWN injected `now()`
    -- exactly how `RefreshToken.issued_at`/`expires_at`/`used_at` above are
    handled), so this model deliberately does NOT use `auto_now_add`/
    `auto_now` for `created_at` (which would set it from the DB/request
    clock, not `SingleUseTokenService`'s own injected, test-deterministic
    `now`) -- matching `app/models/single_use_token.py`'s own docstring on
    why it composes `UUIDPrimaryKey` only, no `TimestampMixin`. A single-use
    token is never soft-deleted either -- its lifecycle is fully captured by
    `used_at`, the same "retain, don't delete" posture `RefreshToken`'s own
    docstring documents."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # UNIQUE — the lookup key `SingleUseTokenStore.get_by_hash` queries by
    # (SHA-256 hex digest of the raw token, per `_core.hash_token`).
    token_hash = models.CharField(max_length=64, unique=True)
    # INDEXED (not unique) — a user can have more than one outstanding
    # single-use token (e.g. a verify token and a reset token at once).
    # PROTECT, not CASCADE — see this class's own docstring.
    user = models.ForeignKey(User, on_delete=models.PROTECT, db_index=True)
    # "verify" or "reset" today — `_core.py` does not enumerate the allowed
    # values as a closed set (see `SingleUseTokenRecord`'s own docstring), so
    # this stays a plain CharField rather than a DB-level enum/CHECK
    # constraint.
    purpose = models.CharField(max_length=32)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "single_use_tokens"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"SingleUseToken(purpose={self.purpose})"


class LoginAttempt(models.Model):
    """One row per account currently being tracked for failed-login lockout
    bookkeeping -- the `LoginAttempt` the vendored auth component's
    `LockoutStore` protocol (`core/security/auth/_core.py`) is implemented
    against (see `core/security/auth/stores.py`'s `DjangoLockoutStore`),
    persisted exactly as `_core.AttemptRecord` describes (`_core.
    LockoutPolicy`'s own docstring is THE reference for what this table's
    `failure_count`/`first_failure_at`/`last_failure_at`/`locked_until`
    columns mean at each state -- ALL of the counting/threshold/
    rolling-window logic lives in `LockoutPolicy`, not here; this model is
    dumb persistence for whatever `AttemptRecord` it's handed). Column-for-
    column match to `backend/fastapi`'s `app/models/login_attempt.py`
    `LoginAttempt` and `alembic/versions/0003_stage5c_account_lifecycle.py`'s
    `login_attempts` table.

    `account_key` stores the id `_core.AuthService.login` passes AS TEXT (a
    plain `CharField`, not a `UUIDField` `ForeignKey`) -- `_core.
    LockoutStore`'s own docstring notes a framework adapter is free to key it
    some other way than a bare user id (e.g. `f"{user_id}:{client_ip}"`); a
    plain `CharField`, UNIQUE (one row per `account_key`, matching
    `LockoutPolicy`'s "the one row per account" contract -- see
    `DjangoLockoutStore.upsert`) keeps this table correct for either keying
    scheme without assuming `account_key` is always a `User.id`. This table
    has NO foreign key to `User` -- deliberately decoupled at the DB level,
    matching `app/models/login_attempt.py`'s own module docstring and
    `alembic/versions/0003_stage5c_account_lifecycle.py`'s own note on the
    same point.

    No `auto_now_add`/`auto_now`/`TimestampMixin`-style bookkeeping here,
    same reasoning `SingleUseToken` above documents: `first_failure_at`/
    `last_failure_at`/`locked_until` are each already explicit,
    application-supplied columns (`LockoutPolicy`'s own injected `now()`),
    and a lockout row is deleted outright by `clear()` (`_core.
    LockoutPolicy.clear` -> `LockoutStore.clear`), never soft-deleted --
    there is no "tombstone" state worth keeping around for spent lockout
    bookkeeping the way there is for a used refresh/single-use token."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    account_key = models.CharField(max_length=320, unique=True)
    failure_count = models.IntegerField()
    first_failure_at = models.DateTimeField()
    last_failure_at = models.DateTimeField()
    locked_until = models.DateTimeField(null=True, blank=True, default=None)

    class Meta:
        db_table = "login_attempts"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"LoginAttempt(account_key={self.account_key})"


# ---------------------------------------------------------------------------
# Stage 13d: blog/CMS tables -- BlogPost/Comment, the admin surface
# core/views.py's blog admin views serve. Column-for-column match to
# backend/fastapi's app/models/{blog_post,comment}.py, cross-checked
# against alembic/versions/0005_stage13d_blog.py -- see each model's own
# docstring for the exact shape mirrored. Not vendored files -- this
# block's own app code, same as Item/User/RefreshToken above.
# ---------------------------------------------------------------------------


class BlogPostQuerySet(models.QuerySet):
    """`BlogPost`'s soft-delete filter -- identical shape to
    `ItemQuerySet`/`UserQuerySet` above; see `ItemQuerySet`'s own
    docstring."""

    def not_deleted(self) -> "BlogPostQuerySet":
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> "BlogPostQuerySet":
        return self


class BlogPostManager(models.Manager.from_queryset(BlogPostQuerySet)):
    def get_queryset(self) -> BlogPostQuerySet:
        return super().get_queryset().not_deleted()


class BlogPost(models.Model):
    """Column-for-column match to `backend/fastapi`'s
    `app/models/blog_post.py` `BlogPost` — see that model's own docstring
    for the full render-rule/FK-integrity rationale this mirrors.

    `body_json` (the raw ProseMirror doc) is stored OPAQUE — a plain
    `JSONField`, never rendered anywhere public, reloaded into the (later,
    Stage 13d UI) TipTap editor only. `body_html` is the SANITIZED render
    source of truth — `core/services/sanitize.py`'s `sanitize_blog_html()`
    is called on it by the write-path (`core/views.py`'s blog admin views)
    BEFORE this column is ever written.

    `status` is a plain `CharField`, NOT a DB enum — the SAME `User.
    status` precedent (see that field's own comment above), over
    `{"draft", "published"}`. `db_default="draft"` is the SAME
    `db_default`-vs-`default` two-layer precedent `User.status`/`User.
    email_verified` already establish.

    `author` uses `on_delete=models.PROTECT` — the SAME app-level
    (Django ORM) enforcement `RefreshToken.user`/`SingleUseToken.user`
    use above (see `RefreshToken`'s own docstring, nuance 1) — deleting a
    `User` row while it still owns `BlogPost` rows is refused rather than
    silently cascading away authored content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # NOT `unique=True` — DB-level uniqueness is enforced by the PARTIAL
    # `uq_blog_posts_slug_active` constraint in `Meta.constraints` below
    # instead (see that constraint's own comment for why). Plain
    # CharField, NOT Django's own `SlugField` — `SlugField`'s built-in
    # `validate_slug` validator accepts `[-a-zA-Z0-9_]+` (uppercase AND
    # underscore both allowed), a WIDER charset than this app's own
    # `^[a-z0-9-]+$` policy (`core/serializers.py`'s
    # `BlogPostCreateSerializer`/`BlogPostUpdateSerializer`); using
    # `SlugField` here would invite a second, looser, easy-to-forget
    # validation path to drift from the one this app actually enforces at
    # the request boundary.
    slug = models.CharField(max_length=220)
    title = models.CharField(max_length=200)
    body_json = models.JSONField()
    body_html = models.TextField()
    status = models.CharField(max_length=16, default="draft", db_default="draft")
    published_at = models.DateTimeField(null=True, blank=True, default=None)
    # PROTECT, not CASCADE — see this class's own docstring.
    author = models.ForeignKey(User, on_delete=models.PROTECT, db_index=True, related_name="blog_posts")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None)

    objects = BlogPostManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "blog_posts"
        indexes = [
            models.Index(fields=["status"], name="blog_posts_status_idx"),
        ]
        constraints = [
            # PARTIAL unique constraint (WHERE deleted_at IS NULL), not a
            # plain `unique=True` column constraint on `slug` above —
            # `core/views.py`'s `_slug_taken` friendly-error-path check
            # scopes its lookup through `BlogPost.objects`'s own soft-
            # delete-scoped default manager (a soft-deleted post's slug is
            # considered FREE), so the DB-level backstop has to agree with
            # that scoping or the two disagree: create `foo`, soft-delete
            # it, create another `foo` — the friendly check says "free",
            # the INSERT reaches a full-table-unique constraint anyway,
            # and that daylights as an unenveloped 500 (`IntegrityError`)
            # instead of a clean 201. `condition=Q(deleted_at__isnull=
            # True)` makes the constraint match the default manager's own
            # scoping exactly: only one LIVE row may hold a given slug at
            # once; any number of soft-deleted rows may still hold it.
            # Mirrored exactly in `core/migrations/0005_stage13d_blog.py`'s
            # `AddConstraint` — keep both in sync. Byte-identical intent to
            # `app/models/blog_post.py`'s `uq_blog_posts_slug_active`
            # partial index on the FastAPI track.
            models.UniqueConstraint(
                fields=["slug"],
                condition=Q(deleted_at__isnull=True),
                name="uq_blog_posts_slug_active",
            ),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.slug

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, when=None) -> None:
        from django.utils import timezone

        self.deleted_at = when or timezone.now()


class CommentQuerySet(models.QuerySet):
    """`Comment`'s soft-delete filter -- identical shape to
    `ItemQuerySet`/`UserQuerySet`/`BlogPostQuerySet` above."""

    def not_deleted(self) -> "CommentQuerySet":
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> "CommentQuerySet":
        return self


class CommentManager(models.Manager.from_queryset(CommentQuerySet)):
    def get_queryset(self) -> CommentQuerySet:
        return super().get_queryset().not_deleted()


class Comment(models.Model):
    """Column-for-column match to `backend/fastapi`'s
    `app/models/comment.py` `Comment` — see that model's own docstring for
    the full "admin list/hide/delete only, no public create in THIS
    stage" scope note and the never-store-raw-untrusted-HTML warning on
    `body`.

    `status` is a plain `CharField`, NOT a DB enum, over `{"visible",
    "hidden", "pending"}`, `db_default="visible"`. `post`/`author` are
    both `on_delete=models.PROTECT` — same "don't silently cascade away
    content" rationale `BlogPost.author`'s own docstring documents;
    `author` is additionally `null=True` (an optional FK — see the
    FastAPI model's own docstring on why)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    post = models.ForeignKey(BlogPost, on_delete=models.PROTECT, db_index=True, related_name="comments")
    author = models.ForeignKey(
        User, on_delete=models.PROTECT, db_index=True, null=True, blank=True, related_name="blog_comments"
    )
    body = models.TextField()
    status = models.CharField(max_length=16, default="visible", db_default="visible")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None)

    objects = CommentManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "blog_comments"
        indexes = [
            models.Index(fields=["status"], name="blog_comments_status_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Comment(post_id={self.post_id})"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, when=None) -> None:
        from django.utils import timezone

        self.deleted_at = when or timezone.now()


# ---------------------------------------------------------------------------
# Stage 13c: moderation -- the `Flag` admin queue. Column-for-column match
# to backend/fastapi's `app/models/flag.py` -- see that model's own
# docstring for the full "admin-only queue, polymorphic target, no
# cross-table FK" rationale this mirrors. Not a vendored file -- this
# block's own app code, same as Item/User/BlogPost/Comment above.
# ---------------------------------------------------------------------------


class FlagQuerySet(models.QuerySet):
    """`Flag`'s soft-delete filter -- identical shape to
    `ItemQuerySet`/`UserQuerySet`/`BlogPostQuerySet`/`CommentQuerySet`
    above."""

    def not_deleted(self) -> "FlagQuerySet":
        return self.filter(deleted_at__isnull=True)

    def with_deleted(self) -> "FlagQuerySet":
        return self


class FlagManager(models.Manager.from_queryset(FlagQuerySet)):
    def get_queryset(self) -> FlagQuerySet:
        return super().get_queryset().not_deleted()


class Flag(models.Model):
    """Column-for-column match to `backend/fastapi`'s `app/models/flag.py`
    `Flag` -- see that model's own docstring for the full rationale this
    mirrors: admin-only moderation queue (no end-user create endpoint
    anywhere in this app -- a consuming app writes rows itself), a
    POLYMORPHIC `target_type`/`target_id` pair with deliberately NO
    cross-table FK (`target_type` is what `core/views.py`'s moderation
    resolve view dispatches on; `target_id` is looked up by hand, per
    `target_type`, at the view layer), and `reporter` as the ONE FK on
    this model that is NOT `on_delete=models.PROTECT` (`SET_NULL` instead --
    see the FastAPI model's own docstring for why a flag's own audit value
    outlives its reporter's account).

    `target_type`/`status` are both plain `CharField`s, NOT DB enums -- the
    SAME `User.status`/`BlogPost.status`/`Comment.status` precedent (see
    each of those fields' own comments). `status` defaults `"open"` at both
    the Python/ORM level and the DB level (`db_default="open"`), the same
    two-layer shape `BlogPost.status`/`Comment.status` document."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    # Plain CharField, NOT a DB enum -- see class docstring. No FK on
    # target_id below -- this column is what a lookup dispatches on.
    target_type = models.CharField(max_length=16, db_index=True)
    # Polymorphic, deliberately no ForeignKey -- see class docstring.
    target_id = models.UUIDField(db_index=True)
    # Optional -- a consuming app supplies it; NULL is a legitimate,
    # permanent state. SET_NULL, not PROTECT/CASCADE -- see class
    # docstring for why this is the one FK on this model that isn't
    # RESTRICT/PROTECT like every other FK in this catalog.
    reporter = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        db_index=True,
        null=True,
        blank=True,
        related_name="reported_flags",
    )
    reason = models.TextField()
    # Plain CharField, NOT a DB enum -- see class docstring. `db_index=True`
    # (unlike `BlogPost.status`/`Comment.status`, which rely solely on
    # their own `Meta.indexes` entry) -- byte-identical intent to
    # `app/models/flag.py`'s individually-`index=True` `status` column on
    # the FastAPI track, on top of the SAME composite `Meta.indexes` entry
    # below (`flags_status_target_type_idx`) both tracks also carry.
    status = models.CharField(max_length=16, default="open", db_default="open", db_index=True)
    # The ACTING ADMIN -- set once, at resolve/dismiss time. PROTECT, not
    # CASCADE/SET_NULL -- the SAME "don't silently cascade away an audit
    # trail" rationale `BlogPost.author`/`RefreshToken.user` document above
    # (see class docstring for why this FK differs from `reporter`'s).
    resolved_by = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        db_index=True,
        null=True,
        blank=True,
        related_name="resolved_flags",
    )
    resolved_at = models.DateTimeField(null=True, blank=True, default=None)
    resolution_note = models.TextField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True, default=None)

    objects = FlagManager()
    all_objects = models.Manager()

    class Meta:
        db_table = "flags"
        indexes = [
            # The admin queue's own composite filter shape (`?status=&
            # target_type=`) -- byte-identical intent to
            # `app/models/flag.py`'s `ix_flags_status_target_type` on the
            # FastAPI track.
            models.Index(fields=["status", "target_type"], name="flags_status_target_type_idx"),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"Flag(target_type={self.target_type}, target_id={self.target_id})"

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    def mark_deleted(self, *, when=None) -> None:
        from django.utils import timezone

        self.deleted_at = when or timezone.now()
