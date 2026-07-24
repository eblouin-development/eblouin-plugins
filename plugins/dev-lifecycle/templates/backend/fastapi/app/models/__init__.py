"""Model aggregator: imports every ORM model in the app so a single
`import app.models` registers the full schema on `Base.metadata`.

Nothing outside this package should import an individual model module
(`app.models.item`) directly for its import *side effect* — always go
through `app.models` (this file) instead, so a future model that forgets
to update this file fails loudly (missing from Alembic autogenerate,
missing from `Base.metadata.create_all()` in tests) rather than silently
never being migrated or registered. Importing a model's *class* directly
(`from app.models.item import Item`, as app/api/routers/items.py does) is
unaffected — this aggregator is about the registration side effect, not
about where callers get the class from."""

from __future__ import annotations

from app.models.blog_post import BlogPost  # noqa: F401
from app.models.comment import Comment  # noqa: F401
from app.models.flag import Flag  # noqa: F401
from app.models.item import Item  # noqa: F401
from app.models.login_attempt import LoginAttempt  # noqa: F401
from app.models.refresh_token import RefreshToken  # noqa: F401
from app.models.session import Session  # noqa: F401
from app.models.single_use_token import SingleUseToken  # noqa: F401
from app.models.user import User  # noqa: F401

__all__ = [
    "BlogPost",
    "Comment",
    "Flag",
    "Item",
    "LoginAttempt",
    "RefreshToken",
    "Session",
    "SingleUseToken",
    "User",
]
