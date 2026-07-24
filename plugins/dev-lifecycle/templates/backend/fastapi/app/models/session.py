"""The `Session` model the vendored auth component's `SessionStore`
protocol is implemented against (see `app/core/security/auth/stores.py`'s
`SqlAlchemySessionStore`) — one row per live server-side session, the
DEFAULT browser credential in this app.

Persisted exactly as `_sessions.SessionRecord` describes: `session_hash`
(never the raw session id — see `_core.hash_token`'s own docstring on why
only a hash is ever stored, and `_sessions.generate_session_id`'s on why
the id itself is opaque and unguessable) is the lookup key;
`last_seen_at` carries the sliding idle deadline and `absolute_expires_at`
the hard ceiling, both of which `_sessions.SessionService.resolve` checks
on every authenticated request; `revoked` is what makes logout, a password
reset, and an administrative ban take effect on the NEXT request rather
than after a token TTL elapses.

Deliberately stores **no roles column**. `SessionService.resolve` reads the
user's current roles from `UserStore` on every request, so a role change
lands immediately — see that method's docstring, and this block's README
"Sessions" section, on why denormalizing roles here would reintroduce
exactly the staleness window sessions exist to close.

Not a vendored file itself — built on top of the vendored
`app/core/db/mixins.py`, the same composition pattern as
`app/models/refresh_token.py`/`app/models/user.py`. Composes
`UUIDPrimaryKey` + `TimestampMixin` only, NOT `SoftDeleteMixin`, for the
identical reason `RefreshToken` does: a session row's lifecycle is fully
captured by `revoked`/`absolute_expires_at` already, and a revoked row is
RETAINED rather than deleted so a replay of a logged-out session is
recognizable (and auditable) as revoked rather than merely absent.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy import Uuid as SAUuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base, TimestampMixin, UUIDPrimaryKey


class Session(Base, UUIDPrimaryKey, TimestampMixin):
    __tablename__ = "sessions"

    # UNIQUE -- the lookup key `SessionStore.get_by_hash` queries by
    # (SHA-256 hex digest of the raw session id, per `_core.hash_token`).
    # This index is on the hot path of EVERY authenticated request, which
    # is the read cost session auth trades statelessness for; a
    # primary-key-shaped unique lookup is what keeps that trade cheap.
    session_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        SAUuid(as_uuid=True, native_uuid=True),
        ForeignKey("users.id"),
        index=True,
        nullable=False,
    )
    # INDEXED via user_id above -- `SessionStore.revoke_all_for_user`
    # ("sign out everywhere", run on password reset and account
    # deactivation) is a single UPDATE filtered on this column.
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
