<!--
block: backend/django
needs:
  - DATABASE_URL (required); SECRET_KEY (required, no default); DEBUG/ALLOWED_HOSTS (optional, secure defaults) — see "Composition contract" + docs/fragment.md
  - CORS_ALLOWED_ORIGINS / RATE_LIMIT_* / JWT_SIGNING_KEY (all optional, secure defaults) — see "Security composition" + docs/fragment.md
  - port: 8000 (uvicorn/gunicorn default, matching backend/fastapi)
  - Python 3.13.x + uv (no committed uv.lock — see pyproject.toml)
exposes:
  - the Item model + the DRF contract-emission layer (routes, serializers, ErrorEnvelope exception handler, Page paginator) — see "Conformance"
  - core/security/ — the vendored security-composition MIDDLEWARE stack — see "Security composition"
  - /api/schema — drf-spectacular's OpenAPI schema, wire-surface-proven vs. packages/api-client/openapi.json — see "Step 4" below
  - its co-located doc fragment: docs/fragment.md
versions-pinned-to: references/compatibility-matrix.md
last-verified: 2026-07-23
provenance: manual
-->

# backend/django

The Django + DRF backend block: Django 5.2 LTS + Django REST Framework +
Postgres via psycopg, built as an **ALTERNATIVE** to `backend/fastapi` in the
same `apps/api/` materialization slot (a project picks one backend track,
not both). Lives at `templates/backend/django/` in this repo; scaffolding
materializes it into a project's `apps/api/`, exactly like the FastAPI block
does. Stage 4 (#27, epic #22) in four steps: Step 1 shipped the project
skeleton, env-driven settings, the `Item` contract-exemplar model + initial
migration, and the two vendored contract sources (`error-envelope`,
`pagination`); Step 2 was DRF contract-EMISSION — the routes/serializers,
the custom `EXCEPTION_HANDLER`, and the pagination class that actually
render those vendored contracts over HTTP, reproducing `backend/fastapi`'s
wire shape (see "Conformance" below); Step 3 was security composition —
vendoring and wiring the six baseline `templates/components/security/`
catalog components into this track's MIDDLEWARE stack, fulfilling the
Step 1/2 transport-security-headers deferral (see "Security composition"
below); **Step 4 (this state) is the OpenAPI schema + the wire-surface
CONFORMANCE PROOF** — drf-spectacular wired with best-effort
operationId/component-name parity, PLUS the dev Dockerfile/compose and a
real-PostgreSQL-16 verification pass — see "Step 4: OpenAPI schema +
wire-surface conformance proof" below.

## Contents
- Composition contract
- Vendored contract sources
- App layout
- The Item model
- Conformance (Step 1 vs. Step 2)
  - Step 4: OpenAPI schema + wire-surface conformance proof (#27)
- Sessions (the default auth path)
- Security
  - Security composition (Stage 4 Step 3, #27)
- Database & migrations
- Dev run (Docker)
- Testing
- Judgment calls

## Composition contract

**NEEDS**
- **A Postgres database**, reached via `DATABASE_URL` (`postgres://...` /
  `postgresql://...`, parsed by `dj-database-url` in `config/settings.py`).
  Required, no default — a missing `DATABASE_URL` raises at settings-module
  import time (`config/settings.py`'s own guard), not on the first request
  that touches the database. Hermetic checks/tests that need no real server
  use `config/settings_test.py` instead (`DJANGO_SETTINGS_MODULE=
  config.settings_test`), which overrides `DATABASES` to Django's stdlib
  sqlite3 backend — Django's equivalent of backend/fastapi's aiosqlite
  hermetic-test posture, no extra driver needed.
- **`SECRET_KEY`** — required, no default, **never hardcoded** (per this
  step's own instructions). Read directly from `os.environ["SECRET_KEY"]`;
  a missing value raises `RuntimeError` at settings-module import with a
  pointer to generate one.
- **`DEBUG`** (default `false` — the safe default, not Django's own
  `startproject` scaffold, which defaults `True`) and **`ALLOWED_HOSTS`**
  (default `[]`, comma-separated env var) — both env-driven, no code change
  needed per environment.
- **Port 8000** — matching `backend/fastapi`'s own default, so a project
  swapping tracks doesn't also have to change its port assumption.
- **Python 3.13.x, uv-managed** — `uv sync --all-groups` installs the
  pinned deps from `pyproject.toml`; no `uv.lock` is committed (see
  "Judgment calls").

**EXPOSES** (as of this step)
- The `Item` model (`core/models.py`) and its initial migration
  (`core/migrations/0001_initial.py`), verified against real PostgreSQL —
  see "Database & migrations".
- `core/contract/` — the vendored `ErrorEnvelope`/`AppError` hierarchy and
  `PageParams`/`Page` shapes THE contract every future route in this block
  conforms to (see "Vendored contract sources").
- No HTTP routes yet: `config/urls.py`'s `urlpatterns` is empty, with a
  `TODO` marking Step 2's DRF router seam. No OpenAPI schema is served yet
  either, even though `drf-spectacular` is installed and in
  `INSTALLED_APPS` — no `SPECTACULAR_SETTINGS`/schema view is wired.
- Its co-located doc fragment: `docs/fragment.md`.

## Vendored contract sources

`core/contract/` contains **byte-copies** (plus a short header note) of the
two locked catalog components this block's later steps reproduce the wire
shape of — the same "THE contract" sources `backend/fastapi` vendors, minus
the FastAPI/SQLAlchemy-specific parts neither applies to Django:

| Vendored into | Sourced from | Component README |
| --- | --- | --- |
| `core/contract/errors.py` | `error-envelope/errors.py` | `templates/components/backend/error-envelope/README.md` |
| `core/contract/pagination.py` | `pagination/schema.py` (PageParams+Page only — `PageResult` dropped, see file header) | `templates/components/backend/pagination/README.md` |
| `core/contract/secret_store.py` | `secrets-loading/secret_store.py` | `templates/components/security/secrets-loading/README.md` |

`errors.py` and `secret_store.py` are **byte-identical** below their header
(both are framework-neutral with no cross-imports to adapt — see each
file's own header). `pagination.py` is **not** byte-identical: it drops
`PageResult`, the SQLAlchemy-repository-only internal container that
`schema.py`'s own module docstring says "a Django project (Stage 4) copies
schema.py alone — in practice, only its `PageParams`/`Page` shapes matter
there; `PageResult` is SQLAlchemy-repository plumbing a Django view has no
use for" — see `pagination.py`'s own `DRIFT:` header line for the exact
diff.

`core/contract/__init__.py` is **new glue, not vendored** — a package seam
re-exporting each vendored file's public names, the same pattern
`backend/fastapi`'s `app/core/db/__init__.py` uses for its own vendored
subpackage.

**Kept in sync via the weekly freshness audit** (Stage 12, #35): each
vendored file's header note names its source path; the audit diffs the
vendored copy (below the header) against the current source and flags
drift. Don't hand-edit a vendored file's logic directly — edit the source
component, then re-sync the copy (re-applying the `pagination.py` trim by
hand, since that file is a deliberate subset, not a straight copy).

## App layout

```
manage.py                # Django's management entrypoint
pyproject.toml            # pinned deps — see references/compatibility-matrix.md's "Backend — Django track" + the new psycopg row
config/
  settings.py              # env-driven Settings: SECRET_KEY/DEBUG/ALLOWED_HOSTS/DATABASE_URL, REST_FRAMEWORK (EXCEPTION_HANDLER + DEFAULT_PAGINATION_CLASS wired)
  settings_test.py          # hermetic sqlite override for `manage.py check`/pytest — no real DB server needed
  urls.py                    # root URLconf — delegates to core.urls
  asgi.py                     # ASGI entrypoint (uvicorn)
  wsgi.py                      # WSGI entrypoint (gunicorn)
core/                    # the Django app (INSTALLED_APPS label: "core")
  models.py                # Item model + ItemManager/ItemQuerySet (soft-delete scoping)
  serializers.py             # ItemOut/ItemCreate/ItemUpdate + health/auth-stub serializers
  views.py                    # ItemViewSet + health/readyz + auth-stub views
  urls.py                      # DRF router (trailing_slash=False) + explicit health/readyz/auth paths
  exceptions.py                 # custom EXCEPTION_HANDLER -> ErrorEnvelope
  pagination.py                  # custom PageNumberPagination -> {items,total,page,size,pages}
  apps.py                   # CoreConfig
  migrations/
    0001_initial.py           # hand-verified-generated initial migration (see "Database & migrations")
  contract/                 # vendored contract sources — see "Vendored contract sources"
    errors.py
    pagination.py
    secret_store.py           # ALSO the composition target for Stage 4 Step 3's Secrets seam — see "Security composition"
  security/                 # Stage 4 Step 3 (#27) — vendored security-composition components, self-contained subpackages
    security_headers/           # _core.py + django.py — MIDDLEWARE, sets HSTS/nosniff/frame-options/referrer-policy/CSP/Permissions-Policy
    cors_lockdown/               # _core.py + django.py — settings emitter for django-cors-headers' CorsMiddleware
    rate_limiting/                 # _core.py + django.py — MIDDLEWARE, token-bucket 429s with Retry-After
    audit_logging/                   # audit.py (vendored) + middleware.py (NEW glue) — request-id bind + structured JSON audit logging
    input_validation/                 # validation.py (vendored, unused as of this step) — StrictModel for a future shared/service layer, NOT the DRF request boundary
tests/                   # conformance-proof + security-composition-proof suite — see "Testing"
docs/
  fragment.md              # this block's machine-parseable doc fragment (see documentation-standard.md)
```

## The Item model

`core/models.py`'s `Item` matches `backend/fastapi`'s `app/models/item.py`
field-for-field: `id` (UUID, `default=uuid.uuid4`, primary key),
`created_at` (`auto_now_add`), `updated_at` (`auto_now`), `deleted_at`
(nullable, `NULL` = not deleted), `name`/`description`. It is the Django-ORM
counterpart to `templates/components/backend/db-mixins/mixins.py`'s
`UUIDPrimaryKey`/`TimestampMixin`/`SoftDeleteMixin` — that component's own
module docstring says explicitly: "a Django backend (Stage 4) does NOT
reuse this file; it reaches for Django's own `models.UUIDField`,
`auto_now_add`/`auto_now`, and a custom soft-delete manager instead." This
model is that reach — not a vendored file, this step's own app code.

**Soft-delete scoping**: `Item.objects` (the default manager) is
`ItemManager`, built on `ItemQuerySet.not_deleted()` — every default lookup
(`Item.objects.all()`, `.get()`, `.filter()`) already excludes rows with a
non-null `deleted_at`, mirroring `SoftDeleteMixin.not_deleted()`'s `WHERE`
fragment for the SQLAlchemy track. `Item.all_objects` (Django's plain,
unscoped `models.Manager()`) and `Item.objects.with_deleted()` are the
escape hatches for the rare caller that needs soft-deleted rows too.

**Partial index**: `Meta.indexes` declares a partial B-tree index on
`deleted_at` where `deleted_at IS NULL`
(`models.Index(fields=["deleted_at"], condition=Q(deleted_at__isnull=True),
name="items_not_deleted_idx")`) — verified to compile to exactly that on
real PostgreSQL (see "Database & migrations"): `CREATE INDEX
items_not_deleted_idx ON public.items USING btree (deleted_at) WHERE
(deleted_at IS NULL)`. Speeds the default manager's `deleted_at IS NULL`
filter on every unscoped lookup without indexing the much rarer
soft-deleted rows. (Django's partial-index `condition=` is Postgres/
sqlite/Oracle-only per Django's own docs; the hermetic sqlite test settings
still create *some* index there, just not a genuinely partial one on
backends that don't support the feature — harmless for this step's
purposes.)

## Conformance (Step 1 vs. Step 2, + fix round)

**Gate-1 decision (recorded in the PR's decision log):** the client-
interchangeability guarantee this Django track works toward is **wire-
contract identity** — byte-identical paths, methods, status codes, and JSON
response bodies to what `backend/fastapi` serves for the same operation —
with best-effort OpenAPI `operationId`/component-name parity, and
Django-only client regeneration documented as an accepted, expected step
(swapping tracks means re-running the client generator against this app's
`/api/schema/` once a later step exposes it, not necessarily getting the
exact committed FastAPI-generated `packages/api-client` as a byte-for-byte
drop-in).

**What Step 1 delivered toward that:** the two contract SOURCES
(`core/contract/errors.py`, `core/contract/pagination.py`) that define the
target shapes, vendored verbatim/near-verbatim, plus the `Item` model — no
DRF view/router/serializer/exception-handler/pagination-class yet.

**What Step 2 (this step, #27) delivers — the DRF contract-EMISSION
layer:** `core/serializers.py` (`ItemOut`/`ItemCreate`/`ItemUpdate` +
health/auth-stub shapes, matched field-for-field to `packages/api-client/
openapi.json`), `core/views.py` (`ItemViewSet` + health/readyz + auth-stub
views, wired via `core/urls.py`/`config/urls.py` reproducing
`backend/fastapi`'s route set — `GET/POST /items`, `GET/PATCH/DELETE
/items/{item_id}`, `/health`, `/readyz`, the `/auth/*` stubs),
`core/exceptions.py` (the custom `EXCEPTION_HANDLER` mapping DRF's own
exception types — and anything unhandled — onto `ErrorEnvelope`, wired via
`REST_FRAMEWORK["EXCEPTION_HANDLER"]`), and `core/pagination.py` (the
custom `PageNumberPagination` subclass emitting `{items, total, page, size,
pages}` instead of DRF's own default `{count, next, previous, results}`
shape, wired via `REST_FRAMEWORK["DEFAULT_PAGINATION_CLASS"]`). The
conformance-proof test suite (`tests/test_conformance_errors.py`,
`tests/test_conformance_pagination.py`) asserts these shapes byte-equal
against `core/contract/`'s vendored Pydantic models for the same inputs —
not just "some 4xx"/"some paginated list".

**Fix round (this state):** an internal review of Step 2 found several real
bugs where this layer broke wire-contract identity rather than merely
diverging from it — see each fix's own commit/docstring for detail:
- A malformed (non-UUID) `item_id` used to raise `django.core.exceptions.
  ValidationError` inside `ItemViewSet.get_object()`'s ORM lookup, which
  escaped the narrower `except (DoesNotExist, ValueError, TypeError)` and
  reached the client as a bare, un-enveloped 500. Fixed: that exception type
  is now caught too (`core/views.py`) — the documented 404 `NotFoundError`
  envelope, verified via a real `GET /items/not-a-uuid` request.
- Every DRF `APIException` subclass without its own explicit branch
  (`AuthenticationFailed`, `ParseError`/malformed JSON, `MethodNotAllowed`,
  `UnsupportedMediaType`, ...) used to fall through `core/exceptions.py`'s
  catch-all straight to a bare 500 with a logged traceback — including for
  ordinary client mistakes, not just genuine bugs. Fixed: a new branch maps
  every remaining `APIException` from its own real `status_code` onto the
  best-fit `ErrorCode`, keeps that real status code (never fakes it to
  500), and only logs at `exception` level for a genuine 5xx — see
  `core/exceptions.py`'s own module docstring for the full mapping table.
  Verified via real requests: malformed JSON → 400 `validation_failed`
  (not 500), `PUT /items/{id}` → 405 `validation_failed` (not 500), bad
  Basic-auth credentials → 401 `unauthenticated` (not 500), a genuine
  `RuntimeError` → still 500 `internal_error` with no `str(exc)` leak.
- DRF's default `DEFAULT_AUTHENTICATION_CLASSES`
  (`SessionAuthentication`+`BasicAuthentication`) was left unset, so every
  view without its own explicit `authentication_classes` override —
  `ItemViewSet` included — silently accepted HTTP Basic credentials against
  Django's `auth_user` table, an auth surface nothing in this block
  populates or intends before Stage 5 (#28). Fixed: `config/settings.py`
  now sets `"DEFAULT_AUTHENTICATION_CLASSES": []` kit-wide; the health/
  readyz/auth-stub views' own explicit `authentication_classes = []`
  (`core/views.py`) is unchanged and now consistent with, rather than the
  only thing closing, that surface.
- `page`/`size` bounds (`page=0`/negative, `size=0`, `size>200`) used to be
  silently accepted or clamped by DRF's own `PageNumberPagination` rather
  than rejected, and a `page` past the last one 404'd ("Invalid page")
  instead of returning an empty page — both real divergences from
  FastAPI's `Depends(PageParams)` behavior. Fixed, turning both into real
  conformance rather than accepted gaps: `core/pagination.py`'s
  `paginate_queryset` now validates `page`/`size` through the SAME
  vendored `PageParams` FastAPI uses (out-of-bounds → 422
  `validation_failed`, verified for `page=0`, `page=-1`, `size=0`,
  `size=201`, `size=500`), and tolerates a past-the-end `page` as `items:
  []` at 200 (verified) rather than DRF's own 404.
- `ModelViewSet` + a router used to also expose `PUT /items/{item_id}`
  (full replace, forced to partial semantics), which `openapi.json` does
  not define at all. Fixed: `ItemViewSet.http_method_names` (`core/
  views.py`) now excludes `"put"` outright, so `PUT` 405s via the
  `APIException` fix above instead of being silently accepted — verified
  a `PUT` now returns 405 and `PATCH` still works.

**Accepted, documented per-framework divergences** (none forced — see each
source's own docstring for the full rationale; the guarantee is
wire-identity on the DOCUMENTED operations and their success/documented-
error responses, not on framework-level plumbing a well-behaved generated
client never triggers):
- `PageParams`'s `extra="forbid"` (an unrecognized query param is a hard
  422 on FastAPI) has no DRF equivalent wired here — an unrelated unknown
  query param is silently ignored (`core/pagination.py`).
- `ItemViewSet.get_object()` treats a malformed (non-UUID) `item_id` path
  segment as 404 (`core/views.py`) where FastAPI's path-typed `item_id:
  uuid.UUID` rejects it at 422 before the handler runs — a routing-level
  type-coercion difference, not a contract-shape one (the underlying 500
  BUG this used to also trigger is the fix above; the 404-vs-422 choice
  itself remains an accepted, deliberate divergence).
- **Framework-level negotiation/parse errors may differ in exact status
  between the two tracks.** A malformed JSON body is DRF's own `ParseError`
  (`status_code = 400`) here vs. FastAPI's 422 for the equivalent case; a
  disallowed method (`PUT`) is 405 on both, but DRF's `UnsupportedMediaType`
  (415) has no FastAPI-side equivalent pinned. None of these are documented
  operations or documented error responses in `packages/api-client/
  openapi.json` — a well-behaved client generated from that frozen schema
  never sends a request that triggers them. The wire-identity guarantee
  this block targets (see "Gate-1 decision" above) covers the DOCUMENTED
  surface; these framework-negotiation edges are honestly out of scope for
  byte-identical status matching, though both sides still return a real,
  enveloped `ErrorEnvelope` (never a bare 500) for every one of them.
- **429 (rate limit exceeded) is the one non-enveloped error shape in this
  block** — `core/security/rate_limiting/django.py`'s `RateLimitMiddleware`
  returns a plain `{"detail": "rate limit exceeded"}` body (never
  `ErrorEnvelope`), same as every other vendored security-composition
  component that short-circuits outside DRF's own exception-handling path
  (it runs as MIDDLEWARE, before `EXCEPTION_HANDLER` ever sees the
  request). Cross-track consistent, not a Django-only gap:
  `backend/fastapi`'s equivalent `RateLimitMiddleware`
  (`app/core/security/rate_limiting/fastapi.py`) returns the identical
  `{"detail": ...}` shape for the same reason — a rate-limit denial is
  deliberately NOT wrapped in `ErrorCode`/`ErrorEnvelope` on either track,
  since `ErrorCode` (a locked, versioned enum) has no rate-limit member and
  adding one is exactly the kind of contract change the vendored
  `core/contract/errors.py`'s own docstring says needs the same
  coordination as any other wire-shape edit.
- **The 422 `details[]` array's exact content is NOT byte-identical**
  between the two tracks, even though the envelope's outer shape (`code`,
  `status`, `message`) is. `field` differs (DRF's `ValidationError.detail`
  keys are the bare field name, e.g. `"name"`; FastAPI's
  `RequestValidationError.errors()` `loc` includes the request-part prefix,
  e.g. `"body.name"`) and `message` text differs (DRF's own validator
  messages vs. Pydantic's). A client that switches on `error.code` (as
  `core/contract/errors.py`'s own `ErrorBody` docstring already instructs —
  "a client should switch on `code`, never on `message`") is unaffected;
  one that parses `details[].field`/`.message` as a stable, cross-backend
  identifier is not supported by either track's contract.
- **List ordering**: `ItemViewSet.queryset` here is explicitly
  `.order_by("created_at", "id")` (`core/views.py`) — a deterministic,
  repeatable page-to-page order. `backend/fastapi`'s equivalent list query
  currently applies no `ORDER BY` at all. This is flagged, not fixed here
  (out of this fix round's scope, which is this Django block only): the
  FastAPI block should adopt the same deterministic order for genuine
  cross-backend list-order parity — tracked as a follow-up for Step 4 /
  the whole-PR review, not addressed in this commit.

### Step 4: OpenAPI schema + wire-surface conformance proof (#27)

**GATE-1, restated precisely** (this is what Step 4 exists to PROVE, not
just assert): WIRE-CONTRACT IDENTITY — byte-identical paths, methods,
response statuses, and request/response JSON shapes — between this Django
block and the frozen `packages/api-client/openapi.json` contract, ON THE
DOCUMENTED OPERATIONS (the ones above's "Accepted, documented
per-framework divergences" already carves out real, narrow exceptions to).
Best-effort (not guaranteed) parity on OpenAPI operationIds and
component names. Documented, not automated: how a Django-only project
regenerates ITS OWN client from THIS block's schema, not a promise that
`packages/api-client`'s already-committed, FastAPI-generated client is a
drop-in for a Django-backed project.

**drf-spectacular is wired**: `REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"]` +
`SPECTACULAR_SETTINGS` (`config/settings.py`), `/api/schema`
(`config/urls.py`'s `SpectacularAPIView` — additive, not part of the
frozen route set itself), and every view in `core/views.py` carries an
explicit `@extend_schema`/`@extend_schema_view` declaration. Every
`operation_id` is set to the EXACT string `openapi.json` already uses for
that operation (FastAPI auto-derives operationIds from
`{handler}_{path}_{method}`; drf-spectacular has no equivalent
auto-derivation that lands on the same string, so this block sets them by
hand) — this is FULL, not best-effort, operationId parity, verified by
`tests/test_schema_conformance.py::test_operation_id_and_component_name_parity_report`
(fails if any operationId ever regresses). Component-NAME parity is
best-effort and only partial — see the table below.

**`core/pagination.py`'s `ContractPageNumberPagination.get_paginated_response_schema`**
(drf-spectacular's own documented override point, the same one DRF's base
`PageNumberPagination` uses) renders the `{items, total, page, size,
pages}` envelope inline rather than as a named component — a real,
reported component-naming divergence from `openapi.json`'s named
`Page[ItemOut]`, but structurally identical once dereferenced (see the
conformance proof below).

**`core/serializers.py`'s `ErrorDetailSerializer`/`ErrorBodySerializer`/
`ErrorEnvelopeSerializer`** are DOCUMENTATION-ONLY — `core/exceptions.py`
still builds every actual error response straight from the vendored
Pydantic `core.contract.errors.ErrorEnvelope`, never these DRF
serializers. They exist purely so `@extend_schema(responses={...:
ErrorEnvelopeSerializer})` has a DRF serializer to point drf-spectacular
at (it documents DRF serializers, not arbitrary Pydantic models) —
field-for-field hand-kept copies of the vendored contract; a drift here
only ever affects the DOCUMENTED schema, never the actual wire response,
which is exactly what the conformance proof below is there to catch.

**THE conformance proof: `tests/test_schema_conformance.py`.** Loads BOTH
schemas — this block's own (generated in-process by drf-spectacular's
`SchemaGenerator`, the same code path `manage.py spectacular
--format openapi-json --file <path>` runs; verified separately by hand,
0 warnings, no live database touched either way — schema generation only
introspects views/serializers, never queries) and the committed frozen
`openapi.json` — normalizes each operation's request/response JSON Schema
(fully dereferenced, with a narrow, individually-justified set of
normalizations: OpenAPI 3.0-vs-3.1 nullable representation collapsed to
one form; cosmetic keys — `title`/`description`/`example`/`default` —
stripped; `additionalProperties`/`readOnly`/`writeOnly` stripped as
validation-strictness annotations, not shape facts; a response body's
`required` array stripped, since pydantic ties it to constructor-default
presence — a REQUEST concept — while both backends' actual HTTP responses
always include every documented key regardless, verified directly by
`test_conformance_errors.py`/`test_items.py`; see the test module's own
docstrings for the full reasoning on each), then asserts the two wire
surfaces — the set of `(path, method, response-status, request-body-shape,
response-body-shape)` — are EQUAL.

**RESULT: wire surfaces are EQUAL** on every one of the 7 documented
routes (14 operations), with exactly ONE further, individually-documented
exception (`_KNOWN_DIVERGENCES` in that test file, not a blanket
normalization): `PATCH /items/{item_id}`'s request body. The frozen
contract's `ItemUpdate.name` (`backend/fastapi/app/schemas/item.py`) is
declared schema-NULLABLE (`str | None = Field(default=None, ...)`) with NO
guard in `update_item` (`backend/fastapi/app/api/routers/items.py`)
against an explicitly-null value before `repo.update(obj, name=None)` —
since `Item.name` is a NOT-NULL column on BOTH tracks, that request would
reach the database and raise an unhandled 500 on the FastAPI side too.
**This is a discovered gap in the frozen contract's own reference
implementation, not a Django-side shortfall** — mirroring the nullable
declaration into `core/serializers.py`'s `ItemUpdateSerializer.name`
would import the identical crash risk into this block for the sake of a
closer schema match. Django's actual behavior (explicit `null` is REJECTED
with a clean 422 `validation_failed`, never reaches the DB) is the safer
posture and is kept as-is. **Flagged as a FastAPI-side follow-up in this
PR's decision log** (Stage 12/hardening candidate, out of this
Django-only step's scope): either make `ItemUpdate.name` genuinely
non-nullable, or guard against an explicit null before the repository
call.

**Best-effort component-name parity — what matched vs. differs:**

| Match? | Django component | Frozen contract component | Note |
|---|---|---|---|
| ✅ | `ErrorBody` | `ErrorBody` | exact |
| ✅ | `ErrorDetail` | `ErrorDetail` | exact |
| ✅ | `ErrorEnvelope` | `ErrorEnvelope` | exact |
| ✅ | `HealthStatus` | `HealthStatus` | exact |
| ✅ | `ItemCreate` | `ItemCreate` | exact |
| ✅ | `ItemOut` | `ItemOut` | exact |
| ✅ | `LoginRequest` | `LoginRequest` | exact |
| ✅ | `PrincipalOut` | `PrincipalOut` | exact |
| ✅ | `ReadinessStatus` | `ReadinessStatus` | exact |
| ✅ | `RefreshRequest` | `RefreshRequest` | exact |
| ✅ | `TokenResponse` | `TokenResponse` | exact |
| ❌ | `CodeEnum` | `ErrorCode` | drf-spectacular auto-names extracted enums from a hash of their choices, not the Python `StrEnum` class name; structurally identical (same 7 members) |
| ❌ | `PaginatedItemOutList` | `Page_ItemOut_` | drf-spectacular's own pagination-schema naming convention vs. FastAPI's `Page[ItemOut]`-derived name; structurally identical once dereferenced (see the proof above) |
| ❌ | `PatchedItemUpdate` | `ItemUpdate` | drf-spectacular auto-generates a separate `Patched<X>` variant for PATCH (DRF's `partial=True` convention has no FastAPI-side equivalent — `ItemUpdate` is already all-optional, so FastAPI reuses it directly for PATCH); structurally identical except the ONE documented `name`-nullability divergence above |

11 of 14 components match exactly; the 3 that don't are drf-spectacular's
own naming conventions (enum-hash naming, pagination-envelope naming,
partial-update variant naming) with no `ENUM_NAME_OVERRIDES`/custom
naming hook applied to force them — a real gap a future step could close,
left as best-effort per this step's own instructions rather than fought
to zero.

**Regenerating a Django-only project's own client** (this block's schema,
not the frozen FastAPI one committed at `packages/api-client/
openapi.json` — that file is FastAPI's export and is NOT overwritten by
this step):

```sh
manage.py spectacular --format openapi-json --file packages/api-client/openapi.json
just client-generate
```

**`just client-generate`'s re-export step currently targets the FastAPI
app specifically** (the monorepo justfile's recipe re-runs FastAPI's own
`app/main.py`-based export before calling `orval`) — a Django-only project
(no `apps/api` FastAPI process to import) swaps that one command for the
`manage.py spectacular` line above; the rest of the pipeline (`orval`
generating the TypeScript client from whichever `openapi.json` is on
disk) is backend-agnostic already. Making that swap automatic — detecting
which backend track a materialized project actually has and calling the
right export command — is flagged as a Stage 12 item (backend-agnostic
client regeneration), not built here.

### Conformance / Auth (Stage 5b, #44)

**All five `/auth/*` operations now carry real behavior and are in the
STRICT wire-surface comparison** — `tests/test_schema_conformance.py`'s
`_PENDING_PARITY_OPS` (Stage 5a → Stage 5b's own tracking constant,
carried over unchanged from the Step 4 section above) is now the empty
set, kept declared (not deleted) as the documented seam a future
in-flight-parity effort would reuse the same way. `test_wire_surface_is_
identical_to_the_frozen_contract` passes with `POST /auth/register`, `POST
/auth/login`, `POST /auth/refresh`, `POST /auth/logout`, and `GET
/auth/me` all fully compared against `openapi.json` — **zero new
`_KNOWN_DIVERGENCES` entries were needed**: every documented status code
and request/response body shape matches byte-for-byte after the same
honest normalization Step 4 already established (nullable-representation
collapse, cosmetic-key stripping, validation-strictness-key stripping —
see that section above; no auth-specific normalizer change was made).
`test_operation_id_and_component_name_parity_report` confirms
`operationId` parity is now FULL across all 12 documented operations
(the 7 from Step 4 plus these 5), not just best-effort — `RegisterRequest`
joins the "component names in both, exact match" list too (11 shared
component names became 12).

**Real handlers, thin wire-shape mapping only.** `core/views.py`'s
`RegisterView`/`LoginView`/`RefreshView`/`LogoutView`/`MeView` each:
validate the request body via the matching `core/serializers.py`
serializer (`RegisterRequestSerializer`/`LoginRequestSerializer`/
`RefreshRequestSerializer`), build a fresh `AuthService` via
`core.security.auth.stores.build_auth_service()`, and bridge into that
service's `async def` methods with `asgiref.sync.async_to_sync(...)` —
every view stays an ordinary SYNC DRF `APIView` method, matching this
block's locked "DRF dispatch is sync, the auth core is async, bridge at
the call site" decision (`core/security/auth/stores.py`'s own module
docstring). `MeView` enforces authentication itself via
`core.security.auth.django.resolve_principal` (never DRF's own
`IsAuthenticated`, which would 403 instead of 401 on a missing bearer) —
`permission_classes = [AllowAny]`/`authentication_classes = []` stay as
they were even for this real handler, deliberately.

**The `AuthError` → `ErrorEnvelope` mapping (`core/exceptions.py`)**
reproduces `backend/fastapi`'s Stage 5a FIX B exactly: an
`isinstance(exc, AuthError)` branch, placed before the generic
`APIException`/catch-all branches, looks up `core.security.auth.django.
AUTH_ERROR_HTTP` and emits a SINGLE fixed `"Authentication failed."`
message for the entire `unauthenticated` (401) bucket regardless of
`str(exc)` — so `TokenReused` (whose own message names exactly what
happened) is byte-indistinguishable, at the wire, from any other
invalid-refresh-token failure. `conflict` (409)/`permission_denied` (403)
keep echoing `str(exc)`. An unmapped `AuthError` subclass fails safe-
closed to 401. `AuthNotConfiguredError` (a plain `RuntimeError`, an unset
`JWT_SIGNING_KEY`) is confirmed NOT caught by this branch — it falls
through to the generic catch-all, rendering `internal_error` at 500, never
a misleading `unauthenticated`/401. `tests/test_exceptions.py` unit-tests
this branch directly (every concrete `AuthError` subclass, including
`InsufficientRole`, which has no dedicated route to exercise it via HTTP
at all).

**`tests/test_auth.py`** (18 tests, `@pytest.mark.django_db
(transaction=True)` throughout — required for the same two reasons
`tests/test_auth_stores.py`'s own docstring documents: genuine autocommit
durability and avoiding async-ORM-under-rolled-back-`atomic()` flakiness)
ports every scenario `backend/fastapi`'s own `tests/test_auth.py`
exercises, over `rest_framework.test.APIClient` against the real routes:
register → login → me; email normalization; duplicate register → 409;
bad login → 401; refresh rotation; refresh-token reuse detected AND the
WHOLE token family revoked (including the still-live rotated tip, not
just the reused row); reuse vs. an ordinary invalid token byte-identical
on the wire; soft-deleted user denied login/refresh; logout → 204 then
refresh → 401; idempotent logout; `/auth/me` unauthenticated/garbage/
refresh-token-as-bearer → 401; `JWT_SIGNING_KEY` unset (`django.test.
override_settings`) fails closed to 500, never a misleading 401;
`HTTPBearer` declared in the generated schema.

**Real PostgreSQL 16 verification** (the sandbox's available cluster —
same documented PostgreSQL-18.x-in-the-compatibility-matrix gap the
"Database & migrations" section below already carries): see that
section's own "Stage 5b" subsection for the full transcript — this is the
`0002_user_refreshtoken` migration's first real-database run (Stage 5b's
own migration-authoring commit only verified it against hermetic sqlite;
Step 4's real-Postgres pass above predates the `users`/`refresh_tokens`
tables existing at all).

### Conformance / Account lifecycle + lockout (Stage 5c, #45)

**All three new `/auth/*` operations carry real behavior and are in the
STRICT wire-surface comparison** — `tests/test_schema_conformance.py`'s
`_PENDING_PARITY_OPS` (carried over from the Step 4 / Stage 5b sections
above) is now the empty set again, kept declared (not deleted), same
posture as Stage 5a → Stage 5b's own transition. `test_wire_surface_is_
identical_to_the_frozen_contract` passes with `POST /auth/verify-email`,
`POST /auth/request-password-reset`, and `POST /auth/reset-password` all
fully compared against `openapi.json` — **zero new `_KNOWN_DIVERGENCES`
entries were needed**. `test_operation_id_and_component_name_parity_
report` confirms `operationId` parity stays FULL across all 15 documented
operations now (12 from Stage 5b plus these 3); `VerifyEmailRequest`/
`RequestPasswordResetRequest`/`ResetPasswordRequest` join the "component
names in both, exact match" list too.

One documented wire-generation subtlety, not a divergence: `POST /auth/
request-password-reset`'s 202 response is declared `responses={202:
OpenApiTypes.ANY, ...}` (`core/views.py`), not a raw `{}` dict or `None`
— `packages/api-client/openapi.json`'s own 202 for this operation
documents an `application/json` media type with an empty (`{}`,
"unspecified shape") schema (FastAPI's own default for a route with no
`response_model`), and drf-spectacular's `AutoSchema._get_response_for_
code` falsy-checks a raw `{}` dict BEFORE reaching the branch that would
pass it through as a schema unchanged, collapsing it to "No response
body" (no `content` key at all, matching this block's OWN `204: None`
convention elsewhere) instead — `OpenApiTypes.ANY` (`build_basic_type`
→ `{}`) is what genuinely reproduces the frozen contract's shape here.

**`LoginView` is now gated on email verification, per-account lockout, and
audit events.** `core/views.py`'s new `_build_login_auth_service()`
constructs the `AuthService` `LoginView.post` uses with `lockout=
build_lockout_policy()`, `require_verification=settings.AUTH_REQUIRE_
EMAIL_VERIFICATION` (default `True`), and `events=AuditAuthEventSink()` —
mirroring `backend/fastapi`'s `app/api/deps.py:get_auth_service`'s own
Stage 5c wiring exactly. `core/security/auth/stores.py:build_auth_service()`
itself (Agent A's factory) stays UNCHANGED — every other `/auth/*` view
(`RegisterView`/`RefreshView`/`LogoutView`/`MeView`) still uses the plain,
unwired version, since `AuthService.register`/`refresh`/`logout` never
consult `lockout`/`require_verification`/`events` at all (`_core.py`).
`RegisterView.post` additionally calls `build_account_service().
request_email_verification(user)` after a successful `AuthService.
register`, wrapped in `try/except Exception` (ported M2 fix) so a mail
failure never turns the already-committed 201 into a 500 — see `core/
views.py`'s `RegisterView` docstring for the full rationale, identical to
`app/api/routers/auth.py`'s own `register` handler.

**`VerifyEmailView`/`RequestPasswordResetView`/`ResetPasswordView`** each
validate via the matching `core/serializers.py` serializer
(`VerifyEmailRequestSerializer`/`RequestPasswordResetRequestSerializer`/
`ResetPasswordRequestSerializer`), build a fresh `AccountService` via
`build_account_service()`, and bridge into its `async def` methods with
`async_to_sync(...)` — same "sync view, async service, bridge at the call
site" posture every other `/auth/*` view in this file already uses. Every
raised `InvalidSingleUseToken` is left UNCAUGHT — `core/exceptions.py`'s
existing `AuthError` branch already maps it (via `AUTH_ERROR_HTTP`) to the
same generic 401 `unauthenticated` every other auth failure uses; no
`core/exceptions.py` change was needed for this stage.

**`tests/test_auth.py`** (34 tests, up from Stage 5b's 18, `@pytest.mark.
django_db(transaction=True)` throughout) ports every scenario `backend/
fastapi`'s own `tests/test_auth.py` Stage 5c section exercises: register→
verify→login happy path (asserting the verification-email side effect
itself); login-before-verify → 401 byte-identical to a wrong password;
request-password-reset known vs. unknown email → byte-identical 202 (a
token issued ONLY for the known account) + empty-email → 422; reset happy
path (old password 401, new password 200 immediately, EVERY pre-reset
refresh token revoked — not just the one family that requested it);
verify/reset garbage/reused/expired tokens → 401 generic; lockout (5
wrong passwords locks the account, even the CORRECT password then 401s,
a completed reset lifts the lock and the new password logs in
immediately — proving `AuthService`/`AccountService` observe the same
underlying `DjangoLockoutStore` table); a raising `EmailSender` never
turning `register`/`request-password-reset` into a 500 (M1/M2, ported);
and the M2 recovery story end to end (register, never verify, blocked
login, reset, login now succeeds because `AccountService.reset_password`
also marks the email verified).

**Token-capture seam**: an `email_sender` pytest fixture monkeypatches
`core.security.auth.stores.get_email_sender` — the exact module-level
name `build_account_service(email=None)` resolves at call time — with a
tiny in-memory, SYNCHRONOUS-bodied `EmailSender` whose `send()` is
directly `await`ed by `AccountService` (unlike the real `DjangoEmailSender`,
which schedules delivery as a fire-and-forget background task — see that
class's own docstring). This guarantees the captured message is present
by the time the `async_to_sync(...)`-bridged view call returns, with no
dependence on real delivery timing and no log-string parsing — the
Django-track counterpart to `backend/fastapi`'s own `app.dependency_
overrides[get_email_sender] = ...` FastAPI-dependency-override seam.

**Real PostgreSQL 16 verification, end to end over HTTP.** The sandbox's
PostgreSQL 16 cluster was started (`pg_ctlcluster 16 main start` — it was
down when this verification began), a scratch role + same-named database
created, `manage.py migrate --no-input` run under `DJANGO_SETTINGS_
MODULE=config.settings` (the real-DB settings module) — `core.
0003_stage5c_account_lifecycle` applying cleanly on top of `0001`/`0002`
— then a full `rest_framework.test.APIClient` round-trip against the REAL
connection, using the identical `get_email_sender` monkeypatch seam
`tests/test_auth.py` uses (not a pytest fixture here, a plain module-level
reassignment in a standalone script, same underlying seam):

```
register(pg16proof@example.com)                              -> 201, verification email captured (to/subject match)
login BEFORE verify                                           -> 401 unauthenticated
verify-email(real token)                                      -> 204, empty body
verify-email(REUSED token)                                    -> 401 unauthenticated
login AFTER verify                                             -> 200, access+refresh pair
GET /auth/me  Bearer <access>                                  -> 200, email matches
request-password-reset(known email)                            -> 202, empty body
request-password-reset(unknown email)                          -> 202, empty body
  -- byte-identical to the known-email response
reset-password(real token, new password)                       -> 204, empty body
login with OLD password                                        -> 401 unauthenticated
login with NEW password                                        -> 200, IMMEDIATELY (no re-verify needed)
refresh(pre-reset refresh_token)                                -> 401 unauthenticated
  -- proves reset_password's revoke_all_for_user, not just one family
5x login with wrong password                                   -> 401 unauthenticated each
login with the (now-current) CORRECT password, while LOCKED     -> 401 unauthenticated
  -- the real password is never checked against a locked account
request-password-reset -> reset-password (lifts the lockout)    -> 202, then 204
login IMMEDIATELY after the lockout-lifting reset               -> 200, no remaining cooldown
-- direct Postgres read (bypassing the API): users.email_verified = true,
   users.verified_at IS NOT NULL, 3 single_use_tokens rows for this user
   (1 verify + 2 reset, one reset consumed twice-issued across the two
   reset flows above), login_attempts row for this account_key gone
   (cleared by the lockout-lifting reset)
```

Every line passed against the real Postgres 16 connection. Schema verified
column-for-column against `alembic/versions/0003_stage5c_account_
lifecycle.py`'s own Postgres-native result: `users.email_verified` NOT
NULL boolean (`server_default=false`), `users.verified_at` nullable
timestamptz; `single_use_tokens.token_hash` UNIQUE, `single_use_tokens.
user_id` indexed + FK to `users(id)`; `login_attempts.account_key` UNIQUE,
no FK to `users` (matching `_core.LockoutStore`'s own "account_key is
free-form" docstring) — an exact match. `manage.py spectacular --format
openapi-json` was also re-run under `config.settings` (real-DB) and
diffed byte-identical against the hermetic-`config.settings_test` export,
confirming — same as Step 4's and Stage 5b's own passes — that schema
generation genuinely never touches the database. Scratch role/database
dropped afterward; the cluster itself was stopped afterward too (it was
down when this verification pass started, unlike Stage 5b's pass, which
found it already online and left it running) — no DB artifacts, `.venv`,
or `uv.lock` committed.

### Conformance / Web cookie mode + RBAC admin example (Stage 5d, #46)

**`GET /admin/ping` is now real and in the STRICT wire-surface
comparison.** `tests/test_schema_conformance.py`'s `_PENDING_PARITY_OPS`
(added when `backend/fastapi`'s own Stage 5d extended the frozen contract
with this one new operation — see that constant's own module-level
comment history) is emptied back to the empty set again, kept declared
(not deleted), the same posture every prior stage's own transition
documents. `test_wire_surface_is_identical_to_the_frozen_contract` passes
with `admin_ping` fully compared against `openapi.json` — **zero new
`_KNOWN_DIVERGENCES` entries were needed**: the 200 (`HealthStatus`), 401,
and 403 (`ErrorEnvelope`) responses and the `security: [{"HTTPBearer":
[]}]` declaration match byte-for-byte. `test_operation_id_and_component_
name_parity_report` confirms `operationId` parity stays FULL across all 16
documented operations now (15 from Stage 5c plus this one).

**`AdminPingView` (`core/views.py`) adds no new auth logic of its own** —
`permission_classes = [has_role("admin")]` is the entire gate.
`has_role(*roles)` (new `core/security/auth/permissions.py`) is a factory
returning a `BasePermission` subclass whose `has_permission` builds an
`AuthService` via the existing `build_auth_service()` and bridges into the
vendored, DRF-free `require_roles(request, auth_service, *roles)`
(`core/security/auth/django.py`) with `asgiref.sync.async_to_sync(...)` —
the SAME sync-view/async-service bridge every other view in this file
already uses. `InvalidToken` (→ 401) and `InsufficientRole` (→ 403)
propagate uncaught to `core/exceptions.py`'s existing `AuthError` branch;
`has_permission` itself never returns `False`, so DRF's own un-enveloped
403 path is never reached — see that file's own module docstring for the
full rationale, including WHY this DRF-specific permission class lives in
its own app-code file rather than the vendored, `rest_framework`-free
`django.py` (that file's own docstring already says a DRF permission class
belongs in app code, not there).

**Admin provisioning**: `core.security.auth.stores.seed_admin(email,
password)` — mirroring `app/core/security/auth/stores.py`'s identically-
named FastAPI function — is the ONE place this app ever constructs a user
with `roles=["admin"]` (via `DjangoUserStore.create`); `POST /auth/
register` has no `roles` field on the wire and cannot self-grant it. New
`manage.py seed_admin <email> [--password ...]` management command wraps
it for operator use (interactive `getpass` prompt if `--password` is
omitted; a friendly `CommandError` — not a raw traceback — on a duplicate
email, caught via the DB-level unique constraint the same way
`AuthService.register`'s own race-condition fallback already relies on).

**Web cookie mode**: `LoginView`/`RefreshView`/`LogoutView` (`core/
views.py`) gain cookie-mode behavior mirroring `app/api/routers/auth.py`
handler-for-handler, entirely over the already-vendored `core/security/
auth/django.py` glue (`set_auth_cookies`/`clear_auth_cookies`/
`read_refresh_cookie`/`enforce_csrf`) — login reads `request.headers.get(
"X-Auth-Mode") == "cookie"` (default/anything-else stays bearer, byte-
unchanged) and sets both cookies with `refresh_token=""` in the body;
refresh/logout are dual-source on `read_refresh_cookie(request)`
presence, never a client-declared header, with `enforce_csrf(request)`
running BEFORE the cookie's token is ever used on the cookie path.
**No wire-schema change**: `X-Auth-Mode`/`X-CSRF-Token` are read directly
off `request.headers`, never a declared serializer field, and an empty
string still satisfies `TokenResponseSerializer.refresh_token`'s required
`str` — the strict conformance gate stays green unmodified by this half of
the stage.

**CORS**: new `AUTH_COOKIE_MODE_ENABLED` setting (`config/settings.py`,
default `False`) gates `CORS_ALLOW_CREDENTIALS=True` plus `x-csrf-token`/
`x-auth-mode` in `CORS_ALLOW_HEADERS` — only when `CORS_ALLOWED_ORIGINS`
also resolves to a non-empty, explicit (never wildcard) allowlist, the
identical invariant `app/main.py`'s own `auth_cookie_mode_enabled`-gated
CORS construction enforces on the FastAPI track. `CORSPolicy`'s own
`InsecureCORSPolicyError` guard makes a wildcard-plus-credentials
configuration impossible to construct regardless of this flag's value —
verified directly (`CORS_ALLOWED_ORIGINS="*" AUTH_COOKIE_MODE_ENABLED=true`
still raises at settings-import time).

**`tests/test_cookie_auth.py`** (15 tests, `@pytest.mark.django_db
(transaction=True)`) ports every scenario `backend/fastapi`'s own
`tests/test_cookie_auth.py` exercises: cookie login sets an HttpOnly+
Secure+SameSite=lax+Path=/auth refresh cookie and a non-HttpOnly csrf
cookie (asserted off the real, rendered `Set-Cookie` text via `http.
cookies.Morsel.output()` — see the test module's own docstring for why
that representation, not a raw boolean read off the Morsel's reserved
keys, is what reliably distinguishes "deliberately absent" from
"explicitly false" for a flag Django's `set_cookie` only ever writes when
truthy), body `refresh_token == ""`; bearer login unchanged (real token,
no `Set-Cookie` at all); a non-`"cookie"` `X-Auth-Mode` value stays bearer
too; cookie refresh missing/blank/mismatched `X-CSRF-Token` → 403
`permission_denied`, valid → 200 + both cookies rotated (values change);
replaying the rotated-out refresh cookie → 401 (reuse detection fires on
the cookie path exactly as the bearer path already proves); bearer
refresh with no CSRF header → 200, unchanged; cookie logout missing CSRF
→ 403 before the cookie is ever cleared, valid CSRF → 204 + both cookies
cleared (`Max-Age=0`) + idempotent; admin ping for a `seed_admin()`-
provisioned, verified admin → 200, an authenticated non-admin → 403, an
unauthenticated caller → 401; `seed_admin`'s own return value round-trips
through `uuid.UUID` with `roles == ("admin",)`.

**A genuine Django-`test.Client`-vs-httpx fidelity gap, documented rather
than silently worked around**: unlike httpx's cookie jar (which, like a
real browser, DELETES a cookie once it receives a `Max-Age=0` clear),
Django's `test.Client.request()` unconditionally does `self.cookies.
update(response.cookies)` — merging the CLEARED (empty-value, `Max-Age=0`)
Morsel into the jar rather than removing the key. A follow-up request
therefore still finds a (now-empty, but present) `refresh_token` cookie,
so `read_refresh_cookie`'s plain `.get()` still takes the cookie path and
(correctly) demands a `X-CSRF-Token` the idempotency test doesn't send.
This is a TEST-CLIENT fidelity gap, not a real behavioral one — a genuine
browser honors `Max-Age=0` exactly like httpx's jar already does — so
`test_cookie_logout_clears_both_cookies_and_204s_and_is_idempotent`
explicitly `del`s both cookie keys from the jar before its second,
idempotency-proving logout call, modeling what a real browser's cookie
jar would already have done on its own; see that test's own inline
comment for the full derivation.

**Real PostgreSQL 16 verification, over real HTTP against a live
database.** A fresh scratch role + same-named database (the sandbox's
already-running PostgreSQL 16 cluster, left online afterward — it was
found already online, matching Stage 5b's own "found online, left
running" posture, not Stage 5c's "found down, stopped after"), `manage.py
migrate --no-input` under `DJANGO_SETTINGS_MODULE=config.settings` (the
`0003 (head)` schema, no new migration this stage), `AUTH_REQUIRE_EMAIL_
VERIFICATION=false` for this run (email verification was already proven
against real PG16 in the Stage 5c transcript above; this run isolates
cookie mode and RBAC instead of re-proving that gate — mirroring `backend/
fastapi`'s own identical choice for its Stage 5d PG16 pass), then a full
`rest_framework.test.APIClient` round-trip against the REAL connection:

```
== register (regular user) ==
201 {'id': '9b42e2c6-b9b0-4dd9-afbe-58d0cd03d053', 'email': 'pg16cookieverify@example.com'}
== cookie login ==
200 {'access_token': 'eyJhbGciOi...<truncated>', 'refresh_token': '', 'token_type': 'bearer'}
refresh Set-Cookie: refresh_token=eyJhbGciOi...<truncated>; expires=Thu, 06 Aug 2026 16:08:48 GMT; HttpOnly; Max-Age=1209600; Path=/auth; SameSite=lax; Secure
csrf Set-Cookie: csrf_token=PX-8ufTsXNrtyvCWbWp4ObJOKkSvaliRUoI6wu2xYOA; expires=Thu, 06 Aug 2026 16:08:48 GMT; Max-Age=1209600; Path=/auth; SameSite=lax; Secure
COOKIE FLAGS VERIFIED (HttpOnly+Secure+SameSite=lax+Path=/auth on refresh; non-HttpOnly on csrf)
== cookie refresh WITHOUT X-CSRF-Token (expect 403) ==
403 {'error': {'code': 'permission_denied', 'message': 'CSRF validation failed: the X-CSRF-Token header is missing, blank, or does not match the csrf_token cookie.', 'details': None}}
== cookie refresh WITH valid X-CSRF-Token (expect 200, rotate) ==
200 {'access_token': 'eyJhbGciOi...<truncated>', 'refresh_token': '', 'token_type': 'bearer'}
BOTH COOKIES ROTATED (refresh + csrf values changed)
== replay the ROTATED-OUT refresh cookie (expect 401, whole family revoked) ==
401 {'error': {'code': 'unauthenticated', 'message': 'Authentication failed.', 'details': None}}
== cookie logout WITHOUT X-CSRF-Token (expect 403, cookies untouched) ==
403 {'error': {'code': 'permission_denied', 'message': 'CSRF validation failed: the X-CSRF-Token header is missing, blank, or does not match the csrf_token cookie.', 'details': None}}
== cookie logout WITH valid X-CSRF-Token (expect 204, both cookies cleared) ==
204
BOTH COOKIES CLEARED
== cookie logout again, jar cleared to model a real browser honoring Max-Age=0 (idempotent, falls to bearer path, expect 204) ==
204
== bearer login (unchanged -- no X-Auth-Mode header) ==
200 {'access_token': 'eyJhbGciOi...<truncated>', 'refresh_token': '<redacted, non-empty>', 'token_type': 'bearer'}
BEARER LOGIN UNCHANGED (real refresh_token in body, no Set-Cookie)
== admin ping: seeded admin (expect 200) ==
200 {'status': 'ok'}
== admin ping: authenticated non-admin (expect 403) ==
403 {'error': {'code': 'permission_denied', 'message': 'This action requires a role the current principal does not have.', 'details': None}}
== admin ping: unauthenticated (expect 401) ==
401 {'error': {'code': 'unauthenticated', 'message': 'Authentication failed.', 'details': None}}

ALL PG16 STAGE 5D ASSERTIONS PASSED
```

Direct-DB proof (a fresh `psql` connection, independent of the HTTP
client above):

```
== users ==
 id                                   | email                          | roles
 103de31c-1661-4ba7-b8c2-8a4bf507e22f | pg16adminverify@example.com    | ["admin"]
 9b42e2c6-b9b0-4dd9-afbe-58d0cd03d053 | pg16cookieverify@example.com   | []

== refresh_tokens for the regular user (5 rows, 4 families) ==
 family_id                        | used | revoked
 213a249a8d5b4cd3947f120a7b16b1a5 | t    | t   <- cookie-login family, tip row (rotated out, then replayed)
 213a249a8d5b4cd3947f120a7b16b1a5 | f    | t   <- cookie-login family, rotated tip (never itself reused -- still revoked, whole-family kill)
 8f80aba5f21441df98ff36832590a9f2 | f    | t   <- SECOND cookie-login family (the one used for the logout half) -- revoked via cookie logout, no rotation involved
 0658378b2b7d4c429bcc1d193fa4156b | f    | f   <- bearer-login family -- untouched
 d6e5920f71854c218b667dc154d420e8 | f    | f   <- bearer-login family (admin-ping non-admin check's own login) -- untouched
```

Two independent proofs in one transcript: (1) `AuthService.refresh`'s
whole-family reuse-detection revocation fires identically on the cookie
path as it already does on the bearer path (`213a249a...`: BOTH rows
`revoked = true`, including the tip that was never itself replayed); (2)
`AuthService.logout`'s revocation also reaches a family that was NEVER
rotated at all (`8f80aba5...`: a single row, `used = false` but `revoked
= true` — the cookie-mode logout call revoked it directly). The two
plain bearer-login families (`0658378b...`, `d6e5920f...`) are untouched
in both columns — proving neither kind of revocation reaches a caller's
other, unrelated sessions. The admin row's `roles` column round-trips as
Postgres `json` `["admin"]` — `seed_admin()` is the real, working
admin-provisioning path against a live database, not just the hermetic
sqlite suite.

`manage.py spectacular --format openapi-json` was also re-run under
`config.settings` (real-DB, a syntactically-valid but unreachable
`DATABASE_URL` after the scratch database was already dropped) and diffed
byte-identical against the hermetic-`config.settings_test` export,
confirming — same as every prior stage's own pass — that schema
generation genuinely never touches the database. Scratch role/database
dropped afterward; the cluster itself was left running (found already
online). Verification script written ad hoc for this run (not committed —
`tests/test_cookie_auth.py`, hermetic against sqlite, is the durable,
CI-running proof of this exact behavior; this transcript is the one-time
real-PG16 confirmation).

## Sessions (the default auth path)

**A browser client authenticating against this block gets a server-side
session, not a JWT.** `POST /auth/login` issues an opaque session id in an
`HttpOnly; Secure; SameSite=Lax; Path=/` `session_id` cookie unless the
caller explicitly asks for another transport, and every protected view
resolves that cookie. The JWT paths remain fully wired for native/mobile
clients and service-to-service callers — this is a default, not a
deprecation.

Behaviourally identical to `backend/fastapi`'s own session path, against
the same wire contract (`tests/test_schema_conformance.py` holds the two
byte-identical); see that block's README "Sessions" section for the
side-by-side session-vs-JWT comparison, and the vendored
`core/security/auth/_sessions.py`'s module docstring for the full
argument. The short version: a JWT is valid because it says it is, so
logout, bans, and privilege revocation only land after a TTL elapses. A
session's authority lives in a row, so revocation lands on the next
request, roles are read live, an idle timeout is enforceable, and nothing
readable sits in the browser. The cost is one indexed `sessions` lookup
plus one `users` lookup per authenticated request.

### Wiring

- **`core/models.py`** → the `Session` model / `sessions` table (migration
  `0007_sessions`). Stores the SHA-256 hash of the session id, never the
  id; no roles column, deliberately; `PROTECT` on the user FK.
- **`core/security/auth/stores.py`** → `DjangoSessionStore` (async ORM,
  durability from Django's own autocommit — see
  `DjangoRefreshTokenStore`'s docstring for that posture) and
  `build_session_service()`.
- **`core/views.py`** → `LoginView` defaults to session mode;
  `LogoutView` is triple-source; every role-gated view resolves through
  `require_roles_either` / `resolve_principal_either`, which take
  whichever credential the request actually carries, session first.
- **`core/security/auth/permissions.py`** → `has_role(...)`, the DRF
  permission class, now dual-transport for the same reason.
- **`core/security/auth/csrf_middleware.py`** → `SessionCsrfMiddleware`,
  registered in `config/settings.py`'s `MIDDLEWARE`. See "CSRF" below.
- **`config/settings.py`** → `SESSION_IDLE_TTL_SECONDS` (12h, sliding),
  `SESSION_ABSOLUTE_TTL_SECONDS` (7d, hard ceiling),
  `SESSION_TOUCH_INTERVAL_SECONDS` (60s, write-rate bound). A session must
  be inside BOTH deadlines. None of the three is a secret — a session id
  is opaque rather than signed, so unlike `JWT_SIGNING_KEY` there is
  nothing here to load from a secret store and nothing to fail closed on.

### Picking a transport

`POST /auth/login` reads `X-Auth-Mode`. It is never inferred from
`User-Agent` or any other signal, and an unrecognized value falls to the
default — the safer direction for a mistake to fall.

| `X-Auth-Mode` | Result |
| --- | --- |
| absent / `session` / anything unrecognized | **Session cookie** (default). Body: `token_type: "session"`, both token fields `""` — deliberately no token for the client to hold. |
| `bearer` | Access + refresh JWTs in the body. No cookies. |
| `cookie` | Refresh JWT in a `Path=/auth` cookie. Superseded by session mode; kept for a project mid-migration. |

`POST /auth/logout` decides by which credential is actually PRESENT
(session cookie → refresh cookie → body), so a forged or absent cookie
cannot claim a path it does not hold. There is **no refresh endpoint on
the session path**: `resolve` slides the idle deadline forward on every
authenticated request, so the credential renews itself as a side effect of
being used, up to the absolute ceiling.

### CSRF

The session cookie is `Path=/`, so **every** unsafe-method view is a CSRF
target, not just `/auth/*`. `SessionCsrfMiddleware` enforces the
double-submit check (`X-CSRF-Token` header must equal the `csrf_token`
cookie, constant-time compared) on every `POST`/`PUT`/`PATCH`/`DELETE`
that carries a session cookie. A per-view call would eventually be
forgotten on some new view; a middleware cannot be. It skips safe methods,
skips requests with no session cookie (a bearer client has no ambient
credential and must not be asked to echo a token it was never given), and
exempts exactly `POST /auth/login`. Logout is NOT exempt.

**This is not Django's own `CsrfViewMiddleware` and does not replace it** —
that one protects `django.contrib.sessions`' cookie with its own
`csrftoken`/`X-CSRFToken` pair. The two are independent and can coexist.
The middleware renders its own 403 envelope rather than raising, because
DRF's exception handler is per-view and runs after middleware; it reads
status and code from the same `AUTH_ERROR_HTTP` table `core/exceptions.py`
uses, so the two cannot drift.

### Revoking everywhere

`build_account_service()` wires BOTH revocation seams, so a password reset
kills every refresh-token family AND every server-side session. Wiring only
one would leave a reset account half-revoked — an attacker's session cookie
would keep authenticating despite every JWT being killed.

### Tests

`tests/test_session_auth.py` (27 tests) mirrors `backend/fastapi`'s suite
of the same name: login defaults to session mode and hands the client no
token, cookie flags, one `sessions` row and no refresh-token row, a fresh
id per login (session fixation), logout revoking on the very next request,
idempotent logout with no body required, CSRF on `/auth/*` and on a
non-auth view alike, safe methods and bearer clients exempt, the
middleware's envelope matching the serializer's shape, live role
revocation, password reset killing every session, and both deadlines.

## Security

### Security composition (Stage 4 Step 3, #27)

Four of the six baseline `templates/components/security/` catalog
components are now vendored under `core/security/` and wired into
`config/settings.py`'s `MIDDLEWARE`, as self-contained subpackages
(`_core.py`/`django.py`/`audit.py` as applicable, relative imports, a 4-line
vendored-from header + `DRIFT:` note where the import style needed
rewriting) — the same composition pattern `backend/fastapi`'s
`app/core/security/` uses for the FastAPI track. The other two
(`secret_store`, `input_validation`) are library code composed at the point
of use, not middleware — see "Secrets" below and
`core/security/input_validation/__init__.py`'s docstring respectively.
`webhook_signature` and `idempotency` (also in the component catalog) are
**NOT** vendored here at all — payments-shaped concerns with no consumer
yet; the Stage 11 payments recipe vendors and wires them when there's an
actual webhook endpoint to protect, mirroring `backend/fastapi`'s own
"not vendored yet" posture for the same two components.

**MIDDLEWARE order, outermost to innermost (top-to-bottom in
`config/settings.py`'s `MIDDLEWARE` list):**

1. **`corsheaders.middleware.CorsMiddleware`** — outermost. A genuine
   divergence from `backend/fastapi`, where CORS sits innermost of its four:
   `django-cors-headers`' own docs require `CorsMiddleware` to run as early
   as possible and specifically **before** `CommonMiddleware`, because
   `CommonMiddleware` can issue a redirect (e.g. `APPEND_SLASH`) that
   returns a response without ever calling further into the wrapped
   middleware chain — if `CorsMiddleware` sat inside `CommonMiddleware`,
   that redirect would never reach `CorsMiddleware`'s own response-phase
   header injection, silently breaking CORS on exactly the requests that
   get redirected. Starlette's stack has no equivalent "can synchronously
   short-circuit before calling downstream" concern baked into an
   analogous middleware, which is why the FastAPI track's CORS is innermost
   instead — a genuine Django-vs-Starlette mechanics difference, not an
   inconsistency between the two tracks.
2. **`core.security.security_headers.django.SecurityHeadersMiddleware`** —
   fulfills the Step 1/2 deferral note below. Placed before Django's own
   `SecurityMiddleware` so it runs LAST in the response phase (Django's
   `process_response` runs bottom-to-top, the reverse of `MIDDLEWARE`'s
   order) and gets the final, authoritative word on any overlapping header.
3. **`core.security.audit_logging.middleware.RequestIDMiddleware`** — NEW
   glue (not vendored), mirroring `backend/fastapi`'s own audit-bind
   middleware for this track. Binds a per-request id (inbound
   `X-Request-ID` if shape-valid, else a fresh `uuid4`) before rate-limiting
   runs, so every downstream `audit_event()` call — including a future
   rate-limit-denial audit trail — carries it automatically.
4. **`core.security.rate_limiting.django.RateLimitMiddleware`** —
   innermost of the four. Pre-auth (no real authentication exists yet —
   Stage 5, #28), general per-client-IP ceiling.
5. Django's own `SecurityMiddleware` / `CommonMiddleware` — innermost of
   all six; CORS still precedes `CommonMiddleware` per its own hard
   requirement, security-headers still precedes `SecurityMiddleware` per
   its own hard requirement, and neither Django middleware has a documented
   ordering requirement relative to request-id/rate-limiting.

See `config/settings.py`'s own "Security composition" comment block for the
full mechanics derivation (why Django's list-order semantics and
Starlette's `add_middleware()`-prepends-then-reverses semantics land the
same four components in the same relative order despite being opposite
mechanisms, with CORS as the one documented exception).

**Transport security headers (HSTS, `X-Content-Type-Options: nosniff`,
`Referrer-Policy`) are now WIRED**, fulfilling the deferral this section
previously carried: `core.security.security_headers.django.
SecurityHeadersMiddleware` sets the full header set (HSTS, nosniff,
frame-options, referrer-policy, CSP, Permissions-Policy) on every response.
Django's own `SECURE_HSTS_SECONDS`/`SECURE_CONTENT_TYPE_NOSNIFF`/
`SECURE_REFERRER_POLICY` settings are deliberately left at their off/unset
values in `config/settings.py` — turning them on too would double-stamp the
same headers from two uncoordinated sources; the vendored middleware is
placed before Django's own `SecurityMiddleware` in `MIDDLEWARE` so it wins
even if a future edit reorders things. `SECURE_SSL_REDIRECT` stays at
Django's own default (`False`) — an HTTP→HTTPS redirect is a routing/proxy-
layer concern this header-only component doesn't claim either.

**CORS is deny-by-default.** `CORS_ALLOWED_ORIGINS` (a comma-separated env
var) composes a `core.security.cors_lockdown.CORSPolicy`, whose constructor
refuses to build an empty-or-wildcard allowlist — an unconfigured
environment gets no cross-origin access at all, never an accidental
allow-all. Never `*` combined with credentials — `CORSPolicy`'s constructor
makes that configuration impossible to construct in the first place.
**`AUTH_COOKIE_MODE_ENABLED`** (Stage 5d, #46; default `False`) is a
SEPARATE, explicit opt-in gating whether that policy also allows
credentials (cookies) and the `X-CSRF-Token`/`X-Auth-Mode` headers web
cookie mode's SPA sends cross-origin — see "Conformance / Web cookie mode
+ RBAC admin example (Stage 5d, #46)" above for the full mechanics and
verification.

**Rate limiting** reads `RATE_LIMIT_CAPACITY` / `RATE_LIMIT_REFILL_PER_SECOND`
/ `RATE_LIMIT_TRUSTED_HOPS` from env, with the component's own defaults
(60, 1.0, 0) as the fallback — see `core/security/rate_limiting/_core.py`'s
`client_ip_key` docstring for what `RATE_LIMIT_TRUSTED_HOPS` actually gates.
Uses the component's stdlib, per-process `InMemoryBucketStore` — same
documented multi-worker/multi-replica limitation
`templates/components/security/rate-limiting/README.md` describes; a
Redis-backed store is Stage 11 work on both tracks.

**`/health` and `/readyz` are exempt from rate limiting** (Stage 4 review
fix, #27 — `core/security/rate_limiting/django.py`'s `_DEFAULT_EXEMPT_
PATHS`, proven by `tests/test_security_composition.py`'s
`test_health_and_readyz_are_never_rate_limited_even_under_burst`). A
readiness/liveness probe sitting behind an edge proxy at
`RATE_LIMIT_TRUSTED_HOPS=0` polls far more often than a general per-client
ceiling allows for ordinary traffic — without this exemption, a burst of
probe traffic (or probe traffic sharing a bucket with real traffic from the
same untrusted-proxy IP) could 429 the readiness check itself, reading as
an outage at the load balancer and pulling a healthy instance out of
rotation. **Issue #42:** this exemption has since been promoted to the
CANONICAL `templates/components/security/rate-limiting/django.py` (and its
`fastapi.py` sibling) — it is no longer DRIFT specific to this block, and
`backend/fastapi`'s `RateLimitMiddleware` now carries the identical
`/health`/`/readyz` exemption by the same default, closing the cross-track
gap this section used to flag.

**`RATE_LIMIT_MAX_KEYS`** (default `50000`, env-configurable, Stage 4
review fix, #27) bounds the in-process `InMemoryBucketStore`'s key
cardinality by threading a `max_keys` value into its construction —
`_core.InMemoryBucketStore` already supports this bound (its own
docstring); this block just wires a setting to it instead of leaving it at
the component's own unbounded (`None`) default, so a high-cardinality
client-IP key space (many distinct clients over the process lifetime) has
a hard per-process memory ceiling on top of the existing idle-TTL eviction.

**`webhook_signature`/`idempotency` are referenced, not wired** — see
above; the Stage 11 payments recipe adds them when there's a real webhook
endpoint.

## Database & migrations

`core/migrations/0001_initial.py` was generated with `manage.py
makemigrations core` against this model and applied with `manage.py
migrate` — **verified against real PostgreSQL 16** (the sandbox's available
cluster; this matrix pins PostgreSQL **18.x** — same documented gap
`backend/fastapi`'s README already carries, not repeated in detail here).
The partial index landed exactly as declared (see "The Item model" and the
transcript in this step's PR description). A create → get round-trip
through `Item.objects` (default manager) and the soft-delete scoping
(`mark_deleted()` + `save()` making a row invisible to `Item.objects` but
still visible via `Item.all_objects`) were both exercised directly against
that real database via `manage.py shell`.

**Stage 4 Step 4 (#27) re-verified against real PostgreSQL 16, this time
end-to-end over HTTP** (not just the ORM directly): `pg_ctlcluster 16 main
start`, a scratch role/database, `manage.py migrate --no-input` online
(clean, same schema as the sqlite-hermetic suite implies), then a full
round-trip through `rest_framework.test.APIClient` against the REAL
Postgres connection — `POST`/`GET`/`PATCH`/`DELETE /items`, the pagination
envelope shape, and every documented conformance case (malformed-UUID 404,
blank-`name` 422, `size=500` 422, `PUT` 405, post-delete 404, soft-delete
scoping directly via `Item.all_objects` vs. `Item.objects`) — all passed
against the real database, not sqlite standing in for it. `manage.py
spectacular --format openapi-json --file <path>` was ALSO re-run under
`config.settings` (the real-DB settings module, not `config.settings_test`)
and produced byte-identical output to the hermetic-settings export —
schema generation never touches the database either way, confirming that
this step's whole schema-export path is genuinely DB-independent, not
merely untested against a real one. Cluster stopped and the scratch
role/database dropped afterward — no DB artifacts, `.venv`, or `uv.lock`
committed (see this repo's root `.gitignore` plus this block's own, both
already covering `.venv/`/`*.sqlite3`/`uv.lock`).

**Stage 5b (#44): `core.0002_user_refreshtoken`'s first real-PostgreSQL
run**, plus a full end-to-end auth transcript over HTTP — same posture as
Step 4's own pass above, and mirroring `backend/fastapi`'s own
`alembic/versions/0002_create_auth_tables.py` real-database verification
(see that block's README). The sandbox's already-running PostgreSQL 16
cluster (`pg_lsclusters` — no `pg_ctlcluster ... start`/Docker container
needed this time, the cluster was already online) was used directly: a
scratch role + same-named database, `manage.py migrate --no-input` under
`DJANGO_SETTINGS_MODULE=config.settings` (the real-DB settings module),
then a full `rest_framework.test.APIClient` round-trip against the REAL
connection (`ALLOWED_HOSTS=testserver` set for the duration, since
`config.settings`'s real posture has no default host allowlist the way
`config.settings_test` doesn't need either).

Schema verified column-for-column against `alembic/versions/
0002_create_auth_tables.py`'s own Postgres-native result: `users.email`
UNIQUE (`users_email_key`), `refresh_tokens.token_hash` UNIQUE
(`refresh_tokens_token_hash_key`), `refresh_tokens.family_id` indexed
(`refresh_tokens_family_id_814ef118`), `refresh_tokens.user_id` indexed +
FK to `users(id)` (`refresh_tokens_user_id_b02a6331[_fk_users_id]`),
`used_at` nullable, `revoked` NOT NULL boolean — an exact match, modulo
the two already-documented nuances noted on the models themselves
(app-level `PROTECT` vs. DB-level `RESTRICT`; no `created_at`/`updated_at`
on `refresh_tokens`).

**The transcript, driven directly against that real database (not sqlite
standing in for it):**

```
register("pgproof@example.com", ...)        -> 201, email normalized (lowercase+stripped)
login(...)                                    -> 200, access+refresh pair
GET /auth/me  Bearer <access>                -> 200, email matches
register(same email again)                   -> 409 conflict
refresh(refresh_token)                        -> 200, ROTATED pair (both tokens differ)
refresh(ORIGINAL refresh_token)  # REPLAY     -> 401 unauthenticated
  -> response body contains none of "reuse"/"revoked"/"family"
refresh(ROTATED refresh_token)   # the tip,
  never itself reused                         -> 401 unauthenticated
  -- proves WHOLE-FAMILY revocation, not just the replayed row
-- direct DB read (bypassing the API): both refresh_tokens rows for
   this user's one family have revoked = true in Postgres itself
register("softdel@example.com", ...) -> 201; login -> 200 (tokens issued)
User.mark_deleted() + save()                  # soft-delete via the ORM
login("softdel@example.com", ...)             -> 401 unauthenticated
refresh(pre-deletion refresh_token)           -> 401 unauthenticated
  -- direct DB read: row still present via User.all_objects, invisible
     via User.objects (the default, not-deleted-scoped manager)
logout(rotated refresh_token)                 -> 204
refresh(that same token, post-logout)         -> 401 unauthenticated
```

Every line passed against the real Postgres 16 connection. `manage.py
spectacular --format openapi-json` was also re-run under `config.settings`
(real-DB) and diffed byte-identical against the hermetic-settings export —
confirming, same as Step 4's own pass, that schema generation genuinely
never touches the database. The exported schema's five `/auth/*`
operations were independently diffed against `packages/api-client/
openapi.json` (operationId + documented status-code set) and matched
exactly. Scratch role/database dropped afterward; the already-running
cluster itself was left as it was found (not stopped, since it wasn't
started by this verification pass) — no DB artifacts, `.venv`, or
`uv.lock` committed.

**Operational note — persistent connections and the async-ORM bridge.**
The `/auth/*` views are synchronous DRF views that reach the async auth
core via `async_to_sync(...)`, and `DjangoRefreshTokenStore` runs its
writes through Django's async ORM (`.acreate()`/`.aupdate()`). Django runs
that async ORM SQL in a thread-sensitive executor thread, which interacts
awkwardly with `CONN_MAX_AGE > 0` persistent connections (the default here
is `conn_max_age=600`): a persistent connection opened in the executor
thread can outlive the request that opened it, surfacing as intermittent
"connection already closed" errors under load. This does **not** affect
auth *correctness* — the reuse-detection durability guarantee holds because
every store write autocommits (no `ATOMIC_REQUESTS`, no wrapping
`transaction.atomic()`), verified above. It is purely a connection-
*lifecycle* concern for a real multi-worker deployment.

**#48 (L2) — `CONN_HEALTH_CHECKS=True` is now this block's default**
(`config/settings.py`'s `DATABASES` block, via `dj_database_url.config(...,
conn_health_checks=True)`) — the cheap mitigation: a liveness check
(Django 4.1+) runs on a reused persistent connection before it's handed
back out, transparently reconnecting if it's gone stale, instead of
surfacing that staleness as a query failure. This is the minimal safe
config default and is sufficient for most deployments. For a heavy
multi-worker deployment still observing this under sustained load, the
heavier alternative is `CONN_MAX_AGE=0` (close the connection at the end
of every request instead of pooling it in-process) paired with an
external connection pooler such as PgBouncer in front of Postgres — the
usual ECS/Fargate posture — rather than relying on Django's own
per-worker persistent-connection pool at all.

**Regenerating/diffing the exported schema against the frozen contract**
(same command Step 4's own "Conformance" section documents, now covering
the five `/auth/*` operations too):

```sh
manage.py spectacular --format openapi-json --file /tmp/django_schema.json
diff <(jq -S . /tmp/django_schema.json) <(jq -S . packages/api-client/openapi.json)
```

(a raw `diff` will show plenty of expected, accepted divergences —
`title`/cosmetic keys, component-naming choices, framework-level
metadata — the same ones `tests/test_schema_conformance.py`'s normalizer
already narrows away for the actual pass/fail gate; use that test, not a
raw `diff`, as the source of truth for wire-contract identity.)

## Dev run (Docker)

`Dockerfile` + `docker-compose.yml` (this directory), Stage 4 Step 4 (#27)
— boot this block for local development, mirroring `backend/fastapi`'s own
dev-run seam line-for-line (same base image, same uv-via-`COPY --from`
pattern, same non-root policy, same migrate-then-boot `api` command
ordering) — see each file's own header comment for the shared rationale.
Not a production deploy manifest — Stage 9's devops/infrastructure block
owns that.

`docker-compose.yml` materializes to `apps/api/docker-compose.yml` — the
SAME slot `backend/fastapi/docker-compose.yml` materializes into (this
block is an ALTERNATIVE to that one in that slot, never both at once, see
"Composition contract" above). The monorepo justfile's `dev` recipe
(`templates/monorepo/justfile`) needed NO changes for this — confirmed by
direct inspection: that recipe only checks whether `apps/api/
docker-compose.yml` exists and, if so, runs `docker compose up --build`
there; it has no FastAPI-specific assumption baked in (the same generic
check already worked for that block, and works identically for this one).

`gunicorn config.wsgi:application --reload`, not `uvicorn` — this block's
WSGI entrypoint is the primary one (`pyproject.toml`'s own comment); a
project that later adds real async/websocket views swaps the Dockerfile's
CMD to `uvicorn config.asgi:application --reload` instead (`config/
asgi.py` already exists for that seam). `--reload` + bind-mounted
`./core`/`./config` (not the FastAPI block's `./app`) mirror that block's
own live-edit dev posture exactly.

**Verification performed**: `docker compose config` (this directory)
renders a clean, fully-resolved compose manifest — confirmed directly (see
this step's PR description for the transcript). **NOT performed: an actual
image build/boot** — this sandbox has no reachable Docker daemon (`docker
build`/`docker compose up` both fail immediately with "no such file or
directory" on `/var/run/docker.sock`, not a proxy-blocked pull), the same
constraint `backend/fastapi`'s own Step 4 documented (there: registry
pulls blocked by the sandbox's egress proxy; here: no daemon at all —
different failure mode, same practical outcome). Assessed by review +
`docker compose config` instead, matching that block's own precedent for
this exact gap.

Postgres image pin (`postgres:18-bookworm`) and the uv/Python base image
pins are cited in `references/compatibility-matrix.md`'s "Containers"
section for BOTH backend tracks now (Stage 4 Step 4, #27) — no new pin
values, just the existing FastAPI-block pins now also covering this one.

## Testing

`tests/` (pytest + pytest-django + DRF's `APIClient`, all against
`config.settings_test`'s hermetic sqlite — `[tool.pytest.ini_options]` in
`pyproject.toml`):

- `test_items.py` — basic create/list/get/update/soft-delete smoke
  coverage (Step 2's first commit).
- `test_conformance_errors.py` — THE conformance-proof for
  `core/exceptions.py`: triggers a validation failure (422), a missing
  item (404), an unauthenticated/forbidden request (401/403, via a
  throwaway test-only route — see `tests/_conformance_urls.py`), and a
  forced unhandled exception (500); asserts each response body equals the
  `ErrorEnvelope` core/contract/errors.py's own model would produce for
  the same inputs (round-tripped through `ErrorEnvelope.model_validate`
  and, for the 404/500 cases, built directly from `NotFoundError`/
  `AppError`). Also asserts `deleted_at` never appears in any item
  response and `name=""` is rejected at 422. **Fix round additions**
  (every one hits the real route via `APIClient`, not the handler
  function in isolation): `test_malformed_uuid_path_is_404_not_500`,
  `test_malformed_json_body_is_not_500`, `test_put_is_405_not_500`,
  `test_bad_basic_auth_credentials_are_401_not_500` (the last via a new
  throwaway `_BasicAuthOnlyView` in `tests/_conformance_urls.py`, since no
  real route runs `BasicAuthentication` any more after the
  `DEFAULT_AUTHENTICATION_CLASSES = []` fix).
- `test_conformance_pagination.py` — creates N items, asserts `GET /items`
  equals `{items, total, page, size, pages}` cross-checked against
  `core.contract.pagination.Page.create(...)` for the same data. **Fix
  round**: `test_max_page_size_is_capped_at_200` (the old accepted-
  divergence clamp test) is replaced by `test_size_over_200_is_422`
  (same input, now the correct 422 outcome); new
  `test_size_exactly_201_is_422`, `test_size_zero_is_422`,
  `test_page_zero_is_422`, `test_page_negative_is_422`,
  `test_page_past_the_end_is_200_with_empty_items` cover the rest of the
  now-validated `PageParams` bounds.

Verification for Step 2 + its fix round: `manage.py check` (hermetic-sqlite),
`manage.py migrate` clean, the full pytest suite green (26 tests — 17 from
Step 2 plus 9 added this fix round: 4 in `test_conformance_errors.py`, 5 net
in `test_conformance_pagination.py`), and `scripts/validate_plugin.py` 0
warnings — see that step's PR description for the full transcript.
Step 1's own real-PostgreSQL-16 verification (model + migration) is
unchanged and not repeated here.

- `test_security_composition.py` (Stage 4 Step 3, #27) — the
  security-composition-proof for the wired MIDDLEWARE stack (see "Security
  composition" above), all via `APIClient`/`django.test.Client` against real
  routes, not the vendored components' own unit-level `tests/` (those live
  in `templates/components/security/*/tests/` and are unchanged by this
  step): security headers present on a normal `/health` response (nosniff,
  frame-deny, referrer-policy, CSP, Permissions-Policy) and
  `Strict-Transport-Security` present only when the request is secure
  (`secure=True`) and absent otherwise; CORS preflight rejects a disallowed
  `Origin` (no `Access-Control-Allow-Origin`), allows a configured one, and
  denies every origin when `CORS_ALLOWED_ORIGINS` is unset (deny-by-default);
  rate limiting returns `429` with a `Retry-After` header once a small
  test-configured burst (`RATE_LIMIT_CAPACITY=2`) is exhausted against
  `/items`, and a fresh budget is not denied; **`/health`/`/readyz` are
  never rate-limited even under a `RATE_LIMIT_CAPACITY=1` burst (Stage 4
  review fix, #27)**; `X-Request-ID` is bound and reflected — minted as a
  `uuid4` when absent, reflected verbatim when the inbound header is
  shape-valid, and replaced (not reflected) when it's malformed (embedded
  CR/LF) or oversize (>128 chars); `/items` is JSON-only (no browsable-API
  HTML in the response body). **Test isolation (Stage 4 review fix, #27):**
  the shared module-level `InMemoryBucketStore` singleton
  (`core.security.rate_limiting.django._default_store`) is now reset before
  EVERY test in the suite by an autouse fixture
  (`tests/conftest.py`'s `_reset_rate_limit_store`), not just the
  rate-limiting tests by hand — so the shared bucket can't leak a 429 into
  an unrelated test as the suite grows. Rate-limiting tests still construct
  a fresh `APIClient()` after overriding `RATE_LIMIT_*` settings — see the
  test file's own module docstring for why (the middleware reads settings
  at `__init__`, not per-request, and each `Client()`/`APIClient()` rebuilds
  its own middleware chain from current settings at construction).

Verification for this step: `manage.py check` (hermetic-sqlite), `manage.py
migrate` clean, the full pytest suite green (39 tests — the 26 above plus 13
new in `test_security_composition.py`), and `scripts/validate_plugin.py` 0
warnings. **Review fix round (#27):** one new test
(`test_health_and_readyz_are_never_rate_limited_even_under_burst`) brings
the suite to 40, still green; order-independence verified directly by
running the full test-module list both forwards and reversed (e.g.
`test_security_composition.py`, then `test_items.py`, then
`test_conformance_pagination.py`, and the same three files in the opposite
order) — both orderings pass, confirming the autouse
`_reset_rate_limit_store` fixture (`tests/conftest.py`) actually decouples
the rate-limiting tests' outcome from whatever ran before them.

- `test_schema_conformance.py` (Stage 4 Step 4, #27) — THE wire-surface
  conformance proof; see "Step 4: OpenAPI schema + wire-surface conformance
  proof" above for the full result. Two tests:
  `test_wire_surface_is_identical_to_the_frozen_contract` (the hard gate —
  fails loudly on any undocumented divergence) and
  `test_operation_id_and_component_name_parity_report` (prints the
  best-effort operationId/component-name delta report and fails only if an
  operationId — which this block controls exactly — ever regresses).

Verification for this step (this state, #27): `manage.py check`
(hermetic-sqlite), the full pytest suite green (42 tests — the 40 above
plus 2 in `test_schema_conformance.py`), `manage.py spectacular
--format openapi-json --file <path>` exports clean with 0 warnings (run by
hand, not part of the hermetic suite itself — see that section above), and
`scripts/validate_plugin.py` 0 warnings.

## Judgment calls

- **App label is `core`, not `api`.** The materialized *directory* both
  this block and `backend/fastapi` land in is `apps/api/` (same slot,
  alternative tracks — see this file's top-level description) — but
  Django's own `INSTALLED_APPS` app-label convention is a different
  namespace from that directory name, and naming the Django app itself
  `api` would read as "the whole API" when it's actually just the model +
  contract package this step ships. `core` avoids that collision and
  leaves room for a later `api` app (views/serializers/routers) if Step 2+
  wants that split instead of extending this one.
- **No admin/sessions/messages/staticfiles apps.** This is an API-only
  block (JSON over DRF, no server-rendered templates) as of this step — the
  Django defaults that assume a browsable admin site or session-backed
  auth aren't needed yet and add attack surface/migration weight for
  nothing consumed. Add them back deliberately if a materialized project's
  Step 2+ needs the admin site.
- **`psycopg[binary]`, not bare `psycopg` + system libpq.** The `[binary]`
  extra vendors a self-contained C extension wheel with no local libpq
  headers/toolchain required for `uv sync` to succeed — matches this kit's
  "no surprise system deps" posture elsewhere (e.g. `asyncpg` needing no
  separate system package). A project deploying with its own optimized
  libpq build can swap to bare `psycopg` + `psycopg[c]` later; not this
  block's default.
- **`SECRET_KEY` has no fallback value, not even in `DEBUG` mode.** Unlike
  Django's own `startproject` scaffold (which ships a real, checked-in
  dev key), this block's instructions are explicit: "SECRET_KEY from env —
  NEVER hardcode." A missing value fails immediately and loudly at
  settings-module import (mirroring `backend/fastapi`'s "no default means
  required" `AppSettings` convention) rather than silently running on a
  fabricated key that risks surviving into a real deployment.
  `config/settings_test.py` sets an explicit, clearly-named placeholder
  value only for the hermetic-sqlite path, and only via `setdefault` (a
  real `SECRET_KEY` in the environment still wins).
- **PostgreSQL 16, not the matrix's pinned 18.x, for the real-DB
  verification.** Same gap `backend/fastapi`'s README already documents —
  the sandbox only had a startable 16 cluster; nothing in this block's
  model or migration is 18-specific, but this is a noted gap, not a
  substitute for an eventual 18 run.
- **`gunicorn` pinned to `26.0.*`, not `23.0.*`.** This step's own
  instructions suggested "gunicorn or uvicorn for asgi" as an illustrative
  example version; PyPI's actual current stable at verification time was
  `26.0.0` (verified via `pip index versions gunicorn`) — pinned the real
  current line rather than the illustrative one, per this catalog's
  "recall is not a source, verify against the registry" convention (see
  compatibility-matrix.md's "Version check").
- **`ItemManager`/`ItemQuerySet` scope every default lookup, `Item.
  all_objects` stays unscoped.** Django's own docs on custom managers
  recommend keeping an unscoped default manager available even when a
  project narrows `objects` — some internal machinery expects one to
  exist. `all_objects` is that escape hatch, doubling as the "hard-delete
  cleanup job" / "admin view that needs soft-deleted rows" access path
  `ItemQuerySet.with_deleted()` also provides via the scoped manager.
- **`secret_store`: ONE copy, not two — left in `core/contract/`, not moved
  or re-vendored under `core/security/`.** Step 1 already vendored
  `secrets-loading/secret_store.py` verbatim into
  `core/contract/secret_store.py`, alongside `errors.py`/`pagination.py`, as
  one of the three "vendored contract sources" sharing that directory's
  weekly-freshness-audit maintenance path. Step 3 needed `secret_store` too
  (for the `JWT_SIGNING_KEY` composition seam, "Security composition"
  above) and had two options: vendor a second copy under
  `core/security/secrets_loading/` (matching every other security
  component's subpackage shape) or reuse the Step 1 copy in place. Chose
  reuse — a second byte-identical copy of the same file would mean the
  freshness audit tracking two locations for content that's supposed to be
  identical, a pure maintenance liability with no compensating benefit
  (unlike the other five components, `secret_store` has no per-track
  variant to justify a second copy; it's the same framework-neutral file
  either way). `core/security/`'s own `__init__.py` documents the absence
  of a `secrets_loading/` subpackage for the same reason, so a future reader
  doesn't mistake the gap for an oversight.
- **`input_validation` is vendored but not called from anywhere yet.**
  Unlike the other three MIDDLEWARE-wired components, `StrictModel`/the
  hardened field types are shared/service-layer tooling per the component
  README's own "Django/DRF note" — DRF serializers remain the actual HTTP
  request-validation layer (`core/serializers.py`, unchanged by this step).
  This block has no shared/service layer yet (Stage 4's scope is the
  contract-emission layer + its security composition, not business logic
  underneath it), so nothing calls into `core/security/input_validation/`
  as of this step. Vendored now anyway, matching `backend/fastapi`'s own
  precedent (that track's `input_validation` subpackage is equally unused
  by its current schemas — see that block's `app/schemas/item.py`
  docstring) — so a later step adding a real shared/service layer has it
  available immediately rather than needing a fresh vendoring pass.
- **Rate-limiting's `RateLimitMiddleware` reads settings at `__init__`, so
  its security-composition test constructs a fresh `APIClient()` after
  overriding settings, and resets the shared `InMemoryBucketStore`
  singleton first.** Django instantiates each `MIDDLEWARE` entry with only
  `get_response` — no per-request kwarg-passing mechanism — so the
  component reads `RATE_LIMIT_*` from `django.conf.settings` once, at
  construction. `django.test.Client.__init__` (and DRF's `APIClient`, which
  subclasses it) rebuilds a fresh middleware chain on every instantiation,
  which is what makes a per-test settings override actually take effect —
  but only if the override happens BEFORE the client is constructed, and
  only if the module-level default store (per-process, shared across every
  middleware instance that doesn't get an explicit `store=` kwarg) is reset
  first, since otherwise an earlier test's requests against the same test
  client `REMOTE_ADDR` would have already partially drained the bucket. See
  `tests/test_security_composition.py`'s own module docstring for the full
  mechanics.
