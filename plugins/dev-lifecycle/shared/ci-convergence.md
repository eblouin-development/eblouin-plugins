# CI & review convergence

The canonical reference for taking a pushed branch to **CI green and review clean**, without the
human doing the shuttling. Orchestrating skills point here; a coding session runs this loop by
default on every feature.

Governing idea: **the orchestrator owns the loop, workers own the fixes.** Work is built on a
branch in the container, pushed when the worker believes it's ready, and from that moment the
conducting thread is responsible for driving it to green — watching the run, triaging failures,
delegating fixes, and re-entering the loop — until CI passes and the review has nothing blocking
left. The human is not the message bus between CI and the fixer.

Two failure modes this exists to prevent: a red branch sitting because everyone assumed someone
else was watching, and a loop that spins forever on a failure it can't fix. Both are handled by
the same discipline — watchdogs on everything, and a bounded number of rounds before escalation.

## The loop

```
build on branch (worker, container)
   └─ local gate: lint / type-check / tests green BEFORE pushing
push
   └─ CI runs ──► red ──► triage ──► fix worker ──► push ──► (CI again)
                  │
                green
                   └─ code review runs, comments posted on the PR
                        └─ blocking findings ──► fix worker ──► push ──► (CI again, then re-review)
                        └─ clean + green ──► PR ready, hand to the human
```

Rules that make it work:

- **Push when ready, not when finished-ish.** A worker runs the project's own lint, type-check and
  test commands in the container first. Pushing to let CI find what a local run would have caught
  wastes a whole CI cycle per mistake.
- **CI is a gate, not a notification.** A push that goes red is the orchestrator's problem
  immediately; it does not wait to be asked.
- **Green before review.** Don't spend a review pass on code CI has already rejected — the review's
  findings would be interleaved with build breakage and half of it may be rewritten anyway. Fix to
  green first, then review. (Exception: if CI is red for a reason clearly unrelated to the change —
  see "Triage" — reviewing in parallel is fine.)
- **Review comments land on the PR.** The final review posts per-finding comments with file:line
  and severity, plus one summary verdict comment, so the record is on the PR rather than in a
  thread the human can't see.
- **The orchestrator picks the review back up.** It reads the posted findings, decides what's
  blocking, and delegates those to a fix worker — it does not hand the human a list of comments to
  route.
- **Any fix re-enters at CI.** A fix push means CI runs again and the affected review points get
  re-checked. Never mark converged on the strength of a pre-fix green.
- **Bound every loop.** ~3 CI-fix rounds on the same failure, ~2 review-fix rounds without
  convergence, then stop and escalate with the diagnosis. A loop that isn't converging is signal.
- **Log what the loop decided.** Fixes made without the human — especially anything that changed
  behavior rather than just satisfying a check — go in the decision log the human reads at sign-off.

## Watchdogs on both halves

Every stage of this loop can go silent, and each needs its own backstop
(`${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` for the pattern):

- **Worker watchdog.** Every fix/build/review subagent is dispatched in the background with a
  watchdog sized to its expected duration. On fire: progressing → re-arm; silent → stop it, then
  re-dispatch; already done → proceed. Cap re-dispatches (~2) before escalating.
- **CI watchdog.** A CI run is *not* harness-tracked work — no completion notification is coming
  for it. After a push, register a wake-up sized to the pipeline's typical duration (use the
  repo's recent run times; a few minutes for a small pipeline), then check the run's status
  non-blockingly and re-arm with backoff while it's still running. Never busy-poll a run that
  takes minutes, and never block the conductor on one.
- **Queue stalls count as stalls.** A run that stays queued well past its normal window, or a push
  that produced no run at all, is a CI-availability problem — not something to keep waiting on.
  Treat it under "CI unavailable" below.

## Triage: whose failure is it?

Before delegating a fix, classify the red. Getting this wrong burns rounds.

- **Ours, deterministic** (test failure, lint/type error, build break caused by the diff) → fix
  worker, with the failing job's log excerpt and the command to reproduce locally in the brief.
- **Ours, flaky** (passes on re-run, timing/order-dependent) → don't paper over it with a re-run
  alone; a flaky test the change introduced is a real finding. A pre-existing flake gets one
  re-run and a note.
- **Not ours** (fails identically on the base branch, an upstream outage, a rate limit, an expired
  credential) → say so once on the PR, don't burn rounds fixing it, and re-check when the base
  recovers.
- **Infrastructure/CI itself down** (no runner, workflow can't start, the provider is unavailable)
  → switch to the local loop below.

## When CI is unavailable — the loop lives inside Claude

If CI is down, absent from the repo, or unreachable from the container, the loop does **not**
pause and does **not** get handed to the human. Run the equivalent gate locally, in the container,
and keep the same structure:

- **Reconstruct the gate from the pipeline definition.** Read the workflow file(s) and run the same
  commands the pipeline would — install, lint, type-check, unit/integration tests, build, and any
  security or workflow linting the project runs. If there's no pipeline to read, use the project's
  own documented commands (from `CLAUDE.md`, `package.json` scripts, `Makefile`, `pyproject.toml`).
- **Run it in a worker, not the conductor.** Same dispatch-plus-watchdog discipline; the local
  suite is just another worker whose output is a pass/fail plus failing excerpts.
- **Keep the same rounds and bounds.** Local red → fix worker → re-run locally → repeat, bounded
  the same way. Review still runs after local green, and its fixes still re-enter at the local
  gate.
- **Be explicit about what verified the change.** The PR and the sign-off package must say the
  gate was run locally and which checks ran — a human must never read "green" and assume CI
  produced it. Re-run through CI once it's back, before the PR is treated as merge-ready.
- **Note the CI outage itself.** It's a real problem for the repo even if it isn't this feature's
  problem; surface it rather than silently routing around it forever.

## Anti-patterns

- **Push-and-forget.** Leaving a red branch because the completion signal for a worker arrived and
  nobody was watching the run.
- **Handing CI failures to the human.** "CI is failing, what do you want to do?" is an escalation
  only after triage says it's not ours or the round budget is spent.
- **Reviewing a red branch** and then re-reviewing the rewrite — two passes for one review.
- **Re-running until green.** Retrying a failing job without a diagnosis hides real flakiness and
  burns rounds.
- **Blocking on CI.** Sitting in a foreground wait for a run instead of a watchdog-backed check.
- **Declaring converged from a stale green.** A green from before the last fix push proves nothing.
- **Stopping the loop because CI is down.** The gate moves into the container; the loop continues.

## See also

- `${CLAUDE_PLUGIN_ROOT}/shared/worker-cadence.md` — the dispatch → watchdog → stop-and-re-dispatch
  pattern every worker and the CI watch use.
- `${CLAUDE_PLUGIN_ROOT}/shared/parallelization.md` — running independent fixes concurrently.
- `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md` — the bar this loop converges on.
