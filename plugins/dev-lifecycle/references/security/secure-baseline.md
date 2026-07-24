<!--
library: secure-baseline
versions-covered: "OWASP Top 10:2025, OWASP ASVS 5.0"
last-verified: 2026-07-23
provenance: manual
sources:
  - https://owasp.org/Top10/
  - https://owasp.org/www-project-application-security-verification-standard/
  - https://cheatsheetseries.owasp.org/cheatsheets/HTTP_Headers_Cheat_Sheet.html
  - https://cheatsheetseries.owasp.org/cheatsheets/Cross-Site_Request_Forgery_Prevention_Cheat_Sheet.html
  - references/compatibility-matrix.md (Security tooling (CI scanners) section)
-->

# Secure baseline

**The firm security standard every build inherits.** This is the bar a template block or catalog component must clear to call itself secure-by-default — no insecure default a scaffolded project has to remember to fix later. `template-author`'s fourth acceptance bar points here directly.

## Contents
- Version check (do this first)
- Transport: TLS everywhere
- Security headers & CSP
- CORS lockdown
- Input validation & output encoding
- Authentication & authorization
- Rate limiting & lockout
- Secrets never in code or images
- Dependency, SAST, and IaC scanning in CI
- Audit logging
- Least privilege
- Secure-by-default conventions (template blocks)
- Related canon

## Version check (do this first)
Anchored to **OWASP Top 10:2025** (current) and **ASVS 5.0**. Security-relevant dependency versions (auth libraries, TLS stacks, the Terraform AWS provider) are governed by `references/compatibility-matrix.md` — verify a security-relevant dep is on-matrix before trusting its defaults.

## Transport: TLS everywhere
- HTTPS only, in every environment including local dev where practical (self-signed or mkcert). No plaintext HTTP for anything carrying auth tokens, session cookies, or PII.
- Redirect HTTP → HTTPS at the edge (ALB/CloudFront), and set `Strict-Transport-Security` (HSTS) once TLS is confirmed everywhere including subdomains.
- Terminate TLS with managed certs (ACM) — never hand-roll cert issuance/renewal. See `references/infra/aws.md`.

## Security headers & CSP
Set on every HTTP response by default (middleware, not per-route opt-in):
- `Content-Security-Policy` — start restrictive (`default-src 'self'`), allowlist only what the app actually loads. No `unsafe-inline`/`unsafe-eval` without a documented reason.
- `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` (or `frame-ancestors 'none'` in CSP), `Referrer-Policy: strict-origin-when-cross-origin`.
- `Permissions-Policy` scoped to features the app actually uses (camera, geolocation, etc. off by default).

## CORS lockdown
- Explicit allowlist of origins — never `*` combined with `credentials: true`. Distinct allowlists per environment (dev/staging/prod), not one list shared across all three.
- Only allow the methods/headers the API actually needs; don't default to allow-all for convenience during development and forget to tighten it.

## Input validation & output encoding
- Validate all external input (body, query, path, headers, uploads) at the boundary — type, range, length, format. Schema validation (Pydantic, DRF serializers, Zod) is the mechanism, not hand-rolled checks; see the relevant backend/frontend reference.
- Parameterize every query — never string-built SQL with user input (the ORM used correctly defends this by default).
- Encode output for its context (HTML-escape by default in templates; no `dangerouslySetInnerHTML`/`| safe` on unsanitized user content). Never trust client-side validation as the actual control.
- Full checklist: `references/security/owasp.md` (A05 — Injection) and `references/security/attack-surfaces.md`.

## Authentication & authorization
- Authentication proves identity; authorization checks whether *this* identity may act on *this* resource — enforce both, on every protected route, server-side. Being logged in is not being allowed.
- Hash passwords with a strong adaptive algorithm (bcrypt/argon2); never store or log plaintext or reversible "encryption" of a password.
- **Prefer a server-side session over a JWT for a browser client.** A JWT is valid because it says it is — the server has no say between mint and expiry, so logout, a ban, or a role change only take effect once the access token's TTL elapses. An opaque, server-side session (its authority in a store row, not in the client-held credential) is revocable on the very next request, resolves roles live, and enforces a real idle timeout — properties a JWT cannot offer. That is why this firm's default auth component (`templates/components/security/auth/`) issues a session for browser clients and reserves JWTs (access + refresh, with rotation and reuse detection) for native/mobile clients — which have a real OS-backed secret store and no ambient-cookie exposure — and service-to-service callers, which have no user session to look up at all. Whichever credential is in play, validate it fully: signature/opacity, expiry, audience/issuer for a JWT; store-row state for a session.
- Check ownership/scope on every ID-addressed resource (`/orders/{id}` must confirm the order belongs to the caller) — this is the IDOR class, the #1 OWASP category two cycles running.
- **Cookie-borne credentials (session or refresh) ⇒ CSRF defense; bearer tokens ⇒ none needed.** If auth rides an ambient credential the browser attaches automatically (a session cookie or a JWT refresh cookie), a state-changing request needs CSRF protection: a **double-submit cookie** (a non-`HttpOnly` CSRF token echoed back in a request header the server checks equals the cookie) and/or `SameSite=Lax`/`Strict`, plus the cookie itself `HttpOnly; Secure; SameSite=Lax` with a scoped `Path`. A **session cookie is typically scoped `Path=/`** (it authenticates every route), which means CSRF must be enforced on every unsafe-method request app-wide, not only on the auth endpoints a `Path=/auth`-scoped refresh cookie would touch — a single method-filtering middleware, not a per-route call, is how the reference implementation covers this without relying on every future route remembering it. A token the app attaches *explicitly* in an `Authorization` header (bearer/SecureStore, the mobile default) is not ambient and needs no CSRF. Cookie-credentialed CORS must name explicit origins with `Allow-Credentials: true` — never a `*` wildcard (a wildcard is incompatible with `credentials: "include"`). Full end-to-end pattern: `references/wiring/auth-end-to-end.md`.

## Rate limiting & lockout
- Rate-limit authentication endpoints (login, password reset, token refresh) and any expensive or abuse-prone action. Backoff/lockout on repeated auth failures for a given account or IP.
- Apply a general API rate limit (per-user or per-IP) so no single client can exhaust shared capacity — return `429` with a `Retry-After` where practical.

## Secrets never in code or images
- No secret (API key, DB password, signing key) ever committed, hardcoded, or baked into a container image layer. `.env` is gitignored; only `.env.example` (placeholders) is committed.
- Full lifecycle — where each secret lives per environment, rotation, and OIDC over long-lived keys — is canon in `references/security/secrets-management.md`. This baseline states the rule; that doc states the mechanics.

## Dependency, SAST, and IaC scanning in CI
- **Dependency scanning:** `pip-audit`/`npm audit` (or equivalent) on every PR; block merge on new high/critical findings with no waiver.
- **SAST:** static analysis (e.g. `bandit` for Python, `eslint` security rules for JS/TS) runs in CI, not just locally.
- **IaC scanning:** tfsec/checkov on every `infra/` change — see `references/infra/terraform.md`.
- A scan that always passes because it's not wired into a required check is not a control.

## Audit logging
- Log security-relevant events: authentication successes/failures, authorization denials, privilege/role changes, and administrative actions — with actor, action, resource, and timestamp.
- Logs never contain secrets, tokens, passwords, or full PII payloads. Log the fact and the identifiers, not the sensitive content.
- Fail closed: an error in an auth/authz check denies access, it never silently proceeds. See `references/security/owasp.md` (A10).

## Least privilege
- IAM roles, DB users, and service accounts scoped to exactly what a task needs — no wildcard admin roles used out of convenience. CI and workload identity via OIDC-assumed roles, not long-lived static credentials.
- Network segmentation by default: data stores in private subnets, nothing public that doesn't need to be, security groups scoped tight rather than default-open.

## Secure-by-default conventions (template blocks)
Concrete defaults a scaffolded project ships with — the enforcement half of this baseline, not restating the sections above. Every catalog component and template block conforms to these; `code-review`'s security dimension and `security-audit`'s config-hygiene step both check diffs against them.

- **Non-root containers.** Every Dockerfile runs the app as a created, unprivileged user (`USER app`, not root) — never the image default. See `references/compatibility-matrix.md`'s Containers row for the base images this assumes.
- **`.env.example` only.** A repo ships `.env.example` with placeholder values and nothing else; real `.env` files are gitignored and never committed. This is the concrete rule behind "Secrets never in code or images" above.
- **Settings deny-read `.env*`.** Editor/agent settings (`.claude/settings.json` and equivalents) explicitly deny reading `.env` and its variants, not just `.gitignore`-hide them — a secret an agent can read it can leak into a transcript or a generated file.
- **Least-privilege IAM by default.** Terraform modules in the catalog scope IAM policies to the exact actions/resources a role needs — no `Action: "*"` or `Resource: "*"` shipped as a starting point that a team is expected to tighten later. Concretizes "Least privilege" above.
- **Encrypted at rest.** RDS and S3 resources in the catalog set encryption (`storage_encrypted = true`, default SSE) by default, not as an opt-in flag a project has to remember to set.
- **Secrets never in images or code.** No `ARG`/`ENV` bakes a secret into a Docker layer, no default value in code is a real credential (placeholders are obviously fake — see the planted-violation convention in this workflow's own test fixtures). Full lifecycle in `references/security/secrets-management.md`.
- **CI security gates required, not advisory.** `assets/workflows/security.yml` — the firm-standard scaffolded workflow — is a required check, not a report-only job: `sast` (bandit + semgrep), `secrets` (gitleaks), `deps` (pip-audit / pnpm audit), `iac` (checkov), and `containers` (trivy) each fail their job on a finding at or above the configured severity, and a red gate blocks merge per `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`. Pinned tool versions and the choice of scanner per stack are recorded in `references/compatibility-matrix.md`'s Security tooling section — that matrix, not this doc, is where a version bump happens.

## Related canon
This is the hub; the other three security references specialize it:
- `references/security/secrets-management.md` — where every secret lives and how to obtain it.
- `references/security/payments-security.md` — PCI-conscious money handling (Stripe).
- `references/security/data-protection.md` — PII classification, encryption, retention.
- `references/compatibility-matrix.md` — the pinned versions this baseline assumes.
- `references/security/owasp.md` and `references/security/attack-surfaces.md` — the per-change review checklist and attack-surface map this baseline's defaults are meant to pre-empt.
