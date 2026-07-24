"""Server-side sessions -- the default browser credential

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-24

Adds `sessions` (`app/models/session.py`) column-for-column, written by
hand rather than via `alembic revision --autogenerate` (no live DB was
used to generate this file, matching 0001-0006's own convention).

This is the table that makes server-side session auth possible at all:
every authenticated browser request resolves against a row here, which is
precisely what lets logout, a password reset, an administrative ban, and a
role change take effect on the NEXT request rather than after a JWT's TTL
elapses. See `app/core/security/auth/_sessions.py`'s module docstring for
the full argument, and this block's README "Sessions" section for the
wiring.

`sessions.session_hash` gets a UNIQUE index and IS the lookup key
`SessionStore.get_by_hash` queries by on every authenticated request --
the single hottest index in this schema. It stores the SHA-256 hex digest
of the raw session id (per `_core.hash_token`), never the id itself, so a
leaked backup or compromised read replica hands out no usable cookies.

`sessions.user_id` gets a plain index (the FK column) plus the FK
constraint itself, `ondelete` left at its default (RESTRICT) -- identical
reasoning to `refresh_tokens.user_id` in 0002: deleting a `User` row while
it still has `Session` rows is refused by the DB rather than silently
cascading away a security-relevant record of that user's past sessions.
That index is also what makes `revoke_all_for_user` ("sign out
everywhere", run on every password reset) a single indexed UPDATE.

No `roles` column, deliberately -- roles are resolved live from the users
table on every request (see `SessionService.resolve`), so there is nothing
here to go stale. No `deleted_at`/soft-delete column either: a revoked
session is marked `revoked`, and the row is RETAINED so a replay of a
logged-out session is recognizable as revoked rather than merely absent.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: Union[str, None] = "0006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.Uuid(as_uuid=True, native_uuid=True), primary_key=True),
        sa.Column("session_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(as_uuid=True, native_uuid=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_sessions_user_id_users"),
    )
    op.create_index("ix_sessions_session_hash", "sessions", ["session_hash"], unique=True)
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_sessions_user_id", table_name="sessions")
    op.drop_index("ix_sessions_session_hash", table_name="sessions")
    op.drop_table("sessions")
