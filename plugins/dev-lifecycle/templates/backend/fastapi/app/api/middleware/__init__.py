"""App-level ASGI middleware — this app's own code, not vendored.

Distinct from `app/core/security/*`'s middleware (security headers, rate
limiting, request-id binding), which are vendored catalog components: the
middleware here composes those components' primitives into
application-specific policy. `SessionCsrfMiddleware` is the current
inhabitant — it wraps the vendored auth component's `enforce_csrf` in the
method/cookie/path filtering this app's session-mode routing needs.
"""

from __future__ import annotations

from app.api.middleware.csrf import SessionCsrfMiddleware

__all__ = ["SessionCsrfMiddleware"]
