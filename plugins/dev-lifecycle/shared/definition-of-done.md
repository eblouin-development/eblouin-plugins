# Definition of done (merge-ready)

The shared bar for "done." Planning derives acceptance criteria from it, testing writes to it, code-review gates on it, devops ships only what clears it. "Merge-ready" is not "the agent posted a diff" — it is all of the following. At that point the work waits for the human to merge.

A change is merge-ready when:

- **Behavior** meets the acceptance criteria in the issue/plan — the happy path and the named edge cases.
- **Tests** exist at the right levels (see the `testing` skill), are meaningful (behavior and contracts, not internals), and pass. New logic is covered; a fixed bug has a regression test guarding it.
- **Standards** are met: idiomatic for the installed stack/version, consistent with the codebase's existing patterns and naming, honestly typed (no `any` / `# type: ignore` used as an escape hatch).
- **Security**: every touched path is clean against the OWASP checklist (see `${CLAUDE_PLUGIN_ROOT}/references/security/owasp.md`) — authorization on protected routes, no injection, no secrets committed or logged.
- **No regressions**: the change's blast radius was checked; existing tests pass; callers and consumers still hold.
- **Docs** moved with the code where relevant: docstrings, README/usage, the API contract, or an ADR for a significant decision.
- **The gate is green** on the PR head: lint, type-check, tests, and security scans all pass — including `actionlint` on any GitHub Actions workflow file and `shellcheck` on any shell script the change touches. CI runs only on PRs that are **ready for review**, so this is verified in two stages: the **local gate** (the pipeline's own checks, run in the container) must be green *before* the PR is flipped to ready, and the CI run the flip starts must then confirm it. Merge waits for that CI green; a red run is triaged before the PR's state changes (`${CLAUDE_PLUGIN_ROOT}/shared/ci-convergence.md`).
- **Reviewed**: a `code-review` pass is complete and its blocker/high findings are resolved — and green and clean hold on the **same commit**, not a green from before the last fix.

Then stop. The PR is merge-ready; **the human merges.** No agent self-merges.
