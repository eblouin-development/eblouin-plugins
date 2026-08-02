<!--
library: github-actions
versions-covered: "n/a"
last-verified: 2026-08-02
provenance: manual
sources: []
-->

# CI/CD pipeline conventions

Guidance for the build/test/deploy pipeline and its gates. Default platform: GitHub Actions. The project's existing pipeline overrides anything here.

## Contents
- Pipeline shape
- When the PR gates run (draft vs ready)
- The gates (this is the point)
- Build, tag, push
- Deploy stage
- GitHub Actions specifics
- Right-sizing

## Pipeline shape
A fully built-out pipeline handles: lint → type-check → test → security scan → build image → push to registry → deploy. Continuous integration is the first half (validate every push/PR); continuous deployment is the second (ship automatically when the gates pass, optionally behind a manual approval for production).

- **On a PR that is ready for review:** run the gates (lint, types, tests, scans). These must pass for the PR to be mergeable. Draft PRs are excluded — see the next section.
- **On merge to the deploy branch:** re-run gates, build and push the image tagged by git SHA, then deploy.
- Use the same container image throughout — the artifact that passed tests is the artifact that deploys.

## When the PR gates run (draft vs ready)

**Firm convention: PR gates run only on pull requests that are ready for review.** A draft PR is a
work-in-progress belonging to whoever is building it — an agent pushing a dozen intermediate commits
through a coding session, or a human iterating — and running the full pipeline on each of those
pushes burns Actions minutes and runner concurrency to tell someone something they already know.
Flipping to ready is the moment the change is claimed to be done, so that's the moment the gates
mean something.

The trade this makes is explicit: **the gate moves into the container until the flip.** Whoever
builds the branch runs the pipeline's own checks locally before flipping, so the post-flip run
confirms rather than discovers. The agent-side half of this contract is
`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`.

Wire it with both halves — the trigger type *and* a job-level draft guard. `types:` alone is not
enough: `synchronize` still fires on pushes to a draft.

```yaml
on:
  pull_request:
    # `ready_for_review` is what makes the flip itself trigger a run.
    types: [opened, synchronize, reopened, ready_for_review]

jobs:
  test:
    # The guard is what actually excludes drafts, since `synchronize`
    # fires on every push to one.
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
```

When the same workflow also runs on `push` (or `schedule`/`workflow_dispatch`), guard only the PR
case — `github.event.pull_request.draft` is unset on those events:

```yaml
    if: github.event_name != 'pull_request' || github.event.pull_request.draft == false
```

Notes:

- **A PR opened directly as ready still runs** — `opened` fires with `draft == false`.
- **Skipped ≠ failed.** A job excluded by the guard reports as skipped, which branch protection
  treats as satisfied — harmless, because GitHub won't let a draft PR be merged anyway, and the
  flip re-runs everything for real.
- **In a fan-out/fan-in pipeline**, put the guard on the upstream job the others `needs:`, and check
  the aggregator's `if: always()` logic still fails correctly when a real job fails (a skipped
  dependency must not be read as a pass on anything but a draft).
- **Deploy and post-merge workflows are unaffected** — they trigger on `push` to the deploy branch,
  not on PR state.

## The gates (this is the point)
Deployment is *gated*. A red gate blocks the deploy — never configure a pipeline that ships on failure.

1. **Lint & format:** the project's linters/formatters (Ruff/ESLint/Prettier) in check mode. Also lint the pipeline itself when the repo has one: `actionlint` on `.github/workflows/*` (it catches invalid expressions — e.g. `secrets` used in an `if:`, or an unknown self-hosted runner label needing an `.github/actionlint.yaml`) and `shellcheck` on any committed shell script. The build agent runs these locally before opening the PR (definition-of-done), and this gate is the backstop.
2. **Type-check:** `mypy`/`pyright` for Python, `tsc --noEmit` for TypeScript. Type errors fail the build.
3. **Tests:** the full suite (pytest, the JS test runner). Fail on any failure; enforce a coverage threshold if the project sets one. This gate runs the tests the build skills wrote — it only protects you if those tests exist and are meaningful.
4. **Security scans** — the automated counterpart to the code-review skill's security audit:
   - **Dependency scanning** for known-vulnerable packages (OWASP A03) — e.g. `pip-audit`, `npm audit`, or a scanner action.
   - **Image scanning** (Trivy/Grype/Docker Scout) on the built image.
   - **Secret detection** (gitleaks/trufflehog) so credentials never land in the repo or image.
   - **SAST** (e.g. CodeQL) where it fits, for injection and similar classes.
   - Set severity thresholds deliberately: fail on high/critical; triage the rest rather than blocking on noise.

Run independent gates in parallel for speed; cache dependencies and Docker layers.

## Build, tag, push
- Build the image only after the gates pass.
- Tag by immutable git SHA (and optionally a moving tag like `latest` or an environment name). SHA tags make every deploy traceable and every rollback addressable.
- Push to the project's registry (GitHub Container Registry, ECR, Artifact Registry, Docker Hub). Authenticate via CI secrets / OIDC, never hardcoded creds.

## Deploy stage
- Pull/reference the exact tested image by SHA and deploy it to the target.
- Run database migrations as an explicit, ordered step before/with the release (see backend Alembic conventions) — never implicitly, never skipped.
- Prefer a zero-downtime strategy the target supports (rolling, blue-green, canary). Verify health after rollout.
- Production deploys can sit behind a manual approval (GitHub Environments protection rules) — continuous delivery with a human gate — when full continuous deployment isn't wanted.
- Define rollback: redeploy the previous SHA, and have a plan for migrations that aren't trivially reversible.

## GitHub Actions specifics
- Workflows in `.github/workflows/*.yml`, triggered on `push`, `pull_request`, and environment events. PR-gate workflows carry the `ready_for_review` trigger and the draft guard above.
- **Pin action versions** (ideally by SHA, at least by major tag) — third-party actions are supply-chain surface.
- Use `secrets` for credentials and prefer **OIDC** federation to cloud providers over long-lived keys.
- Use **Environments** with protection rules and required reviewers for production; scope secrets per environment.
- Use a matrix for multi-version testing; cache (`actions/cache`, Docker layer caching / Build Cloud) to keep runs fast.
- Set least-privilege `permissions:` on the `GITHUB_TOKEN`.

## Right-sizing
- For a small app on a modern PaaS, the platform may handle build/deploy/preview/rollback itself — then CI is just the gates (lint/type/test/scan) and the platform does the rest. Don't build a bespoke deploy pipeline you don't need.
- Add complexity (multi-env promotion, canary, GitOps) only when the project's scale and risk justify it.
