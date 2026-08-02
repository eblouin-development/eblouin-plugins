# 0002. CI gates run on ready-for-review PRs only; drafts are gated in the container

## Status
Accepted

## Context
The firm's pipeline builds a feature as a series of commits on one branch under a single **draft**
PR (`coding-session`). Until now every one of those pushes ran the full CI suite. For a feature
built in eight or ten steps, that's eight or ten full pipeline runs whose only audience is the agent
that just pushed — burning GitHub Actions minutes and runner concurrency to report on
work-in-progress nobody is reading, and (on a busy repo) queueing behind runs that matter.

The convergence doctrine (`shared/ci-convergence.md`) already required a **local gate** before every
push: the worker runs the project's lint, type-check, and tests in the container and pushes only
when they're green. CI on a draft was therefore mostly re-confirming what the container had already
established, and its "CI is unavailable" fallback already described how to reconstruct and run the
full pipeline locally. The mechanism to gate drafts existed; it just wasn't the default.

The counter-argument is real and worth stating: excluding drafts means a broken branch can sit
undetected for longer, and the first CI run happens at the moment the change is handed to a human —
the worst moment to discover a failure. That risk is only acceptable if the local gate is genuinely
the pipeline's gate rather than a subset of it.

## Decision
**PR gates run only on pull requests that are ready for review.** Drafts are excluded from CI and
verified in the container instead. Concretely:

- **Wiring** (`references/devops/cicd.md`): every PR-triggered workflow carries `ready_for_review`
  in its `pull_request` `types:` *and* a job-level draft guard
  (`if: github.event.pull_request.draft == false`, or the `github.event_name != 'pull_request' || …`
  form when the workflow also runs on `push`). The types filter alone is insufficient — `synchronize`
  fires on every push to a draft. Applied to the shipped `assets/workflows/security.yml`, to this
  repo's own `validate` / `version-bump` / `template-tests` workflows, and required of the `devops`
  skill for any PR gate it writes.
- **The draft/ready contract** (`shared/ci-convergence.md`): a draft PR belongs to the session and is
  gated by the **local gate** — the pipeline reconstructed from the workflow files and run in the
  container. A ready PR belongs to the human and is gated by CI. Flipping to ready is therefore two
  signals at once: "this is for you" and "start the pipeline."
- **The flip is a claim, not a probe.** Preconditions: local gate green on the PR head, final review
  clean on that same commit. The session notifies the user at the flip rather than waiting on the
  run, but keeps a CI watchdog armed — the session is not done until that run resolves.
- **Post-flip red is triaged before the PR's state changes.** A failure the diff caused sends the PR
  **back to draft** (with the reason stated on the PR), through the normal fix loop, and back to
  ready. A failure the diff did not cause — Actions usage or concurrency limits, a runner or
  provider outage, a rate limit, an expired secret, a failure that reproduces on the base branch —
  **leaves the PR ready** and goes to the user as a diagnosis. Flipping back on someone else's
  failure hides a mergeable PR behind a problem the session cannot fix.

## Consequences
- **The local gate is now load-bearing, not a courtesy.** It must reconstruct the *whole* pipeline —
  lint, type-check, tests, build, security scans, `actionlint`/`shellcheck` — not just the fast
  checks. Where a job genuinely can't run in the container (needs a repo secret, a service, a runner
  matrix), that's named explicitly in the sign-off package as the residual the post-flip run tests.
- **A red first run is now a signal about the gate, not just the branch.** The doctrine requires
  noting what the local gate should have caught and closing that hole, so the gate converges on the
  pipeline over time instead of drifting from it.
- **Skipped ≠ failed in branch protection.** A job excluded by the draft guard reports as skipped,
  which satisfies a required status check. This is harmless because GitHub won't merge a draft PR,
  and the flip re-runs everything for real — but it means "green" on a draft PR proves nothing, and
  the doctrine says so.
- **Fan-out pipelines need care.** Where jobs use `needs:` plus `if: always()`, the guard goes on the
  upstream job and the aggregator must still fail on a real failure. This repo's `template-tests`
  does exactly that (guard on `changes`, aggregator unchanged).
- **The session's shape changes at the end.** It no longer ends at the flip; step 7d watches the
  post-flip run, and gate 2 (the human's merge) explicitly waits for CI green.
- **Repos that still gate every push keep working.** The doctrine tells the session to read the
  workflow triggers rather than assume, and to keep the local gate either way — it's cheaper than a
  CI round trip regardless.
