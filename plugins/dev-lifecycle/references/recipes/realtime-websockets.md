<!--
recipe: realtime-websockets
applies-to:
  - backend block: fastapi (native Starlette WebSocket — the kit-documented path; see references/backend/websockets.md)
  - backend block: django — NOT covered by this kit today (Django Channels is a project addition; see "What the kit does not provide")
last-verified: 2026-07-23
provenance: manual
sources:
  - https://fastapi.tiangolo.com/advanced/websockets/
  - https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html
  - references/backend/websockets.md
  - references/backend/redis.md
  - templates/components/security/auth/_core.py
  - templates/components/security/auth/fastapi.py
-->

# Realtime: WebSockets / SSE

Wire a real-time push channel to a client — FastAPI's native `WebSocket` endpoint for bidirectional streams, authenticated at the handshake using the existing auth component's token verification, with a Redis pub/sub fan-out path when the deployment runs more than one process. Everything here is **subordinate to the project's existing conventions** — when they conflict, the project wins.

## Contents
- What this wires
- Prerequisites
- Wire-up steps (FastAPI native WebSocket)
- Auth on the handshake
- Connection lifecycle
- Scaling: fan-out across processes/instances
- Server-Sent Events (SSE) as the one-way alternative
- What the kit does not provide (Django Channels)
- Doc fragment

## What this wires
Applying this recipe gives a feature a working real-time channel: a client opens a `WebSocket` connection to a FastAPI endpoint, the handshake is authenticated against the same principal the rest of the API trusts, the connection is tracked in a per-process connection manager, and — once the deployment runs more than one Uvicorn/Gunicorn worker or more than one instance — messages fan out across all of them via Redis pub/sub rather than silently only reaching clients on the process that originated the message.

It **composes existing pieces**:
- **`references/backend/websockets.md`** — the kit's own WebSocket convention doc: the FastAPI server-side endpoint/receive-loop pattern, the connection-manager pattern and its stated per-process limit, auth-before-`accept()`, one-reader/one-writer-task concurrency, and the "no blocking work in the handler" rule. This recipe wires a project's own real-time feature to it; it does not restate its content.
- **`templates/components/security/auth/_core.py`**'s `AuthService.resolve_access(raw_access_token) -> AccessClaims` — the same access-token verification every authenticated HTTP route already goes through (via `fastapi.py`'s `build_get_current_principal`), reused directly for the WS handshake since a browser cannot set an `Authorization` header on the WebSocket upgrade request (see "Auth on the handshake" below).
- **`references/backend/redis.md`**'s "Pub/sub vs streams" section — the cross-process fan-out mechanism `websockets.md`'s own "Server: broadcasting" section names as the fix for its stated per-process limit, and the same Redis instance the `background-jobs` recipe's Celery broker (Django track) or a project's own cache already runs against.

## Prerequisites
- A backend block on the **FastAPI track** with the auth component vendored (`app/core/security/auth/`) — this recipe's handshake-auth step calls the same `AuthService` every other authenticated route already constructs per-request.
- A Redis instance reachable from the app, **only if** the deployment runs more than one process/instance and cross-client broadcast must work across all of them (see "Scaling" — a single-process deployment can skip this and still work correctly for every client connected to that one process).
- No new compatibility-matrix row: `websockets.readthedocs.io`'s client library (`websockets` 16.x) is for a project **consuming** an external WS stream (see `references/backend/websockets.md`'s own client-side sections); a FastAPI project **serving** WebSocket connections needs no additional package beyond `fastapi`/`starlette`, already pinned in the compatibility matrix's Backend row.

## Wire-up steps (FastAPI native WebSocket)
1. **Declare the endpoint and accept only after auth succeeds** (see "Auth on the handshake" next — don't `accept()` first and check identity after; a rejected connection should never complete the upgrade). Per `references/backend/websockets.md`'s "Server: FastAPI endpoint & receive loop":
   ```python
   from fastapi import WebSocket, WebSocketDisconnect

   @app.websocket("/ws/{room_id}")
   async def ws_endpoint(websocket: WebSocket, room_id: str):
       claims = await authenticate_ws(websocket)  # see "Auth on the handshake"
       if claims is None:
           await websocket.close(code=1008)  # policy violation — reject before accept()
           return
       await websocket.accept()
       manager.add(room_id, websocket)
       try:
           while True:
               msg = await websocket.receive_json()
               await handle_inbound(room_id, claims, msg)
       except WebSocketDisconnect:
           manager.remove(room_id, websocket)
   ```
2. **Wrap the whole receive loop in `try`/`except WebSocketDisconnect`** — an uncaught disconnect spams logs and leaks the connection's registration in the manager, per `websockets.md`.
3. **Run a separate reader task and writer task if the endpoint both pushes server-originated data and reads client messages** (`asyncio.create_task` for each) — a single loop cannot `await receive` and `await send` concurrently, per `websockets.md`'s "Server: auth, concurrency, blocking work."
4. **Never do blocking work in the handler.** A sync DB/CPU call freezes the event loop for every connection on that process, not just the one handling it — offload to a threadpool or, for anything heavier, the `background-jobs` recipe's task-queue path.
5. **Version every message** (`{"v": 1, "type": "...", ...}`) and validate inbound payloads with Pydantic, per `websockets.md`'s "Message schema" — ignore an unrecognized `type` forward-compatibly rather than erroring the connection closed.

## Auth on the handshake
Browsers cannot set arbitrary headers on a WebSocket upgrade request, so the bearer-token flow every other authenticated route uses (`Authorization: Bearer <token>` via `HTTPBearer`) does not apply directly to `/ws/*`. Per `websockets.md`'s own "Server: auth, concurrency, blocking work" section, authenticate via a **query-param token** or a **cookie**, validated **before** `accept()`:
- **Bearer-mode clients (mobile, or a web client in bearer mode)** — pass the short-lived access token as a query parameter on the upgrade URL (`wss://.../ws/{room_id}?token=<access_token>`) and resolve it through the **same** `AuthService.resolve_access(token)` every HTTP route uses via `build_get_current_principal` — don't hand-roll a second token-verification path:
  ```python
  async def authenticate_ws(websocket: WebSocket) -> AccessClaims | None:
      token = websocket.query_params.get("token")
      if not token:
          return None
      try:
          return await auth_service.resolve_access(token)
      except _core.AuthError:
          return None
  ```
  A token in a query string lands in access/proxy logs more readily than a header — mitigate by keeping the access token's TTL short (the same TTL the end-to-end-auth recipe already pins) and never logging the full request URL with query string at the edge/proxy layer.
- **Cookie-borne clients (web, session mode — the default)** — the browser attaches the existing `HttpOnly` `session_id` cookie automatically on the WS upgrade (cookies are sent on same-origin WebSocket handshakes same as any other request); read it via `websocket.cookies.get(...)` (Starlette's `WebSocket` exposes the same `.cookies` mapping as `Request`) and resolve the session the same way a session-authenticated HTTP route would (`_sessions.SessionService.resolve`). CSRF's double-submit check does not apply here — a WS handshake is not a state-changing form-style request the way a `POST` is, and the origin is already implicitly checked by the browser's same-origin WS policy plus the app's own CORS/`Origin` allowlist.
- **Never accept an unauthenticated WS connection "to check identity in the first inbound message" instead** — that races an attacker's crafted first message against your own auth check and briefly holds an unauthenticated connection open; reject at the handshake per step 1 above.

## Connection lifecycle
- **Connect:** validate the token/cookie, `accept()`, register in the connection manager (`websockets.md`'s in-memory `set[WebSocket]` pattern, or a per-room `dict[str, set[WebSocket]]` for the room-scoped case above).
- **Disconnect (client-initiated or network drop):** caught as `WebSocketDisconnect` in the receive loop — always deregister from the manager in that branch, never assume a clean close.
- **Server-initiated shutdown:** on app lifespan shutdown, close each tracked socket with code `1001` (going away) so clients reconnect elsewhere instead of erroring — per `websockets.md`'s "Message schema, shutdown, testing."
- **Heartbeats:** Starlette's server-side `WebSocket` doesn't run the same automatic ping/pong loop the `websockets` **client** library does by default — if a dead connection needs detecting proactively (rather than waiting for the next failed `send`/`receive`), send an application-level ping message on an interval and prune a peer that doesn't ack within a timeout window; don't assume TCP-level keepalive alone catches a silently-wedged client.

## Scaling: fan-out across processes/instances
`websockets.md`'s own "Server: broadcasting" section states the limit plainly: an in-memory connection manager is **per-process** — a client connected to worker A never sees a message published from worker B. The moment a deployment runs more than one Uvicorn/Gunicorn worker or more than one instance (the normal case in production), a single-process manager silently drops cross-process broadcasts rather than erroring, which makes this easy to miss in dev (one process) and painful to discover in prod (several).

Fix: **publish to Redis pub/sub, have each process subscribe and forward to its own local sockets** — per `references/backend/redis.md`'s "Pub/sub vs streams" section:
```python
# On any process handling an inbound message meant for the whole room:
await redis.publish(f"room:{room_id}", json.dumps(message))

# One background subscriber task per process, started at app startup:
async def forward_room_messages():
    pubsub = redis.pubsub()
    await pubsub.subscribe("room:*")  # or a pattern subscribe, per project's fan-out shape
    async for msg in pubsub.listen():
        room_id = ...  # parse from msg["channel"]
        await manager.broadcast_local(room_id, msg["data"])
```
Redis pub/sub is **at-most-once, fire-and-forget** (per `redis.md`) — a message published while a given process's subscriber is briefly reconnecting is lost for that process's clients. This is normally acceptable for a live/ephemeral broadcast (a chat message, a live cursor position); it is **not** acceptable as the durable record of anything that must not be lost — persist that separately (a DB write, or Redis Streams per `redis.md`'s "Pub/sub vs streams") and treat the pub/sub publish as the live-fan-out notification, not the source of truth.

## Server-Sent Events (SSE) as the one-way alternative
When the channel only needs **server → client** push (a live status update, a progress stream) and never client → server messages over the same connection, SSE (`StreamingResponse` with `media_type="text/event-stream"`) is simpler than a WebSocket: plain HTTP (works through more proxies/load balancers unmodified), auto-reconnects natively in `EventSource`, and needs no upgrade handshake to authenticate — it's an ordinary authenticated GET, so the existing bearer/cookie auth dependency applies unchanged, no query-param-token workaround needed. The kit has no dedicated SSE convention doc today; follow FastAPI's own `StreamingResponse` pattern (an async generator yielding `data: ...\n\n` frames) and the same "no blocking work in the handler" and "version every message" discipline this recipe's WebSocket steps already establish. Choose WebSocket over SSE only when the client genuinely needs to send messages back over the same live connection.

## What the kit does not provide (Django Channels)
The kit's Django track has **no WebSocket/Channels coverage at all** — no `references/backend/` doc, no vendored ASGI/Channels configuration, no compatibility-matrix row for `channels`/`channels-redis`/`daphne` (confirmed by inspection at this recipe's `last-verified` date — grep the block and `references/backend/django.md`/`drf.md` before trusting this claim to have not gone stale). A Django project that needs real-time push has to add Django Channels itself: an ASGI server (Daphne/Uvicorn-with-Channels), the `channels` package, a channel layer (typically `channels-redis`, which reuses the same Redis instance this recipe's FastAPI fan-out step reaches for), and its own consumer classes — none of it wired by this kit today. Don't cite a Channels component/path from this kit; there isn't one. If the project's realtime need is on the Django track, either add Channels as a project-level decision (out of scope for this recipe) or run the real-time surface as a small dedicated FastAPI service alongside the Django app, reusing this recipe's FastAPI wire-up as-is.

## Doc fragment
The portable fragment this recipe contributes to the project's root README when applied:

```markdown
### Real-time (WebSockets)
- **Setup:** `/ws/*` endpoints authenticate at the handshake — bearer-mode clients pass a short-TTL access token as a query param, resolved through the same `AuthService.resolve_access()` every HTTP route uses; cookie-mode web clients rely on the browser's automatic same-origin cookie attachment. Connections are tracked in a per-process manager; once the deployment runs more than one process, messages fan out via Redis pub/sub (each process subscribes and forwards to its own local sockets) — a single-process deployment doesn't need this. See `references/backend/websockets.md`.
- **Secrets:** none new — reuses the existing `JWT_SIGNING_KEY`-backed access token and the project's existing Redis instance (no new credential).
- **Maintenance:** Redis pub/sub is fire-and-forget — don't treat a broadcast as the durable record of anything that must not be lost; persist that separately. The kit has no Django Channels wiring; a Django-track real-time need is a project addition, not a kit-provided recipe path.
```

---
<!--
Recipe authored via the `recipe-author` skill (Stage 11, #34, batch 2).
Wires references/backend/websockets.md's FastAPI server-side pattern plus
the existing auth component's AuthService.resolve_access for handshake
auth, and references/backend/redis.md's pub/sub section for cross-process fan-out
(tying back to background-jobs' Redis). The kit's Django track has no
WebSocket/Channels coverage — flagged explicitly as a gap, not cited as an
existing component, matching batch 1's honesty standard.
-->
